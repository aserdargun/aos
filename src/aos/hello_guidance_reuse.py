"""Explicit, same-session reuse of versioned finite hello guidance, not general RAG."""

import fcntl
import json
import os
import sqlite3
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .failure_followup import TaskContract, _time, _typed
from .failure_guidance import (FailureGuidancePreview, FailureGuidanceReport, FailureGuidanceStart,
                               GuidanceCode, VERSION, context)
from .failure_improvement import Hash, Identifier, _check, _hash
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _write_private_child
from .workspace_identity import open_existing_workspace, workspace_identity


ADMISSION = 'hello_guidance_reuse_admission'


class ReuseFlags(TypedModel):
    execution_authorized: Literal[False]
    causality_verified: Literal[False]
    gold: Literal[False]
    training_ready: Literal[False]


FLAGS = {'execution_authorized': False, 'causality_verified': False, 'gold': False, 'training_ready': False}


class HelloReuseScope(TypedModel):
    session_id: Identifier
    parent_runtime_id: Identifier
    image_id: str = Field(min_length=1, max_length=128)
    configuration_sha256: Hash
    workspace_sha256: Hash
    system1_deployment_id: Identifier
    role: Literal['system1']


class HelloReuseSource(TypedModel):
    source_job_id: Identifier
    candidate_sha256: Hash
    receipt_sha256: Hash
    source_sha256: Hash
    contract: TaskContract


class HelloGuidanceEntry(ReuseFlags):
    schema_version: Literal['1.0']
    kind: Literal['finite_hello_guidance']
    entry_version: Literal[1]
    guidance_code: GuidanceCode
    context_version: Literal['hello-guidance-v1']
    context_sha256: Hash
    scope: HelloReuseScope
    source: HelloReuseSource


class HelloReusePublicationPreview(ReuseFlags):
    schema_version: Literal['1.0']
    entry: HelloGuidanceEntry
    entry_sha256: Hash
    confirm_sha256: Hash
    manual_approval_required: Literal[True]


class HelloReuseEntryResponse(ReuseFlags):
    schema_version: Literal['1.0']
    entry: HelloGuidanceEntry
    entry_sha256: Hash
    saved: bool
    current_valid: bool
    manual_approval_required: Literal[True]


class HelloReusePreview(ReuseFlags):
    schema_version: Literal['1.0']
    entry: HelloGuidanceEntry
    entry_sha256: Hash
    guidance: FailureGuidancePreview
    confirm_sha256: Hash
    manual_approval_required: Literal[True]


class HelloReuseStart(FailureGuidanceStart):
    entry_sha256: Hash
    reuse_intent_sha256: Hash


class HelloReuseReport(TypedModel):
    schema_version: Literal['1.0']
    job_id: Identifier
    entry_sha256: Hash
    reuse_intent_sha256: Hash
    entry_binding_verified: bool
    current_entry_valid: bool
    guidance: FailureGuidanceReport
    manual_approval_required: Literal[True]
    causality_verified: Literal[False]
    gold: Literal[False]
    training_ready: Literal[False]


