import fcntl
import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .agent_contracts import (
    AgentAdmissionError, AgentCleanupProof, AgentHandle, AgentJobRequest,
    AgentJobStatus, AgentObservation, AgentPrepared, AgentRegistration, AgentResultProof,
)
from .contracts import canonical, digest, now


class AgentOrchestrationStore:
    """Separate private single-writer journal with conservative interrupted-effect recovery."""

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self._lock_fd = None
        self.connection = None
        try:
            self._lock_fd = self._private_file(Path(str(self.path) + '.writer.lock'))
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            database_fd = self._private_file(self.path)
            os.close(database_fd)
            self.connection = sqlite3.connect(self.path, timeout=1, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA journal_mode=DELETE')
            self.connection.execute('PRAGMA synchronous=FULL')
            migration = Path(__file__).resolve().parents[2] / 'database/agent_orchestration_migrations/0001_agent_jobs.sql'
            migration_text = migration.read_text()
            migration_sha = hashlib.sha256(migration_text.encode()).hexdigest()
            tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables:
                self.connection.executescript(migration_text)
                with self.transaction():
                    self.connection.execute('INSERT INTO agent_orchestration_meta VALUES(1,?)', (migration_sha,))
            else:
                record = self.connection.execute('SELECT * FROM agent_orchestration_meta').fetchall()
                if len(record) != 1 or record[0]['version'] != 1 or record[0]['migration_sha256'] != migration_sha:
                    raise AgentAdmissionError('Separate orchestration migration identity differs')
            with self.transaction():
                interrupted = self.connection.execute(
                    "SELECT job_id FROM agent_jobs WHERE state IN ('prepare_intent','dispatch_intent','cancel_intent')"
                ).fetchall()
                for record in interrupted:
                    self.change(record['job_id'], state='uncertain', last_error='Interrupted effect; no automatic replay')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _private_file(path):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
            os.close(descriptor)
            raise AgentAdmissionError('Orchestration files must be owned private regular files (0600)')
        return descriptor

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise AgentAdmissionError('Orchestration requires a separate transaction')
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def register(self, registration):
        registration = AgentRegistration.model_validate(registration.model_dump(), strict=True)
        serialized = canonical(registration.model_dump(mode='json'))
        with self.transaction():
            row = self.connection.execute('SELECT * FROM agent_registrations WHERE agent_id=? AND version=?',
                                          (registration.agent_id, registration.version)).fetchone()
            if row:
                if row['registration_json'] != serialized:
                    raise AgentAdmissionError('Registered version is immutable')
            else:
                self.connection.execute('INSERT INTO agent_registrations VALUES(?,?,?,?)',
                                        (registration.agent_id, registration.version, serialized,
                                         digest(registration.model_dump(mode='json'))))
        return digest(registration.model_dump(mode='json'))

    def registration(self, agent_id, version):
        row = self.connection.execute('SELECT * FROM agent_registrations WHERE agent_id=? AND version=?',
                                      (agent_id, version)).fetchone()
        if row is None:
            raise AgentAdmissionError('Agent version is not registered')
        registration = AgentRegistration.model_validate_json(row['registration_json'])
        if digest(registration.model_dump(mode='json')) != row['registration_sha256']:
            raise AgentAdmissionError('Registration hash differs')
        return registration

    def dependencies_verified(self, request):
        for dependency in request.dependencies:
            status = self.status(dependency)
            if (status.state != 'succeeded' or status.reservation_held
                    or status.result_proof is None or not status.result_proof.verified
                    or status.cleanup_proof is None or not status.cleanup_proof.verified):
                raise AgentAdmissionError('Dependencies require independently verified success and cleanup')

    def submit(self, request):
        request = AgentJobRequest.model_validate(request.model_dump(), strict=True)
        serialized = canonical(request.model_dump(mode='json'))
        if len(serialized.encode()) > 65536:
            raise AgentAdmissionError('Job request exceeds 64 KiB')
        with self.transaction():
            existing = self.connection.execute('SELECT request_json FROM agent_jobs WHERE job_id=?',
                                               (request.job_id,)).fetchone()
            if existing:
                if existing['request_json'] != serialized:
                    raise AgentAdmissionError('Job identity cannot be reused for another request')
                return self.status(request.job_id)
            registration = self.registration(request.agent_id, request.agent_version)
            if request.operation not in registration.capabilities:
                raise AgentAdmissionError('Operation is not an allowlisted capability')
            for unit in ('wall_seconds', 'model_tokens', 'experiments'):
                if getattr(request.budget, unit) > getattr(registration.per_job_budget, unit):
                    raise AgentAdmissionError('Per-job reservation limit exceeded')
            for dependency in request.dependencies:
                self.status(dependency)
            timestamp = now()
            self.connection.execute('INSERT INTO agent_jobs '
                '(job_id,agent_id,agent_version,request_json,request_sha256,registration_sha256,state,'
                'reservation_held,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,\'queued\',0,0,?,?)',
                (request.job_id, request.agent_id, request.agent_version, serialized,
                 request.request_sha256(), digest(registration.model_dump(mode='json')), timestamp, timestamp))
            self._event(request.job_id)
        return self.status(request.job_id)

    def reserve(self, job_id):
        with self.transaction():
            status = self.status(job_id)
            if status.state != 'queued':
                raise AgentAdmissionError('Only queued jobs may reserve capacity')
            self.dependencies_verified(status.request)
            registration = self.registration(status.request.agent_id, status.request.agent_version)
            active = self.connection.execute(
                'SELECT request_json FROM agent_jobs WHERE agent_id=? AND agent_version=? AND reservation_held=1',
                (registration.agent_id, registration.version)).fetchall()
            if len(active) >= registration.max_active_instances:
                raise AgentAdmissionError('Active instance capacity exhausted')
            budgets = [AgentJobRequest.model_validate_json(row['request_json']).budget for row in active]
            for unit in ('wall_seconds', 'model_tokens', 'experiments'):
                total = sum(getattr(budget, unit) for budget in budgets) + getattr(status.request.budget, unit)
                if total > getattr(registration.aggregate_budget, unit):
                    raise AgentAdmissionError('Aggregate reservation capacity exhausted')
            self.change(job_id, state='prepare_intent', reservation_held=True, last_error=None)
        return self.status(job_id)

    def status(self, job_id):
        row = self.connection.execute('SELECT * FROM agent_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise AgentAdmissionError('Unknown delegated job')
        request = AgentJobRequest.model_validate_json(row['request_json'])
        registration = self.registration(request.agent_id, request.agent_version)
        if (request.request_sha256() != row['request_sha256']
                or digest(registration.model_dump(mode='json')) != row['registration_sha256']):
            raise AgentAdmissionError('Immutable request or registration hash differs')
        def decoded(column, model):
            return None if row[column] is None else model.model_validate_json(row[column])
        status = AgentJobStatus(request=request, request_sha256=row['request_sha256'],
            registration_sha256=row['registration_sha256'], state=row['state'],
            prepared=decoded('prepared_json', AgentPrepared), handle=decoded('handle_json', AgentHandle),
            observation=decoded('observation_json', AgentObservation),
            result_proof=decoded('result_proof_json', AgentResultProof),
            cleanup_proof=decoded('cleanup_proof_json', AgentCleanupProof),
            reservation_held=bool(row['reservation_held']), revision=row['revision'], last_error=row['last_error'])
        handles = [status.handle, status.prepared.handle if status.prepared else None,
                   status.observation.handle if status.observation else None,
                   status.result_proof.handle if status.result_proof else None,
                   status.cleanup_proof.handle if status.cleanup_proof else None]
        for handle in handles:
            if handle is not None and (handle.job_id != request.job_id
                    or handle.request_sha256 != status.request_sha256 or handle.authority != request.authority
                    or status.handle is None or handle.handle_id != status.handle.handle_id):
                raise AgentAdmissionError('Stored adapter handle differs from immutable job binding')
        for proof in (status.result_proof, status.cleanup_proof):
            if proof is not None and proof.handle != status.handle:
                raise AgentAdmissionError('Stored proof differs from current exact handle')
        if status.observation and status.observation.handle != status.handle:
            raise AgentAdmissionError('Stored observation differs from current exact handle')
        if status.cleanup_proof and status.cleanup_proof.scope != registration.cleanup_scope:
            raise AgentAdmissionError('Stored cleanup scope differs from registered executor')
        if status.result_proof and (status.observation is None or status.result_proof.outcome != status.observation.state):
            raise AgentAdmissionError('Stored result differs from independently observed terminal outcome')
        if status.state in {'succeeded', 'failed', 'cancelled'} and status.handle is not None:
            if (status.result_proof is None or not status.result_proof.verified
                    or status.result_proof.outcome != status.state or status.cleanup_proof is None
                    or not status.cleanup_proof.verified or status.reservation_held):
                raise AgentAdmissionError('Terminal delegated job lacks independently verified result and cleanup')
        return status

    def change(self, job_id, **updates):
        if not self.connection.in_transaction:
            raise AgentAdmissionError('Job transitions require a transaction')
        current = self.status(job_id)
        value = AgentJobStatus.model_validate({**current.model_dump(), **updates,
                                              'revision': current.revision + 1}, strict=True)
        fields = ['state', 'prepared', 'handle', 'observation', 'result_proof', 'cleanup_proof',
                  'reservation_held', 'revision', 'last_error']
        values = []
        columns = []
        for field in fields:
            content = getattr(value, field)
            if field in {'prepared', 'handle', 'observation', 'result_proof', 'cleanup_proof'}:
                columns.append(field + '_json=?')
                content = None if content is None else canonical(content.model_dump(mode='json'))
                if content is not None and len(content.encode()) > 65536:
                    raise AgentAdmissionError('Adapter record exceeds 64 KiB')
            else:
                columns.append(field + '=?')
                if field == 'reservation_held':
                    content = int(content)
            values.append(content)
        self.connection.execute('UPDATE agent_jobs SET ' + ','.join(columns) + ',updated_at=? WHERE job_id=?',
                                (*values, now(), job_id))
        self._event(job_id)

    def _event(self, job_id):
        status = self.status(job_id)
        self.connection.execute('INSERT INTO agent_job_events(job_id,revision,state,status_json,observed_at) '
                                'VALUES(?,?,?,?,?)', (job_id, status.revision, status.state,
                                 canonical(status.model_dump(mode='json')), now()))

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
