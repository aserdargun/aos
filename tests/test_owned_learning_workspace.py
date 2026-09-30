import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from aos.owned_learning_workspace import OwnedLearningWorkspace


class OwnedLearningWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'workspace'
        self.root.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def test_create_is_explicit_private_and_release_releases_lock(self):
        with self.assertRaises(FileNotFoundError):
            OwnedLearningWorkspace.acquire(self.root)
        self.assertFalse((self.root / '.learning.lock').exists())

        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        self.assertEqual((self.root / '.learning.lock').stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'busy'):
            OwnedLearningWorkspace.acquire(self.root)
        owner.close()
        owner.close()

        with OwnedLearningWorkspace.acquire(self.root):
            pass

    def test_inherited_descriptor_keeps_lock_until_child_exits(self):
        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        inherited_fd = owner.inherit_fd()
        script = (
            'import sys,time; '
            'from aos.owned_learning_workspace import OwnedLearningWorkspace; '
            'workspace=OwnedLearningWorkspace.adopt(sys.argv[1], int(sys.argv[2])); '
            'print("ready", flush=True); time.sleep(0.35); workspace.close()')
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        process = subprocess.Popen(
            [sys.executable, '-c', script, str(self.root), str(inherited_fd)],
            pass_fds=(inherited_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        owner.close()
        os.close(inherited_fd)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'busy'):
            OwnedLearningWorkspace.acquire(self.root)
        self.assertLess(time.monotonic() - started, 0.2)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, (stdout, stderr))

        with OwnedLearningWorkspace.acquire(self.root):
            pass

    def test_adoption_requires_same_already_held_file_description(self):
        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        inherited_fd = owner.inherit_fd()
        adopted = OwnedLearningWorkspace.adopt(self.root, inherited_fd)
        owner.close()
        os.close(inherited_fd)
        with self.assertRaisesRegex(ValueError, 'busy'):
            OwnedLearningWorkspace.acquire(self.root)
        adopted.close()
        with OwnedLearningWorkspace.acquire(self.root):
            pass

        unrelated_fd = os.open(self.root / '.learning.lock', os.O_RDWR)
        try:
            with self.assertRaisesRegex(ValueError, 'not_held'):
                OwnedLearningWorkspace.adopt(self.root, unrelated_fd)
        finally:
            os.close(unrelated_fd)

    def test_root_must_be_owner_only_and_symlink_free(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'directory_invalid'):
            OwnedLearningWorkspace.acquire(self.root, create=True)
        self.root.chmod(0o700)

        target = self.root.parent / 'real-workspace'
        target.mkdir(mode=0o700)
        linked = self.root.parent / 'linked-workspace'
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            OwnedLearningWorkspace.acquire(linked, create=True)

    def test_lock_rejects_public_hardlinked_and_symlinked_files(self):
        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        owner.close()
        lock_path = self.root / '.learning.lock'
        lock_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'lock_invalid'):
            OwnedLearningWorkspace.acquire(self.root)
        lock_path.chmod(0o600)

        hardlink = self.root / 'second-link'
        os.link(lock_path, hardlink)
        with self.assertRaisesRegex(ValueError, 'lock_invalid'):
            OwnedLearningWorkspace.acquire(self.root)
        hardlink.unlink()

        lock_path.unlink()
        target = self.root / 'target-lock'
        target.touch(mode=0o600)
        (self.root / '.learning.lock').symlink_to(target.name)
        with self.assertRaises(OSError):
            OwnedLearningWorkspace.acquire(self.root)

    def test_assert_current_detects_lock_and_root_path_replacement(self):
        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        lock_path = self.root / '.learning.lock'
        lock_path.rename(self.root / 'old-lock')
        lock_path.touch(mode=0o600)
        with self.assertRaisesRegex(ValueError, 'lock_changed'):
            owner.assert_current()
        owner.close()
        lock_path.unlink()
        (self.root / 'old-lock').rename(lock_path)

        owner = OwnedLearningWorkspace.acquire(self.root)
        moved = self.root.parent / 'moved-workspace'
        self.root.rename(moved)
        self.root.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'path_changed'):
            owner.assert_current()
        owner.close()

    def test_assert_current_does_not_reacquire_an_unlocked_descriptor(self):
        owner = OwnedLearningWorkspace.acquire(self.root, create=True)
        import fcntl

        fcntl.flock(owner.lock_fd, fcntl.LOCK_UN)
        with self.assertRaisesRegex(ValueError, 'lock_lost'):
            owner.assert_current()
        owner.close()


if __name__ == '__main__':
    unittest.main()
