import asyncio
from concurrent.futures import Future
import threading
import time

from .scientist_protocol import ScientistTurnRequest
from .scientist_transport import (
    ScientistAdmissionError, ScientistTurnClient, _deny_unconfirmed, _deny_unpersisted,
    _deny_unresolved,
)


def _deny_receipt(receipt, peer):
    raise ScientistAdmissionError('Durable broker receipt persistence is not configured')


class ScientistHostBridge:
    def __init__(self, loop, cancelled, deadline):
        self.loop, self.cancelled, self.deadline = loop, cancelled, deadline

    def invoke(self, callback, *arguments):
        future = Future()

        def invoke():
            if future.cancelled():
                return
            try:
                if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                    raise ScientistAdmissionError('Host callback lost its authority or deadline')
                result = callback(*arguments)
                if not future.done():
                    future.set_result(result)
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)

        self.loop.call_soon_threadsafe(invoke)
        try:
            return future.result(timeout=max(0, self.deadline - time.monotonic()))
        except BaseException:
            future.cancel()
            self.cancelled.set()
            raise


class ScientistAsyncTurnClient:
    def __init__(self, socket_path, *, timeout_seconds=720, authenticator=None,
                 verify_admission=_deny_unconfirmed, persist_intent=_deny_unpersisted,
                 record_receipt=_deny_receipt, validate_receipt=None, prepare_infer=None,
                 resolve_successful=None):
        if validate_receipt is not None and not callable(validate_receipt):
            raise ValueError('Scientist receipt validator must be an explicit callable')
        if prepare_infer is not None and not callable(prepare_infer):
            raise ValueError('Scientist pre-infer preparation must be an explicit async callable')
        if resolve_successful is not None and not callable(resolve_successful):
            raise ValueError('Scientist successful resolution must be an explicit async callable')
        self._prepare_infer = prepare_infer
        self._resolve_successful = resolve_successful
        self._callbacks = (verify_admission, persist_intent, record_receipt, validate_receipt)
        self._context = None
        self._active = None
        self._active_request_id = None
        self._cancelled = False
        self._cancelled_request_id = None
        self.client = ScientistTurnClient(socket_path, timeout_seconds=timeout_seconds,
            authenticator=authenticator, verify_admission=lambda *arguments: self._host(0, arguments),
            persist_intent=lambda *arguments: self._host(1, arguments),
            record_receipt=lambda *arguments: self._host(2, arguments),
            validate_receipt=(None if validate_receipt is None else
                              lambda *arguments: self._host(3, arguments)))

    @property
    def cleanup_pending(self):
        return self._active is not None and not self._active.done()

    @property
    def uncertain_request_id(self):
        return self.client.uncertain_request_id

    def rearm(self, request_id, *, verify_resolution=_deny_unresolved):
        if self.cleanup_pending:
            raise ScientistAdmissionError('Active Scientist task prevents client rearm')
        self.client._rearm(request_id, verify_resolution=verify_resolution,
                           cancelled_request_id=self._cancelled_request_id)
        self._cancelled = False
        self._cancelled_request_id = None
        self._active = None
        self._active_request_id = None
        self._context = None

    def _host(self, index, arguments):
        loop, cancelled, deadline = self._context
        return ScientistHostBridge(loop, cancelled, deadline).invoke(self._callbacks[index], *arguments)

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    def _finish_task(self, task):
        if self._active is task and task.done():
            self._active = None
            self._active_request_id = None

    async def infer(self, request):
        if self._cancelled or self.cleanup_pending:
            raise ScientistAdmissionError('Cancelled or active broker turn requires trusted reconciliation')
        request = ScientistTurnRequest.model_validate(request.model_dump(), strict=True)
        cancelled = threading.Event()
        self._context = (asyncio.get_running_loop(), cancelled, time.monotonic() + self.client.timeout_seconds)
        self._active_request_id = request.request_id
        prepare = self._prepare_infer
        deadline = self._context[2]

        async def run():
            expiry = asyncio.get_running_loop().call_later(max(0, deadline - time.monotonic()), cancelled.set)
            try:
                if prepare is not None:
                    if await prepare(request.model_copy(deep=True), cancel_event=cancelled, deadline=deadline) is not None:
                        raise ScientistAdmissionError('Pre-infer preparation must complete or raise')
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise ScientistAdmissionError('Pre-infer preparation lost its authority or deadline')
                receipt = await asyncio.to_thread(self.client.infer, request, cancel_event=cancelled, deadline=deadline)
                if self._resolve_successful is not None:
                    remaining = deadline - time.monotonic()
                    if cancelled.is_set() or remaining <= 0:
                        raise ScientistAdmissionError('Successful resolution lost its original deadline')
                    try:
                        result = await asyncio.wait_for(self._resolve_successful(request.request_id), remaining)
                    except TimeoutError as error:
                        raise ScientistAdmissionError('Successful resolution exceeded its original deadline') from error
                    if result is not None or cancelled.is_set() or time.monotonic() >= deadline:
                        raise ScientistAdmissionError('Successful resolution must complete within original authority')
                return receipt
            finally:
                expiry.cancel()

        task = asyncio.create_task(run())
        self._active = task
        task.add_done_callback(self._finish_task)
        try:
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            self._cancelled = True
            self._cancelled_request_id = request.request_id
            cancelled.set()
            task.add_done_callback(self._consume)
            raise
        finally:
            self._finish_task(task)
