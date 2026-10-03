import time
from contextlib import contextmanager

from .contracts import canonical, now
from .scientist_intents import ScientistIntentJournal
from .scientist_lab import ScientistLabAction, ScientistLabHandle, ScientistLabTask, ScientistLabUncertain
from .scientist_protocol import ScientistRunStatus
from .scientist_transport import ScientistAdmissionError


def _deny_human(approver: str, task: ScientistLabTask, action: ScientistLabAction) -> None:
    raise ScientistAdmissionError('Authenticated human approval provider is not configured')


def _deny_capability(task: ScientistLabTask, action: ScientistLabAction) -> None:
    raise ScientistAdmissionError('Joint capability and host state authority are not configured')


class ScientistLabJournal:
    def __init__(self, store, *, authenticate_human=_deny_human, verify_capability=_deny_capability,
                 verify_admission=None):
        self.store = store
        self.authenticate_human = authenticate_human
        self.verify_capability = verify_capability
        self.verify_admission = verify_admission

    def _admission(self, task, action):
        if self.verify_admission is not None and self.verify_admission(task, action) is not None:
            raise ScientistAdmissionError('Lab admission verifier must complete or raise')

    @contextmanager
    def _transaction(self):
        connection = self.store.connection
        if connection.in_transaction:
            raise ScientistAdmissionError('Lab journal requires its own durable transaction')
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _current(self, task, action):
        ScientistIntentJournal(self.store, task.binding)._current()
        if (action.task_id != task.request.external_task_id
                or action.run_id != task.request.external_run_id
                or action.runtime_id != task.binding.runtime_id
                or action.owner_lease_id != task.binding.lease_id
                or action.state_version != task.state_version
                or action.deadline <= time.time()):
            raise ScientistAdmissionError('Lab journal action fence or deadline differs')

    def _job(self, task):
        row = self.store.connection.execute('SELECT * FROM scientist_lab_jobs WHERE run_id=?',
                                            (task.request.external_run_id,)).fetchone()
        if row is None:
            raise ScientistAdmissionError('Unknown durable Lab job')
        stored = ScientistLabTask.model_validate_json(row['task_json'], strict=True)
        normalized = task.model_copy(update={'lab_run_id': None, 'state_version': 0, 'binding': stored.binding})
        if (row['session_id'] != task.binding.session_id
                or any(getattr(task.binding, field) != getattr(stored.binding, field)
                       for field in ['session_id', 'runtime_id', 'authorization_context_sha256'])
                or task.binding.generation < stored.binding.generation
                or (row['lab_run_id'] is None and task.binding != stored.binding)
                or row['task_json'] != normalized.model_dump_json()
                or row['lab_run_id'] != task.lab_run_id):
            raise ScientistAdmissionError('Lab task differs from its durable remote binding')
        return row

    def verify_authority(self, task, action):
        self._current(task, action)
        self._job(task)
        if self.verify_capability(task.model_copy(deep=True), action.model_copy(deep=True)) is not None:
            raise ScientistAdmissionError('Capability verifier must complete or raise')
        self._current(task, action)
        self._job(task)

    def queue(self, task: ScientistLabTask, action: ScientistLabAction):
        task = ScientistLabTask.model_validate(task.model_dump(), strict=True)
        action = ScientistLabAction.model_validate(action.model_dump(), strict=True)
        if action.tool not in {'lab.start', 'lab.stop'}:
            raise ScientistAdmissionError('Only Lab effects require approval intents')
        body = canonical(task.request.model_dump(mode='json')) if action.tool == 'lab.start' else '{}'
        with self._transaction() as connection:
            self._current(task, action)
            self._admission(task, action)
            if action.tool == 'lab.start':
                if (task.lab_run_id is not None or task.state_version != 0
                        or task.binding.owner != 'AGENT'
                        or action.action_id != task.request.external_action_id):
                    raise ScientistAdmissionError('Initial Lab job must match its original agent intent')
                connection.execute('INSERT INTO scientist_lab_jobs VALUES(?,?,?,NULL,?)',
                    (action.run_id, task.binding.session_id, task.model_dump_json(), now()))
            self._job(task)
            unresolved = connection.execute(
                "SELECT 1 FROM scientist_lab_actions WHERE state='intent' LIMIT 1",
            ).fetchone()
            if unresolved:
                raise ScientistAdmissionError('Store has an unresolved Lab effect; trusted reconciliation required')
            connection.execute('INSERT INTO scientist_lab_actions '
                '(action_id,run_id,task_json,action_json,body,expires_at,state,approver,result_json,created_at) '
                'VALUES(?,?,?,?,?,?,\'pending\',NULL,NULL,?)',
                (action.action_id, action.run_id, task.model_dump_json(), action.model_dump_json(),
                 body, action.deadline, now()))

    def respond(self, action_id: str, *, approver: str, accept: bool):
        if type(accept) is not bool or type(approver) is not str or not 1 <= len(approver) <= 128:
            raise ScientistAdmissionError('Lab approval identity and decision are invalid')
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM scientist_lab_actions WHERE action_id=?',
                                      (action_id,)).fetchone()
            if row is None or row['state'] != 'pending':
                raise ScientistAdmissionError('Lab approval is not pending')
            task = ScientistLabTask.model_validate_json(row['task_json'], strict=True)
            action = ScientistLabAction.model_validate_json(row['action_json'], strict=True)
            self.verify_authority(task, action)
            if self.authenticate_human(approver, task.model_copy(deep=True), action.model_copy(deep=True)) is not None:
                raise ScientistAdmissionError('Human authenticator must complete or raise')
            self.verify_authority(task, action)
            if accept:
                self._admission(task, action)
            connection.execute('UPDATE scientist_lab_actions SET state=?,approver=?,rejection_reason=? WHERE action_id=?',
                               ('approved' if accept else 'rejected', approver,
                                None if accept else 'human_rejected', action_id))

    def authorize_and_persist(self, task, action, body: bytes):
        with self._transaction() as connection:
            self.verify_authority(task, action)
            unresolved = connection.execute(
                "SELECT 1 FROM scientist_lab_actions WHERE state='intent' LIMIT 1",
            ).fetchone()
            if unresolved:
                raise ScientistAdmissionError('Store has an unresolved Lab effect; trusted reconciliation required')
            row = connection.execute('SELECT * FROM scientist_lab_actions WHERE action_id=?',
                                      (action.action_id,)).fetchone()
            if (row is None or row['state'] != 'approved'
                    or row['task_json'] != task.model_dump_json()
                    or row['action_json'] != action.model_dump_json()
                    or type(body) is not bytes or body != row['body'].encode()):
                raise ScientistAdmissionError('Lab effect lacks exact unconsumed durable approval')
            self._admission(task, action)
            connection.execute("UPDATE scientist_lab_actions SET state='intent' WHERE action_id=?",
                               (action.action_id,))

    def record_result(self, task, action, result):
        if action.tool == 'lab.start':
            result = ScientistLabHandle.model_validate(result.model_dump(), strict=True)
        elif action.tool == 'lab.stop':
            result = ScientistRunStatus.model_validate(result.model_dump(), strict=True)
            if result.run_id != task.lab_run_id:
                raise ScientistAdmissionError('Lab stop result differs from bound run')
        else:
            raise ScientistAdmissionError('Read-only results are not effect acknowledgments')
        with self._transaction() as connection:
            self.verify_authority(task, action)
            row = connection.execute('SELECT * FROM scientist_lab_actions WHERE action_id=?',
                                      (action.action_id,)).fetchone()
            if (row is None or row['state'] != 'intent'
                    or row['task_json'] != task.model_dump_json()
                    or row['action_json'] != action.model_dump_json()):
                raise ScientistAdmissionError('Lab result is not bound to a pending exact intent')
            if action.tool == 'lab.start':
                connection.execute('UPDATE scientist_lab_jobs SET lab_run_id=? WHERE run_id=?',
                                   (result.run_id, action.run_id))
            connection.execute("UPDATE scientist_lab_actions SET state='acknowledged',result_json=? WHERE action_id=?",
                               (result.model_dump_json(), action.action_id))

    def execute(self, client, task, action):
        if (client.verify_authority != self.verify_authority
                or client.authorize_and_persist != self.authorize_and_persist):
            raise ScientistAdmissionError('Lab client must use this journal together with its capability policy')
        result = client.execute(task, action)
        if action.tool in {'lab.start', 'lab.stop'}:
            try:
                self.record_result(task, action, result)
            except Exception as error:
                raise ScientistLabUncertain('Lab effect completed but durable result is unresolved; do not retry') from error
        return result

    async def execute_async(self, client, task, action):
        if (client.verify_authority != self.verify_authority
                or client.authorize_and_persist != self.authorize_and_persist):
            raise ScientistAdmissionError('Lab client must use this journal together with its capability policy')
        result = await client.execute_async(task, action)
        if action.tool in {'lab.start', 'lab.stop'}:
            try:
                self.record_result(task, action, result)
            except Exception as error:
                raise ScientistLabUncertain('Lab effect completed but durable result is unresolved; do not retry') from error
        return result
