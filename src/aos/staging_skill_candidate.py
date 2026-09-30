"""Read-only, content-free S1 candidate from one completed synthetic staging run."""

import argparse
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import Field, ValidationError

from .contracts import (AOSFault, Action, Option, Phase, Prediction, STAGING_WORKFLOW_GOAL,
                        STAGING_WORKFLOW_MESSAGE, STAGING_WORKFLOW_SCOPE, State, TypedModel,
                        canonical, digest)
from .dataset_audit import audit_snapshot
from .dataset_reviews import source_fingerprint
from .decision import decision_request
from .desktop_tasks import Approval
from .staging_workflow_operator import EXPECTED_OUTCOMES, STAGES, staging_outcome


RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
SHA256 = re.compile(r'[a-f0-9]{64}\Z')
EFFECT_TOOLS = tuple(stage[0] for stage in STAGES)


class StageLineage(TypedModel):
    ordinal: int = Field(ge=0, le=3)
    stage: Literal['open_app', 'follow_draft', 'fill_message', 'submit_draft']
    state_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    call_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    action_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    approval_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    verification_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    outcome_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class StagingSkillCandidate(TypedModel):
    schema_version: Literal['1.0']
    candidate_version: Literal['synthetic-staging-s1-v1']
    status: Literal['unreviewed_candidate']
    model_role: Literal['system1']
    model_kind: Literal['decider_native_worker', 'laya_candidate']
    run_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    job_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    workflow_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    steps: list[StageLineage] = Field(min_length=4, max_length=4)
    approved_action_count: Literal[4]
    independent_verification_count: Literal[4]
    observed_submission_count: Literal[1]
    synthetic: Literal[True]
    profile_bound: Literal[False]
    reviewed: Literal[False]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def one(rows: list[sqlite3.Row], reason: str) -> sqlite3.Row:
    require(len(rows) == 1, reason)
    return rows[0]


def json_value(value: str):
    return json.loads(value)


