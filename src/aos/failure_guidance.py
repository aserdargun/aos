"""Separately consented, finite hello guidance; context evidence is not causality."""

import fcntl
import json
import os
import sqlite3
from typing import Literal

from pydantic import model_validator

from .contracts import Option, Phase, Prediction, State, canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .decision import decision_request
from .failure_followup import (FailureFollowupPreview, FailureFollowupReport, FailureFollowupStart,
                               FollowupFlags, FollowupTarget, _typed)
from .failure_improvement import Hash, Identifier, _check, _hash
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _write_private_child


VERSION = 'hello-guidance-v1'
ADMISSION = 'failure_guidance_admission'
MARKER = 'failure.guidance_context'
GUIDANCE = {
    'refresh_observation': 'Use only the fresh authorized file observation for this decision; do not reuse a previous attempt state.',
    'inspect_before_retry': 'Inspect the fresh authorized file observation before choosing; ask the user about conflicting content and never overwrite it.',
}
OBSERVATIONS = (
    'File is absent. Creating the exact requested file is authorized.',
    'File already contains the exact authorized content.',
    'File exists with different content. Overwriting is not authorized by this harness.',
)
GuidanceCode = Literal['refresh_observation', 'inspect_before_retry']


def context(code):
    _check(type(code) is str and code in GUIDANCE)
    return {'version': VERSION, 'code': code, 'text': GUIDANCE[code]}


def observation_context(base, code):
    _check(base in OBSERVATIONS)
    return base + '\nReviewed hello guidance (' + VERSION + '): ' + context(code)['text']


def context_options(base):
    choices = {
        OBSERVATIONS[0]: Option(id='write_file', label='Create the authorized hello file with the exact requested content'),
        OBSERVATIONS[1]: Option(id='read_file', label='Read the existing file and independently verify exact content'),
        OBSERVATIONS[2]: Option(id='ask_human', label='Ask the user to resolve the existing conflicting file'),
    }
    first = choices[base]
    return [first, Option(id='ask_supervisor', label='Ask the supervisor for help')] + (
        [Option(id='ask_human', label='Ask the user for help')] if first.id != 'ask_human' else [])


class HelloGuidanceTarget(FollowupTarget):
    task_kind: Literal['hello']


class HelloGuidanceFollowup(FailureFollowupPreview):
    target: HelloGuidanceTarget
    correction_code: GuidanceCode


class FailureGuidancePreview(FollowupFlags):
    schema_version: Literal['1.0']
    followup: HelloGuidanceFollowup
    guidance_code: GuidanceCode
    context_version: Literal['hello-guidance-v1']
    context_sha256: Hash
    confirm_sha256: Hash


class FailureGuidanceStart(FailureFollowupStart):
    guidance_intent_sha256: Hash


class FailureGuidanceReport(FollowupFlags):
    guidance_applied: bool
    schema_version: Literal['1.0']
    job_id: Identifier
    guidance_intent_sha256: Hash
    guidance_code: GuidanceCode
    followup: FailureFollowupReport
    context_binding_verified: bool
    model_request_verified: bool

    @model_validator(mode='after')
    def check_application(self):
        _check(self.followup.job_id == self.job_id
               and self.guidance_applied == (self.context_binding_verified and self.model_request_verified)
               and (not self.model_request_verified or self.context_binding_verified))
        return self


class FailureGuidanceContext:
    def __init__(self, scheduler, job_id):
        self.scheduler = scheduler
        self.job_id = job_id

    def apply(self, state, observation):
        self.scheduler.check_failure_guidance(self.job_id)
        binding = self.scheduler._failure_guidance_jobs[self.job_id]
        preview = self.scheduler.failure_guidance._intent(binding['guidance_intent_sha256'])['preview']
        _check(state.task_kind == 'hello' and state.phase == Phase.OBSERVE)
        return observation_context(observation, preview['guidance_code'])

    def record(self, state, options, call_id):
        self.scheduler.check_failure_guidance(self.job_id)
        binding = self.scheduler._failure_guidance_jobs[self.job_id]
        self.scheduler.failure_guidance.record(self.scheduler.store, binding['guidance_intent_sha256'],
                                               self.job_id, state, options, call_id)

    def before_call(self):
        self.scheduler.check_failure_guidance(self.job_id)


