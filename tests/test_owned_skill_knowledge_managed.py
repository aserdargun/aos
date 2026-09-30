from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical
from aos.knowledge import KnowledgeStore
import test_owned_candidate_execution_managed as execution_helpers
import test_owned_skill_release_managed as release_helpers
from test_owned_skill_reuse_managed import owned_subprocess_session, read_isolated_state


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_SKILL_KNOWLEDGE_MANAGED_TESTS') == '1',
                     'Explicit isolated real Bonsai-to-Decider document-context acceptance; synthetic site')
class OwnedSkillKnowledgeManagedTests(unittest.TestCase):
    def post(self, client, path, value, expected=200):
        response = client.post(path, json=value)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def await_plan(self, client):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get('/api/tasks/owned-skill-plan')
            response.raise_for_status()
            status = response.json()
            if status['status'] != 'pending':
                self.assertEqual(status['status'], 'ready', status)
                return status
            time.sleep(.1)
        self.fail('Context plan did not complete within 180 seconds')

    def test_real_bilingual_context_plans_execute_and_revocation_blocks_fill(self):
        base = REPO_ROOT / 'data' / ('owned-skill-knowledge-managed-' + uuid4().hex)
        base.mkdir(mode=0o700)
        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        releases = release_helpers.OwnedSkillReleaseManagedTests()
        prefix = '/api/tasks/owned-form-candidate/'
        scope = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
        store = KnowledgeStore(base / 'document-knowledge')
        publication = store.publish_preview(scope=scope, source_id='manual', title='Synthetic skill manual',
            text='Synthetic skill manual: preserve the admitted recipe, exact message and verify the receipt.',
            previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        document = store.publish(preview=publication, confirm_sha256=publication['preview_sha256'])
        review_preview = store.review_preview(scope=scope, document_sha256=document['document_sha256'], decision='accept')
        store.review(preview=review_preview, confirm_sha256=review_preview['preview_sha256'])
        audits = []
        started_at = time.perf_counter()
        try:
            with owned_subprocess_session(base) as first:
                context, candidate, _job = helper.create_source_candidate(first.client)
                review = releases.evidence_and_review(first.client, helper, context, candidate, 'beta')
                request = {'schema_version': '1.0', 'review_sha256': review, 'parent_release_sha256': None}
                release = self.post(first.client, prefix + 'release-preview', request)['release_sha256']
                self.post(first.client, prefix + 'release-publish', request | {'confirm_sha256': release})
                request = {'schema_version': '1.0', 'release_sha256': release,
                           'expected_selection_sha256': None, 'operation': 'select'}
                selection = self.post(first.client, prefix + 'selection-preview', request)['selection_sha256']
                self.post(first.client, prefix + 'selection-commit', request | {'confirm_sha256': selection})
                database = first.source / 'store.sqlite'
            previous = read_isolated_state(base)
            self.assertEqual(previous.phase, 'stopped')
            with patch('socket.socket', side_effect=AssertionError('offline_reuse_preview')):
                material = local_app.prepare_owned_skill_reuse(previous, release, selection, base=base)
            with owned_subprocess_session(base, previous=previous, material=material) as second:
                client = second.client
                self.assertTrue(client.get('/api/tasks').json()['owned_skill_planning']['knowledge_available'])
                for language, goal in (('en', 'Save message "gamma"'),
                                       ('tr', 'Mesaj alanına "delta" kaydet'),
                                       ('en-revoked', 'Save message "epsilon"')):
                    counts = helper.database_counts(database)
                    control = client.get('/api/state').json()['control']
                    wire = self.post(client, '/api/tasks/owned-skill-knowledge/preview', {
                        'schema_version': '1.0', 'goal': goal, 'scope': scope, 'query': 'skill recipe',
                        'top_k': 4, 'context_chars': 2048,
                        'lease_id': control['lease_id'], 'generation': control['generation']})
                    preview = wire['preview']
                    self.assertEqual(preview['deployment']['pins']['temperature'], 0.0)
                    self.assertIsInstance(preview['deployment']['pins']['temperature'], float)
                    self.assertEqual(helper.database_counts(database), counts)
                    start = {'schema_version': '1.1', 'preview_canonical': wire['preview_canonical'],
                        'confirm_sha256': preview['confirm_sha256'], 'inference_consent': True,
                        'storage_consent': True, 'lease_id': control['lease_id'], 'generation': control['generation']}
                    self.post(client, '/api/tasks/owned-skill-knowledge/start', start, 202)
                    plan = self.await_plan(client)
                    report_request = {'schema_version': '1.0', 'planning_bundle_sha256': plan['bundle_sha256']}
                    wire_report = self.post(client, '/api/tasks/owned-skill-knowledge/report', report_request)
                    report = wire_report['report']
                    self.assertTrue(report['real_model'] and report['knowledge_applied'] and report['model_request_verified'])
                    self.assertTrue(report['current_source_valid'] and report['current_authority_valid'])
                    self.assertFalse(report['execution_authorized'] or report['training_ready'] or report['downstream_verified'])
                    self.assertEqual(report['bundle']['knowledge']['intent']['preview'], preview)
                    self.assertEqual(helper.database_counts(database), counts)
                    bind_request = {'schema_version': '1.4', 'planning_bundle_sha256': plan['bundle_sha256'],
                        'confirm_plan_sha256': plan['bundle_sha256'],
                        'lease_id': control['lease_id'], 'generation': control['generation']}
                    bound = self.post(client, '/api/tasks/owned-skill-plan/bind', bind_request)
                    self.assertEqual(bound['planning_bundle_sha256'], plan['bundle_sha256'])
                    self.assertEqual(len(bound['steps']), 6)
                    self.assertEqual(helper.database_counts(database), counts)
                    execution_start = bind_request | {'preview_sha256': bound['preview_sha256'],
                                                       'confirm_sha256': bound['preview_sha256']}
                    executed = self.post(client, '/api/tasks/owned-skill-plan/start', execution_start)
                    if language == 'en-revoked':
                        self.revoke_before_fill(client, store, scope, document, helper, database)
                        historical = self.post(client, '/api/tasks/owned-skill-knowledge/report', report_request)['report']
                        self.assertTrue(historical['historical_binding_verified'] and historical['knowledge_applied'])
                        self.assertFalse(historical['current_source_valid'])
                    else:
                        job, approvals = helper.approve_job(client)
                        self.assertEqual(job['status'], 'succeeded', job)
                        self.assertEqual(len(approvals), 6)
                        releases.wait_idle(client)
                        audited = self.post(client, '/api/tasks/owned-skill-plan/audit', {
                            'schema_version': '1.4', 'candidate_execution_sha256': executed['candidate_execution_sha256']})
                        self.assertTrue(audited['available'] and audited['planning_admission_verified'], audited)
                        self.assertEqual(audited['planning_bundle_sha256'], plan['bundle_sha256'])
                        audits.append({'language': language, 's2_report': report, 'execution_audit': audited,
                                       'manual_approvals': len(approvals), 'job': job})
                        with (base / ('audit-' + language + '.json')).open('x') as stream:
                            os.chmod(stream.name, 0o600)
                            stream.write(canonical(audits[-1]))
                    after = helper.database_counts(database)
                    self.post(client, '/api/tasks/owned-skill-knowledge/start', start, 409)
                    self.post(client, '/api/tasks/owned-skill-plan/start', execution_start, 409)
                    self.assertEqual(helper.database_counts(database), after)
            self.assertEqual(read_isolated_state(base).phase, 'stopped')
            result = {'synthetic': True, 'scope': 'real managed Bonsai-to-Decider with synthetic site and corpus',
                'cases': [{'language': item['language'], 'manual_approvals': item['manual_approvals'],
                           'planning_bundle_sha256': item['s2_report']['planning_bundle_sha256']} for item in audits],
                'revocation_blocked_fill_and_post': True, 'replay_rejected': True,
                'wall_seconds': time.perf_counter() - started_at,
                'real_site_acceptance': False, 'causality_verified': False, 'training_ready': False}
            with (base / 'acceptance.json').open('x') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(canonical(result))
            print('Managed S2 context acceptance: ' + str(base.relative_to(REPO_ROOT) / 'acceptance.json'), flush=True)
        finally:
            print('Private managed context evidence retained: ' + str(base.relative_to(REPO_ROOT)), flush=True)

    def revoke_before_fill(self, client, store, scope, document, helper, database):
        accepted = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            tasks = client.get('/api/tasks').json()
            approval = tasks.get('approval')
            if approval and approval['action']['tool'] == 'browser.form.fill':
                break
            if approval and approval['approval_id'] not in accepted:
                self.assertIn(approval['action']['tool'], ('browser.form.open', 'browser.form.state_before'))
                self.post(client, '/api/approvals/' + approval['approval_id'], {
                    'action_sha256': approval['action_sha256'], 'accept': True})
                accepted.append(approval['approval_id'])
            self.assertNotIn(tasks['jobs'][0]['status'], ('succeeded', 'failed', 'cancelled'))
            time.sleep(.1)
        else:
            self.fail('No pending fill reached before deadline')
        self.assertEqual(len(accepted), 2)
        before = helper.database_counts(database)
        review = store.review_preview(scope=scope, document_sha256=document['document_sha256'], decision='revoke')
        store.review(preview=review, confirm_sha256=review['preview_sha256'])
        self.post(client, '/api/approvals/' + approval['approval_id'], {
            'action_sha256': approval['action_sha256'], 'accept': True}, 409)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = client.get('/api/tasks').json()['jobs'][0]
            if job['status'] in ('failed', 'cancelled'):
                break
            self.assertNotEqual(job['status'], 'succeeded')
            time.sleep(.1)
        else:
            self.fail('Revoked source failed to cancel pending fill')
        self.assertEqual(helper.database_counts(database)[2], before[2])
        with closing(sqlite3.connect(database)) as connection:
            tools = connection.execute('SELECT tool FROM actions WHERE run_id=?', (job['run_id'],)).fetchall()
        self.assertFalse(any('submit' in row[0] or 'fill' in row[0] for row in tools), tools)
