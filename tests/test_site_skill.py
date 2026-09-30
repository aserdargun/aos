import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
PAGE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
FIXTURE = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())


class SiteSkillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
        self.profile_sha256 = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha256)
        self.pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        self.page = SitePageDraft.model_validate(copy.deepcopy(PAGE))
        self.page_sha256 = digest(self.page.model_dump())
        self.pages.register(self.page, confirm_sha256=self.page_sha256)
        self.store = SiteSkillStore(self.root / 'skills', self.profiles, self.pages)
        self.skill = SiteSkillDraft.model_validate(copy.deepcopy(FIXTURE['skill']))
        self.checksum = digest(self.skill.model_dump())

    def test_synthetic_fixture_and_schema_parity(self):
        self.assertTrue(FIXTURE['synthetic'])
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_draft.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillDraft.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(self.skill.model_dump())
        self.assertEqual(self.skill.profile_sha256, self.profile_sha256)
        self.assertEqual(self.skill.page_draft_sha256, self.page_sha256)
        self.assertEqual(self.skill.source_event_ids[0],
                         json.loads((REPO_ROOT / 'examples/learning_event_v2.json').read_text())['event_id'])

    def test_private_registration_preview_restart_and_role_separation(self):
        preview = self.store.preview(self.skill)
        self.assertEqual(preview['skill_sha256'], self.checksum)
        self.assertFalse(self.store.root.exists())
        self.assertEqual(self.store.register(self.skill, confirm_sha256=self.checksum), self.checksum)
        self.assertEqual(self.store.register(self.skill, confirm_sha256=self.checksum), self.checksum)
        self.assertEqual(SiteSkillStore(self.store.root, self.profiles, self.pages).get(self.checksum), self.skill)
        self.assertEqual((self.store.root / (self.checksum + '.json')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.store.list(profile_sha256=self.profile_sha256, model_role='system1')), 1)
        self.assertEqual(self.store.list(profile_sha256=self.profile_sha256, model_role='system2'), [])
        self.assertTrue(all(preview[key] is False for key in
                            ('execution_authorized', 'collection_authorized',
                             'training_ready', 'activation_authorized')))
        self.assertNotIn('step_keys', preview)
        self.assertNotIn('source_event_ids', preview)

        supervisor = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'find-record-plan', 'model_role': 'system2',
            'candidate_kind': 'workflow_plan', 'source_verification_ids': []})
        supervisor_sha256 = digest(supervisor.model_dump())
        self.store.register(supervisor, confirm_sha256=supervisor_sha256)
        self.assertEqual([item['skill_sha256'] for item in
                          self.store.list(profile_sha256=self.profile_sha256, model_role='system2')],
                         [supervisor_sha256])

    def test_revisions_require_exact_parent_and_same_scope(self):
        self.store.register(self.skill, confirm_sha256=self.checksum)
        revised = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'revision': 2, 'previous_sha256': self.checksum,
            'step_keys': ['choose-search-action', 'inspect-result']})
        revised_sha256 = digest(revised.model_dump())
        self.assertEqual(self.store.preview(revised)['skill_sha256'], revised_sha256)
        self.store.register(revised, confirm_sha256=revised_sha256)
        self.assertEqual([item['revision'] for item in
                          self.store.list(profile_sha256=self.profile_sha256, model_role='system1')], [1, 2])
        for changes in ({'previous_sha256': '0' * 64}, {'revision': 3},
                        {'skill_key': 'different-skill'}, {'task_key': 'update-draft'},
                        {'model_role': 'system2', 'candidate_kind': 'workflow_plan'},
                        {'tenant_key': 'other-tenant'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                changed = SiteSkillDraft.model_validate({**revised.model_dump(), **changes})
                self.store.register(changed, confirm_sha256=digest(changed.model_dump()))

    def test_scope_and_authority_rejections(self):
        for changes in ({'model_role': 'system2'}, {'candidate_kind': 'workflow_plan'},
                        {'step_keys': ['run;rm-rf']}, {'step_keys': ['../hidden']},
                        {'source_event_ids': []}, {'source_event_ids': ['learning-test']},
                        {'source_verification_ids': ['verification-test', 'verification-test']},
                        {'source_kind': 'automatic_extraction'}, {'status': 'active'},
                        {'execution_authorized': True}, {'execution_authorized': 0},
                        {'collection_authorized': True}, {'training_ready': True},
                        {'activation_authorized': True}, {'shell': 'rm -rf /'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SiteSkillDraft.model_validate({**self.skill.model_dump(), **changes})
        for changes in ({'profile_sha256': '0' * 64}, {'application_key': 'other-app'},
                        {'tenant_key': 'other-tenant'}, {'account_role': 'viewer'},
                        {'task_key': 'other-task'}, {'page_key': 'other-page'},
                        {'page_draft_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                changed = SiteSkillDraft.model_validate({**self.skill.model_dump(), **changes})
                self.store.preview(changed)
        with self.assertRaises(ValueError):
            self.store.register(self.skill, confirm_sha256='0' * 64)
        self.assertFalse(self.store.root.exists())
        with self.assertRaises(ValueError):
            self.store.list(profile_sha256=self.profile_sha256, model_role='both')

    def test_disabled_role_and_profile_revision_do_not_inherit_skill(self):
        revised_profile = WebApplicationProfile.model_validate({**self.profile.model_dump(),
            'revision': 2, 'previous_sha256': self.profile_sha256,
            'learning': {**self.profile.learning.model_dump(), 'system2': 'disabled'}})
        revised_sha256 = profile_report(revised_profile).profile_sha256
        self.profiles.register(revised_profile, confirm_sha256=revised_sha256)
        stale = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'profile_sha256': revised_sha256})
        with self.assertRaises(ValueError):
            self.store.preview(stale)
        self.store.register(self.skill, confirm_sha256=self.checksum)
        self.assertEqual(self.store.list(profile_sha256=revised_sha256, model_role='system1'), [])

    def test_corruption_symlink_hardlink_and_unexpected_store_file_fail_closed(self):
        self.store.register(self.skill, confirm_sha256=self.checksum)
        path = self.store.root / (self.checksum + '.json')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        path.chmod(0o600)
        extra = self.store.root / 'extra.json'
        os.link(path, extra)
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        extra.unlink()
        original = path.read_bytes()
        path.write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.store.get(self.checksum)
        path.write_bytes(original)
        path.rename(extra)
        path.symlink_to(extra)
        with self.assertRaises((ValueError, OSError)):
            self.store.get(self.checksum)
        path.unlink()
        extra.rename(path)
        (self.store.root / 'unexpected.txt').write_text('synthetic')
        with self.assertRaises(ValueError):
            self.store.list(profile_sha256=self.profile_sha256, model_role='system1')


if __name__ == '__main__':
    unittest.main()