class FailureGuidanceService:
    def __init__(self, followups):
        self.followups = followups
        self.directory = followups.directory.parent / 'failure-guidance'

    def preview(self, followup):
        followup = _typed(FailureFollowupPreview, followup)
        _check(followup['target']['task_kind'] == 'hello' and followup['correction_code'] in GUIDANCE)
        result = {key: followup[key] for key in ('manual_approval_required', 'guidance_applied',
                                               'causality_verified', 'gold', 'training_ready')}
        result.update(schema_version='1.0', followup=followup, guidance_code=followup['correction_code'],
                      context_version=VERSION, context_sha256=digest(context(followup['correction_code'])))
        result['confirm_sha256'] = digest(result)
        return _typed(FailureGuidancePreview, result)

    def _preview(self, value):
        value = _typed(FailureGuidancePreview, value)
        _check(canonical(value) == canonical(self.preview(value['followup'])))
        return value

    def commit(self, preview, confirm_sha256, consent, target):
        preview = self._preview(preview)
        _check(consent is True and confirm_sha256 == preview['confirm_sha256'])
        with audit_snapshot(self.followups.failures.database) as (connection, unused_identity):
            self.followups._check_preview(connection, preview['followup'], target)
        intent = {'schema_version': '1.0', 'preview': preview, 'consent': True}
        intent_sha256 = digest(intent)
        descriptor = _directory(self.directory, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = _directory(self.directory, create=False)
            try:
                before, linked = os.fstat(descriptor), os.fstat(current)
                _check((before.st_dev, before.st_ino) == (linked.st_dev, linked.st_ino))
            finally:
                os.close(current)
            _write_private_child(descriptor, 'guidance-' + preview['followup']['attempt_id'] + '.json', canonical(intent).encode())
            _write_private_child(descriptor, 'intent-' + intent_sha256 + '.json', canonical(intent).encode())
        finally:
            os.close(descriptor)
        ordinary = self.followups.commit(preview['followup'], preview['followup']['confirm_sha256'], True, target)
        return {'guidance_intent_sha256': intent_sha256, 'intent_sha256': ordinary['intent_sha256']}

    def _intent(self, intent_sha256):
        _hash(intent_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            intent = self.followups._read(descriptor, 'intent-' + intent_sha256 + '.json')
            _check(type(intent) is dict and set(intent) == {'schema_version', 'preview', 'consent'}
                   and intent['schema_version'] == '1.0' and intent['consent'] is True
                   and digest(intent) == intent_sha256)
            preview = self._preview(intent['preview'])
            _check(canonical(self.followups._read(descriptor,
                   'guidance-' + preview['followup']['attempt_id'] + '.json')) == canonical(intent))
            return intent
        finally:
            os.close(descriptor)

    def _binding(self, guidance_sha256, followup_sha256, job_id):
        intent = self._intent(guidance_sha256)
        ordinary = self.followups._intent(followup_sha256)
        _check(canonical(intent['preview']['followup']) == canonical(ordinary['preview']))
        return {'schema_version': '1.0', 'guidance_intent_sha256': guidance_sha256,
                'intent_sha256': followup_sha256, 'job_id': job_id,
                'source_job_id': ordinary['preview']['source_job_id']}

    def admit(self, store, guidance_sha256, followup_sha256, job_id, target):
        connection = store.connection
        _check(connection.in_transaction and target['task_kind'] == 'hello')
        payload = self._binding(guidance_sha256, followup_sha256, job_id)
        intent = self._intent(guidance_sha256)
        self.followups._check_preview(connection, intent['preview']['followup'], target)
        self.followups._association(connection, followup_sha256, intent['preview']['followup'], job_id)
        _check(connection.execute('SELECT 1 FROM desktop_events WHERE kind=? AND '
               '(json_extract(payload_json,\'$.guidance_intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?)',
               (ADMISSION, guidance_sha256, job_id)).fetchone() is None)
        store.insert('desktop_events', event_id=identifier('event'), session_id=target['session_id'],
                     kind=ADMISSION, payload_json=canonical(payload), created_at=now())
        return {'guidance_intent_sha256': guidance_sha256, 'source_job_id': payload['source_job_id']}

    def _association(self, connection, guidance_sha256, job_id):
        from .failure_followup import _time

        intent = self._intent(guidance_sha256)
        events = connection.execute('SELECT * FROM desktop_events WHERE kind=? AND '
               '(json_extract(payload_json,\'$.guidance_intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?) LIMIT 3',
               (ADMISSION, guidance_sha256, job_id)).fetchall()
        _check(len(events) == 1)
        payload = json.loads(events[0]['payload_json'])
        _check(events[0]['session_id'] == self.followups.failures.session_id
               and events[0]['payload_json'] == canonical(self._binding(guidance_sha256, payload['intent_sha256'], job_id)))
        job, historical = self.followups._historical(connection, payload['intent_sha256'],
                                                     intent['preview']['followup'], job_id)
        unused_job, admission = self.followups._association(
            connection, payload['intent_sha256'], intent['preview']['followup'], job_id)
        _check(_time(admission['created_at']) <= _time(events[0]['created_at']))
        if job['run_id'] is not None:
            run = connection.execute('SELECT started_at FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
            _check(run is not None and _time(events[0]['created_at']) <= _time(run['started_at']))
        return intent, payload, job, historical

    def guard(self, connection, guidance_sha256, job_id, target, require_context=False):
        intent, payload, job, historical = self._association(connection, guidance_sha256, job_id)
        _check(historical)
        self.followups.guard(connection, payload['intent_sha256'], job_id, target)
        if require_context:
            bound, unused_request = self._application(connection, intent, payload, job, completed=True)
            _check(bound)

    def record(self, store, guidance_sha256, job_id, state, options, call_id):
        connection = store.connection
        _check(connection.in_transaction)
        intent, payload, job, historical = self._association(connection, guidance_sha256, job_id)
        _check(historical and state.task_kind == 'hello' and state.phase == Phase.DECIDE
               and state.run_id == job['run_id'] and state.owner == 'AGENT')
        preview = intent['preview']
        base = next((value for value in OBSERVATIONS
                     if state.observation == observation_context(value, preview['guidance_code'])), None)
        _check(base is not None and options == context_options(base))
        snapshots = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND state_version=? LIMIT 2',
                                        (state.run_id, state.state_version)).fetchall()
        _check(len(snapshots) == 1 and snapshots[0]['content_sha256'] == digest(state.model_dump(mode='json')))
        _check(connection.execute('SELECT 1 FROM observations WHERE run_id=? AND kind=?', (state.run_id, MARKER)).fetchone() is None)
        marker = self._marker(payload, preview, state, snapshots[0]['snapshot_id'], options, call_id)
        store.insert('observations', observation_id=identifier('observation'), run_id=state.run_id,
                     step_id=state.step_id, action_id=None, kind=MARKER, payload_json=canonical(marker), created_at=now())

    def _marker(self, payload, preview, state, snapshot_id, options, call_id):
        ordinary = preview['followup']
        return {**payload, 'source_sha256': ordinary['source_sha256'],
                'candidate_sha256': ordinary['candidate_sha256'], 'receipt_sha256': ordinary['receipt_sha256'],
                'guidance_code': preview['guidance_code'], 'context_version': VERSION,
                'context_sha256': preview['context_sha256'], 'run_id': state.run_id,
                'step_id': state.step_id, 'state_version': state.state_version,
                'snapshot_id': snapshot_id, 'state_sha256': digest(state.model_dump(mode='json')),
                'options_sha256': digest([option.model_dump() for option in options]),
                'request_sha256': digest(decision_request(state, options)), 'call_id': call_id}

    def _application(self, connection, intent, payload, job, completed=False):
        from .failure_followup import _time

        rows = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=? LIMIT 2',
                                   (job['run_id'], MARKER)).fetchall()
        _check(len(rows) == 1)
        row = rows[0]
        marker = json.loads(row['payload_json'])
        snapshot = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND snapshot_id=?',
                                       (job['run_id'], marker['snapshot_id'])).fetchone()
        _check(snapshot is not None)
        state = State.model_validate_json(snapshot['state_json'])
        _check(state.phase == Phase.DECIDE and state.task_kind == 'hello' and state.run_id == job['run_id']
               and snapshot['content_sha256'] == digest(state.model_dump(mode='json')))
        preview = intent['preview']
        base = next((value for value in OBSERVATIONS
                     if state.observation == observation_context(value, preview['guidance_code'])), None)
        _check(base is not None)
        options = context_options(base)
        _check(row['action_id'] is None and row['step_id'] == state.step_id
               and row['payload_json'] == canonical(self._marker(payload, preview, state, snapshot['snapshot_id'], options, marker['call_id']))
               and _time(snapshot['created_at']) <= _time(row['created_at']))
        request_verified = False
        if marker['call_id'] is not None:
            call = connection.execute('SELECT * FROM model_calls WHERE call_id=? AND run_id=? AND step_id=?',
                                       (marker['call_id'], state.run_id, state.step_id)).fetchone()
            _check(job['real_model'] == 1 and call is not None and call['role'] == 'system1'
                   and call['deployment_id'] == state.deployment_id
                   and call['request_json'] == canonical(decision_request(state, options))
                   and _time(call['created_at']) <= _time(row['created_at']))
            request_verified = call['status'] == 'ok'
        else:
            _check(job['real_model'] == 0)
        decisions = connection.execute('SELECT * FROM decisions WHERE run_id=? AND snapshot_id=? LIMIT 2',
                                        (state.run_id, snapshot['snapshot_id'])).fetchall()
        if completed or marker['call_id'] is None or request_verified:
            _check(len(decisions) == 1)
        if decisions:
            _check(len(decisions) == 1 and decisions[0]['step_id'] == state.step_id
                   and decisions[0]['call_id'] == marker['call_id']
                   and decisions[0]['options_json'] == canonical([option.model_dump() for option in options])
                   and _time(row['created_at']) <= _time(decisions[0]['created_at']))
        if request_verified:
            prediction = Prediction.model_validate_json(call['response_json'])
            prediction.validate_options(options)
            _check(decisions[0]['selected_option'] == prediction.selected_option
                   and decisions[0]['probabilities_json'] == canonical(prediction.probabilities))
        if completed and marker['call_id'] is not None:
            _check(request_verified)
        return True, request_verified

    def inspect(self, job_id):
        with audit_snapshot(self.followups.failures.database) as (connection, unused_identity):
            return self.inspect_connection(connection, job_id)

    def inspect_connection(self, connection, job_id):
        events = connection.execute('SELECT payload_json FROM desktop_events WHERE session_id=? AND kind=? '
                                    'AND json_extract(payload_json,\'$.job_id\')=? LIMIT 2',
                                    (self.followups.failures.session_id, ADMISSION, job_id)).fetchall()
        _check(len(events) == 1)
        guidance_sha256 = json.loads(events[0]['payload_json'])['guidance_intent_sha256']
        intent, payload, job, historical = self._association(connection, guidance_sha256, job_id)
        bound = request = False
        if historical:
            try:
                bound, request = self._application(connection, intent, payload, job)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                pass
        ordinary = self.followups.inspect_connection(connection, job_id)
        _check(ordinary['intent_sha256'] == payload['intent_sha256'])
        return _typed(FailureGuidanceReport, {'schema_version': '1.0', 'job_id': job_id,
            'guidance_intent_sha256': guidance_sha256, 'guidance_code': intent['preview']['guidance_code'],
            'followup': ordinary, 'context_binding_verified': bound, 'model_request_verified': request,
            'guidance_applied': bound and request, 'manual_approval_required': True,
            'causality_verified': False, 'gold': False, 'training_ready': False})
