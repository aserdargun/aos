import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_page_retrieval import main, preview_site_page_retrieval
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from tests import test_site_page_change as change_fixture


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
PAGE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
FINGERPRINT = json.loads((REPO_ROOT / 'examples/site_page_evidence.json').read_text())['page']['page_fingerprint_sha256']
FIXTURE = json.loads((REPO_ROOT / 'examples/site_page_retrieval.json').read_text())


class SitePageRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
        self.profile_sha256 = profile_report(profile).profile_sha256
        self.profiles.register(profile, confirm_sha256=self.profile_sha256)
        self.store = SiteKnowledgeStore(self.root / 'knowledge', self.profiles)
        self.page = SitePageDraft.model_validate({**PAGE, 'page_key': 'start',
            'outgoing_page_keys': [], 'page_fingerprint_sha256': FINGERPRINT})
        self.knowledge_sha256 = digest(self.page.model_dump())
        self.store.register(self.page, confirm_sha256=self.knowledge_sha256)
        self.database = self.root / 'trajectory.sqlite'
        trajectory = TrajectoryStore(self.database)
        self.addCleanup(trajectory.close)
        self.trajectory = trajectory
        for run_id in ('run-before', 'run-after'):
            change_fixture.SitePageChangeTests._add_navigation_run(trajectory, run_id)

    def preview(self, **changes):
        arguments = {
            'before_verification_id': 'verification-run-before',
            'after_verification_id': 'verification-run-after',
            'profiles': self.profiles.root, 'store': self.store.root,
            'knowledge_sha256': self.knowledge_sha256,
            'selected_profile_sha256': self.profile_sha256,
        }
        arguments.update(changes)
        return preview_site_page_retrieval(self.database, 'run-before', 'run-after', **arguments)

    def test_exact_two_run_candidate_is_content_free_and_non_authorizing(self):
        self.assertTrue(FIXTURE['synthetic'])
        validator('site_page_retrieval').validate(FIXTURE['report'])
        before = self.database.read_bytes()
        report = self.preview()
        validator('site_page_retrieval').validate(report)
        self.assertEqual(report, self.preview())
        self.assertEqual(report['status'], 'candidate_unbound')
        self.assertEqual(report['page_key'], 'start')
        self.assertEqual(report['page_fingerprint_sha256'], FINGERPRINT)
        self.assertEqual(report['knowledge_sha256'], self.knowledge_sha256)
        self.assertNotEqual(report['before_run_ref'], report['after_run_ref'])
        self.assertEqual(self.database.read_bytes(), before)
        for field in ('profile_bound', 'origin_verified', 'route_verified', 'reviewed',
                      'execution_authorized', 'collection_authorized', 'training_ready'):
            self.assertFalse(report[field])
            self.assertFalse(validator('site_page_retrieval').is_valid({**report, field: True}))
        serialized = json.dumps(report)
        for private in ('run-before', 'run-after', 'verification-run-after',
                        'private-do-not-export', 'https://'):
            self.assertNotIn(private, serialized)

    def test_wrong_source_or_draft_fails_closed(self):
        for changes in ({'before_verification_id': 'missing'},
                        {'after_verification_id': 'missing'},
                        {'knowledge_sha256': '0' * 64},
                        {'selected_profile_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                self.preview(**changes)
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE verifications SET actual_json='{}' WHERE verification_id='verification-run-after'")
        with self.assertRaises(ValueError):
            self.preview()

    def test_fingerprint_mismatch_and_superseded_revision_fail_closed(self):
        different = SitePageDraft.model_validate({**self.page.model_dump(),
            'page_fingerprint_sha256': 'b' * 64})
        different_sha256 = digest(different.model_dump())
        with self.assertRaises(FileNotFoundError):
            self.preview(knowledge_sha256=different_sha256)
        self.store.register(different, confirm_sha256=different_sha256)
        with self.assertRaises(ValueError):
            self.preview(knowledge_sha256=different_sha256)
        with self.assertRaises(ValueError):
            self.preview()

    def test_descendant_and_snapshot_mismatch_fail_closed(self):
        from aos.site_page_change import compare_site_page_change

        unchanged = compare_site_page_change(
            self.database, 'run-before', 'run-after',
            before_verification_id='verification-run-before',
            after_verification_id='verification-run-after')
        with patch('aos.site_page_retrieval.compare_site_page_change',
                   return_value={**unchanged, 'status': 'changed_unbound'}):
            with self.assertRaises(ValueError):
                self.preview()
        revision = SitePageDraft.model_validate({**self.page.model_dump(), 'revision': 2,
            'previous_sha256': self.knowledge_sha256,
            'route_template': '/app/records/{record_id}'})
        self.store.register(revision, confirm_sha256=digest(revision.model_dump()))
        with self.assertRaises(ValueError):
            self.preview()
        with patch('aos.site_page_retrieval.compare_site_page_draft') as comparison:
            comparison.return_value = {'status': 'hash_equal_unbound',
                                       'draft_page_key': 'start', 'evidence_page_key': 'start',
                                       'snapshot_sha256': '0' * 64}
            with self.assertRaises(ValueError):
                self.preview()

    def test_cli_requires_exact_selection_and_suppresses_private_errors(self):
        arguments = ['--database', str(self.database), '--before-run-id', 'run-before',
                     '--before-verification-id', 'verification-run-before',
                     '--after-run-id', 'run-after',
                     '--after-verification-id', 'verification-run-after',
                     '--profiles', str(self.profiles.root), '--store', str(self.store.root),
                     '--knowledge-sha256', self.knowledge_sha256,
                     '--selected-profile-sha256', self.profile_sha256]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(arguments)
        self.assertEqual(json.loads(output.getvalue()), self.preview())
        self.assertNotIn(str(self.database), output.getvalue())
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as failure:
                main([*arguments, '--knowledge-sha256', '0' * 64])
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn(str(self.database), errors.getvalue())
        with self.assertRaises(SystemExit):
            main([*arguments, '--unexpected'])


if __name__ == '__main__':
    unittest.main()
