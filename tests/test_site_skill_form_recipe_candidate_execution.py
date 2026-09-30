import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_web_https_relay as relay_tests
from aos.contracts import AOSFault, REPO_ROOT, Settings, canonical, digest
from aos.desktop import DesktopRuntime
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.reusable_decider import ReusableDeciderEngine
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import (
    SiteSkillCaseInputs, SiteSkillFormFieldBinding, parameter_variant_sha256)
from aos.site_skill_form_recipe import (
    SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe, compile_site_skill_form_recipe_invocation)
from aos.site_skill_form_recipe_candidate_execution import (
    audit_candidate_execution, prepare_candidate_execution, revalidate_candidate_execution)
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.storage import TrajectoryStore
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
from aos.web_https_form_transport import form_body, plan_web_https_form


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_FORM_RECIPE_CANDIDATE_TESTS') == '1',
                     'Explicit real-Decider trajectory-derived recipe acceptance')
class RecipeCandidateExecutionTests(unittest.TestCase):
    setUp = relay_tests.WebHTTPSRelayTests.setUp

    def test_verified_source_derives_persisted_recipe_for_two_fresh_values(self):
        from aos.site_skill_form_recipe_candidate import (
            SiteSkillFormRecipeCandidateAnnotation, derive_site_skill_form_recipe_candidate,
            load_site_skill_form_recipe_candidate, persist_site_skill_form_recipe_candidate)

        page_value = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
        page = SitePageDraft.model_validate({**page_value,
            'profile_sha256': self.checksum, 'origin': self.profile.allowed_origins[0],
            'route_template': '/entry', 'outgoing_page_keys': []})
        pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        page_sha256 = digest(page.model_dump())
        pages.register(page, confirm_sha256=page_sha256)
        annotation = SiteSkillFormRecipeCandidateAnnotation.model_validate_json(canonical({
            'schema_version': '1.0', 'synthetic': True, 'skill_key': 'derived-form',
            'page_key': page.page_key, 'page_draft_sha256': page_sha256,
            'task_key': self.draft.task.task_key,
            'parameter_bindings': [{'parameter_key': 'record-query', 'form_field_name': 'message'}],
            'preconditions': [
                {'precondition_key': 'baseline-known', 'kind': 'declared_state_before'},
                {'precondition_key': 'form-available', 'kind': 'entry_form_available'}],
            'outcome': {'outcome_key': 'submitted-value-visible', 'parameter_key': 'record-query'},
            'operation_step_keys': [{'operation': operation, 'step_key': operation.replace('_', '-')}
                                    for operation in sorted(SUPPORTED_RECIPE_ORDERS[0])],
        }))
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1 id="outcome">Empty synthetic state</h1></html>'
        self.server.route_bodies = {
            '/entry': (b'<html><title>Derived recipe form</title><h1>Form</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Receipt</title><h1>Saved locally</h1></html>'}
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        engine = ReusableDeciderEngine(REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        candidate_directory = self.root / 'candidates'
        source_parameters = {'record-query': 'alpha'}

        def plans(value):
            body = form_body((('message', value),))
            after = f'<html><h1 id="outcome">{value}</h1></html>'.encode()
            form_plan = plan_web_https_form(self.profiles, self.draft.task,
                submit_url=origin + '/submit', receipt_url=origin + '/receipt',
                body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
            state_plan = WebHTTPSFormStatePlan(
                profile_sha256=self.checksum, task_sha256=digest(self.draft.task.model_dump()),
                form_plan_sha256=digest(form_plan.model_dump()), state_url=origin + '/state',
                expected_before_sha256=hashlib.sha256(before).hexdigest(),
                expected_after_sha256=hashlib.sha256(after).hexdigest(), marker_id='outcome',
                expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
                expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'),
                submitted_field_name='message')
            return body, after, form_plan, state_plan

        async def execute(value, form_plan, state_plan, *, invocation=None, admission=None,
                          revalidator=None, mutate=None, wrong_state=False):
            body, after, _, _ = plans(value)
            self.server.posts.clear()
            self.server.paths.clear()
            self.server.state_bodies = {False: before, True: before if wrong_state else after}
            scheduler = DesktopScheduler(controller,
                Settings(workspace=self.workspace, database=database), engine,
                browser_manifest=REPO_ROOT / 'models/browser-manifest.json', desktop_browser=True,
                remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
                remote_entry_task=self.draft.task, remote_form_plan=form_plan,
                remote_form_field_name='message', remote_form_value=value,
                remote_form_tls_context=self.tls_context, remote_form_state_plan=state_plan,
                remote_form_skill_invocation=invocation,
                remote_form_skill_invocation_sha256=digest(invocation) if invocation else None,
                remote_form_skill_revalidator=revalidator,
                remote_form_candidate_admission=admission)
            try:
                owner = controller.state()
                job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                         'browser_remote_form')['job_id']
                previous = None
                for operation in SUPPORTED_RECIPE_ORDERS[0]:
                    for _attempt in range(3000):
                        approval = scheduler.status()['approval']
                        if approval is not None and approval['approval_id'] != previous:
                            break
                        if scheduler.task.done():
                            self.fail('Candidate run terminated early: ' + str(scheduler.status()))
                        await asyncio.sleep(.02)
                    else:
                        self.fail('Candidate run approval timed out')
                    self.assertEqual(approval['action']['selected_option'], operation)
                    if mutate is not None and operation == 'fill_form':
                        mutate()
                        with self.assertRaises(AOSFault):
                            scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                        await scheduler.close()
                        break
                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                    previous = approval['approval_id']
                try:
                    await scheduler.task
                except asyncio.CancelledError:
                    if mutate is None:
                        raise
                job = store.connection.execute('SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                                               (job_id,)).fetchone()
                self.assertEqual(self.server.posts, [] if mutate else [('/submit', body)])
                if not wrong_state and mutate is None:
                    self.assertEqual(job['status'], 'succeeded')
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls "
                        "WHERE run_id=? AND role='system1' AND status='ok'", (job['run_id'],)).fetchone()[0], 6)
                else:
                    self.assertNotEqual(job['status'], 'succeeded')
                self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls "
                    "WHERE run_id=? AND role='system2'", (job['run_id'],)).fetchone()[0], 0)
                return job['run_id']
            finally:
                await scheduler.close()

        async def scenario():
            try:
                _, _, source_form, source_state = plans('alpha')
                source_run = await execute('alpha', source_form, source_state)
                candidate = derive_site_skill_form_recipe_candidate(database, source_run,
                    profiles=self.profiles, pages=pages, annotation=annotation,
                    source_parameters=source_parameters)
                self.assertEqual(len(candidate['skill']['source_event_ids']), 6)
                self.assertEqual(candidate['source_event_ids_by_role']['system2'], [])
                self.assertEqual(candidate['source_parameter_variant_sha256'],
                                 parameter_variant_sha256(source_parameters))
                renamed = derive_site_skill_form_recipe_candidate(database, source_run,
                    profiles=self.profiles, pages=pages,
                    annotation=annotation.model_copy(update={'skill_key': 'renamed-form'}),
                    source_parameters=source_parameters)
                self.assertEqual(renamed['source_group_sha256'], candidate['source_group_sha256'])
                self.assertNotEqual(digest(renamed), digest(candidate))
                renamed_annotation = annotation.model_dump(mode='json')
                renamed_annotation['parameter_bindings'][0]['parameter_key'] = 'renamed-parameter'
                renamed_annotation['outcome']['parameter_key'] = 'renamed-parameter'
                renamed_parameter = derive_site_skill_form_recipe_candidate(database, source_run,
                    profiles=self.profiles, pages=pages,
                    annotation=SiteSkillFormRecipeCandidateAnnotation.model_validate_json(
                        canonical(renamed_annotation)),
                    source_parameters={'renamed-parameter': 'alpha'})
                self.assertEqual(renamed_parameter['source_group_sha256'], candidate['source_group_sha256'])
                self.assertNotEqual(renamed_parameter['source_parameter_variant_sha256'],
                                    candidate['source_parameter_variant_sha256'])
                checksum = digest(candidate)
                annotation_file = self.root / 'annotation.json'
                parameter_file = self.root / 'source-parameters.json'
                for path, value in ((annotation_file, annotation.model_dump(mode='json')),
                                    (parameter_file, source_parameters)):
                    path.write_bytes(canonical(value).encode())
                    path.chmod(0o600)
                common = ['--database', str(database), '--profiles', str(self.profiles.root),
                          '--pages', str(pages.root), '--source-parameters-file', str(parameter_file)]
                selection = ['--run-id', source_run, '--annotation-file', str(annotation_file)]

                def candidate_cli(command, arguments, expected_code=0):
                    result = subprocess.run([sys.executable, '-m', 'aos.site_skill_form_recipe_candidate',
                                             command, *common, *arguments],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, expected_code, result.stderr)
                    self.assertNotIn('alpha', result.stdout + result.stderr)
                    return json.loads(result.stdout) if expected_code == 0 else None

                preview = candidate_cli('preview', selection)
                self.assertEqual(preview['candidate_sha256'], checksum)
                self.assertFalse(candidate_directory.exists())
                candidate_cli('publish', [*selection, '--directory', str(candidate_directory),
                                          '--confirm-sha256', '0' * 64], expected_code=1)
                self.assertFalse(candidate_directory.exists())
                published = candidate_cli('publish', [*selection, '--directory', str(candidate_directory),
                                                       '--confirm-sha256', checksum])
                self.assertEqual(published['candidate_sha256'], checksum)
                inspected = candidate_cli('inspect', ['--directory', str(candidate_directory),
                                                       '--sha256', checksum])
                self.assertEqual(inspected, published)
                self.assertEqual(persist_site_skill_form_recipe_candidate(
                    candidate_directory, candidate, confirm_sha256=checksum), checksum)
                self.assertEqual(persist_site_skill_form_recipe_candidate(
                    candidate_directory, candidate, confirm_sha256=checksum), checksum)
                candidate = load_site_skill_form_recipe_candidate(candidate_directory, checksum,
                    database=database, profiles=self.profiles, pages=pages,
                    source_parameters=source_parameters)
                skills = SiteSkillStore(self.root / 'skills', self.profiles, pages)
                skill = SiteSkillDraft.model_validate(candidate['skill'])
                skill_sha256 = digest(candidate['skill'])
                skills.register(skill, confirm_sha256=skill_sha256)
                recipe = SiteSkillFormRecipe.model_validate_json(canonical(candidate['recipe']))
                bindings = [SiteSkillFormFieldBinding.model_validate(binding)
                            for binding in candidate['field_bindings']]
                values = [('dev-beta', 'beta', 'development'), ('dev-gamma', 'gamma', 'development'),
                          ('reserved-delta', 'delta', 'held_out'), ('reserved-epsilon', 'epsilon', 'held_out')]
                plan = SiteSkillValidationPlan.model_validate({
                    'schema_version': '1.0', 'synthetic': True, 'skill_sha256': skill_sha256,
                    'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
                    'task_key': skill.task_key, 'model_role': 'system1',
                    'source_variant_sha256': [candidate['source_parameter_variant_sha256']],
                    'cases': [{'case_key': key, 'cohort': cohort, 'parameter_keys': ['record-query'],
                               'parameter_variant_sha256': parameter_variant_sha256({'record-query': value}),
                               'expected_outcome_key': skill.expected_outcome_key}
                              for key, value, cohort in values]})
                inputs = SiteSkillCaseInputs.model_validate({
                    'schema_version': '1.0', 'synthetic': True, 'plan_sha256': digest(plan.model_dump()),
                    'cases': [{'case_key': key, 'parameters': {'record-query': value}}
                              for key, value, _cohort in values]})
                reports = []
                for key, value, _cohort in values[:2]:
                    _, _, form_plan, state_plan = plans(value)
                    invocation = compile_site_skill_form_recipe_invocation(
                        skills, plan, inputs, key, self.profiles, self.draft.task,
                        form_plan, state_plan, bindings, recipe)
                    admission = prepare_candidate_execution(candidate, checksum, invocation, plan, inputs, key)
                    for partition in ('test', 'validation', 'independent_held_out'):
                        with self.assertRaises(ValueError):
                            prepare_candidate_execution(candidate, checksum, invocation,
                                                        plan, inputs, key, requested_partition=partition)
                    sources = dict(database=database, profiles=self.profiles, pages=pages,
                        source_parameters=source_parameters, store=skills, plan=plan, inputs=inputs,
                        case_key=key, task=self.draft.task, form_plan=form_plan, state_plan=state_plan,
                        field_bindings=bindings, recipe=recipe, invocation=invocation)
                    revalidator = lambda: revalidate_candidate_execution(admission, candidate_directory, **sources)
                    run_id = await execute(value, form_plan, state_plan, invocation=invocation,
                                           admission=admission, revalidator=revalidator)
                    report = audit_candidate_execution(admission, candidate_directory, run_id=run_id, **sources)
                    self.assertFalse(report['held_out_independence_verified'])
                    self.assertFalse(report['dataset_ingestion_authorized'])
                    self.assertTrue(report['source_bound'])
                    reports.append(report)
                    with self.assertRaises(ValueError):
                        audit_candidate_execution(admission, candidate_directory, run_id=source_run, **sources)
                self.assertEqual(len({report['source_group_sha256'] for report in reports}), 1)
                self.assertEqual(len({report['execution_run_ref'] for report in reports}), 2)
                self.assertEqual(load_site_skill_form_recipe_candidate(candidate_directory, checksum,
                    database=database, profiles=self.profiles, pages=pages,
                    source_parameters=source_parameters), candidate)
                copied = self.root / 'tampered.sqlite'
                with closing(sqlite3.connect(copied)) as connection, connection:
                    store.connection.backup(connection)
                    connection.execute("UPDATE model_calls SET request_json='{}' WHERE run_id=?", (source_run,))
                with self.assertRaises(ValueError):
                    load_site_skill_form_recipe_candidate(candidate_directory, checksum,
                        database=copied, profiles=self.profiles, pages=pages,
                        source_parameters=source_parameters)
                with self.assertRaises(ValueError):
                    derive_site_skill_form_recipe_candidate(copied, source_run,
                        profiles=self.profiles, pages=pages, annotation=annotation,
                        source_parameters=source_parameters)
                with closing(sqlite3.connect(copied)) as connection, connection:
                    store.connection.backup(connection)
                    connection.execute("DELETE FROM observations WHERE run_id=? "
                                       "AND kind='skill.recipe_candidate_admission'", (run_id,))
                with self.assertRaises(ValueError):
                    audit_candidate_execution(admission, candidate_directory, run_id=run_id,
                                              **{**sources, 'database': copied})
                _, _, failed_form, failed_state = plans('negative')
                failed_run = await execute('negative', failed_form, failed_state, wrong_state=True)
                with self.assertRaises(ValueError):
                    derive_site_skill_form_recipe_candidate(database, failed_run, profiles=self.profiles,
                        pages=pages, annotation=annotation, source_parameters={'record-query': 'negative'})

                def mutate_candidate():
                    with (candidate_directory / (checksum + '.json')).open('ab') as stream:
                        stream.write(b' ')

                await execute(value, form_plan, state_plan, invocation=invocation,
                              admission=admission, revalidator=revalidator, mutate=mutate_candidate)
            finally:
                await engine.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection)):
            asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
