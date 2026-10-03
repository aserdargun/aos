import asyncio
from copy import deepcopy
import inspect
import threading

from .contracts import digest
from .desktop_tasks import DesktopScheduler
from .scientist_async import ScientistAsyncTurnClient
from .scientist_bootstrap import ScientistBootstrapCapture
from .scientist_admission_history import ScientistAdmissionHistory
from .scientist_decision import ScientistDecisionEngine
from .scientist_intents import ScientistIntentBinding, ScientistIntentJournal, scientist_unresolved_predicate
from .scientist_inventory import scientist_control_inventory
from .scientist_protocol import ScientistTurnRequest
from .scientist_supervisor import ScientistBonsaiVisionSupervisor
from .scientist_transport import BrokerPeer, ScientistAdmissionError
from .scientist_successful_resolution import ScientistRetainedResolutionResult


def _deny_runtime(profiles):
    raise ScientistAdmissionError('Joint Scientist runtime capability/version is not confirmed')


class ScientistDesktopScheduler(DesktopScheduler):
    def _scientist_engines(self):
        binding = self.scientist_binding
        if self.engine is not binding.engine or self.vision_supervisor is not binding.vision_supervisor:
            raise ScientistAdmissionError('Scientist runtime cannot switch to an unbrokered model engine')

    def _scientist_admission(self):
        self._scientist_engines()
        binding = self.scientist_binding
        if binding.confirm_runtime(deepcopy(binding.profiles)) is not None:
            raise ScientistAdmissionError('Joint runtime verifier must complete or raise')
        unresolved = self.store.connection.execute(
            'SELECT 1 FROM scientist_turn_intents WHERE '
            + scientist_unresolved_predicate(self.store.connection) + ' UNION ALL '
            "SELECT 1 FROM scientist_lab_actions WHERE state='intent' LIMIT 1").fetchone()
        controls = scientist_control_inventory(self.store, self.controller.session_id)
        if unresolved or not controls['available'] or (controls['supported'] and controls['pending_count'] != 0):
            raise ScientistAdmissionError('Scientist GPU cleanup requires trusted reconciliation before admission')

    def start(self, *arguments, **options):
        self._scientist_admission()
        return super().start(*arguments, **options)

    def resume(self, *arguments, **options):
        self._scientist_admission()
        return super().resume(*arguments, **options)

    def configure_owned_skill_planning(self, planner):
        raise ScientistAdmissionError('Owned planning needs a confirmed broker model adapter; native fallback is disabled')

    def configure_knowledge_answer(self, knowledge, answerer, directory):
        raise ScientistAdmissionError('Knowledge answering needs a confirmed broker model adapter; native fallback is disabled')


