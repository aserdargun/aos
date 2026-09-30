import asyncio
import os
import signal
import socket
import subprocess

import httpx

from aos.contracts import REPO_ROOT
from aos.owned_skill_knowledge_transport import preview_transport, report_transport


async def validate_native_reports(reports):
    from playwright.async_api import async_playwright

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    origin = 'http://127.0.0.1:' + str(port)
    server = subprocess.Popen(['pnpm', 'exec', 'vite', '--host', '127.0.0.1',
        '--port', str(port), '--strictPort'], cwd=REPO_ROOT / 'ui',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for attempt in range(100):
                if server.poll() is not None:
                    raise RuntimeError('native_report_validator_server_failed')
                try:
                    response = await client.get(origin + '/ui/')
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError('native_report_validator_server_timeout')
        async with async_playwright() as playwright:
            browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
            browser = await playwright.chromium.launch(executable_path=str(browser_path))
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.route('**/ui/native-proof-validation', lambda route: route.fulfill(
                    content_type='text/html', body='<html><body></body></html>'))
                await page.goto(origin + '/ui/native-proof-validation')
                for report in reports:
                    preview = report['bundle']['knowledge']['intent']['preview']
                    results = await page.evaluate('''async (input) => {
                        const validators = await import('/ui/src/OwnedSkillKnowledge.tsx');
                        const preview = input.preview.preview;
                        return [await validators.validOwnedSkillKnowledgePreview(input.preview,
                            preview.scope, preview.query, preview.goal,
                            preview.authority.runtime_id, preview.authority.reuse_admission_sha256),
                            await validators.validOwnedSkillKnowledgeReport(input.report,
                            input.report.report.planning_bundle_sha256,
                            preview.authority.runtime_id, preview.authority.reuse_admission_sha256)];
                    }''', {'preview': preview_transport(preview), 'report': report_transport(report)})
                    if results != [True, True]:
                        raise AssertionError('native_report_browser_validation_failed: ' + repr(results))
                if errors:
                    raise AssertionError('native_report_browser_errors: ' + repr(errors))
            finally:
                await browser.close()
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
