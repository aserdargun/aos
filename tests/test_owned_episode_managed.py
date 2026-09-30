from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical, digest
import test_owned_candidate_execution_managed as execution_helpers
import test_owned_skill_release_managed as release_helpers
from test_owned_skill_reuse_managed import owned_subprocess_session, read_isolated_state


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and (os.environ.get('AOS_OWNED_EPISODE_TESTS') == '1'
                          or os.environ.get('AOS_OWNED_ADAPTER_PAIR_TESTS') == '1'),
                     'Explicit real owned episode S1/S2 content review and local export')
class OwnedEpisodeManagedTests(unittest.TestCase):
    def test_live_two_role_candidates_explicit_review_and_private_export(self):
        from playwright.sync_api import expect, sync_playwright

        base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        base.mkdir(mode=0o700)
        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        releases = release_helpers.OwnedSkillReleaseManagedTests()
        complete = False

        def source_post(client, operation, request):
            response = client.post('/api/tasks/owned-form-candidate/' + operation, json=request)
            self.assertEqual(response.status_code, 200)
            return response.json()

        try:
            with owned_subprocess_session(base) as first:
                context, candidate, _job = helper.create_source_candidate(first.client)
                review = releases.evidence_and_review(first.client, helper, context, candidate, 'beta')
                request = {'schema_version': '1.0', 'review_sha256': review, 'parent_release_sha256': None}
                release = source_post(first.client, 'release-preview', request)['release_sha256']
                source_post(first.client, 'release-publish', request | {'confirm_sha256': release})
                request = {'schema_version': '1.0', 'release_sha256': release,
                           'expected_selection_sha256': None, 'operation': 'select'}
                selection = source_post(first.client, 'selection-preview', request)['selection_sha256']
                source_post(first.client, 'selection-commit', request | {'confirm_sha256': selection})
                database = first.source / 'store.sqlite'
            previous = read_isolated_state(base)
            with patch('socket.socket', side_effect=AssertionError('offline_preview')):
                material = local_app.prepare_owned_skill_reuse(previous, release, selection, base=base)
            with owned_subprocess_session(base, previous=previous, material=material) as second, sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    expected_rejections = set()
                    observed_rejections = set()
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def console(message):
                        if message.type not in {'error', 'warning'}:
                            return
                        if (message.location.get('url') == second.origin + '/api/remote-form/repeats'
                                and any(code in message.text for code in ('404 (Not Found)', '409 (Conflict)'))):
                            return
                        if (message.location.get('url') in expected_rejections
                                and '409 (Conflict)' in message.text):
                            observed_rejections.add(message.location['url'])
                            return
                        errors.append(message.text)

                    page.on('console', console)
                    page.goto(second.origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token').fill(second.token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.locator('#task-kind').select_option('browser_remote_form')
                    expect(page.get_by_test_id('episode-opt-in')).not_to_be_checked()
                    page.get_by_test_id('planning-goal').fill('Save message "gamma"')
                    page.get_by_test_id('planning-begin').click()
                    expect(page.get_by_test_id('planning-bind')).to_be_enabled(timeout=180000)
                    self.assertFalse((second.directory / 'owned-episodes').exists())
                    page.get_by_test_id('planning-discard').click()
                    expect(page.get_by_test_id('planning-status')).to_contain_text('cancelled')
                    page.get_by_test_id('planning-goal').fill('Mesaj alanına "delta" kaydet')
                    page.get_by_test_id('episode-opt-in').check()
                    with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan')) as proposed:
                        page.get_by_test_id('planning-begin').click()
                    self.assertEqual(proposed.value.status, 202)
                    episode_id = proposed.value.json()['episode_id']
                    expect(page.get_by_test_id('episode-opt-in')).not_to_be_checked()
                    episode_root = second.directory / 'owned-episodes' / episode_id
                    self.assertTrue((episode_root / 'consent.json').exists())
                    expect(page.get_by_test_id('planning-bind')).to_be_enabled(timeout=180000)
                    with page.expect_response(lambda response: response.url.endswith('/owned-episode/inspect')) as inspected:
                        page.get_by_test_id('episode-inspect').click()
                    self.assertEqual(inspected.value.status, 200)
                    provisional = inspected.value.json()
                    self.assertFalse(provisional['reviewable'])
                    self.assertEqual([item['role'] for item in provisional['candidates']], ['system2'])
                    expect(page.get_by_test_id('episode-accept-system2')).to_have_count(0)
                    page.get_by_test_id('planning-bind').click()
                    page.get_by_test_id('planning-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/start')) as started:
                        page.get_by_test_id('planning-start').click()
                    self.assertEqual(started.value.status, 200)
                    probed = []

                    def live_content(approval):
                        if probed:
                            return False
                        tasks = second.client.get('/api/tasks').json()
                        summary = tasks['owned_episode_learning']
                        self.assertEqual(summary['episode_id'], episode_id)
                        self.assertGreaterEqual(summary['counts']['system1'], 1)
                        self.assertEqual(summary['counts']['system2'], 1)
                        with page.expect_response(lambda response: response.url.endswith('/owned-episode/inspect')) as live:
                            page.get_by_test_id('episode-inspect').click()
                        self.assertEqual(live.value.status, 200)
                        self.assertFalse(live.value.json()['reviewable'])
                        self.assertEqual({item['role'] for item in live.value.json()['candidates']}, {'system1', 'system2'})
                        self.assertGreater(len(page.locator('body').inner_text()), 100)
                        page.get_by_test_id('owned-episode-learning').screenshot(path='/tmp/aos-episode-live-en.png')
                        page.get_by_test_id('episode-status').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-episode-live-viewport-en.png')
                        probed.append(True)
                        return False

                    job, approvals = helper.approve_job(second.client, page=page, before_approval=live_content)
                    self.assertEqual(job['status'], 'succeeded')
                    self.assertEqual(len(approvals), 6)
                    self.assertTrue(probed)
                    releases.wait_idle(second.client)
                    expect(page.get_by_test_id('episode-status')).to_contain_text('reviewable')
                    counts_before = helper.database_counts(database)
                    with page.expect_response(lambda response: response.url.endswith('/owned-episode/inspect')) as ready:
                        page.get_by_test_id('episode-inspect').click()
                    self.assertEqual(ready.value.status, 200)
                    report = ready.value.json()
                    self.assertTrue(report['reviewable'])
                    for candidate_record in report['candidates']:
                        self.assertNotIn('target', candidate_record)
                        self.assertFalse(candidate_record['training_ready'])
                    page.get_by_test_id('episode-accept-system1').click()
                    expect(page.get_by_test_id('episode-export')).to_be_disabled()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.get_by_test_id('episode-accept-system2').click()
                    expect(page.get_by_test_id('episode-export')).to_be_enabled()
                    with page.expect_response(lambda response: response.url.endswith('/owned-episode/export')) as exported:
                        page.get_by_test_id('episode-export').click()
                    self.assertEqual(exported.value.status, 200)
                    export = exported.value.json()
                    expect(page.get_by_test_id('episode-exported')).to_be_visible()
                    self.assertGreaterEqual(export['counts']['system1'], 6)
                    self.assertEqual(export['counts']['system2'], 1)
                    self.assertFalse(export['training_ready'])
                    self.assertEqual(helper.database_counts(database), counts_before)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.get_by_test_id('owned-episode-learning').screenshot(path='/tmp/aos-episode-reviewed-tr.png')
                    page.get_by_test_id('episode-exported').scroll_into_view_if_needed()
                    page.screenshot(path='/tmp/aos-episode-reviewed-viewport-tr.png')
                    saved = episode_root / (export['export_sha256'] + '-manifest.json')
                    manifest = json.loads(saved.read_text())
                    self.assertEqual(digest(manifest), export['export_sha256'])
                    self.assertEqual(manifest['split'], 'development_only')
                    control = second.client.get('/api/state').json()['control']
                    request = {'schema_version': '1.0', 'episode_id': episode_id,
                               'review_receipts': manifest['review_receipts'],
                               'lease_id': control['lease_id'], 'generation': control['generation']}
                    repeated = second.client.post('/api/tasks/owned-episode/export', json=request)
                    self.assertEqual(repeated.status_code, 200)
                    self.assertEqual(repeated.json(), export)
                    conversion = (self.prepare_from_ui(page, second.client, episode_id, export, episode_root)
                                  if os.environ.get('AOS_OWNED_PREPARATION_TESTS') == '1' else None)
                    run_pair = os.environ.get('AOS_OWNED_ADAPTER_PAIR_TESTS') == '1'
                    adaptation = (self.adapt_from_ui(page, second.client, episode_id, conversion, episode_root)
                                  if os.environ.get('AOS_OWNED_ADAPTATION_TESTS') == '1' or run_pair else None)
                    adapter_runtime = None
                    adapter_pair = None
                    if run_pair:
                        if adaptation is None:
                            self.fail('Adapter pair acceptance requires completed adaptation evidence')
                        adapter_pair = self.adapter_pair_from_ui(
                            page, second.client, episode_id, adaptation, episode_root,
                            helper, database)
                    elif os.environ.get('AOS_OWNED_ADAPTER_RUNTIME_TESTS') == '1':
                        if adaptation is None:
                            self.fail('Adapter runtime acceptance requires completed adaptation evidence')
                        adapter_runtime = self.adapter_runtime_from_ui(
                            page, second.client, episode_id, adaptation,
                            first.source / 'owned-form',
                            episode_root,
                            helper, database, context, candidate, review, release, selection,
                            expected_rejections)
                    self.reopen_offline(first.source / 'owned-form', database, second.directory,
                                        episode_id, export['export_sha256'], revoked=False,
                                        conversion=conversion, adaptation=adaptation)
                    stale = second.client.post('/api/tasks/owned-episode/export', json=request | {'generation': control['generation'] + 1})
                    self.assertEqual(stale.status_code, 409)
                    before = saved.read_bytes()
                    with page.expect_response(lambda response: response.url.endswith('/owned-episode/revoke')) as revoked:
                        page.get_by_test_id('episode-revoke-system2').click()
                    self.assertEqual(revoked.value.status, 200)
                    self.assertTrue(revoked.value.json()['revocation_recorded'])
                    expect(page.get_by_test_id('episode-export')).to_be_disabled()
                    denied = second.client.post('/api/tasks/owned-episode/export', json=request)
                    self.assertEqual(denied.status_code, 409)
                    if conversion is not None:
                        rejected = second.client.post('/api/tasks/owned-episode/readiness', json={
                            'schema_version': '1.0', 'episode_id': episode_id, 'conversion_sha256': conversion})
                        self.assertEqual(rejected.status_code, 409)
                    self.assertEqual(saved.read_bytes(), before)
                    if adaptation:
                        denied = second.client.post('/api/tasks/owned-episode/adaptation-inspect', json={
                            'schema_version': '1.0', 'episode_id': episode_id, 'authorization_sha256': adaptation})
                        self.assertEqual(denied.status_code, 409)
                    with closing(sqlite3.connect(database)) as connection:
                        self.assertEqual(connection.execute('SELECT SUM(training_eligible) FROM runs').fetchone()[0], 0)
                    self.assertEqual(errors, [])
                    if adapter_runtime is not None:
                        self.assertEqual(len(expected_rejections), 1)
                        self.assertEqual(observed_rejections, expected_rejections)
                finally:
                    browser.close()
            self.assertEqual(read_isolated_state(base).phase, 'stopped')
            self.reopen_offline(first.source / 'owned-form', database, second.directory,
                                episode_id, export['export_sha256'], revoked=True,
                                conversion=conversion, adaptation=adaptation, pair_sha256=adapter_pair,
                                runtime_execution=adapter_runtime)
            complete = True
        finally:
            if complete:
                shutil.rmtree(base)
            else:
                print('Preserved private episode evidence:', base, flush=True)

    def prepare_from_ui(self, page, client, episode_id, exported, directory):
        from playwright.sync_api import expect

        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/conversion-preview')) as previewed:
            page.get_by_test_id('preparation-preview').click()
        self.assertEqual(previewed.value.status, 200)
        preview = previewed.value.json()
        self.assertEqual(preview['counts'], exported['counts'])
        self.assertFalse(preview['persisted'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})
        expect(page.get_by_test_id('preparation-publish')).to_be_disabled()
        page.get_by_test_id('preparation-confirm').check()
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/convert')) as published:
            page.get_by_test_id('preparation-publish').click()
        self.assertEqual(published.value.status, 200)
        self.assertTrue(published.value.json()['persisted'])
        self.assertTrue(published.value.json()['system1']['tokenizer_start_available'])
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/tokenizer-start')) as started:
            page.get_by_test_id('preparation-tokenizer').click()
        self.assertEqual(started.value.status, 202)
        before_read = time.monotonic()
        self.assertEqual(client.get('/api/state').status_code, 200)
        self.assertLess(time.monotonic() - before_read, 3)
        page.wait_for_function("""() => ['verified','failed','cancelled'].includes(
            document.querySelector('[data-testid="preparation-status"]')?.dataset.state)""", timeout=180000)
        expect(page.get_by_test_id('preparation-status')).to_have_attribute('data-state', 'verified')
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/readiness')) as inspected:
            page.get_by_test_id('preparation-readiness').click()
        self.assertEqual(inspected.value.status, 200)
        readiness = inspected.value.json()
        self.assertEqual(readiness['system1']['tokenizer'], 'verified')
        self.assertEqual(readiness['system2']['trainer'], 'unverified')
        self.assertFalse(readiness['training_ready'])
        self.assertEqual(readiness['split'], 'development_only')
        blocker = 'independent_validation_test_missing'
        self.assertIn(blocker, readiness['blockers'])
        blocker_item = page.locator('[data-testid="episode-preparation"] [data-code="'
                                  + blocker + '"]')
        expect(blocker_item).to_contain_text('Bağımsız doğrulama ve test örnekleri eksik.')
        self.assertNotIn(blocker, page.get_by_test_id('episode-preparation').inner_text())
        preparation_panel = page.get_by_test_id('episode-preparation')
        expect(preparation_panel).to_contain_text('Doğrulandı')
        expect(preparation_panel).to_contain_text('Doğrulanmadı')
        expect(page.get_by_test_id('preparation-status')).to_have_attribute('data-state', 'verified')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        page.get_by_test_id('preparation-readiness').scroll_into_view_if_needed()
        page.screenshot(path='/tmp/aos-preparation-real-tr.png')
        page.get_by_role('button', name='English', exact=True).click()
        expect(blocker_item).to_contain_text('Independent validation and test examples are missing.')
        self.assertNotIn(blocker, page.get_by_test_id('episode-preparation').inner_text())
        expect(preparation_panel).to_contain_text('Verified')
        expect(preparation_panel).to_contain_text('Unverified')
        expect(page.get_by_test_id('preparation-status')).to_have_attribute('data-state', 'verified')
        page.set_viewport_size({'width':1440,'height':1000})
        page.get_by_test_id('preparation-readiness').scroll_into_view_if_needed()
        page.screenshot(path='/tmp/aos-preparation-real-en.png')
        conversion = readiness['conversion_sha256']
        inventory = {path.name: path.read_bytes() for path in directory.iterdir()}
        repeated = client.post('/api/tasks/owned-episode/readiness', json={
            'schema_version': '1.0', 'episode_id': episode_id, 'conversion_sha256': conversion})
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json(), readiness)
        self.assertEqual(inventory, {path.name: path.read_bytes() for path in directory.iterdir()})
        report_files = list(directory.glob(conversion + '-*-tokenizer.json'))
        self.assertEqual(len(report_files), 1)
        probe = json.loads(report_files[0].read_text())['probe']
        self.assertEqual(probe['examples'], exported['counts']['system1'])
        self.assertEqual(probe['variants'], exported['counts']['system1'] * 40)
        self.assertFalse(probe['weights_loaded'])
        self.assertFalse(probe['cuda_initialized'])
        converted_file = directory / (conversion + '-system1.converted.jsonl')
        original = converted_file.read_bytes()
        try:
            converted_file.write_bytes(original + b' ')
            rejected = client.post('/api/tasks/owned-episode/readiness', json={
                'schema_version': '1.0', 'episode_id': episode_id, 'conversion_sha256': conversion})
            self.assertEqual(rejected.status_code, 409)
        finally:
            converted_file.write_bytes(original)
        missing_file = directory / (exported['export_sha256'] + '-system2.jsonl')
        original = missing_file.read_bytes()
        try:
            missing_file.unlink()
            rejected = client.post('/api/tasks/owned-episode/conversion-preview', json={
                'schema_version': '1.0', 'episode_id': episode_id, 'export_sha256': exported['export_sha256']})
            self.assertEqual(rejected.status_code, 409)
            self.assertFalse(missing_file.exists())
        finally:
            missing_file.write_bytes(original)
            missing_file.chmod(0o600)
        print('Real episode tokenizer:', probe['examples'], 'examples,', probe['variants'],
              'variants, max tokens', probe['max_input_tokens'], flush=True)
        return conversion

    def adapt_from_ui(self, page, client, episode_id, conversion, directory):
        from playwright.sync_api import expect

        self.assertIsNotNone(conversion)
        before = {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob('*') if path.is_file()}
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/adaptation-preview')) as previewed:
            page.get_by_test_id('adaptation-preview').click()
        self.assertEqual(previewed.value.status, 200)
        preview = previewed.value.json()
        authorization = preview['authorization_sha256']
        self.assertEqual(preview['authorization']['development_decisions'], 6)
        self.assertEqual(before, {str(path.relative_to(directory)): path.read_bytes()
                                 for path in directory.rglob('*') if path.is_file()})
        expect(page.get_by_test_id('adaptation-start')).to_be_disabled()
        page.get_by_test_id('adaptation-rights').check()
        expect(page.get_by_test_id('adaptation-start')).to_be_disabled()
        page.get_by_test_id('adaptation-consent').check()
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/adaptation-start')) as started:
            page.get_by_test_id('adaptation-start').click()
        self.assertEqual(started.value.status, 202)
        self.assertEqual(started.value.json()['authorization_sha256'], authorization)
        start_payload = started.value.request.post_data_json
        self.assertEqual(set(start_payload), {'schema_version', 'episode_id', 'conversion_sha256', 'lease_id',
                                             'generation', 'confirm_sha256', 'rights_redaction_reviewed',
                                             'experimental_training_authorized'})
        self.assertEqual(client.post('/api/tasks/owned-episode/adaptation-start', json=start_payload).status_code, 409)
        status = client.get('/api/tasks').json()
        self.assertTrue(status['reserved'])
        self.assertIn(status['owned_episode_learning']['adaptation']['state'], {'training', 'evaluating'})
        self.assertEqual(client.get('/api/state').status_code, 200)
        page.wait_for_function("""() => ['verified','failed','cancelled'].includes(
            document.querySelector('[data-testid="adaptation-status"]')?.dataset.state)""", timeout=420000)
        expect(page.get_by_test_id('adaptation-status')).to_have_attribute('data-state', 'verified')
        with page.expect_response(lambda response: response.url.endswith('/owned-episode/adaptation-inspect')) as inspected:
            page.get_by_test_id('adaptation-inspect').click()
        self.assertEqual(inspected.value.status, 200)
        report = inspected.value.json()
        self.assertEqual(client.post('/api/tasks/owned-episode/adaptation-start', json=start_payload).status_code, 409)
        self.assertEqual(report['evaluation'], 'resubstitution')
        self.assertFalse(report['training_ready'])
        self.assertFalse(report['promotion_authorized'])
        self.assertEqual(report['train']['development_decisions'], 6)
        self.assertEqual(report['replay']['development_decisions'], 6)
        self.assertGreater(report['train']['gradient_norm'], 0)
        self.assertTrue(report['replay']['candidate_loaded'])
        self.assertTrue(report['train']['base_parameters_unchanged'])
        panel = page.get_by_test_id('episode-adaptation')
        expect(panel).to_contain_text('Same-cohort comparison; not independent validation.')
        page.get_by_test_id('adaptation-report').scroll_into_view_if_needed()
        page.screenshot(path='/tmp/aos-adaptation-real-en.png')
        page.get_by_role('button', name='Türkçe', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(panel).to_contain_text('Aynı grupta karşılaştırma; bağımsız doğrulama değildir.')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        page.get_by_test_id('adaptation-report').scroll_into_view_if_needed()
        page.screenshot(path='/tmp/aos-adaptation-real-tr.png')
        artifact = directory / ('adapter-' + authorization) / 'candidate.bin'
        original = artifact.read_bytes()
        request = {'schema_version': '1.0', 'episode_id': episode_id, 'authorization_sha256': authorization}
        try:
            artifact.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            self.assertEqual(client.post('/api/tasks/owned-episode/adaptation-inspect', json=request).status_code, 409)
        finally:
            artifact.write_bytes(original)
        self.assertEqual(client.post('/api/tasks/owned-episode/adaptation-inspect', json=request).json(), report)
        candidate = next(directory.glob('candidate-*.json'))
        original_candidate = candidate.read_bytes()
        try:
            candidate.unlink()
            self.assertEqual(client.post('/api/tasks/owned-episode/adaptation-inspect', json=request).status_code, 409)
            self.assertFalse(candidate.exists())
        finally:
            candidate.write_bytes(original_candidate)
            candidate.chmod(0o600)
        print('Real owned adaptation:', json.dumps({
            'examples': report['train']['development_decisions'], 'gradient_norm': report['train']['gradient_norm'],
            'base_nll': report['replay']['base_development_nll'], 'adapter_nll': report['replay']['candidate_development_nll'],
            'base_accuracy': report['replay']['base_development_accuracy'],
            'adapter_accuracy': report['replay']['candidate_development_accuracy'],
            'peak_vram_allocated_bytes': report['train']['peak_vram_allocated_bytes']}), flush=True)
        return authorization

    def adapter_runtime_from_ui(self, page, client, episode_id, authorization,
                                session_directory, directory, helper, database, source_context,
                                candidate_sha256, review_sha256, release_sha256,
                                selection_sha256, expected_rejections):
        from playwright.sync_api import expect

        control = client.get('/api/state').json()['control']
        files_before = {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob('*') if path.is_file()}
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/runtime-preview')) as previewed:
            page.get_by_test_id('adapter-runtime-preview').click()
        self.assertEqual(previewed.value.status, 200)
        preview_payload = previewed.value.json()
        self.assertEqual(preview_payload['preview']['schema_version'], '1.5')
        self.assertFalse(preview_payload['started'])
        self.assertEqual(preview_payload['admission']['case_key'], 'adapter-canary')
        self.assertEqual(files_before, {str(path.relative_to(directory)): path.read_bytes()
                                        for path in directory.rglob('*') if path.is_file()})
        page.get_by_test_id('adapter-runtime-consent').check()
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/runtime-start')) as started:
            page.get_by_test_id('adapter-runtime-start').click()
        self.assertEqual(started.value.status, 202)
        start_response = started.value.json()
        self.assertEqual(start_response['schema_version'], '1.5')
        self.assertEqual(start_response['adapter_deployment_id'],
                         preview_payload['admission']['adapter_deployment_id'])
        start_request = started.value.request.post_data_json
        self.assertTrue(start_request['experimental_runtime_authorized'])
        self.assertEqual(client.post('/api/tasks/owned-episode/runtime-start',
                                     json=start_request).status_code, 409)
        job, approvals = helper.approve_job(
            client, page=page, approve_label='Onayla')
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(job['job_id'], start_response['job_id'])
        self.assertEqual(len(approvals), 6)
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/runtime-audit')) as audited:
            page.get_by_test_id('adapter-runtime-audit').click()
        self.assertEqual(audited.value.status, 200)
        report = audited.value.json()
        self.assertTrue(report['available'], report)
        self.assertEqual(report['schema_version'], '1.5')
        self.assertTrue(report['adapter_admission_verified'])
        expect(page.get_by_test_id('adapter-runtime-verified')).to_be_visible()
        runtime_execution_sha256 = report['candidate_execution_sha256']
        runtime_run_id = report['run_id']
        deployment_id = preview_payload['admission']['adapter_deployment_id']
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_test_id('owned-adapter-runtime').scroll_into_view_if_needed()
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
        page.screenshot(path='/tmp/aos-adapter-runtime-real-tr.png')
        page.get_by_role('button', name='English', exact=True).click()
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.get_by_test_id('owned-adapter-runtime').scroll_into_view_if_needed()
        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
        page.screenshot(path='/tmp/aos-adapter-runtime-real-en.png')
        page.get_by_role('button', name='Türkçe', exact=True).click()
        page.get_by_role('button', name='Türkçe', exact=True).click()
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            job_row = connection.execute(
                'SELECT job_id,run_id,status FROM desktop_tasks WHERE run_id=?',
                (runtime_run_id,)).fetchone()
            self.assertIsNotNone(job_row)
            self.assertEqual(job_row['status'], 'succeeded')
            calls = connection.execute(
                'SELECT deployment_id,role,status FROM model_calls WHERE run_id=?',
                (runtime_run_id,)).fetchall()
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(row['deployment_id'] == deployment_id
                                and row['role'] == 'system1' and row['status'] == 'ok'
                                for row in calls))
            from aos.owned_adapter_identity import verify_adapter_run
            from aos.owned_form_candidate_execution import load_candidate_execution_bundle

            loaded_bundle, _ = load_candidate_execution_bundle(
                session_directory / 'candidate-execution-bundles', runtime_execution_sha256)
            self.assertTrue(verify_adapter_run(
                connection, runtime_run_id, loaded_bundle['adapter-admission']))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                (runtime_run_id,)).fetchone()[0], 1)

        task_status = client.get('/api/tasks').json()
        reuse_sha256 = task_status['owned_form_invocation']['reuse_admission_sha256']
        base_request = {
            'schema_version': '1.3', 'candidate_sha256': candidate_sha256,
            'source_run_ref': source_context['source_run_ref'],
            'source_invocation_sha256': source_context['invocation_sha256'],
            'case_key': 'base-after-adapter', 'development_value': 'base only',
            'review_sha256': review_sha256, 'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'reuse_admission_sha256': reuse_sha256,
        }
        base_preview = client.post(
            '/api/tasks/owned-form-candidate/execution-preview', json=base_request)
        self.assertEqual(base_preview.status_code, 200, base_preview.text)
        self.assertEqual(base_preview.json()['schema_version'], '1.3')
        current_control = client.get('/api/state').json()['control']
        base_start_request = base_request | {
            'preview_sha256': base_preview.json()['preview_sha256'],
            'confirm_sha256': base_preview.json()['preview_sha256'],
            'lease_id': current_control['lease_id'],
            'generation': current_control['generation'],
        }
        base_started = client.post(
            '/api/tasks/owned-form-candidate/execution-start', json=base_start_request)
        self.assertEqual(base_started.status_code, 200, base_started.text)
        self.assertEqual(base_started.json()['schema_version'], '1.3')
        self.assertNotIn('adapter_admission_sha256', base_started.json())
        base_job, base_approvals = helper.approve_job(
            client, page=page, approve_label='Onayla')
        self.assertEqual(base_job['status'], 'succeeded', base_job)
        self.assertEqual(base_job['job_id'], base_started.json()['job_id'])
        self.assertEqual(len(base_approvals), 6)
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        base_run_id = base_job['run_id']
        self.assertIsNotNone(base_run_id)
        with closing(sqlite3.connect(database)) as connection:
            base_calls = connection.execute(
                'SELECT deployment_id FROM model_calls WHERE run_id=?', (base_run_id,)).fetchall()
        self.assertEqual(len(base_calls), 6)
        base_deployment_id = preview_payload['admission']['runtime_binding']['base_deployment_id']
        self.assertTrue(all(row[0] == base_deployment_id for row in base_calls))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                (base_run_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM observations WHERE run_id=? AND kind='model.owned_adapter_admission'",
                (base_run_id,)).fetchone()[0], 0)

        tamper_case = 'adapter-tamper'
        tamper_value = 'tamper message'
        fresh_control = client.get('/api/state').json()['control']
        tamper_preview = client.post('/api/tasks/owned-episode/runtime-preview', json={
            'schema_version': '1.0', 'episode_id': episode_id,
            'authorization_sha256': authorization, 'case_key': tamper_case,
            'development_value': tamper_value, 'lease_id': fresh_control['lease_id'],
            'generation': fresh_control['generation']})
        self.assertEqual(tamper_preview.status_code, 200, tamper_preview.text)
        tamper_start = client.post('/api/tasks/owned-episode/runtime-start', json={
            'schema_version': '1.0', 'episode_id': episode_id,
            'authorization_sha256': authorization, 'case_key': tamper_case,
            'development_value': tamper_value, 'lease_id': fresh_control['lease_id'],
            'generation': fresh_control['generation'],
            'confirm_sha256': tamper_preview.json()['preview']['preview_sha256'],
            'experimental_runtime_authorized': True})
        self.assertEqual(tamper_start.status_code, 202, tamper_start.text)
        artifact = directory / ('adapter-' + authorization) / 'candidate.bin'
        original_artifact = artifact.read_bytes()
        mutated = []
        mutated_tools = []

        def mutate_before_fill(approval):
            if (not mutated and approval['action']['tool'] == 'browser.form.fill'):
                expected_rejections.add(page.url.split('/ui/')[0]
                                        + '/api/approvals/' + approval['approval_id'])
                artifact.write_bytes(original_artifact[:-1] + bytes([original_artifact[-1] ^ 1]))
                mutated.append(True)
                mutated_tools.append(approval['action']['tool'])
                return True
            return False

        try:
            rejected_job, rejected_approvals = helper.approve_job(
                client, page=page, before_approval=mutate_before_fill,
                approve_label='Onayla')
            self.assertEqual(rejected_job['status'], 'failed', rejected_job)
            self.assertEqual(rejected_job['job_id'], tamper_start.json()['job_id'])
            self.assertTrue(mutated)
            self.assertEqual(mutated_tools, ['browser.form.fill'])
            self.assertEqual(len(rejected_approvals), 3)
            rejected_run_id = rejected_job.get('run_id')
            self.assertIsNotNone(rejected_run_id)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                    (rejected_run_id,)).fetchone()[0], 0)
        finally:
            artifact.write_bytes(original_artifact)
            artifact.chmod(0o600)
        print('Real owned adapter runtime:', json.dumps({
            'adapter_calls': 6, 'adapter_deployment_id': deployment_id,
            'base_calls': len(base_calls), 'tamper_post_actions': 0}), flush=True)
        return runtime_execution_sha256

    def adapter_pair_from_ui(self, page, client, episode_id, authorization, directory,
                             helper, database):
        from playwright.sync_api import expect

        with closing(sqlite3.connect(database)) as connection:
            pointers_before = connection.execute('SELECT * FROM active_deployments ORDER BY deployment_id').fetchall()
            eligible_before = connection.execute('SELECT SUM(training_eligible) FROM runs').fetchone()[0]
        panel = page.get_by_test_id('owned-adapter-evaluation')
        page.get_by_test_id('pair-case').fill('episode-pair')
        page.get_by_test_id('pair-value').fill('same paired value')
        files_before = {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob('*') if path.is_file()}
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-preview')) as previewed:
            page.get_by_test_id('pair-preview').click()
        self.assertEqual(previewed.value.status, 200, previewed.value.text())
        preview = previewed.value.json()
        pair_sha256 = preview['pair_sha256']
        self.assertFalse(preview['committed'])
        self.assertFalse(preview['started'])
        record = preview['record']
        self.assertEqual(record['arguments']['case_key'], 'episode-pair')
        self.assertEqual(record['arguments']['development_value'], 'same paired value')
        self.assertEqual(record['adapter_admission']['development_value_sha256'],
                         digest({'value': record['arguments']['development_value']}))
        self.assertEqual(record['scope'], 'development_only')
        self.assertFalse(record['promotion_authorized'])
        self.assertEqual(record['independent_held_out'], 0)
        self.assertTrue(record['cold_worker_each_arm'])
        for key in ('candidate_sha256', 'source_run_ref', 'source_invocation_sha256',
                    'invocation_sha256', 'case_key', 'parameter_variant_sha256',
                    'form_plan_sha256', 'state_plan_sha256', 'review_sha256',
                    'release_sha256', 'selection_sha256', 'reuse_admission_sha256', 'steps'):
            self.assertEqual(record['base_preview'][key], record['adapter_preview'][key], key)
        self.assertNotEqual(record['base_deployment_id'], record['adapter_deployment_id'])
        self.assertEqual(files_before, {str(path.relative_to(directory)): path.read_bytes()
                                        for path in directory.rglob('*') if path.is_file()})
        expect(page.get_by_test_id('pair-commit')).to_be_disabled()
        page.get_by_test_id('pair-consent').check()
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-commit')) as committed:
            page.get_by_test_id('pair-commit').click()
        self.assertEqual(committed.value.status, 200, committed.value.text())
        committed_pair = committed.value.json()
        self.assertTrue(committed_pair['committed'])
        self.assertEqual(committed_pair['pair_sha256'], pair_sha256)
        self.assertEqual((directory / f'pair-{pair_sha256}.json').read_text(), canonical(record))
        page.get_by_test_id('pair-consent').check()
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-start')) as base_started:
            page.get_by_test_id('pair-start-base').click()
        self.assertEqual(base_started.value.status, 202, base_started.value.text())
        base_start = base_started.value.json()
        self.assertEqual(base_start['arm'], 'base')
        self.assertTrue(base_start['accepted'])
        self.assertEqual(base_start['pair_sha256'], pair_sha256)
        base_job, base_approvals = helper.approve_job(client, page=page, approve_label='Onayla')
        self.assertEqual(base_job['status'], 'succeeded', base_job)
        self.assertEqual(base_job['job_id'], base_start['job_id'])
        self.assertEqual(len(base_approvals), 6)
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-report')) as base_report_response:
            page.get_by_test_id('pair-report').click()
        self.assertEqual(base_report_response.value.status, 200)
        base_report = base_report_response.value.json()
        self.assertFalse(base_report['verified'])
        self.assertEqual(base_report['arms']['base']['status'], 'verified')
        self.assertEqual(base_report['arms']['adapter']['status'], 'not_started')
        expect(page.get_by_test_id('pair-start-adapter')).to_be_visible()
        page.get_by_test_id('pair-consent').check()
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-start')) as adapter_started:
            page.get_by_test_id('pair-start-adapter').click()
        self.assertEqual(adapter_started.value.status, 202, adapter_started.value.text())
        adapter_start = adapter_started.value.json()
        self.assertEqual(adapter_start['arm'], 'adapter')
        self.assertTrue(adapter_start['accepted'])
        self.assertEqual(adapter_start['pair_sha256'], pair_sha256)
        adapter_job, adapter_approvals = helper.approve_job(client, page=page, approve_label='Onayla')
        self.assertEqual(adapter_job['status'], 'succeeded', adapter_job)
        self.assertEqual(adapter_job['job_id'], adapter_start['job_id'])
        self.assertEqual(len(adapter_approvals), 6)
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        with page.expect_response(lambda response: response.url.endswith(
                '/owned-episode/pair-report')) as final_report_response:
            page.get_by_test_id('pair-report').click()
        self.assertEqual(final_report_response.value.status, 200)
        report = final_report_response.value.json()
        self.assertTrue(report['verified'], report)
        self.assertEqual(report['pair_sha256'], pair_sha256)
        self.assertEqual(report['scope'], 'development_only')
        self.assertEqual(report['independent_held_out'], 0)
        self.assertFalse(report['promotion_authorized'])
        self.assertFalse(report['runtime_reuse_authorized'])
        self.assertFalse(report['comparison']['quality_superiority_verified'])
        for arm in ('base', 'adapter'):
            self.assertEqual(report['arms'][arm]['status'], 'verified')
        for metrics in (report['comparison']['base'], report['comparison']['adapter']):
            self.assertEqual(metrics['call_count'], 6)
            self.assertEqual(metrics['action_count'], 7)
            self.assertEqual(metrics['consumed_approval_count'], 6)
            self.assertGreaterEqual(metrics['call_latency_sum_ms'], 0)
            self.assertGreaterEqual(metrics['approval_window_sum_ms'], 0)
        expect(page.get_by_test_id('pair-verified')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        panel.scroll_into_view_if_needed()
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
        page.screenshot(path='/tmp/aos-adapter-pair-real-tr.png')
        page.get_by_role('button', name='English', exact=True).click()
        page.set_viewport_size({'width': 1440, 'height': 1000})
        panel.scroll_into_view_if_needed()
        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
        page.screenshot(path='/tmp/aos-adapter-pair-real-en.png')
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            for arm, job in (('base', base_job), ('adapter', adapter_job)):
                run_id = job['run_id']
                self.assertIsNotNone(run_id)
                calls = connection.execute(
                    'SELECT deployment_id,role,status FROM model_calls WHERE run_id=?', (run_id,)).fetchall()
                self.assertEqual(len(calls), 6)
                expected_deployment = record[arm + '_deployment_id']
                self.assertTrue(all(call['deployment_id'] == expected_deployment
                                    and call['role'] == 'system1' and call['status'] == 'ok'
                                    for call in calls))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'", (run_id,)
                ).fetchone()[0], 1)
                markers = connection.execute(
                    "SELECT payload_json FROM observations WHERE run_id=? AND kind='model.owned_adapter_evaluation'",
                    (run_id,)).fetchall()
                self.assertEqual(len(markers), 1)
                marker = json.loads(markers[0]['payload_json'])
                self.assertEqual(marker['pair_sha256'], pair_sha256)
                self.assertEqual(marker['arm'], arm)
                self.assertTrue(marker['cold_worker'])
                adapter_proofs = connection.execute(
                    "SELECT COUNT(*) FROM observations WHERE run_id=? AND kind='model.owned_adapter_inference'",
                    (run_id,)).fetchone()[0]
                self.assertEqual(adapter_proofs, 0 if arm == 'base' else 6)
            self.assertEqual(connection.execute('SELECT SUM(training_eligible) FROM runs').fetchone()[0], eligible_before)
            self.assertEqual(connection.execute('SELECT * FROM active_deployments ORDER BY deployment_id').fetchall(),
                             pointers_before)
        self.assertEqual(client.get('/api/state').json()['control']['status'], 'running')
        print('Real owned adapter pair:', json.dumps({
            'case_key': record['arguments']['case_key'],
            'value_sha256': digest({'value': record['arguments']['development_value']}),
            'base_calls': 6, 'adapter_calls': 6, 'base_approvals': 6, 'adapter_approvals': 6,
            'base_submit_count': 1, 'adapter_submit_count': 1,
            'quality_superiority_verified': False}), flush=True)
        return pair_sha256

    def reopen_offline(self, source, database, directory, episode_id, export_sha256, *, revoked,
                       conversion=None, adaptation=None, runtime_execution=None, pair_sha256=None):
        result = subprocess.run([sys.executable, '-c', '''
import asyncio,json,sqlite3,subprocess,sys
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
from aos.contracts import REPO_ROOT,digest
from aos.decision import DeciderEngine
from aos.desktop_tasks import DesktopScheduler
from aos.owned_episode_service import OwnedEpisodeLearning
from aos.site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from aos.web_application import WebApplicationProfiles
from aos.site_knowledge import SiteKnowledgeStore
source,database,directory=map(Path,sys.argv[1:4])
episode_id,export_sha256=sys.argv[4:6]
revoked=sys.argv[6]=='true'
conversion=sys.argv[7]
adaptation=sys.argv[8]
runtime_execution=sys.argv[9]
pair_sha256=sys.argv[10]
manifest=json.loads((source/'manifest.json').read_text())
profiles=WebApplicationProfiles(source/'profiles')
session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,manifest_sha256=digest(manifest),
 candidate_directory=source/'site-skill-recipe-candidates',database=database,
 profiles=profiles,pages=SiteKnowledgeStore(source/'site-knowledge',profiles))
with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
 connection.row_factory=sqlite3.Row
 scheduler=DesktopScheduler.__new__(DesktopScheduler)
 scheduler.task=None
 scheduler.job_id=None
 scheduler.owned_skill_planning=None
 scheduler.sequences=SimpleNamespace(reserved=False)
 scheduler.store=SimpleNamespace(connection=connection)
 scheduler.settings=SimpleNamespace(database=database)
 scheduler.remote_form_owned_candidate_session=session
 scheduler.remote_form_owned_manifest=manifest
 scheduler._owned_candidate_execution_history={}
 scheduler.engine=DeciderEngine(REPO_ROOT/'models/decider-manifest.json',Path.home()/'.venv/bin/python')
 learning=OwnedEpisodeLearning(scheduler,directory/'owned-episodes')
 episode=directory/'owned-episodes'/episode_id
 before={str(path.relative_to(episode)):path.read_bytes() for path in episode.rglob('*') if path.is_file()}
 with patch('socket.socket',side_effect=AssertionError('network_forbidden')), \
     patch('subprocess.Popen',side_effect=AssertionError('worker_spawn_forbidden')), \
     patch('asyncio.create_subprocess_exec',side_effect=AssertionError('worker_spawn_forbidden')):
  report=learning.inspect(episode_id,read_only=True)
  assert report['reviewable'] is True
  assert report['reviews']['system2']['revoked'] is revoked
  receipts={role:value['receipt_sha256'] for role,value in report['reviews'].items()}
  if revoked:
   try:
    learning.export(episode_id,receipts)
   except ValueError:
    pass
   else:
    raise AssertionError('revoked_export_allowed')
  else:
   exported=learning.export(episode_id,receipts)
   assert exported['export_sha256']==export_sha256
  if conversion:
   if revoked:
    try:
     learning.readiness(episode_id,conversion)
    except ValueError:
     pass
    else:
     raise AssertionError('revoked_conversion_allowed')
   else:
    readiness=learning.readiness(episode_id,conversion)
    assert readiness['system1']['tokenizer']=='verified'
    assert readiness['training_ready'] is False
  if adaptation:
   if revoked:
    try:
     learning.adaptation.inspect(episode_id,adaptation)
    except ValueError:
     pass
    else:
     raise AssertionError('revoked_adaptation_allowed')
   else:
    checked=learning.adaptation.inspect(episode_id,adaptation)
    assert checked['replay']['candidate_loaded'] is True
    assert checked['authorization']['development_decisions']==6
    assert checked['training_ready'] is False and checked['promotion_authorized'] is False
  if runtime_execution:
   audit=learning.runtime_audit(
    episode_id=episode_id,
    authorization_sha256=json.loads((source/'candidate-execution-bundles'/runtime_execution/'adapter-admission.json').read_text())['authorization_sha256'],
    candidate_execution_sha256=runtime_execution)
   assert audit['available'] is True and audit['schema_version']=='1.5', audit
   assert audit['adapter_admission_verified'] is True
   assert audit['verification_scope']=='historical_execution'
   assert audit['runtime_reuse_authorized'] is False
   expected_source='unavailable' if revoked else 'available'
   assert audit['current_source_status']==expected_source, audit
   from aos.owned_form_candidate_execution import load_candidate_execution_bundle
   bundle,_=load_candidate_execution_bundle(source/'candidate-execution-bundles',runtime_execution)
   run_id=bundle['completion']['run_id']
   def audit_path(path):
    local_profiles=WebApplicationProfiles(source/'profiles')
    local_session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,
     manifest_sha256=digest(manifest),candidate_directory=source/'site-skill-recipe-candidates',
     database=path,profiles=local_profiles,pages=SiteKnowledgeStore(source/'site-knowledge',local_profiles))
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as local_connection:
     local_connection.row_factory=sqlite3.Row
     local_scheduler=DesktopScheduler.__new__(DesktopScheduler)
     local_scheduler.task=None
     local_scheduler.job_id=None
     local_scheduler.owned_skill_planning=None
     local_scheduler.sequences=SimpleNamespace(reserved=False)
     local_scheduler.store=SimpleNamespace(connection=local_connection)
     local_scheduler.settings=SimpleNamespace(database=path)
     local_scheduler.remote_form_owned_candidate_session=local_session
     local_scheduler.remote_form_owned_manifest=manifest
     local_scheduler._owned_candidate_execution_history={}
     return local_scheduler.audit_owned_form_candidate_execution(runtime_execution)
   for mutation in ('missing_admission','mixed_call'):
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
     copy_path=Path(temporary)/'store.sqlite'
     baseline_path=Path(temporary)/'baseline.sqlite'
     with closing(sqlite3.connect(database)) as source_connection, closing(sqlite3.connect(baseline_path)) as baseline_connection:
      source_connection.backup(baseline_connection)
     with closing(sqlite3.connect(baseline_path)) as baseline_connection, closing(sqlite3.connect(copy_path)) as copy_connection:
      baseline_connection.backup(copy_connection)
     baseline=audit_path(copy_path)
     assert baseline['available'] is True and baseline['adapter_admission_verified'] is True, baseline
     with closing(sqlite3.connect(copy_path)) as copy_connection:
      triggers=list(copy_connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
      for trigger_name,_trigger_sql in triggers:
       copy_connection.execute('DROP TRIGGER "'+trigger_name.replace('"','""')+'"')
      if mutation=='missing_admission':
       changed=copy_connection.execute("DELETE FROM observations WHERE run_id=? AND kind='model.owned_adapter_admission'",(run_id,))
       assert changed.rowcount==1
      else:
       base_id=bundle['adapter-admission']['runtime_binding']['base_deployment_id']
       changed=copy_connection.execute('UPDATE model_calls SET deployment_id=? WHERE run_id=? AND call_id=(SELECT call_id FROM model_calls WHERE run_id=? LIMIT 1)',(base_id,run_id,run_id))
       assert changed.rowcount==1
      for _trigger_name,trigger_sql in triggers:
       copy_connection.execute(trigger_sql)
      copy_connection.commit()
     rejected=audit_path(copy_path)
     assert rejected['available'] is False, (mutation,rejected)
     with closing(sqlite3.connect(baseline_path)) as baseline_connection, closing(sqlite3.connect(copy_path)) as copy_connection:
      baseline_connection.backup(copy_connection)
     restored=audit_path(copy_path)
     assert restored['available'] is True and restored['adapter_admission_verified'] is True, restored
  if pair_sha256:
   pair_report=learning.adapter_evaluation.report(episode_id,pair_sha256)
   assert pair_report['verified'] is True, pair_report
   assert pair_report['comparison']['quality_superiority_verified'] is False
   pair_record=json.loads((episode/('pair-'+pair_sha256+'.json')).read_text())
   pair_run_ids={arm:pair_report['arms'][arm]['run_id'] for arm in ('base','adapter')}
   def pair_report_path(path):
    local_profiles=WebApplicationProfiles(source/'profiles')
    local_session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,
     manifest_sha256=digest(manifest),candidate_directory=source/'site-skill-recipe-candidates',
     database=path,profiles=local_profiles,pages=SiteKnowledgeStore(source/'site-knowledge',local_profiles))
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as local_connection:
     local_connection.row_factory=sqlite3.Row
     local_scheduler=DesktopScheduler.__new__(DesktopScheduler)
     local_scheduler.task=None
     local_scheduler.job_id=None
     local_scheduler.owned_skill_planning=None
     local_scheduler.sequences=SimpleNamespace(reserved=False)
     local_scheduler.store=SimpleNamespace(connection=local_connection)
     local_scheduler.settings=SimpleNamespace(database=path)
     local_scheduler.remote_form_owned_candidate_session=local_session
     local_scheduler.remote_form_owned_manifest=manifest
     local_scheduler._owned_candidate_execution_history={}
     local_scheduler.engine=DeciderEngine(REPO_ROOT/'models/decider-manifest.json',Path.home()/'.venv/bin/python')
     local_learning=OwnedEpisodeLearning(local_scheduler,directory/'owned-episodes')
     return local_learning.adapter_evaluation.report(episode_id,pair_sha256)
   import tempfile
   for mutation in ('missing_pair_marker','mixed_deployment'):
    with tempfile.TemporaryDirectory() as temporary:
     copy_path=Path(temporary)/'store.sqlite'
     baseline_path=Path(temporary)/'baseline.sqlite'
     with closing(sqlite3.connect(database)) as source_connection, closing(sqlite3.connect(baseline_path)) as baseline_connection:
      source_connection.backup(baseline_connection)
     with closing(sqlite3.connect(baseline_path)) as baseline_connection, closing(sqlite3.connect(copy_path)) as copy_connection:
      baseline_connection.backup(copy_connection)
     cloned=pair_report_path(copy_path)
     assert cloned['verified'] is True, cloned
     with closing(sqlite3.connect(copy_path)) as copy_connection:
      triggers=list(copy_connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
      for trigger_name,_trigger_sql in triggers:
       copy_connection.execute('DROP TRIGGER "'+trigger_name.replace('"','""')+'"')
      if mutation=='missing_pair_marker':
       changed=copy_connection.execute("DELETE FROM observations WHERE run_id=? AND kind='model.owned_adapter_evaluation'",
                                       (pair_run_ids['adapter'],))
       assert changed.rowcount==1
      else:
       changed=copy_connection.execute('UPDATE model_calls SET deployment_id=? WHERE run_id=? AND call_id=(SELECT call_id FROM model_calls WHERE run_id=? LIMIT 1)',
          (pair_record['base_deployment_id'],pair_run_ids['adapter'],pair_run_ids['adapter']))
       assert changed.rowcount==1
      for _trigger_name,trigger_sql in triggers:
       copy_connection.execute(trigger_sql)
      copy_connection.commit()
     try:
      rejected=pair_report_path(copy_path)
     except (ValueError, KeyError, TypeError):
      rejected=None
     assert rejected is None or rejected['verified'] is False, (mutation,rejected)
     with closing(sqlite3.connect(baseline_path)) as baseline_connection, closing(sqlite3.connect(copy_path)) as copy_connection:
      baseline_connection.backup(copy_connection)
     restored=pair_report_path(copy_path)
     assert restored['verified'] is True, restored
  assert before=={str(path.relative_to(episode)):path.read_bytes() for path in episode.rglob('*') if path.is_file()}
print('offline episode reinspection and export boundary verified')
''', str(source), str(database), str(directory), episode_id, export_sha256,
            'true' if revoked else 'false', conversion or '', adaptation or '',
            runtime_execution or '', pair_sha256 or ''], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