def derive_stage(connection: sqlite3.Connection, run: sqlite3.Row, job: sqlite3.Row,
                 identity: dict, action: sqlite3.Row, pre_observation: sqlite3.Row,
                 ordinal: int) -> StageLineage:
    tool, option, label, summary = STAGES[ordinal]
    require(action['tool'] == tool and action['status'] == 'ok' and action['actual_option'] == option,
            'staging_action_order_or_outcome_differs')
    decision = one(connection.execute('SELECT * FROM decisions WHERE decision_id=? AND run_id=? AND step_id=?',
                                      (action['decision_id'], run['run_id'], action['step_id'])).fetchall(),
                   'staging_decision_missing')
    call = one(connection.execute('SELECT * FROM model_calls WHERE call_id=? AND run_id=? AND step_id=?',
                                  (decision['call_id'], run['run_id'], action['step_id'])).fetchall(),
               'staging_model_call_missing')
    snapshot = one(connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=? AND run_id=? AND step_id=?',
                                      (decision['snapshot_id'], run['run_id'], action['step_id'])).fetchall(),
                   'staging_decision_state_missing')
    state = State.model_validate_json(snapshot['state_json'])
    require(snapshot['content_sha256'] == digest(state.model_dump(mode='json'))
            and state.phase == Phase.DECIDE and state.task_kind == 'browser_staging_workflow'
            and state.run_id == run['run_id'] and state.task_id == run['task_id']
            and state.step_id == action['step_id'] and state.state_version == snapshot['state_version']
            and state.deployment_id == identity['deployment_id']
            and state.runtime_id == job['runtime_id'] and state.owner_lease_id == job['lease_id']
            and state.authorized_path == STAGING_WORKFLOW_SCOPE
            and state.authorized_content == STAGING_WORKFLOW_MESSAGE
            and state.normalized_goal == STAGING_WORKFLOW_GOAL
            and state.observation == summary,
            'staging_decision_state_differs')
    require(pre_observation['run_id'] == run['run_id']
            and pre_observation['step_id'] == action['step_id']
            and pre_observation['kind'] == 'browser.local_navigation'
            and pre_observation['action_id'] is None,
            'staging_preaction_observation_missing')
    prior = json_value(pre_observation['payload_json'])
    if ordinal == 0:
        require(prior == {'page': 'entry', 'value': '', 'receipt': '', 'submissions': 0},
                'staging_entry_observation_differs')
    else:
        require(staging_outcome(prior) == EXPECTED_OUTCOMES[ordinal - 1]
                and isinstance(prior['snapshot_id'], str)
                and re.fullmatch('[a-f0-9]{32}', prior['snapshot_id']) is not None,
                'staging_preaction_observation_differs')
    options = [Option.model_validate(value) for value in json_value(decision['options_json'])]
    require(options == [Option(id=option, label=label), Option(id='ask_human', label='Ask the user for help')],
            'staging_finite_options_differ')
    prediction = Prediction(selected_option=decision['selected_option'],
                            probabilities=json_value(decision['probabilities_json']))
    prediction.validate_options(options)
    request = decision_request(state, options)
    require(decision['question'] == request['question'] and decision['selected_option'] == option
            and decision['policy_result'] == 'allow'
            and decision['confidence'] == prediction.probabilities[option]
            and call['role'] == 'system1' and call['status'] == 'ok'
            and call['deployment_id'] == identity['deployment_id']
            and json_value(call['request_json']) == request
            and json_value(call['response_json']) == prediction.model_dump(),
            'staging_model_decision_input_or_prediction_differs')

    envelope = one(connection.execute('SELECT * FROM action_envelopes WHERE action_id=?',
                                      (action['action_id'],)).fetchall(), 'staging_action_envelope_missing')
    effect = Action.model_validate_json(envelope['envelope_json'])
    require(envelope['payload_sha256'] == digest(effect.model_dump(mode='json'))
            and effect.action_id == action['action_id'] and effect.run_id == run['run_id']
            and effect.task_id == run['task_id'] and effect.step_id == action['step_id']
            and effect.runtime_id == job['runtime_id'] and effect.owner_lease_id == job['lease_id']
            and effect.tool == tool and effect.selected_option == option
            and effect.verification == 'independent_staging_state_equals'
            and effect.arguments == json_value(action['arguments_json'])
            and effect.idempotency_key == action['idempotency_key']
            and effect.state_version > state.state_version,
            'staging_action_envelope_differs')
    if ordinal == 0:
        require(effect.arguments == {}, 'staging_open_arguments_differ')
    else:
        require(set(effect.arguments) == ({'snapshot_id', 'element_id', 'value'} if ordinal == 2
                 else {'snapshot_id', 'element_id'})
                and all(isinstance(effect.arguments[key], str) and re.fullmatch('[a-f0-9]{32}', effect.arguments[key])
                        for key in ('snapshot_id', 'element_id'))
                and effect.arguments['snapshot_id'] == prior['snapshot_id']
                and effect.arguments['element_id'] == prior['elements'][0 if ordinal in (1, 2) else 1]['element_id']
                and (ordinal != 2 or effect.arguments['value'] == 'Hello from the local agent.'),
                'staging_action_arguments_differ')

    approval = one(connection.execute('SELECT * FROM desktop_approvals WHERE job_id=? AND action_sha256=?',
                                      (job['job_id'], envelope['payload_sha256'])).fetchall(),
                   'staging_manual_approval_missing')
    approved = Approval.model_validate_json(approval['envelope_json'])
    intervention = one(connection.execute('SELECT * FROM human_interventions WHERE run_id=? AND step_id=? '
                                          "AND kind='approve' AND json_extract(payload_json,'$.approval_id')=?",
                                          (run['run_id'], action['step_id'], approval['approval_id'])).fetchall(),
                       'staging_manual_intervention_missing')
    require(approval['status'] == 'consumed' and approved.approval_id == approval['approval_id']
            and approved.job_id == job['job_id'] and approved.action_sha256 == envelope['payload_sha256']
            and approved.action == effect and approved.expires_at == approval['expires_at']
            and intervention['actor'] == 'local_authenticated_user'
            and json_value(intervention['payload_json']) == {
                'approval_id': approval['approval_id'], 'action_sha256': envelope['payload_sha256']},
            'staging_manual_approval_differs')

    verification = one(connection.execute('SELECT * FROM verifications WHERE run_id=? AND step_id=? AND action_id=?',
                                          (run['run_id'], action['step_id'], action['action_id'])).fetchall(),
                       'staging_verification_missing')
    expected = EXPECTED_OUTCOMES[ordinal]
    require(verification['result'] == 'passed'
            and verification['method'] == 'independent_staging_state_equals'
            and verification['verifier'] == 'aos-synthetic-staging-v1'
            and json_value(verification['expected_json']) == expected
            and json_value(verification['actual_json']) == expected,
            'staging_verification_outcome_differs')
    evidence_ids = json_value(verification['evidence_refs_json'])
    require(isinstance(evidence_ids, list) and len(evidence_ids) == 1 and isinstance(evidence_ids[0], str),
            'staging_independent_evidence_missing')
    evidence = one(connection.execute('SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
                                      (evidence_ids[0], run['run_id'], action['step_id'])).fetchall(),
                   'staging_independent_evidence_missing')
    readback = one(connection.execute('SELECT * FROM actions WHERE action_id=? AND run_id=? AND step_id=?',
                                      (evidence['action_id'], run['run_id'], action['step_id'])).fetchall(),
                   'staging_independent_readback_missing')
    observed = json_value(evidence['payload_json'])
    require(readback['action_id'] != action['action_id'] and readback['tool'] == 'browser.staging.snapshot'
            and readback['status'] == 'ok' and readback['decision_id'] == decision['decision_id']
            and readback['actual_option'] == option and json_value(readback['arguments_json']) == {}
            and json_value(readback['result_json']) == observed
            and staging_outcome(json_value(action['result_json'])) == expected
            and json_value(action['result_json'])['snapshot_id'] != observed['snapshot_id']
            and evidence['kind'] == 'browser.local_navigation'
            and staging_outcome(observed) == expected,
            'staging_independent_readback_differs')
    return StageLineage(ordinal=ordinal, stage=option,
                        state_sha256=snapshot['content_sha256'], input_sha256=digest(request),
                        call_ref=digest({'call_id': call['call_id']}),
                        decision_ref=digest({'decision_id': decision['decision_id']}),
                        action_ref=digest({'action_id': action['action_id']}),
                        approval_ref=digest({'approval_id': approval['approval_id']}),
                        verification_ref=digest({'verification_id': verification['verification_id']}),
                        outcome_sha256=digest(expected))


