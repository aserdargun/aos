import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.owned_episode_candidates import derive_system1_candidates, derive_system2_candidate
from aos.owned_episode_learning import FIXTURE_GROUP, OwnedEpisodeStore, consent_record
from test_owned_skill_plan_bound import synthetic_native_contract_bundle
import test_owned_episode_candidates as candidate_helpers


class OwnedEpisodeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = OwnedEpisodeStore(self.root / 'episodes')
        self.bundle = synthetic_native_contract_bundle()
        self.consent = consent_record(self.bundle['planning_id'], self.bundle['request'], self.bundle['authority'])
        self.episode_id = self.consent['episode_id']
        self.consent_sha = self.store.create(self.consent)
        self.bundle.update(schema_version='1.1', episode_id=self.episode_id,
                           collection_consent_sha256=self.consent_sha)
        self.helper = candidate_helpers.OwnedEpisodeCandidateTests()
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.candidates = derive_system1_candidates(self.helper.connection, self.helper.run_id,
            episode_id=self.episode_id, consent_sha256=self.consent_sha,
            planning_bundle_sha256=digest(self.bundle), source_group_sha256=FIXTURE_GROUP)
        self.candidates.append(derive_system2_candidate(self.bundle, episode_id=self.episode_id,
            consent_sha256=self.consent_sha, source_group_sha256=FIXTURE_GROUP))
        self.store.refresh(self.episode_id, self.candidates)
        self.store.bind_execution(self.episode_id, 'a' * 64, digest(self.bundle))
        self.execution = self.store.execution(self.episode_id) | {'execution_source_sha256': 'b' * 64}

    def accept(self):
        receipts = {}
        for role in ('system1', 'system2'):
            selected = [item for item in self.candidates if item['role'] == role]
            selection = self.store.review_selection(self.episode_id, role, selected, self.execution)
            receipts[role] = self.store.review(self.episode_id, role, 'accept', selected,
                                               self.execution, digest(selection))
        return receipts

    def test_consent_is_private_and_exact_planning_authority_bound(self):
        self.assertEqual(self.store.assert_plan(self.episode_id, self.bundle), self.consent)
        self.assertNotIn('goal', self.consent)
        directory = self.root / 'episodes' / self.episode_id
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'consent.json').stat().st_mode & 0o777, 0o600)
        for change in ({'episode_id': 'episode-' + '0' * 32}, {'collection_consent_sha256': '0' * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.assert_plan(self.episode_id, self.bundle | change)

    def test_no_review_means_no_export_or_automatic_target(self):
        with self.assertRaises(ValueError):
            self.store.export(self.episode_id, self.candidates, self.execution,
                              {'system1': '0' * 64, 'system2': '0' * 64})
        self.assertTrue(all('target' not in item for item in self.store.candidates(self.episode_id)))

    def test_two_role_export_is_stable_after_reopen_without_new_model_calls(self):
        receipts = self.accept()
        result = self.store.export(self.episode_id, self.candidates, self.execution, receipts)
        again = OwnedEpisodeStore(self.store.root)
        again.refresh(self.episode_id, self.candidates)
        self.assertEqual(result, again.export(self.episode_id, self.candidates, self.execution, receipts))
        self.assertEqual(result['counts'], {'system1': 1, 'system2': 1})
        for role in ('system1', 'system2'):
            path = self.store.root / self.episode_id / (result['export_sha256'] + '-' + role + '.jsonl')
            row = json.loads(path.read_text())
            validator('owned_episode_export_record').validate(row)
            self.assertEqual(row['split'], 'development_only')
            self.assertEqual(row['split_group'], FIXTURE_GROUP)
            self.assertFalse(row['training_ready'])
            candidate = next(item for item in self.candidates if item['role'] == role)
            self.assertEqual(row['input'], candidate['input'])
            self.assertEqual(row['target'], candidate['prediction'])
            self.assertNotIn('target', row['input'])

    def test_export_record_schema_rejects_role_mismatch_and_authority_fields(self):
        receipts = self.accept()
        result = self.store.export(self.episode_id, self.candidates, self.execution, receipts)
        for role in ('system1', 'system2'):
            path = self.store.root / self.episode_id / (result['export_sha256'] + '-' + role + '.jsonl')
            row = json.loads(path.read_text())
            other = 'system2' if role == 'system1' else 'system1'
            with self.subTest(role=role):
                self.assertFalse(validator('owned_episode_export_record').is_valid(
                    row | {'record_kind': 'owned_episode_' + other}))
                self.assertFalse(validator('owned_episode_export_record').is_valid(
                    row | {'execution_authorized': False}))
        example = json.loads((REPO_ROOT / 'examples/owned_episode_export_record.json').read_text())
        self.assertIs(example['synthetic'], True)
        validator('owned_episode_export_record').validate(example['record'])

    def test_review_exact_candidate_set_and_confirmation_cannot_drift(self):
        role = 'system1'
        selected = [item for item in self.candidates if item['role'] == role]
        with self.assertRaises(ValueError):
            self.store.review(self.episode_id, role, 'accept', selected, self.execution, '0' * 64)
        receipts = self.accept()
        changed = copy.deepcopy(self.candidates)
        changed[0]['source']['call_ref'] = '0' * 64
        changed[0]['candidate_id'] = digest({key: value for key, value in changed[0].items() if key != 'candidate_id'})
        with self.assertRaises(ValueError):
            self.store.export(self.episode_id, changed, self.execution, receipts)

    def test_revocation_blocks_new_export_without_mutating_old_export(self):
        receipts = self.accept()
        result = self.store.export(self.episode_id, self.candidates, self.execution, receipts)
        path = self.store.root / self.episode_id / (result['export_sha256'] + '-manifest.json')
        before = path.read_bytes()
        self.store.revoke(self.episode_id, 'system2', receipts['system2'])
        with self.assertRaises(ValueError):
            self.store.export(self.episode_id, self.candidates, self.execution, receipts)
        self.assertEqual(path.read_bytes(), before)

    def test_accepted_review_and_export_reject_execution_source_drift(self):
        receipts = self.accept()
        review = self.store.reviews(self.episode_id)['system1']['receipt']
        self.assertEqual(review['execution']['execution_source_sha256'], 'b' * 64)
        changed_execution = self.execution | {'execution_source_sha256': 'c' * 64}
        selected = [item for item in self.candidates if item['role'] == 'system1']
        self.assertNotEqual(
            self.store.review_selection(self.episode_id, 'system1', selected, self.execution),
            self.store.review_selection(self.episode_id, 'system1', selected, changed_execution))
        with self.assertRaises(ValueError):
            self.store.export(self.episode_id, self.candidates, changed_execution, receipts)
        self.assertEqual(self.store.reviews(self.episode_id)['system1']['receipt'], review)

    def test_modified_candidate_bytes_and_links_fail_closed(self):
        directory = self.store.root / self.episode_id
        path = directory / ('candidate-' + self.candidates[0]['candidate_id'] + '.json')
        raw = path.read_bytes()
        path.write_bytes(raw + b' ')
        with self.assertRaises(ValueError):
            self.store.candidates(self.episode_id)
        path.write_bytes(raw)
        os.link(path, directory / 'linked.json')
        with self.assertRaises(ValueError):
            self.store.candidates(self.episode_id)
