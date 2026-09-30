"""Read-only synthetic S2 scene candidate with an explicitly downstream S1 outcome."""

import argparse
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import Field, ValidationError

from .contracts import AOSFault, Action, Option, Phase, Prediction, State, TypedModel, canonical, digest
from .dataset_audit import audit_snapshot
from .dataset_reviews import source_fingerprint
from .decision import decision_request
from .desktop_tasks import Approval
from .learning_events import project_learning_events
from .vision import CaptureEvidence, VISION_EXPECTED, VISION_SCOPE, VisionScene


RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
SHA256 = re.compile(r'[a-f0-9]{64}\Z')


class VisionSkillCandidate(TypedModel):
    schema_version: Literal['1.0']
    candidate_version: Literal['synthetic-canvas-s2-v1']
    status: Literal['unreviewed_downstream_linked']
    model_role: Literal['system2']
    model_kind: Literal['bonsai_native_supervisor']
    run_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    job_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    supervisor_deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    operator_deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    capture_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    scene_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    supervisor_call_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    operator_decision_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved_action_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    downstream_verification_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    downstream_outcome_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    synthetic: Literal[True]
    supervisor_outcome_verified: Literal[False]
    downstream_operator_outcome_verified: Literal[True]
    profile_bound: Literal[False]
    reviewed: Literal[False]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]


class VisionOperatorCandidate(TypedModel):
    model_role: Literal['system1']
    model_kind: Literal['decider_native_worker']
    deployment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved_action_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    verification_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    outcome_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    outcome_verified: Literal[True]
    reviewed: Literal[False]
    training_ready: Literal[False]


class VisionDualRoleCandidate(TypedModel):
    schema_version: Literal['1.0']
    candidate_version: Literal['synthetic-canvas-dual-role-v1']
    status: Literal['unreviewed_pair']
    run_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    job_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    operator: VisionOperatorCandidate
    supervisor: VisionSkillCandidate
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


def exactly_one(rows, reason: str):
    rows = list(rows)
    require(len(rows) == 1, reason)
    return rows[0]


