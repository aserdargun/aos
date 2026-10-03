"""Explicit immutable report retention; historical audit is not GPU authority."""

from contextlib import contextmanager
import json
import re
import sqlite3
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, digest as request_digest, identifier, now
from .scientist_intents import ScientistIntentBinding
from .scientist_lab import ScientistLabTask
from .scientist_protocol import ScientistReport, _reject_constant, _unique_object
from .scientist_terminal import canonical, digest
from .scientist_transport import ScientistAdmissionError


REPORT_LIMIT = 262144
RECORD_LIMIT = 524288
HISTORY_LIMIT = 32
DISPLAY_LIMIT = 20
_HASH = r'^[a-f0-9]{64}$'
_REMOTE = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
_TRIGGERS = {'scientist_lab_readback_original', 'scientist_lab_readback_bound',
             'scientist_lab_readback_no_update', 'scientist_lab_readback_no_delete',
             'scientist_lab_readback_no_replace'}


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


class ScientistLabReadback(TypedModel):
    schema_version: Literal['1.0']
    readback_id: str = Field(pattern=r'^lab-readback-[a-f0-9]{32}$')
    session_id: str = Field(min_length=1, max_length=128)
    local_run_id: str = Field(pattern=r'^run-[a-f0-9]{32}$')
    remote_run_id: str = Field(pattern=_REMOTE)
    current_binding: ScientistIntentBinding
    authority_url: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=128)
    original_request_sha256: str = Field(pattern=_HASH)
    report_sha256: str = Field(pattern=_HASH)
    result_sha256: str = Field(pattern=_HASH)
    report: ScientistReport
    retention_source: Literal['explicit_console_request']
    created_at: str = Field(min_length=1, max_length=64)


def _deny_authority(task, action):
    raise ScientistAdmissionError('Current Lab readback authority is not configured')