class ScientistDesktopBinding:
    def __init__(self, controller, *, confirm_runtime=_deny_runtime, admission_history=None,
                 output_validator=None, bootstrap_capture=None, expected_bootstrap_peer=None,
                 resolver_factory=None):
        if admission_history is not None and (
                not isinstance(admission_history, ScientistAdmissionHistory)
                or admission_history.store is not controller.store):
            raise ScientistAdmissionError('Scientist admission history requires the original desktop store')
        if resolver_factory is not None and (not callable(resolver_factory)
                or admission_history is None or admission_history.record_version != '2.0'):
            raise ScientistAdmissionError('Successful resolution requires trusted factory and original history2.0')
        self.controller = controller
        self.confirm_runtime = confirm_runtime
        self.admission_history = admission_history
        self.output_validator = output_validator
        self.bootstrap_capture = bootstrap_capture
        self.expected_bootstrap_peer = expected_bootstrap_peer
        self._resolver_factory = resolver_factory
        self._owner_loop = None
        self._owner_thread = threading.get_ident()
        self.scheduler = None
        self.profiles = {}
        self._state = None
        self._journal = None

    def _current(self, state):
        scheduler = self.scheduler
        if scheduler is None or scheduler.closed or scheduler.restart_quiesced:
            raise ScientistAdmissionError('Scientist desktop runtime is unavailable')
        scheduler._scientist_engines()
        scheduler.check_lease(scheduler.job_id)
        for name in ('check_failure_followup', 'check_failure_guidance',
                     'check_task_knowledge_binding', 'check_local_navigation_binding',
                     'check_local_staging_binding', 'check_remote_entry_binding',
                     'check_remote_routes_binding', 'check_remote_static_assets_binding',
                     'check_remote_form_binding'):
            getattr(scheduler, name)(scheduler.job_id)
        row = scheduler.store.connection.execute(
            'SELECT run_id FROM desktop_tasks WHERE job_id=?', (scheduler.job_id,)).fetchone()
        if (row is None or row['run_id'] != state.run_id or state.owner != 'AGENT'
                or scheduler.store.state(state.run_id).model_dump() != state.model_dump()
                or scheduler.active_runtime is None
                or scheduler.active_runtime.runtime_id != state.runtime_id):
            raise ScientistAdmissionError('Scientist caller is not the current typed desktop task')
        if self.confirm_runtime(deepcopy(self.profiles)) is not None:
            raise ScientistAdmissionError('Joint runtime verifier must complete or raise')

    def _bind(self, state, context):
        if self._resolver_factory is not None:
            loop = asyncio.get_running_loop()
            if threading.get_ident() != self._owner_thread or (
                    self._owner_loop is not None and self._owner_loop is not loop):
                raise ScientistAdmissionError('Successful resolution binding lost its original owner context')
            self._owner_loop = loop
        self._current(state)
        desktop = self.controller.state()
        binding = ScientistIntentBinding(session_id=desktop['session_id'], runtime_id=desktop['runtime_id'],
            owner=desktop['owner'], lease_id=desktop['lease_id'], generation=desktop['generation'],
            authorization_context_sha256=digest(context))
        self._state = state.model_copy(deep=True)
        self._journal = ScientistIntentJournal(self.controller.store, binding,
                                               admission_history=self.admission_history)

    def verify_state(self, state, options):
        self._bind(state, {'state': state.model_dump(mode='json'),
                          'options': [option.model_dump(mode='json') for option in options]})

    def verify_vision(self, problem, evidence, request_context):
        scheduler = self.scheduler
        if scheduler is None or scheduler.job_id is None:
            raise ScientistAdmissionError('Vision requires the current desktop task')
        row = scheduler.store.connection.execute(
            'SELECT run_id,kind FROM desktop_tasks WHERE job_id=?', (scheduler.job_id,)).fetchone()
        if row is None or row['kind'] != 'vision_canvas' or row['run_id'] is None:
            raise ScientistAdmissionError('Vision requires a bound canvas task')
        state = scheduler.store.state(row['run_id'])
        if evidence[0]['state_version'] != state.state_version:
            raise ScientistAdmissionError('Vision capture belongs to a stale task state')
        self._bind(state, {'state': state.model_dump(mode='json'), 'problem': problem,
                          'evidence': evidence, 'request_context': request_context})

    def verify_admission(self, request):
        if self._state is None or self._journal is None:
            raise ScientistAdmissionError('No typed Scientist caller is bound')
        self._current(self._state)
        if self.profiles.get(request.profile_id) != request.deployment_digest:
            raise ScientistAdmissionError('Scientist request profile/deployment is not admitted')
        self._journal.verify_admission(request)

    async def prepare_infer(self, request, *, cancel_event, deadline):
        self.verify_admission(request)
        history, capture, peer_reader = self.admission_history, self.bootstrap_capture, self.expected_bootstrap_peer
        if (not isinstance(history, ScientistAdmissionHistory) or not isinstance(capture, ScientistBootstrapCapture)
                or not callable(peer_reader)):
            raise ScientistAdmissionError('Scientist asynchronous bootstrap is not configured')
        journal = self._journal
        binding = journal.binding.model_copy(deep=True)

        def current():
            if (self.admission_history is not history or self.bootstrap_capture is not capture
                    or self.expected_bootstrap_peer is not peer_reader or history.capture is not capture
                    or history.store is not self.controller.store or history.record_version != '2.0'
                    or capture.store is not self.controller.store or self._journal is not journal
                    or journal.binding != binding or self.controller.store.connection.in_transaction
                    or cancel_event.is_set()):
                raise ScientistAdmissionError('Scientist bootstrap caller or original capture binding changed')
            self.verify_admission(request)

        current()
        peer = peer_reader(request.model_copy(deep=True), binding.model_copy(deep=True))
        if not isinstance(peer, BrokerPeer):
            raise ScientistAdmissionError('Scientist bootstrap needs an independently pinned inference broker peer')
        current()
        await capture.prepare_async(request, binding, peer, cancel_event=cancel_event, deadline=deadline)
        current()
        if peer_reader(request.model_copy(deep=True), binding.model_copy(deep=True)) != peer:
            raise ScientistAdmissionError('Scientist bootstrap expected broker generation changed during staging')
        current()

    def persist_intent(self, frame, checksum, deadline, peer):
        request = ScientistTurnRequest.model_validate_json(frame, strict=True)
        self.verify_admission(request)
        self._journal.persist_intent(frame, checksum, deadline, peer)
        if self.output_validator is not None:
            self.output_validator.verify_admission(request)

    def record_receipt(self, receipt, peer):
        self._current(self._state)
        self._journal.record_receipt(receipt, peer)

    async def resolve_successful(self, request_id):
        if self._resolver_factory is None:
            raise ScientistAdmissionError('Successful resolution factory is not configured')
        loop = asyncio.get_running_loop()
        thread = threading.get_ident()
        if self._owner_loop is not loop or self._owner_thread != thread:
            raise ScientistAdmissionError('Successful resolution lost its original owner loop or thread')
        self._current(self._state)
        client = self.engine.client
        if (not isinstance(client, ScientistAsyncTurnClient)
                or client._active_request_id != request_id or not client.cleanup_pending):
            raise ScientistAdmissionError('Successful resolution is not the exact active broker request')
        active_task, context = client._active, client._context
        connection = self.controller.store.connection
        if connection.in_transaction or self._journal is None:
            raise ScientistAdmissionError('Successful resolution requires original idle journal connection')
        binding = self._journal.binding.model_copy(deep=True)
        intent = connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                    (request_id,)).fetchone()
        if (intent is None or intent['state'] != 'receipt_recorded' or intent['receipt_json'] is None
                or ScientistIntentBinding.model_validate_json(intent['binding_json'], strict=True) != binding):
            raise ScientistAdmissionError('Successful resolution requires the exact original recorded receipt')
        original = dict(intent)
        admission, admission_sha256 = self.admission_history.read(request_id)
        try:
            resolver = self._resolver_factory(self, binding.model_copy(deep=True))
            if not callable(getattr(resolver, 'resolve_successful', None)):
                raise ScientistAdmissionError('Trusted resolver has no successful resolution method')
            result = resolver.resolve_successful(request_id)
            if inspect.isawaitable(result):
                result = await result
        except ScientistAdmissionError:
            raise
        except Exception as error:
            raise ScientistAdmissionError('Trusted successful resolution failed') from error
        self._current(self._state)
        if (client._active_request_id != request_id or client._active is not active_task
                or client._context is not context or not client.cleanup_pending):
            raise ScientistAdmissionError('Successful resolution lost its original active broker request')
        if connection.in_transaction or self._journal.binding != binding:
            raise ScientistAdmissionError('Successful resolution changed transaction or original binding')
        if not isinstance(result, ScientistRetainedResolutionResult):
            raise ScientistAdmissionError('Successful resolution requires a typed result')
        result = ScientistRetainedResolutionResult.model_validate(result.model_dump(), strict=True)
        row = connection.execute('SELECT * FROM scientist_turn_resolutions WHERE request_id=?',
                                 (request_id,)).fetchone()
        current_intent = connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                            (request_id,)).fetchone()
        current_admission, current_sha256 = self.admission_history.read(request_id)
        if (result.request_id != request_id or result.current_binding != binding
                or result.admission_record_sha256 != admission_sha256
                or current_sha256 != admission_sha256 or current_admission != admission
                or current_intent is None or dict(current_intent) != original or row is None
                or row['session_id'] != binding.session_id
                or row['admission_record_sha256'] != result.admission_record_sha256
                or row['control_id'] != result.reconcile_control_id
                or row['response_sha256'] != result.response_sha256
                or row['terminal_receipt_sha256'] != result.terminal_receipt_sha256
                or ScientistIntentBinding.model_validate_json(row['current_binding_json'], strict=True) != binding):
            raise ScientistAdmissionError('Successful resolution result differs from immutable original records')

    def create_retained_host(self, current_binding, **host_options):
        from .scientist_retained_host import ScientistRetainedHost

        if (self.admission_history is None or self.admission_history.record_version != '2.0'
                or self.admission_history.store is not self.controller.store
                or not isinstance(current_binding, ScientistIntentBinding)):
            raise ScientistAdmissionError('Retained host requires the same original admission history2.0')
        desktop = self.controller.state()
        if any(desktop[field] != getattr(current_binding, field)
               for field in ('session_id', 'runtime_id', 'owner', 'lease_id', 'generation')):
            raise ScientistAdmissionError('Retained host current controller binding differs')
        return ScientistRetainedHost(self.controller.store, self.admission_history,
                                     current_binding.model_copy(deep=True), **host_options)

    def rearm_retained_client(self, host, request_id, *, reconcile_control_id,
                              capability_control_id, response_sha256):
        from .scientist_retained_host import ScientistRetainedHost

        if (not isinstance(host, ScientistRetainedHost)
                or host.store is not self.controller.store
                or host.history is not self.admission_history
                or self.admission_history is None
                or self.admission_history.record_version != '2.0'
                or self.scheduler is None or self.scheduler.closed):
            raise ScientistAdmissionError('Rearm requires the retained original desktop runtime and history2.0')
        self.scheduler._scientist_engines()
        client = self.engine.client
        if not isinstance(client, ScientistAsyncTurnClient):
            raise ScientistAdmissionError('Rearm cannot replace the original broker client')

        def current():
            desktop = self.controller.state()
            if any(desktop[field] != getattr(host.binding, field)
                   for field in ('session_id', 'runtime_id', 'owner', 'lease_id', 'generation')):
                raise ScientistAdmissionError('Rearm retained host controller binding differs')
            self.scheduler._scientist_engines()
            if self.scheduler.closed:
                raise ScientistAdmissionError('Rearm desktop runtime is closed')

        def verify_resolution(expected_request_id):
            current()
            selected = host.inspect(reconcile_control_id,
                                    capability_control_id=capability_control_id)
            if selected['request']['target']['request_id'] != expected_request_id:
                raise ScientistAdmissionError('Rearm selected ACK belongs to another original request')
            resolution = host.resolve(reconcile_control_id, capability_control_id,
                                      response_sha256=response_sha256)
            if resolution['request_id'] != expected_request_id:
                raise ScientistAdmissionError('Rearm resolution belongs to another original request')
            current()

        current()
        client.rearm(request_id, verify_resolution=verify_resolution)


