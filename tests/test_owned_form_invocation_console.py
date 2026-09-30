import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import AOSFault, ErrorCode, REPO_ROOT, digest
from aos.desktop_console import create_console


class OwnedFormInvocationConsoleTests(unittest.TestCase):
    def test_audit_route_is_manual_authenticated_and_fail_closed(self):
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation_audit.json').read_text())['report']
        audited = {'available': True, 'status': 'verified', 'report': report,
                   'report_sha256': digest(report)}
        scheduler = Mock()
        scheduler.audit_owned_form_invocation.return_value = audited
        origin = 'http://127.0.0.1:19041'
        assets = tempfile.TemporaryDirectory(prefix='owned-form-invocation-console-assets-')
        self.addCleanup(assets.cleanup)
        app = create_console(object(), 'synthetic-token', origin, Path(assets.name),
                             scheduler=scheduler)

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                endpoint = '/api/tasks/owned-form-invocation-audit'
                self.assertEqual((await client.get(endpoint)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})
                self.assertEqual((await client.get(endpoint + '?run_id=other')).status_code, 400)
                self.assertEqual((await client.request('GET', endpoint, content=b'{}')).status_code, 400)
                scheduler.audit_owned_form_invocation.assert_not_called()

                response = await client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), audited)
                scheduler.audit_owned_form_invocation.assert_called_once_with()

                scheduler.audit_owned_form_invocation.reset_mock()
                scheduler.audit_owned_form_invocation.return_value = {
                    **audited, 'report_sha256': '0' * 64}
                malformed = await client.get(endpoint)
                self.assertEqual(malformed.status_code, 200)
                self.assertEqual(malformed.json(), {
                    'available': False, 'status': 'unavailable',
                    'report': None, 'report_sha256': None})

                scheduler.audit_owned_form_invocation.side_effect = AOSFault(
                    ErrorCode.UNSAFE_ACTION, 'Owned invocation audit requires an idle scheduler')
                busy = await client.get(endpoint)
                self.assertEqual(busy.status_code, 409)

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
