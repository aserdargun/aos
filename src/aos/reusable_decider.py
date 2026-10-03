import asyncio
import json
import math
import os
import signal
from contextlib import suppress
from copy import deepcopy
from uuid import uuid4

from .contracts import AOSFault, ErrorCode, REPO_ROOT, canonical, digest
from .decision import DeciderEngine, decision_request


class ReusableDeciderEngine(DeciderEngine):
    def __init__(self, *arguments, cpu_prewarm=False, idle_seconds=60,
                 gpu_idle_seconds=0, **keywords):
        if type(cpu_prewarm) is not bool or type(idle_seconds) not in (int, float) or not 0 < idle_seconds <= 600:
            raise ValueError('CPU prewarm requires an explicit boolean and bounded idle deadline')
        if (type(gpu_idle_seconds) not in (int, float) or not 0 <= gpu_idle_seconds <= 120
                or gpu_idle_seconds and not cpu_prewarm):
            raise ValueError('GPU idle retention requires CPU prewarm and at most 120 seconds')
        super().__init__(*arguments, **keywords)
        self.process = None
        self.lock = asyncio.Lock()
        self.preparation = None
        self.preparation_metrics = {}
        self.cpu_prewarm = cpu_prewarm
        self.idle_seconds = idle_seconds
        self.gpu_idle_seconds = gpu_idle_seconds
        self.gpu_idle = False
        self.idle = None
        self.job_active = False
        self.claimed_preparation = None
        self.closing = None
        self.cleanup_pending = False
        self.cleanup_task = None

    def prewarm_status(self):
        state = 'inactive'
        if (self.gpu_idle and self.idle is not None and not self.idle.done()
                and self.process is not None and self.process.returncode is None):
            state = 'ready_gpu'
        elif self.cpu_prewarm and self.idle is not None and not self.idle.done():
            if self.preparation is not None and not self.preparation.done():
                state = 'preparing'
            elif (self.preparation is not None and not self.preparation.cancelled()
                  and self.preparation.exception() is None and self.process is not None
                  and self.process.returncode is None):
                state = 'ready'
        return {'enabled': self.cpu_prewarm, 'state': state}

    def prewarm_idle(self):
        if not self.cpu_prewarm:
            return
        if self.gpu_idle:
            return
        if self.preparation is not None or self.process is not None or self.job_active or self.idle is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Idle preparation requires a fresh unused worker')
        self.preparation = asyncio.create_task(self.prepare_cpu(idle=True))
        self.idle = asyncio.create_task(self.expire_idle(self.preparation))

    async def expire_idle(self, preparation, seconds=None):
        try:
            if preparation is not None:
                await asyncio.shield(preparation)
            await asyncio.sleep(self.idle_seconds if seconds is None else seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if self.idle is asyncio.current_task():
            await self.close()

    def begin_job(self):
        if not self.cpu_prewarm:
            return
        if self.job_active:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Decider job already claimed')
        self.job_active = True
        idle, self.idle = self.idle, None
        if self.gpu_idle:
            self.gpu_idle = False
            if idle is not None:
                idle.cancel()
            self.preparation = asyncio.create_task(self.prepare_gpu_job(idle, self.closing))
            return
        previous = self.preparation
        self.claimed_preparation = previous
        if idle is not None:
            idle.cancel()
        self.preparation = asyncio.create_task(self.prepare_job(previous, idle, self.closing))

    async def prepare_gpu_job(self, idle, closing):
        if closing is not None:
            await asyncio.shield(closing)
        if idle is not None:
            with suppress(asyncio.CancelledError):
                await idle
        if self.process is None or self.process.returncode is not None:
            await self.prepare_cpu()
            return
        response = await self.request({'operation': 'admit_job_gpu'})
        if (response.get('deployment_digest') != digest(self.pins) or response.get('job_admitted') is not True
                or response.get('cuda_initialized') is not True or response.get('gpu_resident') is not True):
            await self.stop_process()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Decider GPU job admission identity or device differs')
        self.preparation_metrics = {**self.preparation_metrics, **response}

    async def park_gpu(self):
        if (not self.gpu_idle_seconds or not self.job_active or self.gpu_idle
                or self.process is None or self.process.returncode is not None
                or self.preparation is not None and not self.preparation.done()):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'GPU idle retention requires a completed active job')
        response = await self.request({'operation': 'prepare_idle_gpu'})
        if (response.get('deployment_digest') != digest(self.pins)
                or response.get('idle_prepared') is not True
                or response.get('cuda_initialized') is not True
                or response.get('job_released') is not True):
            await self.stop_process()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Decider GPU idle identity or device differs')
        self.job_active = False
        self.preparation = None
        self.claimed_preparation = None
        self.preparation_metrics = response
        self.gpu_idle = True
        self.idle = asyncio.create_task(self.expire_idle(None, self.gpu_idle_seconds))

    async def prepare_job(self, previous, idle, closing):
        if closing is not None:
            await asyncio.shield(closing)
        if idle is not None:
            with suppress(asyncio.CancelledError):
                await idle
        if previous is None:
            await self.prepare_cpu()
            return
        await previous
        if self.process is None or self.process.returncode is not None:
            await self.prepare_cpu()
            return
        response = await self.request({'operation': 'admit_job_cpu'})
        if (response.get('deployment_digest') != digest(self.pins) or response.get('job_admitted') is not True
                or response.get('cuda_initialized') is not False):
            await self.stop_process()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Decider job admission identity or device differs')
        self.preparation_metrics = {**self.preparation_metrics, **response}

    def prepare_in_background(self):
        if self.preparation is not None or self.process is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'CPU preparation requires a fresh worker')
        self.preparation = asyncio.create_task(self.prepare_cpu())

    async def prepare_cpu(self, *, idle=False):
        response = await self.request({'operation': 'prepare_idle_cpu' if idle else 'prepare_cpu'})
        if (response.get('deployment_digest') != digest(self.pins) or response.get('prepared_cpu') is not True
                or response.get('cuda_initialized') is not False
                or idle and response.get('idle_prepared') is not True):
            await self.stop_process()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Decider CPU preparation identity or device differs')
        self.preparation_metrics = response

    async def start(self):
        if self.cleanup_pending or self.process is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Previous worker requires verified cleanup before startup')
        idle_argument = ([f'--idle-seconds={math.ceil(max(75, self.idle_seconds + 30, self.gpu_idle_seconds + 30))}']
                         if self.cpu_prewarm else [])
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            str(self.python), str(REPO_ROOT / 'services/decider/worker.py'), str(self.manifest),
            *idle_argument, '--serve',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                 'TOKENIZERS_PARALLELISM': 'false'}, start_new_session=True, limit=65536))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            await self.stop_process()
            raise

    async def close(self):
        current = asyncio.current_task()
        while self.closing is not None and self.closing is not current:
            await asyncio.shield(self.closing)
        self.closing = current
        idle, self.idle = self.idle, None
        preparations = {task for task in (self.preparation, self.claimed_preparation) if task is not None}
        self.preparation = None
        self.claimed_preparation = None
        self.job_active = False
        self.gpu_idle = False
        self.preparation_metrics = {}
        try:
            if idle is not None and idle is not current:
                idle.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await idle
            for preparation in preparations:
                if not preparation.done():
                    preparation.cancel()
            for preparation in preparations:
                with suppress(asyncio.CancelledError, Exception):
                    await preparation
            await self.stop_process()
        finally:
            if self.closing is current:
                self.closing = None

    async def stop_process(self):
        process = self.process
        if process is None:
            return
        self.cleanup_pending = True
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.stdin:
            process.stdin.close()
        draining = self.cleanup_task
        if draining is None or draining.done():
            draining = asyncio.create_task(process.communicate())
            self.cleanup_task = draining
        try:
            await asyncio.wait_for(asyncio.shield(draining), 5)
        except asyncio.CancelledError:
            await asyncio.wait_for(asyncio.shield(draining), 5)
            if process.returncode is not None and self.process is process:
                self.process = None
                self.cleanup_pending = False
                self.cleanup_task = None
            raise
        if process.returncode is None or self.process is not process:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Worker cleanup identity or exit is unproven')
        self.process = None
        self.cleanup_pending = False
        self.cleanup_task = None

    async def decide(self, state, options):
        self.last_request = None
        self.last_response = None
        try:
            if self.cpu_prewarm and not self.job_active:
                self.begin_job()
            if self.preparation is not None:
                await asyncio.shield(self.preparation)
            response = await self.request({'request': decision_request(state, options)})
            prediction = self.parse_response(canonical(response).encode(), options)
            self.last_response = prediction.model_dump(mode='json')
            return prediction
        except BaseException as error:
            if not (isinstance(error, AOSFault) and error.code == ErrorCode.UNSAFE_ACTION and self.lock.locked()):
                await self.close()
            raise

    async def request(self, body):
        if self.cleanup_pending:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Worker cleanup is unproven; new requests are blocked')
        if self.lock.locked():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reusable Decider requires one active request')
        async with self.lock:
            self.last_metrics = {}
            request_id = uuid4().hex
            try:
                payload = canonical({'request_id': request_id, **body}).encode() + b'\n'
                if len(payload) > 65536:
                    raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Decider request exceeded bound')
                async with asyncio.timeout(self.timeout):
                    if self.process is None or self.process.returncode is not None:
                        await self.stop_process()
                        await self.start()
                    if 'request' in body:
                        dispatched_request = json.loads(payload)['request']
                        if self.before_decision_dispatch is not None:
                            self.before_decision_dispatch(deepcopy(dispatched_request))
                        self.last_request = deepcopy(dispatched_request)
                    self.process.stdin.write(payload)
                    await self.process.stdin.drain()
                    try:
                        response = await self.process.stdout.readline()
                    except ValueError:
                        raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Reusable Decider response exceeded bound') from None
                    if not response:
                        raise AOSFault(ErrorCode.MODEL_FAILURE, 'Reusable Decider worker ended before response')
                    if not response.endswith(b'\n') or len(response) > 65536:
                        raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Reusable Decider response exceeded bound or incomplete')
                    try:
                        parsed = json.loads(response)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Reusable Decider response is not valid JSON') from None
                    if not isinstance(parsed, dict) or parsed.pop('request_id', None) != request_id:
                        raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Reusable Decider response belongs to another request')
                    return parsed
            except BaseException as error:
                await self.stop_process()
                if isinstance(error, TimeoutError):
                    raise AOSFault(ErrorCode.TIMEOUT, 'Reusable Decider timed out; no action executed') from None
                if isinstance(error, (ValueError, OSError)):
                    raise AOSFault(ErrorCode.MODEL_FAILURE, 'Reusable Decider worker failed') from None
                raise
