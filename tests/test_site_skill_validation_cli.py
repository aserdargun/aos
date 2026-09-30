import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class SiteSkillValidationCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        profiles.register(profile, confirm_sha256=profile_report(profile).profile_sha256)
        pages = SiteKnowledgeStore(self.root / 'pages', profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        pages.register(page, confirm_sha256=digest(page.model_dump()))
        skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        self.skill_sha256 = digest(skill.model_dump())
        store = SiteSkillStore(self.root / 'skills', profiles, pages)
        store.register(skill, confirm_sha256=self.skill_sha256)
        self.plan = json.loads((REPO_ROOT / 'examples/site_skill_validation_plan.json').read_text())['plan']
        self.plan_path = self.root / 'variation-plan.json'
        self.write_plan(self.plan)
        self.command = [sys.executable, '-m', 'aos.site_skill_validation_cli',
                        '--profiles', str(profiles.root), '--pages', str(pages.root),
                        '--store', str(store.root)]

    def write_plan(self, plan):
        self.plan_path.write_text(canonical(plan))
        self.plan_path.chmod(0o600)

    def invoke(self, *arguments, selected_skill=None, selected_plan=None):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        return subprocess.run([*self.command, '--plan', str(self.plan_path),
                               '--skill-sha256', selected_skill or self.skill_sha256,
                               '--plan-sha256', selected_plan or digest(self.plan), *arguments],
                              cwd=REPO_ROOT, env=environment, capture_output=True,
                              text=True, timeout=10)

    def assert_generic_failure(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip(), 'Site skill validation preview failed')
        self.assertNotIn(str(self.root), result.stderr)

    def test_exact_private_plan_reports_only_structure_without_mutation(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        expected = json.loads((REPO_ROOT / 'examples/site_skill_validation_report.json').read_text())['report']
        self.assertEqual(report, expected)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})
        self.assertFalse(report['execution_performed'])
        self.assertFalse(report['skill_validated'])
        self.assertFalse(report['training_ready'])
        self.assertNotIn('cases', result.stdout)
        self.assertNotIn('record-query', result.stdout)

    def test_explicit_hash_scope_and_case_contract_fail_closed(self):
        self.assert_generic_failure(self.invoke(selected_skill='0' * 64))
        self.assert_generic_failure(self.invoke(selected_plan='0' * 64))
        changed = copy.deepcopy(self.plan)
        changed['model_role'] = 'system2'
        self.write_plan(changed)
        self.assert_generic_failure(self.invoke(selected_plan=digest(changed)))
        changed = copy.deepcopy(self.plan)
        changed['cases'][0]['parameter_keys'] = ['wrong-parameter']
        self.write_plan(changed)
        self.assert_generic_failure(self.invoke(selected_plan=digest(changed)))

    def test_private_canonical_source_and_store_integrity_required(self):
        shortcut = self.root / 'shortcut.json'
        shortcut.symlink_to(self.plan_path)
        self.assert_generic_failure(self.invoke('--plan', str(shortcut)))
        duplicate_link = self.root / 'duplicate-link.json'
        os.link(self.plan_path, duplicate_link)
        self.assert_generic_failure(self.invoke())
        duplicate_link.unlink()
        self.plan_path.chmod(0o644)
        self.assert_generic_failure(self.invoke())
        self.plan_path.chmod(0o600)
        self.plan_path.write_text(json.dumps(self.plan))
        self.assert_generic_failure(self.invoke())
        self.plan_path.write_text(canonical(self.plan)[:-1] + ',"synthetic":true}')
        self.assert_generic_failure(self.invoke())
        self.write_plan(self.plan)
        skill_path = self.root / 'skills' / (self.skill_sha256 + '.json')
        skill_path.write_bytes(b'{}')
        self.assert_generic_failure(self.invoke())

    def test_unknown_missing_and_abbreviated_arguments_do_not_echo_values(self):
        marker = 'private-plan-secret'
        unknown = self.invoke('--unexpected', marker)
        self.assert_generic_failure(unknown)
        self.assertNotIn(marker, unknown.stderr)
        self.assert_generic_failure(self.invoke('--pl', str(self.plan_path)))
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        missing = subprocess.run([*self.command, '--plan', str(self.plan_path)],
                                 cwd=REPO_ROOT, env=environment, capture_output=True,
                                 text=True, timeout=10)
        self.assert_generic_failure(missing)


if __name__ == '__main__':
    unittest.main()
