import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import REPO_ROOT, digest
from aos.desktop_console import create_console


class OwnedFormRecipeConsoleTests(unittest.TestCase):
    def test_recipe_audit_route_requires_matching_discriminant_and_report(self):
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_audit.json').read_text())['report']
        verified = {'mode': 'owned_synthetic_form_recipe', 'available': True,
                    'status': 'verified', 'report': report,
                    'report_sha256': digest(report)}
        scheduler = Mock()
        scheduler.audit_owned_form_invocation.return_value = verified
        origin = 'http://127.0.0.1:19042'
        assets = tempfile.TemporaryDirectory(prefix='owned-form-recipe-console-assets-')
        self.addCleanup(assets.cleanup)
        app = create_console(object(), 'synthetic-token', origin, Path(assets.name),
                             scheduler=scheduler, owned_form_recipe_mode=True)

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                endpoint = '/api/tasks/owned-form-invocation-audit'
                await client.post('/api/login', json={'token': 'synthetic-token'})
                response = await client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), verified)

                scheduler.audit_owned_form_invocation.return_value = {
                    **verified, 'mode': 'owned_synthetic_form_invocation'}
                mismatch = await client.get(endpoint)
                self.assertEqual(mismatch.json(), {
                    'available': False, 'status': 'unavailable', 'report': None,
                    'report_sha256': None, 'mode': 'owned_synthetic_form_recipe'})

                scheduler.audit_owned_form_invocation.return_value = {
                    'mode': 'owned_synthetic_form_recipe', 'available': False,
                    'status': 'not_ready', 'report': None, 'report_sha256': None}
                not_ready = await client.get(endpoint)
                self.assertEqual(not_ready.json(), scheduler.audit_owned_form_invocation.return_value)

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
