import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from jsonschema.exceptions import ValidationError

from aos.desktop_console import create_console


class OwnedEpisodeConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / 'assets').mkdir()
        self.origin = 'http://testserver'
        self.token = 'synthetic-console-test-token'
        self.learning = SimpleNamespace(calls=[], reserved=False)
        self.learning.inspect = self.inspect
        self.learning.review = self.review
        self.learning.revoke = self.revoke
        self.learning.export = self.export
        self.learning.conversion_preview = self.conversion_preview
        self.learning.convert = self.convert
        self.learning.readiness = self.readiness
        self.learning.tokenizer_start = self.tokenizer_start
        self.learning.adaptation_preview = lambda **arguments: self.adaptation('adaptation-preview', arguments)
        self.learning.adaptation_start = lambda **arguments: self.adaptation('adaptation-start', arguments)
        self.learning.adaptation_inspect = lambda **arguments: self.adaptation('adaptation-inspect', arguments)
        self.learning.runtime_preview = lambda **arguments: self.adaptation('runtime-preview', arguments)
        self.learning.runtime_start = lambda **arguments: self.adaptation('runtime-start', arguments)
        self.learning.runtime_audit = lambda **arguments: self.adaptation('runtime-audit', arguments)
        self.learning.pair_preview = lambda **arguments: self.adaptation('pair-preview', arguments)
        self.learning.pair_commit = lambda **arguments: self.adaptation('pair-commit', arguments)
        self.learning.pair_start = lambda **arguments: self.adaptation('pair-start', arguments)
        self.learning.pair_report = lambda **arguments: self.adaptation('pair-report', arguments)
        self.scheduler = SimpleNamespace(owned_episode_learning=self.learning, reserved=False)
        self.controller = SimpleNamespace(state=lambda: {
            'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease-current', 'generation': 7})
        self.app = create_console(self.controller, self.token, self.origin, root / 'assets',
                                  scheduler=self.scheduler, web_profiles_root=root / 'profiles')
        self.client = TestClient(self.app)
        self.client.post('/api/login', headers={'Origin': self.origin}, json={'token': self.token})
        self.episode_id = 'episode-' + '1' * 32

    def inspect(self, episode_id):
        self.learning.calls.append(('inspect', episode_id))
        raise ValidationError('synthetic private candidate body must not be returned')

    def review(self, **kwargs):
        self.learning.calls.append(('review', kwargs))
        return {}

    def revoke(self, **kwargs):
        self.learning.calls.append(('revoke', kwargs))
        return {}

    def export(self, **kwargs):
        self.learning.calls.append(('export', kwargs))
        return {}

    def conversion_preview(self, episode_id, export_sha256):
        self.learning.calls.append(('conversion-preview', episode_id, export_sha256))
        return {'available': True, 'episode_id': episode_id, 'export_sha256': export_sha256,
                'conversion_sha256': 'b' * 64, 'persisted': False}

    def convert(self, **kwargs):
        self.learning.calls.append(('convert', kwargs))
        return {'available': True, 'episode_id': kwargs['episode_id'],
                'export_sha256': kwargs['export_sha256'], 'conversion_sha256': 'b' * 64,
                'persisted': True}

    def readiness(self, episode_id, conversion_sha256):
        self.learning.calls.append(('readiness', episode_id, conversion_sha256))
        return {'available': True, 'episode_id': episode_id, 'conversion_sha256': conversion_sha256}

    def tokenizer_start(self, **kwargs):
        self.learning.calls.append(('tokenizer-start', kwargs))
        return {'state': 'pending', 'episode_id': kwargs['episode_id'],
                'conversion_sha256': kwargs['conversion_sha256'], 'training_ready': False}

    def tearDown(self):
        self.client.close()

    def adaptation(self, operation, arguments):
        self.learning.calls.append((operation, arguments))
        return {'state': 'training' if operation == 'adaptation-start' else 'verified'}

    def test_adaptation_budget_and_start_require_control_inspection_is_read_only(self):
        selected = {'schema_version': '1.0', 'episode_id': self.episode_id, 'conversion_sha256': 'b' * 64,
                    'lease_id': 'lease-current', 'generation': 7}
        request = lambda operation, payload: self.client.post('/api/tasks/owned-episode/' + operation,
            headers={'Origin': self.origin}, json=payload)
        self.assertEqual(request('adaptation-preview', selected).status_code, 200)
        self.assertEqual(self.learning.calls[-1][1]['lease_id'], 'lease-current')
        start = selected | {'confirm_sha256': 'c' * 64, 'rights_redaction_reviewed': True,
                            'experimental_training_authorized': True}
        self.assertEqual(request('adaptation-start', start).status_code, 202)
        self.assertTrue(self.learning.calls[-1][1]['experimental_training_authorized'])
        self.learning.calls.clear()
        self.assertEqual(request('adaptation-start', start | {'generation': 8}).status_code, 409)
        self.scheduler.reserved = True
        self.assertEqual(request('adaptation-start', start).status_code, 409)
        self.assertEqual(self.learning.calls, [])
        self.assertEqual(request('adaptation-inspect', {'schema_version': '1.0', 'episode_id': self.episode_id,
                                                       'authorization_sha256': 'c' * 64}).status_code, 200)
        self.assertNotIn('lease_id', self.learning.calls[-1][1])
        self.assertEqual(request('adaptation-start', {key: value for key, value in start.items()
                                                   if key != 'experimental_training_authorized'}).status_code, 400)

    def test_runtime_requires_exact_fields_separate_permission_and_current_control(self):
        selected = {'schema_version': '1.0', 'episode_id': self.episode_id, 'authorization_sha256': 'c' * 64,
                    'case_key': 'adapter-case', 'development_value': 'synthetic message',
                    'lease_id': 'lease-current', 'generation': 7}
        request = lambda operation, payload: self.client.post('/api/tasks/owned-episode/' + operation,
            headers={'Origin': self.origin}, json=payload)
        self.assertEqual(request('runtime-preview', selected).status_code, 200)
        self.assertEqual(self.learning.calls[-1][1]['lease_id'], 'lease-current')
        start = selected | {'confirm_sha256': 'd' * 64, 'experimental_runtime_authorized': True}
        self.assertEqual(request('runtime-start', start).status_code, 202)
        self.assertEqual(self.learning.calls[-1][1], {key: value for key, value in start.items() if key != 'schema_version'})
        self.learning.calls.clear()
        self.assertEqual(request('runtime-start', selected | {'confirm_sha256': 'd' * 64}).status_code, 400)
        self.assertEqual(request('runtime-start', start | {'experimental_training_authorized': True}).status_code, 400)
        self.assertEqual(request('runtime-start', start | {'generation': 8}).status_code, 409)
        self.scheduler.reserved = True
        self.assertEqual(request('runtime-start', start).status_code, 409)
        self.assertEqual(self.learning.calls, [])
        audit = {'schema_version': '1.0', 'episode_id': self.episode_id,
                 'authorization_sha256': 'c' * 64, 'candidate_execution_sha256': 'd' * 64}
        self.assertEqual(request('runtime-audit', audit).status_code, 200)
        self.assertNotIn('lease_id', self.learning.calls[-1][1])

    def test_pair_routes_preserve_explicit_control_and_exact_payloads(self):
        request = lambda operation, payload: self.client.post('/api/tasks/owned-episode/' + operation,
            headers={'Origin': self.origin}, json=payload)
        selection = {'schema_version': '1.0', 'episode_id': self.episode_id,
            'authorization_sha256': 'a' * 64, 'case_key': 'pair-case', 'development_value': 'synthetic pair',
            'lease_id': 'lease-current', 'generation': 7}
        self.assertEqual(request('pair-preview', selection).status_code, 200)
        committed = selection | {'confirm_sha256': 'b' * 64, 'experimental_evaluation_authorized': True}
        self.assertEqual(request('pair-commit', committed).status_code, 200)
        self.assertEqual(self.learning.calls[-1][1], {key: value for key, value in committed.items() if key != 'schema_version'})
        start = {'schema_version': '1.0', 'episode_id': self.episode_id, 'pair_sha256': 'b' * 64,
            'arm': 'base', 'confirm_sha256': 'b' * 64, 'experimental_runtime_authorized': True,
            'lease_id': 'lease-current', 'generation': 7}
        self.assertEqual(request('pair-start', start).status_code, 202)
        self.learning.calls.clear()
        self.assertEqual(request('pair-start', start | {'generation': 8}).status_code, 409)
        self.assertEqual(request('pair-start', start | {'automatic_second_arm': True}).status_code, 400)
        self.assertEqual(request('pair-commit', selection | {'confirm_sha256': 'b' * 64}).status_code, 400)
        self.scheduler.reserved = True
        self.assertEqual(request('pair-start', start).status_code, 409)
        self.assertEqual(self.learning.calls, [])
        self.assertEqual(request('pair-report', {'schema_version': '1.0', 'episode_id': self.episode_id,
            'pair_sha256': 'b' * 64}).status_code, 200)
        self.assertNotIn('lease_id', self.learning.calls[-1][1])

    def test_write_routes_reject_stale_lease_or_generation_before_service(self):
        requests = (
            {'schema_version': '1.0', 'episode_id': self.episode_id, 'role': 'system1',
             'decision': 'accept', 'confirm_sha256': 'a' * 64,
             'lease_id': 'lease-stale', 'generation': 7},
            {'schema_version': '1.0', 'episode_id': self.episode_id, 'role': 'system1',
             'decision': 'accept', 'confirm_sha256': 'a' * 64,
             'lease_id': 'lease-current', 'generation': 6},
        )
        for payload in requests:
            with self.subTest(lease=payload['lease_id'], generation=payload['generation']):
                response = self.client.post('/api/tasks/owned-episode/review',
                    headers={'Origin': self.origin}, json=payload)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['detail'],
                                 'Episode source or review changed; no training authorized')
                self.assertEqual(self.learning.calls, [])

    def test_private_schema_validation_failure_is_sanitized_at_route(self):
        response = self.client.post('/api/tasks/owned-episode/inspect',
            headers={'Origin': self.origin}, json={
                'schema_version': '1.0', 'episode_id': self.episode_id})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'],
                         'Episode source or review changed; no training authorized')
        self.assertNotIn('private candidate body', response.text)
        self.assertEqual(self.learning.calls, [('inspect', self.episode_id)])

    def test_conversion_preview_and_readiness_are_read_only_and_forward_exact_pins(self):
        export_sha = 'a' * 64
        response = self.client.post('/api/tasks/owned-episode/conversion-preview',
            headers={'Origin': self.origin}, json={'schema_version': '1.0',
                'episode_id': self.episode_id, 'export_sha256': export_sha})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.learning.calls[-1], ('conversion-preview', self.episode_id, export_sha))
        conversion_sha = response.json()['conversion_sha256']
        response = self.client.post('/api/tasks/owned-episode/readiness',
            headers={'Origin': self.origin}, json={'schema_version': '1.0',
                'episode_id': self.episode_id, 'conversion_sha256': conversion_sha})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.learning.calls[-1], ('readiness', self.episode_id, conversion_sha))

    def test_conversion_publish_forwards_export_not_conversion_and_requires_fresh_control(self):
        export_sha, conversion_sha = 'a' * 64, 'b' * 64
        payload = {'schema_version': '1.0', 'episode_id': self.episode_id,
                   'export_sha256': export_sha, 'confirm_sha256': conversion_sha,
                   'lease_id': 'lease-current', 'generation': 7}
        response = self.client.post('/api/tasks/owned-episode/convert',
            headers={'Origin': self.origin}, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.learning.calls[-1], ('convert', {
            'episode_id': self.episode_id, 'export_sha256': export_sha,
            'confirm_sha256': conversion_sha}))
        self.learning.calls.clear()
        for changed in ({**payload, 'lease_id': 'stale'}, {**payload, 'generation': 8}):
            response = self.client.post('/api/tasks/owned-episode/convert',
                headers={'Origin': self.origin}, json=changed)
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.learning.calls, [])

    def test_tokenizer_start_returns_202_and_forwards_conversion_pin_and_control(self):
        payload = {'schema_version': '1.0', 'episode_id': self.episode_id,
                   'conversion_sha256': 'b' * 64, 'confirm_sha256': 'b' * 64,
                   'lease_id': 'lease-current', 'generation': 7}
        response = self.client.post('/api/tasks/owned-episode/tokenizer-start',
            headers={'Origin': self.origin}, json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['state'], 'pending')
        self.assertEqual(self.learning.calls, [('tokenizer-start', {
            'episode_id': self.episode_id, 'conversion_sha256': 'b' * 64,
            'confirm_sha256': 'b' * 64, 'lease_id': 'lease-current', 'generation': 7})])
        self.learning.calls.clear()
        response = self.client.post('/api/tasks/owned-episode/tokenizer-start',
            headers={'Origin': self.origin}, json={**payload, 'generation': 8})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.learning.calls, [])


if __name__ == '__main__':
    unittest.main()
