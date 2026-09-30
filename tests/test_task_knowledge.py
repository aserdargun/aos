from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from aos.contracts import AOSFault, HELLO_CONTENT, Option, Phase, Prediction, REPO_ROOT, canonical, digest, now
from aos.dataset import validator
from aos.decision import decision_request
from aos.knowledge import KnowledgeStore
from aos.task_knowledge import (ADMISSION, DISPATCH, FLAGS, MARKER, PREFIX, REQUESTS, RESPONSES,
                                TaskKnowledgeContext, TaskKnowledgeModelProof, TaskKnowledgeReport,
                                TaskKnowledgeService)
import test_desktop_tasks as desktop_fixtures


SCOPE = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
TEXT = 'Synthetic hello document: use the fresh file observation and verify exact authorized content.'


class TaskKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = desktop_fixtures.DesktopTaskTests.asyncTearDown
    approval = desktop_fixtures.DesktopTaskTests.approval

    async def asyncSetUp(self):
        await desktop_fixtures.DesktopTaskTests.asyncSetUp(self)
        self.instant = datetime.now(timezone.utc)
        self.knowledge = KnowledgeStore(self.root / 'knowledge', clock=lambda: self.instant)
        self.scheduler.configure_task_knowledge(self.knowledge, self.root / 'task-knowledge')
        self.service = self.scheduler.task_knowledge
        self.document = self.publish()
        self.review()

    def authority(self):
        control = self.controller.state()
        return {'lease_id': control['lease_id'], 'generation': control['generation']}

    def publish(self, text=TEXT, previous_sha256=None, scope=None, source_id='manual'):
        preview = self.knowledge.publish_preview(scope=scope or SCOPE, source_id=source_id,
            title='Synthetic manual', text=text, previous_sha256=previous_sha256,
            expires_at=(self.instant + timedelta(hours=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        return self.knowledge.publish(preview=preview, confirm_sha256=preview['preview_sha256'])

    def review(self, decision='accept', document=None):
        document = document or self.document
        preview = self.knowledge.review_preview(scope=document['document']['scope'],
            document_sha256=document['document_sha256'], decision=decision)
        return self.knowledge.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    def preview(self, **changes):
        return self.scheduler.preview_task_knowledge(**({'scope': SCOPE, 'query': 'hello', 'top_k': 4,
            'context_chars': 2048, 'task_kind': 'hello', **self.authority()} | changes))

    def start_knowledge(self, preview=None, **changes):
        preview = preview or self.preview()
        return self.scheduler.start_task_knowledge(**({'preview': preview,
            'confirm_sha256': preview['confirm_sha256'], 'consent': True, **self.authority()} | changes))

    async def complete(self, started):
        approval = await self.approval()
        self.assertEqual(approval['job_id'], started['job_id'])
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')

    async def test_preview_is_readonly_current_bound_and_context_is_data(self):
        before = self.store.connection.total_changes
        preview = self.preview()
        self.assertFalse(self.service.directory.exists())
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertTrue(preview['context_text'].startswith(PREFIX))
        self.assertIn(TEXT, preview['context_text'])
        self.assertEqual(preview['context_sha256'], hashlib.sha256(
            preview['context_text'].encode()).hexdigest())
        self.assertEqual(preview['confirm_sha256'], digest({key: value for key, value in preview.items()
                                                         if key != 'confirm_sha256'}))
        self.assertEqual(preview['target'], self.scheduler.failure_followup_target('hello'))
        self.assertNotEqual(preview['preview_id'], self.preview()['preview_id'])
        for key, value in FLAGS.items():
            self.assertIs(preview[key], value)

    async def test_separate_consent_exact_preview_and_replay_single_use(self):
        preview = self.preview()
        for value in (False, 1, 0, 'true', None):
            with self.assertRaises(ValueError):
                self.start_knowledge(preview, consent=value)
        for changed in (preview | {'extra': 'private'}, preview | {'query': 'changed'},
                        preview | {'context_text': 'injected instructions'}):
            with self.assertRaises(ValueError):
                self.start_knowledge(changed)
        with self.assertRaises(ValueError):
            self.start_knowledge(preview, confirm_sha256='0' * 64)
        self.assertFalse(self.service.directory.exists())
        started = self.start_knowledge(preview)
        await self.complete(started)
        with self.assertRaises((ValueError, OSError)):
            self.start_knowledge(preview)

    async def test_context_bound_to_snapshot_and_unchanged_task_fixture_never_real(self):
        started = self.start_knowledge()
        await self.complete(started)
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        report = self.service.report(started['job_id'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['context_binding_verified'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['knowledge_applied'])
        self.assertTrue(report['current_source_valid'])
        self.assertTrue(report['current_authority_valid'])
        self.assertEqual(report['outcome']['status'], 'verified')
        self.assertEqual(report['prepared_context_count'], 1)
        self.assertEqual(report['dispatched_context_count'], 0)
        self.assertEqual(report['successful_model_call_count'], 0)
        self.assertEqual(report['model_proofs'], [])
        marker = report['context_proofs'][0]
        snapshot = self.store.connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=?',
                                                 (marker['snapshot_id'],)).fetchone()
        from aos.contracts import State
        state = State.model_validate_json(snapshot['state_json'])
        options = [Option.model_validate(value) for value in marker['options']]
        self.assertEqual(marker['state_sha256'], digest(state.model_dump(mode='json')))
        self.assertEqual(marker['request_sha256'], digest(decision_request(state, options)))
        self.assertEqual(state.authorized_content, HELLO_CONTENT)
        self.assertEqual(digest(report['intent']), report['intent_sha256'])
        self.assertEqual(digest(report['admission']), report['admission_sha256'])
        self.assertEqual(self.service.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.service.directory.iterdir()))
        for changes in ({'knowledge_applied': True}, {'successful_model_call_count': 1},
                        {'model_request_verified': True}, {'admission_sha256': '0' * 64}):
            with self.assertRaises(ValueError):
                TaskKnowledgeReport.model_validate(report | changes)

    async def test_source_review_revocation_retains_only_historical_proof(self):
        started = self.start_knowledge()
        await self.complete(started)
        self.review('revoke')
        report = self.service.report(started['job_id'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['context_binding_verified'])
        self.assertFalse(report['current_source_valid'])
        self.assertFalse(report['knowledge_applied'])
        self.assertEqual(report['outcome']['status'], 'verified')

    async def test_source_revoke_while_waiting_approval_blocks_effect(self):
        started = self.start_knowledge()
        approval = await self.approval()
        self.review('revoke')
        with self.assertRaises((AOSFault, ValueError)):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertFalse(self.service.report(started['job_id'])['current_source_valid'])

    async def test_pending_foreign_revoked_expired_and_new_review_reject(self):
        with self.assertRaises(ValueError):
            self.preview(scope=SCOPE | {'tenant_id': 'foreign'})
        pending = self.publish('Synthetic hello pending document.', source_id='pending')
        preview = self.preview()
        self.assertNotIn(pending['document_sha256'], [hit['document_sha256'] for hit in preview['retrieval']['hits']])
        self.review('accept')
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        preview = self.preview()
        self.instant += timedelta(hours=2)
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        self.instant -= timedelta(hours=2)
        preview = self.preview()
        self.publish('Synthetic hello pending successor.', self.document['document_sha256'])
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)

    async def test_control_configuration_preview_expiry_and_workspace_drift(self):
        preview = self.preview()
        self.service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=301)
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        self.service.clock = lambda: datetime.now(timezone.utc)
        self.scheduler.settings = self.scheduler.settings.model_copy(update={'execute_min': .89})
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        preview = self.preview()
        self.controller.control('take-control')
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        self.controller.control('return-control')
        preview = self.preview()
        old = self.root / 'old-workspace'
        self.runtime.root.rename(old)
        self.runtime.root.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)

    async def test_source_root_clone_and_intent_tampering_fail_closed(self):
        preview = self.preview()
        old = self.root / 'old-knowledge'
        self.knowledge.root.rename(old)
        self.knowledge.root.mkdir(mode=0o700)
        for path in old.iterdir():
            copied = self.knowledge.root / path.name
            copied.write_bytes(path.read_bytes())
            copied.chmod(0o600)
        with self.assertRaises(ValueError):
            self.start_knowledge(preview)
        started = self.start_knowledge()
        approval = await self.approval()
        filename = self.service.directory / ('intent-' + started['intent_sha256'] + '.json')
        filename.write_bytes(filename.read_bytes() + b' ')
        with self.assertRaises((AOSFault, ValueError)):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        with self.assertRaisesRegex(ValueError, '^task_knowledge_intent_invalid$'):
            self.service.report(started['job_id'])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_context_marker_snapshot_and_request_tampering_reject_binding(self):
        started = self.start_knowledge()
        await self.complete(started)
        row = self.store.connection.execute('SELECT * FROM observations WHERE kind=?', (MARKER,)).fetchone()
        original = json.loads(row['payload_json'])
        for change in ({'request_sha256': '0' * 64}, {'state_sha256': '0' * 64},
                       {'snapshot_id': 'foreign-snapshot'}, {'options': [{'id': 'unauthorized', 'label': 'Change task'}]}):
            with self.store.connection:
                self.store.connection.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                    (canonical(original | change), row['observation_id']))
            report = self.service.report(started['job_id'])
            self.assertFalse(report['historical_binding_verified'])
            self.assertFalse(report['model_request_verified'])
            self.assertFalse(report['knowledge_applied'])
        with self.store.connection:
            self.store.connection.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                (canonical(original), row['observation_id']))

    async def test_fixture_dispatch_marker_cannot_claim_actual_model_call(self):
        started = self.start_knowledge()
        await self.complete(started)
        report = self.service.report(started['job_id'])
        marker = report['context_proofs'][0]
        with self.store.connection:
            self.store.insert('observations', observation_id='synthetic-dispatch', run_id=marker['run_id'],
                step_id=marker['step_id'], action_id=None, kind=DISPATCH,
                payload_json=canonical(dict(marker, actual_request_sha256=marker['request_sha256'], actual_request={})),
                created_at=now())
        report = self.service.report(started['job_id'])
        self.assertFalse(report['historical_binding_verified'])
        self.assertFalse(report['knowledge_applied'])

    async def test_synthetic_call_rows_require_dispatch_exact_request_and_prediction(self):
        started = self.start_knowledge()
        await self.complete(started)
        report = self.service.report(started['job_id'])
        marker = report['context_proofs'][0] | {'call_id': 'call-synthetic-proof'}
        job = dict(self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                                                 (started['job_id'],)).fetchone())
        job['system1_real_model'] = True
        snapshot = self.store.connection.execute('SELECT state_json FROM state_snapshots WHERE snapshot_id=?',
                                                 (marker['snapshot_id'],)).fetchone()
        from aos.contracts import State
        state = State.model_validate_json(snapshot['state_json'])
        options = [Option.model_validate(value) for value in marker['options']]
        request = decision_request(state, options)
        prediction = Prediction(selected_option=options[0].id,
                                probabilities={option.id: float(index == 0) for index, option in enumerate(options)})
        with self.store.connection:
            self.store.insert('model_calls', call_id=marker['call_id'], run_id=state.run_id,
                step_id=state.step_id, deployment_id=state.deployment_id, role='system1', status='error',
                request_json=canonical(request), response_json=prediction.model_dump_json(), created_at=now())
            self.store.connection.execute('UPDATE observations SET payload_json=?,created_at=? WHERE kind=?',
                                          (canonical(marker), now(), MARKER))
            self.store.connection.execute('UPDATE decisions SET call_id=? WHERE run_id=?',
                                          (marker['call_id'], state.run_id))
        proof = self.service._application(self.store.connection, report['intent'], report['admission'], job)
        self.assertTrue(proof['context_binding_verified'])
        self.assertFalse(proof['model_request_verified'])
        self.assertEqual(proof['model_proofs'], [])
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET status=? WHERE call_id=?',
                                          ('ok', marker['call_id']))
        with self.assertRaises(ValueError):
            self.service._application(self.store.connection, report['intent'], report['admission'], job)
        dispatched = dict(marker, actual_request_sha256=digest(request), actual_request=request)
        with self.store.connection:
            self.store.insert('observations', observation_id='synthetic-model-dispatch', run_id=state.run_id,
                step_id=state.step_id, action_id=None, kind=DISPATCH,
                payload_json=canonical(dispatched), created_at=now())
            self.store.connection.execute('UPDATE decisions SET created_at=? WHERE run_id=?', (now(), state.run_id))
        proof = self.service._application(self.store.connection, report['intent'], report['admission'], job)
        self.assertTrue(proof['model_request_verified'])
        self.assertEqual(proof['successful_model_call_count'], 1)
        model_proof = proof['model_proofs'][0]
        self.assertEqual(model_proof, dict(marker, deployment_id=state.deployment_id,
            decision_id=self.store.connection.execute('SELECT decision_id FROM decisions WHERE run_id=?',
                                                     (state.run_id,)).fetchone()['decision_id'],
            actual_request_sha256=digest(request), response_sha256=digest(prediction.model_dump(mode='json')),
            response_canonical=canonical(prediction.model_dump(mode='json')),
            response=prediction.model_dump(mode='json')))
        audited = report | proof | {'model_request_verified': True, 'knowledge_applied': True}
        TaskKnowledgeReport.model_validate(audited)
        validator('task_knowledge_report_response').validate(audited)
        for changes in ({'model_proofs': []}, {'model_proofs': [model_proof, model_proof]},
                        {'model_proofs': [model_proof | {'response_sha256': '0' * 64}]},
                        {'model_proofs': [model_proof | {'response_canonical': '{}'}]},
                        {'model_proofs': [model_proof | {'deployment_id': 'wrong-deployment'}]},
                        {'model_proofs': [model_proof | {'request_sha256': '0' * 64}]},
                        {'model_proofs': [model_proof | {'snapshot_id': 'wrong-snapshot'}]},
                        {'model_proofs': [model_proof | {'response': {
                            'selected_option': 'unknown', 'probabilities': prediction.probabilities}}]}):
            with self.assertRaises(ValueError):
                TaskKnowledgeReport.model_validate(audited | changes)
        for probabilities in ({options[0].id: 1.0 - 1e-7 - 1e-5, options[1].id: 1e-7,
                               options[2].id: 1e-5},
                              {options[0].id: 1.0, options[1].id: -0.0, options[2].id: 0.0}):
            response = {'selected_option': options[0].id, 'probabilities': probabilities}
            value = model_proof | {'response': response, 'response_canonical': canonical(response),
                                   'response_sha256': digest(response)}
            parsed = TaskKnowledgeModelProof.model_validate(value).model_dump(mode='json')
            self.assertEqual(parsed, value)
            self.assertEqual(hashlib.sha256(parsed['response_canonical'].encode('utf-8')).hexdigest(),
                             parsed['response_sha256'])
            with self.assertRaises(ValueError):
                TaskKnowledgeModelProof.model_validate(value | {'response_canonical': json.dumps(response)})
        for table, column, replacement in (('decisions', 'selected_option', 'unknown'),
                                            ('decisions', 'probabilities_json', '{}'),
                                            ('model_calls', 'deployment_id', 'wrong-deployment')):
            original = self.store.connection.execute('SELECT ' + column + ' FROM ' + table).fetchone()[column]
            with self.store.connection:
                self.store.connection.execute('UPDATE ' + table + ' SET ' + column + '=?', (replacement,))
            with self.assertRaises(ValueError):
                self.service._application(self.store.connection, report['intent'], report['admission'], job)
            with self.store.connection:
                self.store.connection.execute('UPDATE ' + table + ' SET ' + column + '=?', (original,))
        with self.assertRaises(ValueError):
            self.service.report(started['job_id'])
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET request_json=? WHERE call_id=?',
                                          ('{}', marker['call_id']))
        with self.assertRaises(ValueError):
            self.service._application(self.store.connection, report['intent'], report['admission'], job)
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET request_json=?,response_json=? WHERE call_id=?',
                                          (canonical(request), '{}', marker['call_id']))
        with self.assertRaises(ValueError):
            self.service._application(self.store.connection, report['intent'], report['admission'], job)
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET response_json=? WHERE call_id=?',
                                          (prediction.model_dump_json(), marker['call_id']))
            self.store.connection.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                                          (canonical(dict(dispatched, actual_request={})), 'synthetic-model-dispatch'))
        with self.assertRaises(ValueError):
            self.service._application(self.store.connection, report['intent'], report['admission'], job)

    async def test_resumed_or_reused_task_requires_new_authority_and_intent(self):
        started = self.start_knowledge()
        await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.controller.control('resume')
        job = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                                            (started['job_id'],)).fetchone()
        with self.assertRaises(ValueError):
            self.service.guard(self.store.connection, started['intent_sha256'], started['job_id'],
                               self.scheduler.failure_followup_target('hello'))
        self.assertFalse(self.service.report(started['job_id'])['current_authority_valid'])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_private_admission_and_event_must_match(self):
        started = self.start_knowledge()
        await self.complete(started)
        admission_path = self.service.directory / ('admission-' + started['job_id'] + '.json')
        value = json.loads(admission_path.read_text())
        admission_path.write_text(canonical(value | {'context_sha256': '0' * 64}))
        with self.assertRaises(ValueError):
            self.service.report(started['job_id'])

    async def test_missing_context_marker_cannot_authorize_effect(self):
        started = self.start_knowledge()
        approval = await self.approval()
        with self.store.connection:
            self.store.connection.execute('DELETE FROM observations WHERE kind=?', (MARKER,))
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')
        report = self.service.report(started['job_id'])
        self.assertFalse(report['historical_binding_verified'])
        self.assertFalse(report['context_binding_verified'])

    async def test_limits_and_incompatible_runtime_mode(self):
        for changes in ({'top_k': 5}, {'context_chars': 2049}, {'query': ' '},
                        {'query': 'x' * 513}, {'task_kind': 'browser_remote_form'}):
            with self.assertRaises(ValueError):
                self.preview(**changes)
        self.scheduler._owned_skill_reuse = {'synthetic': True}
        with self.assertRaises(ValueError):
            self.preview()
        self.scheduler._owned_skill_reuse = None

    async def test_each_effect_rechecks_engine_target_and_private_directory(self):
        started = self.start_knowledge()
        approval = await self.approval()
        identity = self.scheduler.engine.identity
        self.scheduler.engine.identity = dict(identity, deployment_id='fixture-drift')
        with self.assertRaises((ValueError, AOSFault)):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.scheduler.engine.identity = identity
        self.assertFalse(self.service.report(started['job_id'])['knowledge_applied'])
        old = self.root / 'old-task-knowledge'
        self.service.directory.rename(old)
        self.service.directory.mkdir(mode=0o700)
        for path in old.iterdir():
            target = self.service.directory / path.name
            target.write_bytes(path.read_bytes())
            target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.service.report(started['job_id'])

    async def test_other_learning_and_skill_lanes_cannot_admit_document_context(self):
        for name in ('_owned_candidate_execution', '_active_owned_candidate_execution',
                     'remote_form_skill_invocation'):
            original = getattr(self.scheduler, name, None)
            setattr(self.scheduler, name, {'synthetic': True})
            with self.assertRaises(ValueError):
                self.service.preview(scope=SCOPE, query='hello', top_k=4, context_chars=2048,
                                     task_kind='hello', **self.authority())
            setattr(self.scheduler, name, original)

    async def test_synthetic_fixture_and_schema_contracts(self):
        fixture = json.loads((REPO_ROOT / 'examples' / 'task_knowledge.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertFalse(fixture['responses']['report']['knowledge_applied'])
        for direction, models in (('request', REQUESTS), ('response', RESPONSES)):
            for operation, model in models.items():
                value = fixture[direction + 's'][operation]
                model.model_validate(value)
                schema_name = 'task_knowledge_' + operation + '_' + direction
                validator(schema_name).validate(value)
                schema = json.loads((REPO_ROOT / 'schemas' / (schema_name + '.schema.json')).read_text())
                self.assertEqual({key: item for key, item in schema.items() if key != '$schema'},
                                 model.model_json_schema())
