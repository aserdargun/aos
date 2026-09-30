import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


def _counts(passed=0, skipped=0, failed=0, error=0, expected_failure=0, unexpected_success=0, unknown=0):
    return {
        'passed': passed,
        'skipped': skipped,
        'failed': failed,
        'error': error,
        'expected_failure': expected_failure,
        'unexpected_success': unexpected_success,
        'unknown': unknown,
    }


def _report():
    names = ('contracts', 'ui', 'transport', 'real_tasks', 'real_mcp', 'real_takeover', 'real_reuse', 'real_learning')
    cases = []
    for index, name in enumerate(names):
        status = ('passed', 'partial', 'failed', 'not_run', 'unavailable', 'infrastructure_error', 'not_verified', 'passed')[index]
        counts = _counts(passed=2, skipped=1) if status == 'partial' else _counts(passed=1) if status == 'passed' else _counts(failed=0) if status == 'failed' else None
        seconds = 1.25 if counts is not None else None
        cases.append({
            'case': name,
            'status': status,
            'started_at': None if status in ('not_run', 'unavailable', 'infrastructure_error') else '2026-09-27T12:34:56+00:00',
            'counts': counts,
            'seconds': seconds,
        })
    return {
        'schema_version': '1.0',
        'historical_only': True,
        'real_site_acceptance': False,
        'approval_driver': 'test harness',
        'available': False,
        'cases': cases,
    }


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker desktop and built UI')
class CapabilityEvidenceUITests(unittest.TestCase):
    def _open(self, page, origin, token):
        page.goto(origin + '/ui/')
        page.get_by_label('Local session token', exact=True).fill(token)
        page.get_by_role('button', name='Sign in', exact=True).click()
        return page.get_by_test_id('capability-evidence')

    def test_historical_rows_render_mixed_integrity_and_refresh_is_get_only(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='capability-evidence-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 390, 'height': 844})
                    payload = _report()
                    requests = []

                    def capability_route(route):
                        requests.append((route.request.method, route.request.url))
                        route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

                    page.route('**/api/capability-checks', capability_route)
                    panel = self._open(page, origin, token)
                    expect(panel).to_have_attribute('data-state', 'unavailable')
                    expect(panel).to_contain_text('Historical capability test pool')
                    expect(panel.get_by_test_id('capability-check-case-contracts')).to_have_attribute('data-status', 'passed')
                    expect(panel.get_by_test_id('capability-check-case-ui')).to_have_attribute('data-status', 'partial')
                    expect(panel.get_by_test_id('capability-check-case-contracts')).to_contain_text('2026-09-27T12:34:56+00:00')
                    expect(panel.get_by_test_id('capability-check-case-real_tasks').locator('time')).not_to_have_attribute('datetime', '2026-09-27T12:34:56+00:00')
                    expect(panel.get_by_test_id('capability-check-case-real_reuse')).to_have_attribute('data-status', 'not_verified')
                    expect(panel.locator('[data-testid^="capability-check-case-"]')).to_have_count(8)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Tarihsel yetenek test havuzu')
                    self.assertEqual(len(requests), 1, 'Language switch must not refetch historical evidence')
                    payload['available'] = True
                    with page.expect_response('**/api/capability-checks'):
                        page.get_by_test_id('capability-evidence-refresh').click()
                    expect(panel).to_have_attribute('data-state', 'ready')
                    expect(panel.get_by_test_id('capability-evidence-driver')).to_contain_text('test harness')
                    expect(panel.get_by_test_id('capability-evidence-driver')).to_contain_text('Gerçek site kabulü: hayır')
                    self.assertEqual(requests, [('GET', origin + '/api/capability-checks')] * 2)
                    self.assertLessEqual(page.locator('body').evaluate('(element) => element.scrollWidth'), 390)
                finally:
                    browser.close()

    def test_malformed_reports_and_old_backend_fail_closed(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='capability-evidence-invalid-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    payload = _report()
                    page.route('**/api/capability-checks', lambda route: route.fulfill(
                        status=200, content_type='application/json', body=json.dumps(payload)))
                    panel = self._open(page, origin, token)
                    expect(panel).to_have_attribute('data-state', 'unavailable')

                    invalid_reports = []
                    duplicate = _report()
                    duplicate['cases'][1]['case'] = duplicate['cases'][0]['case']
                    invalid_reports.append(duplicate)
                    negative = _report()
                    negative['cases'][0]['counts']['passed'] = -1
                    invalid_reports.append(negative)
                    green_with_skip = _report()
                    green_with_skip['cases'][0]['counts']['skipped'] = 1
                    invalid_reports.append(green_with_skip)
                    missing_terminal_time = _report()
                    missing_terminal_time['cases'][0]['started_at'] = None
                    invalid_reports.append(missing_terminal_time)
                    wrong_schema = _report()
                    wrong_schema['schema_version'] = '2.0'
                    invalid_reports.append(wrong_schema)

                    for invalid in invalid_reports:
                        payload.clear()
                        payload.update(invalid)
                        page.get_by_test_id('capability-evidence-refresh').click()
                        expect(panel).to_have_attribute('data-state', 'malformed')
                        expect(panel.get_by_test_id('capability-check-case-contracts')).to_have_count(0)

                    payload.clear()
                    payload.update(_report())
                    page.unroute('**/api/capability-checks')
                    page.route('**/api/capability-checks', lambda route: route.fulfill(status=404, body='{}'))
                    page.get_by_test_id('capability-evidence-refresh').click()
                    expect(panel).to_have_attribute('data-state', 'old_backend')
                    expect(panel).to_contain_text('This backend does not provide the test evidence API.')
                finally:
                    browser.close()
