import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from aos import scientist_shared_launch as launch


class SharedLaunchSocketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aos-launch-socket-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'reviewed.sock'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(str(self.path))
        self.path.chmod(0o600)
        self.server.listen(2)
        self.server.settimeout(1)
        self.clock = 100.0
        self.connector = launch.SharedLaunchSocketConnector(self.path, clock=lambda: self.clock)

    def test_private_real_socket_connects_with_credentials_and_original_timeout(self):
        with self.connector(102.0) as channel:
            peer, _address = self.server.accept()
            with peer:
                self.assertEqual(channel.gettimeout(), 2.0)
                self.assertEqual(channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED), 1)
                channel.sendall(b'synthetic')
                self.assertEqual(peer.recv(32), b'synthetic')

    def test_invalid_paths_are_rejected_before_socket_creation(self):
        for value in ('relative.sock', '/tmp/../x', '/tmp//x', '/tmp/x\x00', '/' + 'x' * 108):
            with self.subTest(value=value), patch.object(launch.socket, 'socket') as factory:
                with self.assertRaises(ValueError):
                    launch.SharedLaunchSocketConnector(value)
                factory.assert_not_called()

    def test_expired_nonfinite_boolean_or_excessive_deadline_never_connects(self):
        for deadline in (99.0, 100.0, 103.1, float('nan'), float('inf'), True):
            with self.subTest(deadline=deadline), patch.object(launch.socket, 'socket') as factory:
                with self.assertRaises(ValueError):
                    self.connector(deadline)
                factory.assert_not_called()

    def test_public_directory_or_socket_is_rejected(self):
        for selected in (self.root, self.path):
            original = selected.stat().st_mode & 0o777
            selected.chmod(0o777)
            try:
                with patch.object(launch.socket, 'socket') as factory:
                    with self.assertRaisesRegex(ValueError, 'private'):
                        self.connector(102.0)
                    factory.assert_not_called()
            finally:
                selected.chmod(original)

    def test_regular_file_or_socket_alias_is_rejected(self):
        regular = self.root / 'file'
        regular.write_text('synthetic')
        regular.chmod(0o600)
        alias = self.root / 'alias'
        alias.symlink_to(self.path)
        parent_alias = self.root / 'directory-alias'
        parent_alias.symlink_to(self.root, target_is_directory=True)
        for selected in (regular, alias, parent_alias / self.path.name):
            with self.subTest(selected=selected), patch.object(launch.socket, 'socket') as factory:
                with self.assertRaises(ValueError):
                    launch.SharedLaunchSocketConnector(selected, clock=lambda: self.clock)(102.0)
                factory.assert_not_called()

    def test_foreign_owner_is_rejected(self):
        with patch.object(launch.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, 'user-owned'):
                self.connector(102.0)

    def connect_after(self, effect):
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        channel = Mock(wraps=raw)

        def connect(path):
            raw.connect(path)
            effect()

        channel.connect.side_effect = connect
        with patch.object(launch.socket, 'socket', return_value=channel):
            with self.assertRaises((ValueError, OSError)):
                self.connector(102.0)
        channel.close.assert_called_once()
        self.assertEqual(raw.fileno(), -1)
        self.assertEqual(channel.connect.call_count, 1)

    def test_deadline_expiry_after_connect_closes_without_retry(self):
        self.connect_after(lambda: setattr(self, 'clock', 102.0))

    def test_socket_replacement_after_connect_closes_without_retry(self):
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)

        def replace():
            self.path.unlink()
            replacement.bind(str(self.path))
            self.path.chmod(0o600)

        self.connect_after(replace)
        self.assertEqual(replacement.getsockname(), str(self.path))

    def test_parent_replacement_after_connect_closes_without_retry(self):
        def replace():
            retired = self.root.with_name(self.root.name + '-old')
            self.root.rename(retired)
            self.addCleanup(lambda: retired.rmdir())
            self.addCleanup(lambda: (retired / self.path.name).unlink())
            self.root.mkdir(mode=0o700)

        self.connect_after(replace)

    def test_connect_failure_closes_once(self):
        self.server.close()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        channel = Mock(wraps=raw)
        with patch.object(launch.socket, 'socket', return_value=channel):
            with self.assertRaises(ConnectionRefusedError):
                self.connector(102.0)
        channel.close.assert_called_once()
        self.assertEqual(channel.connect.call_count, 1)
