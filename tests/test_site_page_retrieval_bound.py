import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest, now
from aos.dataset import validator
from aos.local_navigation_admission import SyntheticNavigationPin
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_page_retrieval_bound import main, preview_local_bound_site_page_retrieval
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from tests import test_site_page_change as change_fixture


PIN_FIXTURE = json.loads((REPO_ROOT / 'examples/synthetic_navigation_pin.json').read_text())
PAGE_FIXTURE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
FINGERPRINT = json.loads((REPO_ROOT / 'examples/site_page_evidence.json').read_text())['page']['page_fingerprint_sha256']
REPORT_FIXTURE = json.loads((REPO_ROOT / 'examples/site_page_retrieval_bound.json').read_text())


class LocalBoundPageRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(PIN_FIXTURE['profile'])
        self.profile_sha256 = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha256)
        self.pin = SyntheticNavigationPin.model_validate(PIN_FIXTURE['pin'])
        self.pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        self.register_page('/start')
        self.database = self.root / 'trace.sqlite'
        self.trajectory = TrajectoryStore(self.database)
        self.addCleanup(self.trajectory.close)
        with self.trajectory.connection:
            self.trajectory.insert('desktop_sessions', session_id='session', runtime_id='desktop',
                                   image_id='synthetic-image', owner='AGENT', lease_id='lease',
                                   generation=0, status='running', created_at=now(), updated_at=now())
        for run_id in ('run-before', 'run-after'):
            change_fixture.SitePageChangeTests._add_navigation_run(self.trajectory, run_id)
            with self.trajectory.connection:
                self.trajectory.connection.execute(
                    "UPDATE runs SET status='succeeded',outcome='passed' WHERE run_id=?", (run_id,))
                self.trajectory.insert('desktop_tasks', job_id='job-' + run_id, session_id='session',
                                       run_id=run_id, kind='browser_local_navigation', lease_id='lease',
                                       generation=0, status='succeeded', real_model=0,
                                       created_at=now(), updated_at=now(), runtime_id='browser-' + run_id)

    def register_page(self, route):
        page = SitePageDraft.model_validate({**PAGE_FIXTURE,
            'profile_sha256': self.profile_sha256,
            'application_key': self.profile.application_key,
            'tenant_key': self.profile.tenant_key,
            'account_role': self.profile.account_role,
            'origin': self.profile.allowed_origins[0], 'route_template': route,
            'page_key': 'start', 'page_fingerprint_sha256': FINGERPRINT,
            'outgoing_page_keys': []})
        checksum = digest(page.model_dump())
        self.pages.register(page, confirm_sha256=checksum)
        self.knowledge_sha256 = checksum

    def bind(self, run_id, *, pin_sha256=None):
        record = self.pin.model_dump(mode='json')
        with self.trajectory.connection:
            self.trajectory.insert('desktop_web_profile_bindings', job_id='job-' + run_id,
                                   run_id=run_id, profile_sha256=self.profile_sha256,
                                   task_key=self.pin.task_key, pin_json=canonical(record),
                                   pin_sha256=pin_sha256 or digest(record),
                                   browser_runtime_id='browser-' + run_id, created_at=now())

    def preview(self, **changes):
        arguments = {'before_verification_id': 'verification-run-before',
                     'after_verification_id': 'verification-run-after',
                     'profiles': self.profiles.root, 'store': self.pages.root,
                     'knowledge_sha256': self.knowledge_sha256,
                     'selected_profile_sha256': self.profile_sha256}
        arguments.update(changes)
        return preview_local_bound_site_page_retrieval(
            self.database, 'run-before', 'run-after', **arguments)

    def test_two_bound_runs_are_read_only_and_non_authorizing(self):
        self.assertTrue(REPORT_FIXTURE['synthetic'])
        validator('site_page_retrieval_bound').validate(REPORT_FIXTURE['report'])
        self.bind('run-before')
        self.bind('run-after')
        before = self.database.read_bytes()
        report = self.preview()
        validator('site_page_retrieval_bound').validate(report)
        self.assertEqual(report, self.preview())
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(report['status'], 'candidate_local_profile_bound')
        self.assertEqual(report['selected_profile_sha256'], self.profile_sha256)
        self.assertEqual(report['before_pin_sha256'], digest(self.pin.model_dump(mode='json')))
        self.assertTrue(report['profile_bound'])
        for field in ('origin_verified', 'route_verified', 'reviewed', 'execution_authorized',
                      'collection_authorized', 'training_ready'):
            self.assertFalse(report[field])
            self.assertFalse(validator('site_page_retrieval_bound').is_valid({**report, field: True}))
        serialized = canonical(report)
        for private in ('run-before', 'run-after', 'verification-run-after', 'http://', 'private-do-not-export'):
            self.assertNotIn(private, serialized)

    def test_missing_or_wrong_pin_and_route_fail_closed(self):
        self.bind('run-before')
        with self.assertRaises(ValueError):
            self.preview()
        self.bind('run-after', pin_sha256='0' * 64)
        with self.assertRaises(ValueError):
            self.preview()

    def test_wrong_page_route_fails_without_binding_upgrade(self):
        self.register_page('/details')
        self.bind('run-before')
        self.bind('run-after')
        with self.assertRaises(ValueError):
            self.preview()

    def test_nonterminal_source_and_snapshot_drift_fail_closed(self):
        self.bind('run-before')
        self.bind('run-after')
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE desktop_tasks SET status='failed' WHERE run_id='run-after'")
        with self.assertRaises(ValueError):
            self.preview()
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE desktop_tasks SET status='succeeded' WHERE run_id='run-after'")
        from aos.site_page_retrieval import preview_site_page_retrieval

        def changed_snapshot(*arguments, **options):
            report = preview_site_page_retrieval(*arguments, **options)
            with self.trajectory.connection:
                self.trajectory.connection.execute(
                    "UPDATE runs SET ended_at='2026-09-23T00:00:00Z' WHERE run_id='run-after'")
            return report

        with patch('aos.site_page_retrieval_bound.preview_site_page_retrieval', side_effect=changed_snapshot):
            with self.assertRaisesRegex(ValueError, 'local_page_snapshot_changed'):
                self.preview()

    def test_cli_requires_exact_sources_and_suppresses_private_errors(self):
        self.bind('run-before')
        self.bind('run-after')
        arguments = ['--database', str(self.database), '--before-run-id', 'run-before',
                     '--before-verification-id', 'verification-run-before',
                     '--after-run-id', 'run-after', '--after-verification-id', 'verification-run-after',
                     '--profiles', str(self.profiles.root), '--store', str(self.pages.root),
                     '--knowledge-sha256', self.knowledge_sha256,
                     '--selected-profile-sha256', self.profile_sha256]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(arguments)
        self.assertEqual(json.loads(output.getvalue()), self.preview())
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as failure:
                main([*arguments, '--knowledge-sha256', '0' * 64])
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn(str(self.database), errors.getvalue())


if __name__ == '__main__':
    unittest.main()
