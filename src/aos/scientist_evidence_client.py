import hashlib
import json
import logging
import math
import os
import socket
import stat
import struct
import threading
import time
from copy import deepcopy
from pathlib import Path

from .scientist_evidence_transport import RESPONSE_LIMIT, ScientistEvidenceCodec
from .scientist_transport import (
    BrokerPeer, ScientistAdmissionError, ScientistUncertainTurn, SystemdBrokerAuthenticator,
)


def _deny_authorization(request, peer):
    raise ScientistAdmissionError('Trusted original-target evidence authorization is not configured')


def _deny_intent(frame, fingerprint, deadline, peer):
    raise ScientistAdmissionError('Durable evidence control-intent persistence is not configured')


def _failure_locations(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            frames.append({'function': trace.tb_frame.f_code.co_name[:128], 'line': trace.tb_lineno})
            if len(frames) > 8:
                del frames[4]
            trace = trace.tb_next
        chain.append({'type': type(error).__name__[:128], 'frames': frames})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain


class ScientistEvidenceClient:
    def __init__(self, socket_path: Path, *, codec: ScientistEvidenceCodec, timeout_seconds=10,
                 authenticator=None, authorize=_deny_authorization, persist_intent=_deny_intent,
                 record_response=None):
        if (not isinstance(socket_path, Path) or not socket_path.is_absolute()
                or '..' in socket_path.parts):
            raise ValueError('Evidence socket requires an explicitly configured absolute path')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10):
            raise ValueError('Evidence timeout must be finite, positive and at most 10 seconds')
        if not isinstance(codec, ScientistEvidenceCodec):
            raise TypeError('Evidence client requires an explicitly pinned codec')
        if not callable(authorize) or not callable(persist_intent):
            raise TypeError('Evidence callbacks must be trusted explicit callables')
        if record_response is not None and not callable(record_response):
            raise TypeError('Evidence response persistence must be an explicit trusted callable')
        self.socket_path = socket_path
        self.codec = codec
        self.timeout_seconds = timeout_seconds
        self.authenticator = authenticator if authenticator is not None else SystemdBrokerAuthenticator()
        self.authorize = authorize
        self.persist_intent = persist_intent
        self.record_response = record_response
        self._lock = threading.Lock()
        self._attempted = set()
        self._uncertain_control_id = None

    @property
    def uncertain_control_id(self):
        return self._uncertain_control_id

    @staticmethod
    def _socket_identity(path):
        parent = path.parent
        parent_info, info = parent.lstat(), path.lstat()
        if (parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077
                or path.is_symlink() or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ScientistAdmissionError('Evidence socket and parent must be private and user-owned')
        return parent_info.st_dev, parent_info.st_ino, info.st_dev, info.st_ino

    @staticmethod
    def _remaining(deadline, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise ScientistAdmissionError('Evidence waiting cancelled; remote outcome is not proven')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Evidence control deadline expired')
        return remaining

    def exchange(self, request, *, cancel_event=None):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Evidence client already has an active control request')
        attempted = False
        control_id = None
        stage = 'configuration'
        try:
            codec, authorize, persist = self.codec, self.authorize, self.persist_intent
            record_response = self.record_response
            authenticator, path, timeout = self.authenticator, self.socket_path, self.timeout_seconds
            if authorize is _deny_authorization or persist is _deny_intent:
                raise ScientistAdmissionError('Evidence authorization and durable intent must be explicitly configured')
            if self.uncertain_control_id is not None:
                raise ScientistAdmissionError('Uncertain evidence dispatch requires trusted reconciliation')
            raw = codec.encode_request(request)
            frozen = codec.decode_request(raw)
            control_id = frozen['control_id']
            if control_id in self._attempted or len(self._attempted) >= 256:
                raise ScientistAdmissionError('Evidence control replay or client lifetime bound reached')
            deadline = time.monotonic() + timeout

            def remaining():
                return self._remaining(deadline, cancel_event)

            def authorized(peer):
                remaining()
                if authorize(deepcopy(frozen), deepcopy(peer)) is not None:
                    raise ScientistAdmissionError('Evidence authorization must complete or raise')
                remaining()

            def current(peer, identity):
                remaining()
                if authenticator.still_current(deepcopy(peer)) is not True:
                    raise ScientistAdmissionError('Evidence peer generation changed')
                if self._socket_identity(path) != identity:
                    raise ScientistAdmissionError('Evidence socket identity changed')
                remaining()

            def authenticated_peer(credentials):
                peer_pid, peer_uid, _peer_gid = struct.unpack('3i', credentials)
                peer = authenticator.authenticate(peer_pid, peer_uid)
                if (not isinstance(peer, BrokerPeer) or peer.pid != peer_pid or peer.uid != peer_uid
                        or peer.uid != os.getuid() or peer.pid <= 1):
                    raise ScientistAdmissionError('Evidence authenticator differs from socket peer credentials')
                remaining()
                return deepcopy(peer)

            remaining()
            identity = self._socket_identity(path)
            stage = 'probe_connect'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(remaining())
                probe.connect(str(path))
                credentials = probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            stage = 'probe_authentication'
            peer = authenticated_peer(credentials)
            stage = 'pre_intent_authorization'
            current(peer, identity)
            authorized(peer)
            self._attempted.add(control_id)
            stage = 'intent_persistence'
            if persist(raw, hashlib.sha256(raw).hexdigest(), deadline, deepcopy(peer)) is not None:
                raise ScientistAdmissionError('Evidence intent writer must durably complete or raise')
            stage = 'post_intent_authorization'
            authorized(peer)
            current(peer, identity)
            stage = 'dispatch_connect'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(remaining())
                connection.connect(str(path))
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                stage = 'dispatch_authentication'
                if authenticated_peer(credentials) != peer:
                    raise ScientistAdmissionError('Evidence dispatch peer differs from its staged generation')
                stage = 'dispatch_authorization'
                authorized(peer)
                if self._socket_identity(path) != identity:
                    raise ScientistAdmissionError('Evidence socket changed during final authorization')
                connection.settimeout(remaining())
                attempted = True
                stage = 'frame_send'
                connection.sendall(raw + b'\n')
                stage = 'response_read'
                response = bytearray()
                while True:
                    timeout = remaining()
                    connection.settimeout(timeout if cancel_event is None else min(.1, timeout))
                    try:
                        chunk = connection.recv(min(8192, RESPONSE_LIMIT + 1 - len(response)))
                    except socket.timeout:
                        if cancel_event is None:
                            raise
                        continue
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > RESPONSE_LIMIT:
                        raise ScientistAdmissionError('Evidence response exceeds its frame bound')
                if not response.endswith(b'\n') or response.count(b'\n') != 1:
                    raise ScientistAdmissionError('Evidence response must be exactly one LF-terminated frame followed by EOF')
                stage = 'response_decode'
                value = codec.decode_response(bytes(response[:-1]), raw)
                stage = 'response_authorization'
                authorized(peer)
                current(peer, identity)
                authorized(peer)
                if self._socket_identity(path) != identity:
                    raise ScientistAdmissionError('Evidence socket changed during final authorization')
                remaining()
                if record_response is not None:
                    stage = 'response_persistence'
                    if record_response(deepcopy(value), deepcopy(peer)) is not None:
                        raise ScientistAdmissionError('Evidence response writer must durably complete or raise')
                    current(peer, identity)
                    authorized(peer)
                    if self._socket_identity(path) != identity:
                        raise ScientistAdmissionError('Evidence socket changed during response persistence')
                    remaining()
                return value
        except BaseException as error:
            logging.getLogger(__name__).warning('Scientist evidence control failure: %s', json.dumps(
                {'stage': stage, 'attempted': attempted, 'locations': _failure_locations(error)}, sort_keys=True))
            if attempted:
                self._uncertain_control_id = control_id
                if isinstance(error, Exception):
                    raise ScientistUncertainTurn('Evidence control outcome is uncertain; automatic retry is forbidden') from error
            raise
        finally:
            self._lock.release()
