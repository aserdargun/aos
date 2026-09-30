import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires built UI and Docker for a synthetic polling-response fixture')
class TaskPollingUITests(unittest.TestCase):
    def test_active_task_polling_does_not_repeat_heavy_inspection(self):
        self.check_polling('running')

    def test_verified_task_is_not_reported_as_still_running_during_cleanup(self):
        self.check_polling('succeeded')

    def check_polling(self, initial_status):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='task-polling-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                response = client.get('/api/tasks').json()
                counts = {'tasks': 0, 'resources': 0, 'overview': 0, 'state': 0}
                writes, errors = [], []
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def requested(request):
                        if request.method == 'POST':
                            writes.append(request.url)
                        for name in ('resources', 'overview', 'state'):
                            if request.url == origin + '/api/' + name:
                                counts[name] += 1

                    def synthetic_tasks(route):
                        counts['tasks'] += 1
                        running = counts['tasks'] < 3
                        route.fulfill(json={**response, 'busy': running, 'reserved': running,
                                            'jobs': [{'job_id': 'synthetic-polling-job', 'run_id': None,
                                                      'kind': 'hello', 'real_model': 0, 'runtime_id': None,
                                                      'status': initial_status if running else 'succeeded'}]})

                    page.on('request', requested)
                    page.route('**/api/tasks', synthetic_tasks)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    if initial_status == 'succeeded':
                        expect(page.get_by_test_id('workflow-notice')).to_contain_text('resources are being released')
                    expect(page.get_by_test_id('workflow-notice')).to_have_count(0, timeout=2500)
                    expect(page.get_by_test_id('task-row')).to_contain_text('succeeded', timeout=2500)
                    self.assertGreaterEqual(counts['tasks'], 3)
                    self.assertEqual({name: counts[name] for name in ('resources', 'overview', 'state')},
                                     {'resources': 1, 'overview': 1, 'state': 1})
                    self.assertEqual(writes, [origin + '/api/login'])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
