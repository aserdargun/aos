"""Exclusive lifetime lock for a private, owned learning workspace."""

import fcntl
import os
import stat
from pathlib import Path

from .workspace_identity import open_existing_workspace


_LOCK_NAME = '.learning.lock'
_DIRECTORY_FIELDS = ('st_dev', 'st_ino', 'st_uid', 'st_mode')
_FILE_FIELDS = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size')


def _private_directory(metadata):
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('owned_learning_workspace_directory_invalid')


def _private_lock_file(metadata):
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1 or metadata.st_size != 0):
        raise ValueError('owned_learning_workspace_lock_invalid')


def _same_fields(first, second, fields):
    return all(getattr(first, field) == getattr(second, field)
               for field in fields)


class OwnedLearningWorkspace:
    """Hold an exclusive flock and descriptor-pinned workspace identity."""

    def __init__(self, root, directory_fd, lock_fd):
        self.root = Path(root).absolute()
        self.directory_fd = directory_fd
        self.lock_fd = lock_fd
        self._closed = False
        self._directory_identity = os.fstat(directory_fd)
        self._lock_identity = os.fstat(lock_fd)

    @classmethod
    def acquire(cls, root: Path, *, create: bool = False):
        if type(create) is not bool:
            raise ValueError('owned_learning_workspace_create_flag_invalid')
        root = Path(root).absolute()
        if '..' in root.parts:
            raise ValueError('owned_learning_workspace_path_invalid')
        directory_fd = open_existing_workspace(root)
        lock_fd = None
        try:
            _private_directory(os.fstat(directory_fd))
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if create:
                try:
                    lock_fd = os.open(
                        _LOCK_NAME, flags | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=directory_fd)
                    os.fchmod(lock_fd, 0o600)
                    os.fsync(lock_fd)
                    os.fsync(directory_fd)
                except FileExistsError:
                    lock_fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
            else:
                lock_fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
            cls._verify_lock_link(directory_fd, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('owned_learning_workspace_busy') from error
            instance = cls(root, directory_fd, lock_fd)
            instance.assert_current()
            directory_fd = lock_fd = None
            return instance
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @classmethod
    def adopt(cls, root: Path, inherited_fd: int):
        """Adopt only the exact already-locked file description from the owner."""
        root = Path(root).absolute()
        if '..' in root.parts or type(inherited_fd) is not int or inherited_fd < 0:
            raise ValueError('owned_learning_workspace_adoption_invalid')
        directory_fd = open_existing_workspace(root)
        lock_fd = None
        probe_fd = None
        try:
            _private_directory(os.fstat(directory_fd))
            lock_fd = os.dup(inherited_fd)
            os.set_inheritable(lock_fd, False)
            cls._verify_lock_link(directory_fd, lock_fd)
            probe_fd = os.open(
                _LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd)
            cls._verify_lock_link(directory_fd, probe_fd)
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe_fd, fcntl.LOCK_UN)
                raise ValueError('owned_learning_workspace_inherited_lock_not_held')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('owned_learning_workspace_inherited_lock_mismatch') from error
            instance = cls(root, directory_fd, lock_fd)
            instance.assert_current()
            directory_fd = lock_fd = None
            return instance
        finally:
            if probe_fd is not None:
                os.close(probe_fd)
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _verify_lock_link(directory_fd, lock_fd):
        metadata = os.fstat(lock_fd)
        _private_lock_file(metadata)
        try:
            linked = os.stat(_LOCK_NAME, dir_fd=directory_fd,
                             follow_symlinks=False)
        except OSError as error:
            raise ValueError('owned_learning_workspace_lock_changed') from error
        if (not _same_fields(metadata, linked, _FILE_FIELDS)
                or not stat.S_ISREG(linked.st_mode)):
            raise ValueError('owned_learning_workspace_lock_changed')

    @property
    def closed(self):
        return self._closed

    def inherit_fd(self):
        self.assert_current()
        descriptor = os.dup(self.lock_fd)
        os.set_inheritable(descriptor, False)
        return descriptor

    def assert_current(self):
        if self._closed:
            raise ValueError('owned_learning_workspace_closed')
        _private_directory(self._directory_identity)
        _private_lock_file(self._lock_identity)
        if (not _same_fields(os.fstat(self.directory_fd), self._directory_identity,
                             _DIRECTORY_FIELDS)
                or not _same_fields(os.fstat(self.lock_fd), self._lock_identity,
                                    _FILE_FIELDS)):
            raise ValueError('owned_learning_workspace_identity_changed')
        try:
            current_directory_fd = open_existing_workspace(self.root)
        except OSError as error:
            raise ValueError('owned_learning_workspace_path_changed') from error
        try:
            current_directory = os.fstat(current_directory_fd)
            if not _same_fields(current_directory, self._directory_identity,
                                _DIRECTORY_FIELDS):
                raise ValueError('owned_learning_workspace_path_changed')
            self._verify_lock_link(current_directory_fd, self.lock_fd)
        finally:
            os.close(current_directory_fd)
        probe_fd = os.open(_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=self.directory_fd)
        try:
            self._verify_lock_link(self.directory_fd, probe_fd)
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ValueError('owned_learning_workspace_lock_lost') from error
                return
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
            raise ValueError('owned_learning_workspace_lock_lost')
        finally:
            os.close(probe_fd)

    def close(self):
        if self._closed:
            return
        self._closed = True
        lock_fd, directory_fd = self.lock_fd, self.directory_fd
        self.lock_fd = self.directory_fd = -1
        os.close(lock_fd)
        os.close(directory_fd)

    def __enter__(self):
        self.assert_current()
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()
