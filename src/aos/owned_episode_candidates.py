"""Deterministic episode-content candidates from pinned real model inputs."""

import json
import re
import sqlite3
from typing import Literal

from pydantic import Field, field_validator

from .contracts import (Option, Phase, Prediction, State, TypedModel, canonical,
                        digest)
from .dataset import validator
from .decision import decision_request
from .owned_skill_planner import OwnedSkillPlan
from .owned_skill_planning import validate_planning_bundle


_HASH = re.compile(r'[a-f0-9]{64}\Z', re.ASCII)
_RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z', re.ASCII)
_EPISODE_ID = re.compile(r'episode-[a-f0-9]{32}\Z', re.ASCII)


class OwnedSystem1EpisodeSource(TypedModel):
    run_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    step_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    call_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    state_snapshot_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    planning_bundle_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    deployment_id: str = Field(min_length=1, max_length=256)
    deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class OwnedSystem2EpisodeSource(TypedModel):
    planning_id: str = Field(pattern=r'^planning-[a-f0-9]{32}$')
    planning_bundle_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    deployment_id: str = Field(min_length=1, max_length=256)
    deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class OwnedSystem1EpisodeInput(TypedModel):
    request: dict
    request_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class OwnedSystem2EpisodeInput(TypedModel):
    request: dict
    request_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class OwnedSystem1EpisodeCandidate(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    candidate_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    episode_id: str = Field(pattern=r'^episode-[a-f0-9]{32}$')
    consent_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_group_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    role: Literal['system1']
    source: OwnedSystem1EpisodeSource
    input: OwnedSystem1EpisodeInput
    prediction: Prediction
    reviewed: Literal[False]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('synthetic', mode='before')
    @classmethod
    def exact_true(cls, value):
        if value is not True:
            raise ValueError('owned_episode_candidate_boolean_invalid')
        return value

    @field_validator('reviewed', 'execution_authorized', 'collection_authorized',
                     'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError('owned_episode_candidate_cannot_claim_authority')
        return value


class OwnedSystem2EpisodeCandidate(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    candidate_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    episode_id: str = Field(pattern=r'^episode-[a-f0-9]{32}$')
    consent_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_group_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    role: Literal['system2']
    source: OwnedSystem2EpisodeSource
    input: OwnedSystem2EpisodeInput
    prediction: OwnedSkillPlan
    reviewed: Literal[False]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('synthetic', mode='before')
    @classmethod
    def exact_true(cls, value):
        if value is not True:
            raise ValueError('owned_episode_candidate_boolean_invalid')
        return value

    @field_validator('reviewed', 'execution_authorized', 'collection_authorized',
                     'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError('owned_episode_candidate_cannot_claim_authority')
        return value


def _candidate(model, values: dict) -> dict:
    content = {**values, 'schema_version': '1.0', 'synthetic': True,
               'reviewed': False, 'execution_authorized': False,
               'collection_authorized': False, 'activation_authorized': False,
               'training_ready': False}
    candidate_id = digest(content)
    candidate = model.model_validate({**content, 'candidate_id': candidate_id})
    result = candidate.model_dump(mode='json')
    validator('owned_episode_candidate').validate(result)
    if digest({key: value for key, value in result.items() if key != 'candidate_id'}) != candidate_id:
        raise ValueError('owned_episode_candidate_identity_changed')
    return result


def _valid_pins(episode_id: str, consent_sha256: str,
                source_group_sha256: str, planning_bundle_sha256: str | None = None) -> None:
    if (type(episode_id) is not str or _EPISODE_ID.fullmatch(episode_id) is None
            or any(type(value) is not str or _HASH.fullmatch(value) is None
                   for value in (consent_sha256, source_group_sha256))
            or planning_bundle_sha256 is not None
            and (type(planning_bundle_sha256) is not str
                 or _HASH.fullmatch(planning_bundle_sha256) is None)):
        raise ValueError('owned_episode_candidate_pins_invalid')


def _deployment_identity(connection: sqlite3.Connection, run: sqlite3.Row) -> dict:
    try:
        raw = run['deployment_snapshot_json']
        snapshot = json.loads(raw)
        identity = {key: value for key, value in snapshot.items() if key != 'supervisor'}
        if (not isinstance(snapshot, dict) or canonical(snapshot) != raw
                or identity.get('real_model') is not True
                or identity.get('kind') not in {'decider_native_worker', 'laya_candidate'}
                or not isinstance(identity.get('deployment_id'), str)
                or not isinstance(identity.get('pins'), dict) or not identity['pins']):
            raise ValueError('deployment_shape')
        prefix = {'decider_native_worker': 'decider-',
                  'laya_candidate': 'laya-candidate-'}[identity['kind']]
        if identity['deployment_id'] != prefix + digest(identity['pins']):
            raise ValueError('deployment_pin')
        deployment = connection.execute(
            'SELECT * FROM deployments WHERE deployment_id=?',
            (identity['deployment_id'],)).fetchone()
        if (deployment is None or deployment['status'] != 'EXPERIMENTAL'
                or deployment['config_json'] != canonical(identity['pins'])
                or deployment['config_sha256'] != digest(identity['pins'])):
            raise ValueError('deployment_registry')
        return identity
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError('owned_episode_candidate_deployment_unverified') from error


def _one(rows, reason: str):
    if len(rows) != 1:
        raise ValueError(reason)
    return rows[0]


def derive_system1_candidates(connection: sqlite3.Connection, run_id: str, *,
                              episode_id: str, consent_sha256: str,
                              planning_bundle_sha256: str,
                              source_group_sha256: str) -> list[dict]:
    """Derive content-only S1 inputs/predictions from exact DECIDE records."""
    _valid_pins(episode_id, consent_sha256, source_group_sha256,
                planning_bundle_sha256)
    if (not isinstance(connection, sqlite3.Connection) or type(run_id) is not str
            or _RUN_ID.fullmatch(run_id) is None):
        raise ValueError('owned_episode_candidate_source_invalid')
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError('owned_episode_candidate_run_missing')
    identity = _deployment_identity(connection, run)
    calls = connection.execute(
        "SELECT * FROM model_calls WHERE run_id=? AND role='system1' AND status='ok' "
        'ORDER BY created_at,call_id', (run_id,)).fetchall()
    results = []
    seen_decisions = set()
    for call in calls:
        if (call['deployment_id'] != identity['deployment_id']
                or call['response_json'] is None or call['request_json'] is None):
            raise ValueError('owned_episode_candidate_model_call_unbound')
        decisions = connection.execute(
            'SELECT * FROM decisions WHERE run_id=? AND step_id=? AND call_id=?',
            (run_id, call['step_id'], call['call_id'])).fetchall()
        decision = _one(decisions, 'owned_episode_candidate_decision_ambiguous')
        if decision['decision_id'] in seen_decisions:
            raise ValueError('owned_episode_candidate_decision_duplicate')
        seen_decisions.add(decision['decision_id'])
        snapshots = connection.execute(
            'SELECT * FROM state_snapshots WHERE snapshot_id=? AND run_id=? AND step_id=?',
            (decision['snapshot_id'], run_id, call['step_id'])).fetchall()
        snapshot = _one(snapshots, 'owned_episode_candidate_decision_state_missing')
        state = State.model_validate_json(snapshot['state_json'])
        if (state.model_dump_json() != snapshot['state_json']
                or snapshot['content_sha256'] != digest(state.model_dump(mode='json'))
                or type(snapshot['state_version']) is not int
                or snapshot['state_version'] != state.state_version
                or state.phase != Phase.DECIDE or state.run_id != run_id
                or state.task_id != run['task_id'] or state.step_id != call['step_id']
                or state.deployment_id != identity['deployment_id']
                or not state.runtime_id or not state.owner_lease_id):
            raise ValueError('owned_episode_candidate_decision_state_changed')
        options_raw = json.loads(decision['options_json'])
        if not isinstance(options_raw, list):
            raise ValueError('owned_episode_candidate_options_invalid')
        options = [Option.model_validate(option) for option in options_raw]
        if [option.model_dump(mode='json') for option in options] != options_raw:
            raise ValueError('owned_episode_candidate_options_noncanonical')
        request = decision_request(state, options)
        prediction = Prediction.model_validate_json(call['response_json'])
        prediction.validate_options(options)
        try:
            stored_request = json.loads(call['request_json'])
            probabilities = json.loads(decision['probabilities_json'])
        except (TypeError, ValueError, RecursionError):
            raise ValueError('owned_episode_candidate_model_io_invalid') from None
        if (canonical(stored_request) != canonical(request)
                or decision['question'] != request['question']
                or decision['selected_option'] != prediction.selected_option
                or canonical(probabilities) != canonical(prediction.probabilities)
                or decision['confidence'] != prediction.probabilities[prediction.selected_option]):
            raise ValueError('owned_episode_candidate_model_io_changed')
        source = {
            'run_ref': digest({'run_id': run_id}),
            'step_ref': digest({'step_id': call['step_id']}),
            'call_ref': digest({'call_id': call['call_id']}),
            'decision_ref': digest({'decision_id': decision['decision_id']}),
            'state_snapshot_ref': digest({'snapshot_id': snapshot['snapshot_id']}),
            'planning_bundle_sha256': planning_bundle_sha256,
            'deployment_id': identity['deployment_id'],
            'deployment_sha256': digest(identity),
        }
        content = {
            'episode_id': episode_id, 'consent_sha256': consent_sha256,
            'source_group_sha256': source_group_sha256, 'role': 'system1',
            'source': source,
            'input': {'request': request, 'request_sha256': digest(request)},
            'prediction': prediction.model_dump(mode='json'),
        }
        results.append(_candidate(OwnedSystem1EpisodeCandidate, content))
    return results


def derive_system2_candidate(bundle: dict, *, episode_id: str,
                             consent_sha256: str,
                             source_group_sha256: str) -> dict:
    """Project one validated real Bonsai selected-skill planning bundle."""
    _valid_pins(episode_id, consent_sha256, source_group_sha256)
    try:
        validate_planning_bundle(bundle, planner=None)
        if (bundle.get('real_model') is not True
                or bundle.get('purpose') != 'owned_selected_skill_planning'
                or bundle.get('execution_authorized') is not False
                or bundle.get('activation_authorized') is not False
                or bundle.get('training_ready') is not False
                or bundle.get('downstream_verified') is not False):
            raise ValueError('planning_bundle_scope')
        if ('episode_id' in bundle and bundle['episode_id'] != episode_id
                or 'collection_consent_sha256' in bundle
                and bundle['collection_consent_sha256'] != consent_sha256):
            raise ValueError('planning_bundle_consent_changed')
        deployment = bundle['deployment']
        if (deployment.get('real_model') is not True
                or deployment.get('kind') != 'bonsai_native_owned_skill_planner'
                or deployment.get('deployment_id') != 'bonsai-' + digest(deployment['pins'])):
            raise ValueError('planning_bundle_deployment_unverified')
        planning_id = bundle['planning_id']
        request = bundle['model_request']
        prediction = OwnedSkillPlan.model_validate_json(canonical(bundle['model_response']))
        if (type(planning_id) is not str
                or re.fullmatch(r'planning-[a-f0-9]{32}', planning_id) is None
                or not isinstance(request, dict)):
            raise ValueError('planning_bundle_source_invalid')
        source = {
            'planning_id': planning_id,
            'planning_bundle_sha256': digest(bundle),
            'deployment_id': deployment['deployment_id'],
            'deployment_sha256': digest(deployment),
        }
        content = {
            'episode_id': episode_id, 'consent_sha256': consent_sha256,
            'source_group_sha256': source_group_sha256, 'role': 'system2',
            'source': source,
            'input': {'request': request, 'request_sha256': digest(request)},
            'prediction': prediction.model_dump(mode='json'),
        }
        return _candidate(OwnedSystem2EpisodeCandidate, content)
    except Exception as error:
        raise ValueError('owned_episode_candidate_system2_source_invalid') from error
