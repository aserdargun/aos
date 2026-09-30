import copy
from datetime import datetime, timedelta, timezone
import json
import fcntl
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from aos.contracts import REPO_ROOT
from aos.desktop_console import create_console


class KnowledgeConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.knowledge = self.root / 'document-knowledge'
        self.control = {'owner': 'AGENT', 'status': 'running', 'lease_id': 'synthetic-lease', 'generation': 1}
        self.controller = SimpleNamespace(state=lambda: dict(self.control))
        self.scheduler = SimpleNamespace(reserved=False, closed=False, restart_quiesced=False,
            status=lambda: {'available': True, 'busy': False, 'jobs': [], 'approval': None})
        self.app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
            scheduler=self.scheduler, knowledge_root=self.knowledge, web_profiles_root=self.root / 'profiles')
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'Origin': 'http://testserver'}
        self.client.post('/api/login', headers=self.headers, json={'token': 'synthetic-token'})
        fixture = json.loads((REPO_ROOT / 'examples/knowledge.json').read_text())
        self.upload = fixture['requests']['publish-preview'] | {
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        self.scope = self.upload['scope']
        self.authority = {'lease_id': 'synthetic-lease', 'generation': 1}

    def request(self, operation, value):
        return self.client.post('/api/knowledge/' + operation, headers=self.headers,
                                content=json.dumps(value))

    def publication(self):
        response = self.request('publish-preview', self.upload)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        return {'schema_version': '1.0', 'preview': preview,
                'confirm_sha256': preview['preview_sha256']} | self.authority

    def test_authenticated_actual_preview_publish_review_search_and_revoke_vertical(self):
        self.assertTrue(self.client.get('/api/tasks').json()['knowledge_available'])
        publication = self.publication()
        self.assertFalse(self.knowledge.exists())
        published = self.request('publish', publication)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()['review_status'], 'pending')
        search = {'schema_version': '1.0', 'scope': self.scope, 'query': 'password reset', 'top_k': 3, 'context_chars': 2048}
        self.assertEqual(self.request('search', search).json()['hits'], [])
        selection = {'schema_version': '1.0', 'scope': self.scope,
                     'document_sha256': published.json()['document_sha256']}
        review = self.request('review-preview', selection | {'decision': 'accept'}).json()
        self.assertEqual(self.request('review', {'schema_version': '1.0', 'preview': review,
            'confirm_sha256': review['preview_sha256']} | self.authority).status_code, 200)
        result = self.request('search', search)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['hits'][0]['document_sha256'], selection['document_sha256'])
        self.assertFalse(result.json()['execution_authorized'])
        review = self.request('review-preview', selection | {'decision': 'revoke'}).json()
        result = self.request('review', {'schema_version': '1.0', 'preview': review,
            'confirm_sha256': review['preview_sha256']} | self.authority)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['review_status'], 'revoked')
        self.assertEqual(self.request('search', search).json()['hits'], [])
        self.assertEqual(self.request('inspect', selection).json()['review_status'], 'revoked')
        self.assertEqual(len(self.request('catalog', {'schema_version': '1.0', 'scope': self.scope}).json()['documents']), 1)

    def test_unauthenticated_origin_and_host_fail_before_upload(self):
        with TestClient(self.app) as anonymous:
            response = anonymous.post('/api/knowledge/publish-preview', headers=self.headers, json=self.upload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post('/api/knowledge/publish-preview',
            headers={'Origin': 'http://other.invalid'}, json=self.upload).status_code, 403)
        self.assertEqual(self.client.post('/api/knowledge/publish-preview',
            headers=self.headers | {'Host': 'other.invalid'}, json=self.upload).status_code, 403)
        self.assertFalse(self.knowledge.exists())

    def test_control_lease_reservation_shutdown_and_quiesce_block_writes(self):
        publication = self.publication()
        original = dict(self.control)
        for change in ({'owner': 'HUMAN'}, {'status': 'paused'}, {'lease_id': 'stale'}, {'generation': 2}):
            self.control.update(change)
            self.assertEqual(self.request('publish', publication).status_code, 409)
            self.control = dict(original)
        for field in ('reserved', 'closed', 'restart_quiesced'):
            setattr(self.scheduler, field, True)
            self.assertEqual(self.request('publish', publication).status_code, 409)
            setattr(self.scheduler, field, False)
        self.assertFalse(self.knowledge.exists())
        self.control['owner'] = 'HUMAN'
        self.assertEqual(self.request('publish-preview', self.upload).status_code, 200)

    def test_exact_typed_bounded_body_denies_paths_urls_duplicates_and_coercions(self):
        for modification in ({'source_path': '/etc/passwd'}, {'url': 'https://example.invalid'},
                             {'storage_consent': 1}, {'rights_attested': 1}, {'storage_consent': False},
                             {'text': '\ud800'}, {'title': '\ud800'}, {'title': '  '},
                             {'text': '🧪' * 20000}, {'schema_version': '2.0'},
                             {'scope': self.scope | {'account_role': '*'}}, {'source_id': '../outside'}):
            response = self.request('publish-preview', self.upload | modification)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.request('fetch', self.upload).status_code, 400)
        self.assertEqual(self.request('publish-preview?scope=other', self.upload).status_code, 400)
        content = json.dumps(self.upload)[:-1] + ',"schema_version":"1.0"}'
        self.assertEqual(self.client.post('/api/knowledge/publish-preview',
            headers=self.headers, content=content).status_code, 400)
        self.assertEqual(self.client.post('/api/knowledge/publish-preview',
            headers=self.headers, content='x' * 524289).status_code, 413)
        publication = self.publication()
        self.assertEqual(self.request('publish', publication | {'generation': True}).status_code, 400)
        changed = copy.deepcopy(publication)
        changed['preview']['document']['untrusted'] = 1
        self.assertEqual(self.request('publish', changed).status_code, 400)
        self.assertFalse(self.knowledge.exists())

    def test_unconfigured_old_console_reports_unavailable_and_never_writes(self):
        app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
                             scheduler=self.scheduler, web_profiles_root=self.root / 'profiles')
        with TestClient(app) as client:
            client.post('/api/login', headers=self.headers, json={'token': 'synthetic-token'})
            self.assertFalse(client.get('/api/tasks').json()['knowledge_available'])
            response = client.post('/api/knowledge/publish-preview', headers=self.headers, json=self.upload)
            self.assertEqual(response.status_code, 409)
        self.assertFalse(self.knowledge.exists())

    def test_review_requires_current_idle_control_and_busy_store_is_conflict(self):
        document = self.request('publish', self.publication()).json()
        preview = self.request('review-preview', {'schema_version': '1.0', 'scope': self.scope,
            'document_sha256': document['document_sha256'], 'decision': 'accept'}).json()
        request = {'schema_version': '1.0', 'preview': preview,
                   'confirm_sha256': preview['preview_sha256']} | self.authority
        self.scheduler.reserved = True
        self.assertEqual(self.request('review', request).status_code, 409)
        self.scheduler.reserved = False
        self.assertFalse(any(self.knowledge.glob('review-*.json')))
        descriptor = os.open(self.knowledge, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.request('catalog', {'schema_version': '1.0', 'scope': self.scope}).status_code, 409)
        self.assertEqual(self.request('review', request).status_code, 409)

    def test_bad_hash_wrong_scope_and_corrupt_private_source_errors_are_sanitized(self):
        publication = self.publication()
        self.assertEqual(self.request('publish', publication | {'confirm_sha256': '0' * 64}).status_code, 409)
        result = self.request('publish', publication)
        self.assertEqual(result.status_code, 200, result.text)
        checksum = result.json()['document_sha256']
        selection = {'schema_version': '1.0', 'scope': self.scope | {'tenant_id': 'other'}, 'document_sha256': checksum}
        self.assertEqual(self.request('inspect', selection).status_code, 409)
        filename = self.knowledge / ('document-' + checksum + '.json')
        filename.write_text('synthetic-private-sentinel')
        response = self.request('catalog', {'schema_version': '1.0', 'scope': self.scope})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('synthetic-private-sentinel', response.text)
        self.assertNotIn(str(self.knowledge), response.text)


if __name__ == '__main__':
    unittest.main()
