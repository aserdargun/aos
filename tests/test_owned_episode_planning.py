import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aos.contracts import digest
from aos.owned_episode_learning import OwnedEpisodeStore, consent_record
from aos.owned_episode_service import OwnedEpisodeLearning
from aos.owned_skill_planner import OwnedSkillPlan
import test_owned_skill_planning as planning_helpers


class OwnedEpisodePlanningConsentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = planning_helpers.OwnedSkillPlanningTests.asyncSetUp
    asyncTearDown = planning_helpers.OwnedSkillPlanningTests.asyncTearDown
    prepare = planning_helpers.OwnedSkillPlanningTests.prepare
    begin = planning_helpers.OwnedSkillPlanningTests.begin

    async def test_opt_in_consent_precedes_creation_of_model_task_and_binds_bundle(self):
        store = OwnedEpisodeStore(self.root / 'episodes')
        created = []

        def before(planning_id, request, authority):
            self.assertIsNone(self.service.task)
            self.assertFalse(self.planner.called.is_set())
            receipt = consent_record(planning_id, request, authority)
            checksum = store.create(receipt)
            created.append(receipt)
            return {'episode_id': receipt['episode_id'], 'collection_consent_sha256': checksum}

        self.service.before_begin = before
        self.service.on_proposal = Mock()
        state = self.authority
        result = self.service.begin('Save message "synthetic-value"', state['lease_id'], state['generation'],
                                    collect_learning=True)
        self.assertEqual(result['episode_id'], created[0]['episode_id'])
        self.assertNotIn('collection_consent_sha256', result)
        await self.service.task
        bundle = self.service.load(self.service.current['bundle_sha256'])
        self.assertEqual(bundle['schema_version'], '1.1')
        self.assertEqual(bundle['collection_consent_sha256'], digest(created[0]))
        self.assertEqual(bundle['episode_id'], result['episode_id'])
        self.service.on_proposal.assert_called_once_with(bundle)

    async def test_default_opt_out_writes_no_episode_content_or_consent(self):
        self.service.before_begin = Mock(side_effect=AssertionError('must_not_collect'))
        self.service.on_proposal = Mock(side_effect=AssertionError('must_not_collect'))
        self.begin()
        await self.service.task
        self.assertEqual(self.service.status()['status'], 'ready')
        self.assertNotIn('episode_id', self.service.status())
        self.assertFalse((self.root / 'episodes').exists())
        self.service.before_begin.assert_not_called()
        self.service.on_proposal.assert_not_called()

    async def test_failed_consent_write_starts_no_model_task(self):
        self.service.before_begin = Mock(side_effect=OSError('synthetic-disk-failure'))
        state = self.authority
        with self.assertRaises(OSError):
            self.service.begin('Save message "synthetic-value"', state['lease_id'], state['generation'],
                               collect_learning=True)
        self.assertIsNone(self.service.task)
        self.assertFalse(self.planner.called.is_set())
        self.assertEqual(self.service.current['status'], 'failed')

    def attach_learning(self):
        learning = OwnedEpisodeLearning(SimpleNamespace(), self.root / 'episodes')
        self.service.before_begin = learning.begin
        self.service.episode_learning = learning
        return learning

    async def test_cancel_before_first_tick_latches_episode_terminal_state(self):
        learning = self.attach_learning()
        self.service.begin('Save message "synthetic-value"', self.authority['lease_id'],
                           self.authority['generation'], collect_learning=True)
        await self.service.cancel()
        self.assertFalse(self.planner.called.is_set())
        self.begin()
        self.assertEqual(learning.status()['state'], 'cancelled')

    async def test_model_failure_latches_episode_without_status_poll(self):
        learning = self.attach_learning()
        self.planner.failure = True
        self.service.begin('Save message "synthetic-value"', self.authority['lease_id'],
                           self.authority['generation'], collect_learning=True)
        await self.service.task
        self.begin()
        self.assertEqual(learning.status()['state'], 'failed')

    async def test_terminal_projection_does_not_downgrade_historical_episode(self):
        learning = self.attach_learning()
        learning.current = {'episode_id': 'episode-' + '1' * 32, 'state': 'reviewable',
                            'counts': {'system1': 6, 'system2': 1}}
        learning.planning_terminal({'episode_id': learning.current['episode_id'], 'status': 'cancelled'})
        self.assertEqual(learning.status()['state'], 'reviewable')
        learning.current['state'] = 'collecting'
        learning.planning_terminal({'episode_id': 'episode-' + '2' * 32, 'status': 'failed'})
        self.assertEqual(learning.status()['state'], 'collecting')
        learning.planning_terminal({'episode_id': learning.current['episode_id'], 'status': 'needs_human'})
        self.assertEqual(learning.status()['state'], 'needs_human')

    async def test_structured_abstention_latches_without_new_status_poll(self):
        learning = self.attach_learning()
        self.planner.plan = AsyncMock(return_value=OwnedSkillPlan(
            decision='needs_human', evidence_refs=['admitted-skill'], steps=[],
            reason_code='evidence_insufficient'))
        self.service.begin('Save message "synthetic-value"', self.authority['lease_id'],
                           self.authority['generation'], collect_learning=True)
        await self.service.task
        self.assertEqual(self.service.current['status'], 'needs_human')
        self.begin()
        self.assertEqual(learning.status()['state'], 'needs_human')
