import asyncio
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_web_https_relay as relay_tests
from aos.contracts import AOSFault, REPO_ROOT, Settings, canonical, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.reusable_decider import ReusableDeciderEngine
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import SiteSkillCaseInputs, SiteSkillFormFieldBinding
from aos.site_skill_form_invocation_audit import audit_site_skill_form_invocation_execution
from aos.site_skill_form_recipe import (
    SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe,
    compile_site_skill_form_recipe_invocation, revalidate_site_skill_form_recipe_invocation)
from aos.site_skill_form_recipe_audit import audit_site_skill_form_recipe_execution
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.storage import TrajectoryStore
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
from aos.web_https_form_transport import form_body, plan_web_https_form


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_FORM_RECIPE_TESTS') == '1',
                     'Explicit isolated recipe Ubuntu/Chromium execution')
class SiteSkillFormRecipeExecutionTests(unittest.TestCase):
    setUp = relay_tests.WebHTTPSRelayTests.setUp

    def _sources(self):
        page_value = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
        page = SitePageDraft.model_validate({**page_value,
            'profile_sha256': self.checksum, 'origin': self.profile.allowed_origins[0],
            'route_template': '/entry', 'outgoing_page_keys': []})
        pages = SiteKnowledgeStore(self.root / 'recipe-pages', self.profiles)
        page_sha256 = digest(page.model_dump())
        pages.register(page, confirm_sha256=page_sha256)
        skill_value = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
        self.step_keys = {operation: operation.replace('_', '-')
                          for operation in SUPPORTED_RECIPE_ORDERS[0]}
        skill = SiteSkillDraft.model_validate({**skill_value,
            'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
            'task_key': self.draft.task.task_key,
            'step_keys': sorted(self.step_keys.values()),
            'precondition_keys': ['baseline-known', 'form-available']})
        skills = SiteSkillStore(self.root / 'recipe-skills', self.profiles, pages)
        skill_sha256 = digest(skill.model_dump())
        skills.register(skill, confirm_sha256=skill_sha256)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
        plan = SiteSkillValidationPlan.model_validate({**fixture['plan'],
            'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
            'skill_sha256': skill_sha256, 'task_key': self.draft.task.task_key})
        inputs = SiteSkillCaseInputs.model_validate({**fixture['case_inputs'],
                                                     'plan_sha256': digest(plan.model_dump())})
        bindings = [SiteSkillFormFieldBinding(parameter_key='record-query', form_field_name='message')]
        base_recipe = {
            'schema_version': '1.0', 'synthetic': True, 'skill_sha256': skill_sha256,
            'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
            'task_key': skill.task_key, 'model_role': 'system1',
            'task_sha256': digest(self.draft.task.model_dump()),
            'field_binding_sha256': digest([binding.model_dump() for binding in bindings]),
            'preconditions': [
                {'precondition_key': 'baseline-known', 'kind': 'declared_state_before',
                 'checked_at_operation': 'read_state_before'},
                {'precondition_key': 'form-available', 'kind': 'entry_form_available',
                 'checked_at_operation': 'fill_form'}],
            'outcome': {'outcome_key': skill.expected_outcome_key,
                        'kind': 'submitted_field_state_transition',
                        'parameter_key': 'record-query', 'form_field_name': 'message',
                        'verified_at_operation': 'read_state_after'},
            'execution_authorized': False, 'collection_authorized': False,
            'reviewed': False, 'activation_authorized': False, 'training_ready': False}
        recipes = tuple(SiteSkillFormRecipe.model_validate_json(canonical({**base_recipe,
            'steps': [{'step_key': self.step_keys[operation], 'operation': operation}
                      for operation in order]})) for order in SUPPORTED_RECIPE_ORDERS)
        return skills, plan, inputs, bindings, recipes

    def test_two_schedules_three_values_and_failed_outcome(self):
        skills, plan, inputs, bindings, recipes = self._sources()
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1 id="outcome">Empty synthetic state</h1></html>'
        self.server.route_bodies = {
            '/entry': (b'<html><title>Recipe form</title><h1>Recipe form</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Receipt</title><h1>Saved locally</h1></html>'}
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'recipe.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        real = os.environ.get('AOS_FORM_RECIPE_REAL_DECIDER_TESTS') == '1'
        engine = (ReusableDeciderEngine(REPO_ROOT / 'models/decider-manifest.json',
                  Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
                  if real else FixtureDecisionEngine())
        results = []

        async def scenario():
            try:
                for recipe, case, mode in (
                        (recipes[0], inputs.cases[0], 'passed'),
                        *((recipes[1], case, 'passed') for case in inputs.cases),
                        (recipes[1], inputs.cases[0], 'wrong_state'),
                        (recipes[1], inputs.cases[0], 'reject_submit'),
                        (recipes[1], inputs.cases[0], 'changed_source')):
                    value = case.parameters['record-query']
                    after = f'<html><h1 id="outcome">{value}</h1></html>'.encode()
                    self.server.paths.clear()
                    self.server.posts.clear()
                    self.server.state_bodies = {False: before,
                                               True: before if mode == 'wrong_state' else after}
                    body = form_body((('message', value),))
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
                    source_arguments = (skills, plan, inputs, case.case_key, self.profiles,
                                        self.draft.task, form_plan, state_plan, bindings, recipe)
                    invocation = compile_site_skill_form_recipe_invocation(*source_arguments)
                    current_recipe = recipe

                    def revalidate():
                        return revalidate_site_skill_form_recipe_invocation(
                            invocation, *source_arguments[:-1], current_recipe)

                    scheduler = DesktopScheduler(controller,
                        Settings(workspace=self.workspace, database=database), engine,
                        browser_manifest=REPO_ROOT / 'models/browser-manifest.json', desktop_browser=True,
                        remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                        remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
                        remote_entry_task=self.draft.task, remote_form_plan=form_plan,
                        remote_form_field_name='message', remote_form_value=value,
                        remote_form_tls_context=self.tls_context, remote_form_state_plan=state_plan,
                        remote_form_skill_invocation=invocation,
                        remote_form_skill_invocation_sha256=digest(invocation),
                        remote_form_skill_revalidator=revalidate)
                    try:
                        owner = controller.state()
                        job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                                 'browser_remote_form')['job_id']
                        previous = None
                        for step in recipe.steps:
                            for _attempt in range(1500):
                                approval = scheduler.status()['approval']
                                if approval is not None and approval['approval_id'] != previous:
                                    break
                                if scheduler.task.done():
                                    self.fail('Recipe terminated before approval: ' + str(scheduler.status()))
                                await asyncio.sleep(.02)
                            else:
                                self.fail('Recipe approval timed out')
                            self.assertEqual(approval['action']['selected_option'], step.operation)
                            accepted = not (mode == 'reject_submit' and step.operation == 'submit_form')
                            if mode == 'changed_source' and step.operation == 'fill_form':
                                current_recipe = recipe.model_copy(update={'task_sha256': '0' * 64})
                                with self.assertRaises(AOSFault):
                                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                                await scheduler.close()
                                break
                            scheduler.respond(approval['approval_id'], approval['action_sha256'], accepted)
                            previous = approval['approval_id']
                            if not accepted or current_recipe is not recipe:
                                break
                        try:
                            await scheduler.task
                        except asyncio.CancelledError:
                            if mode not in {'reject_submit', 'changed_source'}:
                                raise
                        job = store.connection.execute(
                            'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
                        self.assertEqual(self.server.posts,
                            [] if mode in {'reject_submit', 'changed_source'} else [('/submit', body)])
                        audit_arguments = (*source_arguments, invocation, database, job['run_id'])
                        if mode != 'passed':
                            self.assertNotEqual(job['status'], 'succeeded')
                            with self.assertRaises(ValueError):
                                audit_site_skill_form_recipe_execution(*audit_arguments)
                            continue
                        self.assertEqual(job['status'], 'succeeded')
                        report = audit_site_skill_form_recipe_execution(*audit_arguments)
                        self.assertTrue(report['executable_recipe_executed'])
                        self.assertFalse(report['site_outcome_verified'])
                        self.assertEqual(report['symbolic_step_keys'], [step.step_key for step in recipe.steps])
                        self.assertEqual(report['recipe_sha256'], digest(recipe.model_dump(mode='json')))
                        with self.assertRaises((ValueError, TypeError)):
                            audit_site_skill_form_invocation_execution(
                                *source_arguments[:-1], invocation, database, job['run_id'])
                        calls = store.connection.execute("SELECT count(*) FROM model_calls "
                            "WHERE run_id=? AND role='system1' AND status='ok'", (job['run_id'],)).fetchone()[0]
                        self.assertEqual(calls, 6 if real else 0)
                        self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls "
                            "WHERE run_id=? AND role='system2'", (job['run_id'],)).fetchone()[0], 0)
                        results.append(report)
                    finally:
                        await scheduler.close()
            finally:
                if real:
                    await engine.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection)):
            asyncio.run(scenario())
        self.assertEqual(len(results), 4)
        self.assertEqual(len({report['recipe_sha256'] for report in results[1:]}), 1)
        self.assertEqual(len({report['invocation_sha256'] for report in results[1:]}), 3)


if __name__ == '__main__':
    unittest.main()
