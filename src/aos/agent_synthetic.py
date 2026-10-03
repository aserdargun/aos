import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from pydantic import Field, model_validator

from .agent_contracts import (
    AgentAdmissionError, AgentCleanupProof, AgentHandle, AgentJobRequest,
    AgentObservation, AgentPrepared, AgentResultProof,
)
from .contracts import TypedModel, canonical, digest, identifier


class SyntheticAgentPayload(TypedModel):
    text: str = Field(max_length=4096)
    delay_ms: int = Field(ge=0, le=10000)

    @model_validator(mode='after')
    def bounded_bytes(self):
        if len(self.text.encode()) > 4096:
            raise ValueError('Synthetic text exceeds its byte budget')
        return self


@dataclass
class _OwnedProcess:
    process: subprocess.Popen
    deadline: float
    handle_sha256: str
    cancelled: bool = False
    timed_out: bool = False


class SyntheticAgentAdapter:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root != self.root.resolve(strict=True):
            raise AgentAdmissionError('Synthetic workspace root must not traverse symlinks')
        self._directory = self._open_directory(self.root)
        self._owned: dict[str, _OwnedProcess] = {}

    @staticmethod
    def _open_directory(path, *, parent=None):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            os.close(descriptor)
            raise AgentAdmissionError('Synthetic directories must be private and user-owned')
        return descriptor

    @staticmethod
    def _read(directory, filename):
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > 65536):
                raise AgentAdmissionError('Synthetic artifact is not a bounded private regular file')
            raw = os.read(descriptor, 65537)
            if len(raw) > 65536:
                raise AgentAdmissionError('Synthetic artifact exceeded its size limit')
            return raw
        finally:
            os.close(descriptor)

    @staticmethod
    def _write(directory, filename, value):
        raw = canonical(value).encode()
        if len(raw) > 65536:
            raise AgentAdmissionError('Synthetic record exceeds its byte budget')
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as output:
                output.write(raw)
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)

    @staticmethod
    def _validate_request(request):
        request = AgentJobRequest.model_validate_json(request.model_dump_json(), strict=True)
        SyntheticAgentPayload.model_validate(request.payload, strict=True)
        if (request.operation != 'synthetic.write.v1' or request.budget.wall_seconds > 30
                or request.budget.model_tokens != 0 or request.budget.experiments != 0):
            raise AgentAdmissionError('Synthetic CPU operation cannot request model or experiment resources')
        return request

    def _workspace(self, handle):
        if (set(handle.payload) != {'request', 'workspace_name', 'workspace_device', 'workspace_inode'}
                or handle.payload['workspace_name'] != handle.handle_id):
            raise AgentAdmissionError('Synthetic handle workspace differs')
        request = self._validate_request(AgentJobRequest.model_validate(handle.payload['request'], strict=True))
        if (request.request_sha256() != handle.request_sha256 or request.job_id != handle.job_id
                or request.authority != handle.authority):
            raise AgentAdmissionError('Synthetic handle differs from its original request')
        descriptor = self._open_directory(handle.handle_id, parent=self._directory)
        try:
            metadata = os.fstat(descriptor)
            if (type(handle.payload['workspace_device']) is not int or type(handle.payload['workspace_inode']) is not int
                    or (metadata.st_dev, metadata.st_ino) != (handle.payload['workspace_device'], handle.payload['workspace_inode'])):
                raise AgentAdmissionError('Synthetic workspace identity changed')
            owned = self._owned.get(handle.handle_id)
            if owned is not None and owned.handle_sha256 != digest(handle.model_dump(mode='json')):
                raise AgentAdmissionError('Synthetic process belongs to a different exact handle')
            if self._read(descriptor, 'request.json') != canonical(request.model_dump(mode='json')).encode():
                raise AgentAdmissionError('Synthetic on-disk request differs')
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, request

    async def prepare(self, request):
        request = self._validate_request(request)
        name = identifier('synthetic-instance')
        os.mkdir(name, mode=0o700, dir_fd=self._directory)
        directory = self._open_directory(name, parent=self._directory)
        try:
            metadata = os.fstat(directory)
            self._write(directory, 'request.json', request.model_dump(mode='json'))
        finally:
            os.close(directory)
        return AgentPrepared(handle=AgentHandle(job_id=request.job_id, request_sha256=request.request_sha256(),
            authority=request.authority, handle_id=name,
            payload={'workspace_name': name, 'workspace_device': metadata.st_dev, 'workspace_inode': metadata.st_ino,
                     'request': request.model_dump(mode='json')}), ready=True)

    async def dispatch(self, prepared):
        if not prepared.ready:
            raise AgentAdmissionError('Synthetic job is not prepared')
        handle = prepared.handle
        directory, request = self._workspace(handle)
        try:
            self._write(directory, 'dispatch.json', {'request_sha256': handle.request_sha256,
                'synthetic': True, 'replay_permitted': False})
            process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('agent_synthetic_worker.py')),
                str(directory), handle.request_sha256], cwd=f'/proc/self/fd/{directory}',
                env={'LANG': 'C.UTF-8', 'CUDA_VISIBLE_DEVICES': '', 'PYTHONNOUSERSITE': '1'},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(directory,), start_new_session=True)
            self._owned[handle.handle_id] = _OwnedProcess(process, time.monotonic() + request.budget.wall_seconds,
                                                        digest(handle.model_dump(mode='json')))
        finally:
            os.close(directory)
        return handle

    async def _stop(self, owned):
        if owned.process.poll() is None:
            owned.process.terminate()
            try:
                await asyncio.to_thread(owned.process.wait, timeout=1)
            except subprocess.TimeoutExpired:
                owned.process.kill()
                await asyncio.to_thread(owned.process.wait, timeout=1)

    async def observe(self, handle):
        directory, _request = self._workspace(handle)
        os.close(directory)
        owned = self._owned.get(handle.handle_id)
        if owned is None:
            return AgentObservation(handle=handle, state='uncertain', detail='Owned process identity unavailable; no replay')
        if owned.process.poll() is None and time.monotonic() >= owned.deadline:
            owned.timed_out = True
            await self._stop(owned)
        code = owned.process.poll()
        state = ('running' if code is None else 'cancelled' if owned.cancelled else 'failed'
                 if owned.timed_out or code != 0 else 'succeeded')
        return AgentObservation(handle=handle, state=state, detail='Fixed synthetic CPU worker; no model or GPU')

    async def request_cancel(self, handle):
        directory, _request = self._workspace(handle)
        os.close(directory)
        owned = self._owned.get(handle.handle_id)
        if owned is None:
            return AgentObservation(handle=handle, state='uncertain', detail='Unknown process identity; cancellation not attempted')
        if owned.process.poll() is None:
            owned.cancelled = True
            await self._stop(owned)
        return await self.observe(handle)

    async def verify_result(self, handle):
        observation = await self.observe(handle)
        if observation.state not in {'succeeded', 'failed', 'cancelled'}:
            return AgentResultProof(handle=handle, verified=False, outcome='failed')
        owned = self._owned[handle.handle_id]
        if observation.state != 'succeeded':
            evidence = {'request_sha256': handle.request_sha256, 'pid': owned.process.pid,
                        'exit_code': owned.process.poll(), 'cancelled': owned.cancelled, 'timed_out': owned.timed_out}
            return AgentResultProof(handle=handle, verified=True, outcome=observation.state,
                                    evidence_sha256=digest(evidence))
        directory, request = self._workspace(handle)
        try:
            raw = self._read(directory, 'result.json')
            expected = {'schema_version': 'aos.synthetic-result.v1', 'synthetic': True, 'job_id': handle.job_id,
                        'request_sha256': handle.request_sha256, 'text': request.payload['text'],
                        'text_sha256': hashlib.sha256(request.payload['text'].encode()).hexdigest()}
            verified = raw == canonical(expected).encode()
            return AgentResultProof(handle=handle, verified=verified, outcome='succeeded' if verified else 'failed',
                                    evidence_sha256=hashlib.sha256(raw).hexdigest() if verified else None)
        except (OSError, ValueError):
            return AgentResultProof(handle=handle, verified=False, outcome='failed')
        finally:
            os.close(directory)

    async def verify_cleanup(self, handle):
        directory, _request = self._workspace(handle)
        os.close(directory)
        owned = self._owned.get(handle.handle_id)
        verified = owned is not None and owned.process.poll() is not None
        evidence = None if not verified else digest({'handle': handle.model_dump(mode='json'),
            'pid': owned.process.pid, 'exit_code': owned.process.returncode, 'synthetic_cpu_only': True})
        return AgentCleanupProof(handle=handle, verified=verified, scope='local_process', evidence_sha256=evidence)

    async def close(self):
        for owned in self._owned.values():
            if owned.process.poll() is None:
                owned.cancelled = True
                await self._stop(owned)
        if self._directory is not None:
            os.close(self._directory)
            self._directory = None
