import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.remote_page_draft_seed import private_seed_output, read_private_seed, seed_output_path, write_private_draft
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
FIXTURE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())


class SiteKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
        self.profile_sha256 = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha256)
        self.store = SiteKnowledgeStore(self.root / 'knowledge', self.profiles)
        self.page = SitePageDraft.model_validate(copy.deepcopy(FIXTURE['page']))
        self.checksum = digest(self.page.model_dump())

    def test_synthetic_fixture_matches_canonical_schema(self):
        self.assertTrue(FIXTURE['synthetic'])
        schema = json.loads((REPO_ROOT / 'schemas/site_page_draft.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SitePageDraft.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(self.page.model_dump())
        self.assertEqual(self.page.profile_sha256, self.profile_sha256)
        self.assertFalse(self.page.execution_authorized)
        self.assertFalse(self.page.collection_authorized)
        self.assertFalse(self.page.training_ready)

    def test_route_seed_output_requires_new_file_in_private_data_directory(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as directory:
            private = Path(directory)
            output = private / 'page.json'
            write_private_draft(output, self.page)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(SitePageDraft.model_validate_json(output.read_bytes()), self.page)
            with self.assertRaises(FileExistsError):
                write_private_draft(output, self.page)
            linked = private / 'linked.json'
            linked.symlink_to(output)
            with self.assertRaises(FileExistsError):
                write_private_draft(linked, self.page)
            private.chmod(0o755)
            with self.assertRaises(ValueError):
                write_private_draft(private / 'other.json', self.page)
        fake_repository = self.root / 'fake-repository'
        fake_repository.mkdir(mode=0o700)
        (fake_repository / 'data').mkdir(mode=0o700)
        with (patch('aos.remote_page_draft_seed.REPO_ROOT', fake_repository),
              self.assertRaises(ValueError)):
            write_private_draft(self.root / 'outside.json', self.page)

    def test_route_seed_ui_output_is_profile_scoped_and_private(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            output = private_seed_output(root, self.profile_sha256, 'entry')
            self.assertEqual(output, root / self.profile_sha256 / 'entry.json')
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
            write_private_draft(output, self.page)
            self.assertEqual(read_private_seed(output, self.checksum), self.page)
            with self.assertRaises(ValueError):
                read_private_seed(output, '0' * 64)
            with self.assertRaises(FileExistsError):
                write_private_draft(private_seed_output(root, self.profile_sha256, 'entry'), self.page)
            output.chmod(0o644)
            with self.assertRaises(ValueError):
                read_private_seed(output, self.checksum)
            output.chmod(0o600)
            shortcut = output.parent / 'shortcut.json'
            shortcut.symlink_to(output)
            with self.assertRaises(OSError):
                read_private_seed(shortcut, self.checksum)
            with self.assertRaises(ValueError):
                private_seed_output(root, self.profile_sha256, '../escape')
            with self.assertRaises(ValueError):
                private_seed_output(root, '0' * 63, 'entry')
        with self.assertRaises(ValueError):
            private_seed_output(self.root / 'outside', self.profile_sha256, 'entry')

    def test_managed_session_seed_roots_write_and_read_private_synthetic_drafts(self):
        data = self.root / 'data'
        data.mkdir(mode=0o700)
        with patch('aos.remote_page_draft_seed.REPO_ROOT', self.root):
            for manager in ('local-app-v1', 'local-app-project-synthetic'):
                session = data / manager / ('app-' + 'a' * 32)
                session.parent.mkdir(mode=0o700)
                session.mkdir(mode=0o700)
                for leaf in ('site-page-seeds', 'json-page-seeds'):
                    with self.subTest(manager=manager, leaf=leaf):
                        root = session / leaf
                        output = private_seed_output(root, self.profile_sha256, 'entry')
                        self.assertEqual(output, root / self.profile_sha256 / 'entry.json')
                        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                        self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
                        write_private_draft(output, self.page)
                        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(read_private_seed(output, self.checksum), self.page)

    def test_nested_seed_scope_rejects_nonmanager_session_leaf_traversal_and_symlinks(self):
        data = self.root / 'data'
        data.mkdir(mode=0o700)
        manager = data / 'local-app-v1'
        manager.mkdir(mode=0o700)
        session = manager / ('app-' + 'a' * 32)
        session.mkdir(mode=0o700)
        seed_root = session / 'site-page-seeds'
        seed_root.mkdir(mode=0o700)
        manager_alias = data / 'local-app-project-alias'
        manager_alias.symlink_to(manager, target_is_directory=True)
        session_alias = manager / ('app-' + 'b' * 32)
        session_alias.symlink_to(session, target_is_directory=True)
        leaf_alias = session / 'json-page-seeds'
        leaf_alias.symlink_to(seed_root, target_is_directory=True)
        with patch('aos.remote_page_draft_seed.REPO_ROOT', self.root):
            for root in (
                data / 'other' / session.name / 'site-page-seeds',
                data / 'local-app-test-' / session.name / 'site-page-seeds',
                data / 'local-app-project-INVALID' / session.name / 'site-page-seeds',
                manager / 'app-invalid' / 'site-page-seeds',
                manager / ('app-' + 'A' * 32) / 'site-page-seeds',
                session / 'other-seeds', seed_root / 'nested',
                session / '..' / session.name / 'site-page-seeds',
                manager_alias / session.name / 'site-page-seeds',
                session_alias / 'site-page-seeds', leaf_alias,
            ):
                with self.subTest(root=root), self.assertRaises(ValueError):
                    private_seed_output(root, self.profile_sha256, 'entry')
            self.assertEqual(list(seed_root.iterdir()), [])

    def test_managed_seed_parent_requires_private_owner_before_creation(self):
        data = self.root / 'data'
        data.mkdir(mode=0o700)
        manager = data / 'local-app-v1'
        manager.mkdir(mode=0o700)
        session = manager / ('app-' + 'a' * 32)
        session.mkdir(mode=0o700)
        seed_root = session / 'site-page-seeds'
        with patch('aos.remote_page_draft_seed.REPO_ROOT', self.root):
            for directory in (manager, session):
                directory.chmod(0o755)
                with self.assertRaises(ValueError):
                    private_seed_output(seed_root, self.profile_sha256, 'entry')
                directory.chmod(0o700)
            other_uid = os.getuid() + 1
            with patch('aos.lifecycle.os.getuid', return_value=other_uid), self.assertRaises(ValueError):
                seed_output_path(seed_root, self.profile_sha256, 'entry')
            self.assertFalse(seed_root.exists())

    def test_private_immutable_registration_restart_and_stale_signal(self):
        self.assertEqual(self.store.register(self.page, confirm_sha256=self.checksum), self.checksum)
        self.assertEqual(self.store.register(self.page, confirm_sha256=self.checksum), self.checksum)
        self.assertEqual(SiteKnowledgeStore(self.store.root, self.profiles).get(self.checksum), self.page)
        self.assertEqual((self.store.root / (self.checksum + '.json')).stat().st_mode & 0o777, 0o600)
        match = self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                                   current_fingerprint_sha256=self.page.page_fingerprint_sha256)
        self.assertEqual(match['status'], 'draft_match')
        self.assertFalse(match['execution_authorized'])
        self.assertFalse(match['collection_authorized'])
        self.assertFalse(match['training_ready'])
        changed = self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                                     current_fingerprint_sha256='b' * 64)
        self.assertEqual(changed['status'], 'stale')
        revised_profile = WebApplicationProfile.model_validate({**self.profile.model_dump(),
            'revision': 2, 'previous_sha256': self.profile_sha256, 'task_keys': ['find-record']})
        revised_sha256 = profile_report(revised_profile).profile_sha256
        self.profiles.register(revised_profile, confirm_sha256=revised_sha256)
        self.assertEqual(self.store.inspect(self.checksum, selected_profile_sha256=revised_sha256,
                         current_fingerprint_sha256=self.page.page_fingerprint_sha256)['status'], 'stale')
        self.assertEqual(self.store.get(self.checksum), self.page)

    def test_revisions_are_explicit_and_bound_to_same_profile_page_and_role(self):
        self.store.register(self.page, confirm_sha256=self.checksum)
        revision = SitePageDraft.model_validate({**self.page.model_dump(), 'revision': 2,
            'previous_sha256': self.checksum, 'route_template': '/app/records/{record_id}',
            'page_fingerprint_sha256': 'b' * 64})
        revision_sha256 = digest(revision.model_dump())
        self.assertEqual(self.store.register(revision, confirm_sha256=revision_sha256), revision_sha256)
        self.assertEqual(SiteKnowledgeStore(self.store.root, self.profiles).get(revision_sha256), revision)
        for changes in ({'previous_sha256': '0' * 64}, {'revision': 3},
                        {'page_key': 'other-page'}, {'account_role': 'viewer'},
                        {'tenant_key': 'other-tenant'}, {'profile_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                changed = SitePageDraft.model_validate({**revision.model_dump(), **changes})
                self.store.register(changed, confirm_sha256=digest(changed.model_dump()))

    def test_inspect_marks_superseded_exact_revision_stale_even_when_fingerprint_matches(self):
        self.store.register(self.page, confirm_sha256=self.checksum)
        revision = SitePageDraft.model_validate({**self.page.model_dump(), 'revision': 2,
            'previous_sha256': self.checksum, 'route_template': '/app/records/{record_id}'})
        revision_sha256 = digest(revision.model_dump())
        self.store.register(revision, confirm_sha256=revision_sha256)
        previous = self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                                      current_fingerprint_sha256=self.page.page_fingerprint_sha256)
        current = self.store.inspect(revision_sha256, selected_profile_sha256=self.profile_sha256,
                                     current_fingerprint_sha256=revision.page_fingerprint_sha256)
        self.assertEqual(previous['status'], 'stale')
        self.assertEqual(current['status'], 'draft_match')
        self.assertFalse(previous['execution_authorized'])
        self.assertFalse(current['execution_authorized'])

    def test_inspect_rejects_ambiguous_revision_graph(self):
        self.store.register(self.page, confirm_sha256=self.checksum)
        revision = SitePageDraft.model_validate({**self.page.model_dump(), 'revision': 2,
            'previous_sha256': self.checksum, 'page_fingerprint_sha256': 'b' * 64})
        self.store.register(revision, confirm_sha256=digest(revision.model_dump()))
        fork = SitePageDraft.model_validate({**revision.model_dump(),
            'page_fingerprint_sha256': 'c' * 64})
        self.store.register(fork, confirm_sha256=digest(fork.model_dump()))
        with self.assertRaises(ValueError):
            self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                               current_fingerprint_sha256=self.page.page_fingerprint_sha256)
        with self.assertRaises(ValueError):
            self.store.inspect(digest(revision.model_dump()),
                               selected_profile_sha256=self.profile_sha256,
                               current_fingerprint_sha256=revision.page_fingerprint_sha256)

    def test_inspect_rejects_competing_roots_and_corrupt_inventory(self):
        self.store.register(self.page, confirm_sha256=self.checksum)
        competing = SitePageDraft.model_validate({**self.page.model_dump(),
            'route_template': '/app/records/{record_id}'})
        self.store.register(competing, confirm_sha256=digest(competing.model_dump()))
        with self.assertRaises(ValueError):
            self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                               current_fingerprint_sha256=self.page.page_fingerprint_sha256)
        (self.store.root / 'unexpected.txt').write_text('private')
        with self.assertRaises(ValueError):
            self.store.inspect(self.checksum, selected_profile_sha256=self.profile_sha256,
                               current_fingerprint_sha256=self.page.page_fingerprint_sha256)

    def test_list_is_profile_scoped_and_never_selects_a_latest_revision(self):
        self.assertEqual(self.store.list(profile_sha256=self.profile_sha256), [])
        self.store.register(self.page, confirm_sha256=self.checksum)
        revision = SitePageDraft.model_validate({**self.page.model_dump(), 'revision': 2,
            'previous_sha256': self.checksum, 'page_fingerprint_sha256': 'b' * 64})
        revision_sha256 = digest(revision.model_dump())
        self.store.register(revision, confirm_sha256=revision_sha256)
        reports = self.store.list(profile_sha256=self.profile_sha256, page_key='record-list')
        self.assertEqual([(item['revision'], item['knowledge_sha256']) for item in reports],
                         [(1, self.checksum), (2, revision_sha256)])
        self.assertTrue(all(item['status'] == 'draft' and item['execution_authorized'] is False
                            and item['training_ready'] is False for item in reports))
        revised_profile = WebApplicationProfile.model_validate({**self.profile.model_dump(),
            'revision': 2, 'previous_sha256': self.profile_sha256, 'task_keys': ['find-record']})
        revised_sha256 = profile_report(revised_profile).profile_sha256
        self.profiles.register(revised_profile, confirm_sha256=revised_sha256)
        foreign_page = SitePageDraft.model_validate({**self.page.model_dump(),
            'profile_sha256': revised_sha256})
        foreign_sha256 = digest(foreign_page.model_dump())
        self.store.register(foreign_page, confirm_sha256=foreign_sha256)
        self.assertEqual(len(self.store.list(profile_sha256=self.profile_sha256)), 2)
        self.assertEqual([item['knowledge_sha256'] for item in self.store.list(profile_sha256=revised_sha256)],
                         [foreign_sha256])
        for invalid in ('../private', '', 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.list(profile_sha256=self.profile_sha256, page_key=invalid)
        unexpected = self.store.root / 'unexpected.txt'
        unexpected.write_text('private')
        with self.assertRaises(ValueError):
            self.store.list(profile_sha256=self.profile_sha256)
        unexpected.unlink()
        (self.store.root / ('.page-' + '0' * 32)).write_text('orphan')
        self.assertEqual(len(self.store.list(profile_sha256=self.profile_sha256)), 2)

    def test_unreviewed_content_and_authority_changes_are_rejected(self):
        for changes in ({'route_template': 'https://crm.example.invalid/app'},
                        {'route_template': '/app//records'}, {'route_template': '/app/../private'},
                        {'route_template': '/app/records/123'}, {'route_template': '/app/records?id=1'},
                        {'route_template': '/app/%2f'}, {'origin': 'https://crm.example.invalid:443'},
                        {'landmark_keys': ['result-table', 'record-search']},
                        {'outgoing_page_keys': ['record-list']},
                        {'recorded_at': '2026-02-31T00:00:00Z'},
                        {'recorded_at': '2026-09-23T03:00:00+03:00'},
                        {'source_kind': 'browser_observed'}, {'status': 'active'},
                        {'execution_authorized': True}, {'execution_authorized': 0},
                        {'collection_authorized': True}, {'training_ready': True},
                        {'raw_dom': '<input type=password>'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SitePageDraft.model_validate({**self.page.model_dump(), **changes})
        for changes in ({'tenant_key': 'different-tenant'}, {'account_role': 'viewer'},
                        {'application_key': 'other-app'}, {'origin': 'https://other.example.invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                changed = SitePageDraft.model_validate({**self.page.model_dump(), **changes})
                self.store.register(changed, confirm_sha256=digest(changed.model_dump()))
        with self.assertRaises(ValueError):
            self.store.register(self.page, confirm_sha256='0' * 64)
        self.assertFalse(self.store.root.exists())

    def test_corruption_symlink_and_hardlink_fail_closed(self):
        self.store.register(self.page, confirm_sha256=self.checksum)
        path = self.store.root / (self.checksum + '.json')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        path.chmod(0o600)
        shortcut = self.store.root / 'other.json'
        os.link(path, shortcut)
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        shortcut.unlink()
        original = path.read_bytes()
        path.write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        path.write_bytes(original)
        path.rename(shortcut)
        path.symlink_to(shortcut)
        with self.assertRaises((ValueError, OSError)):
            self.store.get(self.checksum)

    def test_cli_preview_exact_confirmation_inspection_and_secret_safe_failure(self):
        source = self.root / 'page.json'
        source.write_text(json.dumps(self.page.model_dump()))
        command = [sys.executable, '-m', 'aos.site_knowledge', '--profiles', str(self.profiles.root),
                   '--store', str(self.store.root)]

        def invoke(*arguments):
            return subprocess.run([*command, *arguments], capture_output=True, text=True, timeout=10)

        preview = invoke('preview', '--page', str(source))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)['knowledge_sha256'], self.checksum)
        self.assertNotIn('route_template', preview.stdout)
        self.assertFalse(self.store.root.exists())
        wrong = invoke('register', '--page', str(source), '--confirm-sha256', '0' * 64)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(wrong.stdout, '')
        self.assertFalse(self.store.root.exists())
        accepted = invoke('register', '--page', str(source), '--confirm-sha256', self.checksum)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)['status'], 'draft')
        matched = invoke('inspect', '--sha256', self.checksum,
                         '--selected-profile-sha256', self.profile_sha256,
                         '--current-fingerprint-sha256', self.page.page_fingerprint_sha256)
        self.assertEqual(json.loads(matched.stdout)['status'], 'draft_match')
        listing = invoke('list', '--profile-sha256', self.profile_sha256, '--page-key', 'record-list')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual([item['knowledge_sha256'] for item in json.loads(listing.stdout)['pages']], [self.checksum])
        self.assertNotIn('route_template', listing.stdout)
        shortcut = self.root / 'shortcut.json'
        shortcut.symlink_to(source)
        denied = invoke('preview', '--page', str(shortcut))
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(denied.stdout, '')
        self.assertNotIn(str(source), denied.stderr)


if __name__ == '__main__':
    unittest.main()
