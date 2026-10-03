import hashlib
import json
from pathlib import Path
import tomllib
import unittest

from scripts.package_handoff import collect_source_paths


ROOT = Path(__file__).resolve().parents[1]


class SourceLicenseTests(unittest.TestCase):
    def test_official_license_bytes_are_preserved(self):
        self.assertEqual(hashlib.sha256((ROOT / 'LICENSE').read_bytes()).hexdigest(),
                         'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30')

    def test_package_metadata_agrees_and_python_includes_license(self):
        python = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
        native = tomllib.loads((ROOT / 'ui/src-tauri/Cargo.toml').read_text())['package']
        web = json.loads((ROOT / 'ui/package.json').read_text())
        for package in (python, native, web):
            self.assertEqual(package['license'], 'Apache-2.0')
        self.assertEqual(python['license-files'], ['LICENSE'])

    def test_license_and_scope_guide_are_in_source_allowlist(self):
        paths = collect_source_paths(ROOT)
        self.assertIn('LICENSE', paths)
        self.assertIn('docs/LICENSING.md', paths)


if __name__ == '__main__':
    unittest.main()
