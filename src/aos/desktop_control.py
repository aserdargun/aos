import threading

from .contracts import AOSFault, ErrorCode, canonical, identifier, now
from .desktop import DesktopRuntime
from .session_binding import runtime_binding
from .storage import TrajectoryStore


class DesktopController:
    def __init__(self, store: TrajectoryStore, runtime: DesktopRuntime):
        self.store = store
        self.runtime = runtime
        self.lock = threading.RLock()
        self.session_id = identifier('desktop-session')
        binding = runtime_binding(runtime, self.session_id, 0) if isinstance(runtime, DesktopRuntime) else None
        with store.connection:
            store.insert('desktop_sessions', session_id=self.session_id, runtime_id=runtime.runtime_id,
                         image_id=runtime.pins['image_id'], owner='AGENT', lease_id=identifier('lease'), generation=0,
                         status='running', created_at=now(), updated_at=now())
            if binding is not None:
                self.record_binding(binding)

    def record_binding(self, binding):
        self.store.insert('desktop_events', event_id=identifier('event'), session_id=self.session_id,
                          kind='runtime_binding', payload_json=binding.model_dump_json(), created_at=now())

    def state(self) -> dict:
        return dict(self.store.connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?', (self.session_id,)).fetchone())

    def control(self, command: str) -> dict:
        with self.lock:
            if command not in {'pause', 'take-control', 'return-control', 'resume', 'stop', 'restart'}:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Unknown desktop control')
            state = self.state()
            if state['status'] == 'stopped' and command != 'restart':
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Stopped desktop requires explicit restart')
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_inputs SET status='cancelled' WHERE session_id=? AND status='queued'", (self.session_id,))
                self.store.connection.execute("UPDATE desktop_sessions SET owner='PAUSED',status='paused',lease_id=?,generation=generation+1,updated_at=? WHERE session_id=?",
                                              (identifier('lease'), now(), self.session_id))
                self.store.insert('desktop_events', event_id=identifier('event'), session_id=self.session_id,
                                  kind=command, payload_json=canonical({'previous_owner': state['owner'], 'previous_generation': state['generation']}), created_at=now())
            owner, status = 'PAUSED', 'paused'
            if command == 'stop':
                self.runtime.stop()
                status = 'stopped'
            elif command == 'restart':
                self.runtime.restart()
            elif command == 'take-control':
                owner, status = 'HUMAN', 'running'
            elif command in {'return-control', 'resume'}:
                evidence = self.runtime.perform('probe', {})
                if not all(evidence.get(key) for key in ('display', 'xfce', 'note')) or not evidence.get('vnc', '').startswith('RFB '):
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Fresh desktop readiness verification failed')
                owner, status = 'AGENT', 'running'
            with self.store.connection:
                if command == 'restart' and isinstance(self.runtime, DesktopRuntime):
                    self.record_binding(runtime_binding(self.runtime, self.session_id, self.state()['generation']))
                self.store.connection.execute('UPDATE desktop_sessions SET owner=?,status=?,runtime_id=?,updated_at=? WHERE session_id=?',
                                              (owner, status, self.runtime.runtime_id, now(), self.session_id))
            return self.state()

    def enqueue(self, lease_id: str, generation: int) -> str:
        with self.lock:
            state = self.state()
            if state['owner'] != 'AGENT' or state['status'] != 'running' or state['lease_id'] != lease_id or state['generation'] != generation:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Desktop input ownership is stale')
            input_id = identifier('input')
            with self.store.connection:
                self.store.insert('desktop_inputs', input_id=input_id, session_id=self.session_id, lease_id=lease_id,
                                  generation=generation, tool='type_note', status='queued', created_at=now())
            return input_id

    def execute(self, input_id: str) -> dict:
        with self.lock:
            row = self.store.connection.execute('SELECT * FROM desktop_inputs WHERE input_id=? AND session_id=?', (input_id, self.session_id)).fetchone()
            state = self.state()
            if not row or row['status'] != 'queued' or state['owner'] != 'AGENT' or state['status'] != 'running' or row['lease_id'] != state['lease_id'] or row['generation'] != state['generation']:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Queued desktop input is cancelled or stale')
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_inputs SET status='running' WHERE input_id=?", (input_id,))
            try:
                self.runtime.perform('type_note', {})
                actual = self.runtime.perform('read_note', {})
                if actual != {'text': 'AOS desktop input'}:
                    raise AOSFault(ErrorCode.TOOL_FAILURE, 'Independent desktop input verification failed')
            except BaseException:
                with self.store.connection:
                    self.store.connection.execute("UPDATE desktop_inputs SET status='uncertain' WHERE input_id=?", (input_id,))
                raise
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_inputs SET status='ok',result_json=? WHERE input_id=?", (canonical(actual), input_id))
            return actual
