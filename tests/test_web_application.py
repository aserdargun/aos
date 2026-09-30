import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, canonical
from aos.web_application import (MAX_PROFILE_BYTES, WebApplicationProfile, WebApplicationProfiles,
                                 WebApplicationReport, profile_report, read_source)


FIXTURE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())


class ProfileContractTests(unittest.TestCase):
    def profile(self, **changes):
        return WebApplicationProfile.model_validate({**copy.deepcopy(FIXTURE['profile']), **changes})

    def test_canonical_synthetic_fixture_and_schemas_match_runtime(self):
        self.assertTrue(FIXTURE['synthetic'])
        profile = self.profile()
        report = profile_report(profile)
        self.assertEqual(report.model_dump(), FIXTURE['report'])
        for filename, model, payload in (('web_application_profile', WebApplicationProfile, profile.model_dump()),
                                         ('web_application_report', WebApplicationReport, report.model_dump())):
            schema = json.loads((REPO_ROOT / f'schemas/{filename}.schema.json').read_text())
            jsonschema.Draft202012Validator(schema).validate(payload)
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'}, model.model_json_schema())

    def test_role_preferences_are_separate_and_never_authorize_execution(self):
        for system1 in ('disabled', 'requested'):
            for system2 in ('disabled', 'requested'):
                profile = self.profile(learning={**FIXTURE['profile']['learning'], 'system1': system1, 'system2': system2})
                report = profile_report(profile)
                self.assertEqual(report.requested_learning_roles, [role for role, value in (
                    ('system1', system1), ('system2', system2)) if value == 'requested'])
                self.assertFalse(report.execution_authorized)
                self.assertFalse(report.collection_authorized)
                self.assertFalse(report.training_ready)
                self.assertFalse(report.promotion_authorized)
                self.assertEqual(len(report.blockers), 4 if 'requested' in (system1, system2) else 2)

    def test_enabled_learning_requires_rights_reference_but_reference_is_not_a_review(self):
        with self.assertRaises(ValueError):
            self.profile(learning={**FIXTURE['profile']['learning'], 'data_rights_ref': None})
        profile = self.profile(learning={**FIXTURE['profile']['learning'], 'system1': 'disabled',
                                        'system2': 'disabled', 'data_rights_ref': None})
        self.assertEqual(profile_report(profile).requested_learning_roles, [])
        self.assertIn('rights_review_not_verified', profile_report(self.profile()).blockers)

    def test_invalid_urls_and_noncanonical_origins_are_rejected(self):
        for url in ('https://user:password@crm.example.invalid/app/', 'https://crm.example.invalid/?token=secret',
                    'https://crm.example.invalid/#token', 'javascript:alert(1)', 'file:///etc/passwd',
                    '//crm.example.invalid/app/', 'http://crm.example.invalid/', 'https://*.example.invalid/',
                    'https://crm.example.invalid:443/app/', 'https://CRM.EXAMPLE.INVALID/app/',
                    'https://crm.example.invalid\\@attacker.invalid/', 'https://crm.example.invalid/%2e%2e/admin',
                    'https://crm.example.invalid/../admin', 'https://crm.example.invalid//admin',
                    'https://crm.example.invalid:0/', 'https://crm.example.invalid:65536/',
                    'https://crm.example.invalid/app/\n', 'https://127.1/', 'https://2130706433/',
                    'https://0x7f000001/', 'https://169.254.169.254/', 'https://10.0.0.1/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.profile(entry_url=url)

    def test_entry_and_declared_origin_must_match_exactly(self):
        for entry in ('https://other.example.invalid/app/', 'https://crm.example.invalid:8443/app/',
                      'https://crm.example.invalid.attacker.invalid/app/'):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.profile(entry_url=entry)
        for origin in ('https://crm.example.invalid/', 'https://crm.example.invalid/path',
                       'https://*.example.invalid', 'https://crm.example.invalid?secret'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.profile(allowed_origins=[origin])

    def test_local_test_only_accepts_exact_loopback_and_staging_rejects_it(self):
        for origin in ('http://localhost:3000', 'http://127.0.0.1:8000', 'http://[::1]:8080'):
            profile = self.profile(environment='local_test', entry_url=origin + '/', allowed_origins=[origin])
            self.assertFalse(profile_report(profile).execution_authorized)
            with self.assertRaises(ValueError):
                self.profile(entry_url=origin + '/', allowed_origins=[origin])
        with self.assertRaises(ValueError):
            self.profile(environment='local_test')
        with self.assertRaises(ValueError):
            self.profile(environment='local_test', entry_url='http://127.0.0.2/', allowed_origins=['http://127.0.0.2'])

    def test_duplicate_scope_rejected_and_set_order_is_canonical(self):
        for changes in ({'task_keys': ['find-record', 'find-record']},
                        {'allowed_origins': ['https://crm.example.invalid', 'https://crm.example.invalid']}):
            with self.assertRaises(ValueError):
                self.profile(**changes)
        first = self.profile(task_keys=['find-record', 'update-draft'])
        second = self.profile(task_keys=['update-draft', 'find-record'])
        self.assertEqual(profile_report(first).profile_sha256, profile_report(second).profile_sha256)

    def test_no_inline_credentials_unknown_fields_or_automatic_privileges(self):
        for field in ('password', 'cookie', 'token', 'authorization', 'script', 'execution_authorized'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.profile(**{field: 'synthetic-secret'})
        for field in ('raw_screenshots', 'automatic_training', 'automatic_promotion'):
            for value in (True, 0, 1, 'false'):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.profile(learning={**FIXTURE['profile']['learning'], field: value})

    def test_revision_lineage_and_bounds(self):
        for changes in ({'revision': 0}, {'revision': True}, {'revision': 101}, {'revision': 2},
                        {'previous_sha256': 'a' * 64}, {'application_key': '../escape'}, {'task_keys': []},
                        {'allowed_origins': []}, {'task_keys': ['task-' + str(index) for index in range(33)]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.profile(**changes)

    def test_report_cannot_hide_blockers_or_grant_authority(self):
        report = copy.deepcopy(FIXTURE['report'])
        for field in ('execution_authorized', 'collection_authorized', 'training_ready', 'promotion_authorized'):
            for value in (True, 0):
                with self.assertRaises(ValueError):
                    WebApplicationReport.model_validate({**report, field: value})
        for changes in ({'blockers': []}, {'blockers': report['blockers'][:2]},
                        {'requested_learning_roles': ['system1', 'system1']}, {'status': 'active'}):
            with self.assertRaises(ValueError):
                WebApplicationReport.model_validate({**report, **changes})


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(copy.deepcopy(FIXTURE['profile']))
        self.checksum = profile_report(self.profile).profile_sha256

    def register(self, profile=None):
        profile = profile or self.profile
        return self.store.register(profile, confirm_sha256=profile_report(profile).profile_sha256)

    def test_preview_and_absent_list_do_not_create_files(self):
        self.assertEqual(self.store.preview(self.profile), profile_report(self.profile))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.root.exists())

    def test_successor_preview_checks_immutable_lineage_without_writing(self):
        successor = WebApplicationProfile.model_validate({**self.profile.model_dump(),
            'revision': 2, 'previous_sha256': self.checksum, 'task_keys': ['find-record']})
        with self.assertRaises(FileNotFoundError):
            self.store.preview(successor)
        self.assertFalse(self.store.root.exists())
        self.register()
        self.assertEqual(self.store.preview(successor), profile_report(successor))
        self.assertEqual(len(self.store.list()), 1)
        for changes in ({'tenant_key': 'different'}, {'account_role': 'admin'},
                        {'application_key': 'different'}, {'revision': 3},
                        {'previous_sha256': '0' * 64}):
            candidate = WebApplicationProfile.model_validate({**successor.model_dump(), **changes})
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                self.store.preview(candidate)
        self.assertEqual(len(self.store.list()), 1)

    def test_exact_confirmation_required_before_any_mutation(self):
        for confirmation in ('', '0' * 64, None):
            with self.assertRaises(ValueError):
                self.store.register(self.profile, confirm_sha256=confirmation)
        self.assertFalse(self.store.root.exists())

    def test_private_registration_is_idempotent_and_survives_reopen(self):
        self.register()
        path = self.store.root / (self.checksum + '.json')
        before = path.stat()
        self.assertEqual(stat.S_IMODE(before.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.register()
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        reopened = WebApplicationProfiles(self.store.root)
        self.assertEqual(reopened.get(self.checksum), self.profile)
        self.assertEqual(reopened.list(), [FIXTURE['report']])

    def test_revision_appends_without_replacing_parent_or_creating_active_pointer(self):
        self.register()
        changed = WebApplicationProfile.model_validate({**self.profile.model_dump(), 'revision': 2,
                    'previous_sha256': self.checksum, 'task_keys': ['find-record']})
        report = self.register(changed)
        self.assertNotEqual(report.profile_sha256, self.checksum)
        self.assertEqual(self.store.get(self.checksum), self.profile)
        self.assertEqual(self.store.get(report.profile_sha256), changed)
        self.assertEqual(len(self.store.list()), 2)
        self.assertTrue(all(item['status'] == 'draft' for item in self.store.list()))

    def test_cross_tenant_role_application_and_revision_skips_rejected(self):
        self.register()
        for changes in ({'tenant_key': 'different'}, {'account_role': 'admin'},
                        {'application_key': 'different'}, {'revision': 3}):
            candidate = WebApplicationProfile.model_validate({**self.profile.model_dump(), 'revision': 2,
                            'previous_sha256': self.checksum, **changes})
            with self.assertRaises(ValueError):
                self.register(candidate)
        self.assertEqual(len(self.store.list()), 1)

    def test_missing_and_tampered_parent_fail_closed(self):
        candidate = WebApplicationProfile.model_validate({**self.profile.model_dump(), 'revision': 2,
                                                          'previous_sha256': self.checksum})
        with self.assertRaises(FileNotFoundError):
            self.register(candidate)
        self.register()
        report = self.register(candidate)
        (self.store.root / (self.checksum + '.json')).write_text('{}')
        with self.assertRaises(ValueError):
            self.store.get(report.profile_sha256)
        with self.assertRaises(ValueError):
            self.store.list()

    def test_tamper_noncanonical_bytes_and_hardlinks_rejected(self):
        self.register()
        path = self.store.root / (self.checksum + '.json')
        original = path.read_bytes()
        for content in (original + b'\n', original.replace(b'editor', b'admin')):
            path.write_bytes(content)
            with self.assertRaises(ValueError):
                self.store.get(self.checksum)
        path.write_bytes(original)
        os.link(path, self.root / 'hardlink')
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)

    def test_symlink_directory_file_and_permissive_mode_rejected(self):
        self.register()
        path = self.store.root / (self.checksum + '.json')
        (self.root / 'alias').symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaises(OSError):
            WebApplicationProfiles(self.root / 'alias').get(self.checksum)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        path.unlink()
        path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):
            self.store.get(self.checksum)
        self.store.root.chmod(0o755)
        with self.assertRaises(ValueError):
            self.store.list()

    def test_interrupted_unpublished_write_cleans_temporary_file(self):
        with patch('aos.web_application.os.link', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.register()
        self.assertEqual(list(self.store.root.iterdir()), [])
        self.register()
        self.assertEqual(len(self.store.list()), 1)

    def test_crash_after_publication_is_detected_without_repair_or_replay(self):
        source = self.root / 'source.json'
        source.write_text(canonical(self.profile.model_dump()))
        program = '''
import os
from pathlib import Path
import sys
from unittest.mock import patch
from aos.web_application import WebApplicationProfiles, read_source, profile_report
original_link = os.link
def crash_after_link(*arguments, **keywords):
    original_link(*arguments, **keywords)
    os._exit(77)
profile = read_source(Path(sys.argv[2]))
with patch('aos.web_application.os.link', crash_after_link):
    WebApplicationProfiles(Path(sys.argv[1])).register(profile, confirm_sha256=profile_report(profile).profile_sha256)
'''
        result = subprocess.run([sys.executable, '-c', program, str(self.store.root), str(source)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 77, result.stderr)
        before = {path.name: path.read_bytes() for path in self.store.root.iterdir()}
        self.assertEqual(len(before), 2)
        self.assertEqual((self.store.root / (self.checksum + '.json')).stat().st_nlink, 2)
        for operation in (self.store.list, lambda: self.store.get(self.checksum), self.register):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual({path.name: path.read_bytes() for path in self.store.root.iterdir()}, before)

    def test_existing_destination_is_never_overwritten(self):
        original_link = os.link
        def publish_conflict(source, destination, **keywords):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                 dir_fd=keywords['dst_dir_fd'])
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(b'{}')
            original_link(source, destination, **keywords)
        with patch('aos.web_application.os.link', publish_conflict), self.assertRaises(ValueError):
            self.register()
        self.assertEqual((self.store.root / (self.checksum + '.json')).read_bytes(), b'{}')
        self.assertEqual(len(list(self.store.root.iterdir())), 1)

    def test_mutated_nested_scope_is_revalidated_and_store_size_is_bounded(self):
        self.profile.allowed_origins.append('https://outside.example.invalid/')
        with self.assertRaises(ValueError):
            self.store.register(self.profile, confirm_sha256=self.checksum)
        self.assertFalse(self.store.root.exists())
        self.profile.allowed_origins.pop()
        with patch('aos.web_application.MAX_PROFILES', 0), self.assertRaises(ValueError):
            self.register()
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_unknown_store_file_and_invalid_checksum_rejected(self):
        self.register()
        (self.store.root / 'unknown.txt').write_text('synthetic')
        with self.assertRaises(ValueError):
            self.store.list()
        for checksum in ('../escape', 'a' * 63, None):
            with self.assertRaises(ValueError):
                self.store.get(checksum)

    def test_source_duplicate_keys_nonregular_symlink_and_size_rejected(self):
        source = self.root / 'source.json'
        source.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
        with self.assertRaises(ValueError):
            read_source(source)
        source.write_bytes(b'x' * (MAX_PROFILE_BYTES + 1))
        with self.assertRaises(ValueError):
            read_source(source)
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(ValueError):
            read_source(source)
        source.unlink()
        source.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):
            read_source(source)

    def test_cli_preview_register_inspect_list_and_redacted_errors(self):
        source = self.root / 'source.json'
        source.write_text(json.dumps(self.profile.model_dump()))
        command = [sys.executable, '-m', 'aos.web_application', '--store', str(self.store.root)]
        def invoke(*arguments):
            return subprocess.run([*command, *arguments], capture_output=True, text=True, timeout=10)
        preview = invoke('preview', '--profile', str(source))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)['profile_sha256'], self.checksum)
        self.assertFalse(self.store.root.exists())
        successor = self.root / 'successor.json'
        successor.write_text(json.dumps({**self.profile.model_dump(), 'revision': 2,
                                          'previous_sha256': self.checksum, 'task_keys': ['find-record']}))
        self.assertEqual(invoke('preview', '--profile', str(successor)).returncode, 1)
        self.assertFalse(self.store.root.exists())
        registered = invoke('register', '--profile', str(source), '--confirm-sha256', self.checksum)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(invoke('preview', '--profile', str(successor)).stdout)['revision'], 2)
        inspected = invoke('inspect', '--sha256', self.checksum)
        self.assertEqual(json.loads(inspected.stdout), FIXTURE['report'])
        self.assertEqual(len(json.loads(invoke('list').stdout)['profiles']), 1)
        source.write_text(json.dumps({**self.profile.model_dump(), 'password': 'DO-NOT-LOG-SYNTHETIC-SECRET'}))
        failed = invoke('preview', '--profile', str(source))
        self.assertEqual(failed.returncode, 1)
        self.assertNotIn('DO-NOT-LOG-SYNTHETIC-SECRET', failed.stdout + failed.stderr)
        self.assertEqual(len(self.store.list()), 1)


if __name__ == '__main__':
    unittest.main()
