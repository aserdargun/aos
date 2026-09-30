from contextlib import contextmanager
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from aos.contracts import digest
from aos.owned_episode_candidates import derive_system1_candidates, derive_system2_candidate
from aos.owned_episode_learning import FIXTURE_GROUP, OwnedEpisodeStore, consent_record
from aos.owned_episode_service import OwnedEpisodeLearning
import test_owned_episode_candidates as candidate_helpers
from test_owned_skill_plan_bound import synthetic_native_contract_bundle


class OwnedEpisodeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.helper = candidate_helpers.OwnedEpisodeCandidateTests()
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.helper.connection.execute('''CREATE TABLE desktop_tasks(
            job_id TEXT, kind TEXT, status TEXT, run_id TEXT)''')
        self.job_id = 'job-synthetic-owned-episode'
        self.bundle = synthetic_native_contract_bundle()
        self.consent = consent_record(self.bundle['planning_id'], self.bundle['request'],
                                      self.bundle['authority'])
        self.episode_id = self.consent['episode_id']
        self.store = OwnedEpisodeStore(root / 'episodes')
        self.consent_sha256 = self.store.create(self.consent)
        self.bundle.update(schema_version='1.1', episode_id=self.episode_id,
                           collection_consent_sha256=self.consent_sha256)
        self.store.refresh(self.episode_id, [derive_system2_candidate(
            self.bundle, episode_id=self.episode_id, consent_sha256=self.consent_sha256,
            source_group_sha256=FIXTURE_GROUP)])
        self.execution_sha256 = 'a' * 64
        self.store.bind_execution(self.episode_id, self.execution_sha256, digest(self.bundle))
        self.bundle_loader = {
            'manifest': {'schema_version': '1.4', 'planning_bundle_sha256': digest(self.bundle)},
            'planning-bundle': self.bundle,
            'completion': {'job_id': self.job_id},
        }
        self.status = 'succeeded'
        self.helper.connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?)',
            (self.job_id, 'browser_remote_form', self.status, self.helper.run_id))
        self.session = type('Session', (), {'directory': root / 'owned-form'})()
        self.scheduler = type('Scheduler', (), {})()
        self.scheduler.settings = type('Settings', (), {'database': root / 'trajectory.sqlite'})()
        self.scheduler.remote_form_owned_candidate_session = self.session
        self.scheduler._owned_candidate_execution_history = {self.execution_sha256: {'job_id': self.job_id}}
        self.scheduler.owned_skill_planning = type('Planning', (), {
            'select': lambda _self, checksum, lease, generation: self.bundle})()
        self.scheduler.check_remote_form_binding = Mock()
        self.scheduler.audit_owned_form_candidate_execution = Mock(return_value={
            'available': True, 'schema_version': '1.4', 'planning_admission_verified': True,
            'review_status': 'accepted', 'planning_bundle_sha256': digest(self.bundle)})
        self.service = OwnedEpisodeLearning(self.scheduler, root / 'episodes')
        self.service.current = {'episode_id': self.episode_id, 'state': 'collecting',
                                'counts': {'system1': 0, 'system2': 1}}

        @contextmanager
        def snapshot(_path):
            yield self.helper.connection, {'synthetic': True}

        self.snapshot_patch = patch('aos.owned_episode_service.audit_snapshot', snapshot)
        self.loader_patch = patch('aos.owned_episode_service.load_candidate_execution_bundle',
                                  lambda *_args, **_kwargs: (self.bundle_loader, 'b' * 64))
        self.source_fingerprint_patch = patch(
            'aos.owned_episode_service.source_fingerprint', return_value='c' * 64)

    def inspect_patched(self):
        with self.snapshot_patch, self.loader_patch, self.source_fingerprint_patch:
            return self.service.inspect(self.episode_id)

    def test_read_only_inspection_does_not_recreate_missing_candidate(self):
        report = self.inspect_patched()
        self.assertTrue(report['reviewable'])
        directory = self.service.store.root / self.episode_id
        candidate = next(directory.glob('candidate-*.json'))
        candidate.unlink()
        with self.snapshot_patch, self.loader_patch, self.source_fingerprint_patch:
            with self.assertRaisesRegex(ValueError, 'candidate_source_changed'):
                self.service.inspect(self.episode_id, read_only=True)
        self.assertFalse(candidate.exists())

    def test_failed_cancelled_and_unfinished_source_never_becomes_reviewable(self):
        for status in ('failed', 'cancelled', 'waiting_human', 'running'):
            with self.subTest(status=status):
                self.helper.connection.execute('UPDATE desktop_tasks SET status=? WHERE job_id=?',
                                               (status, self.job_id))
                with self.snapshot_patch, self.loader_patch, self.source_fingerprint_patch:
                    if status in {'failed', 'cancelled', 'waiting_human'}:
                        with self.assertRaises(ValueError):
                            self.service.inspect(self.episode_id)
                    else:
                        report = self.service.inspect(self.episode_id)
                        self.assertFalse(report['reviewable'])
                    with self.assertRaises(ValueError):
                        self.service.review(self.episode_id, 'system2', 'accept', 'd' * 64)
                    with self.assertRaises(ValueError):
                        self.service.export(self.episode_id, {'system1': 'e' * 64, 'system2': 'f' * 64})
                self.assertEqual(self.store.reviews(self.episode_id), {'system1': None, 'system2': None})

    def test_stale_execution_schema_or_candidate_source_cannot_become_reviewable(self):
        with self.subTest(reason='schema_version'):
            self.bundle_loader['manifest']['schema_version'] = '1.3'
            with self.assertRaisesRegex(ValueError, 'owned_episode_execution_changed'):
                self.inspect_patched()
            self.bundle_loader['manifest']['schema_version'] = '1.4'
        with self.subTest(reason='decision_source_drift'):
            self.inspect_patched()
            self.helper.connection.execute("UPDATE model_calls SET response_json='{}' WHERE call_id=?",
                                          (self.helper.call_id,))
            with self.assertRaises(ValueError):
                self.inspect_patched()

    def test_historical_inspection_cannot_replace_new_episode_collection(self):
        newer = {'episode_id': 'episode-' + 'f' * 32, 'state': 'collecting',
                 'counts': {'system1': 0, 'system2': 1}}
        self.service.current = newer.copy()
        report = self.inspect_patched()
        self.assertTrue(report['reviewable'])
        self.assertEqual(report['episode_id'], self.episode_id)
        self.assertEqual(self.service.current, newer)

    def test_changed_source_fingerprint_invalidates_accepted_review_and_export(self):
        with self.snapshot_patch, self.loader_patch, patch(
                'aos.owned_episode_service.source_fingerprint', return_value='c' * 64):
            inspected = self.service.inspect(self.episode_id)
            self.assertTrue(inspected['reviewable'])
            self.service.review(self.episode_id, 'system1', 'accept', inspected['review_sha256']['system1'])
            receipt = self.store.reviews(self.episode_id)['system1']
            self.assertEqual(receipt['receipt']['execution']['execution_source_sha256'], 'c' * 64)
        with self.snapshot_patch, self.loader_patch, patch(
                'aos.owned_episode_service.source_fingerprint', return_value='d' * 64):
            with self.assertRaisesRegex(ValueError, 'owned_episode_current_review_required'):
                self.service.export(self.episode_id, {'system1': receipt['receipt_sha256'],
                                                      'system2': 'e' * 64})
        self.assertEqual(self.store.reviews(self.episode_id)['system1']['receipt']['execution']['execution_source_sha256'],
                         'c' * 64)

    def test_revoke_persists_receipt_even_when_source_inspection_fails(self):
        role = 'system2'
        candidates = self.store.candidates(self.episode_id)
        selected = [item for item in candidates if item['role'] == role]
        selection = self.store.review_selection(self.episode_id, role, selected,
            self.store.execution(self.episode_id) | {'execution_source_sha256': 'c' * 64})
        receipt = self.store.review(self.episode_id, role, 'accept', selected,
            self.store.execution(self.episode_id) | {'execution_source_sha256': 'c' * 64}, digest(selection))
        self.service.inspect = Mock(side_effect=ValueError('synthetic source unavailable'))
        response = self.service.revoke(self.episode_id, role, receipt)
        self.assertFalse(response['available'])
        self.assertTrue(response['revocation_recorded'])
        self.assertTrue(response['reviews'][role]['revoked'])
        self.assertEqual(response['candidates'], [])


if __name__ == '__main__':
    unittest.main()