class HelloGuidanceReuseService:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.followups = scheduler.failure_followups
        self.guidance = scheduler.failure_guidance
        self.directory = self.followups.directory.parent / 'hello-guidance-reuse'

    def workspace_sha256(self):
        runtime = self.scheduler.controller.runtime
        _check(runtime.descriptor is not None)
        pinned = workspace_identity(runtime.root, runtime.descriptor).model_dump()
        descriptor = open_existing_workspace(runtime.root)
        try:
            _check(pinned == workspace_identity(runtime.root, descriptor).model_dump())
        finally:
            os.close(descriptor)
        return digest(pinned)

    def derive_connection(self, connection, source, target):
        self.followups._authority(connection, target)
        candidate, review, contract = self.followups._source(connection, source['source_job_id'],
            source['candidate_sha256'], source['receipt_sha256'])
        _check(candidate['task_kind'] == target['task_kind'] == 'hello' and target['system2_deployment_id'] is None)
        code = review['correction_code']
        scope = {key: target[key] for key in ('session_id', 'parent_runtime_id', 'image_id',
                                            'configuration_sha256', 'system1_deployment_id')}
        scope.update(workspace_sha256=self.workspace_sha256(), role='system1')
        return _typed(HelloGuidanceEntry, {'schema_version': '1.0', 'kind': 'finite_hello_guidance',
            'entry_version': 1, 'guidance_code': code, 'context_version': VERSION,
            'context_sha256': digest(context(code)), 'scope': scope,
            'source': {**source, 'source_sha256': candidate['source_sha256'], 'contract': contract}, **FLAGS})

    def derive(self, source):
        target = self.scheduler.failure_followup_target('hello')
        with audit_snapshot(self.followups.failures.database) as (connection, unused_identity):
            return self.derive_connection(connection, source, target)

    def publish_preview(self, source_job_id, candidate_sha256, receipt_sha256):
        self.scheduler.preview_failure_guidance(source_job_id, candidate_sha256, receipt_sha256)
        entry = self.derive({'source_job_id': source_job_id, 'candidate_sha256': candidate_sha256,
                             'receipt_sha256': receipt_sha256})
        result = {'schema_version': '1.0', 'entry': entry, 'entry_sha256': digest(entry),
                  'manual_approval_required': True, **FLAGS}
        result['confirm_sha256'] = digest(result)
        return _typed(HelloReusePublicationPreview, result)

    def source_selection(self, entry):
        return {key: entry['source'][key] for key in ('source_job_id', 'candidate_sha256', 'receipt_sha256')}

    def _publication(self, preview):
        preview = _typed(HelloReusePublicationPreview, preview)
        _check(preview['entry_sha256'] == digest(preview['entry'])
               and preview['confirm_sha256'] == digest({key: value for key, value in preview.items() if key != 'confirm_sha256'}))
        return preview

    def publish(self, preview, confirm_sha256, consent):
        preview = self._publication(preview)
        _check(consent is True and confirm_sha256 == preview['confirm_sha256'])
        _check(preview['entry'] == self.derive(self.source_selection(preview['entry'])))
        record = {'schema_version': '1.0', 'entry': preview['entry'], 'consent': True}
        descriptor = _directory(self.directory, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._directory_current(descriptor)
            name = 'entry-' + preview['entry_sha256'] + '.json'
            try:
                previous = self.followups._read(descriptor, name)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                _check(canonical(previous) == canonical(record))
            else:
                _write_private_child(descriptor, name, canonical(record).encode())
        finally:
            os.close(descriptor)
        return self.inspect(preview['entry_sha256'])

    def _directory_current(self, descriptor):
        current = _directory(self.directory, create=False)
        try:
            pinned, linked = os.fstat(descriptor), os.fstat(current)
            _check((pinned.st_dev, pinned.st_ino) == (linked.st_dev, linked.st_ino))
        finally:
            os.close(current)

    def read_entry(self, entry_sha256):
        _hash(entry_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            record = self.followups._read(descriptor, 'entry-' + entry_sha256 + '.json')
            _check(type(record) is dict and set(record) == {'schema_version', 'entry', 'consent'}
                   and record['schema_version'] == '1.0' and record['consent'] is True)
            entry = _typed(HelloGuidanceEntry, record['entry'])
            _check(digest(entry) == entry_sha256)
            return entry
        finally:
            os.close(descriptor)

    def current(self, connection, entry, target):
        _check(entry == self.derive_connection(connection, self.source_selection(entry), target))

    def inspect(self, entry_sha256):
        entry = self.read_entry(entry_sha256)
        valid = False
        try:
            valid = entry == self.derive(self.source_selection(entry))
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            pass
        return _typed(HelloReuseEntryResponse, {'schema_version': '1.0', 'entry': entry,
            'entry_sha256': entry_sha256, 'saved': True, 'current_valid': valid,
            'manual_approval_required': True, **FLAGS})

    def reuse_preview(self, entry_sha256):
        entry = self.read_entry(entry_sha256)
        _check(entry == self.derive(self.source_selection(entry)))
        guidance = self.scheduler.preview_failure_guidance(**self.source_selection(entry))
        result = {'schema_version': '1.0', 'entry': entry, 'entry_sha256': entry_sha256,
                  'guidance': guidance, 'manual_approval_required': True, **FLAGS}
        result['confirm_sha256'] = digest(result)
        return _typed(HelloReusePreview, result)

    def _preview(self, preview):
        preview = _typed(HelloReusePreview, preview)
        _check(preview['entry'] == self.read_entry(preview['entry_sha256'])
               and preview['confirm_sha256'] == digest({key: value for key, value in preview.items() if key != 'confirm_sha256'}))
        guide = self.guidance._preview(preview['guidance'])
        ordinary = guide['followup']
        entry = preview['entry']
        _check(self.source_selection(entry) == {key: ordinary[key] for key in self.source_selection(entry)}
               and entry['source']['source_sha256'] == ordinary['source_sha256']
               and entry['source']['contract'] == ordinary['source_contract']
               and entry['guidance_code'] == guide['guidance_code']
               and entry['context_sha256'] == guide['context_sha256'])
        return preview

    def commit(self, preview, confirm_sha256, consent, target):
        preview = self._preview(preview)
        _check(consent is True and confirm_sha256 == preview['confirm_sha256'])
        with audit_snapshot(self.followups.failures.database) as (connection, unused_identity):
            self.current(connection, preview['entry'], target)
            self.followups._check_preview(connection, preview['guidance']['followup'], target)
        record = {'schema_version': '1.0', 'preview': preview, 'consent': True}
        intent_sha256 = digest(record)
        descriptor = _directory(self.directory, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._directory_current(descriptor)
            _write_private_child(descriptor, 'reuse-' + preview['guidance']['followup']['attempt_id'] + '.json', canonical(record).encode())
            _write_private_child(descriptor, 'intent-' + intent_sha256 + '.json', canonical(record).encode())
        finally:
            os.close(descriptor)
        guide = self.guidance.commit(preview['guidance'], preview['guidance']['confirm_sha256'], True, target)
        return {**guide, 'reuse_intent_sha256': intent_sha256, 'entry_sha256': preview['entry_sha256']}

    def intent(self, intent_sha256):
        _hash(intent_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            record = self.followups._read(descriptor, 'intent-' + intent_sha256 + '.json')
            _check(type(record) is dict and set(record) == {'schema_version', 'preview', 'consent'}
                   and record['schema_version'] == '1.0' and record['consent'] is True and digest(record) == intent_sha256)
            preview = self._preview(record['preview'])
            _check(canonical(record) == canonical(self.followups._read(descriptor,
                   'reuse-' + preview['guidance']['followup']['attempt_id'] + '.json')))
            return record
        finally:
            os.close(descriptor)

    def binding(self, reuse_sha256, guidance_sha256, job_id):
        intent = self.intent(reuse_sha256)
        guide = self.guidance._intent(guidance_sha256)
        _check(intent['preview']['guidance'] == guide['preview'])
        return {'schema_version': '1.0', 'job_id': job_id, 'reuse_intent_sha256': reuse_sha256,
            'guidance_intent_sha256': guidance_sha256, 'entry_sha256': intent['preview']['entry_sha256'],
            'source_job_id': intent['preview']['entry']['source']['source_job_id']}

    def admit(self, store, reuse_sha256, guidance_sha256, job_id, target):
        _check(store.connection.in_transaction)
        payload = self.binding(reuse_sha256, guidance_sha256, job_id)
        intent = self.intent(reuse_sha256)
        self.current(store.connection, intent['preview']['entry'], target)
        unused_intent, unused_payload, unused_job, historical = self.guidance._association(store.connection, guidance_sha256, job_id)
        _check(not historical)
        _check(store.connection.execute('SELECT 1 FROM desktop_events WHERE kind=? AND '
            '(json_extract(payload_json,\'$.reuse_intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?)',
            (ADMISSION, reuse_sha256, job_id)).fetchone() is None)
        store.insert('desktop_events', event_id=identifier('event'), session_id=target['session_id'],
                     kind=ADMISSION, payload_json=canonical(payload), created_at=now())
        return {key: payload[key] for key in ('reuse_intent_sha256', 'entry_sha256', 'source_job_id')}

    def association(self, connection, reuse_sha256, job_id):
        intent = self.intent(reuse_sha256)
        events = connection.execute('SELECT * FROM desktop_events WHERE kind=? AND '
            '(json_extract(payload_json,\'$.reuse_intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?) LIMIT 3',
            (ADMISSION, reuse_sha256, job_id)).fetchall()
        _check(len(events) == 1 and events[0]['session_id'] == self.followups.failures.session_id)
        payload = json.loads(events[0]['payload_json'])
        _check(events[0]['payload_json'] == canonical(self.binding(reuse_sha256, payload['guidance_intent_sha256'], job_id)))
        unused_intent, unused_payload, job, historical = self.guidance._association(connection, payload['guidance_intent_sha256'], job_id)
        _check(_time(job['created_at']) <= _time(events[0]['created_at']))
        if job['run_id'] is not None:
            run = connection.execute('SELECT started_at FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
            _check(run is not None and _time(events[0]['created_at']) <= _time(run['started_at']))
        return intent, payload, job, historical

    def guard(self, connection, reuse_sha256, job_id, target):
        intent, unused_payload, unused_job, historical = self.association(connection, reuse_sha256, job_id)
        _check(historical)
        self.current(connection, intent['preview']['entry'], target)

    def report(self, job_id):
        with audit_snapshot(self.followups.failures.database) as (connection, unused_identity):
            events = connection.execute('SELECT payload_json FROM desktop_events WHERE session_id=? AND kind=? '
                'AND json_extract(payload_json,\'$.job_id\')=? LIMIT 2', (self.followups.failures.session_id, ADMISSION, job_id)).fetchall()
            _check(len(events) == 1)
            reuse_sha256 = json.loads(events[0]['payload_json'])['reuse_intent_sha256']
            intent, payload, unused_job, historical = self.association(connection, reuse_sha256, job_id)
            valid = False
            try:
                self.current(connection, intent['preview']['entry'], self.scheduler.failure_followup_target('hello'))
                valid = True
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                pass
            guidance = self.guidance.inspect_connection(connection, job_id)
            _check(guidance['guidance_intent_sha256'] == payload['guidance_intent_sha256'])
            return _typed(HelloReuseReport, {'schema_version': '1.0', 'job_id': job_id,
                'entry_sha256': payload['entry_sha256'], 'reuse_intent_sha256': reuse_sha256,
                'entry_binding_verified': historical, 'current_entry_valid': valid, 'guidance': guidance,
                'manual_approval_required': True, 'causality_verified': False, 'gold': False, 'training_ready': False})