def derive_vision_skill_candidate(database: Path, run_id: str,
                                  *, snapshot_sha256: str | None = None) -> dict:
    require(isinstance(run_id, str) and RUN_ID.fullmatch(run_id) is not None, 'invalid_vision_run_id')
    require(snapshot_sha256 is None or (isinstance(snapshot_sha256, str)
                                        and SHA256.fullmatch(snapshot_sha256) is not None),
            'invalid_snapshot_selection')
    with audit_snapshot(database) as (connection, snapshot):
        require(snapshot_sha256 is None or snapshot_sha256 == snapshot['sha256'], 'vision_snapshot_changed')
        run = exactly_one(connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)), 'vision_run_missing')
        job = exactly_one(connection.execute('SELECT * FROM desktop_tasks WHERE run_id=?', (run_id,)),
                          'vision_job_missing')
        require(run['policy_version'] == 'vision-canvas-policy-v1' and run['status'] == 'succeeded'
                and run['outcome'] == 'passed' and run['training_eligible'] == 0
                and job['kind'] == 'vision_canvas' and job['status'] == 'succeeded'
                and job['real_model'] == 1 and job['runtime_id'] is not None,
                'vision_source_not_completed_real_task')
        identity = json.loads(run['deployment_snapshot_json'])
        require(isinstance(identity, dict) and isinstance(identity.get('supervisor'), dict)
                and identity.get('real_model') is True
                and identity.get('kind') == 'decider_native_worker'
                and isinstance(identity.get('pins'), dict) and identity['pins']
                and identity['supervisor'].get('real_model') is True
                and identity['supervisor'].get('kind') == 'bonsai_native_supervisor'
                and isinstance(identity['supervisor'].get('pins'), dict) and identity['supervisor']['pins'],
                'vision_deployment_unbound')
        events = project_learning_events(connection, run_id)
        require(len(events) == 2 and {event['role'] for event in events} == {'system1', 'system2'},
                'vision_model_events_missing_or_extra')
        operator = next(event for event in events if event['role'] == 'system1')
        supervisor = next(event for event in events if event['role'] == 'system2')
        source = supervisor['source']
        verification_ids = source['downstream_verification_ids']
        require(not supervisor['verified_outcome'] and operator['verified_outcome']
                and source['scene_observation_id'] is not None and len(verification_ids) == 1
                and operator['source']['verification_ids'] == verification_ids
                and source['step_id'] == operator['source']['step_id']
                and source['deployment_sha256'] == digest(identity['supervisor'])
                and operator['source']['deployment_sha256'] == digest({
                    key: value for key, value in identity.items() if key != 'supervisor'}),
                'vision_scene_or_downstream_link_missing')
        calls = list(connection.execute('SELECT * FROM model_calls WHERE run_id=?', (run_id,)))
        decisions = list(connection.execute('SELECT * FROM decisions WHERE run_id=?', (run_id,)))
        actions = list(connection.execute('SELECT * FROM actions WHERE run_id=? ORDER BY rowid', (run_id,)))
        require(len(calls) == 2 and len(decisions) == 1 and len(actions) == 2
                and [action['tool'] for action in actions] == ['vision.click', 'vision.verify']
                and connection.execute('SELECT count(*) FROM verifications WHERE run_id=?', (run_id,)).fetchone()[0] == 1
                and connection.execute('SELECT count(*) FROM desktop_approvals WHERE job_id=?',
                                       (job['job_id'],)).fetchone()[0] == 1
                and connection.execute('SELECT count(*) FROM human_interventions WHERE run_id=?',
                                       (run_id,)).fetchone()[0] == 1,
                'vision_one_action_cardinality_differs')
        supervisor_call = exactly_one((call for call in calls if call['call_id'] == source['call_id']
                                       and call['role'] == 'system2' and call['status'] == 'ok'
                                       and call['step_id'] == source['step_id']
                                       and call['deployment_id'] == identity['supervisor']['deployment_id']),
                                      'vision_supervisor_call_missing')
        scene = VisionScene.model_validate_json(supervisor_call['response_json'])
        require(not scene.needs_human, 'vision_scene_abstained')
        scene_observation = exactly_one(connection.execute(
            'SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
            (source['scene_observation_id'], run_id, source['step_id'])), 'vision_scene_observation_missing')
        require(scene_observation['kind'] == 'vision.scene' and scene_observation['action_id'] is None
                and json.loads(scene_observation['payload_json']) == scene.model_dump(),
                'vision_scene_observation_differs')
        request = json.loads(supervisor_call['request_json'])
        capture = exactly_one(connection.execute(
            "SELECT * FROM observations WHERE run_id=? AND step_id=? AND kind='vision.capture' AND action_id IS NULL",
            (run_id, source['step_id'])), 'vision_capture_observation_missing')
        capture_evidence = CaptureEvidence.model_validate_json(capture['payload_json'])
        artifact = exactly_one(connection.execute(
            'SELECT * FROM artifacts WHERE artifact_id=? AND run_id=? AND step_id=?',
            (capture_evidence.artifact_id, run_id, source['step_id'])), 'vision_capture_artifact_missing')
        require(request == {'purpose': 'synthetic_canvas_vision',
                            'capture': capture_evidence.model_dump(exclude={'state_version', 'artifact_id'}),
                            'state_version': capture_evidence.state_version,
                            'artifact_id': capture_evidence.artifact_id}
                and capture_evidence.capture_id == scene.capture_id
                and capture_evidence.state_version == scene.state_version
                and artifact['sha256'] == capture_evidence.sha256 and artifact['media_type'] == 'image/png',
                'vision_capture_call_binding_differs')
        decision = decisions[0]
        require(decision['decision_id'] == operator['source']['decision_id']
                and decision['call_id'] == operator['source']['call_id']
                and decision['step_id'] == source['step_id'],
                'vision_operator_decision_missing')
        state_row = exactly_one(connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=? AND run_id=?',
                                                   (decision['snapshot_id'], run_id)), 'vision_operator_state_missing')
        state = State.model_validate_json(state_row['state_json'])
        require(state_row['content_sha256'] == digest(state.model_dump(mode='json'))
                and state.phase == Phase.DECIDE and state.task_kind == 'vision_canvas'
                and state.run_id == run_id and state.task_id == run['task_id']
                and state.step_id == source['step_id']
                and state.state_version == state_row['state_version']
                and state.runtime_id == job['runtime_id'] and state.owner_lease_id == job['lease_id']
                and state.deployment_id == identity['deployment_id']
                and state.supervisor_deployment_id == identity['supervisor']['deployment_id']
                and state.authorized_path == VISION_SCOPE and state.authorized_content == 'SAVE'
                and state.capture_id == scene.capture_id and state.scene_sha256 == digest(scene.model_dump()),
                'vision_operator_state_differs')
        targets = sorted(scene.targets().items(), key=lambda item: item[1].label != 'SAVE')
        options = [Option(id=element_id, label='Click the visible ' + element.label + ' button')
                   for element_id, element in targets]
        options.append(Option(id='ask_human', label='Ask the user for help without clicking'))
        prediction = Prediction(selected_option=decision['selected_option'],
                                probabilities=json.loads(decision['probabilities_json']))
        prediction.validate_options(options)
        operator_call = exactly_one((call for call in calls if call['call_id'] == decision['call_id']
                                     and call['role'] == 'system1' and call['status'] == 'ok'
                                     and call['step_id'] == source['step_id']
                                     and call['deployment_id'] == identity['deployment_id']),
                                    'vision_operator_call_missing')
        expected_request = decision_request(state, options)
        require(json.loads(decision['options_json']) == [option.model_dump() for option in options]
                and decision['question'] == expected_request['question']
                and decision['selected_option'] == targets[0][0]
                and decision['confidence'] == prediction.probabilities[targets[0][0]]
                and decision['policy_result'] == 'allow'
                and json.loads(operator_call['request_json']) == expected_request
                and json.loads(operator_call['response_json']) == prediction.model_dump(),
                'vision_operator_decision_differs')
        action, readback = actions
        verification = exactly_one(connection.execute(
            'SELECT * FROM verifications WHERE verification_id=? AND run_id=?',
            (verification_ids[0], run_id)), 'vision_downstream_verification_missing')
        require(action['status'] == readback['status'] == 'ok'
                and action['decision_id'] == readback['decision_id'] == decision['decision_id']
                and action['step_id'] == readback['step_id'] == source['step_id']
                and action['actual_option'] == readback['actual_option'] == targets[0][0]
                and readback['action_id'] != action['action_id']
                and json.loads(readback['arguments_json']) == {}
                and json.loads(readback['result_json']) == VISION_EXPECTED
                and verification['action_id'] == action['action_id']
                and verification['step_id'] == source['step_id']
                and verification['method'] == 'independent_canvas_equals'
                and verification['verifier'] == 'aos-visual-canvas-v1'
                and verification['criterion'] == state.success_criteria[0]
                and verification['result'] == 'passed'
                and json.loads(verification['expected_json']) == VISION_EXPECTED
                and json.loads(verification['actual_json']) == VISION_EXPECTED,
                'vision_downstream_action_differs')
        evidence_ids = json.loads(verification['evidence_refs_json'])
        require(isinstance(evidence_ids, list) and len(evidence_ids) == 1
                and isinstance(evidence_ids[0], str), 'vision_independent_evidence_missing')
        evidence = exactly_one(connection.execute(
            'SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
            (evidence_ids[0], run_id, source['step_id'])), 'vision_independent_evidence_missing')
        require(evidence['kind'] == 'vision.outcome' and evidence['action_id'] == readback['action_id']
                and json.loads(evidence['payload_json']) == VISION_EXPECTED,
                'vision_independent_evidence_differs')
        envelope = exactly_one(connection.execute('SELECT * FROM action_envelopes WHERE action_id=?',
                                                  (action['action_id'],)), 'vision_action_envelope_missing')
        effect = Action.model_validate_json(envelope['envelope_json'])
        require(envelope['payload_sha256'] == digest(effect.model_dump(mode='json'))
                and effect.action_id == action['action_id'] and effect.run_id == run_id
                and effect.task_id == run['task_id'] and effect.step_id == source['step_id']
                and effect.tool == 'vision.click' and effect.selected_option == targets[0][0]
                and effect.verification == 'independent_canvas_equals'
                and effect.arguments == {'capture_id': scene.capture_id,
                                         'scene_sha256': digest(scene.model_dump()),
                                         'element_id': targets[0][0]}
                and json.loads(action['arguments_json']) == effect.arguments
                and action['idempotency_key'] == effect.idempotency_key
                and effect.runtime_id == job['runtime_id'] and effect.owner_lease_id == job['lease_id'],
                'vision_action_envelope_differs')
        approval = exactly_one(connection.execute(
            'SELECT * FROM desktop_approvals WHERE job_id=? AND action_sha256=?',
            (job['job_id'], envelope['payload_sha256'])), 'vision_manual_approval_missing')
        approved = Approval.model_validate_json(approval['envelope_json'])
        intervention = exactly_one(connection.execute(
            "SELECT * FROM human_interventions WHERE run_id=? AND step_id=? AND kind='approve'",
            (run_id, source['step_id'])), 'vision_manual_intervention_missing')
        require(approval['status'] == 'consumed' and approved.approval_id == approval['approval_id']
                and approved.job_id == job['job_id'] and approved.action_sha256 == envelope['payload_sha256']
                and approved.action == effect and approved.expires_at == approval['expires_at']
                and intervention['actor'] == 'local_authenticated_user'
                and json.loads(intervention['payload_json']) == {
                    'approval_id': approval['approval_id'], 'action_sha256': envelope['payload_sha256']},
                'vision_manual_approval_differs')
        report = VisionSkillCandidate(
            schema_version='1.0', candidate_version='synthetic-canvas-s2-v1',
            status='unreviewed_downstream_linked', model_role='system2',
            model_kind='bonsai_native_supervisor', run_ref=digest({'run_id': run_id}),
            job_ref=digest({'job_id': job['job_id']}), snapshot_sha256=snapshot['sha256'],
            source_sha256=source_fingerprint(connection, run_id),
            supervisor_deployment_sha256=digest(identity['supervisor']),
            operator_deployment_sha256=digest({key: value for key, value in identity.items()
                                               if key != 'supervisor'}),
            capture_sha256=capture_evidence.sha256, scene_sha256=digest(scene.model_dump()),
            supervisor_call_ref=digest({'call_id': supervisor_call['call_id']}),
            operator_decision_ref=digest({'decision_id': decision['decision_id']}),
            approved_action_ref=digest({'action_id': action['action_id']}),
            downstream_verification_ref=digest({'verification_id': verification['verification_id']}),
            downstream_outcome_sha256=digest(VISION_EXPECTED), synthetic=True,
            supervisor_outcome_verified=False, downstream_operator_outcome_verified=True,
            profile_bound=False, reviewed=False, execution_authorized=False,
            collection_authorized=False, activation_authorized=False, training_ready=False)
        return report.model_dump()


