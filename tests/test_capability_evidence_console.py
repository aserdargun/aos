from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from aos.desktop_console import create_console
from aos.capability_evidence import CAPABILITY_CASES


class CapabilityEvidenceConsoleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.origin = 'http://testserver'
        self.client = TestClient(create_console(SimpleNamespace(), 'synthetic-test-token',
                                                self.origin, Path(directory.name)))
        self.addCleanup(self.client.close)

    def login(self):
        self.client.post('/api/login', headers={'Origin': self.origin},
                         json={'token': 'synthetic-test-token'}).raise_for_status()

    def test_login_required_and_no_caller_paths_or_mutations(self):
        with patch('aos.desktop_console.read_capability_evidence') as read:
            self.assertEqual(self.client.get('/api/capability-checks').status_code, 401)
            read.assert_not_called()
            self.login()
            self.assertEqual(self.client.get('/api/capability-checks?path=/etc/passwd').status_code, 400)
            self.assertEqual(self.client.post('/api/capability-checks', headers={'Origin': self.origin},
                                              json={}).status_code, 405)
            self.assertEqual(self.client.get('/api/capability-checks',
                                             headers={'Host': 'untrusted.invalid'}).status_code, 403)
            read.assert_not_called()

    def test_read_only_report_and_no_cache(self):
        self.login()
        report = {'schema_version': '1.0', 'historical_only': True,
                  'real_site_acceptance': False, 'approval_driver': 'test harness',
                  'available': True, 'cases': [
                      {'case': case, 'status': 'not_run', 'started_at': None,
                       'counts': None, 'seconds': None} for case in CAPABILITY_CASES]}
        with patch('aos.desktop_console.read_capability_evidence', return_value=report) as read:
            response = self.client.get('/api/capability-checks')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), report)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        read.assert_called_once_with()

    def test_reader_error_does_not_expose_private_paths(self):
        self.login()
        with patch('aos.desktop_console.read_capability_evidence',
                   side_effect=OSError('private token or source path')):
            response = self.client.get('/api/capability-checks')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'Capability evidence unavailable'})
