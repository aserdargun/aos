import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires an owned Docker desktop and built UI')
class SyntheticLearningUITests(unittest.TestCase):
    def test_opt_in_is_task_scoped_and_fixture_does_not_fabricate_model_events(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='learning-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            arguments = ('--synthetic-learning-stream-dir', str(root / 'learning-stream'))
            with task_server(root, 'fixture', arguments) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    self.assertTrue(client.get('/api/tasks').json()['supports_learning_metadata'])
                    option = page.get_by_test_id('learning-metadata-option').get_by_role('checkbox')
                    expect(option).not_to_be_checked()
                    option.check()
                    page.get_by_role('button', name='Start Hello task', exact=True).click()
                    expect(option).not_to_be_checked()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    status = page.get_by_test_id('learning-metadata-status')
                    expect(status).to_contain_text('Synced')
                    expect(status).to_contain_text('S1 events: 0')
                    expect(status).to_contain_text('S2 events: 0')
                    job = client.get('/api/tasks').json()['jobs'][0]
                    self.assertEqual(job['learning_metadata']['state'], 'synced')
                    self.assertEqual(job['learning_metadata']['entries_by_role'], {'system1': 0, 'system2': 0})
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()
