import asyncio
from pathlib import Path
import time
from typing import Annotated

from pydantic import ConfigDict, Field

from .contracts import TypedModel, digest, identifier
from .scientist_intents import ScientistIntentBinding
from .scientist_lab import ScientistLabAction, ScientistLabBudget, ScientistLabClient, ScientistLabPolicy, ScientistLabStart, ScientistLabTask
from .scientist_lab_journal import ScientistLabJournal, _deny_capability
from .scientist_lab_readbacks import ScientistLabReadbacks
from .scientist_transport import ScientistAdmissionError


class ScientistLabStartup(TypedModel):
    model_config = ConfigDict(frozen=True)
    authority_url: str
    token_file: Path
    principal_id: str = Field(min_length=1, max_length=128)
    allowed_suites: frozenset[Annotated[str, Field(min_length=1, max_length=128,
        pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$')]] = Field(min_length=1, max_length=20)
    program_version: str = Field(min_length=1, max_length=64)
    authorization_context_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    timeout_seconds: int = Field(default=10, ge=1, le=10)


def prepare_scientist_lab_startup(config):
    if not isinstance(config, ScientistLabStartup):
        raise ScientistAdmissionError('Lab startup requires typed host configuration')
    config = ScientistLabStartup.model_validate(config.model_dump(), strict=True)
    if not config.token_file.is_absolute() or '..' in config.token_file.parts:
        raise ScientistAdmissionError('Lab startup requires an explicit absolute private token file')
    client = ScientistLabClient(config.authority_url, config.token_file, principal_id=config.principal_id,
        allowed_suites=config.allowed_suites, timeout_seconds=config.timeout_seconds)
    client._token()
    return config, client


class _SharedDrainLabJournal(ScientistLabJournal):
    def __init__(self, service, **options):
        self.service = service
        super().__init__(service.store, verify_admission=service._shared_start_admission, **options)

    def queue(self, task, action):
        with self.service.controller.lock:
            return super().queue(task, action)

    def respond(self, action_id, *, approver, accept):
        with self.service.controller.lock:
            return super().respond(action_id, approver=approver, accept=accept)

    def authorize_and_persist(self, task, action, body):
        with self.service.controller.lock:
            return super().authorize_and_persist(task, action, body)


class ScientistLabService:
    def __init__(self, controller, client, *, authorization_context_sha256: str,
                 program_version: str,
                 verify_capability=_deny_capability):
        self.controller = controller
        self.store = controller.store
        self.client = client
        self.context_sha256 = authorization_context_sha256
        self.program_version = program_version
        self.capability = verify_capability
        self._human_action_id = None
        self._active_controls = set()
        self._closing = False
        self._shared_drain_latched = False
        self._shared_cleanup_controls_closed = False
        self.journal = _SharedDrainLabJournal(self, authenticate_human=self._human,
                                             verify_capability=self._authority)
        self.readbacks = ScientistLabReadbacks(self.store, verify_authority=self.journal.verify_authority)
        client.verify_authority = self.journal.verify_authority
        client.authorize_and_persist = self.journal.authorize_and_persist

    def latch_shared_drain(self):
        with self.controller.lock:
            self._shared_drain_latched = True

    @property
    def cleanup_controls_closed(self):
        with self.controller.lock:
            return self._shared_cleanup_controls_closed

    def seal_shared_drain_controls(self):
        with self.controller.lock:
            if not self._shared_drain_latched:
                raise ScientistAdmissionError('Lab cleanup controls require the existing shared admission latch')
            if self.shared_drain_status()['blockers']:
                raise ScientistAdmissionError('Lab cleanup controls cannot seal while local controls remain unresolved')
            self._shared_cleanup_controls_closed = True

    def shared_drain_status(self):
        with self.controller.lock:
            blockers = []
            if self._active_controls:
                blockers.append('active_controls')
            if self.client.local_cleanup_pending or self.client._lock.locked():
                blockers.append('client_active')
            if self.client.uncertain_action_id is not None:
                blockers.append('client_uncertain')
            states = {row['state'] for row in self.store.connection.execute(
                "SELECT DISTINCT actions.state FROM scientist_lab_actions AS actions "
                "JOIN scientist_lab_jobs AS jobs USING(run_id) "
                "WHERE jobs.session_id=? AND actions.state IN ('pending','approved','intent')",
                (self.controller.session_id,))}
            for state, blocker in (('pending', 'pending_actions'), ('approved', 'approved_actions'),
                                   ('intent', 'unresolved_intents')):
                if state in states:
                    blockers.append(blocker)
            return {'admission_closed': self._shared_drain_latched, 'blockers': blockers}

    def _shared_start_admission(self, task, action):
        if self._shared_cleanup_controls_closed:
            raise ScientistAdmissionError('All new Lab controls are irreversibly sealed by shared drain')
        if action.tool == 'lab.start' and self._shared_drain_latched:
            raise ScientistAdmissionError('Lab start admission is irreversibly closed by shared drain')

    def _authority(self, task, action):
        with self.controller.lock:
            if self._shared_cleanup_controls_closed:
                raise ScientistAdmissionError('All new Lab controls are irreversibly sealed by shared drain')
            if self._closing:
                raise ScientistAdmissionError('Lab service is closing; new job/control admission is disabled')
            current = self.controller.state()
            if (task.binding.session_id != self.controller.session_id or task.state_version != 0
                    or any(current[field] != getattr(task.binding, field)
                           for field in ['runtime_id', 'owner', 'lease_id', 'generation'])
                    or current['status'] != 'running'):
                raise ScientistAdmissionError('Current desktop controller authority differs from Lab job')
            ScientistLabPolicy.check(task, action, authority_url=self.client.authority_url,
                                     principal_id=self.client.principal_id, allowed_suites=self.client.allowed_suites)
            if self.capability(task, action) is not None:
                raise ScientistAdmissionError('Joint capability verifier must complete or raise')

    def _human(self, approver, task, action):
        if approver != 'aos-console-human' or self._human_action_id != action.action_id:
            raise ScientistAdmissionError('Approval requires the authenticated console response path')

    @staticmethod
    def _action(task, tool):
        return ScientistLabAction(task_id=task.request.external_task_id, run_id=task.request.external_run_id,
            step_id=identifier('step'), action_id=task.request.external_action_id if tool == 'lab.start' else identifier('action'),
            runtime_id=task.binding.runtime_id, state_version=0, owner_lease_id=task.binding.lease_id,
            tool=tool, selected_option=tool, idempotency_key=task.request.idempotency_key,
            arguments={'request_sha256': digest(task.request.model_dump(mode='json'))} if tool == 'lab.start'
                       else {'lab_run_id': task.lab_run_id},
            expected_effect='Bounded Scientist Lab control; no GPU release assertion', deadline=time.time() + 60)

    def propose(self, *, suite: str, track: str, budget: ScientistLabBudget, program_version: str):
        with self.controller.lock:
            if self._shared_drain_latched:
                raise ScientistAdmissionError('Lab start admission is irreversibly closed by shared drain')
            if program_version != self.program_version:
                raise ScientistAdmissionError('Lab program version differs from host configuration')
            current = self.controller.state()
            binding = ScientistIntentBinding(session_id=current['session_id'], runtime_id=current['runtime_id'],
                owner=current['owner'], lease_id=current['lease_id'], generation=current['generation'],
                authorization_context_sha256=self.context_sha256)
            request = ScientistLabStart(suite=suite, track=track, budget=budget, program_version=program_version,
                idempotency_key=identifier('scientist-request'), external_task_id=identifier('task'),
                external_run_id=identifier('run'), external_action_id=identifier('action'))
            task = ScientistLabTask(binding=binding, authority_url=self.client.authority_url,
                                   principal_id=self.client.principal_id, state_version=0, request=request)
            action = self._action(task, 'lab.start')
            self._authority(task, action)
            self.journal.queue(task, action)
            return self.approval(action.action_id)

    def _load_action(self, action_id):
        row = self.store.connection.execute('SELECT * FROM scientist_lab_actions WHERE action_id=?',
                                            (action_id,)).fetchone()
        if row is None:
            raise ScientistAdmissionError('Unknown Lab approval action')
        task = ScientistLabTask.model_validate_json(row['task_json'], strict=True)
        action = ScientistLabAction.model_validate_json(row['action_json'], strict=True)
        if task.binding.session_id != self.controller.session_id:
            raise ScientistAdmissionError('Lab action belongs to another desktop session')
        return row, task, action

    def approval(self, action_id):
        row, task, action = self._load_action(action_id)
        envelope = {'task': task.model_dump(mode='json'), 'action': action.model_dump(mode='json'), 'body': row['body']}
        return {'action_id': action_id, 'state': row['state'], 'envelope': envelope,
                'envelope_sha256': digest(envelope), 'expires_at': row['expires_at'],
                'rejection_reason': row['rejection_reason']}

    def respond(self, action_id, *, envelope_sha256: str, accept: bool):
        with self.controller.lock:
            if envelope_sha256 != self.approval(action_id)['envelope_sha256']:
                raise ScientistAdmissionError('Lab approval envelope changed')
            if accept:
                _row, task, action = self._load_action(action_id)
                self._shared_start_admission(task, action)
            self._human_action_id = action_id
            try:
                self.journal.respond(action_id, approver='aos-console-human', accept=accept)
            finally:
                self._human_action_id = None
            return self.approval(action_id)

    def execute(self, action_id):
        with self.controller.lock:
            _, task, action = self._load_action(action_id)
            self._shared_start_admission(task, action)
        return self.journal.execute(self.client, task, action).model_dump(mode='json')

    async def execute_async(self, action_id):
        _, task, action = self._load_action(action_id)
        return await self._execute_async(task, action)

    async def _execute_async(self, task, action):
        pending = asyncio.current_task()
        with self.controller.lock:
            self._shared_start_admission(task, action)
            self._active_controls.add(pending)
        try:
            return (await self.journal.execute_async(self.client, task, action)).model_dump(mode='json')
        finally:
            self._active_controls.discard(pending)

    async def close_async(self, *, timeout_seconds=10):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10:
            raise ValueError('Local Lab cleanup timeout must be within 1..10 seconds')
        self._closing = True
        deadline = time.monotonic() + timeout_seconds
        await self.client.close_async(timeout_seconds=timeout_seconds)
        pending = self._active_controls - {asyncio.current_task()}
        if pending:
            await asyncio.wait(pending, timeout=max(0, deadline - time.monotonic()))
        if self._active_controls - {asyncio.current_task()}:
            raise ScientistAdmissionError('Local Lab result callbacks are not drained; cleanup remains unproven')

    def _load_task(self, run_id):
        row = self.store.connection.execute('SELECT * FROM scientist_lab_jobs WHERE run_id=? AND session_id=?',
                                            (run_id, self.controller.session_id)).fetchone()
        if row is None or row['lab_run_id'] is None:
            raise ScientistAdmissionError('Lab job has no independently verified remote binding')
        original = ScientistLabTask.model_validate_json(row['task_json'], strict=True)
        current = self.controller.state()
        binding = ScientistIntentBinding(session_id=current['session_id'], runtime_id=current['runtime_id'],
            owner=current['owner'], lease_id=current['lease_id'], generation=current['generation'],
            authorization_context_sha256=self.context_sha256)
        return original.model_copy(update={'lab_run_id': row['lab_run_id'], 'binding': binding})

    def propose_stop(self, run_id):
        with self.controller.lock:
            task = self._load_task(run_id)
            action = self._action(task, 'lab.stop')
            self._authority(task, action)
            self._reject_stale_approvals()
            self.journal.queue(task, action)
            return self.approval(action.action_id)

    def _reject_stale_approvals(self):
        with self.journal._transaction() as connection:
            current = self.controller.state()
            rows = connection.execute(
                "SELECT scientist_lab_actions.* FROM scientist_lab_actions JOIN scientist_lab_jobs USING(run_id) "
                "WHERE session_id=? AND state IN ('pending','approved')", (self.controller.session_id,),
            ).fetchall()
            for row in rows:
                task = ScientistLabTask.model_validate_json(row['task_json'], strict=True)
                stale = any(current[field] != getattr(task.binding, field)
                            for field in ['runtime_id', 'owner', 'lease_id', 'generation'])
                expired = row['expires_at'] <= time.time()
                if stale or expired:
                    connection.execute("UPDATE scientist_lab_actions SET state='rejected',"
                        "approver=coalesce(approver,'controller-fence'),rejection_reason=? WHERE action_id=?",
                        ('stale_controller' if stale else 'approval_expired', row['action_id']))

    def read(self, run_id, tool):
        if tool not in {'lab.status', 'lab.report'}:
            raise ScientistAdmissionError('Only independent Lab status or report readback is allowed')
        task = self._load_task(run_id)
        return self.journal.execute(self.client, task, self._action(task, tool)).model_dump(mode='json')

    async def read_async(self, run_id, tool):
        if tool not in {'lab.status', 'lab.report'}:
            raise ScientistAdmissionError('Only independent Lab status or report readback is allowed')
        task = self._load_task(run_id)
        return await self._execute_async(task, self._action(task, tool))

    async def save_report_async(self, run_id, *, expected_report_sha256):
        self.readbacks.check_expected_hash(expected_report_sha256)
        self.readbacks.ensure_available()
        task = self._load_task(run_id)
        action = self._action(task, 'lab.report')
        result = await self._execute_async(task, action)
        return self.readbacks.save(task, action, result, expected_report_sha256=expected_report_sha256)

    def read_saved_report(self, run_id, readback_id):
        task = self._load_task(run_id)
        return self.readbacks.read(task, self._action(task, 'lab.report'), readback_id)

    def inventory(self):
        jobs = []
        for row in self.store.connection.execute(
                'SELECT run_id,lab_run_id FROM scientist_lab_jobs WHERE session_id=? ORDER BY created_at',
                (self.controller.session_id,)):
            actions = [self.approval(item['action_id']) for item in self.store.connection.execute(
                'SELECT action_id FROM scientist_lab_actions WHERE run_id=? ORDER BY created_at', (row['run_id'],))]
            jobs.append({'run_id': row['run_id'], 'lab_run_id': row['lab_run_id'], 'actions': actions,
                         'readbacks': self.readbacks.inventory(self.controller.session_id, row['run_id'])})
        return {'configured': True, 'joint_runtime_admitted': False, 'jobs': jobs,
                'allowed_suites': sorted(self.client.allowed_suites), 'program_version': self.program_version}