def derive_staging_skill_candidate(database: Path, run_id: str,
                                   *, snapshot_sha256: str | None = None) -> dict:
    require(isinstance(run_id, str) and RUN_ID.fullmatch(run_id) is not None, 'invalid_staging_run_id')
    require(snapshot_sha256 is None or (isinstance(snapshot_sha256, str)
                                        and SHA256.fullmatch(snapshot_sha256) is not None),
            'invalid_snapshot_selection')
    with audit_snapshot(database) as (connection, snapshot):
        require(snapshot_sha256 is None or snapshot_sha256 == snapshot['sha256'], 'staging_snapshot_changed')
        run = one(connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchall(), 'staging_run_missing')
        job = one(connection.execute('SELECT * FROM desktop_tasks WHERE run_id=?', (run_id,)).fetchall(),
                  'staging_job_missing')
        require(run['policy_version'] == 'browser-staging-workflow-policy-v1'
                and run['status'] == 'succeeded' and run['outcome'] == 'passed'
                and run['training_eligible'] == 0
                and job['kind'] == 'browser_staging_workflow' and job['status'] == 'succeeded'
                and job['real_model'] == 1 and job['runtime_id'] is not None,
                'staging_source_not_completed_real_s1')
        identity = json_value(run['deployment_snapshot_json'])
        environment = json_value(run['environment_json'])
        require(isinstance(identity, dict) and isinstance(environment, dict)
                and identity.get('real_model') is True
                and identity.get('kind') in {'decider_native_worker', 'laya_candidate'}
                and isinstance(identity.get('deployment_id'), str)
                and isinstance(identity.get('pins'), dict) and identity['pins']
                and environment.get('kind') == 'docker_chromium_mcp'
                and environment.get('browser_transport') == 'playwright_mcp'
                and environment.get('fixture_request_guard') is True
                and environment.get('staging_workflow') is True
                and environment.get('runtime_id') == job['runtime_id']
                and environment.get('network') is False
                and isinstance(environment.get('isolation'), dict)
                and environment['isolation'].get('staging_workflow') is True
                and isinstance(environment['isolation'].get('fixture_request_guard'), dict)
                and environment['isolation']['fixture_request_guard'].get('verified') is True,
                'staging_deployment_or_guard_unbound')
        actions = connection.execute('SELECT * FROM actions WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
        effects = [action for action in actions if action['tool'] in EFFECT_TOOLS]
        readbacks = [action for action in actions if action['tool'] == 'browser.staging.snapshot']
        observations = connection.execute('SELECT * FROM observations WHERE run_id=? ORDER BY rowid',
                                          (run_id,)).fetchall()
        pre_observations = [observation for observation in observations if observation['action_id'] is None]
        require(len(actions) == 8 and len(effects) == len(readbacks) == 4
                and len(observations) == 8 and len(pre_observations) == 4
                and [action['tool'] for action in actions] == [tool for effect in EFFECT_TOOLS
                                                             for tool in (effect, 'browser.staging.snapshot')]
                and connection.execute('SELECT count(*) FROM model_calls WHERE run_id=?', (run_id,)).fetchone()[0] == 4
                and connection.execute('SELECT count(*) FROM decisions WHERE run_id=?', (run_id,)).fetchone()[0] == 4
                and connection.execute('SELECT count(*) FROM verifications WHERE run_id=?', (run_id,)).fetchone()[0] == 4
                and connection.execute('SELECT count(*) FROM desktop_approvals WHERE job_id=?',
                                       (job['job_id'],)).fetchone()[0] == 4
                and connection.execute('SELECT count(*) FROM human_interventions WHERE run_id=?',
                                       (run_id,)).fetchone()[0] == 4
                and connection.execute('SELECT count(*) FROM supervisor_escalations WHERE run_id=?',
                                       (run_id,)).fetchone()[0] == 0,
                'staging_four_step_cardinality_differs')
        steps = [derive_stage(connection, run, job, identity, action,
                              pre_observations[ordinal], ordinal)
                 for ordinal, action in enumerate(effects)]
        require(len({step.call_ref for step in steps}) == 4
                and len({step.decision_ref for step in steps}) == 4
                and len({step.approval_ref for step in steps}) == 4
                and len({step.verification_ref for step in steps}) == 4
                and json_value(effects[-1]['result_json'])['submissions'] == 1,
                'staging_source_reused_or_submission_missing')
        report = StagingSkillCandidate(
            schema_version='1.0', candidate_version='synthetic-staging-s1-v1',
            status='unreviewed_candidate', model_role='system1',
            model_kind=identity['kind'], run_ref=digest({'run_id': run_id}),
            job_ref=digest({'job_id': job['job_id']}), snapshot_sha256=snapshot['sha256'],
            source_sha256=source_fingerprint(connection, run_id),
            deployment_sha256=digest(identity),
            workflow_sha256=digest({'stages': STAGES, 'outcomes': EXPECTED_OUTCOMES}),
            steps=steps, approved_action_count=4, independent_verification_count=4,
            observed_submission_count=1, synthetic=True, profile_bound=False,
            reviewed=False, execution_authorized=False, collection_authorized=False,
            activation_authorized=False, training_ready=False)
        return report.model_dump()


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Synthetic staging skill candidate unavailable: invalid arguments.\n')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Read-only synthetic S1 staging skill candidate')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--snapshot-sha256')
    arguments = parser.parse_args(argv)
    try:
        report = derive_staging_skill_candidate(arguments.database, arguments.run_id,
                                                snapshot_sha256=arguments.snapshot_sha256)
    except (AOSFault, OSError, sqlite3.Error, ValueError, TypeError, KeyError,
            IndexError, RecursionError, ValidationError):
        parser.exit(1, 'Synthetic staging skill candidate unavailable: source is missing, changed or invalid.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