def create_scientist_desktop_scheduler(controller, settings, decider_pins, socket_path, *,
                                       confirm_runtime=_deny_runtime, authenticator=None,
                                       timeout_seconds=720, bonsai_manifest=None,
                                       validate_receipt=None, admission_history=None,
                                       output_contract=None, output_context_tokens=16384,
                                       bootstrap_capture=None, expected_bootstrap_peer=None,
                                       resolver_factory=None, **scheduler_options):
    if bootstrap_capture is not None or expected_bootstrap_peer is not None:
        if (not isinstance(bootstrap_capture, ScientistBootstrapCapture) or not callable(expected_bootstrap_peer)
                or not isinstance(admission_history, ScientistAdmissionHistory)
                or admission_history.record_version != '2.0' or admission_history.capture is not bootstrap_capture
                or admission_history.store is not controller.store or bootstrap_capture.store is not controller.store
                or output_contract is None):
            raise ScientistAdmissionError('Scientist bootstrap needs paired same-store capture/history2.0 and pinned output')
    if output_contract is not None:
        from .scientist_profile_output import admission_profile_validator
        if validate_receipt is not None:
            raise ValueError('Pinned output validation cannot replace another receipt callback')
        validate_receipt = admission_profile_validator(admission_history, output_contract,
                                                       context_tokens=output_context_tokens)
    binding = ScientistDesktopBinding(controller, confirm_runtime=confirm_runtime,
                                       admission_history=admission_history,
                                       output_validator=validate_receipt if output_contract is not None else None,
                                       bootstrap_capture=bootstrap_capture, expected_bootstrap_peer=expected_bootstrap_peer,
                                       resolver_factory=resolver_factory)
    client = ScientistAsyncTurnClient(socket_path, timeout_seconds=timeout_seconds,
        authenticator=authenticator, verify_admission=binding.verify_admission,
        persist_intent=binding.persist_intent, record_receipt=binding.record_receipt,
        validate_receipt=validate_receipt,
        prepare_infer=None if bootstrap_capture is None else binding.prepare_infer,
        resolve_successful=None if resolver_factory is None else binding.resolve_successful)
    engine = ScientistDecisionEngine(decider_pins, client, verify_state=binding.verify_state)
    supervisor = (None if bonsai_manifest is None else ScientistBonsaiVisionSupervisor(
        bonsai_manifest, client, verify_context=binding.verify_vision))
    binding.profiles = {'aos.decider.turn.v1': engine._deployment_digest}
    if supervisor is not None:
        binding.profiles['aos.bonsai.vision.v1'] = supervisor._deployment_digest
    if confirm_runtime(deepcopy(binding.profiles)) is not None:
        raise ScientistAdmissionError('Joint runtime verifier must complete or raise')
    scheduler = ScientistDesktopScheduler(controller, settings, engine, vision_supervisor=supervisor, **scheduler_options)
    binding.scheduler = scheduler
    binding.engine, binding.vision_supervisor = engine, supervisor
    scheduler.scientist_binding = binding
    return scheduler