class ScientistLabReadbacks:
    def __init__(self, store, *, verify_authority=_deny_authority):
        self.store = store
        self._verify = verify_authority

    @staticmethod
    def check_expected_hash(expected):
        _require(type(expected) is str and re.fullmatch(_HASH, expected) is not None,
                 'Explicit report retention requires its exact expected body SHA256')

    def supported(self):
        row = self.store.connection.execute('SELECT name FROM schema_migrations WHERE version=27').fetchone()
        return row is not None and row['name'] == 'scientist_lab_readbacks'

    def ensure_available(self):
        _require(self.supported(), 'Saved Lab report history schema is unsupported')
        columns = {row['name'] for row in self.store.connection.execute('PRAGMA table_info(scientist_lab_readbacks)')}
        expected = {'readback_id', 'local_run_id', 'session_id', 'remote_run_id', 'principal_id', 'authority_url',
                    'original_request_sha256', 'current_binding_json', 'report_sha256', 'result_sha256',
                    'record_json', 'record_sha256', 'created_at'}
        triggers = {row['name'] for row in self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scientist_lab_readbacks'")}
        _require(columns == expected and _TRIGGERS <= triggers, 'Saved Lab report history schema is unavailable')

    @contextmanager
    def _transaction(self):
        connection = self.store.connection
        _require(not connection.in_transaction, 'Saved report history requires its own original-store transaction')
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            _require(connection.in_transaction, 'Saved report transaction ownership changed')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _authority(self, task, action):
        _require(action.tool == action.selected_option == 'lab.report' and self._verify is not _deny_authority,
                 'Saved reports require current report-read authorization')
        _require(self._verify(task.model_copy(deep=True), action.model_copy(deep=True)) is None,
                 'Saved report authority must complete or raise')
        _require(self.store.connection.in_transaction, 'Saved report authority changed transaction ownership')

    @staticmethod
    def _report(value, remote, expected):
        report = ScientistReport.model_validate(value, strict=True)
        raw = canonical(report.model_dump(mode='json')).encode('utf-8')
        _require(len(raw) <= REPORT_LIMIT and report.run_id == remote
                 and report.report.get('run_id') == remote
                 and report.report.get('status') in ('completed', 'stopped', 'failed')
                 and report.report_sha256 == expected == digest(report.report),
                 'Saved report typed result, body hash or terminal run identity differs')
        return report

    def _record(self, row, *, session_id, local_run_id):
        raw = row['record_json']
        _require(type(raw) is str and len(raw.encode('utf-8')) <= RECORD_LIMIT,
                 'Saved Lab record exceeds its canonical byte bound')
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        record = ScientistLabReadback.model_validate(value, strict=True)
        _require(canonical(record.model_dump(mode='json')) == raw and digest(value) == row['record_sha256'],
                 'Saved Lab record canonical checksum differs')
        for field in ('readback_id', 'session_id', 'local_run_id', 'remote_run_id', 'principal_id', 'authority_url',
                      'original_request_sha256', 'report_sha256', 'result_sha256', 'created_at'):
            _require(row[field] == getattr(record, field), 'Saved Lab record columns disagree with its canonical body')
        _require(row['current_binding_json'] == canonical(record.current_binding.model_dump(mode='json'))
                 and record.session_id == session_id == record.current_binding.session_id
                 and record.local_run_id == local_run_id, 'Saved Lab record belongs to another original scope')
        job = self.store.connection.execute('SELECT * FROM scientist_lab_jobs WHERE run_id=? AND session_id=?',
                                            (local_run_id, session_id)).fetchone()
        _require(job is not None, 'Saved Lab report original job is missing')
        original = ScientistLabTask.model_validate_json(job['task_json'], strict=True)
        _require(job['lab_run_id'] == record.remote_run_id and original.request.external_run_id == local_run_id
                 and original.authority_url == record.authority_url and original.principal_id == record.principal_id
                 and request_digest(original.request.model_dump(mode='json')) == record.original_request_sha256
                 and all(getattr(original.binding, field) == getattr(record.current_binding, field)
                         for field in ('session_id', 'runtime_id', 'authorization_context_sha256'))
                 and record.current_binding.generation >= original.binding.generation,
                 'Saved Lab report differs from its immutable original job, request or authority')
        report = self._report(record.report.model_dump(mode='json'), record.remote_run_id, record.report_sha256)
        _require(digest(report.model_dump(mode='json')) == record.result_sha256,
                 'Saved Lab full typed report result checksum differs')
        return record

    @staticmethod
    def _metadata(record, checksum):
        return {field: getattr(record, field) for field in
                ('readback_id', 'local_run_id', 'remote_run_id', 'report_sha256', 'result_sha256', 'created_at', 'retention_source')} | {
                    'record_sha256': checksum, 'status': record.report.report['status'],
                    'historical': True, 'gpu_release_verified': False}

    def save(self, task, action, value, *, expected_report_sha256):
        self.check_expected_hash(expected_report_sha256)
        self.ensure_available()
        report = self._report(value, task.lab_run_id, expected_report_sha256)
        with self._transaction() as connection:
            self._authority(task, action)
            existing = connection.execute('SELECT * FROM scientist_lab_readbacks WHERE local_run_id=? AND report_sha256=?',
                                          (task.request.external_run_id, expected_report_sha256)).fetchone()
            if existing is not None:
                record = self._record(existing, session_id=task.binding.session_id, local_run_id=task.request.external_run_id)
                _require(record.current_binding.generation <= task.binding.generation,
                         'Saved Lab record is newer than the current controller fence')
                self._authority(task, action)
                return self._metadata(record, existing['record_sha256'])
            record = ScientistLabReadback(schema_version='1.0', readback_id=identifier('lab-readback'),
                session_id=task.binding.session_id, local_run_id=task.request.external_run_id, remote_run_id=task.lab_run_id,
                current_binding=task.binding.model_copy(deep=True), authority_url=task.authority_url,
                principal_id=task.principal_id, original_request_sha256=request_digest(task.request.model_dump(mode='json')),
                report_sha256=report.report_sha256, result_sha256=digest(report.model_dump(mode='json')), report=report,
                retention_source='explicit_console_request', created_at=now())
            raw = canonical(record.model_dump(mode='json'))
            _require(len(raw.encode('utf-8')) <= RECORD_LIMIT, 'Saved Lab record exceeds its byte bound')
            checksum = digest(record.model_dump(mode='json'))
            values = {field: getattr(record, field) for field in
                      ('readback_id', 'session_id', 'local_run_id', 'remote_run_id', 'principal_id', 'authority_url',
                       'original_request_sha256', 'report_sha256', 'result_sha256', 'created_at')}
            values.update(current_binding_json=canonical(record.current_binding.model_dump(mode='json')),
                          record_json=raw, record_sha256=checksum)
            self._authority(task, action)
            self.store.insert('scientist_lab_readbacks', **values)
            self._authority(task, action)
            stored = connection.execute('SELECT * FROM scientist_lab_readbacks WHERE readback_id=?', (record.readback_id,)).fetchone()
            _require(stored is not None and self._record(stored, session_id=record.session_id, local_run_id=record.local_run_id) == record,
                     'Saved Lab record did not preserve its exact append-only readback')
        return self._metadata(record, checksum)

    def read(self, task, action, readback_id):
        _require(type(readback_id) is str and re.fullmatch(r'lab-readback-[a-f0-9]{32}', readback_id) is not None,
                 'Saved Lab readback identifier is invalid')
        self.ensure_available()
        with self._transaction() as connection:
            self._authority(task, action)
            row = connection.execute('SELECT * FROM scientist_lab_readbacks WHERE readback_id=? AND local_run_id=? AND session_id=?',
                (readback_id, task.request.external_run_id, task.binding.session_id)).fetchone()
            _require(row is not None, 'Saved Lab report is not in this original job and session')
            record = self._record(row, session_id=task.binding.session_id, local_run_id=task.request.external_run_id)
            _require(record.current_binding.generation <= task.binding.generation,
                     'Saved Lab record is newer than the current controller fence')
            self._authority(task, action)
            return self._metadata(record, row['record_sha256']) | {'record': record.model_dump(mode='json')}

    def inventory(self, session_id, local_run_id):
        supported = False
        try:
            supported = self.supported()
            if not supported:
                return {'supported': False, 'available': False, 'items': [], 'truncated': False}
            self.ensure_available()
            rows = self.store.connection.execute(
                'SELECT * FROM scientist_lab_readbacks WHERE session_id=? AND local_run_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?',
                (session_id, local_run_id, DISPLAY_LIMIT + 1)).fetchall()
            records = [self._record(row, session_id=session_id, local_run_id=local_run_id) for row in rows]
            return {'supported': True, 'available': True, 'truncated': len(rows) > DISPLAY_LIMIT,
                    'items': [self._metadata(record, row['record_sha256']) for record, row in zip(records[:DISPLAY_LIMIT], rows[:DISPLAY_LIMIT])]}
        except (ScientistAdmissionError, sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return {'supported': supported, 'available': False, 'items': [], 'truncated': False}
