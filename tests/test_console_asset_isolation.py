import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT


specification = importlib.util.spec_from_file_location('isolated_console_assets', REPO_ROOT / 'scripts/serve_desktop.py')
SERVE = importlib.util.module_from_spec(specification)
specification.loader.exec_module(SERVE)


class ConsoleAssetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='synthetic-console-assets-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image_hash = 'a' * 64
        self.runtime = SimpleNamespace(pins={'image_id': 'sha256:' + self.image_hash},
                                       container_id='synthetic-owned-container', docker=Mock())

    def test_explicit_private_copy_does_not_overwrite_shared_assets(self):
        shared = self.root / 'data/desktop-console-assets' / self.image_hash
        shared.mkdir(parents=True)
        sentinel = shared / 'vnc.html'
        sentinel.write_text('synthetic-active-user-content')
        private = self.root / 'isolated-runtime/console-assets'

        def copy_assets(arguments):
            (Path(arguments[2]) / 'vnc.html').write_text('synthetic-isolated-content')

        self.runtime.docker.side_effect = copy_assets
        with patch.object(SERVE, 'REPO_ROOT', self.root):
            target = SERVE.prepare_console_assets(self.runtime, private)
        self.assertEqual(target, private / self.image_hash)
        self.assertEqual(sentinel.read_text(), 'synthetic-active-user-content')
        self.assertEqual((target / 'vnc.html').read_text(), 'synthetic-isolated-content')
        self.runtime.docker.assert_called_once_with(
            ['cp', 'synthetic-owned-container:/usr/share/novnc/.', str(target)])

    def test_unspecified_root_preserves_existing_default(self):
        with patch.object(SERVE, 'REPO_ROOT', self.root):
            target = SERVE.prepare_console_assets(self.runtime)
        self.assertEqual(target, self.root / 'data/desktop-console-assets' / self.image_hash)

    def test_relative_root_denied_without_copy(self):
        with self.assertRaises(ValueError):
            SERVE.prepare_console_assets(self.runtime, Path('relative-assets'))
        self.runtime.docker.assert_not_called()

    def test_symlink_root_denied_without_copy(self):
        shared = self.root / 'shared'
        shared.mkdir()
        private = self.root / 'linked'
        private.symlink_to(shared, target_is_directory=True)
        with self.assertRaises(ValueError):
            SERVE.prepare_console_assets(self.runtime, private)
        self.runtime.docker.assert_not_called()

    def test_symlink_image_target_denied_without_copy(self):
        private = self.root / 'private'
        private.mkdir()
        shared = self.root / 'shared'
        shared.mkdir()
        (private / self.image_hash).symlink_to(shared, target_is_directory=True)
        with self.assertRaises(ValueError):
            SERVE.prepare_console_assets(self.runtime, private)
        self.runtime.docker.assert_not_called()