def derive_vision_dual_role_candidate(database: Path, run_id: str,
                                      *, snapshot_sha256: str | None = None) -> dict:
    supervisor = VisionSkillCandidate.model_validate(derive_vision_skill_candidate(
        database, run_id, snapshot_sha256=snapshot_sha256))
    operator = VisionOperatorCandidate(
        model_role='system1', model_kind='decider_native_worker',
        deployment_sha256=supervisor.operator_deployment_sha256,
        decision_ref=supervisor.operator_decision_ref,
        approved_action_ref=supervisor.approved_action_ref,
        verification_ref=supervisor.downstream_verification_ref,
        outcome_sha256=supervisor.downstream_outcome_sha256,
        outcome_verified=True, reviewed=False, training_ready=False)
    return VisionDualRoleCandidate(
        schema_version='1.0', candidate_version='synthetic-canvas-dual-role-v1',
        status='unreviewed_pair', run_ref=supervisor.run_ref,
        job_ref=supervisor.job_ref, snapshot_sha256=supervisor.snapshot_sha256,
        source_sha256=supervisor.source_sha256, operator=operator, supervisor=supervisor,
        synthetic=True, profile_bound=False, reviewed=False,
        execution_authorized=False, collection_authorized=False,
        activation_authorized=False, training_ready=False).model_dump()


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Synthetic vision skill candidate unavailable: invalid arguments.\n')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Read-only synthetic S2 vision skill candidate')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--snapshot-sha256')
    parser.add_argument('--dual-role', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        derive = derive_vision_dual_role_candidate if arguments.dual_role else derive_vision_skill_candidate
        report = derive(arguments.database, arguments.run_id,
                        snapshot_sha256=arguments.snapshot_sha256)
    except (AOSFault, OSError, sqlite3.Error, ValueError, TypeError, KeyError,
            IndexError, RecursionError, ValidationError):
        parser.exit(1, 'Synthetic vision skill candidate unavailable: source is missing, changed or invalid.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
