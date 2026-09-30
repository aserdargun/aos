import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class SiteSkillCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        self.profile_sha256 = profile_report(profile).profile_sha256
        self.profiles.register(profile, confirm_sha256=self.profile_sha256)
        self.pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        self.page_sha256 = digest(page.model_dump())
        self.pages.register(page, confirm_sha256=self.page_sha256)
        self.skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        self.checksum = digest(self.skill.model_dump())
        self.source = self.root / 'candidate.json'
        self.source.write_text(json.dumps(self.skill.model_dump()))
        self.source.chmod(0o600)
        self.store_root = self.root / 'skills'
        self.command = [sys.executable, '-m', 'aos.site_skill_cli',
                        '--profiles', str(self.profiles.root), '--pages', str(self.pages.root),
                        '--store', str(self.store_root)]

    def invoke(self, *arguments):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        return subprocess.run([*self.command, *arguments], cwd=REPO_ROOT, env=environment,
                              capture_output=True, text=True, timeout=10)

    def test_preview_registration_inspection_and_role_scoped_list(self):
        preview = self.invoke('preview', '--skill', str(self.source))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        report = json.loads(preview.stdout)
        self.assertEqual(report['skill_sha256'], self.checksum)
        self.assertFalse(self.store_root.exists())
        self.assertNotIn('source_event_ids', preview.stdout)
        self.assertNotIn('step_keys', preview.stdout)
        self.assertTrue(all(report[key] is False for key in
                            ('execution_authorized', 'collection_authorized',
                             'activation_authorized', 'training_ready')))

        wrong = self.invoke('register', '--skill', str(self.source), '--confirm-sha256', '0' * 64)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(wrong.stdout, '')
        self.assertEqual(wrong.stderr.strip(), 'Site skill draft operation failed')
        self.assertFalse(self.store_root.exists())

        registered = self.invoke('register', '--skill', str(self.source),
                                 '--confirm-sha256', self.checksum)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout), report)
        self.assertEqual(self.invoke('register', '--skill', str(self.source),
                                     '--confirm-sha256', self.checksum).returncode, 0)
        matched = self.invoke('inspect', '--sha256', self.checksum,
                              '--selected-profile-sha256', self.profile_sha256,
                              '--selected-page-sha256', self.page_sha256)
        self.assertEqual(json.loads(matched.stdout)['status'], 'draft_match')
        stale = self.invoke('inspect', '--sha256', self.checksum,
                            '--selected-profile-sha256', 'b' * 64,
                            '--selected-page-sha256', self.page_sha256)
        self.assertEqual(json.loads(stale.stdout)['status'], 'stale')
        stale_page = self.invoke('inspect', '--sha256', self.checksum,
                                 '--selected-profile-sha256', self.profile_sha256,
                                 '--selected-page-sha256', 'b' * 64)
        self.assertEqual(json.loads(stale_page.stdout)['status'], 'stale')
        listed = self.invoke('list', '--profile-sha256', self.profile_sha256,
                             '--model-role', 'system1')
        self.assertEqual([item['skill_sha256'] for item in json.loads(listed.stdout)['skills']],
                         [self.checksum])
        empty = self.invoke('list', '--profile-sha256', self.profile_sha256,
                            '--model-role', 'system2')
        self.assertEqual(json.loads(empty.stdout)['skills'], [])

    def test_source_privacy_duplicate_keys_and_authority_fail_closed(self):
        shortcut = self.root / 'shortcut.json'
        shortcut.symlink_to(self.source)
        cases = [shortcut]
        for source in cases:
            failed = self.invoke('preview', '--skill', str(source))
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, '')
            self.assertEqual(failed.stderr.strip(), 'Site skill draft operation failed')
            self.assertNotIn(str(self.source), failed.stderr)
        hardlink = self.root / 'hardlink.json'
        os.link(self.source, hardlink)
        self.assertNotEqual(self.invoke('preview', '--skill', str(self.source)).returncode, 0)
        hardlink.unlink()
        self.source.chmod(0o644)
        self.assertNotEqual(self.invoke('preview', '--skill', str(self.source)).returncode, 0)
        self.source.chmod(0o600)
        self.source.write_text(self.source.read_text()[:-1] + ',"status":"active"}')
        duplicate = self.invoke('preview', '--skill', str(self.source))
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(duplicate.stderr.strip(), 'Site skill draft operation failed')
        self.source.write_text(json.dumps({**self.skill.model_dump(), 'execution_authorized': True}))
        authority = self.invoke('register', '--skill', str(self.source),
                                '--confirm-sha256', self.checksum)
        self.assertNotEqual(authority.returncode, 0)
        self.assertFalse(self.store_root.exists())

    def test_invalid_inspection_scope_and_corrupt_store_fail_closed(self):
        registered = self.invoke('register', '--skill', str(self.source),
                                 '--confirm-sha256', self.checksum)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        bad_scope = self.invoke('inspect', '--sha256', self.checksum,
                                '--selected-profile-sha256', '../private',
                                '--selected-page-sha256', self.page_sha256)
        self.assertNotEqual(bad_scope.returncode, 0)
        self.assertEqual(bad_scope.stdout, '')
        path = self.store_root / (self.checksum + '.json')
        path.write_bytes(b'{}')
        failed = self.invoke('inspect', '--sha256', self.checksum,
                             '--selected-profile-sha256', self.profile_sha256,
                             '--selected-page-sha256', self.page_sha256)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stderr.strip(), 'Site skill draft operation failed')

    def test_unknown_arguments_fail_generically_before_source_processing(self):
        marker = 'private-candidate-path-or-secret'
        unknown = self.invoke('preview', '--skill', str(self.source), '--unknown', marker)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertEqual(unknown.stdout, '')
        self.assertEqual(unknown.stderr.strip(), 'Site skill draft operation failed')
        self.assertNotIn(marker, unknown.stderr)
        self.assertFalse(self.store_root.exists())
        abbreviated = self.invoke('preview', '--ski', str(self.source))
        self.assertNotEqual(abbreviated.returncode, 0)
        self.assertEqual(abbreviated.stderr.strip(), 'Site skill draft operation failed')
        missing = self.invoke('register', '--skill', str(self.source))
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stderr.strip(), 'Site skill draft operation failed')


if __name__ == '__main__':
    unittest.main()
