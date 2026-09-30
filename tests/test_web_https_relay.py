import base64
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import queue
import select
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import jsonschema

from aos.contracts import AOSFault, Action, Phase, Prediction, REPO_ROOT, State, canonical, digest
from aos import local_app
from aos.computer import SafetyPolicy, ToolRegistry
from aos.desktop import DOCKER, DesktopRuntime
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle, unpack_bundle
from aos.desktop_form_mcp import DesktopHTTPSFormMCPRuntime
from aos.desktop_readonly_mcp import DesktopReadOnlyRoutesMCPRuntime, RemoteRouteObservation
from aos.desktop_remote_mcp import DesktopRemoteEntryMCPRuntime, RemoteEntryObservation
from aos.desktop_static_bundle_mcp import DesktopStaticBundleMCPRuntime
from aos.desktop_tasks import Approval, DesktopScheduler
from aos.contracts import Settings
from aos.dataset_audit import audit_snapshot
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.learning_events import review_learning_events
from aos.reusable_decider import ReusableDeciderEngine
from aos.remote_route_evidence import complete_link_sample_sha256, review_remote_route_evidence
from aos.remote_learning_source import inspect_remote_learning_source
from aos.remote_form_learning_source import inspect_remote_form_learning_source
from aos.remote_form_json_oracle import (plan_remote_form_json_oracle,
                                         probe_remote_form_json_oracle)
from aos.remote_form_json_submission import (plan_remote_form_json_submission,
                                             probe_remote_form_json_submission)
from aos.remote_form_learning_stream import poll_remote_form_learning_stream
from aos.remote_form_repeat import inspect_remote_form_repeats
from aos.remote_static_learning_source import (inspect_remote_readonly_data_source,
                                               inspect_remote_static_learning_source)
from aos.remote_learning_consent import (RemoteLearningConsents,
                                         preview_remote_learning_consent)
from aos.remote_learning_lifecycle import revoke_remote_learning
from aos.remote_learning_stream import poll_remote_learning_stream
from aos.remote_static_learning_stream import (poll_remote_readonly_data_stream,
                                               poll_remote_static_learning_stream)
from aos.remote_route_change import compare_remote_route_change, main as remote_route_change_main
from aos.remote_readonly_data_change import compare_remote_readonly_data_change
from aos.remote_readonly_data_knowledge import preview_remote_readonly_data_knowledge
from aos.remote_readonly_data_page_draft import seed_remote_readonly_data_page_draft
from aos.remote_readonly_data_knowledge_review import (
    RemoteReadonlyDataKnowledgeReviewStore,
    prepare_live_remote_readonly_data_knowledge,
    register_remote_readonly_data_knowledge_review,
    recheck_remote_readonly_data_knowledge)
from aos.remote_page_draft_seed import (RemotePageDraftRegistrationReport, RemotePageDraftSeedReport,
                                        seed_remote_page_draft,
                                        main as remote_page_draft_seed_main)
from aos.remote_navigation_graph import preview_remote_navigation_graph, main as remote_navigation_graph_main
from aos.remote_route_knowledge import preview_remote_route_knowledge, main as remote_route_knowledge_main
from aos.remote_route_knowledge_review import (
    LiveRemoteRouteKnowledgePin, RemoteRouteKnowledgeCandidatePreview,
    RemoteRouteKnowledgeReview, RemoteRouteKnowledgeReviewStore,
    register_remote_route_knowledge_review,
    retrieve_remote_route_knowledge, main as remote_route_knowledge_review_main)
from aos.remote_routes_operator import reviewed_route_context
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                         parameter_variant_sha256)
from aos.site_skill_form_execution import audit_site_skill_form_execution
from aos.site_skill_form_invocation import (compile_site_skill_form_invocation,
                                            revalidate_site_skill_form_invocation)
from aos.site_skill_form_invocation_audit import audit_site_skill_form_invocation_execution
from aos.site_skill_form_cohort import (SiteSkillFormCohortSelection,
                                        audit_site_skill_form_cohort)
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.remote_site_skill_provenance import inspect_remote_site_skill_sources
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import (WebReadOnlyRoutePlan, WebRuntimePin, WebTaskContract, bind_web_task,
                                         plan_web_readonly_routes)
from aos.web_form_draft import (preview_form_task, register_form_task,
                                preview_form_plan, register_form_plan,
                                preview_form_state_plan, register_form_state_plan)
from aos.web_https_relay import ExactEntryRelay, ExactReadOnlyRouteRelay, WebHTTPSRelayReport
from aos.web_static_assets import plan_web_static_assets
from aos.web_readonly_data import (WebReadOnlyDataBundlePlan,
                                   WebReadOnlyDataFetchReport,
                                   plan_web_readonly_data_bundle)
from aos.web_static_asset_relay import (ExactStaticBundleRelay,
                                        WebReadOnlyDataBundleRelayReport,
                                        WebStaticBundleRelayReport)
from aos.web_https_form_transport import (ExactHTTPSFormRelay, ExactHTTPSFormTransport,
                                          WebHTTPSFormPlan, exact_form_fields, form_body,
                                          form_stage_arguments,
                                          parse_form_fields_document, plan_web_https_form)
from aos.web_https_form_state_probe import (ExactHTTPSFormStateProbe,
                                            WebHTTPSFormStatePlan,
                                            WebHTTPSFormStateReport,
                                            form_state_action_arguments,
                                            form_state_request_sha256,
                                            form_state_marker_sha256,
                                            plan_web_https_form_state,
                                            verify_submitted_field_binding)
from test_desktop_mcp import worker_namespace


class SyntheticEntry(BaseHTTPRequestHandler):
    body = b'<html><title>Relay fixture</title><h1>Synthetic entry</h1></html>'

    def do_GET(self):
        self.server.paths.append(self.path)
        if hasattr(self.server, 'cookies'):
            self.server.cookies.append((self.path, self.headers.get('Cookie')))
        if self.path == '/oracle-submitted':
            if (not self.server.posts
                    or getattr(self.server, 'post_cookie', None) != self.headers.get('Cookie')):
                self.send_error(403)
                return
            fields = parse_qs(self.server.posts[-1][1].decode('utf-8'), strict_parsing=True)
            body = json.dumps({'message': fields['message'][0]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        asset_response = getattr(self.server, 'asset_responses', {}).get(self.path)
        if asset_response is not None:
            content_type, body = asset_response
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        state_bodies = getattr(self.server, 'state_bodies', None)
        if self.path == '/state' and state_bodies is not None:
            body = state_bodies[bool(self.server.posts)]
        else:
            body = getattr(self.server, 'route_bodies', {}).get(self.path, self.body)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if hasattr(self.server, 'cookies'):
            self.server.cookies.append((self.path, self.headers.get('Cookie')))
        self.server.post_cookie = self.headers.get('Cookie')
        length = int(self.headers.get('Content-Length', '0'))
        self.server.posts.append((self.path, self.rfile.read(length)))
        self.send_response(getattr(self.server, 'post_status', 303))
        self.send_header('Location', getattr(self.server, 'post_location', '/receipt'))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *_arguments):
        pass


class WebHTTPSRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='web-https-relay-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.socket_path = self.workspace / 'relay.sock'
        self.host = 'www.aos-relay.invalid'
        certificate = self.root / 'certificate.pem'
        key = self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-subj', '/CN=' + self.host,
                        '-addext', 'subjectAltName=DNS:' + self.host
                        + ',DNS:www.aos-form.example.com',
                        '-keyout', str(key), '-out', str(certificate)],
                       check=True, capture_output=True)
        self.tls_context = ssl.create_default_context(cafile=str(certificate))
        self.server = HTTPServer(('127.0.0.1', 0), SyntheticEntry)
        self.server.paths = []
        self.server.posts = []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(certificate), str(key))
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
        origin = f'https://{self.host}:{self.server.server_port}'
        self.profile = WebApplicationProfile.model_validate({**source,
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        self.checksum = profile_report(self.profile).profile_sha256
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profiles.register(self.profile, confirm_sha256=self.checksum)
        binding = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())
        task = WebTaskContract.model_validate({**binding['task'],
            'profile_sha256': self.checksum, 'entry_url': self.profile.entry_url,
            'allowed_origins': self.profile.allowed_origins})
        self.draft = bind_web_task(self.profiles, task, WebRuntimePin.model_validate(binding['runtime']))
        self.entry_relay = ExactEntryRelay(self.profiles, self.checksum, self.checksum,
                                           self.draft, self.draft.binding_sha256,
                                           tls_context=self.tls_context)
        self.original_connection = socket.create_connection

    def relay(self):
        return self.entry_relay

    def remote_runtime(self, desktop):
        runtime = DesktopRemoteEntryMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        with self.assertRaisesRegex(ValueError, 'live remote MCP runtime pin'):
            runtime.attach_relay(self.relay())
        bound = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        relay = ExactEntryRelay(self.profiles, self.checksum, self.checksum, bound,
                                bound.binding_sha256, tls_context=self.tls_context)
        runtime.attach_relay(relay)
        return runtime

    def run_relay(self, action, *, relay=None):
        outcome = {}
        def worker():
            try:
                outcome['report'] = (relay or self.relay()).serve(self.socket_path, wait_seconds=10)
            except Exception as error:
                outcome['error'] = error

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            for _attempt in range(200):
                if self.socket_path.exists() or 'error' in outcome:
                    break
                time.sleep(0.01)
            try:
                action()
            finally:
                thread.join(12)
            self.assertFalse(thread.is_alive())
        return outcome

    def request(self, payload):
        return self.raw_request((canonical(payload) + '\n').encode())

    def raw_request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.socket_path))
            connection.sendall(payload)
            with connection.makefile('rb') as stream:
                return json.loads(stream.readline(100000))

    def valid_request(self, relay=None):
        return {'method': 'GET', 'url': self.profile.entry_url,
                'binding_sha256': self.draft.binding_sha256,
                'client_token': (relay or self.relay()).client_token}

    def test_exact_entry_only_and_one_shot_cleanup(self):
        replies = []
        def action():
            for payload in ({**self.valid_request(), 'method': 'POST'},
                            {**self.valid_request(), 'url': self.profile.entry_url + '?secret=1'},
                            {**self.valid_request(), 'binding_sha256': '0' * 64},
                            {key: value for key, value in self.valid_request().items() if key != 'client_token'},
                            {**self.valid_request(), 'client_token': '0' * 64},
                            self.valid_request()):
                replies.append(self.request(payload))

        outcome = self.run_relay(action)
        self.assertEqual([reply.get('error') for reply in replies[:5]], ['denied'] * 5)
        self.assertEqual(replies[5]['status'], 200)
        self.assertEqual(base64.b64decode(replies[5]['body_base64']), SyntheticEntry.body)
        self.assertEqual(self.server.paths, ['/entry'])
        self.assertEqual(outcome['report'].request_attempts, 6)
        self.assertFalse(outcome['report'].browser_identity_verified)
        self.assertFalse(outcome['report'].execution_authorized)
        self.assertFalse(self.socket_path.exists())
        with self.assertRaisesRegex(ValueError, 'one_shot'):
            self.relay().serve(self.socket_path, wait_seconds=1)

    def test_static_bundle_relay_requires_arm_and_serves_each_asset_once(self):
        origin = self.profile.allowed_origins[0]
        self.server.asset_responses = {
            '/assets/app.js?v=1': ('application/javascript', b'document.title="Ready";'),
            '/assets/site.css': ('text/css', b'body{color:blue}')}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js?v=1', 'content_type': 'application/javascript'},
            {'url': origin + '/assets/site.css', 'content_type': 'text/css'}])
        plan_sha256 = digest(plan.model_dump())
        with self.assertRaisesRegex(ValueError, 'exact_plan_confirmation'):
            ExactStaticBundleRelay(self.profiles, self.draft.task, plan, '0' * 64)
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       plan_sha256, tls_context=self.tls_context)
        replies = []

        def request(url, asset_index, **changes):
            return self.request({'method': 'GET', 'url': url, 'plan_sha256': plan_sha256,
                                 'client_token': relay.client_token,
                                 'asset_index': asset_index, **changes})

        def action():
            replies.append(request(self.profile.entry_url, None))
            self.assertEqual(self.server.paths, [])
            relay.arm()
            replies.append(request(plan.assets[0].url, 0))
            replies.append(request(self.profile.entry_url, None,
                                   client_token='0' * 64))
            self.assertEqual(self.server.paths, [])
            replies.append(request(self.profile.entry_url, None))
            replies.append(request(plan.assets[0].url, 0))
            replies.append(request(plan.assets[0].url, 0))
            replies.append(request(origin + '/assets/app.js?v=2', 0))
            replies.append(request(plan.assets[1].url, 1))

        outcome = self.run_relay(action, relay=relay)
        self.assertEqual([reply.get('error') for reply in replies],
                         ['denied', 'denied', 'denied', None, None, 'denied', 'denied', None])
        self.assertEqual(self.server.paths, ['/entry', '/assets/app.js?v=1', '/assets/site.css'])
        self.assertEqual(base64.b64decode(replies[4]['body_base64']),
                         b'document.title="Ready";')
        self.assertEqual(outcome['report'].request_attempts, 8)
        self.assertEqual(outcome['report'].asset_response_sha256[0],
                         replies[4]['response_sha256'])
        self.assertFalse(outcome['report'].execution_authorized)
        self.assertFalse(self.socket_path.exists())
        with self.assertRaisesRegex(ValueError, 'one_shot'):
            relay.serve(self.socket_path, wait_seconds=1)

    def test_readonly_json_bundle_relay_requires_arm_and_exact_one_use_get(self):
        origin = self.profile.allowed_origins[0]
        data_url = origin + '/api/summary?view=compact&page=1'
        self.server.asset_responses = {
            '/assets/app.js': ('application/javascript', b'window.ready=true;'),
            '/api/summary?view=compact&page=1': ('application/json', b'{"status":"ready"}')}
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}],
            [{'url': data_url}])
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       digest(plan.model_dump()), tls_context=self.tls_context)
        replies = []

        def request(target_url, index, **changes):
            return self.request({'method': 'GET', 'url': target_url,
                                 'plan_sha256': relay.plan_sha256,
                                 'client_token': relay.client_token,
                                 'asset_index': index, **changes})

        def action():
            replies.append(request(self.profile.entry_url, None))
            relay.arm()
            replies.append(request(data_url, 1))
            replies.append(request(data_url, 1, method='POST'))
            replies.append(request(self.profile.entry_url, None))
            replies.append(request(data_url, 1,
                                   url=origin + '/api/summary?view=compact&page=2'))
            replies.append(request(origin + '/assets/app.js', 0))
            replies.append(request(data_url, 1))

        outcome = self.run_relay(action, relay=relay)
        self.assertEqual([reply.get('error') for reply in replies],
                         ['denied', 'denied', 'denied', None, 'denied', None, None])
        self.assertEqual(self.server.paths, ['/entry', '/assets/app.js',
                                             '/api/summary?view=compact&page=1'])
        self.assertEqual(base64.b64decode(replies[-1]['body_base64']), b'{"status":"ready"}')
        self.assertIsInstance(outcome['report'], WebReadOnlyDataBundleRelayReport)
        self.assertEqual(outcome['report'].request_attempts, 7)
        self.assertEqual(len(outcome['report'].data_response_sha256), 1)
        self.assertFalse(outcome['report'].training_ready)
        self.assertNotIn('"status":"ready"', outcome['report'].model_dump_json())
        self.assertFalse(self.socket_path.exists())

    def test_readonly_json_bundle_schemas_and_synthetic_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_readonly_data_bundle.json').read_text())
        models = {'web_readonly_data_bundle_plan': (WebReadOnlyDataBundlePlan, 'plan'),
                  'web_readonly_data_fetch': (WebReadOnlyDataFetchReport, 'report'),
                  'web_readonly_data_bundle_relay': (WebReadOnlyDataBundleRelayReport,
                                                     'relay_report')}
        for name, (model, key) in models.items():
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual({field: value for field, value in schema.items()
                              if field != '$schema'}, model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(fixture[key])
        self.assertEqual(fixture['relay_report']['plan_sha256'], digest(fixture['plan']))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(json.loads((
                REPO_ROOT / 'schemas/web_readonly_data_bundle_relay.schema.json').read_text())).validate({
                    **fixture['relay_report'], 'collection_authorized': True})

    def test_static_bundle_asset_mime_failure_consumes_request_without_retry(self):
        origin = self.profile.allowed_origins[0]
        self.server.asset_responses = {
            '/assets/app.js': ('text/html', b'<html>wrong MIME</html>')}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}])
        plan_sha256 = digest(plan.model_dump())
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       plan_sha256, tls_context=self.tls_context)
        relay.arm()

        def action():
            self.request({'method': 'GET', 'url': self.profile.entry_url,
                          'plan_sha256': plan_sha256, 'client_token': relay.client_token,
                          'asset_index': None})
            result = self.request({'method': 'GET', 'url': plan.assets[0].url,
                                   'plan_sha256': plan_sha256,
                                   'client_token': relay.client_token, 'asset_index': 0})
            self.assertEqual(result, {'error': 'fetch_failed'})

        outcome = self.run_relay(action, relay=relay)
        self.assertIn('error', outcome)
        self.assertEqual(self.server.paths, ['/entry', '/assets/app.js'])
        self.assertFalse(self.socket_path.exists())

    def test_static_bundle_report_schema_and_synthetic_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_static_asset_plan.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_static_bundle_relay.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         WebStaticBundleRelayReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture['relay_report'])
        self.assertEqual(fixture['relay_report']['plan_sha256'], digest(fixture['plan']))
        self.assertEqual(fixture['relay_report']['asset_response_sha256'],
                         [fixture['report']['response_sha256']])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['relay_report'], 'execution_authorized': True})

    def test_readonly_route_relay_requires_arm_for_each_exact_get(self):
        detail_url = self.profile.entry_url.replace('/entry', '/details?view=compact&page=1')
        self.server.route_bodies = {
            '/details?view=compact&page=1': SyntheticEntry.body}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        plan_sha256 = digest(plan.model_dump())
        with self.assertRaisesRegex(ValueError, 'exact_plan_confirmation'):
            ExactReadOnlyRouteRelay(self.profiles, self.draft.task, plan, '0' * 64)
        relay = ExactReadOnlyRouteRelay(self.profiles, self.draft.task, plan,
                                        plan_sha256, tls_context=self.tls_context)
        with self.assertRaisesRegex(ValueError, 'private_data_socket_required'):
            relay.serve(self.socket_path, wait_seconds=self.draft.task.max_seconds + 1)
        replies = []

        def request(url, **changes):
            return self.request({'method': 'GET', 'url': url, 'plan_sha256': plan_sha256,
                                 'client_token': relay.client_token, **changes})

        def action():
            replies.append(request(self.profile.entry_url))
            self.assertEqual(self.server.paths, [])
            relay.arm(0)
            replies.append(request(detail_url))
            replies.append(request(self.profile.entry_url))
            self.assertEqual(relay.wait_for_route(0).route_index, 0)
            replies.append(request(self.profile.entry_url))
            self.assertEqual(self.server.paths, ['/entry'])
            relay.arm(1)
            replies.append(request(detail_url, method='POST'))
            replies.append(request(detail_url, client_token='0' * 64))
            replies.append(request(detail_url.replace('page=1', 'page=2')))
            replies.append(request(detail_url))
            self.assertEqual(relay.wait_for_route(1).route_index, 1)
            with self.assertRaisesRegex(ValueError, 'next_exact_route'):
                relay.arm(1)

        outcome = self.run_relay(action, relay=relay)
        self.assertNotIn('error', outcome)
        self.assertEqual([item.get('error') for item in replies],
                         ['denied', 'denied', None, 'denied', 'denied', 'denied',
                          'denied', None])
        self.assertEqual(self.server.paths, ['/entry', '/details?view=compact&page=1'])
        self.assertEqual(len(outcome['report']), 2)
        self.assertEqual([report.route_index for report in relay.reports], [0, 1])
        self.assertFalse(self.socket_path.exists())
        with self.assertRaisesRegex(ValueError, 'cannot_restart'):
            relay.serve(self.socket_path, wait_seconds=1)

    def test_readonly_route_relay_stop_closes_socket_without_get(self):
        plan = plan_web_readonly_routes(
            self.profiles, self.draft.task,
            [self.profile.entry_url, self.profile.entry_url.replace('/entry', '/details')])
        relay = ExactReadOnlyRouteRelay(self.profiles, self.draft.task, plan,
                                        digest(plan.model_dump()), tls_context=self.tls_context)
        stopped = threading.Event()
        outcome = {}

        def worker():
            try:
                relay.serve(self.socket_path, wait_seconds=3, stop_event=stopped)
            except Exception as error:
                outcome['error'] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        for _attempt in range(200):
            if self.socket_path.exists():
                break
            time.sleep(0.01)
        self.assertTrue(self.socket_path.exists())
        stopped.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome['error'], TimeoutError)
        with self.assertRaises(TimeoutError):
            relay.wait_for_route(0, timeout=0.1)
        with self.assertRaisesRegex(ValueError, 'next_exact_route'):
            relay.arm(0)
        self.assertFalse(self.socket_path.exists())
        self.assertEqual(self.server.paths, [])

    def test_duplicate_json_and_changed_profile_fail_before_fetch(self):
        replies = []
        def duplicate_action():
            request = self.valid_request()
            payload = ('{"method":"GET","method":"POST","url":' + json.dumps(request['url'])
                       + ',"binding_sha256":' + json.dumps(request['binding_sha256']) + '}\n').encode()
            replies.append(self.raw_request(payload))
            replies.append(self.request(self.valid_request()))

        outcome = self.run_relay(duplicate_action)
        self.assertEqual(replies[0], {'error': 'denied'})
        self.assertEqual(replies[1]['status'], 200)
        self.assertEqual(outcome['report'].request_attempts, 2)

        relay = ExactEntryRelay(self.profiles, self.checksum, self.checksum,
                                self.draft, self.draft.binding_sha256,
                                tls_context=self.tls_context)
        (self.profiles.root / (self.checksum + '.json')).chmod(0o644)
        outcome = self.run_relay(lambda: replies.append(self.request(self.valid_request(relay))),
                                 relay=relay)
        self.assertEqual(replies[-1], {'error': 'fetch_failed'})
        self.assertIsInstance(outcome['error'], ValueError)
        self.assertEqual(self.server.paths, ['/entry'])

    def test_client_token_is_unique_to_one_relay_and_never_a_binding_hash(self):
        fresh = ExactEntryRelay(self.profiles, self.checksum, self.checksum,
                                self.draft, self.draft.binding_sha256,
                                tls_context=self.tls_context)
        self.assertNotEqual(fresh.client_token, self.relay().client_token)
        self.assertNotEqual(fresh.client_token, self.draft.binding_sha256)
        replies = []

        def action():
            replies.append(self.request(self.valid_request()))
            replies.append(self.request(self.valid_request(fresh)))

        outcome = self.run_relay(action, relay=fresh)
        self.assertEqual(replies[0], {'error': 'denied'})
        self.assertEqual(replies[1]['status'], 200)
        self.assertEqual(outcome['report'].request_attempts, 2)
        self.assertEqual(self.server.paths, ['/entry'])

    def test_confirmation_and_socket_scope_fail_before_network(self):
        with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
            ExactEntryRelay(self.profiles, self.checksum, '0' * 64, self.draft,
                            self.draft.binding_sha256)
        with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
            ExactEntryRelay(self.profiles, self.checksum, self.checksum, self.draft,
                            '0' * 64)
        with self.assertRaisesRegex(ValueError, 'private_data_socket_required'):
            self.relay().serve(Path('/tmp/aos-relay.sock'), wait_seconds=1)
        self.socket_path.write_text('occupied')
        with self.assertRaises(OSError):
            self.relay().serve(self.socket_path, wait_seconds=1)
        self.assertEqual(self.socket_path.read_text(), 'occupied')
        self.assertEqual(self.server.paths, [])

    def test_denied_attempt_budget_and_tls_failure_never_serve_body(self):
        replies = []
        def denied_action():
            for _attempt in range(8):
                replies.append(self.request({**self.valid_request(), 'method': 'POST'}))

        outcome = self.run_relay(denied_action)
        self.assertEqual(replies, [{'error': 'denied'}] * 8)
        self.assertIsInstance(outcome['error'], TimeoutError)
        self.assertEqual(self.server.paths, [])
        self.assertFalse(self.socket_path.exists())

        untrusted = ExactEntryRelay(self.profiles, self.checksum, self.checksum,
                                    self.draft, self.draft.binding_sha256,
                                    tls_context=ssl.create_default_context())
        outcome = self.run_relay(lambda: replies.append(self.request(self.valid_request(untrusted))),
                                 relay=untrusted)
        self.assertEqual(replies[-1], {'error': 'fetch_failed'})
        self.assertIsInstance(outcome['error'], ssl.SSLCertVerificationError)
        self.assertEqual(self.server.paths, [])
        self.assertFalse(self.socket_path.exists())

    def test_synthetic_report_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_https_relay.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_https_relay.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        WebHTTPSRelayReport.model_validate(fixture['report'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'execution_authorized': True})

    def test_remote_observation_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_remote_entry_observation.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_remote_entry_observation.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['observation'])
        RemoteEntryObservation.model_validate(fixture['observation'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['observation'], 'cookie': 'secret'})

    def test_remote_route_evidence_contract(self):
        observation_fixture = json.loads(
            (REPO_ROOT / 'examples/web_remote_route_observation.json').read_text())
        observation_schema = json.loads(
            (REPO_ROOT / 'schemas/web_remote_route_observation.schema.json').read_text())
        jsonschema.Draft202012Validator(observation_schema).validate(observation_fixture['observation'])
        RemoteRouteObservation.model_validate(observation_fixture['observation'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(observation_schema).validate({
                **observation_fixture['observation'], 'planned_link_indices': [1, 1]})
        fixture = json.loads((REPO_ROOT / 'examples/remote_route_evidence.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_route_evidence.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        legacy_report = json.loads(json.dumps(fixture['report']))
        for field in ('planned_link_indices', 'unregistered_link_count',
                      'links_truncated', 'link_inventory_readback_matched'):
            legacy_report['routes'][0].pop(field)
        jsonschema.Draft202012Validator(schema).validate(legacy_report)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'task_retrieval_authorized': True})
        database = self.root / 'empty-routes.sqlite'
        store = TrajectoryStore(database)
        store.close()
        with self.assertRaises(ValueError):
            review_remote_route_evidence(
                database, 'missing-run', profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256='a' * 64)
        alias = self.root / 'alias-routes.sqlite'
        alias.symlink_to(database)
        with self.assertRaises(ValueError):
            review_remote_route_evidence(
                alias, 'missing-run', profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256='a' * 64)

    def test_remote_route_change_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_route_change.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_route_change.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        self.assertIsNone(complete_link_sample_sha256({
            'planned_link_indices': [1], 'unregistered_link_count': 0,
            'links_truncated': True}))
        self.assertIsNone(complete_link_sample_sha256({}))
        legacy_report = json.loads(json.dumps(fixture['report']))
        for route in legacy_report['routes']:
            route.pop('sampled_link_inventory_changed')
            route.pop('link_sample_sha256')
            route.pop('planned_link_indices')
        jsonschema.Draft202012Validator(schema).validate(legacy_report)
        missing_indices = json.loads(json.dumps(fixture['report']))
        missing_indices['routes'][0].pop('planned_link_indices')
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(missing_indices)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'task_retrieval_authorized': True})
        with self.assertRaises(ValueError):
            compare_remote_route_change(
                self.root / 'missing.sqlite', 'same-run', 'same-run', profiles=self.profiles.root,
                selected_profile_sha256=self.checksum, selected_plan_sha256='a' * 64)

    def test_remote_page_draft_seed_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_page_draft_seed.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_page_draft_seed.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         RemotePageDraftSeedReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        self.assertFalse(fixture['report']['semantic_page_key_verified'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'execution_authorized': True})
        registration = json.loads((REPO_ROOT / 'examples/remote_page_draft_registration.json').read_text())
        registration_schema = json.loads((REPO_ROOT / 'schemas/remote_page_draft_registration.schema.json').read_text())
        self.assertTrue(registration['synthetic'])
        self.assertEqual({key: value for key, value in registration_schema.items() if key != '$schema'},
                         RemotePageDraftRegistrationReport.model_json_schema())
        jsonschema.Draft202012Validator(registration_schema).validate(registration['report'])
        self.assertFalse(registration['report']['reviewed'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(registration_schema).validate({**registration['report'],
                'task_retrieval_authorized': True})
        candidate_preview = json.loads((REPO_ROOT / 'examples/remote_route_knowledge_ui_preview.json').read_text())
        candidate_schema = json.loads((REPO_ROOT / 'schemas/remote_route_knowledge_ui_preview.schema.json').read_text())
        self.assertTrue(candidate_preview['synthetic'])
        self.assertEqual({key: value for key, value in candidate_schema.items() if key != '$schema'},
                         RemoteRouteKnowledgeCandidatePreview.model_json_schema())
        jsonschema.Draft202012Validator(candidate_schema).validate(candidate_preview['preview'])
        self.assertEqual(RemoteRouteKnowledgeCandidatePreview.model_validate(
            candidate_preview['preview']).candidate_sha256,
            digest(candidate_preview['preview']['candidate']))
        with self.assertRaises(ValueError):
            RemoteRouteKnowledgeCandidatePreview.model_validate({**candidate_preview['preview'],
                'candidate_sha256': '0' * 64})

    def test_remote_navigation_graph_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_navigation_graph.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_navigation_graph.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator = jsonschema.Draft202012Validator(schema)
        validator.validate(fixture['report'])
        for invalid in ({**fixture['report'], 'task_retrieval_authorized': True},
                        {**fixture['report'], 'origin_verified': True},
                        {**fixture['report'], 'raw_url': 'https://example.invalid'}):
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(invalid)
        with self.assertRaises(ValueError):
            preview_remote_navigation_graph(
                self.root / 'missing.sqlite', 'same-run', 'same-run',
                profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                selected_plan_sha256='a' * 64)

    def test_remote_route_knowledge_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_route_knowledge.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_route_knowledge.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        mapped_report = {**fixture['report'], 'link_sample_sha256': 'a' * 64,
                         'outgoing_route_indices': [1],
                         'outgoing_knowledge_sha256': ['b' * 64]}
        jsonschema.Draft202012Validator(schema).validate(mapped_report)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **mapped_report, 'outgoing_route_indices': []})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['report'], 'link_sample_sha256': 'a' * 64})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **mapped_report, 'outgoing_knowledge_sha256': []})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'task_retrieval_authorized': True})
        with self.assertRaises(ValueError):
            preview_remote_route_knowledge(
                self.root / 'missing.sqlite', 'before', 'after', profiles=self.profiles.root,
                store=self.root / 'site-knowledge', knowledge_sha256='a' * 64,
                selected_profile_sha256=self.checksum, selected_plan_sha256='b' * 64,
                route_index=True)

    def test_remote_route_knowledge_review_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_route_knowledge_review.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, key in (('remote_route_knowledge_review', 'record'),
                          ('remote_route_knowledge_review_receipt', 'receipt'),
                          ('remote_route_knowledge_live_pin', 'live_pin'),
                          ('remote_route_knowledge_live_event', 'live_event'),
                          ('remote_route_knowledge_reuse', 'reuse')):
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator(schema).validate(fixture[key])
        live_event_schema = json.loads((REPO_ROOT / 'schemas/remote_route_knowledge_live_event.schema.json').read_text())
        jsonschema.Draft202012Validator(live_event_schema).validate({
            **fixture['live_event'], 'status': 'stale',
            'stale_reason': 'target_fingerprint_changed'})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(live_event_schema).validate({
                **fixture['live_event'], 'stale_reason': 'unverified_success'})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(live_event_schema).validate({
                **fixture['live_event'], 'stale_reason': 'target_fingerprint_changed'})
        with self.assertRaises(ValueError):
            LiveRemoteRouteKnowledgePin.model_validate({
                **fixture['live_pin'], 'outgoing_page_keys': ['details']})
        pin_schema = json.loads((REPO_ROOT / 'schemas/remote_route_knowledge_live_pin.schema.json').read_text())
        mapped_pin = {**fixture['live_pin'], 'outgoing_page_keys': ['entry'],
                      'link_sample_sha256': 'a' * 64,
                      'outgoing_route_indices': [0],
                      'outgoing_fingerprint_sha256': ['b' * 64]}
        self.assertEqual(LiveRemoteRouteKnowledgePin.model_validate(
            mapped_pin).outgoing_route_indices, [0])
        self.assertIsNone(reviewed_route_context(
            LiveRemoteRouteKnowledgePin.model_validate(mapped_pin), set()))
        self.assertEqual(reviewed_route_context(
            LiveRemoteRouteKnowledgePin.model_validate(mapped_pin), {0})['page_key'],
            fixture['live_pin']['page_key'])
        multi_target_pin = LiveRemoteRouteKnowledgePin.model_validate({
            **mapped_pin, 'outgoing_page_keys': ['entry', 'third'],
            'outgoing_route_indices': [0, 2],
            'outgoing_fingerprint_sha256': ['b' * 64, 'c' * 64]})
        self.assertIsNone(reviewed_route_context(multi_target_pin, {0}))
        self.assertIsNone(reviewed_route_context(multi_target_pin, {2}))
        self.assertEqual(reviewed_route_context(multi_target_pin, {0, 2})['page_key'],
                         fixture['live_pin']['page_key'])
        self.assertEqual(reviewed_route_context(
            LiveRemoteRouteKnowledgePin.model_validate(fixture['live_pin']), set())['page_key'],
            fixture['live_pin']['page_key'])
        jsonschema.Draft202012Validator(pin_schema).validate(mapped_pin)
        with self.assertRaises(ValueError):
            LiveRemoteRouteKnowledgePin.model_validate({
                **mapped_pin, 'outgoing_fingerprint_sha256': []})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(pin_schema).validate({
                **fixture['live_pin'], 'outgoing_page_keys': ['details']})
        self.assertEqual(fixture['receipt']['review_sha256'], digest(fixture['record']))
        review_store = RemoteRouteKnowledgeReviewStore(self.root / 'route-reviews')
        record = review_store.register(RemoteRouteKnowledgeReview.model_validate(fixture['record']))
        self.assertEqual(record, fixture['receipt']['review_sha256'])
        self.assertEqual(review_store.get(record).model_dump(), fixture['record'])
        with self.assertRaises(ValueError):
            RemoteRouteKnowledgeReviewStore(self.root.parent.parent / 'outside-review')
        review_file = review_store.root / (record + '.json')
        review_file.chmod(0o644)
        with self.assertRaises(ValueError):
            review_store.get(record)
        review_file.chmod(0o600)
        review_file.write_bytes(review_file.read_bytes().replace(b'synthetic-run-b',
                                                                b'synthetic-run-c'))
        with self.assertRaisesRegex(ValueError, 'content_changed'):
            review_store.get(record)

    def test_https_form_transport_contract(self):
        field_fixture = json.loads((REPO_ROOT / 'examples/web_https_form_fields.json').read_text())
        field_schema = json.loads((REPO_ROOT / 'schemas/web_https_form_fields.schema.json').read_text())
        self.assertTrue(field_fixture['synthetic'])
        jsonschema.Draft202012Validator(field_schema).validate(field_fixture['configuration'])
        configuration = field_fixture['configuration']
        self.assertEqual(configuration['fields'][-1],
                         {'name': 'csrf_token', 'value': 'synthetic-token'})
        fields = exact_form_fields(None, None, configuration['fields'])
        self.assertEqual(hashlib.sha256(form_body(fields)).hexdigest(), configuration['body_sha256'])
        self.assertEqual(parse_form_fields_document(canonical(configuration).encode()), fields)
        with self.assertRaises(ValueError):
            parse_form_fields_document(json.dumps(configuration).encode())
        with self.assertRaises(ValueError):
            parse_form_fields_document(canonical({**configuration,
                'fields': [configuration['fields'][0]] * 2}).encode())
        with self.assertRaises(ValueError):
            exact_form_fields('message', 'hello', configuration['fields'])
        with self.assertRaises(ValueError):
            exact_form_fields(None, None, [configuration['fields'][0]] * 2)
        fixture = json.loads((REPO_ROOT / 'examples/web_https_form_transport.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for key, name in (('plan', 'web_https_form_plan'),
                          ('report', 'web_https_form_transport')):
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator(schema).validate(fixture[key])
        report_schema = json.loads((REPO_ROOT / 'schemas/web_https_form_transport.schema.json').read_text())
        for changed in ({**fixture['report'], 'submit_response_status': 307},
                        {**fixture['report'], 'submit_response_status': True},
                        {key: value for key, value in fixture['report'].items()
                         if key != 'submit_response_status'}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(report_schema).validate(changed)
        self.assertEqual(fixture['report']['plan_sha256'], digest(fixture['plan']))
        self.assertEqual(fixture['plan']['body_sha256'], hashlib.sha256(b'message=hello').hexdigest())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(json.loads((
                REPO_ROOT / 'schemas/web_https_form_plan.schema.json').read_text())).validate(
                    {**fixture['plan'], 'execution_authorized': True})
        with self.assertRaises(ValueError):
            plan_web_https_form(
                self.profiles, self.draft.task, submit_url='https://other.invalid/submit',
                receipt_url=self.profile.allowed_origins[0] + '/receipt',
                body_sha256='a' * 64, body_bytes=13)
        with self.assertRaises(ValueError):
            plan_web_https_form(
                self.profiles, self.draft.task.model_copy(update={'max_actions': 3}),
                submit_url=self.profile.allowed_origins[0] + '/submit',
                receipt_url=self.profile.allowed_origins[0] + '/receipt',
                body_sha256='a' * 64, body_bytes=13)
        public_profiles = WebApplicationProfiles(self.root / 'public-profiles')
        public_profile = self.profile.model_copy(update={
            'entry_url': 'https://www.example.com/entry',
            'allowed_origins': ['https://www.example.com']})
        public_sha256 = profile_report(public_profile).profile_sha256
        public_profiles.register(public_profile, confirm_sha256=public_sha256)
        public_task = self.draft.task.model_copy(update={
            'profile_sha256': public_sha256, 'entry_url': public_profile.entry_url,
            'allowed_origins': public_profile.allowed_origins})
        public_plan = plan_web_https_form(
            public_profiles, public_task, submit_url='https://www.example.com/submit',
            receipt_url='https://www.example.com/receipt', body_sha256='a' * 64,
            body_bytes=13)
        with self.assertRaises(ValueError):
            ExactHTTPSFormTransport(
                public_profiles, public_task, public_plan, digest(public_plan.model_dump()),
                consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        for grant in ('0' * 64, 'not-a-plan'):
            with self.assertRaises(ValueError):
                ExactHTTPSFormTransport(
                    public_profiles, public_task, public_plan, digest(public_plan.model_dump()),
                    consume_approval=lambda _checksum: True, tls_context=self.tls_context,
                    confirm_public_plan_sha256=grant)
        transport = ExactHTTPSFormTransport(
            public_profiles, public_task, public_plan, digest(public_plan.model_dump()),
            consume_approval=lambda _checksum: False, tls_context=self.tls_context,
            confirm_public_plan_sha256=digest(public_plan.model_dump()))
        self.assertEqual(transport.public_plan_sha256, transport.plan_sha256)
        self.assertFalse(transport._entry_attempted)
        transport.public_plan_sha256 = '0' * 64
        with self.assertRaises(ValueError):
            transport.open_entry(confirm_request_sha256=digest({
                'method': 'GET', 'url': public_plan.entry_url}))
        self.assertFalse(transport._entry_attempted)
        synthetic_plan = plan_web_https_form(
            self.profiles, self.draft.task,
            submit_url=self.profile.allowed_origins[0] + '/submit',
            receipt_url=self.profile.allowed_origins[0] + '/receipt',
            body_sha256='a' * 64, body_bytes=13)
        with self.assertRaises(ValueError):
            ExactHTTPSFormTransport(
                self.profiles, self.draft.task, synthetic_plan,
                digest(synthetic_plan.model_dump()), consume_approval=lambda _checksum: True,
                tls_context=self.tls_context,
                confirm_public_plan_sha256=digest(synthetic_plan.model_dump()))

    def test_https_form_transport_three_separate_approvals_and_no_retry(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        plan_sha256 = digest(plan.model_dump())
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})
        approvals = []

        def consume_approval(request_sha256):
            approvals.append(request_sha256)
            return True

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaises(ValueError):
                ExactHTTPSFormTransport(self.profiles, self.draft.task, plan, '0' * 64,
                                        consume_approval=consume_approval,
                                        tls_context=self.tls_context)
            transport = ExactHTTPSFormTransport(
                self.profiles, self.draft.task, plan, plan_sha256,
                consume_approval=consume_approval, tls_context=self.tls_context)
            with self.assertRaises(ValueError):
                transport.submit(body, confirm_request_sha256=submit_request)
            with self.assertRaises(ValueError):
                transport.open_entry(confirm_request_sha256='0' * 64)
            self.assertEqual(self.server.paths, [])
            self.assertEqual(approvals, [])
            entry = transport.open_entry(confirm_request_sha256=entry_request)
            self.assertIn(b'Synthetic entry', entry)
            with self.assertRaises(ValueError):
                transport.open_entry(confirm_request_sha256=entry_request)
            with self.assertRaises(ValueError):
                transport.submit(b'message=other', confirm_request_sha256=submit_request)
            self.assertEqual(self.server.posts, [])
            transport.submit(body, confirm_request_sha256=submit_request)
            with self.assertRaises(ValueError):
                transport.submit(body, confirm_request_sha256=submit_request)
            report, receipt = transport.read_receipt(confirm_request_sha256=receipt_request)
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(approvals, [entry_request, submit_request, receipt_request])
            self.assertEqual(report.submit_request_sha256, submit_request)
            self.assertEqual(report.submit_response_status, 303)
            self.assertEqual(report.receipt_response_sha256, hashlib.sha256(receipt).hexdigest())
            self.assertFalse(report.site_outcome_verified)
            self.assertNotIn(body.decode(), canonical(report.model_dump()))
            with self.assertRaises(ValueError):
                transport.read_receipt(confirm_request_sha256=receipt_request)

    def test_https_form_transport_accepts_exact_302_without_following_it(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        self.server.post_status = 302
        approvals = []

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport = ExactHTTPSFormTransport(
                self.profiles, self.draft.task, plan, digest(plan.model_dump()),
                consume_approval=lambda checksum: approvals.append(checksum) or True,
                tls_context=self.tls_context)
            transport.open_entry(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.entry_url}))
            transport.submit(body, confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url,
                'body_sha256': plan.body_sha256}))
            self.assertEqual(self.server.paths, ['/entry'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            with self.assertRaises(ValueError):
                transport.submit(body, confirm_request_sha256=digest({
                    'method': 'POST', 'url': plan.submit_url,
                    'body_sha256': plan.body_sha256}))
            report, receipt = transport.read_receipt(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.receipt_url}))
        self.assertEqual(self.server.paths, ['/entry', '/receipt'])
        self.assertEqual(len(approvals), 3)
        self.assertEqual(report.submit_response_status, 302)
        self.assertEqual(report.receipt_response_sha256, hashlib.sha256(receipt).hexdigest())
        self.assertFalse(report.site_outcome_verified)

    def test_https_form_transport_rejects_307_without_replay(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        self.server.post_status = 307

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport = ExactHTTPSFormTransport(
                self.profiles, self.draft.task, plan, digest(plan.model_dump()),
                consume_approval=lambda _checksum: True, tls_context=self.tls_context)
            transport.open_entry(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.entry_url}))
            with self.assertRaisesRegex(ValueError, 'unexpected_submit_response'):
                transport.submit(body, confirm_request_sha256=digest({
                    'method': 'POST', 'url': plan.submit_url,
                    'body_sha256': plan.body_sha256}))
            with self.assertRaises(ValueError):
                transport.submit(body, confirm_request_sha256=digest({
                    'method': 'POST', 'url': plan.submit_url,
                    'body_sha256': plan.body_sha256}))
            with self.assertRaises(ValueError):
                transport.read_receipt(confirm_request_sha256=digest({
                    'method': 'GET', 'url': plan.receipt_url}))
        self.assertEqual(self.server.paths, ['/entry'])
        self.assertEqual(self.server.posts, [('/submit', body)])

    def test_public_form_cookie_is_exact_and_sent_only_for_approved_host_stages(self):
        from aos.web_https_preflight import reject_reflected_cookie, validate_https_cookie_header

        for invalid in ('session=one\r\nX-Other: injected', 'session="quoted"',
                        'session=one;session=two', 'session=one; session=two; session=three',
                        'session=one; session=two; session=two', 'session=one\n'):
            with self.assertRaises(ValueError):
                validate_https_cookie_header(invalid)
        cookie = 'session=synthetic-secret-123; role=tester'
        cookie_sha256 = hashlib.sha256(cookie.encode('ascii')).hexdigest()
        origin = f'https://www.aos-form.example.com:{self.server.server_port}'
        public_profile = self.profile.model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profile_sha256 = profile_report(public_profile).profile_sha256
        profiles = WebApplicationProfiles(self.root / 'cookie-profiles')
        profiles.register(public_profile, confirm_sha256=profile_sha256)
        task = self.draft.task.model_copy(update={
            'profile_sha256': profile_sha256, 'entry_url': public_profile.entry_url,
            'allowed_origins': public_profile.allowed_origins})
        body = b'message=hello'
        plan = plan_web_https_form(
            profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt',
            body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
        plan_sha256 = digest(plan.model_dump())
        before = b'<html><h1>Empty cookie state</h1></html>'
        after = b'<html><h1>Saved cookie state</h1></html>'
        self.server.state_bodies = {False: before, True: after}
        self.server.cookies = []
        approvals = set()

        def consume_approval(request_sha256):
            return request_sha256 in approvals

        for grant in (None, '0' * 64):
            with self.assertRaises(ValueError):
                ExactHTTPSFormTransport(
                    profiles, task, plan, plan_sha256,
                    consume_approval=consume_approval, tls_context=self.tls_context,
                    confirm_public_plan_sha256=plan_sha256,
                    cookie_header=cookie, confirm_cookie_sha256=grant)
        with self.assertRaises(ValueError):
            ExactHTTPSFormTransport(
                profiles, task, plan, plan_sha256,
                consume_approval=consume_approval, tls_context=self.tls_context,
                confirm_public_plan_sha256=plan_sha256,
                cookie_header=cookie + '\r\nX-Other: injected',
                confirm_cookie_sha256=cookie_sha256)
        transport = ExactHTTPSFormTransport(
            profiles, task, plan, plan_sha256,
            consume_approval=consume_approval, tls_context=self.tls_context,
            confirm_public_plan_sha256=plan_sha256,
            cookie_header=cookie, confirm_cookie_sha256=cookie_sha256)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest())
        state_sha256 = digest(state_plan.model_dump())
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, state_sha256,
            consume_approval=consume_approval,
            confirm_public_state_plan_sha256=state_sha256)
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'approval_denied'):
                transport.open_entry(confirm_request_sha256=entry_request)
            self.assertEqual(self.server.cookies, [])
            approvals.add(entry_request)
            transport.open_entry(confirm_request_sha256=entry_request)
            approvals.add(form_state_request_sha256(state_plan, 'before'))
            probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            approvals.add(submit_request)
            transport.submit(body, confirm_request_sha256=submit_request)
            approvals.add(receipt_request)
            report, _receipt = transport.read_receipt(confirm_request_sha256=receipt_request)
            approvals.add(form_state_request_sha256(state_plan, 'after'))
            state_report = probe.observe_after(
                confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
        self.assertEqual(self.server.cookies, [
            ('/entry', cookie), ('/state', cookie), ('/submit', cookie),
            ('/receipt', cookie), ('/state', cookie)])
        self.assertFalse(report.site_outcome_verified)
        self.assertFalse(state_report.site_outcome_verified)
        self.assertNotIn(cookie, canonical(report.model_dump()))
        self.assertNotIn(cookie, canonical(state_report.model_dump()))

        for reflected in (b'synthetic-secret-123', b'session%3Dsynthetic-secret-123',
                          b'role=tester'):
            with self.assertRaisesRegex(ValueError, 'https_probe_cookie_reflected'):
                reject_reflected_cookie(b'<html>' + reflected + b'</html>', cookie)
        for reflected in (b'abc&def', b'abc&amp;def', b'abc%26def'):
            with self.assertRaisesRegex(ValueError, 'https_probe_cookie_reflected'):
                reject_reflected_cookie(b'<html>' + reflected + b'</html>', 'session=abc&def')
        reject_reflected_cookie(b'<html>Unrelated response</html>', cookie)
        reject_reflected_cookie(cookie.encode(), None)

        self.server.route_bodies = {'/entry': b'<html>session=synthetic-secret-123</html>'}
        guarded_entry = ExactHTTPSFormTransport(
            profiles, task, plan, plan_sha256,
            consume_approval=consume_approval, tls_context=self.tls_context,
            confirm_public_plan_sha256=plan_sha256,
            cookie_header=cookie, confirm_cookie_sha256=cookie_sha256)
        posts_before = len(self.server.posts)
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'https_probe_cookie_reflected'):
                guarded_entry.open_entry(confirm_request_sha256=entry_request)
        self.assertEqual(len(self.server.posts), posts_before)
        with self.assertRaisesRegex(ValueError, 'https_form_entry_is_one_shot'):
            guarded_entry.open_entry(confirm_request_sha256=entry_request)

        self.server.route_bodies = {'/entry': b'<html>Safe entry</html>',
                                    '/receipt': b'<html>synthetic-secret-123</html>'}
        guarded_receipt = ExactHTTPSFormTransport(
            profiles, task, plan, plan_sha256,
            consume_approval=consume_approval, tls_context=self.tls_context,
            confirm_public_plan_sha256=plan_sha256,
            cookie_header=cookie, confirm_cookie_sha256=cookie_sha256)
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            guarded_receipt.open_entry(confirm_request_sha256=entry_request)
            guarded_receipt.submit(body, confirm_request_sha256=submit_request)
            with self.assertRaisesRegex(ValueError, 'https_probe_cookie_reflected'):
                guarded_receipt.read_receipt(confirm_request_sha256=receipt_request)
        self.assertEqual(len(self.server.posts), posts_before + 1)
        with self.assertRaisesRegex(ValueError, 'https_form_requires_one_submit_before_receipt'):
            guarded_receipt.read_receipt(confirm_request_sha256=receipt_request)

        self.server.state_bodies = {False: b'<html>synthetic-secret-123</html>', True: after}
        self.server.posts.clear()
        guarded_state = ExactHTTPSFormTransport(
            profiles, task, plan, plan_sha256,
            consume_approval=consume_approval, tls_context=self.tls_context,
            confirm_public_plan_sha256=plan_sha256,
            cookie_header=cookie, confirm_cookie_sha256=cookie_sha256)
        guarded_probe = ExactHTTPSFormStateProbe(
            guarded_state, state_plan, state_sha256,
            consume_approval=consume_approval,
            confirm_public_state_plan_sha256=state_sha256)
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            guarded_state.open_entry(confirm_request_sha256=entry_request)
            with self.assertRaisesRegex(ValueError, 'https_probe_cookie_reflected'):
                guarded_probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
        self.assertEqual(self.server.posts, [])

    def test_https_form_state_probe_requires_order_and_two_distinct_approvals(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1>Empty synthetic state</h1></html>'
        after = b'<html><h1>Saved synthetic state</h1></html>'
        self.server.state_bodies = {False: before, True: after}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        approvals = []
        allowed = set()

        def consume_approval(request_sha256):
            approvals.append(request_sha256)
            return request_sha256 in allowed

        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=consume_approval, tls_context=self.tls_context)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest())
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=consume_approval)
        before_request = form_state_request_sha256(state_plan, 'before')
        after_request = form_state_request_sha256(state_plan, 'after')
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'requires_entry'):
                probe.observe_before(confirm_request_sha256=before_request)
            with self.assertRaisesRegex(ValueError, 'exact_before'):
                probe.observe_before(confirm_request_sha256='0' * 64)
            self.assertEqual(self.server.paths, [])
            allowed.add(entry_request)
            transport.open_entry(confirm_request_sha256=entry_request)
            with self.assertRaisesRegex(ValueError, 'approval_denied'):
                probe.observe_before(confirm_request_sha256=before_request)
            self.assertEqual(self.server.paths, ['/entry'])
            allowed.add(before_request)
            probe.observe_before(confirm_request_sha256=before_request)
            with self.assertRaisesRegex(ValueError, 'requires_entry'):
                probe.observe_before(confirm_request_sha256=before_request)
            with self.assertRaisesRegex(ValueError, 'requires_receipt'):
                probe.observe_after(confirm_request_sha256=after_request)
            allowed.add(submit_request)
            transport.submit(body, confirm_request_sha256=submit_request)
            self.assertEqual(self.server.posts, [('/submit', body)])
            with self.assertRaisesRegex(ValueError, 'requires_receipt'):
                probe.observe_after(confirm_request_sha256=after_request)
            allowed.add(receipt_request)
            transport_report, _receipt = transport.read_receipt(
                confirm_request_sha256=receipt_request)
            with self.assertRaisesRegex(ValueError, 'approval_denied'):
                probe.observe_after(confirm_request_sha256=after_request)
            self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt'])
            allowed.add(after_request)
            report = probe.observe_after(confirm_request_sha256=after_request)
            self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt', '/state'])
            self.assertEqual(report.before_response_sha256, hashlib.sha256(before).hexdigest())
            self.assertEqual(report.after_response_sha256, hashlib.sha256(after).hexdigest())
            self.assertEqual(report.receipt_response_sha256,
                             transport_report.receipt_response_sha256)
            self.assertTrue(report.declared_response_transition_observed)
            self.assertFalse(report.site_outcome_verified)
            self.assertFalse(report.account_verified)
            self.assertNotIn(origin, canonical(report.model_dump()))
            with self.assertRaisesRegex(ValueError, 'requires_receipt'):
                probe.observe_after(confirm_request_sha256=after_request)
        self.assertEqual(approvals.count(before_request), 2)
        self.assertEqual(approvals.count(after_request), 2)
        self.assertNotEqual(before_request, after_request)

    def test_https_form_state_marker_is_exact_and_contentless(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        before = b'<html><p id="outcome">Draft empty</p></html>'
        after = b'<html><p id="outcome">Draft saved</p></html>'
        self.server.state_bodies = {False: before, True: after}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest(),
            marker_id='outcome',
            expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
            expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'))
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=lambda _checksum: True)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport.open_entry(confirm_request_sha256=digest({'method': 'GET', 'url': plan.entry_url}))
            probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            transport.submit(body, confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
            transport.read_receipt(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.receipt_url}))
            report = probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
        self.assertEqual(report.before_marker_sha256, form_state_marker_sha256(before, 'outcome'))
        self.assertEqual(report.after_marker_sha256, form_state_marker_sha256(after, 'outcome'))
        self.assertTrue(report.declared_marker_transition_observed)
        self.assertFalse(report.site_outcome_verified)
        self.assertNotIn('Draft saved', canonical(report.model_dump()))
        jsonschema.validate(state_plan.model_dump(), json.loads(
            (REPO_ROOT / 'schemas/web_https_form_state_plan.schema.json').read_text()))
        jsonschema.validate(report.model_dump(), json.loads(
            (REPO_ROOT / 'schemas/web_https_form_state_report.schema.json').read_text()))

    def test_https_form_state_marker_can_match_submitted_field_without_claiming_site_outcome(self):
        fields = exact_form_fields('message', 'hello')
        body = form_body(fields)
        origin = self.profile.allowed_origins[0]
        before = b'<html><p id="outcome">pending</p></html>'
        after = b'<html><p id="outcome">hello</p></html>'
        self.server.state_bodies = {False: before, True: after}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        with self.assertRaisesRegex(ValueError, 'submitted_field_requires_marker'):
            plan_web_https_form_state(
                transport, state_url=origin + '/state',
                expected_before_sha256=hashlib.sha256(before).hexdigest(),
                expected_after_sha256=hashlib.sha256(after).hexdigest(),
                submitted_field_name='message')
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest(),
            marker_id='outcome',
            expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
            expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'),
            submitted_field_name='message')
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=lambda _checksum: True)
        with self.assertRaisesRegex(ValueError, 'value_differs_from_after_marker'):
            verify_submitted_field_binding(state_plan, plan, exact_form_fields('message', 'wrong'))
        with self.assertRaisesRegex(ValueError, 'value_differs_from_after_marker'):
            probe.bind_submitted_fields(exact_form_fields('message', 'wrong'))

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport.open_entry(confirm_request_sha256=digest({'method': 'GET', 'url': plan.entry_url}))
            with self.assertRaisesRegex(ValueError, 'requires_entry_before_submit'):
                probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            self.assertEqual(self.server.posts, [])
            probe.bind_submitted_fields(fields)
            probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            transport.submit(body, confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
            transport.read_receipt(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.receipt_url}))
            report = probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
        self.assertTrue(report.submitted_value_readback_verified)
        self.assertEqual(report.submitted_field_name_sha256, digest({'field_name': 'message'}))
        self.assertFalse(report.site_outcome_verified)
        self.assertNotIn('hello', canonical(report.model_dump()))
        self.assertEqual(self.server.posts, [('/submit', body)])
        jsonschema.validate(state_plan.model_dump(), json.loads(
            (REPO_ROOT / 'schemas/web_https_form_state_plan.schema.json').read_text()))
        jsonschema.validate(report.model_dump(), json.loads(
            (REPO_ROOT / 'schemas/web_https_form_state_report.schema.json').read_text()))

    def test_https_form_state_marker_rejects_partial_duplicate_and_wrong_text(self):
        for html in (b'<p>No marker</p>',
                     b'<p id="outcome">one</p><p id="outcome">two</p>',
                     b'<p id="outcome">unterminated',
                     b'<p id="outcome">wrong close</div>',
                     b'<p id="outcome" id="outcome">duplicate attr</p>'):
            with self.assertRaisesRegex(ValueError, 'marker_not_unique'):
                form_state_marker_sha256(html, 'outcome')
        self.assertEqual(form_state_marker_sha256(
            b'<p id="outcome">Expected<br> state</p>', 'outcome'),
            form_state_marker_sha256(
                b'<p id="outcome">Expected state</p>', 'outcome'))
        self.assertNotEqual(form_state_marker_sha256(
            b'<p id="outcome">Wrong state</p>', 'outcome'),
            form_state_marker_sha256(b'<p id="outcome">Expected state</p>', 'outcome'))
        fixture = json.loads((REPO_ROOT / 'examples/web_https_form_state.json').read_text())
        self.assertNotIn('marker_id', WebHTTPSFormStatePlan.model_validate(fixture['plan']).model_dump())
        with self.assertRaisesRegex(ValueError, 'marker_requires_complete'):
            WebHTTPSFormStatePlan.model_validate({**fixture['plan'], 'marker_id': 'outcome'})

    def test_https_form_state_marker_mismatch_consumes_after_read(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        before = b'<html><p id="outcome">Draft empty</p></html>'
        after = b'<html><p id="outcome">Draft saved</p></html>'
        self.server.state_bodies = {False: before, True: after}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest(),
            marker_id='outcome',
            expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
            expected_after_marker_sha256=form_state_marker_sha256(
                b'<p id="outcome">Wrong state</p>', 'outcome'))
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=lambda _checksum: True)
        action = form_state_action_arguments(state_plan, 'a' * 64, 'after')
        self.assertEqual(action['expected_marker_sha256'], state_plan.expected_after_marker_sha256)
        self.assertNotIn('Wrong state', canonical(action))

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport.open_entry(confirm_request_sha256=digest({'method': 'GET', 'url': plan.entry_url}))
            probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            transport.submit(body, confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
            transport.read_receipt(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.receipt_url}))
            with self.assertRaisesRegex(ValueError, 'unexpected_after_marker'):
                probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
            with self.assertRaisesRegex(ValueError, 'requires_receipt'):
                probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
        self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt', '/state'])
        self.assertEqual(self.server.posts, [('/submit', body)])

    def test_https_form_state_probe_rejects_unbound_and_changed_state(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1>Empty state</h1></html>'
        after = b'<html><h1>Saved state</h1></html>'
        self.server.state_bodies = {False: before, True: after}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        arguments = {'state_url': origin + '/state',
                     'expected_before_sha256': hashlib.sha256(before).hexdigest(),
                     'expected_after_sha256': hashlib.sha256(after).hexdigest()}
        for state_url in (plan.entry_url, plan.submit_url, plan.receipt_url,
                          'https://other.invalid/state'):
            with self.assertRaises(ValueError):
                plan_web_https_form_state(transport, **{**arguments, 'state_url': state_url})
        with self.assertRaises(ValueError):
            plan_web_https_form_state(transport, **{**arguments,
                'expected_after_sha256': arguments['expected_before_sha256']})
        state_plan = plan_web_https_form_state(transport, **arguments)
        with self.assertRaisesRegex(ValueError, 'exact_plan_and_approval_gate'):
            ExactHTTPSFormStateProbe(transport, state_plan, '0' * 64,
                                    consume_approval=lambda _checksum: True)
        with self.assertRaisesRegex(ValueError, 'exact_plan_and_approval_gate'):
            ExactHTTPSFormStateProbe(transport,
                state_plan.model_copy(update={'expected_after_sha256': '0' * 64}),
                digest(state_plan.model_dump()), consume_approval=lambda _checksum: True)
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=lambda _checksum: True)
        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport.open_entry(confirm_request_sha256=digest({'method': 'GET', 'url': plan.entry_url}))
            probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
            transport.submit(body, confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
            transport.read_receipt(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.receipt_url}))
            self.server.state_bodies[True] = b'<html><h1>Other state</h1></html>'
            with self.assertRaisesRegex(ValueError, 'unexpected_after_response'):
                probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
            with self.assertRaisesRegex(ValueError, 'requires_receipt'):
                probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
        self.assertEqual(self.server.posts, [('/submit', body)])
        self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt', '/state'])

    def test_https_form_state_fixture_and_public_plan_grant(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_https_form_state.json').read_text())
        state_plan = WebHTTPSFormStatePlan.model_validate(fixture['plan'])
        state_report = WebHTTPSFormStateReport.model_validate(fixture['report'])
        self.assertEqual(state_report.state_plan_sha256, digest(state_plan.model_dump()))
        self.assertFalse(state_report.site_outcome_verified)
        public_origin = f'https://www.aos-form.example.com:{self.server.server_port}'
        public_profile = self.profile.model_copy(update={
            'entry_url': public_origin + '/entry', 'allowed_origins': [public_origin]})
        public_profiles = WebApplicationProfiles(self.root / 'public-state-profiles')
        public_sha256 = profile_report(public_profile).profile_sha256
        public_profiles.register(public_profile, confirm_sha256=public_sha256)
        public_task = self.draft.task.model_copy(update={
            'profile_sha256': public_sha256, 'entry_url': public_profile.entry_url,
            'allowed_origins': [public_origin]})
        form_plan = plan_web_https_form(
            public_profiles, public_task, submit_url=public_origin + '/submit',
            receipt_url=public_origin + '/receipt',
            body_sha256=hashlib.sha256(b'message=hello').hexdigest(), body_bytes=13)
        transport = ExactHTTPSFormTransport(
            public_profiles, public_task, form_plan, digest(form_plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context,
            confirm_public_plan_sha256=digest(form_plan.model_dump()))
        public_state_plan = plan_web_https_form_state(
            transport, state_url=public_origin + '/state',
            expected_before_sha256='a' * 64, expected_after_sha256='b' * 64)
        state_sha256 = digest(public_state_plan.model_dump())
        for grant in (None, '0' * 64):
            with self.assertRaisesRegex(ValueError, 'public_target_requires_exact_plan_grant'):
                ExactHTTPSFormStateProbe(
                    transport, public_state_plan, state_sha256,
                    consume_approval=lambda _checksum: True,
                    confirm_public_state_plan_sha256=grant)
        probe = ExactHTTPSFormStateProbe(
            transport, public_state_plan, state_sha256,
            consume_approval=lambda _checksum: True,
            confirm_public_state_plan_sha256=state_sha256)
        self.assertEqual(probe.public_state_plan_sha256, state_sha256)
        probe.public_state_plan_sha256 = '0' * 64
        with self.assertRaisesRegex(ValueError, 'public_target_requires_exact_plan_grant'):
            probe._verify_public_grant()

    def test_https_form_transport_rejects_changed_valid_plan_identity(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        transport.plan = plan.model_copy(update={'receipt_url': origin + '/other-receipt'})
        with self.assertRaisesRegex(ValueError, 'plan_identity_changed'):
            transport.open_entry(confirm_request_sha256=digest({
                'method': 'GET', 'url': plan.entry_url}))
        with self.assertRaisesRegex(ValueError, 'plan_identity_changed'):
            plan_web_https_form_state(
                transport, state_url=origin + '/state',
                expected_before_sha256='a' * 64, expected_after_sha256='b' * 64)
        self.assertEqual(self.server.paths, [])
        self.assertEqual(self.server.posts, [])

    def test_https_form_state_action_policy_is_phase_and_plan_bound(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_https_form_state.json').read_text())
        plan = WebHTTPSFormStatePlan.model_validate(fixture['plan'])
        form_plan_sha256 = plan.form_plan_sha256
        state = State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                      deployment_id='deployment', owner_lease_id='lease',
                      phase=Phase.EXECUTE, task_kind='browser_remote_form',
                      authorized_path='aos://web/https-form',
                      authorized_content=form_plan_sha256)
        template = Action.model_validate(json.loads((
            REPO_ROOT / 'examples/desktop_remote_form_approval.json').read_text())['approval']['action'])
        for phase in ('before', 'after'):
            arguments = form_state_action_arguments(plan, '2' * 64, phase)
            action = template.model_copy(update={
                'task_id': state.task_id, 'run_id': state.run_id,
                'step_id': state.step_id, 'runtime_id': state.runtime_id,
                'owner_lease_id': state.owner_lease_id,
                'state_version': state.state_version,
                'tool': 'browser.form.state_' + phase,
                'arguments': arguments, 'selected_option': 'read_state_' + phase,
                'deadline': time.time() + 60})
            ToolRegistry.validate(action)
            SafetyPolicy.check(action, state, state.runtime_id)
            for changed in ({**arguments, 'phase': 'after' if phase == 'before' else 'before'},
                            {**arguments, 'url': 'http://app.example.invalid/state'},
                            {**arguments, 'request_sha256': 'invalid'},
                            {**arguments, 'value': 'private'}):
                with self.assertRaises(AOSFault):
                    ToolRegistry.validate(action.model_copy(update={'arguments': changed}))
            with self.assertRaises(AOSFault):
                SafetyPolicy.check(action, state.model_copy(update={
                    'authorized_content': '0' * 64}), state.runtime_id)
            with self.assertRaises(AOSFault):
                SafetyPolicy.check(action.model_copy(update={
                    'selected_option': 'submit_form'}), state, state.runtime_id)
            cookie_arguments = form_state_action_arguments(plan, '2' * 64, phase,
                                                          '3' * 64)
            ToolRegistry.validate(action.model_copy(update={'arguments': cookie_arguments}))
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': {
                    **cookie_arguments, 'cookie_sha256': 'not-a-hash'}}))
            marker_plan = WebHTTPSFormStatePlan.model_validate({
                **fixture['plan'], 'marker_id': 'outcome',
                'expected_before_marker_sha256': '4' * 64,
                'expected_after_marker_sha256': '5' * 64})
            marker_arguments = form_state_action_arguments(marker_plan, '2' * 64, phase)
            ToolRegistry.validate(action.model_copy(update={'arguments': marker_arguments}))
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': {
                    **marker_arguments, 'expected_marker_sha256': 'invalid'}}))
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': {
                    key: value for key, value in marker_arguments.items()
                    if key != 'marker_id_sha256'}}))
            bound_plan = WebHTTPSFormStatePlan.model_validate({
                **marker_plan.model_dump(), 'submitted_field_name': 'message'})
            bound_arguments = form_state_action_arguments(bound_plan, '2' * 64, phase)
            ToolRegistry.validate(action.model_copy(update={'arguments': bound_arguments}))
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': {
                    **bound_arguments, 'submitted_field_name_sha256': 'invalid'}}))
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': {
                    key: value for key, value in bound_arguments.items()
                    if key != 'marker_id_sha256'}}))

    def test_https_form_transport_rejects_redirect_without_replay(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        self.server.post_location = 'https://other.invalid/receipt'

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            for status in (302, 303):
                with self.subTest(status=status):
                    self.server.post_status = status
                    transport = ExactHTTPSFormTransport(
                        self.profiles, self.draft.task, plan, digest(plan.model_dump()),
                        consume_approval=lambda _checksum: True, tls_context=self.tls_context)
                    transport.open_entry(confirm_request_sha256=digest({
                        'method': 'GET', 'url': plan.entry_url}))
                    with self.assertRaises(ValueError):
                        transport.submit(body, confirm_request_sha256=submit_request)
                    with self.assertRaises(ValueError):
                        transport.submit(body, confirm_request_sha256=submit_request)
                    with self.assertRaises(ValueError):
                        transport.read_receipt(confirm_request_sha256=digest({
                            'method': 'GET', 'url': plan.receipt_url}))
        self.assertEqual(self.server.paths, ['/entry', '/entry'])
        self.assertEqual(self.server.posts, [('/submit', body), ('/submit', body)])

    def test_https_form_transport_refused_approvals_do_not_send_requests(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})
        permitted = set()

        def consume_approval(request_sha256):
            return request_sha256 in permitted

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            transport = ExactHTTPSFormTransport(
                self.profiles, self.draft.task, plan, digest(plan.model_dump()),
                consume_approval=consume_approval, tls_context=self.tls_context)
            with self.assertRaises(ValueError):
                transport.open_entry(confirm_request_sha256=entry_request)
            self.assertEqual(self.server.paths, [])
            permitted.add(entry_request)
            transport.open_entry(confirm_request_sha256=entry_request)
            with self.assertRaises(ValueError):
                transport.submit(body, confirm_request_sha256=submit_request)
            self.assertEqual(self.server.posts, [])
            permitted.add(submit_request)
            transport.submit(body, confirm_request_sha256=submit_request)
            with self.assertRaises(ValueError):
                transport.read_receipt(confirm_request_sha256=receipt_request)
            self.assertEqual(self.server.paths, ['/entry'])
            permitted.add(receipt_request)
            transport.read_receipt(confirm_request_sha256=receipt_request)
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])

    def test_https_form_relay_requires_exact_socket_requests(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        approvals = []

        def consume_approval(request_sha256):
            approvals.append(request_sha256)
            return True

        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=consume_approval, tls_context=self.tls_context)
        relay = ExactHTTPSFormRelay(transport)
        outcome = {}

        def serve():
            try:
                outcome['report'] = relay.serve(self.socket_path, wait_seconds=10)
            except Exception as error:
                outcome['error'] = error

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        def request(payload):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(self.socket_path))
                connection.sendall((canonical(payload) + '\n').encode())
                response = bytearray()
                while chunk := connection.recv(8192):
                    response.extend(chunk)
            return json.loads(response)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            for _attempt in range(200):
                if self.socket_path.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(self.socket_path.exists())
            base = {'plan_sha256': transport.plan_sha256,
                    'client_token': relay.client_token}
            self.assertEqual(request({**base, 'method': 'GET', 'url': plan.entry_url,
                                      'client_token': '0' * 64}), {'error': 'denied'})
            self.assertEqual(self.server.paths, [])
            entry = request({**base, 'method': 'GET', 'url': plan.entry_url})
            self.assertEqual(entry['status'], 200)
            self.assertEqual(base64.b64decode(entry['body_base64']), SyntheticEntry.body)
            self.assertEqual(request({**base, 'method': 'POST', 'url': plan.submit_url,
                                      'body_base64': base64.b64encode(body).decode()}),
                             {'status': 204})
            receipt = request({**base, 'method': 'GET', 'url': plan.receipt_url})
            self.assertEqual(receipt['status'], 200)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', outcome)
        self.assertEqual(outcome['report'].submit_request_sha256, approvals[1])
        self.assertEqual(len(approvals), 3)
        self.assertEqual(self.server.paths, ['/entry', '/receipt'])
        self.assertEqual(self.server.posts, [('/submit', body)])
        self.assertFalse(self.socket_path.exists())

    def test_https_form_relay_binds_through_private_directory_for_long_workspace_paths(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        relay = ExactHTTPSFormRelay(transport)
        long_parent = self.workspace / ('x' * 40)
        long_parent.mkdir(mode=0o700)
        socket_path = long_parent / 'relay.sock'
        self.assertGreater(len(str(socket_path).encode()), 107)
        stopped = threading.Event()
        outcome = {}

        def serve():
            try:
                relay.serve(socket_path, wait_seconds=10, stop_event=stopped)
            except Exception as error:
                outcome['error'] = error

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for _attempt in range(200):
            if socket_path.exists() or 'error' in outcome:
                break
            time.sleep(0.01)
        self.assertTrue(socket_path.exists(), outcome.get('error'))
        directory_fd = os.open(long_parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(f'/proc/self/fd/{directory_fd}/relay.sock')
                connection.sendall(b'{}\n')
                self.assertEqual(connection.recv(128), b'{"error":"denied"}\n')
        finally:
            os.close(directory_fd)
        stopped.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get('error'), TimeoutError)
        self.assertFalse(socket_path.exists())

    def test_https_form_relay_cancel_before_entry_cleans_socket(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: True, tls_context=self.tls_context)
        relay = ExactHTTPSFormRelay(transport)
        stopped = threading.Event()
        outcome = {}

        def serve():
            try:
                relay.serve(self.socket_path, wait_seconds=10, stop_event=stopped)
            except Exception as error:
                outcome['error'] = error

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for _attempt in range(200):
            if self.socket_path.exists():
                break
            time.sleep(0.01)
        self.assertTrue(self.socket_path.exists())
        stopped.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome['error'], TimeoutError)
        self.assertFalse(self.socket_path.exists())
        self.assertEqual(self.server.paths, [])
        self.assertEqual(self.server.posts, [])

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS form application worker test')
    def test_owned_form_worker_exact_pin_sequence_and_readback(self):
        value = 'hello@example.invalid'
        body = b'message=hello%40example.invalid'
        origin = self.profile.allowed_origins[0]
        self.server.post_status = 302
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" type="email" name="message" required>'
                       b'<button>Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})
        permitted = {entry_request}
        approvals = []

        def consume_approval(request_sha256):
            if request_sha256 not in permitted:
                return False
            approvals.append(request_sha256)
            return True

        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=consume_approval, tls_context=self.tls_context)
        relay = ExactHTTPSFormRelay(transport)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopHTTPSFormMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        with self.assertRaisesRegex(ValueError, 'live runtime pin'):
            runtime.attach_relay(relay, self.draft, field_name='message', value=value)
        draft = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        with self.assertRaises(ValueError):
            runtime.attach_relay(relay, draft, field_name='message', value='wrong')
        runtime.attach_relay(relay, draft, field_name='message', value=value)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            runtime.start()
            self.assertTrue(runtime.status()['running'])
            self.assertNotIn(relay.client_token, json.dumps(runtime.status()))
            self.assertNotIn(value, json.dumps(runtime.status()))
            self.assertNotIn(value, json.dumps(runtime.process.args))
            with self.assertRaises(AOSFault):
                runtime.perform('browser_navigate', {'url': plan.entry_url})
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.submit', {})
            self.assertEqual(self.server.paths, [])

            def arguments(stage):
                return form_stage_arguments(plan, draft.binding_sha256, 'message', stage)

            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.open',
                                {**arguments(0), 'url': plan.receipt_url})
            self.assertEqual(self.server.paths, [])

            opened = runtime.perform('browser.form.open', arguments(0))
            self.assertEqual(opened['url'], plan.entry_url)
            self.assertEqual(self.server.paths, ['/entry'])
            self.assertEqual(self.server.posts, [])
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.submit', arguments(2))
            self.assertEqual(runtime.perform('browser.form.fill', arguments(1)),
                             {'stage': 'filled', 'body_sha256': plan.body_sha256})
            self.assertEqual(self.server.posts, [])
            permitted.add(submit_request)
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.submit',
                                {**arguments(2), 'body_sha256': '0' * 64})
            self.assertEqual(self.server.posts, [])
            self.assertEqual(runtime.perform('browser.form.submit', arguments(2)),
                             {'stage': 'submitted', 'body_sha256': plan.body_sha256})
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(self.server.paths, ['/entry'])
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.submit', arguments(2))
            permitted.add(receipt_request)
            receipt = runtime.perform('browser.form.receipt', arguments(3))
            self.assertEqual(receipt['url'], plan.receipt_url)
            self.assertNotEqual(opened['heading_sha256'], receipt['heading_sha256'])
            self.assertEqual(runtime.perform('browser.form.observe', arguments(4)), receipt)
            self.assertEqual(approvals, [entry_request, submit_request, receipt_request])
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(runtime.relay_report.submit_response_status, 302)
            runtime.stop()
            self.assertFalse(self.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS form state worker test')
    def test_owned_form_worker_two_host_state_reads_are_separate_from_browser_gate(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1>Empty synthetic state</h1></html>'
        after = b'<html><h1>Saved synthetic state</h1></html>'
        self.server.state_bodies = {False: before, True: after}
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        form_plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        permitted = set()

        def consume_approval(request_sha256):
            if request_sha256 not in permitted:
                return False
            permitted.remove(request_sha256)
            return True

        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, form_plan, digest(form_plan.model_dump()),
            consume_approval=consume_approval, tls_context=self.tls_context)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest())
        probe = ExactHTTPSFormStateProbe(
            transport, state_plan, digest(state_plan.model_dump()),
            consume_approval=consume_approval)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopHTTPSFormMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        draft = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        runtime.attach_relay(ExactHTTPSFormRelay(transport), draft,
                             field_name='message', value='hello')
        runtime.attach_state_probe(probe)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            runtime.start()
            form_arguments = lambda stage: form_stage_arguments(
                form_plan, draft.binding_sha256, 'message', stage)
            state_arguments = lambda phase: form_state_action_arguments(
                state_plan, draft.binding_sha256, phase)
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.state_before', state_arguments('before'))
            self.assertEqual(self.server.paths, [])
            permitted.add(digest({'method': 'GET', 'url': form_plan.entry_url}))
            runtime.perform('browser.form.open', form_arguments(0))
            with self.assertRaises(AOSFault):
                runtime.perform('browser.form.state_after', state_arguments('after'))
            permitted.add(form_state_request_sha256(state_plan, 'before'))
            baseline = runtime.perform('browser.form.state_before', state_arguments('before'))
            self.assertEqual(baseline['response_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(self.server.paths, ['/entry', '/state'])
            runtime.perform('browser.form.fill', form_arguments(1))
            permitted.add(digest({'method': 'POST', 'url': form_plan.submit_url,
                                  'body_sha256': form_plan.body_sha256}))
            runtime.perform('browser.form.submit', form_arguments(2))
            permitted.add(digest({'method': 'GET', 'url': form_plan.receipt_url}))
            runtime.perform('browser.form.receipt', form_arguments(3))
            runtime.perform('browser.form.observe', form_arguments(4))
            permitted.add(form_state_request_sha256(state_plan, 'after'))
            result = runtime.perform('browser.form.state_after', state_arguments('after'))
            self.assertEqual(result['after_response_sha256'], hashlib.sha256(after).hexdigest())
            self.assertFalse(result['site_outcome_verified'])
            self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt', '/state'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(permitted, set())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned public-like HTTPS form worker test')
    def test_owned_public_grant_form_worker_uses_exact_three_requests(self):
        origin = f'https://www.aos-form.example.com:{self.server.server_port}'
        profiles = WebApplicationProfiles(self.root / 'public-worker-profiles')
        profile = self.profile.model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task = self.draft.task.model_copy(update={
            'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
            'allowed_origins': profile.allowed_origins})
        body = b'message=hello'
        self.server.route_bodies = {
            '/entry': (b'<html><title>Public-like form</title><h1>Entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})
        permitted = {entry_request, submit_request, receipt_request}

        def consume_approval(request_sha256):
            if request_sha256 not in permitted:
                return False
            permitted.remove(request_sha256)
            return True

        plan_sha256 = digest(plan.model_dump())
        transport = ExactHTTPSFormTransport(
            profiles, task, plan, plan_sha256, consume_approval=consume_approval,
            tls_context=self.tls_context, confirm_public_plan_sha256=plan_sha256)
        relay = ExactHTTPSFormRelay(transport)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopHTTPSFormMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        draft = bind_web_task(profiles, task, runtime.runtime_pin())
        runtime.attach_relay(relay, draft, field_name='message', value='hello')

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            runtime.start()
            self.assertEqual(runtime.evidence['public_plan_sha256'], plan_sha256)
            self.assertEqual(runtime.perform('browser.form.open',
                             form_stage_arguments(plan, draft.binding_sha256, 'message', 0))['url'],
                             plan.entry_url)
            self.assertEqual(runtime.perform('browser.form.fill',
                             form_stage_arguments(plan, draft.binding_sha256, 'message', 1))['stage'],
                             'filled')
            self.assertEqual(runtime.perform('browser.form.submit',
                             form_stage_arguments(plan, draft.binding_sha256, 'message', 2))['stage'],
                             'submitted')
            self.assertEqual(runtime.perform('browser.form.receipt',
                             form_stage_arguments(plan, draft.binding_sha256, 'message', 3))['url'],
                             plan.receipt_url)
            self.assertEqual(runtime.perform('browser.form.observe',
                             form_stage_arguments(plan, draft.binding_sha256, 'message', 4))['url'],
                             plan.receipt_url)
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(permitted, set())
            self.assertNotIn('hello', json.dumps(runtime.status()))
            runtime.stop()
            self.assertFalse(self.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned backend remote-entry pin startup test')
    def test_owned_backend_advertises_exact_confirmed_remote_entry(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        task_file = self.root / 'private-task.json'
        content = canonical(self.draft.task.model_dump(mode='json')).encode()
        task_file.write_bytes(content)
        task_file.chmod(0o600)
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url,
                                         self.profile.entry_url.replace('/entry', '/details')])
        plan_file = self.root / 'private-routes.json'
        plan_content = canonical(plan.model_dump(mode='json')).encode()
        plan_file.write_bytes(plan_content)
        plan_file.chmod(0o600)
        console = self.root / 'console'
        console.mkdir(mode=0o700)
        arguments = ('--browser-tasks', '--desktop-browser', '--desktop-vision',
                     '--vision-engine', 'fixture',
                     '--desktop-navigation-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--remote-entry-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--web-profiles-root', str(self.profiles.root),
                     '--remote-entry-profile-sha256', self.checksum,
                     '--remote-entry-task-file', str(task_file),
                     '--remote-entry-task-sha256', hashlib.sha256(content).hexdigest(),
                     '--remote-routes-plan-file', str(plan_file),
                     '--remote-routes-plan-sha256', hashlib.sha256(plan_content).hexdigest())
        with task_server(console, 'fixture', arguments) as (origin, token, client, _server):
            status = client.get('/api/tasks').json()
            self.assertIn('browser_remote_entry', status['kinds'])
            self.assertIn('browser_remote_routes', status['kinds'])
            self.assertEqual(status['remote_entry']['profile_sha256'], self.checksum)
            self.assertEqual(status['remote_entry']['entry_url'], self.profile.entry_url)
            self.assertEqual(status['remote_entry']['mode'], 'one_shot_read_only')
            self.assertEqual(status['remote_routes']['plan_sha256'], digest(plan.model_dump()))
            self.assertEqual(status['remote_routes']['route_count'], 2)
            self.assertFalse(status['real_model'])
            self.assertEqual(self.server.paths, [])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_routes')
                    expect(page.get_by_test_id('remote-routes-scope')).to_contain_text(
                        digest(plan.model_dump()))
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_role('button', name='Start route reading', exact=True)).to_be_enabled()
                    self.assertEqual(self.server.paths, [])
                finally:
                    browser.close()

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned static bundle backend and UI approval test')
    def test_owned_backend_static_bundle_ui_start_and_reject_without_fetch(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        origin = self.profile.allowed_origins[0]
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js', 'content_type': 'application/javascript'},
            {'url': origin + '/assets/site.css', 'content_type': 'text/css'}])
        task_file = self.root / 'static-ui-task.json'
        task_content = canonical(self.draft.task.model_dump(mode='json')).encode()
        task_file.write_bytes(task_content)
        task_file.chmod(0o600)
        plan_file = self.root / 'static-ui-plan.json'
        plan_content = canonical(plan.model_dump(mode='json')).encode()
        plan_file.write_bytes(plan_content)
        plan_file.chmod(0o600)
        console = self.root / 'static-ui-console'
        console.mkdir(mode=0o700)
        arguments = ('--browser-tasks', '--desktop-browser',
                     '--remote-entry-mcp-manifest',
                     str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--web-profiles-root', str(self.profiles.root),
                     '--remote-entry-profile-sha256', self.checksum,
                     '--remote-entry-task-file', str(task_file),
                     '--remote-entry-task-sha256', hashlib.sha256(task_content).hexdigest(),
                     '--remote-static-assets-plan-file', str(plan_file),
                     '--remote-static-assets-plan-sha256',
                     hashlib.sha256(plan_content).hexdigest())
        with task_server(console, 'fixture', arguments) as (ui_origin, token, client, _server):
            status = client.get('/api/tasks').json()
            self.assertIn('browser_remote_static_assets', status['kinds'])
            self.assertEqual(status['remote_static_assets']['plan_sha256'],
                             digest(plan.model_dump()))
            self.assertEqual(status['remote_static_assets']['assets'],
                             [asset.url for asset in plan.assets])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(ui_origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option(
                        'browser_remote_static_assets')
                    expect(page.get_by_test_id('remote-static-assets-scope')).to_contain_text(
                        plan.assets[0].url)
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    page.get_by_role('button', name='Start static bundle task', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text(
                        'browser.static.open', timeout=30000)
                    self.assertEqual(self.server.paths, [])
                    page.get_by_role('button', name='Reject', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text(
                        'cancelled', timeout=30000)
                    self.assertEqual(self.server.paths, [])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'][0]['kind'],
                                     'browser_remote_static_assets')
                finally:
                    browser.close()

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned read-only JSON backend and UI approval test')
    def test_owned_backend_readonly_json_bundle_ui_rejects_without_fetch(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        origin = self.profile.allowed_origins[0]
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}],
            [{'url': origin + '/api/summary'}])
        task_file = self.root / 'readonly-data-ui-task.json'
        task_content = canonical(self.draft.task.model_dump(mode='json')).encode()
        task_file.write_bytes(task_content)
        task_file.chmod(0o600)
        plan_file = self.root / 'readonly-data-ui-plan.json'
        plan_content = canonical(plan.model_dump(mode='json')).encode()
        plan_file.write_bytes(plan_content)
        plan_file.chmod(0o600)
        console = self.root / 'readonly-data-ui-console'
        console.mkdir(mode=0o700)
        arguments = ('--browser-tasks', '--desktop-browser',
                     '--remote-entry-mcp-manifest',
                     str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--web-profiles-root', str(self.profiles.root),
                     '--remote-entry-profile-sha256', self.checksum,
                     '--remote-entry-task-file', str(task_file),
                     '--remote-entry-task-sha256', hashlib.sha256(task_content).hexdigest(),
                     '--remote-static-assets-plan-file', str(plan_file),
                     '--remote-static-assets-plan-sha256',
                     hashlib.sha256(plan_content).hexdigest())
        with task_server(console, 'fixture', arguments) as (ui_origin, token, client, _server):
            status = client.get('/api/tasks').json()
            self.assertEqual(status['remote_static_assets']['mode'],
                             'one_shot_readonly_data_bundle')
            self.assertEqual(status['remote_static_assets']['data_resources'],
                             [origin + '/api/summary'])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(ui_origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option(
                        'browser_remote_static_assets')
                    scope = page.get_by_test_id('remote-static-assets-scope')
                    expect(scope).to_contain_text('Exact read-only JSON GET URLs:')
                    expect(scope).to_contain_text(origin + '/api/summary')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_label('Task type', exact=True)).to_contain_text(
                        'HTTPS JS/CSS/images and JSON GET bundle')
                    page.get_by_role('button', name='Start JSON bundle task', exact=True).click()
                    approval = page.get_by_test_id('approval')
                    expect(approval).to_contain_text('JSON GET URLs above', timeout=30000)
                    metadata_panel = page.get_by_test_id('remote-learning-metadata')
                    expect(metadata_panel).to_contain_text('JSON-task metadata recording')
                    expect(metadata_panel).to_contain_text('JSON content is not stored')
                    metadata_panel.get_by_label('S1 decision metadata').check()
                    metadata_panel.get_by_label(
                        'As the local operator, I attest that I have the right to collect metadata for this run').check()
                    metadata_panel.get_by_role('button', name='Preview metadata consent').click()
                    consent_sha256 = metadata_panel.get_by_test_id(
                        'remote-consent-preview').locator('code').first.inner_text()
                    self.assertRegex(consent_sha256, r'^[a-f0-9]{64}$')
                    metadata_panel.get_by_label('Confirm consent SHA-256').fill(consent_sha256)
                    metadata_panel.get_by_role('button', name='Register exact consent').click()
                    expect(metadata_panel).to_contain_text('Private consent recorded')
                    metadata_panel.get_by_role('button', name='Attach metadata recording to this run').click()
                    expect(metadata_panel).to_contain_text('Collecting')
                    self.assertEqual(self.server.paths, [])
                    self.assertEqual(self.server.paths, [])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(scope).to_contain_text('Exact salt okunur JSON GET URL’leri:')
                    expect(approval).to_contain_text('JSON GET URL’lerini kapsar')
                    expect(metadata_panel).to_contain_text('JSON görev metadata kaydı')
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text(
                        'cancelled', timeout=30000)
                    self.assertEqual(self.server.paths, [])
                finally:
                    browser.close()

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned managed public-form backend and UI advertisement test')
    def test_owned_backend_advertises_private_public_form_without_network(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        origin = 'https://www.aos-form.example.com'
        profiles = WebApplicationProfiles(self.root / 'managed-form-profiles')
        profile = self.profile.model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task = self.draft.task.model_copy(update={
            'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
            'allowed_origins': profile.allowed_origins})
        plan = plan_web_https_form(
            profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt',
            body_sha256=hashlib.sha256(b'message=form-private-992').hexdigest(),
            body_bytes=len(b'message=form-private-992'))
        task_file = self.root / 'managed-form-task.json'
        task_file.write_bytes(canonical(task.model_dump(mode='json')).encode())
        task_file.chmod(0o600)
        plan_file = self.root / 'managed-form-plan.json'
        plan_file.write_bytes(canonical(plan.model_dump(mode='json')).encode())
        plan_file.chmod(0o600)
        value_file = self.root / 'managed-form-value.txt'
        value_file.write_bytes(b'form-private-992')
        value_file.chmod(0o600)
        console = self.root / 'managed-form-console'
        console.mkdir(mode=0o700)
        arguments = ('--browser-tasks', '--desktop-browser',
                     '--remote-entry-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--web-profiles-root', str(profiles.root),
                     '--remote-entry-profile-sha256', profile_sha256,
                     '--remote-entry-task-file', str(task_file),
                     '--remote-entry-task-sha256', digest(task.model_dump()),
                     '--remote-form-plan-file', str(plan_file),
                     '--remote-form-plan-sha256', digest(plan.model_dump()),
                     '--remote-form-field-name', 'message',
                     '--remote-form-value-file', str(value_file),
                     '--remote-form-public-plan-sha256', digest(plan.model_dump()))
        with task_server(console, 'fixture', arguments) as (server_origin, token, client, _server):
            status = client.get('/api/tasks').json()
            self.assertIn('browser_remote_form', status['kinds'])
            self.assertTrue(status['supports_remote_learning_metadata'])
            self.assertEqual(status['remote_form']['mode'], 'public_explicit_one_post')
            self.assertEqual(status['remote_form']['submit_url'], plan.submit_url)
            self.assertNotIn('form-private-992', json.dumps(status))
            self.assertEqual(self.server.paths, [])
            self.assertEqual(self.server.posts, [])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(server_origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text(plan.submit_url)
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Explicit public plan grant')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    page.get_by_role('button', name='Start HTTPS form task', exact=True).click()
                    approval = page.get_by_test_id('approval')
                    expect(approval).to_contain_text('browser.form.open', timeout=30000)
                    metadata = page.get_by_test_id('remote-learning-metadata')
                    expect(metadata).to_contain_text('HTTPS form metadata recording')
                    metadata.get_by_label('S1 decision metadata').check()
                    metadata.get_by_label(
                        'As the local operator, I attest that I have the right to collect metadata for this run').check()
                    metadata.get_by_role('button', name='Preview metadata consent').click()
                    consent_sha256 = metadata.get_by_test_id(
                        'remote-consent-preview').locator('code').first.inner_text()
                    self.assertRegex(consent_sha256, r'^[a-f0-9]{64}$')
                    metadata.get_by_label('Confirm consent SHA-256').fill(consent_sha256)
                    metadata.get_by_role('button', name='Register exact consent').click()
                    metadata.get_by_role('button', name='Attach metadata recording to this run').click()
                    expect(metadata).to_contain_text('Collecting')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(metadata).to_contain_text('HTTPS form metadata kaydı')
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text(
                        'cancelled', timeout=30000)
                    self.assertEqual(self.server.paths, [])
                    self.assertEqual(self.server.posts, [])
                finally:
                    browser.close()

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned managed ordered-form backend and UI admission test')
    def test_owned_backend_advertises_ordered_private_form_without_values(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        origin = 'https://www.aos-form.example.com'
        profiles = WebApplicationProfiles(self.root / 'managed-ordered-form-profiles')
        profile = self.profile.model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task = self.draft.task.model_copy(update={
            'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
            'allowed_origins': profile.allowed_origins})
        body = b'subject=private-subject-973&message=private-message-973'
        plan = plan_web_https_form(
            profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt',
            body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
        task_file = self.root / 'managed-ordered-task.json'
        task_file.write_bytes(canonical(task.model_dump(mode='json')).encode())
        task_file.chmod(0o600)
        plan_file = self.root / 'managed-ordered-plan.json'
        plan_file.write_bytes(canonical(plan.model_dump(mode='json')).encode())
        plan_file.chmod(0o600)
        fields_document = {'schema_version': '1.0',
                           'fields': [{'name': 'subject', 'value': 'private-subject-973'},
                                      {'name': 'message', 'value': 'private-message-973'}],
                           'body_sha256': plan.body_sha256, 'body_bytes': len(body),
                           'execution_authorized': False, 'collection_authorized': False}
        fields_file = self.root / 'managed-ordered-fields.json'
        fields_content = canonical(fields_document).encode()
        fields_file.write_bytes(fields_content)
        fields_file.chmod(0o600)
        console = self.root / 'managed-ordered-console'
        console.mkdir(mode=0o700)
        arguments = ('--browser-tasks', '--desktop-browser',
                     '--remote-entry-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                     '--web-profiles-root', str(profiles.root),
                     '--remote-entry-profile-sha256', profile_sha256,
                     '--remote-entry-task-file', str(task_file),
                     '--remote-entry-task-sha256', digest(task.model_dump()),
                     '--remote-form-plan-file', str(plan_file),
                     '--remote-form-plan-sha256', digest(plan.model_dump()),
                     '--remote-form-fields-file', str(fields_file),
                     '--remote-form-fields-sha256', hashlib.sha256(fields_content).hexdigest(),
                     '--remote-form-public-plan-sha256', digest(plan.model_dump()))
        with task_server(console, 'fixture', arguments) as (server_origin, token, client, _server):
            status = client.get('/api/tasks').json()
            self.assertEqual(status['remote_form']['field_names'], ['subject', 'message'])
            self.assertEqual(status['remote_form']['mode'], 'public_explicit_one_post')
            self.assertNotIn('private-subject-973', json.dumps(status))
            self.assertNotIn('private-message-973', json.dumps(status))
            self.assertEqual(self.server.paths, [])
            self.assertEqual(self.server.posts, [])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(server_origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Fields: subject, message')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    self.assertEqual(self.server.paths, [])
                    self.assertEqual(self.server.posts, [])
                finally:
                    browser.close()

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned Ubuntu route task through unmocked console UI')
    def test_unmocked_ui_runs_two_routes_with_separate_approvals(self):
        self.unmocked_ui_routes(FixtureDecisionEngine(), False)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit owned Ubuntu route consent through unmocked console UI')
    def test_unmocked_ui_registers_and_attaches_route_consent(self):
        self.unmocked_ui_routes(FixtureDecisionEngine(), False, consent_flow=True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS',
        'AOS_REAL_BROWSER_TASK_TESTS')),
        'Explicit real Decider route consent through unmocked console UI')
    def test_real_decider_route_consent_from_unmocked_ui(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.unmocked_ui_routes(engine, True, consent_flow=True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS',
        'AOS_REAL_BROWSER_TASK_TESTS')),
        'Explicit two-run real Decider metadata continuity through unmocked UI')
    def test_real_decider_two_consented_route_runs_from_unmocked_ui(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.unmocked_ui_routes(engine, True, consent_flow=True, repeat_consent_flow=True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS')),
        'Explicit legacy-root metadata continuity through unmocked console UI')
    def test_two_consented_route_runs_preserve_legacy_root_store(self):
        self.unmocked_ui_routes(FixtureDecisionEngine(), False, consent_flow=True,
                                repeat_consent_flow=True, legacy_first=True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS',
        'AOS_REAL_BROWSER_TASK_TESTS')),
        'Explicit real Decider route task through unmocked console UI')
    def test_real_decider_two_routes_from_unmocked_ui(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.unmocked_ui_routes(engine, True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_UI_TESTS',
        'AOS_REAL_BROWSER_TASK_TESTS')),
        'Explicit real Decider route task from console private-draft writer')
    def test_real_decider_two_routes_from_registered_private_drafts(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.unmocked_ui_routes(engine, True, generated=True)

    def unmocked_ui_routes(self, engine, real_model, *, generated=False, consent_flow=False,
                           repeat_consent_flow=False, legacy_first=False):
        import uvicorn
        from playwright.sync_api import expect, sync_playwright

        detail_url = self.profile.entry_url.replace('/entry', '/details')
        self.server.route_bodies = {
            '/entry': (b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                       b'<a href="/details">Details</a></html>'),
            '/details': b'<html><title>Relay details</title><h1>Synthetic details</h1></html>'}
        task = self.draft.task
        if generated:
            task_root = self.root / 'task-drafts'
            route_root = self.root / 'route-drafts'
            routes = [self.profile.entry_url, detail_url]
            draft_origin = 'http://127.0.0.1:8765'
            draft_app = create_console(SimpleNamespace(session_id='synthetic-draft', runtime=None),
                'synthetic-token', draft_origin, self.root,
                web_profiles_root=self.profiles.root, web_task_root=task_root,
                web_route_root=route_root)

            async def create_drafts():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=draft_app),
                        base_url=draft_origin, headers={'Origin': draft_origin}) as client:
                    (await client.post('/api/login', json={
                        'token': 'synthetic-token'})).raise_for_status()
                    selection = {'profile_sha256': self.checksum, 'task_key': task.task_key,
                                 'verification_ref': 'synthetic-entry-check', 'route_count': 2}
                    task_preview = (await client.post('/api/web-applications/task-preview',
                                                      json=selection))
                    task_preview.raise_for_status()
                    task_sha256 = task_preview.json()['task_sha256']
                    task_receipt = (await client.post('/api/web-applications/task-register',
                        json={**selection, 'confirm_sha256': task_sha256}))
                    task_receipt.raise_for_status()
                    route_selection = {'profile_sha256': self.checksum,
                                       'task_sha256': task_sha256, 'routes': routes}
                    route_preview = (await client.post('/api/web-applications/routes-preview',
                                                       json=route_selection))
                    route_preview.raise_for_status()
                    plan_sha256 = route_preview.json()['plan_sha256']
                    route_receipt = (await client.post('/api/web-applications/routes-register',
                        json={**route_selection, 'confirm_sha256': plan_sha256}))
                    route_receipt.raise_for_status()
                    return (task_sha256, REPO_ROOT / task_receipt.json()['task_file'],
                            plan_sha256, REPO_ROOT / route_receipt.json()['route_plan_file'])

            task_sha256, task_file, plan_sha256, plan_file = asyncio.run(create_drafts())
            with patch('aos.local_app.WEB_PROFILES', self.profiles.root):
                self.assertEqual(local_app.prepare_remote_entry(
                    'real', self.checksum, task_file)[1], task_sha256)
                prepared_plan = local_app.prepare_remote_routes(
                    'real', self.checksum, task_file, plan_file)
                self.assertEqual(prepared_plan[1], plan_sha256)
            task = WebTaskContract.model_validate_json(local_app.private_read(task_file))
            plan = WebReadOnlyRoutePlan.model_validate_json(prepared_plan[0])
        else:
            plan = plan_web_readonly_routes(self.profiles, task,
                                            [self.profile.entry_url, detail_url])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        ready = queue.Queue(maxsize=1)
        state = {}

        def serve_console():
            desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
            store = None
            scheduler = None
            try:
                desktop.start()
                database = self.root / 'ui-routes.sqlite'
                store = TrajectoryStore(database)
                controller = DesktopController(store, desktop)
                scheduler = DesktopScheduler(
                    controller, Settings(workspace=self.workspace, database=database),
                    engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                    desktop_browser=True,
                    remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                    remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
                    remote_entry_task=task, remote_routes_plan=plan,
                    **({'remote_learning_consents_dir': self.root / 'ui-route-consents',
                        'remote_learning_stream_dir': self.root / 'ui-route-outbox'}
                       if consent_flow else {}))
                app = create_console(controller, 'synthetic-token', origin, self.root, database, scheduler,
                                     web_profiles_root=self.profiles.root,
                                     site_knowledge_root=self.root / 'site-knowledge',
                                     site_skills_root=self.root / 'site-skills',
                                     page_seed_root=self.root,
                                     route_review_root=self.root / 'ui-route-reviews')
                server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                       access_log=False, log_level='error'))
                state['server'] = server
                ready.put(None)
                server.run()
            except BaseException as error:
                state['error'] = error
                if ready.empty():
                    ready.put(error)
            finally:
                if scheduler is not None:
                    asyncio.run(scheduler.close())
                desktop.stop()
                if store is not None:
                    store.close()

        def local_connection(address, timeout=None, **kwargs):
            if address == ('8.8.8.8', self.server.server_port):
                return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)
            return self.original_connection(address, timeout=timeout, **kwargs)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            thread = threading.Thread(target=serve_console, daemon=True)
            thread.start()
            try:
                started = ready.get(timeout=30)
                if started is not None:
                    raise started
                with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=5) as client:
                    for _attempt in range(200):
                        if 'error' in state:
                            raise state['error']
                        try:
                            if client.get('/api/session').status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail('Owned console did not become ready')
                    client.post('/api/login', json={'token': 'synthetic-token'}).raise_for_status()
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=str(
                            REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                        try:
                            page = browser.new_page(viewport={'width': 1280, 'height': 900})
                            errors = []
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            page.goto(origin + '/ui/')
                            page.get_by_label('Local session token', exact=True).fill('synthetic-token')
                            page.get_by_role('button', name='Sign in', exact=True).click()
                            page.get_by_role('button', name='Tasks', exact=True).click()
                            page.get_by_label('Task type', exact=True).select_option('browser_remote_routes')
                            expect(page.get_by_test_id('approve-all')).to_be_disabled()
                            expect(page.locator('[data-testid="learning-metadata-option"] input')).to_be_disabled()

                            def register_and_attach_consent():
                                metadata = page.get_by_test_id('remote-learning-metadata')
                                expect(metadata).to_be_visible()
                                metadata.get_by_label('S1 decision metadata').check()
                                metadata.get_by_label('S2 escalation metadata').check()
                                metadata.get_by_label(
                                    'As the local operator, I attest that I have the right to collect metadata for this run').check()
                                metadata.get_by_role('button', name='Preview metadata consent').click()
                                consent_sha256 = metadata.get_by_test_id(
                                    'remote-consent-preview').locator('code').first.inner_text()
                                self.assertRegex(consent_sha256, r'^[a-f0-9]{64}$')
                                consent_file = self.root / 'ui-route-consents' / (consent_sha256 + '.json')
                                self.assertFalse(consent_file.exists())
                                metadata.get_by_label('Confirm consent SHA-256').fill(consent_sha256)
                                metadata.get_by_role('button', name='Register exact consent').click()
                                expect(metadata).to_contain_text('Private consent recorded')
                                self.assertEqual(consent_file.stat().st_mode & 0o777, 0o600)
                                metadata.get_by_role('button', name='Attach metadata recording to this run').click()
                                expect(metadata).to_contain_text('Collecting')
                                return consent_sha256

                            self.assertEqual(self.server.paths, [])
                            page.get_by_role('button', name='Start route reading', exact=True).click()
                            expect(page.get_by_test_id('approval')).to_contain_text('"route_index": 0', timeout=45000)
                            self.assertEqual(self.server.paths, [])
                            if consent_flow:
                                consent_sha256 = register_and_attach_consent()
                            page.get_by_role('button', name='Approve', exact=True).click()
                            expect(page.get_by_test_id('approval')).to_contain_text('"route_index": 1', timeout=30000)
                            self.assertEqual(self.server.paths, ['/entry'])
                            page.get_by_role('button', name='Approve', exact=True).click()
                            expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                            self.assertEqual(self.server.paths, ['/entry', '/details'])
                            job = client.get('/api/tasks').json()['jobs'][0]
                            self.assertEqual(job['kind'], 'browser_remote_routes')
                            self.assertEqual(bool(job['real_model']), real_model)
                            trace = client.get('/api/runs/' + job['run_id']).json()
                            self.assertEqual([item['result'] for item in trace['verifications']],
                                             ['passed', 'passed'])
                            self.assertEqual([(item['role'], item['status']) for item in trace['model_calls']],
                                             [('system1', 'ok')] * 2 if real_model else [])
                            if consent_flow:
                                expect(page.get_by_test_id('remote-learning-metadata')).to_contain_text('Synced')
                                self.assertEqual(RemoteLearningConsents(
                                    self.root / 'ui-route-consents').get(consent_sha256).run_id,
                                    job['run_id'])
                                self.assertEqual(job['remote_learning_metadata']['entries_by_role'],
                                                 {'system1': 2 if real_model else 0, 'system2': 0})
                                self.assertEqual(self.server.paths, ['/entry', '/details'])
                                if repeat_consent_flow:
                                    if legacy_first:
                                        outbox_root = self.root / 'ui-route-outbox'
                                        first_store = outbox_root / consent_sha256
                                        (first_store / 'remote-learning-stream.sqlite').rename(
                                            outbox_root / 'remote-learning-stream.sqlite')
                                        first_store.rmdir()
                                    page.get_by_role('button', name='Start route reading', exact=True).click()
                                    expect(page.get_by_test_id('approval')).to_contain_text(
                                        '"route_index": 0', timeout=45000)
                                    second_consent_sha256 = register_and_attach_consent()
                                    self.assertNotEqual(second_consent_sha256, consent_sha256)
                                    for route_index in (0, 1):
                                        expect(page.get_by_test_id('approval')).to_contain_text(
                                            f'"route_index": {route_index}', timeout=30000)
                                        page.get_by_role('button', name='Approve', exact=True).click()
                                    expect(page.get_by_test_id('task-row').first).to_contain_text(
                                        'succeeded', timeout=30000)
                                    second_job = client.get('/api/tasks').json()['jobs'][0]
                                    self.assertNotEqual(second_job['run_id'], job['run_id'])
                                    self.assertEqual(second_job['remote_learning_metadata']['state'], 'synced')
                                    self.assertEqual(second_job['remote_learning_metadata']['entries_by_role'],
                                                     {'system1': 2 if real_model else 0, 'system2': 0})
                                    self.assertEqual(self.server.paths,
                                                     ['/entry', '/details', '/entry', '/details'])
                                    for checksum, source_job in ((consent_sha256, job),
                                                                 (second_consent_sha256, second_job)):
                                        outbox = (self.root / 'ui-route-outbox'
                                                  if legacy_first and checksum == consent_sha256
                                                  else self.root / 'ui-route-outbox' / checksum)
                                        self.assertEqual((outbox / 'remote-learning-stream.sqlite').stat().st_mode
                                                         & 0o777, 0o600)
                                        replay = poll_remote_learning_stream(
                                            self.root / 'ui-routes.sqlite', profiles=self.profiles.root,
                                            consents=self.root / 'ui-route-consents',
                                            consent_sha256=checksum,
                                            outbox_dir=self.root / 'ui-route-outbox')
                                        self.assertEqual(replay['new_entries'], 0)
                                        self.assertEqual(replay['entries_by_role'],
                                                         {'system1': 2 if real_model else 0, 'system2': 0})
                                        self.assertEqual(RemoteLearningConsents(
                                            self.root / 'ui-route-consents').get(checksum).run_id,
                                            source_job['run_id'])
                                self.assertFalse(errors)
                                return
                            graph_panel = page.get_by_test_id('remote-navigation-graph')
                            expect(graph_panel).to_contain_text('Two completed route tasks are required')
                            page.get_by_role('button', name='Start route reading', exact=True).click()
                            for route_index in (0, 1):
                                expect(page.get_by_test_id('approval')).to_contain_text(
                                    f'"route_index": {route_index}', timeout=45000)
                                page.get_by_role('button', name='Approve', exact=True).click()
                            expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                            second_job = client.get('/api/tasks').json()['jobs'][0]
                            self.assertEqual(second_job['kind'], 'browser_remote_routes')
                            self.assertEqual(bool(second_job['real_model']), real_model)
                            second_trace = client.get('/api/runs/' + second_job['run_id']).json()
                            self.assertEqual([item['result'] for item in second_trace['verifications']],
                                             ['passed', 'passed'])
                            self.assertEqual([(item['role'], item['status'])
                                              for item in second_trace['model_calls']],
                                             [('system1', 'ok')] * 2 if real_model else [])
                            expect(graph_panel.get_by_role('button', name='Compare routes')).to_be_enabled()
                            graph_panel.get_by_role('button', name='Compare routes').click()
                            expect(page.get_by_test_id('remote-navigation-graph-result')).to_contain_text(
                                'Stable pages: 2 / 2', timeout=30000)
                            expect(page.get_by_test_id('remote-navigation-graph-result')).to_contain_text(
                                'Planned links: 1 → 2')
                            seed_panel = page.get_by_test_id('remote-page-draft-seed')
                            seed_panel.get_by_label('Page key').fill('entry')
                            seed_panel.get_by_role('button', name='Save private page draft').click()
                            expect(page.get_by_test_id('remote-page-draft-seed-result')).to_contain_text(
                                'Private draft saved:', timeout=30000)
                            self.assertTrue((self.root / self.checksum / 'entry.json').is_file())
                            self.assertEqual((self.root / self.checksum / 'entry.json').stat().st_mode & 0o777, 0o600)
                            self.assertFalse((self.root / 'site-knowledge').exists())
                            register_button = page.get_by_role('button', name='Register confirmed draft')
                            expect(register_button).to_be_disabled()
                            page.get_by_label('Confirm draft SHA-256').fill('0' * 64)
                            expect(register_button).to_be_disabled()
                            seeded_page = SitePageDraft.model_validate_json(
                                (self.root / self.checksum / 'entry.json').read_bytes())
                            page.get_by_label('Confirm draft SHA-256').fill(digest(seeded_page.model_dump()))
                            expect(register_button).to_be_enabled()
                            page.get_by_role('button', name='Türkçe', exact=True).click()
                            expect(page.get_by_test_id('remote-navigation-graph-result')).to_contain_text(
                                'Sabit sayfalar: 2 / 2')
                            expect(page.get_by_test_id('remote-navigation-graph-result')).to_contain_text(
                                'Planlı bağlantılar: 1 → 2')
                            expect(page.get_by_test_id('remote-page-draft-seed-result')).to_contain_text(
                                'Özel taslak kaydedildi:')
                            page.get_by_role('button', name='Onaylanan taslağı kaydet').click()
                            expect(page.get_by_test_id('remote-page-draft-registration-result')).to_contain_text(
                                'İncelenmemiş taslak kaydedildi:', timeout=30000)
                            self.assertEqual(SiteKnowledgeStore(
                                self.root / 'site-knowledge', self.profiles).get(
                                    digest(seeded_page.model_dump())), seeded_page)
                            if real_model:
                                source_database = self.root / 'ui-routes.sqlite'
                                with sqlite3.connect(source_database) as source:
                                    first_decision = source.execute(
                                        "SELECT decision_id FROM actions WHERE run_id=? "
                                        "AND tool='browser.remote.route' "
                                        "AND json_extract(arguments_json,'$.route_index')=0",
                                        (job['run_id'],)).fetchone()[0]
                                events = review_learning_events(source_database, job['run_id'])['events']
                                source_event = next(event for event in events
                                                    if event['source']['decision_id'] == first_decision)
                                skill = SiteSkillDraft(
                                    schema_version='1.0', profile_sha256=self.checksum,
                                    application_key=self.profile.application_key,
                                    tenant_key=self.profile.tenant_key,
                                    account_role=self.profile.account_role,
                                    task_key=self.draft.task.task_key, page_key='entry',
                                    page_draft_sha256=digest(seeded_page.model_dump()),
                                    skill_key='entry-route-choice', model_role='system1',
                                    candidate_kind='finite_action_choice', revision=1,
                                    previous_sha256=None, parameter_keys=[],
                                    precondition_keys=[], step_keys=['open-entry'],
                                    expected_outcome_key='entry-readback',
                                    source_event_ids=[source_event['event_id']],
                                    source_verification_ids=[], source_kind='manual_candidate',
                                    status='draft', execution_authorized=False,
                                    collection_authorized=False, training_ready=False,
                                    activation_authorized=False)
                                skill_store = SiteSkillStore(
                                    self.root / 'site-skills', self.profiles,
                                    SiteKnowledgeStore(self.root / 'site-knowledge', self.profiles))
                                skill_sha256 = digest(skill.model_dump())
                                skill_store.register(skill, confirm_sha256=skill_sha256)
                                supervisor_skill = SiteSkillDraft.model_validate({
                                    **skill.model_dump(), 'skill_key': 'entry-route-plan',
                                    'model_role': 'system2', 'candidate_kind': 'workflow_plan',
                                    'source_event_ids': ['learning-' + 'f' * 64]})
                                supervisor_sha256 = digest(supervisor_skill.model_dump())
                                skill_store.register(supervisor_skill, confirm_sha256=supervisor_sha256)
                                skill_panel = page.get_by_test_id('remote-skill-source')
                                skill_button = skill_panel.get_by_role(
                                    'button', name='Skill kaynağını incele')
                                expect(skill_button).to_be_disabled()
                                skill_panel.get_by_label('Kaynak koşu').select_option(job['run_id'])
                                skill_panel.get_by_role('button', name='Özel S1 taslaklarını listele').click()
                                skill_panel.get_by_label('Kayıtlı S1 taslağı').select_option(skill_sha256)
                                expect(skill_panel.get_by_label('Skill SHA-256')).to_have_value(skill_sha256)
                                expect(skill_button).to_be_enabled()
                                skill_button.click()
                                expect(page.get_by_test_id('remote-skill-source-result')).to_contain_text(
                                    skill_sha256, timeout=30000)
                                expect(page.get_by_test_id('remote-skill-source-result')).to_contain_text(
                                    'Yalnız HTTPS taşıma/readback doğrulandı.')
                                supervisor_panel = skill_panel.get_by_test_id('remote-s2-skill-drafts')
                                supervisor_panel.get_by_role('button', name='Özel S2 taslaklarını listele').click()
                                expect(supervisor_panel).to_contain_text(supervisor_sha256)
                                expect(supervisor_panel).to_contain_text('Rota görevi Bonsai çağırmaz')
                                expect(skill_panel.get_by_label('Skill SHA-256')).to_have_value(skill_sha256)
                                page.get_by_role('button', name='English', exact=True).click()
                                expect(page.get_by_test_id('remote-skill-source-result')).to_contain_text(
                                    'Only HTTPS transport/readback was verified.')
                                expect(supervisor_panel).to_contain_text('Route tasks do not call Bonsai')
                                page.get_by_role('button', name='Türkçe', exact=True).click()
                                self.assertEqual(self.server.paths,
                                                 ['/entry', '/details', '/entry', '/details'])
                            page.get_by_role('button', name='Metadata adayını önizle').click()
                            candidate_panel = page.get_by_test_id('remote-page-knowledge-candidate')
                            expect(candidate_panel).to_contain_text('Metadata adayı:', timeout=30000)
                            candidate_hash = client.post('/api/tasks/page-knowledge-preview', json={
                                'before_run_id': job['run_id'], 'after_run_id': second_job['run_id'],
                                'route_index': 0, 'page_key': 'entry',
                                'knowledge_sha256': digest(seeded_page.model_dump())}).json()['candidate_sha256']
                            review_button = candidate_panel.get_by_role('button', name='Metadata incelemesini kaydet')
                            expect(review_button).to_be_disabled()
                            candidate_panel.get_by_label('Aday SHA-256 onayı').fill('0' * 64)
                            candidate_panel.get_by_label('Yalnız metadata incelemesi olduğunu kabul ediyorum').check()
                            expect(review_button).to_be_disabled()
                            candidate_panel.get_by_label('Aday SHA-256 onayı').fill(candidate_hash)
                            expect(review_button).to_be_enabled()
                            review_button.click()
                            expect(page.get_by_test_id('remote-page-knowledge-review-result')).to_contain_text(
                                'Metadata inceleme kaydı:', timeout=30000)
                            self.assertTrue((self.root / 'ui-route-reviews').is_dir())
                            self.assertEqual(len(list((self.root / 'ui-route-reviews').glob('*.json'))), 1)
                            self.assertEqual(self.server.paths,
                                             ['/entry', '/details', '/entry', '/details'])
                            self.assertEqual(errors, [])
                        finally:
                            browser.close()
            finally:
                if 'server' in state:
                    state['server'].should_exit = True
                thread.join(30)
                self.assertFalse(thread.is_alive())
        if 'error' in state:
            raise state['error']

    def test_remote_static_assets_migration_is_bound_and_append_only(self):
        store = TrajectoryStore(self.root / 'static-binding.sqlite')
        self.addCleanup(store.close)
        created = '2026-09-24T00:00:00Z'
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': self.profile.allowed_origins[0] + '/assets/app.js',
             'content_type': 'application/javascript'}])
        self.assertEqual(store.connection.execute(
            'SELECT max(version) FROM schema_migrations').fetchone()[0], 17)
        with store.connection:
            store.insert('tasks', task_id='task', original_goal='synthetic',
                         normalized_goal='synthetic', success_criteria_json='[]',
                         workspace_scope_json='[]', created_at=created)
            store.insert('runs', run_id='run', task_id='task', status='running',
                         policy_version='browser-remote-static-assets-policy-v1',
                         environment_json='{}', deployment_snapshot_json='{}',
                         started_at=created)
            store.insert('desktop_sessions', session_id='session', runtime_id='desktop',
                         image_id='image', owner='AGENT', lease_id='lease', generation=0,
                         status='running', created_at=created, updated_at=created)
            store.insert('desktop_tasks', job_id='job', session_id='session', run_id='run',
                         kind='browser_remote_static_assets', lease_id='lease', generation=0,
                         status='running', real_model=0, created_at=created, updated_at=created,
                         runtime_id=self.draft.runtime.runtime_id)
            binding = {'job_id': 'job', 'run_id': 'run',
                       'profile_sha256': self.checksum,
                       'binding_sha256': self.draft.binding_sha256,
                       'runtime_sha256': self.draft.runtime_sha256,
                       'plan_sha256': digest(plan.model_dump()),
                       'draft_json': canonical(self.draft.model_dump(mode='json')),
                       'plan_json': canonical(plan.model_dump(mode='json')),
                       'browser_runtime_id': self.draft.runtime.runtime_id,
                       'created_at': created}
            store.insert('desktop_remote_static_asset_bindings', **binding)
        for statement in (
                "UPDATE desktop_remote_static_asset_bindings SET binding_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_static_asset_bindings',
                "UPDATE desktop_tasks SET runtime_id='changed' WHERE job_id='job'",
                "UPDATE desktop_tasks SET kind='browser_remote_entry' WHERE job_id='job'"):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert('desktop_remote_static_asset_bindings', **binding)
        store.connection.rollback()
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_remote_entry_migration_is_bound_and_append_only(self):
        store = TrajectoryStore(self.root / 'binding.sqlite')
        self.addCleanup(store.close)
        created = '2026-09-23T00:00:00Z'
        self.assertEqual(store.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 17)
        with store.connection:
            store.insert('tasks', task_id='task', original_goal='synthetic', normalized_goal='synthetic',
                         success_criteria_json='[]', workspace_scope_json='[]', created_at=created)
            store.insert('runs', run_id='run', task_id='task', status='running',
                         policy_version='browser-remote-entry-policy-v1', environment_json='{}',
                         deployment_snapshot_json='{}', started_at=created)
            store.insert('desktop_sessions', session_id='session', runtime_id='desktop', image_id='image',
                         owner='AGENT', lease_id='lease', generation=0, status='running',
                         created_at=created, updated_at=created)
            store.insert('desktop_tasks', job_id='job', session_id='session', run_id='run',
                         kind='browser_remote_entry', lease_id='lease', generation=0,
                         status='running', real_model=0, created_at=created, updated_at=created,
                         runtime_id=self.draft.runtime.runtime_id)
            binding = {'job_id': 'job', 'run_id': 'run', 'profile_sha256': self.checksum,
                       'binding_sha256': self.draft.binding_sha256,
                       'runtime_sha256': self.draft.runtime_sha256,
                       'draft_json': canonical(self.draft.model_dump(mode='json')),
                       'browser_runtime_id': self.draft.runtime.runtime_id, 'created_at': created}
            store.insert('desktop_remote_entry_bindings', **binding)
        for statement in (
                "UPDATE desktop_remote_entry_bindings SET binding_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_entry_bindings',
                "UPDATE desktop_tasks SET runtime_id='changed' WHERE job_id='job'"):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_v11_upgrade_preserves_bound_job_and_approval(self):
        database = self.root / 'legacy-binding.sqlite'
        connection = sqlite3.connect(database)
        migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
        for migration in migrations[:11]:
            connection.executescript(migration.read_text())
        created = '2026-09-23T00:00:00Z'
        connection.execute("INSERT INTO tasks(task_id,original_goal,normalized_goal,success_criteria_json,workspace_scope_json,created_at) VALUES('task','synthetic','synthetic','[]','[]',?)", (created,))
        connection.execute("INSERT INTO runs(run_id,task_id,status,policy_version,environment_json,deployment_snapshot_json,started_at) VALUES('run','task','succeeded','browser-staging-workflow-policy-v1','{}','{}',?)", (created,))
        connection.execute("INSERT INTO desktop_sessions VALUES('session','desktop','image','AGENT','lease',0,'running',?,?)", (created, created))
        connection.execute("INSERT INTO desktop_tasks VALUES('job','session','run','browser_staging_workflow','lease',0,'succeeded',0,?,?,'browser')", (created, created))
        connection.execute("INSERT INTO desktop_approvals VALUES('approval','job','{}',?,123,'consumed',?,?)", ('0' * 64, created, created))
        pin = {'profile_sha256': self.checksum, 'task_key': 'synthetic-staging-workflow',
               'network_mode': 'none', 'collection_authorized': False}
        connection.execute("INSERT INTO desktop_web_profile_bindings VALUES('job','run',?,?,?,?,?,?)",
                           (self.checksum, 'synthetic-staging-workflow', canonical(pin), '0' * 64,
                            'browser', created))
        connection.commit()
        connection.close()
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        self.assertEqual(store.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 17)
        self.assertEqual(store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'consumed')
        self.assertEqual(store.connection.execute('SELECT profile_sha256 FROM desktop_web_profile_bindings').fetchone()[0], self.checksum)
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE desktop_tasks SET runtime_id='changed' WHERE job_id='job'")
        store.connection.rollback()

    def test_remote_route_migration_is_bound_and_append_only(self):
        store = TrajectoryStore(self.root / 'route-binding.sqlite')
        self.addCleanup(store.close)
        created = '2026-09-23T00:00:00Z'
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url,
                                         self.profile.entry_url.replace('/entry', '/details')])
        with store.connection:
            store.insert('tasks', task_id='task', original_goal='synthetic', normalized_goal='synthetic',
                         success_criteria_json='[]', workspace_scope_json='[]', created_at=created)
            store.insert('runs', run_id='run', task_id='task', status='running',
                         policy_version='browser-remote-routes-policy-v1', environment_json='{}',
                         deployment_snapshot_json='{}', started_at=created)
            store.insert('desktop_sessions', session_id='session', runtime_id='desktop', image_id='image',
                         owner='AGENT', lease_id='lease', generation=0, status='running',
                         created_at=created, updated_at=created)
            store.insert('desktop_tasks', job_id='job', session_id='session', run_id='run',
                         kind='browser_remote_routes', lease_id='lease', generation=0,
                         status='running', real_model=0, created_at=created, updated_at=created,
                         runtime_id=self.draft.runtime.runtime_id)
            store.insert('desktop_remote_route_bindings', job_id='job', run_id='run',
                         profile_sha256=self.checksum, binding_sha256=self.draft.binding_sha256,
                         runtime_sha256=self.draft.runtime_sha256,
                         plan_sha256=digest(plan.model_dump()),
                         draft_json=canonical(self.draft.model_dump(mode='json')),
                         plan_json=canonical(plan.model_dump(mode='json')),
                         browser_runtime_id=self.draft.runtime.runtime_id, created_at=created)
        for statement in (
                "UPDATE desktop_remote_route_bindings SET plan_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_route_bindings',
                "UPDATE desktop_tasks SET kind='browser_remote_entry' WHERE job_id='job'"):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_remote_form_migration_is_bound_and_append_only(self):
        store = TrajectoryStore(self.root / 'form-binding.sqlite')
        self.addCleanup(store.close)
        created = '2026-09-23T00:00:00Z'
        body = b'message=synthetic'
        origin = self.profile.allowed_origins[0]
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=lambda _checksum: False, tls_context=self.tls_context)
        state_plan = plan_web_https_form_state(
            transport, state_url=origin + '/state',
            expected_before_sha256='a' * 64,
            expected_after_sha256='b' * 64)
        with store.connection:
            store.insert('tasks', task_id='task', original_goal='synthetic',
                         normalized_goal='synthetic', success_criteria_json='[]',
                         workspace_scope_json='[]', created_at=created)
            store.insert('runs', run_id='run', task_id='task', status='running',
                         policy_version='browser-remote-form-policy-v1', environment_json='{}',
                         deployment_snapshot_json='{}', started_at=created)
            store.insert('desktop_sessions', session_id='session', runtime_id='desktop',
                         image_id='image', owner='AGENT', lease_id='lease', generation=0,
                         status='running', created_at=created, updated_at=created)
            store.insert('desktop_tasks', job_id='job', session_id='session', run_id='run',
                         kind='browser_remote_form', lease_id='lease', generation=0,
                         status='running', real_model=0, created_at=created, updated_at=created,
                         runtime_id=self.draft.runtime.runtime_id)
            store.insert('desktop_remote_form_bindings', job_id='job', run_id='run',
                         profile_sha256=self.checksum,
                         binding_sha256=self.draft.binding_sha256,
                         runtime_sha256=self.draft.runtime_sha256,
                         plan_sha256=digest(plan.model_dump()),
                         draft_json=canonical(self.draft.model_dump(mode='json')),
                         plan_json=canonical(plan.model_dump(mode='json')),
                         browser_runtime_id=self.draft.runtime.runtime_id, created_at=created)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert('desktop_remote_form_state_bindings',
                         job_id='job', run_id='run', form_plan_sha256='0' * 64,
                         state_plan_sha256=digest(state_plan.model_dump()),
                         state_plan_json=canonical(state_plan.model_dump(mode='json')),
                         browser_runtime_id=self.draft.runtime.runtime_id,
                         created_at=created)
        store.connection.rollback()
        with store.connection:
            store.insert('desktop_remote_form_state_bindings',
                         job_id='job', run_id='run', form_plan_sha256=digest(plan.model_dump()),
                         state_plan_sha256=digest(state_plan.model_dump()),
                         state_plan_json=canonical(state_plan.model_dump(mode='json')),
                         browser_runtime_id=self.draft.runtime.runtime_id,
                         created_at=created)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert('desktop_remote_form_cookie_bindings',
                         job_id='job', run_id='run', form_plan_sha256='0' * 64,
                         cookie_sha256='c' * 64,
                         browser_runtime_id=self.draft.runtime.runtime_id,
                         created_at=created)
        store.connection.rollback()
        with store.connection:
            store.insert('desktop_remote_form_cookie_bindings',
                         job_id='job', run_id='run',
                         form_plan_sha256=digest(plan.model_dump()),
                         cookie_sha256='c' * 64,
                         browser_runtime_id=self.draft.runtime.runtime_id,
                         created_at=created)
        for statement in (
                "UPDATE desktop_remote_form_bindings SET plan_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_form_bindings',
                "UPDATE desktop_tasks SET kind='browser_remote_entry' WHERE job_id='job'"):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        for statement in (
                "UPDATE desktop_remote_form_state_bindings SET state_plan_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_form_state_bindings'):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        for statement in (
                "UPDATE desktop_remote_form_cookie_bindings SET cookie_sha256='" + '0' * 64 + "'",
                'DELETE FROM desktop_remote_form_cookie_bindings',
                "INSERT OR REPLACE INTO desktop_remote_form_cookie_bindings VALUES(" +
                "'job','run','" + digest(plan.model_dump()) + "','" + 'd' * 64 +
                "','" + self.draft.runtime.runtime_id + "','" + created + "')"):
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
            store.connection.rollback()
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_v13_upgrade_preserves_remote_routes(self):
        database = self.root / 'v13-remote-routes.sqlite'
        connection = sqlite3.connect(database)
        for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:13]:
            connection.executescript(migration.read_text())
        created = '2026-09-23T00:00:00Z'
        connection.execute('INSERT INTO tasks(task_id,original_goal,normalized_goal,success_criteria_json,workspace_scope_json,created_at) VALUES(?,?,?,?,?,?)',
                           ('task', 'synthetic', 'synthetic', '[]', '[]', created))
        connection.execute('INSERT INTO runs(run_id,task_id,status,policy_version,environment_json,deployment_snapshot_json,started_at) VALUES(?,?,?,?,?,?,?)',
                           ('run', 'task', 'succeeded', 'browser-remote-routes-policy-v1',
                            '{}', '{}', created))
        connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                           ('session', 'desktop', 'image', 'AGENT', 'lease', 0,
                            'running', created, created))
        connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           ('job', 'session', 'run', 'browser_remote_routes', 'lease', 0,
                            'succeeded', 0, created, created, self.draft.runtime.runtime_id))
        connection.execute('INSERT INTO desktop_approvals VALUES(?,?,?,?,?,?,?,?)',
                           ('approval', 'job', '{}', '0' * 64, 123, 'consumed',
                            created, created))
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url,
                                         self.profile.entry_url.replace('/entry', '/details')])
        connection.execute('INSERT INTO desktop_remote_route_bindings VALUES(?,?,?,?,?,?,?,?,?,?)',
                           ('job', 'run', self.checksum, self.draft.binding_sha256,
                            self.draft.runtime_sha256, digest(plan.model_dump()),
                            canonical(self.draft.model_dump(mode='json')),
                            canonical(plan.model_dump(mode='json')),
                            self.draft.runtime.runtime_id, created))
        connection.commit()
        connection.close()
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        self.assertEqual(store.connection.execute(
            'SELECT max(version) FROM schema_migrations').fetchone()[0], 17)
        self.assertEqual(store.connection.execute(
            'SELECT status FROM desktop_approvals').fetchone()[0], 'consumed')
        self.assertEqual(store.connection.execute(
            'SELECT plan_sha256 FROM desktop_remote_route_bindings').fetchone()[0],
            digest(plan.model_dump()))
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE desktop_tasks SET kind='browser_remote_form' WHERE job_id='job'")
        store.connection.rollback()

    def test_v12_upgrade_preserves_remote_entry_binding_and_approval(self):
        database = self.root / 'v12-remote-entry.sqlite'
        connection = sqlite3.connect(database)
        for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:12]:
            connection.executescript(migration.read_text())
        created = '2026-09-23T00:00:00Z'
        connection.execute('INSERT INTO tasks(task_id,original_goal,normalized_goal,success_criteria_json,workspace_scope_json,created_at) VALUES(?,?,?,?,?,?)',
                           ('task', 'synthetic', 'synthetic', '[]', '[]', created))
        connection.execute('INSERT INTO runs(run_id,task_id,status,policy_version,environment_json,deployment_snapshot_json,started_at) VALUES(?,?,?,?,?,?,?)',
                           ('run', 'task', 'succeeded', 'browser-remote-entry-policy-v1', '{}', '{}', created))
        connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                           ('session', 'desktop', 'image', 'AGENT', 'lease', 0, 'running', created, created))
        connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           ('job', 'session', 'run', 'browser_remote_entry', 'lease', 0, 'succeeded', 0,
                            created, created, self.draft.runtime.runtime_id))
        connection.execute('INSERT INTO desktop_approvals VALUES(?,?,?,?,?,?,?,?)',
                           ('approval', 'job', '{}', '0' * 64, 123, 'consumed', created, created))
        connection.execute('INSERT INTO desktop_remote_entry_bindings VALUES(?,?,?,?,?,?,?,?)',
                           ('job', 'run', self.checksum, self.draft.binding_sha256,
                            self.draft.runtime_sha256, canonical(self.draft.model_dump(mode='json')),
                            self.draft.runtime.runtime_id, created))
        connection.commit()
        connection.close()
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        self.assertEqual(store.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 17)
        self.assertEqual(store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'consumed')
        self.assertEqual(store.connection.execute(
            'SELECT binding_sha256 FROM desktop_remote_entry_bindings').fetchone()[0],
            self.draft.binding_sha256)
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE desktop_tasks SET kind='browser_remote_routes' WHERE job_id='job'")
        store.connection.rollback()

    def test_remote_route_approval_fixture_requires_integer_index_and_exact_fields(self):
        record = json.loads((REPO_ROOT / 'examples/desktop_remote_route_approval.json').read_text())
        action = Action.model_validate(record['approval']['action'])
        ToolRegistry.validate(action)
        self.assertEqual(digest(action.model_dump(mode='json')),
                         record['approval']['action_sha256'])
        for arguments in ({**action.arguments, 'route_index': '1'},
                          {**action.arguments, 'route_index': 8},
                          {**action.arguments, 'url': 'http://app.example.invalid/details'},
                          {**action.arguments, 'extra': 'not authorized'}):
            with self.subTest(arguments=arguments), self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': arguments}))

    def test_remote_form_approval_fixture_rejects_value_and_wrong_stage(self):
        record = json.loads((REPO_ROOT / 'examples/desktop_remote_form_approval.json').read_text())
        self.assertTrue(record['synthetic'])
        action = Action.model_validate(record['approval']['action'])
        self.assertEqual(digest(action.model_dump(mode='json')),
                         record['approval']['action_sha256'])
        ToolRegistry.validate(action)
        for arguments in ({**action.arguments, 'value': 'private'},
                          {**action.arguments, 'stage': 1},
                          {**action.arguments, 'stage': True},
                          {**action.arguments, 'url': 'http://app.example.invalid/submit'},
                          {**action.arguments, 'url': 'https://app.example.invalid/other?x=1'},
                          {**action.arguments, 'field_name': 'not a field'},
                          {**action.arguments, 'body_sha256': '0'},
                          {key: value for key, value in action.arguments.items()
                           if key != 'body_sha256'}):
            with self.assertRaises(AOSFault):
                ToolRegistry.validate(action.model_copy(update={'arguments': arguments}))

    def test_relay_cancellation_removes_private_socket(self):
        stopped = threading.Event()
        outcome = {}
        def worker():
            try:
                self.relay().serve(self.socket_path, wait_seconds=10, stop_event=stopped)
            except Exception as error:
                outcome['error'] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        for _attempt in range(200):
            if self.socket_path.exists():
                break
            time.sleep(0.01)
        self.assertTrue(self.socket_path.exists())
        stopped.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome['error'], TimeoutError)
        self.assertFalse(self.socket_path.exists())
        self.assertEqual(self.server.paths, [])

    def test_remote_entry_page_gate_source_rejects_invalid_scope(self):
        make_gate = worker_namespace()['remote_entry_page_gate_source']
        for url, binding, token in [('http://example.invalid/entry', self.draft.binding_sha256, self.relay().client_token),
                                    (self.profile.entry_url, 'wrong', self.relay().client_token),
                                    (self.profile.entry_url, self.draft.binding_sha256, 'wrong')]:
            with self.assertRaises(ValueError):
                make_gate(url, binding, token)
        source = make_gate(self.profile.entry_url, self.draft.binding_sha256,
                           self.relay().client_token)
        self.assertIn("route.abort('blockedbyclient')", source)
        self.assertIn('request.isNavigationRequest()', source)
        self.assertIn('request.frame() !== page.mainFrame()', source)
        self.assertIn("form-action 'none'", source)
        gate = self.root / 'remote_gate.cjs'
        gate.write_text(source)
        subprocess.run(['node', '--check', str(gate)], check=True, capture_output=True)

    def test_remote_entry_page_gate_rejects_same_url_subrequest_before_relay(self):
        source = worker_namespace()['remote_entry_page_gate_source'](
            self.profile.entry_url, self.draft.binding_sha256, self.relay().client_token)
        script = '''const loaded = {exports: {}};
let socketCalls = 0;
new Function('module', 'require', process.argv[1])(loaded, name => {
  if (name === 'node:net') return {createConnection: () => {
    socketCalls += 1; throw new Error('synthetic_socket');}};
  return require(name);
});
let handler;
const mainFrame = {};
const page = {mainFrame: () => mainFrame, route: async (_pattern, callback) => {handler = callback;}};
const entry = process.argv[2];
async function attempt(method, url, navigation, resource, frame) {
  let decision;
  await handler({request: () => ({method: () => method, url: () => url,
    isNavigationRequest: () => navigation, resourceType: () => resource,
    frame: () => frame}), abort: async () => {decision = 'abort';},
    fulfill: async () => {decision = 'fulfill';}});
  return decision;
}
(async () => {
  await loaded.exports.default({page});
  const otherFrame = {};
  const before = [
    await attempt('GET', entry, false, 'fetch', mainFrame),
    await attempt('GET', entry, true, 'document', otherFrame),
    await attempt('GET', entry + '/outside', true, 'document', mainFrame),
    await attempt('POST', entry, true, 'document', mainFrame)];
  if (socketCalls !== 0) throw new Error('relay_contacted_before_main_frame');
  const accepted = await attempt('GET', entry, true, 'document', mainFrame);
  const replay = await attempt('GET', entry, true, 'document', mainFrame);
  process.stdout.write(JSON.stringify({before, accepted, replay, socketCalls}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        result = subprocess.run(['node', '-e', script, source, self.profile.entry_url],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'before': ['abort'] * 4, 'accepted': 'abort', 'replay': 'abort', 'socketCalls': 1})

    def test_static_bundle_page_gate_rejects_wrong_resource_before_socket(self):
        make_gate = worker_namespace()['static_bundle_page_gate_source']
        origin = self.profile.allowed_origins[0]
        assets = [{'url': origin + '/assets/app.js',
                   'content_type': 'application/javascript'}]
        for suffix in ('?view=1&view=2', '?view=%2f', '?view=hello+world',
                       '?view=1#fragment'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                make_gate(self.profile.entry_url, assets, 'a' * 64, 'b' * 64,
                          [{'url': origin + '/api/summary' + suffix,
                            'content_type': 'application/json'}])
        for invalid in ([{**assets[0], 'url': origin + '/assets/app.js?x=1&x=2'}],
                        [{**assets[0], 'url': origin + '/assets/app.js?x=%0A'}],
                        [{**assets[0], 'url': self.profile.entry_url + '?v=1'}],
                        [assets[0], assets[0]],
                        [{**assets[0], 'content_type': 'text/html'}]):
            with self.assertRaises(ValueError):
                make_gate(self.profile.entry_url, invalid, 'a' * 64, 'b' * 64)
        assets[0]['url'] += '?v=1'
        source = make_gate(self.profile.entry_url, assets, 'a' * 64, 'b' * 64)
        gate = self.root / 'static_bundle_gate.cjs'
        gate.write_text(source)
        subprocess.run(['node', '--check', str(gate)], check=True, capture_output=True)
        script = '''const loaded = {exports: {}};
let socketCalls = 0;
new Function('module', 'require', process.argv[1])(loaded, name => {
  if (name === 'node:net') return {createConnection: () => {
    socketCalls += 1; throw new Error('synthetic_socket');}};
  return require(name);
});
let handler;
const mainFrame = {};
const page = {mainFrame: () => mainFrame, route: async (_pattern, callback) => {handler = callback;}};
async function attempt(method, url, navigation, resource, frame) {
  let decision;
  await handler({request: () => ({method: () => method, url: () => url,
    isNavigationRequest: () => navigation, resourceType: () => resource,
    frame: () => frame}), abort: async () => {decision = 'abort';},
    fulfill: async () => {decision = 'fulfill';}});
  return decision;
}
(async () => {
  await loaded.exports.default({page});
  const entry = process.argv[2], asset = process.argv[3], otherFrame = {};
  const before = [
    await attempt('GET', entry, false, 'image', mainFrame),
    await attempt('GET', entry, true, 'document', otherFrame),
    await attempt('GET', asset, false, 'script', mainFrame),
    await attempt('GET', asset.replace('v=1', 'v=2'), false, 'script', mainFrame),
    await attempt('GET', asset, false, 'stylesheet', mainFrame),
    await attempt('POST', entry, true, 'document', mainFrame)];
  if (socketCalls !== 0) throw new Error('relay_contacted_before_main_document');
  const failedEntry = await attempt('GET', entry, true, 'document', mainFrame);
  const after = [
    await attempt('GET', asset, false, 'script', mainFrame),
    await attempt('GET', entry, true, 'document', mainFrame)];
  process.stdout.write(JSON.stringify({before, failedEntry, after, socketCalls}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        result = subprocess.run(['node', '-e', script, source, self.profile.entry_url,
                                 assets[0]['url']], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'before': ['abort'] * 6, 'failedEntry': 'abort',
            'after': ['abort'] * 2, 'socketCalls': 1})

    def test_static_bundle_gate_rejects_wrong_query_after_entry_before_socket(self):
        make_gate = worker_namespace()['static_bundle_page_gate_source']
        origin = self.profile.allowed_origins[0]
        asset = origin + '/assets/app.js?v=1'
        source = make_gate(self.profile.entry_url,
                           [{'url': asset, 'content_type': 'application/javascript'}],
                           'a' * 64, 'b' * 64)
        script = '''const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const loaded = {exports: {}};
let socketCalls = 0;
new Function('module', 'require', process.argv[1])(loaded, name => {
  if (name === 'node:net') return {createConnection: () => {
    socketCalls += 1;
    const connection = new EventEmitter();
    connection.setTimeout = () => {};
    connection.destroy = () => {};
    connection.write = raw => {
      const request = JSON.parse(raw);
      const body = Buffer.from(request.asset_index === null ? '<html>Entry</html>' : 'window.ready=true;');
      const reply = {status: 200, asset_index: request.asset_index,
        content_type: request.asset_index === null ? 'text/html' : 'application/javascript',
        body_base64: body.toString('base64'),
        response_sha256: crypto.createHash('sha256').update(body).digest('hex')};
      setImmediate(() => {connection.emit('data', Buffer.from(JSON.stringify(reply))); connection.emit('end');});
    };
    setImmediate(() => connection.emit('connect'));
    return connection;
  }};
  return require(name);
});
let handler;
const frame = {};
const page = {mainFrame: () => frame, route: async (_pattern, callback) => {handler = callback;}};
async function attempt(url, navigation, resource) {
  let decision;
  await handler({request: () => ({method: () => 'GET', url: () => url,
    isNavigationRequest: () => navigation, resourceType: () => resource,
    frame: () => frame}), abort: async () => {decision = 'abort';},
    fulfill: async () => {decision = 'fulfill';}});
  return decision;
}
(async () => {
  await loaded.exports.default({page});
  const entry = await attempt(process.argv[2], true, 'document');
  const wrong = await attempt(process.argv[3].replace('v=1', 'v=2'), false, 'script');
  const beforeAsset = socketCalls;
  const accepted = await attempt(process.argv[3], false, 'script');
  const replay = await attempt(process.argv[3], false, 'script');
  process.stdout.write(JSON.stringify({entry, wrong, beforeAsset, accepted, replay, socketCalls}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        result = subprocess.run(['node', '-e', script, source, self.profile.entry_url,
                                 asset], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'entry': 'fulfill', 'wrong': 'abort', 'beforeAsset': 1,
            'accepted': 'fulfill', 'replay': 'abort', 'socketCalls': 2})

    def test_static_bundle_image_gate_requires_exact_resource_type(self):
        make_gate = worker_namespace()['static_bundle_page_gate_source']
        origin = self.profile.allowed_origins[0]
        asset = {'url': origin + '/assets/logo.png', 'content_type': 'image/png'}
        source = make_gate(self.profile.entry_url, [asset], 'a' * 64, 'b' * 64)
        self.assertIn("img-src 'self'", source)
        gate = self.root / 'image_bundle_gate.cjs'
        gate.write_text(source)
        subprocess.run(['node', '--check', str(gate)], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            make_gate(self.profile.entry_url,
                      [{**asset, 'content_type': 'image/svg+xml'}], 'a' * 64, 'b' * 64)
        script = '''const loaded = {exports: {}};
const {EventEmitter} = require('node:events');
const crypto = require('node:crypto');
let socketCalls = 0;
new Function('module', 'require', process.argv[1])(loaded, name => {
  if (name === 'node:net') return {createConnection: () => {
    socketCalls += 1;
    const connection = new EventEmitter();
    connection.setTimeout = () => {};
    connection.write = payload => {
      const request = JSON.parse(payload);
      const body = Buffer.from(request.asset_index === null ? '<html></html>' : 'image-bytes');
      const reply = {status: 200, content_type: request.asset_index === null
        ? 'text/html' : 'image/png', asset_index: request.asset_index,
        body_base64: body.toString('base64'),
        response_sha256: crypto.createHash('sha256').update(body).digest('hex')};
      process.nextTick(() => {connection.emit('data', Buffer.from(JSON.stringify(reply)));
        connection.emit('end');});
    };
    process.nextTick(() => connection.emit('connect'));
    return connection;
  }};
  return require(name);
});
let handler;
const mainFrame = {};
const page = {mainFrame: () => mainFrame, route: async (_pattern, callback) => {handler = callback;}};
async function attempt(method, url, navigation, resource, frame) {
  let decision;
  await handler({request: () => ({method: () => method, url: () => url,
    isNavigationRequest: () => navigation, resourceType: () => resource,
    frame: () => frame}), abort: async () => {decision = 'abort';},
    fulfill: async () => {decision = 'fulfill';}});
  return decision;
}
(async () => {
  await loaded.exports.default({page});
  const entry = process.argv[2], asset = process.argv[3], otherFrame = {};
  const before = [await attempt('GET', asset, false, 'image', mainFrame),
    await attempt('GET', entry, false, 'image', mainFrame)];
  const opened = await attempt('GET', entry, true, 'document', mainFrame);
  const wrong = [await attempt('GET', asset, false, 'script', mainFrame),
    await attempt('GET', asset, false, 'image', otherFrame),
    await attempt('POST', asset, false, 'image', mainFrame)];
  const image = await attempt('GET', asset, false, 'image', mainFrame);
  const replay = await attempt('GET', asset, false, 'image', mainFrame);
  process.stdout.write(JSON.stringify({before, opened, wrong, image, replay, socketCalls}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        result = subprocess.run(['node', '-e', script, source, self.profile.entry_url,
                                 asset['url']], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'before': ['abort'] * 2, 'opened': 'fulfill', 'wrong': ['abort'] * 3,
            'image': 'fulfill', 'replay': 'abort', 'socketCalls': 2})

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu static bundle MCP test')
    def test_pinned_mcp_static_js_css_bundle_in_network_none_chromium(self):
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {'/entry': (
            b'<html><head><title>Initial</title>'
            b'<link rel="stylesheet" href="/assets/site.css?v=2">'
            b'<script src="/assets/app.js?v=1" defer></script></head>'
            b'<body><h1>Loading</h1><img src="/assets/logo.png?v=3" alt="Synthetic logo"></body></html>')}
        self.server.asset_responses = {
            '/assets/app.js?v=1': ('application/javascript',
                               b'document.querySelector("img").decode().then(()=>{'
                               b'document.querySelector("h1").textContent="Image decoded";});'
                               b'fetch("/blocked").catch(()=>{});'),
            '/assets/site.css?v=2': ('text/css', b'body{color:rgb(0,128,0)}'),
            '/assets/logo.png?v=3': ('image/png', base64.b64decode(
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=='))}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js?v=1', 'content_type': 'application/javascript'},
            {'url': origin + '/assets/site.css?v=2', 'content_type': 'text/css'},
            {'url': origin + '/assets/logo.png?v=3', 'content_type': 'image/png'}])
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       digest(plan.model_dump()), tls_context=self.tls_context)
        relay.arm()
        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        gate = self.workspace / 'static_bundle_gate.cjs'
        gate.write_text(worker_namespace()['static_bundle_page_gate_source'](
            self.profile.entry_url, [asset.model_dump() for asset in plan.assets],
            relay.plan_sha256, relay.client_token))
        gate.chmod(0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const {spawn} = require('node:child_process');
const child = spawn('/usr/local/bin/node', ['/workspace/node_modules/@playwright/mcp/cli.js',
  '--isolated', '--executable-path', '/opt/chromium/chrome', '--no-sandbox',
  '--block-service-workers', '--no-webmcp', '--codegen', 'none', '--snapshot-mode', 'full',
  '--output-dir', '/home/agent/mcp-output', '--timeout-action', '3000',
  '--timeout-navigation', '10000', '--timeout-settle', '0',
  '--init-page', '/workspace/static_bundle_gate.cjs'],
  {env: {PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/home/agent', DISPLAY: ':99',
         LANG: 'C.UTF-8', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1'}});
let counter = 0, buffer = '';
const pending = new Map();
child.stdout.on('data', chunk => {
  buffer += chunk;
  for (;;) {
    const newline = buffer.indexOf('\\n');
    if (newline < 0) break;
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const reply = JSON.parse(line);
    if (pending.has(reply.id)) {pending.get(reply.id)(reply); pending.delete(reply.id);}
  }
});
function request(method, params) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('timeout'));}, 15000);
    pending.set(id, reply => {clearTimeout(timer); resolve(reply);});
    child.stdin.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\\n');
  });
}
(async () => {
  try {
    const initialized = await request('initialize', {protocolVersion: '2024-11-05',
      capabilities: {}, clientInfo: {name: 'aos-static-bundle-test', version: '1'}});
    if (initialized.result?.serverInfo?.version !== process.argv[2]) throw new Error('version');
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\\n');
    const opened = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: process.argv[1]}});
    if (opened.result?.isError) throw new Error('navigate');
    let content = '';
    for (let attempt = 0; attempt < 20; attempt++) {
      const snapshot = await request('tools/call', {name: 'browser_snapshot', arguments: {}});
      if (snapshot.result?.isError) throw new Error('snapshot');
      content = snapshot.result?.content?.map(block => block.text || '').join('\\n') || '';
      if (content.includes('Image decoded')) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!content.includes('Image decoded')
        || !content.includes('Synthetic logo')) throw new Error('script_or_image');
    const blocked = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: 'https://blocked.aos-relay.invalid/outside'}});
    if (!blocked.result?.isError) throw new Error('off-plan navigation');
    process.stdout.write('dynamic_ready');
  } finally {child.kill('SIGTERM');}
})().catch(error => {process.stderr.write(error.message); child.kill('SIGTERM'); process.exitCode = 1;});'''

        def action():
            result = subprocess.run([*DOCKER, 'exec', '-e', 'DISPLAY=:99',
                                     desktop.container_id, '/usr/local/bin/node', '-e',
                                     script, self.profile.entry_url, PLAYWRIGHT_VERSION],
                                    capture_output=True, timeout=35, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b'dynamic_ready')

        outcome = self.run_relay(action, relay=relay)
        self.assertNotIn('error', outcome)
        self.assertEqual(set(self.server.paths), {'/entry', '/assets/app.js?v=1',
                                                 '/assets/site.css?v=2', '/assets/logo.png?v=3'})
        self.assertEqual(len(self.server.paths), 4)
        self.assertEqual(outcome['report'].plan_sha256, relay.plan_sha256)
        self.assertEqual(len(outcome['report'].asset_response_sha256), 3)
        self.assertFalse(self.socket_path.exists())

    def test_readonly_route_gate_rejects_subresources_and_invalid_scope(self):
        make_gate = worker_namespace()['readonly_route_page_gate_source']
        routes = [self.profile.entry_url,
                  self.profile.entry_url.replace('/entry', '/details?view=compact&page=1')]
        for invalid in ([routes[0]], [routes[0], routes[0]],
                        [routes[0] + '?q=1', routes[1]],
                        [routes[0], routes[1] + '&view=other'],
                        [routes[0], routes[1].replace('page=1', 'page=%0A')]):
            with self.assertRaises(ValueError):
                make_gate(invalid, '0' * 64, '1' * 64)
        source = make_gate(routes, '0' * 64, '1' * 64)
        self.assertIn('request.isNavigationRequest()', source)
        self.assertIn("request.frame() !== page.mainFrame()", source)
        self.assertIn("form-action 'none'", source)
        gate = self.root / 'readonly_gate.cjs'
        gate.write_text(source)
        subprocess.run(['node', '--check', str(gate)], check=True, capture_output=True)
        script = '''const loaded = {exports: {}};
let socketCalls = 0;
new Function('module', 'require', process.argv[1])(loaded, name => {
  if (name === 'node:net') return {createConnection: () => {
    socketCalls += 1; throw new Error('synthetic_socket');}};
  return require(name);
});
let handler;
const mainFrame = {};
const page = {mainFrame: () => mainFrame,
  route: async (_pattern, callback) => {handler = callback;}};
async function attempt(method, url, resourceType, frame) {
  let result;
  await handler({request: () => ({method: () => method, url: () => url,
    isNavigationRequest: () => true, resourceType: () => resourceType,
    frame: () => frame}), abort: async () => {result = 'abort';},
    fulfill: async () => {result = 'fulfill';}});
  return result;
}
(async () => {
  await loaded.exports.default({page});
  const allowed = process.argv[2], wrong = allowed.replace('page=1', 'page=2');
  const denied = [await attempt('GET', wrong, 'document', mainFrame),
    await attempt('POST', allowed, 'document', mainFrame),
    await attempt('GET', allowed, 'script', mainFrame),
    await attempt('GET', allowed, 'document', {})];
  process.stdout.write(JSON.stringify({denied, socketCalls}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        result = subprocess.run(['node', '-e', script, source, routes[1]],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'denied': ['abort'] * 4, 'socketCalls': 0})

    def test_https_form_page_gate_rejects_invalid_scope(self):
        make_gate = worker_namespace()['https_form_page_gate_source']
        origin = self.profile.allowed_origins[0]
        arguments = (self.profile.entry_url, origin + '/submit', origin + '/receipt',
                     'a' * 64, 'b' * 64)
        for invalid in ((self.profile.entry_url.replace('https:', 'http:'), *arguments[1:]),
                        (arguments[0], arguments[0], *arguments[2:]),
                        (arguments[0], 'https://other.invalid/submit', *arguments[2:]),
                        (*arguments[:3], 'invalid', arguments[4])):
            with self.assertRaises(ValueError):
                make_gate(*invalid)
        source = make_gate(*arguments)
        self.assertIn('request.postDataBuffer()', source)
        self.assertIn('request.frame() !== page.mainFrame()', source)
        self.assertIn("'self'", source)
        gate = self.root / 'https_form_gate.cjs'
        gate.write_text(source)
        subprocess.run(['node', '--check', str(gate)], check=True, capture_output=True)
        public_arguments = ('https://www.example.com/entry',
                            'https://www.example.com/submit',
                            'https://www.example.com/receipt', 'a' * 64, 'b' * 64)
        for grant in (None, '0' * 64):
            with self.assertRaises(ValueError):
                make_gate(*public_arguments, grant)
        with self.assertRaises(ValueError):
            make_gate(*arguments, 'a' * 64)
        public_source = make_gate(*public_arguments, 'a' * 64)
        public_gate = self.root / 'public_form_gate.cjs'
        public_gate.write_text(public_source)
        subprocess.run(['node', '--check', str(public_gate)], check=True, capture_output=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned public-form grant admission test')
    def test_public_form_scheduler_requires_exact_grant_and_four_approvals(self):
        cookie = 'session=synthetic-scheduler-cookie'
        cookie_sha256 = hashlib.sha256(cookie.encode('ascii')).hexdigest()
        self.server.cookies = []
        origin = f'https://www.aos-form.example.com:{self.server.server_port}'
        profiles = WebApplicationProfiles(self.root / 'public-scheduler-profiles')
        profile = self.profile.model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task = self.draft.task.model_copy(update={
            'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
            'allowed_origins': profile.allowed_origins})
        body = b'message=hello'
        self.server.route_bodies = {
            '/entry': (b'<html><title>Public-like form</title><h1>Entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'public-form-admission.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        options = dict(
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=profiles, remote_entry_profile_sha256=profile_sha256,
            remote_entry_task=task, remote_form_plan=plan,
            remote_form_field_name='message', remote_form_value='hello',
            remote_form_tls_context=self.tls_context,
            remote_form_cookie=cookie,
            remote_form_cookie_sha256=cookie_sha256)
        for grant in (None, '0' * 64):
            with self.assertRaises(ValueError):
                DesktopScheduler(controller, Settings(workspace=self.workspace, database=database),
                                 FixtureDecisionEngine(), **options,
                                 remote_form_public_plan_sha256=grant)
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, Settings(workspace=self.workspace, database=database),
                             FixtureDecisionEngine(), **{**options,
                                 'remote_form_cookie_sha256': '0' * 64},
                             remote_form_public_plan_sha256=digest(plan.model_dump()))
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            FixtureDecisionEngine(), **options,
            remote_form_public_plan_sha256=digest(plan.model_dump()))
        self.assertEqual(scheduler.status()['remote_form']['mode'], 'public_explicit_one_post')
        self.assertEqual(scheduler.status()['remote_form']['submit_url'], plan.submit_url)
        self.assertEqual(scheduler.status()['remote_form']['cookie_sha256'], cookie_sha256)
        self.assertEqual(self.server.paths, [])
        self.assertEqual(self.server.posts, [])

        async def scenario():
            owner = controller.state()
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            for stage in range(4):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        self.fail('Public-like form stopped before exact approval')
                    await asyncio.sleep(0.02)
                else:
                    self.fail('Public-like form did not request approval')
                self.assertEqual(approval['action']['arguments']['url'],
                                 (plan.entry_url if stage in (0, 1)
                                  else plan.submit_url if stage == 2 else plan.receipt_url))
                self.assertEqual(approval['action']['arguments']['cookie_sha256'],
                                 cookie_sha256)
                self.assertNotIn(cookie, canonical(approval['action']))
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            job = store.connection.execute(
                'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(self.server.cookies, [('/entry', cookie), ('/submit', cookie),
                                                   ('/receipt', cookie)])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 4)
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_form_cookie_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            self.assertEqual(binding['cookie_sha256'], cookie_sha256)
            with self.assertRaisesRegex(ValueError, 'cookie_scope_unsupported'):
                inspect_remote_form_learning_source(
                    database, job['run_id'], profiles=profiles.root,
                    selected_profile_sha256=profile_sha256,
                    selected_plan_sha256=digest(plan.model_dump()))
            with self.assertRaisesRegex(ValueError, 'cookie_scope_unsupported'):
                plan_remote_form_json_oracle(
                    database, job['run_id'], profiles=profiles.root,
                    profile_sha256=profile_sha256,
                    form_plan_sha256=digest(plan.model_dump()),
                    url=origin + '/oracle', field_key='status',
                    expected_value_sha256=digest({'value': 'saved'}))
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            with self.assertRaisesRegex(ValueError, 'cookie_binding_changed'):
                inspect_remote_form_learning_source(
                    database, job['run_id'], profiles=profiles.root,
                    selected_profile_sha256=profile_sha256,
                    selected_plan_sha256=digest(plan.model_dump()),
                    selected_cookie_sha256='0' * 64)
            source = inspect_remote_form_learning_source(
                database, job['run_id'], profiles=profiles.root,
                selected_profile_sha256=profile_sha256,
                selected_plan_sha256=digest(plan.model_dump()),
                selected_cookie_sha256=cookie_sha256)
            self.assertTrue(source['cookie_bound'])
            self.assertEqual(source['source_event_ids_by_role'],
                             {'system1': [], 'system2': []})
            self.assertFalse(source['account_verified'])
            self.assertFalse(source['collection_authorized'])
            self.server.asset_responses = {
                '/oracle': ('application/json', b'{"status":"saved"}')}
            oracle_plan = plan_remote_form_json_oracle(
                database, job['run_id'], profiles=profiles.root,
                profile_sha256=profile_sha256,
                form_plan_sha256=digest(plan.model_dump()),
                url=origin + '/oracle', field_key='status',
                expected_value_sha256=digest({'value': 'saved'}),
                cookie_sha256=cookie_sha256)
            preview = subprocess.run([
                sys.executable, '-m', 'aos.remote_form_json_oracle',
                '--database', str(database), '--run-id', job['run_id'],
                '--profiles', str(profiles.root), '--profile-sha256', profile_sha256,
                '--form-plan-sha256', digest(plan.model_dump()),
                '--url', origin + '/oracle', '--field-key', 'status',
                '--expected-value-sha256', digest({'value': 'saved'}),
                '--cookie-sha256', cookie_sha256], capture_output=True, text=True, timeout=20)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)['plan_sha256'],
                             digest(oracle_plan.model_dump()))
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            with self.assertRaisesRegex(ValueError, 'cookie_scope_mismatch'):
                probe_remote_form_json_oracle(
                    database, job['run_id'], profiles=profiles.root,
                    plan=oracle_plan, confirm_plan_sha256=digest(oracle_plan.model_dump()),
                    tls_context=self.tls_context)
            with self.assertRaisesRegex(ValueError, 'cookie_hash_mismatch'):
                probe_remote_form_json_oracle(
                    database, job['run_id'], profiles=profiles.root,
                    plan=oracle_plan, confirm_plan_sha256=digest(oracle_plan.model_dump()),
                    tls_context=self.tls_context, cookie_header='session=wrong')
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            oracle = probe_remote_form_json_oracle(
                database, job['run_id'], profiles=profiles.root,
                plan=oracle_plan, confirm_plan_sha256=digest(oracle_plan.model_dump()),
                tls_context=self.tls_context, cookie_header=cookie)
            self.assertEqual(oracle.cookie_sha256, cookie_sha256)
            self.assertFalse(oracle.account_verified)
            self.assertFalse(oracle.site_outcome_verified)
            self.assertEqual(self.server.cookies[-1], ('/oracle', cookie))
            self.server.asset_responses = {
                '/oracle': ('application/json', ('{"status":"' + cookie + '"}').encode())}
            with self.assertRaisesRegex(ValueError, 'cookie_reflected'):
                probe_remote_form_json_oracle(
                    database, job['run_id'], profiles=profiles.root,
                    plan=oracle_plan, confirm_plan_sha256=digest(oracle_plan.model_dump()),
                    tls_context=self.tls_context, cookie_header=cookie)
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle', '/oracle'])
            with self.assertRaisesRegex(ValueError, 'form_body_changed'):
                plan_remote_form_json_submission(
                    database, job['run_id'], profiles=profiles.root,
                    profile_sha256=profile_sha256,
                    form_plan_sha256=digest(plan.model_dump()),
                    url=origin + '/oracle-submitted', field_key='message',
                    submitted_field_name='message', fields=(('message', 'wrong'),),
                    cookie_sha256=cookie_sha256)
            with (patch('aos.remote_form_learning_source.audit_snapshot',
                        wraps=audit_snapshot) as source_audits,
                  patch('aos.remote_form_json_oracle.audit_snapshot',
                        wraps=audit_snapshot) as form_audits):
                submission_plan = plan_remote_form_json_submission(
                    database, job['run_id'], profiles=profiles.root,
                    profile_sha256=profile_sha256,
                    form_plan_sha256=digest(plan.model_dump()),
                    url=origin + '/oracle-submitted', field_key='message',
                    submitted_field_name='message', fields=(('message', 'hello'),),
                    cookie_sha256=cookie_sha256)
            self.assertEqual(source_audits.call_count, 1)
            self.assertEqual(form_audits.call_count, 1)
            with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as private_directory:
                value_file = Path(private_directory) / 'value.txt'
                value_file.write_text('hello')
                value_file.chmod(0o600)
                preview = subprocess.run([
                    sys.executable, '-m', 'aos.remote_form_json_submission',
                    '--database', str(database), '--run-id', job['run_id'],
                    '--profiles', str(profiles.root), '--profile-sha256', profile_sha256,
                    '--form-plan-sha256', digest(plan.model_dump()),
                    '--url', origin + '/oracle-submitted', '--field-key', 'message',
                    '--submitted-field-name', 'message', '--form-value-file', str(value_file),
                    '--cookie-sha256', cookie_sha256],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(preview.returncode, 0, preview.stderr)
                self.assertEqual(json.loads(preview.stdout)['plan_sha256'],
                                 digest(submission_plan.model_dump()))
                self.assertNotIn(cookie, preview.stdout)
                self.assertNotIn('hello', preview.stdout)
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle', '/oracle'])
            with self.assertRaisesRegex(ValueError, 'form_body_changed'):
                probe_remote_form_json_submission(
                    database, job['run_id'], profiles=profiles.root,
                    plan=submission_plan,
                    confirm_plan_sha256=digest(submission_plan.model_dump()),
                    fields=(('message', 'wrong'),), cookie_header=cookie,
                    tls_context=self.tls_context)
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle', '/oracle'])
            submission = probe_remote_form_json_submission(
                database, job['run_id'], profiles=profiles.root,
                plan=submission_plan,
                confirm_plan_sha256=digest(submission_plan.model_dump()),
                fields=(('message', 'hello'),), cookie_header=cookie,
                tls_context=self.tls_context)
            self.assertTrue(submission.submitted_value_readback_bound)
            self.assertFalse(submission.site_outcome_verified)
            self.assertEqual(self.server.paths[-1], '/oracle-submitted')
            self.assertEqual(self.server.cookies[-1], ('/oracle-submitted', cookie))
            self.assertNotIn(cookie, '\n'.join(store.connection.iterdump()))
            with self.assertRaises(sqlite3.DatabaseError):
                with store.connection:
                    store.connection.execute(
                        "UPDATE desktop_remote_form_cookie_bindings SET cookie_sha256='" +
                        '0' * 64 + "' WHERE job_id=?", (job_id,))
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu scheduled HTTPS form test')
    def test_scheduled_form_needs_four_consumed_approvals_and_readback(self):
        value = 'form-private-947'
        body = b'message=form-private-947'
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'form-task.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            FixtureDecisionEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_form_plan=plan,
            remote_form_field_name='message', remote_form_value=value,
            remote_form_tls_context=self.tls_context,
            remote_learning_consents_dir=self.root / 'form-late-consents',
            remote_learning_stream_dir=self.root / 'form-late-outbox')
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'],
                            'browser_remote_form', approve_all=True)

        async def next_approval(previous=None):
            for _attempt in range(1500):
                approval = scheduler.status()['approval']
                if approval is not None and approval['approval_id'] != previous:
                    return approval
                if scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.fail('Scheduled HTTPS form did not request the next approval')

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            expected_tools = ('browser.form.open', 'browser.form.fill',
                              'browser.form.submit', 'browser.form.receipt')
            for stage, tool in enumerate(expected_tools):
                approval = await next_approval(previous)
                self.assertEqual(approval['action']['tool'], tool)
                if stage == 1:
                    with self.assertRaisesRegex(
                            ValueError, 'form_learning_attach_requires_entry_approval'):
                        scheduler.prepare_remote_learning_consent(
                            job_id, roles=['system1'],
                            expires_at=(datetime.now(timezone.utc)
                                        + timedelta(minutes=15)).isoformat(),
                            attest_data_rights=True)
                self.assertEqual(approval['action']['arguments']['stage'], stage)
                self.assertEqual(approval['action']['arguments'],
                                 form_stage_arguments(plan, scheduler._remote_form_draft.binding_sha256,
                                                      'message', stage))
                self.assertNotIn(value, json.dumps(approval['action']['arguments']))
                if stage == 0:
                    self.assertEqual(self.server.paths, [])
                elif stage == 1:
                    self.assertEqual(self.server.paths, ['/entry'])
                elif stage == 2:
                    self.assertEqual(self.server.posts, [])
                else:
                    self.assertEqual(self.server.posts, [('/submit', body)])
                    self.assertEqual(self.server.paths, ['/entry'])
                with self.assertRaises(AOSFault):
                    scheduler.respond(approval['approval_id'], '0' * 64, True)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            job = store.connection.execute('SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                                           (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 4)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                (job['run_id'],)).fetchone()[0], 1)
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_form_bindings WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(binding['plan_sha256'], digest(plan.model_dump()))
            source = inspect_remote_form_learning_source(
                database, job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertTrue(source['transport_readback_verified'])
            self.assertEqual(source['approved_stage_count'], 4)
            self.assertFalse(source['site_outcome_verified'])
            self.assertFalse(source['collection_authorized'])
            self.server.asset_responses = {
                '/oracle': ('application/json', b'{"status":"saved"}')}
            oracle_plan = plan_remote_form_json_oracle(
                database, job['run_id'], profiles=self.profiles.root,
                profile_sha256=self.checksum, form_plan_sha256=digest(plan.model_dump()),
                url=origin + '/oracle', field_key='status',
                expected_value_sha256=digest({'value': 'saved'}))
            with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
                probe_remote_form_json_oracle(
                    database, job['run_id'], profiles=self.profiles.root,
                    plan=oracle_plan, confirm_plan_sha256='0' * 64,
                    tls_context=self.tls_context)
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            oracle = probe_remote_form_json_oracle(
                database, job['run_id'], profiles=self.profiles.root,
                plan=oracle_plan, confirm_plan_sha256=digest(oracle_plan.model_dump()),
                tls_context=self.tls_context)
            self.assertTrue(oracle.declared_value_matched)
            self.assertFalse(oracle.site_outcome_verified)
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle'])
            mismatch_plan = oracle_plan.model_copy(update={
                'expected_value_sha256': digest({'value': 'not-saved'})})
            with self.assertRaisesRegex(ValueError, 'declared_value_differs'):
                probe_remote_form_json_oracle(
                    database, job['run_id'], profiles=self.profiles.root,
                    plan=mismatch_plan,
                    confirm_plan_sha256=digest(mismatch_plan.model_dump()),
                    tls_context=self.tls_context)
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle', '/oracle'])
            with self.assertRaises(ValueError):
                plan_remote_form_json_oracle(
                    database, job['run_id'], profiles=self.profiles.root,
                    profile_sha256=self.checksum, form_plan_sha256=digest(plan.model_dump()),
                    url='https://other.example.com/oracle', field_key='status',
                    expected_value_sha256=digest({'value': 'saved'}))
            tampered = self.root / 'tampered-form-task.sqlite'
            with contextlib.closing(sqlite3.connect(tampered)) as copy, copy:
                store.connection.backup(copy)
                copy.execute('''UPDATE verifications SET actual_json=? WHERE run_id=?''',
                             (canonical({'url': plan.receipt_url,
                                         'title_sha256': '0' * 64,
                                         'heading_sha256': '0' * 64,
                                         'plan_sha256': digest(plan.model_dump()),
                                         'submit_request_sha256': '0' * 64}), job['run_id']))
            tampered.chmod(0o600)
            with self.assertRaises(ValueError):
                inspect_remote_form_learning_source(
                    tampered, job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
            self.assertNotIn(value, '\n'.join(store.connection.iterdump()))
            self.assertNotIn(scheduler.completed_runtime.relay.client_token,
                             '\n'.join(store.connection.iterdump()))
            denied_job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                            'browser_remote_form')['job_id']
            previous = None
            for stage in range(3):
                approval = await next_approval(previous)
                self.assertEqual(approval['action']['tool'], expected_tools[stage])
                scheduler.respond(approval['approval_id'], approval['action_sha256'],
                                  stage != 2)
                previous = approval['approval_id']
            try:
                await scheduler.task
            except asyncio.CancelledError:
                pass
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (denied_job_id,)).fetchone()[0], 'cancelled')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(self.server.paths, ['/entry', '/receipt', '/oracle', '/oracle', '/entry'])
            self.assertFalse(self.socket_path.exists())
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_REAL_BROWSER_TASK_TESTS')),
        'Explicit real Decider approved HTTPS form source test')
    def test_real_decider_form_source_has_only_actual_s1_events(self):
        value = 'form-private-real-947'
        body = b'message=form-private-real-947'
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'real-form-source.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database), engine,
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_form_plan=plan,
            remote_form_field_name='message', remote_form_value=value,
            remote_form_tls_context=self.tls_context,
            remote_learning_consents_dir=self.root / 'form-consents',
            remote_learning_stream_dir=self.root / 'form-outbox')
        owner = controller.state()

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            consent_store = RemoteLearningConsents(self.root / 'form-consents')
            outbox_dir = self.root / 'form-outbox'
            consent_sha256 = None
            for stage, tool in enumerate(('browser.form.open', 'browser.form.fill',
                                          'browser.form.submit', 'browser.form.receipt')):
                for _attempt in range(3000):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                self.assertEqual(approval['action']['tool'], tool)
                run_id = store.connection.execute(
                    'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0]
                if stage == 0:
                    consent = preview_remote_learning_consent(
                        database, run_id, profiles=self.profiles.root,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()),
                        roles=['system1', 'system2'],
                        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                        attest_data_rights=True, scope='remote_form_model_metadata_only')
                    consent_sha256 = digest(consent.model_dump())
                    preview_cli = subprocess.run([
                        sys.executable, '-m', 'aos.remote_learning_consent',
                        '--database', str(database), '--run-id', run_id,
                        '--profiles', str(self.profiles.root),
                        '--selected-profile-sha256', self.checksum,
                        '--selected-plan-sha256', digest(plan.model_dump()),
                        '--scope', 'remote_form_model_metadata_only',
                        '--roles', 'system1', 'system2',
                        '--expires-at', consent.expires_at, '--attest-data-rights',
                        '--store', str(consent_store.root)],
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(preview_cli.returncode, 0, preview_cli.stderr)
                    self.assertEqual(json.loads(preview_cli.stdout)['consent_sha256'],
                                     consent_sha256)
                    consent_store.register(
                        consent, confirm_sha256=consent_sha256,
                        database=database, profiles=self.profiles.root)
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        scheduler.attach_remote_learning(job_id, '0' * 64)
                    attached = scheduler.attach_remote_learning(job_id, consent_sha256)
                    self.assertTrue(attached['enabled'])
                    self.assertEqual(attached['entries_by_role'], {'system1': 0, 'system2': 0})
                status = scheduler.remote_learning_status(job_id)
                self.assertEqual(status['entries_by_role'], {'system1': stage, 'system2': 0})
                poll = poll_remote_form_learning_stream(
                    database, profiles=self.profiles.root, consents=consent_store.root,
                    consent_sha256=consent_sha256, outbox_dir=outbox_dir)
                self.assertEqual(poll['approved_stage_count'], stage)
                self.assertEqual(poll['entries_by_role'], {'system1': stage, 'system2': 0})
                self.assertEqual(poll['new_entries'], 0)
                repeat = poll_remote_form_learning_stream(
                    database, profiles=self.profiles.root, consents=consent_store.root,
                    consent_sha256=consent_sha256, outbox_dir=outbox_dir)
                self.assertEqual(repeat['new_entries'], 0)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            job = store.connection.execute(
                'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            report = inspect_remote_form_learning_source(
                database, job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual(len(report['source_event_ids_by_role']['system1']), 4)
            self.assertEqual(report['source_event_ids_by_role']['system2'], [])
            self.assertFalse(report['training_ready'])
            submit = store.connection.execute('''SELECT step_id,decision_id FROM actions
                WHERE run_id=? AND tool='browser.form.submit' ''', (job['run_id'],)).fetchone()
            submit_event_id = next(event['event_id'] for event in
                                   review_learning_events(database, job['run_id'])['events']
                                   if event['role'] == 'system1'
                                   and event['source']['step_id'] == submit['step_id']
                                   and event['source']['decision_id'] == submit['decision_id'])
            verification_id = store.connection.execute('''SELECT verification_id
                FROM verifications WHERE run_id=?''', (job['run_id'],)).fetchone()[0]
            page_source = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
            page = SitePageDraft.model_validate({**page_source,
                'profile_sha256': self.checksum, 'origin': origin,
                'route_template': '/entry', 'outgoing_page_keys': []})
            pages = SiteKnowledgeStore(self.root / 'form-skill-pages', self.profiles)
            page_sha256 = digest(page.model_dump())
            pages.register(page, confirm_sha256=page_sha256)
            skill_source = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
            skill = SiteSkillDraft.model_validate({**skill_source,
                'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
                'task_key': self.draft.task.task_key,
                'source_event_ids': [submit_event_id],
                'source_verification_ids': [verification_id]})
            skills = SiteSkillStore(self.root / 'form-skills', self.profiles, pages)
            skill_sha256 = digest(skill.model_dump())
            skills.register(skill, confirm_sha256=skill_sha256)
            case_fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
            plan_value = case_fixture['plan']
            cases = [dict(item) for item in plan_value['cases']]
            cases[0]['parameter_variant_sha256'] = parameter_variant_sha256({'record-query': value})
            validation_plan = SiteSkillValidationPlan.model_validate({**plan_value,
                'skill_sha256': skill_sha256, 'profile_sha256': self.checksum,
                'page_draft_sha256': page_sha256,
                'task_key': self.draft.task.task_key, 'cases': cases})
            input_value = case_fixture['case_inputs']
            inputs = SiteSkillCaseInputs.model_validate({**input_value,
                'plan_sha256': digest(validation_plan.model_dump()),
                'cases': [{**input_value['cases'][0], 'parameters': {'record-query': value}},
                          *input_value['cases'][1:]]})
            mappings = [SiteSkillFormFieldBinding(parameter_key='record-query',
                                                  form_field_name='message')]

            def audit_case(selected_database=database, selected_run_id=job['run_id'],
                           selected_inputs=inputs):
                return audit_site_skill_form_execution(
                    skills, validation_plan, selected_inputs, 'dev-query',
                    self.profiles, self.draft.task, plan, mappings,
                    selected_database, selected_run_id)

            case_report = audit_case()
            self.assertTrue(case_report['transport_execution_verified'])
            self.assertTrue(case_report['submit_decision_source_bound'])
            self.assertEqual(case_report['submit_event_id'], submit_event_id)
            self.assertFalse(case_report['skill_executed'])
            self.assertFalse(case_report['source_parameters_bound'])
            self.assertFalse(case_report['site_outcome_verified'])
            self.assertNotIn(value, canonical(case_report))
            sources = self.root / 'form-skill-sources'
            sources.mkdir(mode=0o700)
            source_values = {
                'plan': validation_plan.model_dump(mode='json'),
                'cases': inputs.model_dump(mode='json'),
                'task': self.draft.task.model_dump(mode='json'),
                'form_plan': plan.model_dump(mode='json'),
                'field_bindings': [item.model_dump(mode='json') for item in mappings]}
            for name, source_value in source_values.items():
                source_file = sources / (name + '.json')
                source_file.write_bytes(canonical(source_value).encode())
                source_file.chmod(0o600)
            command = [sys.executable, '-m', 'aos.site_skill_form_execution',
                       '--database', str(database), '--run-id', job['run_id'],
                       '--profiles', str(self.profiles.root), '--pages', str(pages.root),
                       '--store', str(skills.root),
                       '--plan', str(sources / 'plan.json'), '--plan-sha256', digest(source_values['plan']),
                       '--cases', str(sources / 'cases.json'), '--cases-sha256', digest(source_values['cases']),
                       '--skill-sha256', skill_sha256, '--case-key', 'dev-query',
                       '--task-file', str(sources / 'task.json'), '--task-sha256', digest(source_values['task']),
                       '--form-plan-file', str(sources / 'form_plan.json'),
                       '--form-plan-sha256', digest(source_values['form_plan']),
                       '--field-bindings-file', str(sources / 'field_bindings.json'),
                       '--field-binding-sha256', digest(source_values['field_bindings'])]
            command_result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(command_result.returncode, 0, command_result.stderr)
            self.assertEqual(json.loads(command_result.stdout), case_report)
            self.assertNotIn(value, command_result.stdout)
            wrong_hash = list(command)
            wrong_hash[wrong_hash.index('--field-binding-sha256') + 1] = '0' * 64
            rejected = subprocess.run(wrong_hash, capture_output=True, text=True, timeout=20)
            self.assertEqual(rejected.returncode, 1)
            self.assertNotIn(value, rejected.stderr)
            with self.assertRaises(ValueError):
                audit_case(selected_run_id='missing-run')
            changed_inputs = SiteSkillCaseInputs.model_validate({**inputs.model_dump(),
                'cases': [{**inputs.cases[0].model_dump(),
                           'parameters': {'record-query': 'wrong'}},
                          *[item.model_dump() for item in inputs.cases[1:]]]})
            with self.assertRaises(ValueError):
                audit_case(selected_inputs=changed_inputs)
            with patch.object(skills, 'get', return_value=skill.model_copy(update={
                    'source_event_ids': ['learning-' + 'f' * 64]})):
                with self.assertRaises(ValueError):
                    audit_case()
            with patch.object(skills, 'get', return_value=skill.model_copy(update={
                    'source_verification_ids': ['verification-wrong']})):
                with self.assertRaises(ValueError):
                    audit_case()
            settled = scheduler.remote_learning_status(job_id)
            self.assertEqual(settled['state'], 'synced')
            self.assertEqual(settled['entries_by_role'], {'system1': 4, 'system2': 0})
            final = poll_remote_form_learning_stream(
                database, profiles=self.profiles.root, consents=consent_store.root,
                consent_sha256=consent_sha256, outbox_dir=outbox_dir)
            self.assertEqual(final['approved_stage_count'], 4)
            self.assertEqual(final['entries_by_role'], {'system1': 4, 'system2': 0})
            self.assertEqual(final['new_entries'], 0)
            cli = subprocess.run([
                sys.executable, '-m', 'aos.remote_form_learning_stream',
                '--database', str(database), '--profiles', str(self.profiles.root),
                '--consents', str(consent_store.root),
                '--consent-sha256', consent_sha256, '--outbox-dir', str(outbox_dir)],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['total_entries'], 4)
            self.assertNotIn(value, cli.stdout)
            tampered = self.root / 'tampered-real-form-stream.sqlite'
            with sqlite3.connect(tampered) as copy:
                store.connection.backup(copy)
                copy.execute('''UPDATE desktop_approvals SET envelope_json='{}'
                    WHERE job_id=? AND status='consumed' ''', (job_id,))
            tampered.chmod(0o600)
            tampered_outbox = self.root / 'tampered-form-outbox'
            with self.assertRaises(ValueError):
                poll_remote_form_learning_stream(
                    tampered, profiles=self.profiles.root, consents=consent_store.root,
                    consent_sha256=consent_sha256, outbox_dir=tampered_outbox)
            with self.assertRaises(ValueError):
                audit_case(selected_database=tampered)
            self.assertFalse(tampered_outbox.exists())
            with self.assertRaises(ValueError):
                poll_remote_static_learning_stream(
                    database, profiles=self.profiles.root, consents=consent_store.root,
                    consent_sha256=consent_sha256, outbox_dir=outbox_dir)
            revoked = revoke_remote_learning(
                consents=consent_store.root, consent_sha256=consent_sha256,
                confirm_sha256=consent_sha256, outbox_dir=outbox_dir)
            self.assertTrue(revoked['outbox_purged'])
            with self.assertRaises(ValueError):
                poll_remote_form_learning_stream(
                    database, profiles=self.profiles.root, consents=consent_store.root,
                    consent_sha256=consent_sha256, outbox_dir=outbox_dir)
            self.assertEqual(self.server.posts, [('/submit', body)])
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu multi-field HTTPS form test')
    def test_scheduled_multifield_form_has_exact_order_and_one_post(self):
        fields = [{'name': 'subject', 'value': 'private-subject-947'},
                  {'name': 'message', 'value': 'private-message-947'}]
        body = b'subject=private-subject-947&message=private-message-947'
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit">'
                       b'<label for="subject">Subject</label>'
                       b'<input id="subject" name="subject" required>'
                       b'<label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<input type="submit" value="Save draft"></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'multi-form-task.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        options = dict(
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_form_plan=plan,
            remote_form_fields=fields, remote_form_tls_context=self.tls_context)
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, Settings(workspace=self.workspace, database=database),
                             FixtureDecisionEngine(), **{**options,
                                 'remote_form_fields': list(reversed(fields))})
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, Settings(workspace=self.workspace, database=database),
                             FixtureDecisionEngine(), **{**options,
                                 'remote_form_field_name': 'subject'})
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            FixtureDecisionEngine(), **options)
        for field in fields:
            self.assertNotIn(field['value'], canonical(scheduler.status()))
        owner = controller.state()

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            for stage, tool in enumerate(('browser.form.open', 'browser.form.fill',
                                          'browser.form.submit', 'browser.form.receipt')):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                self.assertEqual(approval['action']['tool'], tool)
                self.assertEqual(approval['action']['arguments'], form_stage_arguments(
                    plan, scheduler._remote_form_draft.binding_sha256,
                    ('subject', 'message'), stage))
                for field in fields:
                    self.assertNotIn(field['value'], canonical(approval['action']))
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            row = store.connection.execute(
                'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(row['status'], 'succeeded')
            self.assertEqual(self.server.paths, ['/entry', '/receipt'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 4)
            source = inspect_remote_form_learning_source(
                database, row['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual(source['submit_count'], 1)
            for field in fields:
                self.assertNotIn(field['value'], '\n'.join(store.connection.iterdump()))
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" type="email" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<input id="message" name="message" required>'
                b'<button type="submit">Save draft</button></form></html>')
            invalid_email_job = scheduler.start(owner['lease_id'], owner['generation'],
                                                'browser_remote_form')['job_id']
            previous = None
            for _stage in range(3):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (invalid_email_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" type="password" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<input id="message" name="message" required>'
                b'<button type="submit">Save draft</button></form></html>')
            password_job = scheduler.start(owner['lease_id'], owner['generation'],
                                           'browser_remote_form')['job_id']
            previous = None
            for _stage in range(2):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (password_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="message">Message</label>'
                b'<input id="message" name="message" required>'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<button type="submit">Save draft</button></form></html>')
            rejected_job = scheduler.start(owner['lease_id'], owner['generation'],
                                           'browser_remote_form')['job_id']
            previous = None
            for _stage in range(2):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (rejected_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<textarea id="message" name="message" required></textarea>'
                b'<button type="submit">Save draft</button></form></html>')
            textarea_job = scheduler.start(owner['lease_id'], owner['generation'],
                                           'browser_remote_form')['job_id']
            previous = None
            for stage in range(4):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                self.assertEqual(approval['action']['arguments'], form_stage_arguments(
                    plan, scheduler._remote_form_draft.binding_sha256,
                    ('subject', 'message'), stage))
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (textarea_job,)).fetchone()[0], 'succeeded')
            self.assertEqual(self.server.posts, [('/submit', body), ('/submit', body)])
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<select id="message" name="message" required>'
                b'<option value="">Choose</option>'
                b'<option value="private-message-947">Private choice</option>'
                b'</select><button type="submit">Save draft</button></form></html>')
            select_job = scheduler.start(owner['lease_id'], owner['generation'],
                                         'browser_remote_form')['job_id']
            previous = None
            for _stage in range(4):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (select_job,)).fetchone()[0], 'succeeded')
            self.assertEqual(self.server.posts, [('/submit', body)] * 3)
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<select id="message" name="message" required>'
                b'<option value="private-message-947" disabled>Unavailable</option>'
                b'</select><button type="submit">Save draft</button></form></html>')
            disabled_select_job = scheduler.start(owner['lease_id'], owner['generation'],
                                                  'browser_remote_form')['job_id']
            previous = None
            for _stage in range(2):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (disabled_select_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)] * 3)
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<select id="message" name="message" required>'
                b'<option value="private-message-947">First</option>'
                b'<option value="private-message-947">Duplicate</option>'
                b'</select><button type="submit">Save draft</button></form></html>')
            duplicate_select_job = scheduler.start(owner['lease_id'], owner['generation'],
                                                   'browser_remote_form')['job_id']
            previous = None
            for _stage in range(2):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (duplicate_select_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)] * 3)
            self.server.route_bodies['/entry'] = (
                b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                b'<form id="draft" method="post" action="/submit">'
                b'<label for="subject">Subject</label>'
                b'<input id="subject" name="subject" required>'
                b'<label for="message">Message</label>'
                b'<textarea id="message" name="message" required></textarea>'
                b'<button type="submit">Save draft</button></form>'
                b'<input form="draft" type="hidden" name="hidden" value="extra"></html>')
            hidden_job = scheduler.start(owner['lease_id'], owner['generation'],
                                         'browser_remote_form')['job_id']
            previous = None
            for _stage in range(2):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (hidden_job,)).fetchone()[0], 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)] * 3)
            for submit_control in (
                    b'<input type="submit" name="commit" value="Save draft">',
                    b'<button formaction="/submit">Save draft</button>',
                    b'<fieldset disabled><button>Save draft</button></fieldset>',
                    b''):
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                    b'<form id="draft" method="post" action="/submit">'
                    b'<label for="subject">Subject</label>'
                    b'<input id="subject" name="subject" required>'
                    b'<label for="message">Message</label>'
                    b'<input id="message" name="message" required>'
                    + submit_control + b'</form>'
                    + (b'<button form="draft">Save draft</button>'
                       if not submit_control else b'') + b'</html>')
                unsafe_job = scheduler.start(owner['lease_id'], owner['generation'],
                                             'browser_remote_form')['job_id']
                previous = None
                for _stage in range(2):
                    for _attempt in range(1500):
                        approval = scheduler.status()['approval']
                        if approval is not None and approval['approval_id'] != previous:
                            break
                        if scheduler.task.done():
                            break
                        await asyncio.sleep(0.02)
                    self.assertIsNotNone(approval)
                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                    previous = approval['approval_id']
                await scheduler.task
                self.assertEqual(store.connection.execute(
                    'SELECT status FROM desktop_tasks WHERE job_id=?',
                    (unsafe_job,)).fetchone()[0], 'failed')
                self.assertEqual(self.server.posts, [('/submit', body)] * 3)
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu exact hidden HTTPS field test')
    def test_scheduled_form_exact_declared_hidden_field(self):
        fields = [{'name': 'subject', 'value': 'private-subject-947'},
                  {'name': 'message', 'value': 'private-message-947'},
                  {'name': 'csrf_token', 'value': 'private-token-947'}]
        body = b'subject=private-subject-947&message=private-message-947&csrf_token=private-token-947'
        origin = self.profile.allowed_origins[0]
        prefix = (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                  b'<form id="draft" method="post" action="/submit">'
                  b'<label for="subject">Subject</label>'
                  b'<input id="subject" name="subject" required>'
                  b'<label for="message">Message</label>'
                  b'<textarea id="message" name="message" required></textarea>')
        hidden = b'<input type="hidden" name="csrf_token" value="private-token-947">'
        button = b'<button type="submit">Save draft</button></form></html>'
        self.server.route_bodies = {
            '/entry': prefix + hidden + button,
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'hidden-form-task.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            FixtureDecisionEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_form_plan=plan,
            remote_form_fields=fields, remote_form_tls_context=self.tls_context)
        self.assertNotIn('private-token-947', canonical(scheduler.status()))
        owner = controller.state()

        async def run_form(approvals):
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            for _stage in range(approvals):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                self.assertNotIn('private-token-947', canonical(approval['action']))
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            return store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0]

        async def scenario():
            self.assertEqual(await run_form(4), 'succeeded')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertNotIn('private-token-947', '\n'.join(store.connection.iterdump()))
            self.server.route_bodies['/entry'] = prefix + hidden.replace(
                b'private-token-947', b'changed-token-947') + button
            self.assertEqual(await run_form(2), 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.server.route_bodies['/entry'] = prefix + button.replace(
                b'</html>', b'') + hidden.replace(
                    b'<input ', b'<input form="draft" ', 1) + b'</html>'
            self.assertEqual(await run_form(2), 'failed')
            self.assertEqual(self.server.posts, [('/submit', body)])
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_FORM_COHORT_REAL_DECIDER_TESTS') == '1',
                         'Explicit owned three-run real Decider skill form cohort test')
    def test_real_decider_three_distinct_form_state_runs_bind_synthetic_cohort(self):
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1 id="outcome">Empty synthetic state</h1></html>'
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'form-cohort.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        values = ('alpha-cohort', 'beta-cohort', 'gamma-cohort')
        case_keys = ('dev-query', 'heldout-alpha', 'heldout-beta')
        run_selections = []
        source_event_id = None
        source_verification_ids = None

        async def scenario():
            nonlocal source_event_id, source_verification_ids
            try:
                for index, value in enumerate(values):
                    self.server.paths.clear()
                    self.server.posts.clear()
                    after = (f'<html><h1 id="outcome">{value}</h1></html>').encode()
                    self.server.state_bodies = {False: before, True: after}
                    body = form_body((('message', value),))
                    form_plan = plan_web_https_form(
                        self.profiles, self.draft.task,
                        submit_url=origin + '/submit', receipt_url=origin + '/receipt',
                        body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
                    state_plan = WebHTTPSFormStatePlan(
                        profile_sha256=self.checksum,
                        task_sha256=digest(self.draft.task.model_dump()),
                        form_plan_sha256=digest(form_plan.model_dump()),
                        state_url=origin + '/state',
                        expected_before_sha256=hashlib.sha256(before).hexdigest(),
                        expected_after_sha256=hashlib.sha256(after).hexdigest(),
                        marker_id='outcome',
                        expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
                        expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'),
                        submitted_field_name='message')
                    scheduler = DesktopScheduler(
                        controller, Settings(workspace=self.workspace, database=database),
                        engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                        desktop_browser=True,
                        remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                        remote_entry_profiles=self.profiles,
                        remote_entry_profile_sha256=self.checksum,
                        remote_entry_task=self.draft.task, remote_form_plan=form_plan,
                        remote_form_field_name='message', remote_form_value=value,
                        remote_form_tls_context=self.tls_context,
                        remote_form_state_plan=state_plan)
                    try:
                        owner = controller.state()
                        job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                                 'browser_remote_form')['job_id']
                        previous = None
                        for tool in ('browser.form.open', 'browser.form.state_before',
                                     'browser.form.fill', 'browser.form.submit',
                                     'browser.form.receipt', 'browser.form.state_after'):
                            for _attempt in range(1500):
                                approval = scheduler.status()['approval']
                                if approval is not None and approval['approval_id'] != previous:
                                    break
                                if scheduler.task.done():
                                    self.fail('Cohort form stopped before exact approval')
                                await asyncio.sleep(0.02)
                            else:
                                self.fail('Cohort form did not request approval')
                            self.assertEqual(approval['action']['tool'], tool)
                            scheduler.respond(approval['approval_id'],
                                              approval['action_sha256'], True)
                            previous = approval['approval_id']
                        await scheduler.task
                        job = store.connection.execute(
                            'SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                            (job_id,)).fetchone()
                        self.assertEqual(job['status'], 'succeeded')
                        self.assertEqual(self.server.posts, [('/submit', body)])
                        self.assertEqual(self.server.paths,
                                         ['/entry', '/state', '/receipt', '/state'])
                        self.assertEqual(store.connection.execute(
                            "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                            (job['run_id'],)).fetchone()[0], 6)
                        if index == 0:
                            submit = store.connection.execute('''SELECT step_id,decision_id
                                FROM actions WHERE run_id=? AND tool='browser.form.submit' ''',
                                (job['run_id'],)).fetchone()
                            source_event_id = next(event['event_id'] for event in
                                review_learning_events(database, job['run_id'])['events']
                                if event['role'] == 'system1'
                                and event['source']['step_id'] == submit['step_id']
                                and event['source']['decision_id'] == submit['decision_id'])
                            source_verification_ids = sorted(row[0] for row in
                                store.connection.execute('''SELECT verification_id
                                FROM verifications WHERE run_id=?''', (job['run_id'],)))
                        run_selections.append({'case_key': case_keys[index],
                            'run_id': job['run_id'], 'task': self.draft.task.model_dump(),
                            'form_plan': form_plan.model_dump(),
                            'state_plan': state_plan.model_dump()})
                    finally:
                        await scheduler.close()
            finally:
                await engine.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())
        page_source = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
        page = SitePageDraft.model_validate({**page_source,
            'profile_sha256': self.checksum, 'origin': origin,
            'route_template': '/entry', 'outgoing_page_keys': []})
        pages = SiteKnowledgeStore(self.root / 'cohort-pages', self.profiles)
        page_sha256 = digest(page.model_dump())
        pages.register(page, confirm_sha256=page_sha256)
        skill_source = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
        skill = SiteSkillDraft.model_validate({**skill_source,
            'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
            'task_key': self.draft.task.task_key, 'source_event_ids': [source_event_id],
            'source_verification_ids': source_verification_ids})
        skills = SiteSkillStore(self.root / 'cohort-skills', self.profiles, pages)
        skill_sha256 = digest(skill.model_dump())
        skills.register(skill, confirm_sha256=skill_sha256)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
        cases = [{**item, 'parameter_variant_sha256': parameter_variant_sha256(
            {'record-query': value})} for item, value in zip(fixture['plan']['cases'], values,
                                                              strict=True)]
        plan = SiteSkillValidationPlan.model_validate({**fixture['plan'],
            'skill_sha256': skill_sha256, 'profile_sha256': self.checksum,
            'page_draft_sha256': page_sha256, 'task_key': self.draft.task.task_key,
            'cases': cases})
        inputs = SiteSkillCaseInputs.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'plan_sha256': digest(plan.model_dump()),
            'cases': [{'case_key': key, 'parameters': {'record-query': value}}
                      for key, value in zip(case_keys, values, strict=True)]})
        bindings = [SiteSkillFormFieldBinding(parameter_key='record-query',
                                             form_field_name='message')]
        selection = SiteSkillFormCohortSelection.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'skill_sha256': skill_sha256, 'plan_sha256': digest(plan.model_dump()),
            'case_inputs_sha256': digest(inputs.model_dump()),
            'field_binding_sha256': digest([item.model_dump() for item in bindings]),
            'runs': run_selections})
        with patch('aos.site_skill_form_cohort.audit_snapshot', wraps=audit_snapshot) as audits:
            report = audit_site_skill_form_cohort(
                skills, plan, inputs, bindings, selection, database)
        self.assertEqual(audits.call_count, 1)
        self.assertEqual((report['development_count'], report['held_out_count']), (1, 2))
        self.assertTrue(report['source_ids_separated_from_held_out'])
        self.assertFalse(report['skill_executed'])
        self.assertFalse(report['site_outcome_verified'])
        self.assertFalse(report['held_out_independence_verified'])
        self.assertFalse(report['reviewed'])
        self.assertNotIn('alpha-cohort', canonical(report))
        self.assertNotIn('beta-cohort', canonical(report))
        self.assertNotIn('gamma-cohort', canonical(report))
        sources = self.root / 'cohort-sources'
        sources.mkdir(mode=0o700)
        source_values = {
            'plan': plan.model_dump(mode='json'),
            'cases': inputs.model_dump(mode='json'),
            'field-bindings': [item.model_dump(mode='json') for item in bindings],
            'selection': selection.model_dump(mode='json')}
        for name, value in source_values.items():
            source_file = sources / (name + '.json')
            source_file.write_bytes(canonical(value).encode())
            source_file.chmod(0o600)
        command = [sys.executable, '-m', 'aos.site_skill_form_cohort',
                   '--database', str(database), '--profiles', str(self.profiles.root),
                   '--pages', str(pages.root), '--store', str(skills.root),
                   '--plan', str(sources / 'plan.json'),
                   '--cases', str(sources / 'cases.json'),
                   '--field-bindings-file', str(sources / 'field-bindings.json'),
                   '--selection', str(sources / 'selection.json'),
                   '--selection-sha256', digest(selection.model_dump())]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        wrong_hash = list(command)
        wrong_hash[-1] = '0' * 64
        rejected = subprocess.run(wrong_hash, capture_output=True, text=True, timeout=30)
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(rejected.stdout, '')
        self.assertNotIn('alpha-cohort', rejected.stderr)
        wrong_run = selection.model_dump()
        wrong_run['runs'][2]['run_id'] = 'run-missing'
        with self.assertRaises(ValueError):
            audit_site_skill_form_cohort(
                skills, plan, inputs, bindings,
                SiteSkillFormCohortSelection.model_validate(wrong_run), database)
        wrong_state = selection.model_dump()
        wrong_state['runs'][1]['state_plan']['expected_after_marker_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            audit_site_skill_form_cohort(
                skills, plan, inputs, bindings,
                SiteSkillFormCohortSelection.model_validate(wrong_state), database)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_FORM_INVOCATION_REAL_DECIDER_TESTS') == '1',
                         'Explicit owned real-Decider typed invocation relay test')
    def test_real_decider_scheduled_typed_form_invocation_audit(self):
        with patch.dict(os.environ, {'AOS_FORM_STATE_REAL_DECIDER_TESTS': '1',
                                     'AOS_FORM_INVOCATION_REAL_DECIDER_TESTS': '1'}):
            self.test_scheduled_form_state_six_consumed_approvals_and_append_only_binding()

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu scheduled HTTPS form state test')
    def test_scheduled_form_state_six_consumed_approvals_and_append_only_binding(self):
        invocation_mode = os.environ.get('AOS_FORM_INVOCATION_REAL_DECIDER_TESTS') == '1'
        selected_value = 'alpha' if invocation_mode else 'Saved synthetic state'
        body = b'message=alpha' if invocation_mode else b'message=Saved+synthetic+state'
        origin = self.profile.allowed_origins[0]
        before = b'<html><h1 id="outcome">Empty synthetic state</h1></html>'
        after_text = 'alpha' if invocation_mode else 'Saved synthetic state'
        after = f'<html><h1 id="outcome">{after_text}</h1></html>'.encode()
        self.server.state_bodies = {False: before, True: after}
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        task_root, plan_root, value_root, state_root = (
            self.root / name for name in ('form-tasks', 'form-plans', 'form-values', 'form-states'))
        task, task_sha256 = preview_form_task(
            self.profiles, self.checksum, 'update-draft', 'synthetic-form-check',
            state_readback=True)
        registered_task_sha256, task_path = register_form_task(
            self.profiles, task_root, self.checksum, 'update-draft',
            'synthetic-form-check', task_sha256, state_readback=True)
        self.assertEqual(registered_task_sha256, task_sha256)
        form_plan_preview, form_plan_sha256 = preview_form_plan(
            self.profiles, task_root, self.checksum, task_sha256,
            origin + '/submit', origin + '/receipt', 'message', selected_value)
        registered_form_sha256, form_path, value_path, form_plan = register_form_plan(
            self.profiles, task_root, plan_root, value_root, self.checksum,
            task_sha256, origin + '/submit', origin + '/receipt', 'message',
            selected_value, form_plan_sha256)
        self.assertEqual(registered_form_sha256, form_plan_sha256)
        self.assertEqual(form_plan, form_plan_preview)
        self.assertEqual(local_app.private_read(value_path), selected_value.encode())
        state_arguments = dict(
            marker_id='outcome',
            before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
            after_marker_sha256=form_state_marker_sha256(after, 'outcome'),
            submitted_field_name='message')
        state_plan_preview, state_plan_sha256 = preview_form_state_plan(
            self.profiles, task_root, plan_root, self.checksum, task_sha256,
            form_plan_sha256, origin + '/state', hashlib.sha256(before).hexdigest(),
            hashlib.sha256(after).hexdigest(), **state_arguments)
        registered_state_sha256, state_path, state_plan = register_form_state_plan(
            self.profiles, task_root, plan_root, state_root, self.checksum,
            task_sha256, form_plan_sha256, origin + '/state',
            hashlib.sha256(before).hexdigest(), hashlib.sha256(after).hexdigest(),
            state_plan_sha256, **state_arguments)
        self.assertEqual(registered_state_sha256, state_plan_sha256)
        self.assertEqual(state_plan, state_plan_preview)

        invocation_context = None
        if invocation_mode:
            page_store = SiteKnowledgeStore(self.root / 'invocation-pages', self.profiles)
            page_fixture = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
            page = SitePageDraft.model_validate({**page_fixture,
                'profile_sha256': self.checksum,
                'application_key': self.profile.application_key,
                'tenant_key': self.profile.tenant_key,
                'account_role': self.profile.account_role,
                'origin': origin, 'route_template': '/entry',
                'outgoing_page_keys': []})
            page_sha256 = digest(page.model_dump())
            page_store.register(page, confirm_sha256=page_sha256)
            skill_store = SiteSkillStore(self.root / 'invocation-skills', self.profiles, page_store)
            skill_fixture = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
            skill = SiteSkillDraft.model_validate({**skill_fixture,
                'profile_sha256': self.checksum,
                'application_key': self.profile.application_key,
                'tenant_key': self.profile.tenant_key,
                'account_role': self.profile.account_role,
                'task_key': task.task_key, 'page_draft_sha256': page_sha256})
            skill_sha256 = digest(skill.model_dump())
            skill_store.register(skill, confirm_sha256=skill_sha256)
            binding_fixture = json.loads(
                (REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
            plan = SiteSkillValidationPlan.model_validate(binding_fixture['plan']).model_copy(update={
                'skill_sha256': skill_sha256, 'profile_sha256': self.checksum,
                'page_draft_sha256': page_sha256, 'task_key': task.task_key})
            plan = plan.model_copy(update={'cases': [case.model_copy(update={
                'parameter_variant_sha256': parameter_variant_sha256(
                    {'record-query': {'dev-query': 'alpha', 'heldout-alpha': 'beta',
                                      'heldout-beta': 'gamma'}[case.case_key]})
            }) for case in plan.cases]})
            inputs = SiteSkillCaseInputs.model_validate(binding_fixture['case_inputs']).model_copy(update={
                'plan_sha256': digest(plan.model_dump())})
            field_bindings = [SiteSkillFormFieldBinding(parameter_key='record-query',
                                                        form_field_name='message')]
            invocation = compile_site_skill_form_invocation(
                skill_store, plan, inputs, 'dev-query', self.profiles, task,
                form_plan, state_plan, field_bindings)
            invocation_context = (skill_store, plan, inputs, field_bindings,
                                  invocation, digest(invocation))
        for path in (task_path, form_path, value_path, state_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'form-state-task.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        settings = Settings(workspace=self.workspace, database=database)
        real_model = os.environ.get('AOS_FORM_STATE_REAL_DECIDER_TESTS') == '1'
        engine = (ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
            if real_model else FixtureDecisionEngine())
        options = dict(
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles,
            remote_entry_profile_sha256=self.checksum,
            remote_entry_task=task,
            remote_form_plan=form_plan,
            remote_form_field_name='message',
            remote_form_value=local_app.private_read(value_path).decode(),
            remote_form_tls_context=self.tls_context,
            remote_form_state_plan=state_plan)
        if invocation_context is not None:
            skill_store, skill_plan, case_inputs, field_bindings, invocation, invocation_sha256 = invocation_context
            options.update(
                remote_form_skill_invocation=invocation,
                remote_form_skill_invocation_sha256=invocation_sha256,
                remote_form_skill_revalidator=lambda: revalidate_site_skill_form_invocation(
                    invocation, skill_store, skill_plan, case_inputs, 'dev-query',
                    self.profiles, task, form_plan, state_plan, field_bindings))
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, settings, engine, **options,
                             remote_form_public_state_plan_sha256=digest(state_plan.model_dump()))
        consent_root = self.root / 'state-consents'
        outbox_root = self.root / 'state-outbox'
        scheduler = DesktopScheduler(
            controller, settings, engine, **options,
            remote_learning_consents_dir=consent_root,
            remote_learning_stream_dir=outbox_root)
        self.assertEqual(scheduler.status()['remote_form']['state_plan_sha256'],
                         digest(state_plan.model_dump()))

        async def scenario():
            owner = controller.state()
            with self.assertRaises(AOSFault):
                scheduler.start(owner['lease_id'], owner['generation'],
                                'browser_remote_form', approve_all=True)
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_form')['job_id']
            previous = None
            expected_tools = ('browser.form.open', 'browser.form.state_before',
                              'browser.form.fill', 'browser.form.submit',
                              'browser.form.receipt', 'browser.form.state_after')
            consent_sha256 = None
            for stage, tool in enumerate(expected_tools):
                for _attempt in range(1500):
                    approval = scheduler.status()['approval']
                    if approval is not None and approval['approval_id'] != previous:
                        break
                    if scheduler.task.done():
                        self.fail('State-bound form stopped before exact approval')
                    await asyncio.sleep(0.02)
                else:
                    self.fail('State-bound form did not request approval')
                self.assertEqual(approval['action']['tool'], tool)
                if stage:
                    self.assertEqual(
                        scheduler.remote_learning_status(job_id)['entries_by_role'],
                        {'system1': stage if real_model else 0, 'system2': 0})
                if stage == 0:
                    selection = {
                        'profiles': self.profiles.root,
                        'selected_profile_sha256': self.checksum,
                        'selected_plan_sha256': form_plan_sha256,
                        'roles': ['system1', 'system2'],
                        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                        'attest_data_rights': True}
                    run_id = store.connection.execute(
                        'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0]
                    with self.assertRaises(ValueError):
                        preview_remote_learning_consent(
                            database, run_id, **selection,
                            scope='remote_form_model_metadata_only')
                    with self.assertRaises(ValueError):
                        preview_remote_learning_consent(
                            database, run_id, **selection,
                            scope='remote_form_state_model_metadata_only',
                            selected_state_plan_sha256='0' * 64)
                    self.assertFalse(consent_root.exists())
                    consent_request = {
                        'job_id': job_id, 'roles': selection['roles'],
                        'expires_at': selection['expires_at'], 'attest_data_rights': True}
                    learning_origin = 'http://127.0.0.1:8765'
                    learning_app = create_console(
                        controller, 'synthetic-token', learning_origin, self.root,
                        database, scheduler, web_profiles_root=self.profiles.root)
                    async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=learning_app),
                            base_url=learning_origin,
                            headers={'Origin': learning_origin}) as client:
                        self.assertEqual((await client.post(
                            '/api/tasks/remote-learning/consent',
                            json=consent_request)).status_code, 401)
                        self.assertEqual((await client.post(
                            '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                        prepared = await client.post(
                            '/api/tasks/remote-learning/consent', json=consent_request)
                        self.assertEqual(prepared.status_code, 200, prepared.text)
                        consent_sha256 = prepared.json()['consent_sha256']
                        self.assertFalse(prepared.json()['registered'])
                        self.assertFalse(consent_root.exists())
                        registered = await client.post(
                            '/api/tasks/remote-learning/consent',
                            json={**consent_request, 'confirm_sha256': consent_sha256})
                        self.assertEqual(registered.status_code, 200, registered.text)
                        self.assertTrue(registered.json()['registered'])
                        attached = await client.post('/api/tasks/remote-learning',
                                                     json={'job_id': job_id,
                                                           'consent_sha256': consent_sha256})
                        self.assertEqual(attached.status_code, 200, attached.text)
                        self.assertEqual(attached.json()['state'], 'collecting')
                    consent = RemoteLearningConsents(consent_root).get(consent_sha256)
                    self.assertEqual(consent.scope, 'remote_form_state_model_metadata_only')
                    self.assertEqual(consent.state_plan_sha256, state_plan_sha256)
                    self.assertEqual(poll_remote_form_learning_stream(
                        database, profiles=self.profiles.root, consents=consent_root,
                        consent_sha256=consent_sha256,
                        outbox_dir=outbox_root)['approved_stage_count'], 0)
                arguments = approval['action']['arguments']
                if tool.startswith('browser.form.state_'):
                    phase = 'before' if tool.endswith('before') else 'after'
                    self.assertEqual(arguments, form_state_action_arguments(
                        state_plan, scheduler._remote_form_draft.binding_sha256, phase))
                    self.assertEqual(arguments['expected_marker_sha256'],
                                     state_plan.expected_before_marker_sha256 if phase == 'before'
                                     else state_plan.expected_after_marker_sha256)
                self.assertNotIn(body.decode(), json.dumps(arguments))
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                previous = approval['approval_id']
            await scheduler.task
            job = store.connection.execute(
                'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(self.server.paths, ['/entry', '/state', '/receipt', '/state'])
            self.assertEqual(self.server.posts, [('/submit', body)])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 6)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                (job['run_id'],)).fetchone()[0], 2)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                (job['run_id'],)).fetchone()[0], 6 if real_model else 0)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system2'",
                (job['run_id'],)).fetchone()[0], 0)
            learning = poll_remote_form_learning_stream(
                database, profiles=self.profiles.root, consents=consent_root,
                consent_sha256=consent_sha256, outbox_dir=outbox_root)
            self.assertEqual(learning['approved_stage_count'], 6)
            self.assertEqual(learning['new_entries'], 0)
            self.assertEqual(learning['entries_by_role'],
                             {'system1': 6 if real_model else 0, 'system2': 0})
            self.assertEqual(learning['state_plan_sha256'], state_plan_sha256)
            self.assertEqual(scheduler.remote_learning_status(job_id)['state'], 'synced')
            state_binding = store.connection.execute(
                'SELECT * FROM desktop_remote_form_state_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            self.assertEqual(state_binding['state_plan_sha256'], digest(state_plan.model_dump()))
            self.assertEqual(state_binding['form_plan_sha256'], digest(form_plan.model_dump()))
            self.assertEqual(json.loads(state_binding['state_plan_json'])['marker_id'], 'outcome')
            self.assertEqual(json.loads(state_binding['state_plan_json'])['submitted_field_name'], 'message')
            state_result = json.loads(store.connection.execute(
                "SELECT result_json FROM actions WHERE run_id=? AND tool='browser.form.state_after'",
                (job['run_id'],)).fetchone()[0])
            self.assertTrue(state_result['submitted_value_readback_verified'])
            self.assertEqual(state_result['submitted_field_name_sha256'],
                             digest({'field_name': 'message'}))
            self.assertNotIn(selected_value, '\n'.join(store.connection.iterdump()))
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "UPDATE desktop_remote_form_state_bindings SET state_plan_sha256='" +
                    '0' * 64 + "' WHERE job_id=?", (job_id,))
            store.connection.rollback()
            self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            if real_model:
                self.server.posts.clear()
                self.server.paths.clear()
                next_job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                              'browser_remote_form')['job_id']
                previous = None
                for tool in expected_tools:
                    for _attempt in range(1500):
                        approval = scheduler.status()['approval']
                        if approval is not None and approval['approval_id'] != previous:
                            break
                        if scheduler.task.done():
                            self.fail('Second state-bound form stopped before exact approval')
                        await asyncio.sleep(0.02)
                    else:
                        self.fail('Second state-bound form did not request approval')
                    self.assertEqual(approval['action']['tool'], tool)
                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                    previous = approval['approval_id']
                await scheduler.task
                self.assertEqual(store.connection.execute(
                    'SELECT status FROM desktop_tasks WHERE job_id=?',
                    (next_job_id,)).fetchone()[0], 'succeeded')
                self.assertEqual(self.server.posts, [('/submit', body)])
                with (patch('aos.remote_form_repeat.audit_snapshot',
                            wraps=audit_snapshot) as repeat_audits,
                      patch('aos.remote_form_learning_source.audit_snapshot',
                            wraps=audit_snapshot) as source_audits):
                    repeated = inspect_remote_form_repeats(
                        database, profiles=self.profiles.root,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=form_plan_sha256,
                        selected_state_plan_sha256=state_plan_sha256)
                self.assertEqual(repeat_audits.call_count, 1)
                self.assertEqual(source_audits.call_count, 0)
                self.assertEqual((repeated['bound_attempts'], repeated['transport_verified'],
                                  repeated['failed_or_cancelled']), (2, 2, 0))
                self.assertEqual(repeated['model_call_count'], 12)
                self.assertEqual(repeated['first_action_samples'], 2)
                self.assertEqual(repeated['approval_window_count'], 12)
                self.assertGreater(repeated['approval_window_sum_ms'], 0)
                self.assertEqual(len(repeated['post_approval_stage_ms']), 6)
                self.assertTrue(all(timing['samples'] == 2
                                    and timing['p95_ms'] >= timing['p50_ms'] >= 0
                                    for timing in repeated['post_approval_stage_ms'].values()))
                self.assertLessEqual(repeated['elapsed_excluding_approval_p95_ms'],
                                     repeated['elapsed_p95_ms'])
                self.assertFalse(repeated['site_outcome_verified'])
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=learning_app),
                        base_url=learning_origin,
                        headers={'Origin': learning_origin}) as client:
                    self.assertEqual((await client.get('/api/remote-form/repeats')).status_code,
                                     401)
                    self.assertEqual((await client.post(
                        '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                    measured = await client.get('/api/remote-form/repeats')
                    self.assertEqual(measured.status_code, 200, measured.text)
                    self.assertEqual(measured.json()['mode'],
                                     'read_only_post_admission_form_state_repeats')
                    self.assertEqual(measured.json()['bound_attempts'], 2)
            await scheduler.close()
            selected = {
                'profiles': self.profiles.root,
                'selected_profile_sha256': self.checksum,
                'selected_plan_sha256': form_plan_sha256}
            with self.assertRaises(ValueError):
                inspect_remote_form_learning_source(database, job['run_id'], **selected)
            with self.assertRaises(ValueError):
                inspect_remote_form_learning_source(
                    database, job['run_id'], **selected,
                    selected_state_plan_sha256='0' * 64)
            source = inspect_remote_form_learning_source(
                database, job['run_id'], **selected,
                selected_state_plan_sha256=state_plan_sha256)
            self.assertEqual(source['approved_stage_count'], 6)
            self.assertTrue(source['declared_state_readback_verified'])
            self.assertFalse(source['site_outcome_verified'])
            self.assertEqual(len(source['source_event_ids_by_role']['system1']),
                             6 if real_model else 0)
            if invocation_context is not None:
                skill_store, skill_plan, case_inputs, field_bindings, invocation, invocation_sha256 = invocation_context
                audited = audit_site_skill_form_invocation_execution(
                    skill_store, skill_plan, case_inputs, 'dev-query', self.profiles,
                    task, form_plan, state_plan, field_bindings, invocation, database,
                    job['run_id'])
                self.assertTrue(audited['invocation_execution_verified'])
                self.assertEqual(audited['consumed_approval_count'], 6)
                self.assertEqual(audited['submit_count'], 1)
                self.assertEqual(tuple(stage['stage'] for stage in audited['stages']),
                                 ('open_entry', 'read_state_before', 'fill_form',
                                  'submit_form', 'read_receipt', 'read_state_after'))
                source_root = self.root / 'invocation-audit-sources'
                source_root.mkdir(mode=0o700)
                source_values = {
                    'plan': skill_plan.model_dump(mode='json'),
                    'cases': case_inputs.model_dump(mode='json'),
                    'task': task.model_dump(mode='json'),
                    'form_plan': form_plan.model_dump(mode='json'),
                    'state_plan': state_plan.model_dump(mode='json'),
                    'field_bindings': [item.model_dump(mode='json') for item in field_bindings]}
                source_paths = {}
                for name, source_value in source_values.items():
                    source_file = source_root / (name + '.json')
                    source_file.write_bytes(canonical(source_value).encode())
                    source_file.chmod(0o600)
                    source_paths[name] = source_file
                command = [sys.executable, '-m', 'aos.site_skill_form_invocation_audit',
                           '--database', str(database), '--run-id', job['run_id'],
                           '--profiles', str(self.profiles.root),
                           '--pages', str(skill_store.pages.root), '--store', str(skill_store.root),
                           '--plan', str(source_paths['plan']),
                           '--plan-sha256', digest(source_values['plan']),
                           '--cases', str(source_paths['cases']),
                           '--cases-sha256', digest(source_values['cases']),
                           '--skill-sha256', invocation['skill_sha256'],
                           '--case-key', 'dev-query', '--task-file', str(source_paths['task']),
                           '--task-sha256', digest(source_values['task']),
                           '--form-plan-file', str(source_paths['form_plan']),
                           '--form-plan-sha256', digest(source_values['form_plan']),
                           '--state-plan-file', str(source_paths['state_plan']),
                           '--state-plan-sha256', digest(source_values['state_plan']),
                           '--field-bindings-file', str(source_paths['field_bindings']),
                           '--field-binding-sha256', digest(source_values['field_bindings']),
                           '--invocation-sha256', invocation_sha256]
                cli_result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
                self.assertEqual(json.loads(cli_result.stdout), audited)
                for option, wrong_hash in (('--invocation-sha256', '0' * 64),
                                           ('--skill-sha256', '0' * 64)):
                    wrong = list(command)
                    wrong[wrong.index(option) + 1] = wrong_hash
                    rejected = subprocess.run(wrong, capture_output=True, text=True, timeout=30)
                    self.assertEqual(rejected.returncode, 1)
                    self.assertEqual(rejected.stdout, '')
                    self.assertNotIn(selected_value, rejected.stderr)
                for mutation in ('missing_initial_pin', 'expired_approval',
                                 'reversed_approval_order', 'duplicate_submit'):
                    tampered = self.root / f'invocation-audit-{mutation}.sqlite'
                    backup = sqlite3.connect(tampered)
                    try:
                        store.connection.backup(backup)
                    finally:
                        backup.close()
                    changed = sqlite3.connect(tampered)
                    changed.row_factory = sqlite3.Row
                    trigger_sql = [row['sql'] for row in changed.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()]
                    for trigger in changed.execute(
                            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                        changed.execute(f' DROP TRIGGER "{trigger[0]}"')
                    if mutation == 'missing_initial_pin':
                        row = changed.execute('''SELECT state_json FROM state_snapshots
                            WHERE run_id=? AND state_version=0''', (job['run_id'],)).fetchone()
                        initial = State.model_validate_json(row['state_json']).model_copy(
                            update={'skill_invocation_sha256': None})
                        changed.execute('''UPDATE state_snapshots SET state_json=?,content_sha256=?
                            WHERE run_id=? AND state_version=0''', (
                            initial.model_dump_json(), digest(initial.model_dump(mode='json')),
                            job['run_id']))
                    elif mutation == 'expired_approval':
                        row = changed.execute('''SELECT approval_id,envelope_json FROM desktop_approvals
                            WHERE job_id=? ORDER BY created_at LIMIT 1''', (job_id,)).fetchone()
                        envelope = json.loads(row['envelope_json'])
                        envelope['expires_at'] = 1.0
                        changed.execute('''UPDATE desktop_approvals SET expires_at=1.0,envelope_json=?
                            WHERE approval_id=?''', (Approval.model_validate(envelope).model_dump_json(),
                                                      row['approval_id']))
                    elif mutation == 'reversed_approval_order':
                        rows = changed.execute('''SELECT approval_id FROM desktop_approvals
                            WHERE job_id=? ORDER BY created_at''', (job_id,)).fetchall()
                        changed.execute('''UPDATE desktop_approvals SET created_at='1970-01-01T00:00:00Z'
                            WHERE approval_id=?''', (rows[1]['approval_id'],))
                    else:
                        submit_row = changed.execute('''SELECT * FROM actions WHERE run_id=?
                            AND tool='browser.form.submit' ''', (job['run_id'],)).fetchone()
                        columns = [row['name'] for row in changed.execute('PRAGMA table_info(actions)')]
                        duplicate = dict(submit_row)
                        duplicate['action_id'] = 'tamper-' + submit_row['action_id']
                        duplicate['idempotency_key'] = 'tamper-' + submit_row['idempotency_key']
                        changed.execute('INSERT INTO actions (' + ','.join(columns) + ') VALUES ('
                                        + ','.join('?' for _ in columns) + ')',
                                        [duplicate[column] for column in columns])
                    for statement in trigger_sql:
                        changed.execute(statement)
                    changed.commit()
                    changed.close()
                    error_match = ('skill_form_invocation_approval_order_changed'
                                   if mutation == 'expired_approval' else None)
                    assertion = (self.assertRaisesRegex(ValueError, error_match)
                                 if error_match else self.assertRaises(ValueError))
                    with self.subTest(mutation=mutation), assertion:
                        audit_site_skill_form_invocation_execution(
                            skill_store, skill_plan, case_inputs, 'dev-query', self.profiles,
                            task, form_plan, state_plan, field_bindings, invocation,
                            tampered, job['run_id'])
            if real_model:
                submit = store.connection.execute('''SELECT step_id,decision_id FROM actions
                    WHERE run_id=? AND tool='browser.form.submit' ''', (job['run_id'],)).fetchone()
                submit_event_id = next(event['event_id'] for event in
                                       review_learning_events(database, job['run_id'])['events']
                                       if event['role'] == 'system1'
                                       and event['source']['step_id'] == submit['step_id']
                                       and event['source']['decision_id'] == submit['decision_id'])
                verification_ids = sorted(row[0] for row in store.connection.execute(
                    'SELECT verification_id FROM verifications WHERE run_id=?', (job['run_id'],)))
                page_source = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
                page = SitePageDraft.model_validate({**page_source,
                    'profile_sha256': self.checksum, 'origin': origin,
                    'route_template': '/entry', 'outgoing_page_keys': []})
                pages = SiteKnowledgeStore(self.root / 'state-skill-pages', self.profiles)
                page_sha256 = digest(page.model_dump())
                pages.register(page, confirm_sha256=page_sha256)
                skill_source = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
                skill = SiteSkillDraft.model_validate({**skill_source,
                    'profile_sha256': self.checksum, 'page_draft_sha256': page_sha256,
                    'task_key': task.task_key, 'source_event_ids': [submit_event_id],
                    'source_verification_ids': verification_ids})
                skills = SiteSkillStore(self.root / 'state-skills', self.profiles, pages)
                skill_sha256 = digest(skill.model_dump())
                skills.register(skill, confirm_sha256=skill_sha256)
                case_fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
                plan_value = case_fixture['plan']
                cases = [dict(item) for item in plan_value['cases']]
                cases[0]['parameter_variant_sha256'] = parameter_variant_sha256(
                    {'record-query': selected_value})
                validation_plan = SiteSkillValidationPlan.model_validate({**plan_value,
                    'skill_sha256': skill_sha256, 'profile_sha256': self.checksum,
                    'page_draft_sha256': page_sha256, 'task_key': task.task_key,
                    'cases': cases})
                input_value = case_fixture['case_inputs']
                inputs = SiteSkillCaseInputs.model_validate({**input_value,
                    'plan_sha256': digest(validation_plan.model_dump()),
                    'cases': [{**input_value['cases'][0],
                               'parameters': {'record-query': selected_value}},
                              *input_value['cases'][1:]]})
                mappings = [SiteSkillFormFieldBinding(parameter_key='record-query',
                                                      form_field_name='message')]
                bound = audit_site_skill_form_execution(
                    skills, validation_plan, inputs, 'dev-query', self.profiles,
                    task, form_plan, mappings, database, job['run_id'], state_plan)
                self.assertEqual(bound['status'], 'audited_form_state_readback_candidate')
                self.assertTrue(bound['submitted_value_readback_bound'])
                self.assertEqual(bound['state_plan_sha256'], state_plan_sha256)
                self.assertFalse(bound['skill_executed'])
                self.assertFalse(bound['site_outcome_verified'])
                self.assertNotIn(selected_value, canonical(bound))
                sources = self.root / 'state-skill-sources'
                sources.mkdir(mode=0o700)
                source_values = {
                    'plan': validation_plan.model_dump(mode='json'),
                    'cases': inputs.model_dump(mode='json'),
                    'task': task.model_dump(mode='json'),
                    'form_plan': form_plan.model_dump(mode='json'),
                    'state_plan': state_plan.model_dump(mode='json'),
                    'field_bindings': [item.model_dump(mode='json') for item in mappings]}
                for name, source_value in source_values.items():
                    source_file = sources / (name + '.json')
                    source_file.write_bytes(canonical(source_value).encode())
                    source_file.chmod(0o600)
                command = [sys.executable, '-m', 'aos.site_skill_form_execution',
                           '--database', str(database), '--run-id', job['run_id'],
                           '--profiles', str(self.profiles.root), '--pages', str(pages.root),
                           '--store', str(skills.root), '--plan', str(sources / 'plan.json'),
                           '--plan-sha256', digest(source_values['plan']),
                           '--cases', str(sources / 'cases.json'),
                           '--cases-sha256', digest(source_values['cases']),
                           '--skill-sha256', skill_sha256, '--case-key', 'dev-query',
                           '--task-file', str(sources / 'task.json'),
                           '--task-sha256', digest(source_values['task']),
                           '--form-plan-file', str(sources / 'form_plan.json'),
                           '--form-plan-sha256', digest(source_values['form_plan']),
                           '--state-plan-file', str(sources / 'state_plan.json'),
                           '--state-plan-sha256', digest(source_values['state_plan']),
                           '--field-bindings-file', str(sources / 'field_bindings.json'),
                           '--field-binding-sha256', digest(source_values['field_bindings'])]
                command_result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(command_result.returncode, 0, command_result.stderr)
                self.assertEqual(json.loads(command_result.stdout), bound)
                self.assertNotIn(selected_value, command_result.stdout)
                incomplete = list(command)
                del incomplete[incomplete.index('--state-plan-sha256'):
                               incomplete.index('--state-plan-sha256') + 2]
                self.assertEqual(subprocess.run(
                    incomplete, capture_output=True, text=True, timeout=20).returncode, 1)
                wrong_hash = list(command)
                wrong_hash[wrong_hash.index('--state-plan-sha256') + 1] = '0' * 64
                self.assertEqual(subprocess.run(
                    wrong_hash, capture_output=True, text=True, timeout=20).returncode, 1)
                with self.assertRaises(ValueError):
                    audit_site_skill_form_execution(
                        skills, validation_plan, inputs, 'dev-query', self.profiles,
                        task, form_plan, mappings, database, job['run_id'])
                with patch.object(skills, 'get', return_value=skill.model_copy(update={
                        'source_verification_ids': verification_ids[:1]})):
                    with self.assertRaises(ValueError):
                        audit_site_skill_form_execution(
                            skills, validation_plan, inputs, 'dev-query', self.profiles,
                            task, form_plan, mappings, database, job['run_id'], state_plan)
                wrong_state_plan = state_plan.model_copy(update={
                    'expected_after_marker_sha256': '0' * 64})
                with self.assertRaises(ValueError):
                    audit_site_skill_form_execution(
                        skills, validation_plan, inputs, 'dev-query', self.profiles,
                        task, form_plan, mappings, database, job['run_id'], wrong_state_plan)
            tampered = self.root / 'form-state-tampered.sqlite'
            with (contextlib.closing(sqlite3.connect(database)) as original,
                  contextlib.closing(sqlite3.connect(tampered)) as changed):
                original.backup(changed)
                changed.execute("UPDATE verifications SET actual_json='{}' "
                                "WHERE run_id=? AND method='declared_https_form_state_readback'",
                                (job['run_id'],))
                changed.commit()
            with self.assertRaises(ValueError):
                inspect_remote_form_learning_source(
                    tampered, job['run_id'], **selected,
                    selected_state_plan_sha256=state_plan_sha256)
            duplicate_approval = self.root / 'form-state-duplicate-human.sqlite'
            with (contextlib.closing(sqlite3.connect(database)) as original,
                  contextlib.closing(sqlite3.connect(duplicate_approval)) as changed):
                original.backup(changed)
                changed.execute('''INSERT INTO human_interventions
                    SELECT 'intervention-extra',run_id,step_id,actor,kind,payload_json,created_at
                    FROM human_interventions WHERE run_id=? AND kind='approve' LIMIT 1''',
                    (job['run_id'],))
                changed.commit()
            with self.assertRaises(ValueError):
                inspect_remote_form_learning_source(
                    duplicate_approval, job['run_id'], **selected,
                    selected_state_plan_sha256=state_plan_sha256)
            with self.assertRaises(ValueError):
                poll_remote_form_learning_stream(
                    duplicate_approval, profiles=self.profiles.root,
                    consents=consent_root, consent_sha256=consent_sha256,
                    outbox_dir=self.root / 'state-outbox-tampered')
            if real_model:
                timing_tampered = self.root / 'form-state-timing-tampered.sqlite'
                with (contextlib.closing(sqlite3.connect(database)) as original,
                      contextlib.closing(sqlite3.connect(timing_tampered)) as changed):
                    original.backup(changed)
                    changed.execute('''UPDATE actions SET completed_at=?
                        WHERE run_id=? AND tool='browser.form.submit' ''',
                        ('2020-01-01T00:00:00+00:00', job['run_id']))
                    changed.commit()
                with self.assertRaisesRegex(ValueError, 'invalid_duration'):
                    inspect_remote_form_repeats(
                        timing_tampered, profiles=self.profiles.root,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=form_plan_sha256,
                        selected_state_plan_sha256=state_plan_sha256)
                with contextlib.closing(sqlite3.connect(timing_tampered)) as changed, changed:
                    changed.execute('''UPDATE actions SET completed_at=NULL
                        WHERE run_id=? AND tool='browser.form.submit' ''', (job['run_id'],))
                with self.assertRaisesRegex(ValueError, 'stage_timing_binding_invalid'):
                    inspect_remote_form_repeats(
                        timing_tampered, profiles=self.profiles.root,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=form_plan_sha256,
                        selected_state_plan_sha256=state_plan_sha256)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS form MCP transport test')
    def test_owned_visible_mcp_exact_https_form_transport(self):
        body = b'message=hello'
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {
            '/entry': (b'<html><title>Synthetic form</title><h1>Synthetic entry</h1>'
                       b'<form method="post" action="/submit"><label for="message">Message</label>'
                       b'<input id="message" name="message" required>'
                       b'<button type="submit">Save draft</button></form></html>'),
            '/receipt': b'<html><title>Synthetic receipt</title><h1>Saved locally</h1></html>'}
        plan = plan_web_https_form(
            self.profiles, self.draft.task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body))
        entry_request = digest({'method': 'GET', 'url': plan.entry_url})
        submit_request = digest({'method': 'POST', 'url': plan.submit_url,
                                 'body_sha256': plan.body_sha256})
        receipt_request = digest({'method': 'GET', 'url': plan.receipt_url})
        permitted = {entry_request}
        approvals = []

        def consume_approval(request_sha256):
            if request_sha256 not in permitted:
                return False
            approvals.append(request_sha256)
            return True

        transport = ExactHTTPSFormTransport(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            consume_approval=consume_approval, tls_context=self.tls_context)
        relay = ExactHTTPSFormRelay(transport)
        outcome = {}

        def serve():
            try:
                outcome['report'] = relay.serve(self.socket_path, wait_seconds=30)
            except Exception as error:
                outcome['error'] = error

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        gate = self.workspace / 'https_form_gate.cjs'
        gate.write_text(worker_namespace()['https_form_page_gate_source'](
            plan.entry_url, plan.submit_url, plan.receipt_url,
            transport.plan_sha256, relay.client_token))
        gate.chmod(0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const {spawn} = require('node:child_process');
const child = spawn('/usr/local/bin/node', ['/workspace/node_modules/@playwright/mcp/cli.js',
  '--isolated', '--executable-path', '/opt/chromium/chrome', '--no-sandbox',
  '--block-service-workers', '--no-webmcp', '--codegen', 'none', '--snapshot-mode', 'full',
  '--output-dir', '/home/agent/mcp-output', '--timeout-action', '3000',
  '--timeout-navigation', '5000', '--timeout-settle', '0',
  '--init-page', '/workspace/https_form_gate.cjs'],
  {env: {PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/home/agent', DISPLAY: ':99',
         LANG: 'C.UTF-8', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1'}});
let counter = 0;
let buffer = '';
const pending = new Map();
child.stdout.on('data', chunk => {
  buffer += chunk;
  for (;;) {
    const newline = buffer.indexOf('\\n');
    if (newline < 0) break;
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const reply = JSON.parse(line);
    if (pending.has(reply.id)) {pending.get(reply.id)(reply); pending.delete(reply.id);}
  }
});
function request(method, params) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('timeout'));}, 8000);
    pending.set(id, reply => {clearTimeout(timer); resolve(reply);});
    child.stdin.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\\n');
  });
}
async function tool(name, args) {
  const reply = await request('tools/call', {name, arguments: args});
  if (reply.result?.isError) throw new Error(name + ': ' + JSON.stringify(reply.result));
  return reply.result?.content?.map(block => block.text || '').join('\\n') || '';
}
function ref(snapshot, role, label) {
  const expression = new RegExp('- ' + role + ' "' + label + '"[^\\\\n]*?\\\\[ref=([a-zA-Z0-9]+)\\\\]');
  const match = snapshot.match(expression);
  if (!match) throw new Error('missing ' + label);
  return match[1];
}
function advance(expected) {
  return new Promise((resolve, reject) => process.stdin.once('data', chunk => {
    if (chunk.toString() !== expected + '\\n') reject(new Error('host gate')); else resolve();
  }));
}
(async () => {
  try {
    const initialized = await request('initialize', {protocolVersion: '2024-11-05',
      capabilities: {}, clientInfo: {name: 'aos-https-form-test', version: '1'}});
    if (initialized.result?.serverInfo?.version !== process.argv[3]) throw new Error('version');
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\\n');
    await tool('browser_navigate', {url: process.argv[1]});
    const first = await tool('browser_snapshot', {});
    if (!first.includes('Synthetic entry')) throw new Error('entry');
    await tool('browser_fill_form', {fields: [{target: ref(first, 'textbox', 'Message'),
      name: 'Message', type: 'textbox', value: 'hello'}]});
    const filled = await tool('browser_snapshot', {});
    if (!filled.includes('Save draft')) throw new Error('filled');
    process.stdout.write('filled\\n');
    await advance('submit');
    await tool('browser_click', {target: ref(filled, 'button', 'Save draft')});
    process.stdout.write('posted\\n');
    await advance('receipt');
    await tool('browser_navigate', {url: process.argv[2]});
    const last = await tool('browser_snapshot', {});
    if (!last.includes('Saved locally')) throw new Error('receipt');
    const blocked = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: 'https://blocked.aos-relay.invalid/outside'}});
    if (!blocked.result?.isError) throw new Error('off-plan navigation');
    process.stdout.write('verified\\n');
  } finally {child.kill('SIGTERM');}
})().catch(error => {process.stderr.write(error.message); child.kill('SIGTERM'); process.exitCode = 1;});'''

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            relay_thread = threading.Thread(target=serve, daemon=True)
            relay_thread.start()
            for _attempt in range(200):
                if self.socket_path.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(self.socket_path.exists())
            process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', '-e', 'DISPLAY=:99', desktop.container_id,
                 '/usr/local/bin/node', '-e', script, plan.entry_url, plan.receipt_url,
                 PLAYWRIGHT_VERSION], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 25)[0])
                first_line = process.stdout.readline()
                self.assertEqual(first_line, b'filled\n',
                                 process.stderr.read().decode() if not first_line else '')
                self.assertEqual(self.server.paths, ['/entry'])
                self.assertEqual(self.server.posts, [])
                permitted.add(submit_request)
                process.stdin.write(b'submit\n')
                process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 20)[0])
                self.assertEqual(process.stdout.readline(), b'posted\n')
                self.assertEqual(self.server.posts, [('/submit', body)])
                self.assertEqual(self.server.paths, ['/entry'])
                permitted.add(receipt_request)
                process.stdin.write(b'receipt\n')
                process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 20)[0])
                self.assertEqual(process.stdout.readline(), b'verified\n')
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0,
                                 process.stderr.read().decode())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()
                if not process.stdin.closed:
                    process.stdin.close()
            relay_thread.join(5)
        self.assertFalse(relay_thread.is_alive())
        self.assertNotIn('error', outcome)
        self.assertEqual(approvals, [entry_request, submit_request, receipt_request])
        self.assertEqual(self.server.paths, ['/entry', '/receipt'])
        self.assertEqual(self.server.posts, [('/submit', body)])
        self.assertFalse(outcome['report'].browser_connected)
        self.assertFalse(outcome['report'].site_outcome_verified)
        self.assertFalse(self.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu two-route MCP test')
    def test_pinned_mcp_same_visible_browser_two_armed_host_routes(self):
        detail_url = self.profile.entry_url.replace('/entry', '/details')
        self.server.route_bodies = {
            '/details': b'<html><title>Relay details</title><h1>Synthetic details</h1></html>'}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        plan_sha256 = digest(plan.model_dump())
        relay = ExactReadOnlyRouteRelay(self.profiles, self.draft.task, plan,
                                        plan_sha256, tls_context=self.tls_context)
        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        gate = self.workspace / 'readonly_gate.cjs'
        gate.write_text(worker_namespace()['readonly_route_page_gate_source'](
            plan.routes, plan_sha256, relay.client_token))
        gate.chmod(0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const {spawn} = require('node:child_process');
const cli = '/workspace/node_modules/@playwright/mcp/cli.js';
const child = spawn('/usr/local/bin/node', [cli, '--isolated', '--executable-path',
  '/opt/chromium/chrome', '--no-sandbox', '--block-service-workers', '--no-webmcp',
  '--codegen', 'none', '--snapshot-mode', 'full', '--output-dir', '/home/agent/mcp-output',
  '--timeout-action', '3000', '--timeout-navigation', '5000', '--timeout-settle', '0',
  '--init-page', '/workspace/readonly_gate.cjs'],
  {env: {PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/home/agent', DISPLAY: ':99',
         LANG: 'C.UTF-8', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1'}});
let counter = 0;
let buffer = '';
const pending = new Map();
child.stdout.on('data', chunk => {
  buffer += chunk;
  for (;;) {
    const newline = buffer.indexOf('\\n');
    if (newline < 0) break;
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const reply = JSON.parse(line);
    if (pending.has(reply.id)) {pending.get(reply.id)(reply); pending.delete(reply.id);}
  }
});
function request(method, params) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('timeout'));}, 8000);
    pending.set(id, reply => {clearTimeout(timer); resolve(reply);});
    child.stdin.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\\n');
  });
}
async function navigate(url, expected) {
  const opened = await request('tools/call', {name: 'browser_navigate', arguments: {url}});
  if (opened.result?.isError) throw new Error('navigate');
  const snapshot = await request('tools/call', {name: 'browser_snapshot', arguments: {}});
  const content = snapshot.result?.content?.map(block => block.text || '').join('\\n') || '';
  if (snapshot.result?.isError || !content.includes(expected)) throw new Error('snapshot');
}
(async () => {
  try {
    const initialized = await request('initialize', {protocolVersion: '2024-11-05',
      capabilities: {}, clientInfo: {name: 'aos-readonly-routes-test', version: '1'}});
    if (initialized.result?.serverInfo?.version !== process.argv[3]) throw new Error('version');
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\\n');
    await navigate(process.argv[1], 'Synthetic entry');
    process.stdout.write('first_done\\n');
    await new Promise((resolve, reject) => process.stdin.once('data', chunk => {
      if (chunk.toString() !== 'go\\n') reject(new Error('host gate')); else resolve();
    }));
    await navigate(process.argv[2], 'Synthetic details');
    const blocked = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: 'https://blocked.aos-relay.invalid/outside'}});
    if (!blocked.result?.isError) throw new Error('off-plan navigation');
    process.stdout.write(JSON.stringify({second: true}));
  } finally {child.kill('SIGTERM');}
})().catch(error => {process.stderr.write(error.message); child.kill('SIGTERM'); process.exitCode = 1;});'''

        def action():
            relay.arm(0)
            process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', '-e', 'DISPLAY=:99', desktop.container_id,
                 '/usr/local/bin/node', '-e', script, self.profile.entry_url,
                 detail_url, '1.64.0-alpha-1789764292000'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 25)[0])
                self.assertEqual(process.stdout.readline(), b'first_done\n')
                self.assertEqual(relay.wait_for_route(0).route_index, 0)
                self.assertEqual(self.server.paths, ['/entry'])
                relay.arm(1)
                process.stdin.write(b'go\n')
                process.stdin.flush()
                process.stdin.close()
                returncode = process.wait(timeout=25)
                output = process.stdout.read()
                error = process.stderr.read().decode()
                self.assertEqual(returncode, 0, error)
                self.assertEqual(json.loads(output), {'second': True})
                self.assertEqual(relay.wait_for_route(1).route_index, 1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

        outcome = self.run_relay(action, relay=relay)
        self.assertNotIn('error', outcome)
        self.assertEqual(self.server.paths, ['/entry', '/details'])
        self.assertEqual([report.route_index for report in outcome['report']], [0, 1])

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu read-only application worker test')
    def test_owned_readonly_worker_requires_exact_pin_and_fresh_readback(self):
        detail_url = self.profile.entry_url.replace('/entry', '/details?view=compact&page=1')
        self.server.route_bodies = {
            '/details?view=compact&page=1':
                b'<html><title>Relay details</title><h1>Synthetic details</h1></html>'}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        relay = ExactReadOnlyRouteRelay(self.profiles, self.draft.task, plan,
                                        digest(plan.model_dump()), tls_context=self.tls_context)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopReadOnlyRoutesMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        with self.assertRaisesRegex(ValueError, 'live runtime pin'):
            runtime.attach_relay(relay, self.draft)
        draft = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        runtime.attach_relay(relay, draft)
        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            runtime.start()
            status = runtime.status()
            self.assertTrue(status['running'])
            self.assertEqual(status['plan_sha256'], relay.plan_sha256)
            self.assertNotIn(relay.client_token, json.dumps(status))
            self.assertNotIn(detail_url, json.dumps(status))
            self.assertNotIn(detail_url, json.dumps(runtime.process.args))
            self.assertEqual(status['isolation']['routes_sha256'], digest(plan.routes))
            with self.assertRaises(AOSFault):
                runtime.perform('browser_navigate', {'url': self.profile.entry_url})
            with self.assertRaises(AOSFault):
                runtime.perform('browser.remote.route', {'route_index': 0})
            self.assertEqual(self.server.paths, [])
            first_args = {'profile_sha256': self.checksum,
                          'binding_sha256': draft.binding_sha256,
                          'plan_sha256': relay.plan_sha256,
                          'route_index': 0, 'url': self.profile.entry_url}
            first = runtime.perform('browser.remote.route', first_args)
            self.assertEqual(first['url'], self.profile.entry_url)
            self.assertEqual(self.server.paths, ['/entry'])
            second_args = {**first_args, 'route_index': 1, 'url': detail_url}
            with self.assertRaises(AOSFault):
                runtime.perform('browser.remote.route', second_args)
            self.assertEqual(self.server.paths, ['/entry'])
            self.assertEqual(runtime.perform('browser.remote.observe', {'route_index': 0}), first)
            second = runtime.perform('browser.remote.route', second_args)
            self.assertEqual(second['url'], detail_url)
            self.assertNotEqual(second['heading_sha256'], first['heading_sha256'])
            self.assertEqual(runtime.perform('browser.remote.observe', {'route_index': 1}), second)
            self.assertEqual(self.server.paths, ['/entry', '/details?view=compact&page=1'])
            with self.assertRaises(AOSFault):
                runtime.perform('browser.remote.route', second_args)
            runtime.stop()
            self.assertFalse(self.workspace.joinpath('relay.sock').exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote MCP runtime test')
    def test_owned_remote_mcp_runtime_one_shot_entry(self):
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = self.remote_runtime(desktop)
        self.addCleanup(runtime.stop)
        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch.object(runtime, 'worker_source', return_value='changed worker source'):
            with self.assertRaises(AOSFault):
                runtime.start()
        self.assertFalse(runtime.socket_path.exists())
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            runtime.start()
            self.assertNotIn(runtime.relay.client_token, canonical(runtime.process.args))
            with self.assertRaises(AOSFault):
                runtime.perform('browser.remote.observe', {})
            with self.assertRaises(AOSFault):
                runtime.perform('browser_navigate', {'url': self.profile.entry_url})
            result = runtime.perform('browser.remote.open', {
                'profile_sha256': runtime.relay.profile_sha256,
                'binding_sha256': runtime.relay.draft.binding_sha256,
                'entry_url': runtime.relay.entry_url})
            self.assertEqual(result, {'url': self.profile.entry_url,
                                      'title_sha256': hashlib.sha256(b'Relay fixture').hexdigest(),
                                      'heading_sha256': hashlib.sha256(b'Synthetic entry').hexdigest()})
            self.assertEqual(runtime.perform('browser.remote.observe', {}), result)
            self.assertNotIn('Relay fixture', canonical(runtime.status()))
            self.assertNotIn('Synthetic entry', canonical(runtime.status()))
            with self.assertRaises(AOSFault):
                runtime.perform('browser.remote.open', {
                    'profile_sha256': runtime.relay.profile_sha256,
                    'binding_sha256': runtime.relay.draft.binding_sha256,
                    'entry_url': runtime.relay.entry_url})
            self.assertEqual(runtime.relay_report.request_attempts, 1)
            self.assertFalse(runtime.relay_report.execution_authorized)
            self.assertNotIn(runtime.relay.client_token, canonical(runtime.status()))
            self.assertEqual(self.server.paths, ['/entry'])
        runtime.stop()
        self.assertFalse(runtime.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu static bundle MCP runtime test')
    def test_owned_static_bundle_mcp_runtime_exact_assets(self):
        origin = self.profile.allowed_origins[0]
        self.server.route_bodies = {
            '/entry': (b'<html><head><title>Before</title>'
                       b'<link rel="stylesheet" href="/assets/site.css?v=2">'
                       b'<script defer src="/assets/app.js?v=1"></script></head>'
                       b'<body><h1>Before</h1></body></html>')}
        self.server.asset_responses = {
            '/assets/app.js?v=1': ('application/javascript',
                               b'document.title="Ready";document.querySelector("h1").textContent="Ready";'
                               b'fetch("/blocked").catch(()=>{});'),
            '/assets/site.css?v=2': ('text/css', b'body{color:blue}')}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js?v=1', 'content_type': 'application/javascript'},
            {'url': origin + '/assets/site.css?v=2', 'content_type': 'text/css'}])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopStaticBundleMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       digest(plan.model_dump()), tls_context=self.tls_context)
        with self.assertRaises(ValueError):
            runtime.attach_relay(relay, self.draft)
        bound = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        runtime.attach_relay(relay, bound)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with patch.object(runtime, 'worker_source', return_value='changed worker source'):
                with self.assertRaises(AOSFault):
                    runtime.start()
            self.assertFalse(runtime.socket_path.exists())
            runtime.start()
            self.assertNotIn(relay.client_token, canonical(runtime.process.args))
            with self.assertRaises(AOSFault):
                runtime.perform('browser.static.observe', {})
            with self.assertRaises(AOSFault):
                runtime.perform('browser_navigate', {'url': self.profile.entry_url})
            arguments = {'profile_sha256': plan.profile_sha256,
                         'binding_sha256': bound.binding_sha256,
                         'plan_sha256': relay.plan_sha256,
                         'entry_url': relay.entry_url}
            with self.assertRaises(AOSFault):
                runtime.perform('browser.static.open', {**arguments, 'plan_sha256': '0' * 64})
            self.assertEqual(self.server.paths, [])
            result = runtime.perform('browser.static.open', arguments)
            self.assertEqual(result, {
                'url': self.profile.entry_url,
                'title_sha256': hashlib.sha256(b'Ready').hexdigest(),
                'heading_sha256': hashlib.sha256(b'Ready').hexdigest()})
            self.assertEqual(runtime.perform('browser.static.observe', {}), result)
            with self.assertRaises(AOSFault):
                runtime.perform('browser.static.open', arguments)
            self.assertEqual(self.server.paths,
                             ['/entry', '/assets/site.css?v=2', '/assets/app.js?v=1'])
            self.assertEqual(runtime.relay_report.request_attempts, 3)
            self.assertEqual(len(runtime.relay_report.asset_response_sha256), 2)
            self.assertFalse(runtime.relay_report.execution_authorized)
            self.assertNotIn(relay.client_token, canonical(runtime.status()))
            self.assertNotIn('Ready', canonical(runtime.status()))
        runtime.stop()
        self.assertFalse(runtime.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu read-only JSON bundle MCP runtime test')
    def test_owned_readonly_json_bundle_mcp_updates_dom_without_off_plan_get(self):
        origin = self.profile.allowed_origins[0]
        data_url = origin + '/api/summary?view=compact&page=1'
        self.server.route_bodies = {
            '/entry': (b'<html><head><title>Before</title>'
                       b'<script defer src="/assets/app.js?v=1"></script></head>'
                       b'<body><h1>Before</h1></body></html>')}
        self.server.asset_responses = {
            '/assets/app.js?v=1': ('application/javascript',
                               b'fetch("/api/summary?view=compact&page=1",{method:"POST"})'
                               b'.catch(()=>fetch("/api/summary?view=compact&page=2"))'
                               b'.catch(()=>fetch("/api/summary?view=compact&page=1"))'
                               b'.then(response=>response.json())'
                               b'.then(data=>{document.title=data.title;'
                               b'document.querySelector("h1").textContent=data.title;});'
                               b'fetch("/api/off-plan").catch(()=>{});'),
            '/api/summary?view=compact&page=1': ('application/json', b'{"title":"JSON ready"}')}
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': origin + '/assets/app.js?v=1', 'content_type': 'application/javascript'}],
            [{'url': data_url}])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopStaticBundleMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        relay = ExactStaticBundleRelay(self.profiles, self.draft.task, plan,
                                       digest(plan.model_dump()), tls_context=self.tls_context)
        bound = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        runtime.attach_relay(relay, bound)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            runtime.start()
            self.assertEqual(runtime.evidence['data_count'], 1)
            self.assertEqual(self.server.paths, [])
            arguments = {'profile_sha256': plan.profile_sha256,
                         'binding_sha256': bound.binding_sha256,
                         'plan_sha256': relay.plan_sha256,
                         'entry_url': relay.entry_url}
            with self.assertRaises(AOSFault):
                runtime.perform('browser.static.open', {**arguments, 'plan_sha256': '0' * 64})
            self.assertEqual(self.server.paths, [])
            runtime.perform('browser.static.open', arguments)
            observed = runtime.perform('browser.static.observe', {})
            self.assertEqual(observed['url'], self.profile.entry_url)
            self.assertEqual(observed['title_sha256'], hashlib.sha256(b'JSON ready').hexdigest())
            self.assertEqual(observed['heading_sha256'], hashlib.sha256(b'JSON ready').hexdigest())
            self.assertEqual(self.server.paths, ['/entry', '/assets/app.js?v=1',
                                                 '/api/summary?view=compact&page=1'])
            self.assertIsInstance(runtime.relay_report, WebReadOnlyDataBundleRelayReport)
            self.assertEqual(runtime.relay_report.request_attempts, 3)
            self.assertEqual(runtime.status()['kind'],
                             'docker_chromium_readonly_data_bundle_mcp')
            self.assertFalse(runtime.relay_report.collection_authorized)
            self.assertNotIn('JSON ready', canonical(runtime.status()))
        runtime.stop()
        self.assertFalse(runtime.socket_path.exists())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu static bundle cancellation test')
    def test_owned_static_bundle_mcp_stop_before_open_never_fetches(self):
        origin = self.profile.allowed_origins[0]
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = DesktopStaticBundleMCPRuntime(
            desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        self.addCleanup(runtime.stop)
        bound = bind_web_task(self.profiles, self.draft.task, runtime.runtime_pin())
        runtime.attach_relay(ExactStaticBundleRelay(
            self.profiles, self.draft.task, plan, digest(plan.model_dump()),
            tls_context=self.tls_context), bound)
        runtime.start()
        self.assertTrue(runtime.socket_path.exists())
        runtime.stop()
        self.assertFalse(runtime.socket_path.exists())
        self.assertFalse(runtime.relay_thread.is_alive())
        self.assertEqual(self.server.paths, [])
        self.assertIsNone(runtime.relay_report)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu approved static bundle task test')
    def test_scheduler_static_bundle_requires_approval_and_readback(self):
        self._scheduler_static_bundle(fail_remote_learning=False, query_assets=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu approved static image bundle task test')
    def test_scheduler_static_image_bundle_requires_approval_and_readback(self):
        self._scheduler_static_bundle(fail_remote_learning=False, include_image=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu read-only JSON managed task test')
    def test_scheduler_readonly_json_bundle_requires_approval_and_binds_run(self):
        origin = self.profile.allowed_origins[0]
        data_url = origin + '/api/summary?view=compact&page=1'
        self.server.route_bodies = {
            '/entry': (b'<html><head><title>Before</title>'
                       b'<script defer src="/assets/app.js"></script></head>'
                       b'<body><h1>Before</h1></body></html>')}
        self.server.asset_responses = {
            '/assets/app.js': ('application/javascript',
                               b'fetch("/api/summary?view=compact&page=1")'
                               b'.then(response=>response.json())'
                               b'.then(data=>{document.title=data.title;'
                               b'document.querySelector("h1").textContent=data.title;});'
                               b'fetch("/api/off-plan").catch(()=>{});'),
            '/api/summary?view=compact&page=1': ('application/json', b'{"title":"JSON managed ready"}')}
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}],
            [{'url': data_url}])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'readonly-data-trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        real_model = os.environ.get('AOS_READONLY_DATA_REAL_DECIDER_TESTS') == '1'
        engine = (ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
            if real_model else FixtureDecisionEngine())
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_static_assets_plan=plan,
            remote_static_tls_context=self.tls_context,
            remote_learning_consents_dir=self.root / 'readonly-data-consents',
            remote_learning_stream_dir=self.root / 'readonly-data-outbox')
        scope = scheduler.status()['remote_static_assets']
        self.assertEqual(scope['mode'], 'one_shot_readonly_data_bundle')
        self.assertEqual(scope['data_resources'], [data_url])
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'],
                            'browser_remote_static_assets', approve_all=True)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port),
                                            timeout=timeout)

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_static_assets')['job_id']
            for _attempt in range(500):
                approval = scheduler.status()['approval']
                if approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval, scheduler.status()['jobs'])
            self.assertEqual(self.server.paths, [])
            self.assertEqual(approval['action']['tool'], 'browser.static.open')
            self.assertEqual(approval['action']['arguments']['plan_sha256'],
                             digest(plan.model_dump()))
            self.assertIn('JSON GETs', approval['action']['expected_effect'])
            pending = store.connection.execute(
                'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
            with self.assertRaises(ValueError):
                preview_remote_learning_consent(
                    database, pending['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()),
                    roles=['system1'], expires_at=expires_at,
                    attest_data_rights=True,
                    scope='remote_static_model_metadata_only')
            consent_preview = scheduler.prepare_remote_learning_consent(
                job_id, roles=['system1'], expires_at=expires_at,
                attest_data_rights=True)
            self.assertFalse(consent_preview['registered'])
            self.assertFalse((self.root / 'readonly-data-consents').exists())
            consent_sha256 = consent_preview['consent_sha256']
            scheduler.prepare_remote_learning_consent(
                job_id, roles=['system1'], expires_at=expires_at,
                attest_data_rights=True, confirm_sha256=consent_sha256)
            attached = scheduler.attach_remote_learning(job_id, consent_sha256)
            self.assertEqual(attached['entries_by_role'], {'system1': 0, 'system2': 0})
            with self.assertRaises(AOSFault):
                scheduler.respond(approval['approval_id'], '0' * 64, True)
            self.assertEqual(self.server.paths, [])
            scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            await scheduler.task
            job = store.connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['run_id'], pending['run_id'])
            self.assertEqual(job['status'], 'succeeded')
            model_calls = store.connection.execute(
                'SELECT role,status FROM model_calls WHERE run_id=?',
                (job['run_id'],)).fetchall()
            self.assertEqual(len(model_calls), int(real_model))
            if real_model:
                self.assertEqual((model_calls[0]['role'], model_calls[0]['status']),
                                 ('system1', 'ok'))
            self.assertEqual(self.server.paths, ['/entry', '/assets/app.js',
                                                 '/api/summary?view=compact&page=1'])
            metadata = scheduler.remote_learning_status(job_id)
            self.assertEqual(metadata['entries_by_role'],
                             {'system1': int(real_model), 'system2': 0})
            stream_selection = {'profiles': self.profiles.root,
                                'consents': self.root / 'readonly-data-consents',
                                'consent_sha256': consent_sha256,
                                'outbox_dir': self.root / 'readonly-data-outbox'}
            self.assertEqual(poll_remote_readonly_data_stream(
                database, **stream_selection)['new_entries'], 0)
            with self.assertRaises(ValueError):
                poll_remote_static_learning_stream(
                    database, **{**stream_selection,
                                 'outbox_dir': self.root / 'readonly-wrong-scope'})
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_stream',
                '--database', str(database), '--profiles', str(self.profiles.root),
                '--consents', str(self.root / 'readonly-data-consents'),
                '--consent-sha256', consent_sha256,
                '--outbox-dir', str(self.root / 'readonly-data-outbox')],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertEqual(json.loads(command.stdout)['entries_by_role'],
                             {'system1': int(real_model), 'system2': 0})
            self.assertNotIn(origin, command.stdout)
            self.assertNotIn('JSON managed ready', command.stdout)
            outbox_bytes = (self.root / 'readonly-data-outbox' / consent_sha256
                            / 'remote-learning-stream.sqlite').read_bytes()
            self.assertNotIn(origin.encode(), outbox_bytes)
            self.assertNotIn(b'JSON managed ready', outbox_bytes)
            for table, field, changed in (
                    ('desktop_approvals', 'status', 'rejected'),
                    ('human_interventions', 'actor', 'untrusted_actor')):
                with self.subTest(stream_source=table):
                    tampered = self.root / ('readonly-stream-' + table + '.sqlite')
                    with contextlib.closing(sqlite3.connect(tampered)) as destination:
                        store.connection.backup(destination)
                        selector = 'job_id' if table == 'desktop_approvals' else 'run_id'
                        identity = job_id if table == 'desktop_approvals' else job['run_id']
                        destination.execute(f'UPDATE {table} SET {field}=? WHERE {selector}=?',
                                            (changed, identity))
                        destination.commit()
                    tampered.chmod(0o600)
                    with self.assertRaises(ValueError):
                        poll_remote_readonly_data_stream(
                            tampered, **{**stream_selection,
                                         'outbox_dir': self.root / ('readonly-stream-rejected-' + table)})
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_static_asset_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            self.assertEqual(binding['plan_sha256'], digest(plan.model_dump()))
            self.assertEqual(binding['plan_json'], canonical(plan.model_dump()))
            self.assertEqual(binding['browser_runtime_id'], job['runtime_id'])
            verification = store.connection.execute(
                'SELECT * FROM verifications WHERE run_id=?', (job['run_id'],)).fetchone()
            self.assertEqual(verification['result'], 'passed')
            self.assertEqual(verification['verifier'], 'aos-remote-readonly-data-v1')
            self.assertEqual(verification['method'],
                             'independent_readonly_data_bundle_readback')
            response = json.loads(verification['expected_json'])['responses']
            self.assertEqual(len(response['asset_response_sha256']), 1)
            self.assertEqual(len(response['data_response_sha256']), 1)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 1)
            self.assertNotIn('JSON managed ready', '\n'.join(store.connection.iterdump()))
            with self.assertRaises(ValueError):
                inspect_remote_static_learning_source(
                    database, job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
            source = inspect_remote_readonly_data_source(
                database, job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual((source['asset_count'], source['data_count']), (1, 1))
            self.assertEqual(len(source['source_event_ids_by_role']['system1']), int(real_model))
            self.assertEqual(source['source_event_ids_by_role']['system2'], [])
            self.assertTrue(source['transport_readback_verified'])
            self.assertFalse(source['collection_authorized'])
            self.assertFalse(source['training_ready'])
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_source',
                '--database', str(database), '--run-id', job['run_id'],
                '--profiles', str(self.profiles.root),
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump())],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertEqual(json.loads(command.stdout), source)
            self.assertNotIn(origin, command.stdout)
            self.assertNotIn('JSON managed ready', command.stdout)
            with self.assertRaises(ValueError):
                inspect_remote_readonly_data_source(
                    database, job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256='0' * 64)
            self.server.asset_responses['/api/summary?view=compact&page=1'] = (
                'application/json', b'{"title":"JSON managed ready","version":2}')
            next_owner = controller.state()
            next_job_id = scheduler.start(
                next_owner['lease_id'], next_owner['generation'],
                'browser_remote_static_assets')['job_id']
            for _attempt in range(500):
                next_approval = scheduler.status()['approval']
                if next_approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(next_approval)
            scheduler.respond(next_approval['approval_id'],
                              next_approval['action_sha256'], True)
            await scheduler.task
            next_job = store.connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?', (next_job_id,)).fetchone()
            self.assertEqual(next_job['status'], 'succeeded')
            next_model_calls = store.connection.execute(
                'SELECT role,status FROM model_calls WHERE run_id=?',
                (next_job['run_id'],)).fetchall()
            self.assertEqual(len(next_model_calls), int(real_model))
            if real_model:
                self.assertEqual((next_model_calls[0]['role'], next_model_calls[0]['status']),
                                 ('system1', 'ok'))
            selection = {'profiles': self.profiles.root,
                         'selected_profile_sha256': self.checksum,
                         'selected_plan_sha256': digest(plan.model_dump())}
            change = compare_remote_readonly_data_change(
                database, job['run_id'], next_job['run_id'], **selection)
            self.assertEqual(change['status'], 'changed_profile_bound')
            self.assertEqual(change['data_changed_indices'], [0])
            self.assertEqual(change['asset_changed_indices'], [])
            self.assertFalse(change['entry_response_changed'])
            self.assertFalse(change['page_identity_changed'])
            self.assertFalse(change['task_retrieval_authorized'])
            self.assertNotEqual(change['before_fingerprint_sha256'],
                                change['after_fingerprint_sha256'])
            with self.assertRaises(ValueError):
                compare_remote_readonly_data_change(
                    database, next_job['run_id'], job['run_id'], **selection)
            with self.assertRaises(ValueError):
                compare_remote_readonly_data_change(
                    database, job['run_id'], next_job['run_id'],
                    **{**selection, 'selected_plan_sha256': '0' * 64})
            change_command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_change',
                '--database', str(database), '--before-run-id', job['run_id'],
                '--after-run-id', next_job['run_id'],
                '--profiles', str(self.profiles.root),
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump())],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(change_command.returncode, 0, change_command.stderr)
            self.assertEqual(json.loads(change_command.stdout), change)
            self.assertNotIn(origin, change_command.stdout)
            self.assertNotIn('JSON managed ready', change_command.stdout)
            stable_owner = controller.state()
            stable_job_id = scheduler.start(
                stable_owner['lease_id'], stable_owner['generation'],
                'browser_remote_static_assets')['job_id']
            for _attempt in range(500):
                stable_approval = scheduler.status()['approval']
                if stable_approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(stable_approval)
            scheduler.respond(stable_approval['approval_id'],
                              stable_approval['action_sha256'], True)
            await scheduler.task
            stable_job = store.connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?', (stable_job_id,)).fetchone()
            self.assertEqual(stable_job['status'], 'succeeded')
            stable_model_calls = store.connection.execute(
                'SELECT role,status FROM model_calls WHERE run_id=?',
                (stable_job['run_id'],)).fetchall()
            self.assertEqual(len(stable_model_calls), int(real_model))
            if real_model:
                self.assertEqual((stable_model_calls[0]['role'], stable_model_calls[0]['status']),
                                 ('system1', 'ok'))
            stable_change = compare_remote_readonly_data_change(
                database, next_job['run_id'], stable_job['run_id'], **selection)
            self.assertEqual(stable_change['status'], 'unchanged_profile_bound')
            self.assertEqual(stable_change['data_changed_indices'], [])
            with self.assertRaises(ValueError):
                seed_remote_readonly_data_page_draft(
                    database, job['run_id'], next_job['run_id'],
                    profiles=self.profiles.root, store=self.root / 'readonly-pages',
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()), page_key='unstable')
            page_store = SiteKnowledgeStore(self.root / 'readonly-pages', self.profiles)
            page = SitePageDraft(
                schema_version='1.0', profile_sha256=self.checksum,
                application_key=self.profile.application_key,
                tenant_key=self.profile.tenant_key,
                account_role=self.profile.account_role,
                page_key='summary', revision=1, previous_sha256=None,
                origin=origin, route_template='/entry',
                page_fingerprint_sha256=stable_change['after_fingerprint_sha256'],
                recorded_at='2026-09-24T00:00:00Z',
                landmark_keys=['heading'], outgoing_page_keys=[],
                source_kind='manual_draft', status='draft',
                execution_authorized=False, collection_authorized=False,
                training_ready=False)
            page_sha256 = digest(page.model_dump())
            page_store.register(page, confirm_sha256=page_sha256)
            knowledge_selection = {
                **selection, 'store': page_store.root,
                'knowledge_sha256': page_sha256}
            candidate = preview_remote_readonly_data_knowledge(
                database, next_job['run_id'], stable_job['run_id'],
                **knowledge_selection)
            self.assertEqual(candidate['page_key'], 'summary')
            self.assertEqual(candidate['page_fingerprint_sha256'],
                             stable_change['after_fingerprint_sha256'])
            self.assertFalse(candidate['task_retrieval_authorized'])
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    database, job['run_id'], next_job['run_id'],
                    **knowledge_selection)
            with self.assertRaises((ValueError, FileNotFoundError)):
                preview_remote_readonly_data_knowledge(
                    database, next_job['run_id'], stable_job['run_id'],
                    **{**knowledge_selection, 'knowledge_sha256': '0' * 64})
            outgoing = page.model_copy(update={
                'page_key': 'outgoing-summary', 'outgoing_page_keys': ['other']})
            outgoing_sha256 = digest(outgoing.model_dump())
            page_store.register(outgoing, confirm_sha256=outgoing_sha256)
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    database, next_job['run_id'], stable_job['run_id'],
                    **{**knowledge_selection,
                       'knowledge_sha256': outgoing_sha256})
            candidate_command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_knowledge',
                '--database', str(database),
                '--before-run-id', next_job['run_id'],
                '--after-run-id', stable_job['run_id'],
                '--profiles', str(self.profiles.root),
                '--store', str(page_store.root),
                '--knowledge-sha256', page_sha256,
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump())],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(candidate_command.returncode, 0, candidate_command.stderr)
            self.assertEqual(json.loads(candidate_command.stdout), candidate)
            self.assertNotIn(origin, candidate_command.stdout)
            self.assertNotIn('JSON managed ready', candidate_command.stdout)
            console_origin = 'http://127.0.0.1:8765'
            json_api_reviews = self.root / 'readonly-api-reviews'
            json_app = create_console(
                controller, 'synthetic-token', console_origin, self.root,
                database, scheduler, web_profiles_root=self.profiles.root,
                site_knowledge_root=page_store.root,
                json_page_seed_root=self.root,
                json_review_root=json_api_reviews)
            json_api_selection = {
                'before_run_id': next_job['run_id'],
                'after_run_id': stable_job['run_id'],
                'knowledge_sha256': page_sha256}
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=json_app),
                    base_url=console_origin,
                    headers={'Origin': console_origin}) as client:
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-seed', json={
                        'before_run_id': next_job['run_id'],
                        'after_run_id': stable_job['run_id'],
                        'page_key': 'json-summary'})).status_code, 401)
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-preview',
                    json=json_api_selection)).status_code, 401)
                self.assertEqual((await client.post(
                    '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                seed_selection = {
                    'before_run_id': next_job['run_id'],
                    'after_run_id': stable_job['run_id'],
                    'page_key': 'json-summary'}
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-seed',
                    json={**seed_selection, 'after_run_id': 'missing'})).status_code, 404)
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-seed', json={
                        **seed_selection, 'before_run_id': job['run_id'],
                        'after_run_id': next_job['run_id']})).status_code, 409)
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-seed?ignore=1',
                    json=seed_selection)).status_code, 400)
                seed_response = await client.post(
                    '/api/tasks/json-page-draft-seed', json=seed_selection)
                self.assertEqual(seed_response.status_code, 200, seed_response.text)
                seed_report = seed_response.json()
                self.assertEqual(seed_report['page_fingerprint_sha256'],
                                 stable_change['after_fingerprint_sha256'])
                self.assertFalse(seed_report['task_retrieval_authorized'])
                self.assertNotIn(origin, seed_response.text)
                seed_path = self.root / self.checksum / 'json-summary.json'
                self.assertEqual(seed_path.stat().st_mode & 0o777, 0o600)
                original_seed = seed_path.read_bytes()
                claimed_links = json.loads(original_seed)
                claimed_links['outgoing_page_keys'] = ['other']
                seed_path.write_bytes(canonical(claimed_links).encode())
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-register', json={
                        **seed_selection,
                        'confirm_sha256': digest(claimed_links)})).status_code, 409)
                seed_path.write_bytes(original_seed)
                self.assertEqual((await client.post(
                    '/api/tasks/json-page-draft-register', json={
                        **seed_selection, 'confirm_sha256': '0' * 64})).status_code, 409)
                self.assertFalse(page_store.list(profile_sha256=self.checksum,
                                                  page_key='json-summary'))
                registration_response = await client.post(
                    '/api/tasks/json-page-draft-register', json={
                        **seed_selection, 'confirm_sha256': seed_report['draft_sha256']})
                self.assertEqual(registration_response.status_code, 200,
                                 registration_response.text)
                registration = registration_response.json()
                self.assertEqual(registration['knowledge_sha256'],
                                 seed_report['draft_sha256'])
                self.assertFalse(registration['reviewed'])
                self.assertEqual(page_store.get(registration['knowledge_sha256']).page_key,
                                 'json-summary')
                edited_selection = {**seed_selection, 'page_key': 'json-landmarks'}
                edited_seed = await client.post(
                    '/api/tasks/json-page-draft-seed', json=edited_selection)
                self.assertEqual(edited_seed.status_code, 200, edited_seed.text)
                edited_path = self.root / self.checksum / 'json-landmarks.json'
                edited_page = json.loads(edited_path.read_text())
                edited_page['landmark_keys'] = ['heading']
                edited_path.write_bytes(canonical(edited_page).encode())
                edited_checksum = digest(edited_page)
                edited_registration = await client.post(
                    '/api/tasks/json-page-draft-register', json={
                        **edited_selection, 'confirm_sha256': edited_checksum})
                self.assertEqual(edited_registration.status_code, 200,
                                 edited_registration.text)
                self.assertEqual(page_store.get(edited_checksum).landmark_keys,
                                 ['heading'])
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-preview',
                    json={**json_api_selection, 'after_run_id': 'missing'})).status_code, 404)
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-preview',
                    json={**json_api_selection, 'knowledge_sha256': '0' * 64})).status_code, 409)
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-preview?ignore=1',
                    json=json_api_selection)).status_code, 400)
                api_candidate_response = await client.post(
                    '/api/tasks/json-knowledge-preview', json=json_api_selection)
                self.assertEqual(api_candidate_response.status_code, 200,
                                 api_candidate_response.text)
                self.assertEqual(api_candidate_response.json()['candidate'], candidate)
                self.assertEqual(api_candidate_response.json()['candidate_sha256'],
                                 digest(candidate))
                self.assertNotIn(origin, api_candidate_response.text)
                api_review_selection = {
                    **json_api_selection, 'confirm_candidate_sha256': digest(candidate),
                    'acknowledge_metadata_only': True}
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-review',
                    json={**api_review_selection,
                          'acknowledge_metadata_only': False})).status_code, 404)
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-review',
                    json={**api_review_selection,
                          'confirm_candidate_sha256': '0' * 64})).status_code, 409)
                self.assertFalse(json_api_reviews.exists())
                api_review_response = await client.post(
                    '/api/tasks/json-knowledge-review', json=api_review_selection)
                self.assertEqual(api_review_response.status_code, 200,
                                 api_review_response.text)
                self.assertFalse(api_review_response.json()['task_retrieval_authorized'])
                api_review_sha256 = api_review_response.json()['review_sha256']
                self.assertEqual(RemoteReadonlyDataKnowledgeReviewStore(
                    json_api_reviews).get(api_review_sha256).knowledge_sha256,
                    page_sha256)
            review_selection = {
                'profiles': self.profiles.root, 'site_store': page_store.root,
                'review_store': self.root / 'readonly-reviews',
                'knowledge_sha256': page_sha256,
                'selected_profile_sha256': self.checksum,
                'selected_plan_sha256': digest(plan.model_dump()),
                'confirm_candidate_sha256': digest(candidate)}
            with self.assertRaises(ValueError):
                register_remote_readonly_data_knowledge_review(
                    database, next_job['run_id'], stable_job['run_id'],
                    **review_selection, acknowledge_metadata_only=False)
            with self.assertRaises(ValueError):
                register_remote_readonly_data_knowledge_review(
                    database, next_job['run_id'], stable_job['run_id'],
                    **{**review_selection, 'confirm_candidate_sha256': '0' * 64},
                    acknowledge_metadata_only=True)
            self.assertFalse(review_selection['review_store'].exists())
            receipt = register_remote_readonly_data_knowledge_review(
                database, next_job['run_id'], stable_job['run_id'],
                **review_selection, acknowledge_metadata_only=True)
            review_record = RemoteReadonlyDataKnowledgeReviewStore(
                review_selection['review_store']).get(receipt['review_sha256'])
            self.assertEqual(review_record.candidate_sha256, digest(candidate))
            self.assertFalse(receipt['task_retrieval_authorized'])
            historical_pin = prepare_live_remote_readonly_data_knowledge(
                database, profiles=self.profiles.root,
                site_store=page_store.root,
                review_store=review_selection['review_store'],
                review_sha256=receipt['review_sha256'],
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual(historical_pin.page_key, 'summary')
            self.assertFalse(historical_pin.current_readback_bound)
            review_command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_knowledge_review',
                'register', '--database', str(database),
                '--before-run-id', next_job['run_id'],
                '--after-run-id', stable_job['run_id'],
                '--profiles', str(self.profiles.root),
                '--site-store', str(page_store.root),
                '--review-store', str(review_selection['review_store']),
                '--knowledge-sha256', page_sha256,
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump()),
                '--confirm-candidate-sha256', digest(candidate),
                '--acknowledge-metadata-only'],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(review_command.returncode, 0, review_command.stderr)
            self.assertEqual(json.loads(review_command.stdout)['candidate_sha256'],
                             digest(candidate))
            self.assertNotIn(origin, review_command.stdout)

            live_pin = prepare_live_remote_readonly_data_knowledge(
                database, profiles=self.profiles.root, site_store=page_store.root,
                review_store=review_selection['review_store'],
                review_sha256=receipt['review_sha256'],
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            task_file = self.root / 'json-live-task.json'
            task_file.write_text(canonical(self.draft.task.model_dump(mode='json')))
            task_file.chmod(0o600)
            plan_file = self.root / 'json-live-plan.json'
            plan_file.write_text(canonical(plan.model_dump(mode='json')))
            plan_file.chmod(0o600)
            backend_arguments = (
                '--browser-tasks', '--desktop-browser',
                '--remote-entry-mcp-manifest',
                str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
                '--web-profiles-root', str(self.profiles.root),
                '--remote-entry-profile-sha256', self.checksum,
                '--remote-entry-task-file', str(task_file),
                '--remote-entry-task-sha256', digest(self.draft.task.model_dump()),
                '--remote-static-assets-plan-file', str(plan_file),
                '--remote-static-assets-plan-sha256', digest(plan.model_dump()),
                '--remote-json-review-source-database', str(database),
                '--remote-json-review-site-store', str(page_store.root),
                '--remote-json-review-store', str(review_selection['review_store']),
                '--remote-json-review-sha256', receipt['review_sha256'])
            rejected = subprocess.run([
                sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                '--port', '18766', '--engine', 'fixture', *backend_arguments,
                '--remote-json-review-source-snapshot-sha256', '0' * 64],
                capture_output=True, timeout=20)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn(b'JSON review source is unavailable or stale', rejected.stderr)
            from test_desktop_task_ui import task_server

            reviewed_console = self.root / 'json-live-console'
            reviewed_console.mkdir(mode=0o700)
            with patch('socket.create_connection', self.original_connection), task_server(
                    reviewed_console, 'fixture', (
                        *backend_arguments,
                        '--remote-json-review-source-snapshot-sha256',
                        live_pin.source_snapshot_sha256)) as (_origin, _token, client, _server):
                advertised = client.get('/api/tasks').json()['remote_static_assets']
                self.assertEqual(advertised['plan_sha256'], digest(plan.model_dump()))
                self.assertEqual(advertised['review_sha256'], receipt['review_sha256'])

            review_scheduler = DesktopScheduler(
                controller, Settings(workspace=self.workspace, database=database),
                engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                desktop_browser=True,
                remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                remote_entry_profiles=self.profiles,
                remote_entry_profile_sha256=self.checksum,
                remote_entry_task=self.draft.task,
                remote_static_assets_plan=plan,
                remote_static_tls_context=self.tls_context,
                remote_json_review_source_database=database,
                remote_json_review_site_store=page_store.root,
                remote_json_review_store=review_selection['review_store'],
                remote_json_review_sha256=receipt['review_sha256'],
                remote_learning_consents_dir=self.root / 'readonly-data-consents',
                remote_learning_stream_dir=self.root / 'readonly-data-outbox')
            self.assertEqual(review_scheduler.status()['remote_static_assets']['review_sha256'],
                             receipt['review_sha256'])

            async def run_fresh_bundle():
                fresh_owner = controller.state()
                fresh_job_id = review_scheduler.start(
                    fresh_owner['lease_id'], fresh_owner['generation'],
                    'browser_remote_static_assets')['job_id']
                for _attempt in range(500):
                    fresh_approval = review_scheduler.status()['approval']
                    if fresh_approval is not None or review_scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(fresh_approval)
                review_scheduler.respond(fresh_approval['approval_id'],
                                  fresh_approval['action_sha256'], True)
                await review_scheduler.task
                fresh_job = store.connection.execute(
                    'SELECT * FROM desktop_tasks WHERE job_id=?',
                    (fresh_job_id,)).fetchone()
                self.assertEqual(fresh_job['status'], 'succeeded')
                return fresh_job

            fresh_job = await run_fresh_bundle()
            live_match = review_scheduler.status()['jobs'][0]['json_knowledge']
            self.assertEqual(live_match, {
                'status': 'matched', 'page_key': 'summary', 'draft_revision': 1})
            live_match_row = store.connection.execute(
                "SELECT payload_json FROM observations WHERE run_id=? "
                "AND kind='browser.remote_json_knowledge'", (fresh_job['run_id'],)).fetchone()
            self.assertIsNotNone(live_match_row)
            self.assertNotIn(origin, live_match_row[0])
            self.assertNotIn('JSON managed ready', live_match_row[0])
            recheck_selection = {
                'profiles': self.profiles.root, 'site_store': page_store.root,
                'review_store': review_selection['review_store'],
                'review_sha256': receipt['review_sha256'],
                'selected_profile_sha256': self.checksum,
                'selected_plan_sha256': digest(plan.model_dump())}
            matched = recheck_remote_readonly_data_knowledge(
                database, fresh_job['run_id'], **recheck_selection)
            self.assertEqual((matched['page_key'], matched['draft_revision']),
                             ('summary', 1))
            self.assertTrue(matched['current_readback_bound'])
            self.assertFalse(matched['task_retrieval_authorized'])
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=json_app),
                    base_url=console_origin,
                    headers={'Origin': console_origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                api_recheck_selection = {
                    'current_run_id': fresh_job['run_id'],
                    'review_sha256': api_review_sha256}
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-recheck',
                    json={**api_recheck_selection,
                          'current_run_id': stable_job['run_id']})).status_code, 409)
                api_recheck_response = await client.post(
                    '/api/tasks/json-knowledge-recheck',
                    json=api_recheck_selection)
                self.assertEqual(api_recheck_response.status_code, 200,
                                 api_recheck_response.text)
                self.assertEqual(api_recheck_response.json()['page_key'], 'summary')
                self.assertFalse(api_recheck_response.json()['task_retrieval_authorized'])
                self.assertNotIn(origin, api_recheck_response.text)
            later_review = review_record.model_copy(update={
                'recorded_at': datetime.now(timezone.utc).strftime(
                    '%Y-%m-%dT%H:%M:%S.%fZ')})
            later_review_sha256 = RemoteReadonlyDataKnowledgeReviewStore(
                review_selection['review_store']).register(later_review)
            with self.assertRaises(ValueError):
                recheck_remote_readonly_data_knowledge(
                    database, fresh_job['run_id'],
                    **{**recheck_selection, 'review_sha256': later_review_sha256})
            fresh_started_at = store.connection.execute(
                'SELECT started_at FROM runs WHERE run_id=?',
                (fresh_job['run_id'],)).fetchone()[0]
            legacy_review = later_review.model_copy(update={
                'recorded_at': datetime.fromisoformat(fresh_started_at).strftime(
                    '%Y-%m-%dT%H:%M:%SZ')})
            legacy_review_sha256 = RemoteReadonlyDataKnowledgeReviewStore(
                review_selection['review_store']).register(legacy_review)
            with self.assertRaises(ValueError):
                recheck_remote_readonly_data_knowledge(
                    database, fresh_job['run_id'],
                    **{**recheck_selection, 'review_sha256': legacy_review_sha256})
            recheck_command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_knowledge_review',
                'recheck', '--database', str(database),
                '--current-run-id', fresh_job['run_id'],
                '--profiles', str(self.profiles.root),
                '--site-store', str(page_store.root),
                '--review-store', str(review_selection['review_store']),
                '--review-sha256', receipt['review_sha256'],
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump())],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(recheck_command.returncode, 0, recheck_command.stderr)
            self.assertEqual(json.loads(recheck_command.stdout), matched)
            self.assertNotIn(origin, recheck_command.stdout)
            self.assertNotIn('JSON managed ready', recheck_command.stdout)
            for table, field, changed in (
                    ('desktop_approvals', 'status', 'rejected'),
                    ('verifications', 'result', 'failed'),
                    ('human_interventions', 'actor', 'untrusted_actor')):
                with self.subTest(recheck_source=table):
                    tampered = self.root / ('readonly-recheck-' + table + '.sqlite')
                    with contextlib.closing(sqlite3.connect(tampered)) as destination:
                        store.connection.backup(destination)
                        selector = 'job_id' if table == 'desktop_approvals' else 'run_id'
                        identity = (fresh_job['job_id'] if table == 'desktop_approvals'
                                    else fresh_job['run_id'])
                        destination.execute(f'UPDATE {table} SET {field}=? WHERE {selector}=?',
                                            (changed, identity))
                        destination.commit()
                    tampered.chmod(0o600)
                    with self.assertRaises(ValueError):
                        recheck_remote_readonly_data_knowledge(
                            tampered, fresh_job['run_id'], **recheck_selection)
            self.server.asset_responses['/api/summary?view=compact&page=1'] = (
                'application/json', b'{"title":"JSON managed ready","version":3}')
            drift_job = await run_fresh_bundle()
            self.assertEqual(review_scheduler.status()['jobs'][0]['json_knowledge'], {
                'status': 'stale', 'page_key': None, 'draft_revision': None})
            live_stale_row = store.connection.execute(
                "SELECT payload_json FROM observations WHERE run_id=? "
                "AND kind='browser.remote_json_knowledge'", (drift_job['run_id'],)).fetchone()
            self.assertNotIn('page_key', json.loads(live_stale_row[0]))
            with self.assertRaises(ValueError):
                recheck_remote_readonly_data_knowledge(
                    database, drift_job['run_id'], **recheck_selection)
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=json_app),
                    base_url=console_origin,
                    headers={'Origin': console_origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                self.assertEqual((await client.post(
                    '/api/tasks/json-knowledge-recheck', json={
                        'current_run_id': drift_job['run_id'],
                        'review_sha256': api_review_sha256})).status_code, 409)
            successor_page = page.model_copy(update={
                'revision': 2, 'previous_sha256': page_sha256})
            successor_page_sha256 = digest(successor_page.model_dump())
            page_store.register(successor_page,
                                confirm_sha256=successor_page_sha256)
            with self.assertRaises(ValueError):
                recheck_remote_readonly_data_knowledge(
                    database, fresh_job['run_id'], **recheck_selection)
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    database, next_job['run_id'], stable_job['run_id'],
                    **knowledge_selection)
            self.assertEqual(preview_remote_readonly_data_knowledge(
                database, next_job['run_id'], stable_job['run_id'],
                **{**knowledge_selection,
                   'knowledge_sha256': successor_page_sha256})['draft_revision'], 2)
            for table, field, changed in (
                    ('desktop_approvals', 'status', 'rejected'),
                    ('verifications', 'result', 'failed'),
                    ('human_interventions', 'actor', 'untrusted_actor')):
                with self.subTest(table=table):
                    tampered = self.root / ('readonly-source-' + table + '.sqlite')
                    with contextlib.closing(sqlite3.connect(tampered)) as destination:
                        store.connection.backup(destination)
                        selector = 'job_id' if table == 'desktop_approvals' else 'run_id'
                        identity = job_id if table == 'desktop_approvals' else job['run_id']
                        destination.execute(f'UPDATE {table} SET {field}=? WHERE {selector}=?',
                                            (changed, identity))
                        destination.commit()
                    tampered.chmod(0o600)
                    with self.assertRaises(ValueError):
                        inspect_remote_readonly_data_source(
                            tampered, job['run_id'], profiles=self.profiles.root,
                            selected_profile_sha256=self.checksum,
                            selected_plan_sha256=digest(plan.model_dump()))
                    with self.assertRaises(ValueError):
                        compare_remote_readonly_data_change(
                            tampered, job['run_id'], next_job['run_id'], **selection)
                    self.assertEqual(preview_remote_readonly_data_knowledge(
                        tampered, next_job['run_id'], stable_job['run_id'],
                        **{**knowledge_selection,
                           'knowledge_sha256': successor_page_sha256})['status'],
                        'candidate_remote_profile_bound')
                    candidate_tampered = self.root / ('readonly-candidate-' + table + '.sqlite')
                    with contextlib.closing(sqlite3.connect(candidate_tampered)) as destination:
                        store.connection.backup(destination)
                        identity = (next_job_id if table == 'desktop_approvals'
                                    else next_job['run_id'])
                        destination.execute(f'UPDATE {table} SET {field}=? WHERE {selector}=?',
                                            (changed, identity))
                        destination.commit()
                    candidate_tampered.chmod(0o600)
                    with self.assertRaises(ValueError):
                        preview_remote_readonly_data_knowledge(
                            candidate_tampered, next_job['run_id'], stable_job['run_id'],
                            **{**knowledge_selection,
                               'knowledge_sha256': successor_page_sha256})
            revoked = revoke_remote_learning(
                consents=self.root / 'readonly-data-consents',
                consent_sha256=consent_sha256,
                confirm_sha256=consent_sha256,
                outbox_dir=self.root / 'readonly-data-outbox')
            self.assertTrue(revoked['outbox_purged'])
            with self.assertRaises(ValueError):
                poll_remote_readonly_data_stream(database, **stream_selection)
            successor_profile = self.profile.model_copy(update={
                'revision': 2, 'previous_sha256': self.checksum})
            successor_profile_sha256 = profile_report(successor_profile).profile_sha256
            self.profiles.register(successor_profile,
                                   confirm_sha256=successor_profile_sha256)
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    database, next_job['run_id'], stable_job['run_id'],
                    **{**knowledge_selection,
                       'knowledge_sha256': successor_page_sha256})
            await review_scheduler.close()
            await scheduler.close()
            if real_model:
                await engine.close()

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            asyncio.run(scenario())

    def _scheduler_static_bundle(self, *, fail_remote_learning: bool,
                                 include_image: bool = False, query_assets: bool = False):
        origin = self.profile.allowed_origins[0]
        script_path = '/assets/app.js?v=1' if query_assets else '/assets/app.js'
        style_path = '/assets/site.css?v=2' if query_assets else '/assets/site.css'
        self.server.route_bodies = {
            '/entry': (b'<html><head><title>Before</title>'
                       + f'<link rel="stylesheet" href="{style_path}">'.encode()
                       + f'<script defer src="{script_path}"></script></head>'.encode()
                       + b'<body><h1>Before</h1>'
                       + (b'<img src="/assets/logo.png" alt="Synthetic logo">'
                          if include_image else b'') + b'</body></html>')}
        self.server.asset_responses = {
            script_path: ('application/javascript',
                               b'document.title="Ready";document.querySelector("h1").textContent="Ready";'
                               b'fetch("/blocked").catch(()=>{});'),
            style_path: ('text/css', b'body{color:blue}')}
        plan_assets = [
            {'url': origin + script_path, 'content_type': 'application/javascript'},
            {'url': origin + style_path, 'content_type': 'text/css'}]
        if include_image:
            self.server.asset_responses['/assets/logo.png'] = (
                'image/png', base64.b64decode(
                    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=='))
            plan_assets.append({'url': origin + '/assets/logo.png',
                                'content_type': 'image/png'})
        plan = plan_web_static_assets(self.profiles, self.draft.task, plan_assets)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'static-trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        real_model = os.environ.get('AOS_STATIC_REAL_DECIDER_TESTS') == '1'
        engine = (ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
            if real_model else FixtureDecisionEngine())
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_static_assets_plan=plan,
            remote_static_tls_context=self.tls_context,
            remote_learning_consents_dir=self.root / 'static-learning-consents',
            remote_learning_stream_dir=self.root / 'static-learning-outbox')
        self.assertIn('browser_remote_static_assets', scheduler.kinds())
        self.assertEqual(scheduler.status()['remote_static_assets']['assets'],
                         [asset['url'] for asset in plan_assets])
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'],
                            'browser_remote_static_assets', approve_all=True)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_static_assets')['job_id']
            for _attempt in range(500):
                approval = scheduler.status()['approval']
                if approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval, scheduler.status()['jobs'])
            self.assertEqual(self.server.paths, [])
            self.assertEqual(approval['action']['tool'], 'browser.static.open')
            self.assertEqual(approval['action']['arguments'], {
                'profile_sha256': self.checksum,
                'binding_sha256': scheduler._remote_static_assets_draft.binding_sha256,
                'plan_sha256': digest(plan.model_dump()),
                'entry_url': self.profile.entry_url})
            pending_job = store.connection.execute(
                'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            selection = {'profiles': self.profiles.root,
                         'selected_profile_sha256': self.checksum,
                         'selected_plan_sha256': digest(plan.model_dump()),
                         'roles': ['system1', 'system2'],
                         'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                         'attest_data_rights': True,
                         'scope': 'remote_static_model_metadata_only'}
            consents = RemoteLearningConsents(self.root / 'static-learning-consents')
            consent_request = {'job_id': job_id, 'roles': selection['roles'],
                               'expires_at': selection['expires_at'],
                               'attest_data_rights': True}
            learning_origin = 'http://127.0.0.1:8765'
            learning_app = create_console(
                controller, 'synthetic-token', learning_origin, self.root,
                database, scheduler, web_profiles_root=self.profiles.root)
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=learning_app),
                    base_url=learning_origin, headers={'Origin': learning_origin}) as client:
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json=consent_request)).status_code, 401)
                self.assertEqual((await client.post(
                    '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                self.assertTrue((await client.get('/api/tasks')).json()['supports_remote_learning_metadata'])
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent',
                    json={**consent_request, 'attest_data_rights': False})).status_code, 400)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent',
                    json={**consent_request, 'scope': 'remote_route_model_metadata_only'})).status_code, 400)
                preview = await client.post('/api/tasks/remote-learning/consent', json=consent_request)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertFalse(preview.json()['registered'])
                self.assertFalse(consents.root.exists())
                consent_sha256 = preview.json()['consent_sha256']
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent',
                    json={**consent_request, 'confirm_sha256': '0' * 64})).status_code, 409)
                registered = await client.post('/api/tasks/remote-learning/consent',
                                               json={**consent_request,
                                                     'confirm_sha256': consent_sha256})
                self.assertEqual(registered.status_code, 200, registered.text)
                self.assertTrue(registered.json()['registered'])
                self.assertEqual(consents.get(consent_sha256).run_id, pending_job['run_id'])
                self.assertEqual(consents.get(consent_sha256).scope,
                                 'remote_static_model_metadata_only')
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning',
                    json={'job_id': job_id, 'consent_sha256': '0' * 64})).status_code, 409)
                attached_response = await client.post(
                    '/api/tasks/remote-learning',
                    json={'job_id': job_id, 'consent_sha256': consent_sha256})
                self.assertEqual(attached_response.status_code, 200, attached_response.text)
                self.assertEqual(attached_response.json()['state'], 'collecting')
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning',
                    json={'job_id': job_id, 'consent_sha256': consent_sha256})).status_code, 409)
            stream = {'profiles': self.profiles.root, 'consents': consents.root,
                      'consent_sha256': consent_sha256,
                      'outbox_dir': self.root / 'static-learning-outbox'}
            self.assertEqual(scheduler.remote_learning_status(job_id)['total_entries'], 0)
            self.assertEqual(poll_remote_static_learning_stream(
                database, **stream)['total_entries'], 0)
            with self.assertRaises(ValueError):
                poll_remote_learning_stream(database, **stream)
            with self.assertRaises(ValueError):
                preview_remote_learning_consent(
                    database, pending_job['run_id'], **{**selection, 'scope': 'remote_route_model_metadata_only'})
            with self.assertRaises(AOSFault):
                scheduler.respond(approval['approval_id'], '0' * 64, True)
            self.assertEqual(self.server.paths, [])
            if fail_remote_learning:
                with patch('aos.remote_static_learning_stream.poll_remote_static_learning_stream',
                           side_effect=ValueError('synthetic_metadata_failure')):
                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                    await scheduler.task
            else:
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                await scheduler.task
            job = store.connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(set(self.server.paths),
                             {'/entry', style_path, script_path}
                             | ({'/assets/logo.png'} if include_image else set()))
            self.assertEqual(len(self.server.paths), 3 + int(include_image))
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_static_asset_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            self.assertEqual(binding['plan_sha256'], digest(plan.model_dump()))
            self.assertEqual(binding['browser_runtime_id'], job['runtime_id'])
            self.assertEqual(store.connection.execute(
                "SELECT policy_version FROM runs WHERE run_id=?",
                (job['run_id'],)).fetchone()[0], 'browser-remote-static-assets-policy-v1')
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                (job['run_id'],)).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT tool,status FROM actions WHERE run_id=? ORDER BY created_at",
                (job['run_id'],)).fetchall()[0]['tool'], 'browser.static.open')
            self.assertNotIn('Ready', '\n'.join(store.connection.iterdump()))
            self.assertNotIn(scheduler.completed_runtime.relay.client_token,
                             '\n'.join(store.connection.iterdump()))
            source = inspect_remote_static_learning_source(
                database, job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual(source['asset_count'], len(plan_assets))
            self.assertTrue(source['transport_readback_verified'])
            self.assertEqual(source['requested_roles'], ['system1', 'system2'])
            self.assertEqual(len(source['source_event_ids_by_role']['system1']), int(real_model))
            self.assertEqual(source['source_event_ids_by_role']['system2'], [])
            self.assertFalse(source['site_outcome_verified'])
            collected = poll_remote_static_learning_stream(database, **stream)
            self.assertEqual(collected['new_entries'], int(real_model) if fail_remote_learning else 0)
            self.assertEqual(collected['entries_by_role'], {'system1': int(real_model), 'system2': 0})
            self.assertEqual(poll_remote_static_learning_stream(database, **stream)['new_entries'], 0)
            cli = subprocess.run([
                sys.executable, '-m', 'aos.remote_static_learning_stream',
                '--database', str(database), '--profiles', str(self.profiles.root),
                '--consents', str(consents.root), '--consent-sha256', consent_sha256,
                '--outbox-dir', str(stream['outbox_dir'])],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['total_entries'], int(real_model))
            self.assertNotIn(self.profile.entry_url, cli.stdout)
            automatic = scheduler.remote_learning_status(job_id)
            self.assertEqual(automatic['state'], 'failed' if fail_remote_learning else 'synced')
            if not fail_remote_learning:
                self.assertEqual(automatic['entries_by_role'],
                                 {'system1': int(real_model), 'system2': 0})
            with self.assertRaises((ValueError, FileNotFoundError)):
                poll_remote_static_learning_stream(database, **{**stream, 'consent_sha256': '0' * 64})
            with self.assertRaises(ValueError):
                inspect_remote_static_learning_source(
                    database, job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256='0' * 64)
            for table, field, value in (
                    ('desktop_approvals', 'status', 'rejected'),
                    ('verifications', 'result', 'failed'),
                    ('human_interventions', 'actor', 'untrusted_actor')):
                with self.subTest(table=table):
                    tampered = self.root / ('static-source-' + table + '.sqlite')
                    with contextlib.closing(sqlite3.connect(tampered)) as destination:
                        store.connection.backup(destination)
                        selector = 'job_id' if table == 'desktop_approvals' else 'run_id'
                        identity = job_id if table == 'desktop_approvals' else job['run_id']
                        destination.execute(f'UPDATE {table} SET {field}=? WHERE {selector}=?',
                                            (value, identity))
                        destination.commit()
                    tampered.chmod(0o600)
                    with self.assertRaises(ValueError):
                        inspect_remote_static_learning_source(
                            tampered, job['run_id'], profiles=self.profiles.root,
                            selected_profile_sha256=self.checksum,
                            selected_plan_sha256=digest(plan.model_dump()))
                    changed_stream = {**stream, 'outbox_dir': self.root / ('static-tampered-' + table)}
                    if table == 'verifications':
                        unchecked = poll_remote_static_learning_stream(tampered, **changed_stream)
                        self.assertEqual(unchecked['new_entries'], int(real_model))
                        self.assertFalse(unchecked['site_outcome_verified'])
                    else:
                        with self.assertRaises(ValueError):
                            poll_remote_static_learning_stream(tampered, **changed_stream)
            revoked = revoke_remote_learning(
                consents=consents.root, consent_sha256=consent_sha256,
                confirm_sha256=consent_sha256, outbox_dir=stream['outbox_dir'])
            self.assertTrue(revoked['outbox_purged'])
            with self.assertRaises(ValueError):
                poll_remote_static_learning_stream(database, **stream)
            await scheduler.close()

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu static metadata failure isolation test')
    def test_scheduler_static_metadata_failure_does_not_fail_task(self):
        self._scheduler_static_bundle(fail_remote_learning=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu static bundle approval rejection test')
    def test_scheduler_static_bundle_rejection_never_fetches(self):
        origin = self.profile.allowed_origins[0]
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'static-reject-trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            FixtureDecisionEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_static_assets_plan=plan,
            remote_static_tls_context=self.tls_context)

        async def scenario():
            owner = controller.state()
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_static_assets')['job_id']
            for _attempt in range(500):
                approval = scheduler.status()['approval']
                if approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval, scheduler.status()['jobs'])
            self.assertEqual(self.server.paths, [])
            scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.task
            self.assertEqual(self.server.paths, [])
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?',
                (job_id,)).fetchone()[0], 'cancelled')
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='rejected'",
                (job_id,)).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                'SELECT count(*) FROM actions WHERE run_id=(SELECT run_id FROM desktop_tasks WHERE job_id=?)',
                (job_id,)).fetchone()[0], 0)
            await scheduler.close()

        asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote MCP cancellation test')
    def test_owned_remote_mcp_stop_before_open_never_fetches(self):
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        runtime = self.remote_runtime(desktop)
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(runtime.socket_path.exists())
        runtime.stop()
        self.assertFalse(runtime.socket_path.exists())
        self.assertFalse(runtime.relay_thread.is_alive())
        self.assertEqual(self.server.paths, [])
        self.assertIsNone(runtime.relay_report)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu approved remote-entry task test')
    def test_scheduler_approved_remote_entry_and_verified_readback(self):
        self.scheduled_remote_entry(FixtureDecisionEngine(), False)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote-entry console API test')
    def test_authenticated_console_remote_entry_requires_manual_approval(self):
        self.console_remote_entry(FixtureDecisionEngine(), False)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider remote-entry console API test')
    def test_real_decider_authenticated_console_remote_entry(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.console_remote_entry(engine, True)

    @unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
        'AOS_DESKTOP_TESTS', 'AOS_HTTPS_RELAY_TESTS', 'AOS_REAL_BROWSER_TASK_TESTS', 'AOS_UI_TESTS')),
        'Explicit real Decider, owned Ubuntu MCP and unmocked console UI test')
    def test_real_decider_remote_entry_from_unmocked_ui(self):
        import uvicorn
        from playwright.sync_api import expect, sync_playwright

        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        ready = queue.Queue(maxsize=1)
        state = {}

        def serve_console():
            desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
            store = None
            scheduler = None
            try:
                desktop.start()
                database = self.root / 'trajectory.sqlite'
                store = TrajectoryStore(database)
                controller = DesktopController(store, desktop)
                engine = ReusableDeciderEngine(
                    REPO_ROOT / 'models/decider-manifest.json',
                    Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
                scheduler = DesktopScheduler(
                    controller, Settings(workspace=self.workspace, database=database),
                    engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                    desktop_browser=True,
                    remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                    remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
                    remote_entry_task=self.draft.task)
                app = create_console(controller, 'synthetic-token', origin, self.root, database, scheduler,
                                     web_profiles_root=self.profiles.root)
                server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                       access_log=False, log_level='error'))
                state['server'] = server
                ready.put(None)
                server.run()
            except BaseException as error:
                state['error'] = error
                if ready.empty():
                    ready.put(error)
            finally:
                if scheduler is not None:
                    asyncio.run(scheduler.close())
                desktop.stop()
                if store is not None:
                    store.close()

        def local_connection(address, timeout=None, **kwargs):
            if address == ('8.8.8.8', self.server.server_port):
                return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)
            return self.original_connection(address, timeout=timeout, **kwargs)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            thread = threading.Thread(target=serve_console, daemon=True)
            thread.start()
            try:
                started = ready.get(timeout=30)
                if started is not None:
                    raise started
                with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=5) as client:
                    for _attempt in range(200):
                        if 'error' in state:
                            raise state['error']
                        try:
                            if client.get('/api/session').status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail('Owned console did not become ready')
                    client.post('/api/login', json={'token': 'synthetic-token'}).raise_for_status()
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=str(
                            REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                        try:
                            page = browser.new_page(viewport={'width': 1280, 'height': 900})
                            errors = []
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            page.goto(origin + '/ui/')
                            page.get_by_label('Local session token', exact=True).fill('synthetic-token')
                            page.get_by_role('button', name='Sign in', exact=True).click()
                            expect(page.get_by_test_id('first-use-capability-browser_remote_entry')).to_contain_text('Enabled')
                            page.get_by_role('button', name='Tasks', exact=True).click()
                            page.get_by_label('Task type', exact=True).select_option('browser_remote_entry')
                            expect(page.get_by_test_id('remote-entry-scope')).to_contain_text(self.profile.entry_url)
                            expect(page.get_by_test_id('approve-all')).to_be_disabled()
                            expect(page.locator('[data-testid="learning-metadata-option"] input')).to_be_disabled()
                            page.get_by_role('button', name='Türkçe', exact=True).click()
                            expect(page.get_by_role('button', name='Giriş denemesini başlat', exact=True)).to_be_enabled()
                            page.get_by_role('button', name='English', exact=True).click()
                            self.assertEqual(self.server.paths, [])
                            page.get_by_role('button', name='Start entry trial', exact=True).click()
                            expect(page.get_by_test_id('approval')).to_contain_text('browser.remote.open', timeout=45000)
                            self.assertEqual(self.server.paths, [])
                            page.get_by_role('button', name='Approve', exact=True).click()
                            expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                            expect(page.get_by_test_id('visible-task-status')).to_contain_text('single HTTPS entry')
                            self.assertEqual(self.server.paths, ['/entry'])
                            job = client.get('/api/tasks').json()['jobs'][0]
                            self.assertEqual(job['kind'], 'browser_remote_entry')
                            self.assertTrue(job['real_model'])
                            trace = client.get('/api/runs/' + job['run_id']).json()
                            self.assertEqual([item['result'] for item in trace['verifications']], ['passed'])
                            self.assertEqual([(item['role'], item['status']) for item in trace['model_calls']],
                                             [('system1', 'ok')])
                            self.assertEqual(errors, [])
                        finally:
                            browser.close()
            finally:
                if 'server' in state:
                    state['server'].should_exit = True
                thread.join(30)
                self.assertFalse(thread.is_alive())
        if 'error' in state:
            raise state['error']

    def console_remote_entry(self, engine, real_model):
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database),
            engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task)
        origin = 'http://127.0.0.1:8765'
        app = create_console(controller, 'synthetic-token', origin, self.root, database, scheduler,
                             web_profiles_root=self.profiles.root)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                         headers={'Origin': origin}) as client:
                self.assertEqual((await client.get('/api/tasks')).status_code, 401)
                self.assertEqual((await client.post('/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                advertised = (await client.get('/api/tasks')).json()
                self.assertIn('browser_remote_entry', advertised['kinds'])
                self.assertEqual(advertised['remote_entry']['entry_url'], self.profile.entry_url)
                owner = controller.state()
                payload = {'kind': 'browser_remote_entry', 'lease_id': owner['lease_id'],
                           'generation': owner['generation']}
                for forbidden in ({'approve_all': True}, {'learning_metadata': True}):
                    self.assertEqual((await client.post('/api/tasks', json={**payload, **forbidden})).status_code, 409)
                self.assertEqual(self.server.paths, [])
                response = await client.post('/api/tasks', json=payload)
                self.assertEqual(response.status_code, 200)
                job_id = response.json()['job_id']
                for _attempt in range(500):
                    approval = (await client.get('/api/tasks')).json()['approval']
                    if approval is not None or scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval)
                self.assertEqual(approval['action']['tool'], 'browser.remote.open')
                self.assertEqual(self.server.paths, [])
                address = '/api/approvals/' + approval['approval_id']
                self.assertEqual((await client.post(address, json={
                    'action_sha256': '0' * 64, 'accept': True})).status_code, 409)
                self.assertEqual(self.server.paths, [])
                self.assertEqual((await client.post(address, json={
                    'action_sha256': approval['action_sha256'], 'accept': True})).status_code, 200)
                await scheduler.task
                jobs = (await client.get('/api/tasks')).json()['jobs']
                self.assertEqual(jobs[0]['job_id'], job_id)
                self.assertEqual(jobs[0]['status'], 'succeeded')
                self.assertEqual(bool(jobs[0]['real_model']), real_model)
                self.assertEqual(self.server.paths, ['/entry'])
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                    (jobs[0]['run_id'],)).fetchone()[0], 1)
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                    (jobs[0]['run_id'],)).fetchone()[0], int(real_model))
            await scheduler.close()

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider remote-entry opt-in')
    def test_real_decider_scheduled_remote_entry_with_separate_approval(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.scheduled_remote_entry(engine, True)

    def scheduled_remote_entry(self, engine, real_model):
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        store = TrajectoryStore(self.root / 'trajectory.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=self.root / 'trajectory.sqlite'),
            engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task)
        self.assertIn('browser_remote_entry', scheduler.kinds())
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'], 'browser_remote_entry', approve_all=True)

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'], 'browser_remote_entry')['job_id']
            for _attempt in range(500):
                approval = scheduler.status()['approval']
                if approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval)
            self.assertEqual(self.server.paths, [])
            self.assertEqual(approval['action']['tool'], 'browser.remote.open')
            self.assertEqual(approval['action']['arguments']['profile_sha256'], self.checksum)
            self.assertEqual(approval['action']['arguments']['entry_url'], self.profile.entry_url)
            self.assertEqual(approval['action']['arguments']['binding_sha256'],
                             scheduler._remote_entry_draft.binding_sha256)
            scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            await scheduler.task
            job = store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(bool(job['real_model']), real_model)
            self.assertEqual(self.server.paths, ['/entry'])
            decision = store.connection.execute(
                'SELECT selected_option,confidence,policy_result FROM decisions WHERE run_id=?',
                (job['run_id'],)).fetchone()
            self.assertEqual(decision['selected_option'], 'open_entry')
            self.assertGreaterEqual(decision['confidence'], scheduler.settings.execute_min)
            self.assertEqual(decision['policy_result'], 'allow')
            if real_model:
                print('Real Decider remote entry confidence:', round(decision['confidence'], 6))
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_entry_bindings WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(binding['binding_sha256'], approval['action']['arguments']['binding_sha256'])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                (job['run_id'],)).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                (job['run_id'],)).fetchone()[0], int(real_model))
            self.assertFalse(any('Synthetic entry' in row['result_json'] for row in store.connection.execute(
                "SELECT result_json FROM actions WHERE run_id=? AND result_json IS NOT NULL",
                (job['run_id'],))))
            self.assertNotIn(scheduler.completed_runtime.relay.client_token,
                             '\n'.join(store.connection.iterdump()))
            await scheduler.close()

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu scheduled read-only route test')
    def test_scheduler_two_routes_need_separate_approvals(self):
        self.scheduled_remote_routes(FixtureDecisionEngine(), False)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu canonical-query route task test')
    def test_scheduler_canonical_query_route_requires_separate_approval(self):
        detail_url = self.profile.entry_url.replace('/entry', '/details?view=compact&page=1')
        self.server.route_bodies = {
            '/entry': (b'<html><title>Entry</title><h1>Entry</h1>'
                       b'<a href="/details?view=compact&page=1">Details</a></html>'),
            '/details?view=compact&page=1':
                b'<html><title>Details</title><h1>Query details</h1></html>'}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'query-routes-trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        real_model = os.environ.get('AOS_ROUTE_QUERY_REAL_DECIDER_TESTS') == '1'
        engine = (ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
            if real_model else FixtureDecisionEngine())
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=database), engine,
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles,
            remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_routes_plan=plan)
        self.assertEqual(scheduler.status()['remote_routes']['plan_sha256'],
                         digest(plan.model_dump()))
        self.assertEqual(scheduler.status()['remote_routes']['route_count'], 2)
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'],
                            'browser_remote_routes', approve_all=True)
        self.assertEqual(self.server.paths, [])

        async def scenario():
            try:
                job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                         'browser_remote_routes')['job_id']
                previous_approval = None
                for route_index, expected_url in enumerate(plan.routes):
                    for _attempt in range(1500):
                        approval = scheduler.status()['approval']
                        if approval is not None and approval['approval_id'] != previous_approval:
                            break
                        if scheduler.task.done():
                            self.fail('Query route task stopped before approval')
                        await asyncio.sleep(0.02)
                    else:
                        self.fail('Query route approval missing')
                    self.assertEqual(approval['action']['arguments']['url'], expected_url)
                    self.assertEqual(approval['action']['arguments']['route_index'], route_index)
                    self.assertEqual(self.server.paths,
                                     ['/entry'] if route_index == 1 else [])
                    scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                    previous_approval = approval['approval_id']
                await scheduler.task
                job = store.connection.execute(
                    'SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                    (job_id,)).fetchone()
                self.assertEqual(job['status'], 'succeeded')
                self.assertEqual(self.server.paths,
                                 ['/entry', '/details?view=compact&page=1'])
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                    (job_id,)).fetchone()[0], 2)
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                    (job['run_id'],)).fetchone()[0], 2)
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                    (job['run_id'],)).fetchone()[0], 2 if real_model else 0)
                self.assertEqual(scheduler.completed_runtime.relay.reports[1].request_sha256,
                                 digest({'method': 'GET', 'url': detail_url}))
            finally:
                await scheduler.close()
                if real_model:
                    await engine.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port),
                                            timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection),
              patch('aos.web_https_preflight.ssl.create_default_context',
                    return_value=self.tls_context)):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu three-route reviewed context test')
    def test_three_route_review_context_waits_for_target_readback(self):
        self.three_route_review_context(False)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu three-route stale target test')
    def test_three_route_review_context_rejects_stale_target(self):
        self.three_route_review_context(False, target_drift=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider three-route reviewed context test')
    def test_real_decider_three_route_review_context_waits_for_target_readback(self):
        self.three_route_review_context(True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider three-route stale target test')
    def test_real_decider_three_route_review_context_rejects_stale_target(self):
        self.three_route_review_context(True, target_drift=True)

    def three_route_review_context(self, real_model: bool, *, target_drift: bool = False):
        detail_url = self.profile.entry_url.replace('/entry', '/details')
        final_url = self.profile.entry_url.replace('/entry', '/final')
        self.server.route_bodies = {
            '/entry': (b'<html><title>Entry</title><h1>Entry</h1>'
                       b'<a href="/details">Details</a></html>'),
            '/details': b'<html><title>Details</title><h1>Details</h1></html>',
            '/final': b'<html><title>Final</title><h1>Final</h1></html>'}
        plan = plan_web_readonly_routes(
            self.profiles, self.draft.task,
            [self.profile.entry_url, detail_url, final_url])
        plan_sha256 = digest(plan.model_dump())
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        database = self.root / 'three-routes-trajectory.sqlite'
        store = TrajectoryStore(database)
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        settings = Settings(workspace=self.workspace, database=database)
        scheduler_options = dict(
            browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles,
            remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task,
            remote_routes_plan=plan)
        scheduler = DesktopScheduler(
            controller, settings, FixtureDecisionEngine(), **scheduler_options)

        async def run_routes(active_scheduler):
            owner = controller.state()
            job_id = active_scheduler.start(
                owner['lease_id'], owner['generation'], 'browser_remote_routes')['job_id']
            for route_index in range(3):
                approval = None
                for _attempt in range(1500):
                    approval = active_scheduler.status()['approval']
                    if approval is not None or active_scheduler.task.done():
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(approval, active_scheduler.status()['jobs'])
                self.assertEqual(approval['action']['arguments']['route_index'], route_index)
                active_scheduler.respond(
                    approval['approval_id'], approval['action_sha256'], True)
            await active_scheduler.task
            job = store.connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 3)
            return job['run_id']

        def local_connection(_address, timeout=None):
            return self.original_connection(
                ('127.0.0.1', self.server.server_port), timeout=timeout)

        async def scenario():
            try:
                before_run_id = await run_routes(scheduler)
                after_run_id = await run_routes(scheduler)
                evidence = review_remote_route_evidence(
                    database, after_run_id, profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=plan_sha256)
                page_store = SiteKnowledgeStore(self.root / 'three-route-pages', self.profiles)

                def register_page(page_key, route_template, fingerprint, outgoing_keys):
                    page = SitePageDraft(
                        schema_version='1.0', profile_sha256=self.checksum,
                        application_key=self.profile.application_key,
                        tenant_key=self.profile.tenant_key,
                        account_role=self.profile.account_role,
                        page_key=page_key, revision=1, previous_sha256=None,
                        origin=self.profile.allowed_origins[0],
                        route_template=route_template,
                        page_fingerprint_sha256=fingerprint,
                        recorded_at='2026-09-24T00:00:00Z',
                        landmark_keys=['entry_heading'] if page_key == 'entry' else [],
                        outgoing_page_keys=outgoing_keys,
                        source_kind='manual_draft', status='draft',
                        execution_authorized=False, collection_authorized=False,
                        training_ready=False)
                    checksum = digest(page.model_dump())
                    page_store.register(page, confirm_sha256=checksum)
                    return checksum

                register_page('details', '/details',
                              evidence['routes'][1]['page_fingerprint_sha256'], [])
                entry_sha256 = register_page(
                    'entry', '/entry', evidence['routes'][0]['page_fingerprint_sha256'],
                    ['details'])
                review_options = dict(
                    profiles=self.profiles.root, site_store=page_store.root,
                    review_store=self.root / 'three-route-reviews',
                    knowledge_sha256=entry_sha256,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=plan_sha256, route_index=0)
                candidate = preview_remote_route_knowledge(
                    database, before_run_id, after_run_id,
                    profiles=self.profiles.root, store=page_store.root,
                    knowledge_sha256=entry_sha256,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=plan_sha256, route_index=0)
                self.assertEqual(candidate['outgoing_route_indices'], [1])
                receipt = register_remote_route_knowledge_review(
                    database, before_run_id, after_run_id,
                    **review_options,
                    confirm_candidate_sha256=digest(candidate),
                    acknowledge_metadata_only=True)
                scheduler.release_completed_runtime()
                if target_drift:
                    self.server.route_bodies['/details'] = (
                        b'<html><title>Details updated</title>'
                        b'<h1>Details updated</h1></html>')
                engine = (ReusableDeciderEngine(
                    REPO_ROOT / 'models/decider-manifest.json',
                    Path(os.environ.get('AOS_MODEL_PYTHON',
                                        str(Path.home() / '.venv/bin/python'))))
                    if real_model else FixtureDecisionEngine())
                reviewed_scheduler = DesktopScheduler(
                    controller, settings, engine, **scheduler_options,
                    remote_route_review_source_database=database,
                    remote_route_review_site_store=page_store.root,
                    remote_route_review_store=review_options['review_store'],
                    remote_route_review_sha256=receipt['review_sha256'])
                try:
                    current_run_id = await run_routes(reviewed_scheduler)
                    observations = [json.loads(row[0]) for row in store.connection.execute(
                        "SELECT payload_json FROM observations WHERE run_id=? "
                        "AND kind='browser.remote_route' ORDER BY rowid",
                        (current_run_id,)).fetchall()]
                    choices = {observation['next_route_index']: observation
                               for observation in observations
                               if 'next_route_index' in observation}
                    self.assertNotIn('reviewed_route_metadata', choices[0])
                    self.assertNotIn('reviewed_route_metadata', choices[1])
                    if target_drift:
                        self.assertNotIn('reviewed_route_metadata', choices[2])
                        events = [json.loads(row[0]) for row in store.connection.execute(
                            "SELECT payload_json FROM observations WHERE run_id=? "
                            "AND kind='browser.remote_route_knowledge' ORDER BY rowid",
                            (current_run_id,)).fetchall()]
                        self.assertEqual([(event['route_index'], event['status'])
                                          for event in events], [(0, 'matched'), (1, 'stale')])
                        self.assertEqual(events[1]['stale_reason'],
                                         'target_fingerprint_changed')
                    else:
                        self.assertEqual(choices[2]['reviewed_route_metadata']['page_key'],
                                         'entry')
                        self.assertEqual(choices[2]['reviewed_route_metadata']['outgoing_page_keys'],
                                         ['details'])
                    snapshots = [json.loads(row[0]) for row in store.connection.execute(
                        'SELECT state_json FROM state_snapshots WHERE run_id=?',
                        (current_run_id,)).fetchall()]
                    self.assertFalse(any(
                        'Live-fingerprint-matched historical symbolic metadata:' in
                        snapshot['observation'] and 'Exact route 2 of 3' in snapshot['observation']
                        for snapshot in snapshots))
                    self.assertEqual(any(
                        'Live-fingerprint-matched historical symbolic metadata:' in
                        snapshot['observation'] and 'Exact route 3 of 3' in snapshot['observation']
                        for snapshot in snapshots), not target_drift)
                    self.assertEqual(store.connection.execute(
                        "SELECT count(*) FROM model_calls WHERE run_id=? "
                        "AND role='system1' AND status='ok'",
                        (current_run_id,)).fetchone()[0], 3 if real_model else 0)
                    self.assertEqual(self.server.paths,
                                     ['/entry', '/details', '/final'] * 3)
                finally:
                    await reviewed_scheduler.close()
            finally:
                await scheduler.close()

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection',
                      side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context',
                      return_value=self.tls_context):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider read-only route test')
    def test_real_decider_two_routes_need_separate_approvals(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.scheduled_remote_routes(engine, True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote metadata failure isolation test')
    def test_remote_learning_failure_does_not_fail_route_task(self):
        self.scheduled_remote_routes(FixtureDecisionEngine(), False,
                                     fail_remote_learning=True)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1'
                         and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real Decider revocation during owned route task')
    def test_real_decider_revocation_stops_metadata_without_failing_routes(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.scheduled_remote_routes(engine, True, revoke_remote_learning_midrun=True)

    def scheduled_remote_routes(self, engine, real_model, *, fail_remote_learning=False,
                                revoke_remote_learning_midrun=False):
        detail_url = self.profile.entry_url.replace('/entry', '/details')
        self.server.route_bodies = {
            '/entry': (b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                       b'<a href="/details">Details</a><a href="/outside">Outside</a></html>'),
            '/details': b'<html><title>Relay details</title><h1>Synthetic details</h1></html>'}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        store = TrajectoryStore(self.root / 'routes-trajectory.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=self.root / 'routes-trajectory.sqlite'),
            engine, browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_routes_plan=plan,
            remote_learning_consents_dir=self.root / 'active-route-consents',
            remote_learning_stream_dir=self.root / 'automatic-route-outbox')
        owner = controller.state()
        with self.assertRaises(AOSFault):
            scheduler.start(owner['lease_id'], owner['generation'],
                            'browser_remote_routes', approve_all=True)

        async def next_approval():
            for _attempt in range(1500):
                approval = scheduler.status()['approval']
                if approval is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval)
            return approval

        async def scenario():
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_routes')['job_id']
            first = await next_approval()
            consent_request = {
                'job_id': job_id,
                'roles': ['system1', 'system2'],
                'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                'attest_data_rights': True,
            }
            active_consents = self.root / 'active-route-consents'
            learning_origin = 'http://127.0.0.1:8765'
            learning_app = create_console(
                controller, 'synthetic-token', learning_origin, self.root,
                self.root / 'routes-trajectory.sqlite', scheduler,
                web_profiles_root=self.profiles.root)
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=learning_app),
                    base_url=learning_origin,
                    headers={'Origin': learning_origin}) as client:
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json=consent_request)).status_code, 401)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning', json={'job_id': job_id,
                                                        'consent_sha256': '0' * 64})).status_code, 401)
                self.assertEqual((await client.post(
                    '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json={**consent_request,
                                                                'attest_data_rights': False})).status_code, 400)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json={**consent_request,
                                                                'roles': [['system1']]})).status_code, 400)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json={**consent_request,
                                                                'profile_sha256': self.checksum})).status_code, 400)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent',
                    content=f'{{"job_id":"{job_id}","job_id":"{job_id}"}}')).status_code, 400)
                wrong_source = sqlite3.connect(self.root / 'wrong-consent-source.sqlite')
                wrong_source.close()
                with patch.object(scheduler, 'settings', scheduler.settings.model_copy(
                        update={'database': self.root / 'wrong-consent-source.sqlite'})):
                    self.assertEqual((await client.post(
                        '/api/tasks/remote-learning/consent', json=consent_request)).status_code, 409)
                preview = await client.post('/api/tasks/remote-learning/consent', json=consent_request)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertFalse(preview.json()['registered'])
                self.assertFalse(active_consents.exists())
                active_consent_sha256 = preview.json()['consent_sha256']
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json={**consent_request,
                                                                'confirm_sha256': '0' * 64})).status_code, 409)
                registered = await client.post('/api/tasks/remote-learning/consent',
                                               json={**consent_request,
                                                     'confirm_sha256': active_consent_sha256})
                self.assertEqual(registered.status_code, 200, registered.text)
                self.assertTrue(registered.json()['registered'])
                active_consent = RemoteLearningConsents(active_consents).get(active_consent_sha256)
                self.assertEqual(active_consent.run_id, first['action']['run_id'])
                self.assertFalse(active_consent.training_authorized)
                selection = {'job_id': job_id, 'consent_sha256': active_consent_sha256}
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning', json={**selection,
                                                        'consent_sha256': '0' * 64})).status_code, 409)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning', json={**selection,
                                                        'run_id': active_consent.run_id})).status_code, 400)
                attached = await client.post('/api/tasks/remote-learning', json=selection)
                self.assertEqual(attached.status_code, 200, attached.text)
                self.assertEqual(attached.json()['state'], 'collecting')
                self.assertEqual(attached.json()['total_entries'], 0)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning', json=selection)).status_code, 409)
                self.assertEqual((await client.post(
                    '/api/tasks/remote-learning/consent', json=consent_request)).status_code, 409)
            stream_selection = {
                'profiles': self.profiles.root, 'consents': active_consents,
                'consent_sha256': active_consent_sha256,
                'outbox_dir': (self.root / 'automatic-route-outbox'
                               if revoke_remote_learning_midrun else self.root / 'active-route-outbox'),
            }
            initial = poll_remote_learning_stream(
                self.root / 'routes-trajectory.sqlite', **stream_selection)
            self.assertEqual(initial['new_entries'], 0)
            if fail_remote_learning:
                (active_consents / (active_consent_sha256 + '.json')).chmod(0o644)
            self.assertEqual(self.server.paths, [])
            self.assertEqual(first['action']['tool'], 'browser.remote.route')
            self.assertEqual(first['action']['arguments']['route_index'], 0)
            self.assertEqual(first['action']['arguments']['url'], self.profile.entry_url)
            with self.assertRaises(AOSFault):
                scheduler.respond(first['approval_id'], '0' * 64, True)
            self.assertEqual(self.server.paths, [])
            scheduler.respond(first['approval_id'], first['action_sha256'], True)
            for _attempt in range(1500):
                approval = scheduler.status()['approval']
                if approval is not None and approval['approval_id'] != first['approval_id']:
                    break
                if scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(approval)
            self.assertNotEqual(approval['approval_id'], first['approval_id'])
            self.assertEqual(self.server.paths, ['/entry'])
            self.assertEqual(approval['action']['arguments']['route_index'], 1)
            self.assertEqual(approval['action']['arguments']['url'], detail_url)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
            if fail_remote_learning:
                with self.assertRaises(ValueError):
                    poll_remote_learning_stream(
                        self.root / 'routes-trajectory.sqlite', **stream_selection)
                self.assertEqual(scheduler.remote_learning_status(job_id)['state'], 'failed')
            elif revoke_remote_learning_midrun:
                self.assertEqual(scheduler.remote_learning_status(job_id)['entries_by_role']['system1'], 1)
                revoked = revoke_remote_learning(
                    consents=active_consents, consent_sha256=active_consent_sha256,
                    confirm_sha256=active_consent_sha256,
                    outbox_dir=self.root / 'automatic-route-outbox')
                self.assertTrue(revoked['outbox_purged'])
                with self.assertRaises(ValueError):
                    poll_remote_learning_stream(
                        self.root / 'routes-trajectory.sqlite', **stream_selection)
            else:
                middle = poll_remote_learning_stream(
                    self.root / 'routes-trajectory.sqlite', **stream_selection)
                self.assertEqual(middle['new_entries'], int(real_model))
                self.assertEqual(middle['entries_by_role']['system2'], 0)
                self.assertEqual(scheduler.remote_learning_status(job_id)['entries_by_role']['system1'],
                                 int(real_model))
            scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            await scheduler.task
            job = store.connection.execute('SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                                           (job_id,)).fetchone()
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(bool(store.connection.execute(
                'SELECT real_model FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0]),
                real_model)
            self.assertEqual(self.server.paths, ['/entry', '/details'])
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM model_calls WHERE run_id=? AND role='system1' AND status='ok'",
                (job['run_id'],)).fetchone()[0], 2 if real_model else 0)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM verifications WHERE run_id=? AND result='passed'",
                (job['run_id'],)).fetchone()[0], 2)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 2)
            automatic = scheduler.remote_learning_status(job_id)
            if fail_remote_learning or revoke_remote_learning_midrun:
                self.assertEqual(automatic['state'], 'failed')
            else:
                settled = poll_remote_learning_stream(
                    self.root / 'routes-trajectory.sqlite', **stream_selection)
                self.assertEqual(settled['new_entries'], int(real_model))
                self.assertEqual(settled['entries_by_role']['system1'], 2 if real_model else 0)
                self.assertEqual(settled['entries_by_role']['system2'], 0)
                self.assertEqual(poll_remote_learning_stream(
                    self.root / 'routes-trajectory.sqlite', **stream_selection)['new_entries'], 0)
                self.assertEqual(automatic['state'], 'synced')
                self.assertEqual(automatic['entries_by_role']['system1'], 2 if real_model else 0)
                scheduler._remote_learning_attached.clear()
                self.assertEqual(scheduler.remote_learning_status(job_id)['state'], 'synced')
            binding = store.connection.execute(
                'SELECT * FROM desktop_remote_route_bindings WHERE job_id=?', (job_id,)).fetchone()
            self.assertEqual(binding['plan_sha256'], digest(plan.model_dump()))
            self.assertEqual(binding['browser_runtime_id'], scheduler.completed_runtime.runtime_id)
            consent_selection = {
                'profiles': self.profiles.root,
                'selected_profile_sha256': self.checksum,
                'selected_plan_sha256': digest(plan.model_dump()),
                'roles': ['system1', 'system2'],
                'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                'attest_data_rights': True,
            }
            consent = preview_remote_learning_consent(
                self.root / 'routes-trajectory.sqlite', job['run_id'], **consent_selection)
            self.assertEqual(consent.binding_sha256, binding['binding_sha256'])
            consents = RemoteLearningConsents(self.root / 'remote-learning-consents')
            consent_sha256 = digest(consent.model_dump())
            consent_source = {'database': self.root / 'routes-trajectory.sqlite',
                              'profiles': self.profiles.root}
            with self.assertRaises(ValueError):
                consents.register(consent, confirm_sha256='0' * 64, **consent_source)
            self.assertEqual(consents.register(consent, confirm_sha256=consent_sha256,
                                               **consent_source), consent_sha256)
            self.assertEqual(consents.get(consent_sha256), consent)
            self.assertEqual(consents.register(consent, confirm_sha256=consent_sha256,
                                               **consent_source), consent_sha256)
            self.assertFalse(consent.external_rights_verified)
            self.assertFalse(consent.training_authorized)
            with self.assertRaises(ValueError):
                preview_remote_learning_consent(
                    self.root / 'routes-trajectory.sqlite', job['run_id'],
                    **{**consent_selection, 'selected_plan_sha256': '0' * 64})
            with self.assertRaises(ValueError):
                preview_remote_learning_consent(
                    self.root / 'routes-trajectory.sqlite', job['run_id'],
                    **{**consent_selection, 'attest_data_rights': False})
            self.assertNotIn(scheduler.completed_runtime.relay.client_token,
                             '\n'.join(store.connection.iterdump()))
            if real_model:
                source_database = self.root / 'routes-trajectory.sqlite'
                evidence = review_remote_route_evidence(
                    source_database, job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
                learning = review_learning_events(source_database, job['run_id'])
                self.assertEqual(len(learning['events']), 2)
                page_store = SiteKnowledgeStore(self.root / 'site-knowledge', self.profiles)
                page = SitePageDraft(
                    schema_version='1.0', profile_sha256=self.checksum,
                    application_key=self.profile.application_key,
                    tenant_key=self.profile.tenant_key, account_role=self.profile.account_role,
                    page_key='entry', revision=1, previous_sha256=None,
                    origin=self.profile.allowed_origins[0], route_template='/entry',
                    page_fingerprint_sha256=evidence['routes'][0]['page_fingerprint_sha256'],
                    recorded_at='2026-09-24T00:00:00Z', landmark_keys=[],
                    outgoing_page_keys=[], source_kind='manual_draft', status='draft',
                    execution_authorized=False, collection_authorized=False,
                    training_ready=False)
                page_sha256 = digest(page.model_dump())
                page_store.register(page, confirm_sha256=page_sha256)
                skill_store = SiteSkillStore(self.root / 'site-skills', self.profiles, page_store)
                first_action = store.connection.execute(
                    "SELECT step_id,decision_id FROM actions WHERE run_id=? AND tool='browser.remote.route' "
                    "AND json_extract(arguments_json,'$.route_index')=0",
                    (job['run_id'],)).fetchone()
                first_event = next(event for event in learning['events']
                                   if event['source']['decision_id'] == first_action['decision_id'])
                self.assertEqual(first_event['source']['step_id'], first_action['step_id'])
                skill = SiteSkillDraft(
                    schema_version='1.0', profile_sha256=self.checksum,
                    application_key=self.profile.application_key,
                    tenant_key=self.profile.tenant_key, account_role=self.profile.account_role,
                    task_key=self.draft.task.task_key, page_key='entry',
                    page_draft_sha256=page_sha256, skill_key='entry-route-choice',
                    model_role='system1', candidate_kind='finite_action_choice',
                    revision=1, previous_sha256=None, parameter_keys=[],
                    precondition_keys=[], step_keys=['open-entry'],
                    expected_outcome_key='entry-readback',
                    source_event_ids=[first_event['event_id']], source_verification_ids=[],
                    source_kind='manual_candidate', status='draft',
                    execution_authorized=False, collection_authorized=False,
                    training_ready=False, activation_authorized=False)
                skill_sha256 = digest(skill.model_dump())
                skill_store.register(skill, confirm_sha256=skill_sha256)
                supervisor_skill = SiteSkillDraft.model_validate({
                    **skill.model_dump(), 'skill_key': 'entry-route-plan',
                    'model_role': 'system2', 'candidate_kind': 'workflow_plan',
                    'source_event_ids': ['learning-' + 'f' * 64]})
                supervisor_sha256 = digest(supervisor_skill.model_dump())
                skill_store.register(supervisor_skill, confirm_sha256=supervisor_sha256)
                selection = {'profiles': self.profiles.root, 'pages': page_store.root,
                             'skills': skill_store.root, 'skill_sha256': skill_sha256,
                             'selected_plan_sha256': digest(plan.model_dump()), 'route_index': 0}
                with patch('aos.remote_site_skill_provenance.audit_snapshot',
                           wraps=audit_snapshot) as audited:
                    provenance = inspect_remote_site_skill_sources(
                        source_database, job['run_id'], **selection)
                self.assertEqual(audited.call_count, 1)
                validator('remote_site_skill_provenance').validate(provenance)
                self.assertTrue(provenance['profile_bound'])
                self.assertTrue(provenance['transport_readback_verified'])
                self.assertFalse(provenance['site_outcome_verified'])
                self.assertFalse(provenance['training_ready'])
                self.assertNotIn(self.profile.entry_url, canonical(provenance))
                skill_origin = 'http://127.0.0.1:8765'
                skill_app = create_console(
                    controller, 'synthetic-token', skill_origin, self.root,
                    source_database, scheduler, web_profiles_root=self.profiles.root,
                    site_knowledge_root=page_store.root, site_skills_root=skill_store.root)
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=skill_app),
                        base_url=skill_origin, headers={'Origin': skill_origin}) as client:
                    skill_selection = {'run_id': job['run_id'], 'route_index': 0,
                                       'skill_sha256': skill_sha256}
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', json=skill_selection)).status_code, 401)
                    self.assertEqual((await client.get('/api/tasks/skill-drafts')).status_code, 401)
                    self.assertEqual((await client.post(
                        '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                    self.assertEqual((await client.get('/api/tasks/skill-drafts?profile=ignored')).status_code, 400)
                    self.assertEqual((await client.get('/api/tasks/skill-drafts')).json(),
                                     skill_store.list(profile_sha256=self.checksum, model_role='system1'))
                    self.assertEqual((await client.get('/api/tasks/skill-drafts?model_role=system1')).json(),
                                     skill_store.list(profile_sha256=self.checksum, model_role='system1'))
                    self.assertEqual((await client.get('/api/tasks/skill-drafts?model_role=system2')).json(),
                                     skill_store.list(profile_sha256=self.checksum, model_role='system2'))
                    for query in ('model_role=other', 'model_role=system2&model_role=system1',
                                  'model_role=system2&profile=ignored'):
                        self.assertEqual((await client.get('/api/tasks/skill-drafts?' + query)).status_code, 400)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', json={**skill_selection,
                            'skill_sha256': supervisor_sha256})).status_code, 409)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', json={
                            **skill_selection, 'run_id': 'other-run'})).status_code, 404)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', json={
                            **skill_selection, 'route_index': True})).status_code, 404)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect?run_id=ignored',
                        json=skill_selection)).status_code, 400)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', content=(
                            '{"run_id":"%s","route_index":0,"skill_sha256":"%s",'
                            '"skill_sha256":"%s"}' % (job['run_id'], skill_sha256,
                                                       skill_sha256)),
                        headers={'Content-Type': 'application/json'})).status_code, 400)
                    self.assertEqual((await client.post(
                        '/api/tasks/skill-source-inspect', json={
                            **skill_selection, 'skill_sha256': '0' * 64})).status_code, 409)
                    response = await client.post('/api/tasks/skill-source-inspect',
                                                 json=skill_selection)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), provenance)
                    self.assertNotIn(self.profile.entry_url, response.text)
                with self.assertRaises(ValueError):
                    inspect_remote_site_skill_sources(
                        source_database, job['run_id'], **{**selection, 'route_index': 1})
                with self.assertRaises(ValueError):
                    inspect_remote_site_skill_sources(
                        source_database, job['run_id'],
                        **{**selection, 'selected_plan_sha256': '0' * 64})
                other_task_key = next(key for key in self.profile.task_keys
                                      if key != self.draft.task.task_key)
                wrong_task = skill.model_copy(update={'task_key': other_task_key,
                                                      'skill_key': 'wrong-task-choice'})
                wrong_task_sha256 = digest(wrong_task.model_dump())
                skill_store.register(wrong_task, confirm_sha256=wrong_task_sha256)
                with self.assertRaises(ValueError):
                    inspect_remote_site_skill_sources(
                        source_database, job['run_id'],
                        **{**selection, 'skill_sha256': wrong_task_sha256})
                second_event = next(event for event in learning['events']
                                    if event['source']['decision_id'] != first_action['decision_id'])
                wrong_event = skill.model_copy(update={
                    'skill_key': 'other-route-choice',
                    'source_event_ids': [second_event['event_id']]})
                wrong_event_sha256 = digest(wrong_event.model_dump())
                skill_store.register(wrong_event, confirm_sha256=wrong_event_sha256)
                with self.assertRaises(ValueError):
                    inspect_remote_site_skill_sources(
                        source_database, job['run_id'],
                        **{**selection, 'skill_sha256': wrong_event_sha256})
            second_run_id = None
            if not real_model:
                self.server.route_bodies['/details'] = (
                    b'<html><title>Relay details changed</title><h1>Synthetic details</h1></html>')
                second_job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                                'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_approval()
                    scheduler.respond(next_action['approval_id'], next_action['action_sha256'], True)
                await scheduler.task
                second_job = store.connection.execute(
                    'SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                    (second_job_id,)).fetchone()
                self.assertEqual(second_job['status'], 'succeeded')
                second_run_id = second_job['run_id']
                self.assertEqual(self.server.paths,
                                 ['/entry', '/details', '/entry', '/details'])
                change = compare_remote_route_change(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                    profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
                self.assertEqual(change['status'], 'changed_profile_bound')
                self.assertEqual([route['changed'] for route in change['routes']],
                                 [False, True])
                self.assertEqual([route['sampled_link_inventory_changed']
                                  for route in change['routes']], [False, False])
                self.assertEqual(change['routes'][0]['planned_link_indices'], [1])
                self.assertNotIn(detail_url, canonical(change))
                seed_arguments = {'profiles': self.profiles.root,
                                  'store': self.root / 'site-knowledge',
                                  'selected_profile_sha256': self.checksum,
                                  'selected_plan_sha256': digest(plan.model_dump()),
                                  'route_index': 0, 'page_key': 'entry'}
                with patch('aos.remote_page_draft_seed.audit_snapshot', wraps=audit_snapshot) as audited_seed:
                    seeded_page, seed_report = seed_remote_page_draft(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                        **seed_arguments)
                self.assertEqual(audited_seed.call_count, 1)
                self.assertEqual(seeded_page.origin + seeded_page.route_template, self.profile.entry_url)
                self.assertEqual(seeded_page.page_fingerprint_sha256,
                                 change['routes'][0]['after_fingerprint_sha256'])
                self.assertEqual(seed_report['draft_sha256'], digest(seeded_page.model_dump()))
                self.assertFalse(seed_report['semantic_page_key_verified'])
                self.assertFalse(seed_report['reviewed'])
                self.assertFalse((self.root / 'site-knowledge').exists())
                occupied_store = SiteKnowledgeStore(self.root / 'seed-occupied', self.profiles)
                occupied_store.register(seeded_page, confirm_sha256=digest(seeded_page.model_dump()))
                with self.assertRaises(ValueError):
                    seed_remote_page_draft(self.root / 'routes-trajectory.sqlite',
                        job['run_id'], second_run_id,
                        **{**seed_arguments, 'store': occupied_store.root})
                with self.assertRaises(ValueError):
                    seed_remote_page_draft(self.root / 'routes-trajectory.sqlite',
                        job['run_id'], second_run_id, **{**seed_arguments, 'route_index': 1})
                with self.assertRaises(ValueError):
                    seed_remote_page_draft(self.root / 'routes-trajectory.sqlite',
                        job['run_id'], second_run_id,
                        **{**seed_arguments, 'selected_plan_sha256': '0' * 64})
                seed_output = self.root / 'seeded-entry.json'
                seed_cli_args = ['--database', str(self.root / 'routes-trajectory.sqlite'),
                                 '--before-run-id', job['run_id'], '--after-run-id', second_run_id,
                                 '--profiles', str(self.profiles.root), '--store', str(seed_arguments['store']),
                                 '--selected-profile-sha256', self.checksum,
                                 '--selected-plan-sha256', digest(plan.model_dump()),
                                 '--route-index', '0', '--page-key', 'entry',
                                 '--output', str(seed_output)]
                seed_stdout = io.StringIO()
                with contextlib.redirect_stdout(seed_stdout):
                    remote_page_draft_seed_main(seed_cli_args)
                self.assertEqual(json.loads(seed_stdout.getvalue())['draft_sha256'],
                                 digest(SitePageDraft.model_validate_json(seed_output.read_bytes()).model_dump()))
                self.assertEqual(seed_output.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(self.profile.entry_url, seed_stdout.getvalue())
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        remote_page_draft_seed_main(seed_cli_args)
                changed_graph = preview_remote_navigation_graph(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                    profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
                self.assertEqual([page['route_index'] for page in changed_graph['stable_pages']], [0])
                self.assertEqual(changed_graph['observed_edges'], [])
                self.assertEqual(changed_graph['changed_route_indices'], [1])
                cli_arguments = ['--database', str(self.root / 'routes-trajectory.sqlite'),
                                 '--before-run-id', job['run_id'], '--after-run-id', second_run_id,
                                 '--profiles', str(self.profiles.root),
                                 '--selected-profile-sha256', self.checksum,
                                 '--selected-plan-sha256', digest(plan.model_dump())]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    remote_route_change_main(cli_arguments)
                self.assertEqual(json.loads(output.getvalue()), change)
                with self.assertRaises(ValueError):
                    compare_remote_route_change(
                        self.root / 'routes-trajectory.sqlite', second_run_id, job['run_id'],
                        profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()))
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as failure:
                        remote_route_change_main([*cli_arguments,
                            '--before-run-id', second_run_id, '--after-run-id', job['run_id']])
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(output.getvalue(), '')
                self.assertNotIn(str(self.root), errors.getvalue())
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a></html>')
                third_job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                               'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_approval()
                    scheduler.respond(next_action['approval_id'], next_action['action_sha256'], True)
                await scheduler.task
                third_run_id = store.connection.execute(
                    'SELECT run_id FROM desktop_tasks WHERE job_id=? AND status=\'succeeded\'',
                    (third_job_id,)).fetchone()[0]
                with patch('aos.remote_route_change.audit_snapshot', wraps=audit_snapshot) as audited:
                    unchanged = compare_remote_route_change(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()))
                self.assertEqual(audited.call_count, 1)
                self.assertEqual(unchanged['status'], 'unchanged_profile_bound')
                self.assertEqual([route['changed'] for route in unchanged['routes']],
                                 [False, False])
                self.assertEqual([route['sampled_link_inventory_changed']
                                  for route in unchanged['routes']], [True, False])
                incomplete_graph = preview_remote_navigation_graph(
                    self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                    profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
                self.assertEqual([page['route_index'] for page in incomplete_graph['stable_pages']], [0, 1])
                self.assertEqual(incomplete_graph['observed_edges'], [])
                self.assertEqual(incomplete_graph['incomplete_link_sample_indices'], [0])
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a><a href="/outside">Outside</a></html>')
                self.server.route_bodies['/details'] = (
                    b'<html><title>Relay details</title><h1>Synthetic details</h1></html>')
                graph_job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                               'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_approval()
                    scheduler.respond(next_action['approval_id'], next_action['action_sha256'], True)
                await scheduler.task
                graph_run_id = store.connection.execute(
                    "SELECT run_id FROM desktop_tasks WHERE job_id=? AND status='succeeded'",
                    (graph_job_id,)).fetchone()[0]
                with patch('aos.remote_navigation_graph.audit_snapshot', wraps=audit_snapshot) as audited_graph:
                    graph = preview_remote_navigation_graph(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], graph_run_id,
                        profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()))
                self.assertEqual(audited_graph.call_count, 1)
                self.assertEqual([page['route_index'] for page in graph['stable_pages']], [0, 1])
                self.assertEqual(graph['observed_edges'], [
                    {'source_route_index': 0, 'target_route_index': 1}])
                self.assertEqual(graph['incomplete_link_sample_indices'], [])
                self.assertFalse(graph['task_retrieval_authorized'])
                self.assertNotIn(detail_url, canonical(graph))
                graph_output = io.StringIO()
                with contextlib.redirect_stdout(graph_output):
                    remote_navigation_graph_main([
                        '--database', str(self.root / 'routes-trajectory.sqlite'),
                        '--before-run-id', job['run_id'], '--after-run-id', graph_run_id,
                        '--profiles', str(self.profiles.root),
                        '--selected-profile-sha256', self.checksum,
                        '--selected-plan-sha256', digest(plan.model_dump())])
                self.assertEqual(json.loads(graph_output.getvalue()), graph)
                graph_origin = 'http://127.0.0.1:8765'
                graph_app = create_console(
                    controller, 'synthetic-token', graph_origin, self.root,
                    self.root / 'routes-trajectory.sqlite', scheduler,
                    web_profiles_root=self.profiles.root,
                    site_knowledge_root=self.root / 'seed-registered',
                    page_seed_root=self.root,
                    route_review_root=self.root / 'seed-reviews')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=graph_app),
                                             base_url=graph_origin,
                                             headers={'Origin': graph_origin}) as client:
                    selection = {'before_run_id': job['run_id'], 'after_run_id': graph_run_id}
                    seed_selection = {**selection, 'route_index': 0, 'page_key': 'entry'}
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph', json=selection)).status_code, 401)
                    self.assertEqual((await client.post(
                        '/api/tasks/page-draft-seed', json=seed_selection)).status_code, 401)
                    self.assertEqual((await client.post(
                        '/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph', json={**selection,
                            'after_run_id': 'missing'})).status_code, 404)
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph', json={**selection,
                            'after_run_id': job['run_id']})).status_code, 404)
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph', json={
                            'before_run_id': graph_run_id,
                            'after_run_id': job['run_id']})).status_code, 409)
                    graph_response = await client.post('/api/tasks/navigation-graph', json=selection)
                    self.assertEqual(graph_response.status_code, 200)
                    self.assertEqual(graph_response.json(), graph)
                    self.assertNotIn(detail_url, graph_response.text)
                    seed_response = await client.post('/api/tasks/page-draft-seed', json=seed_selection)
                    self.assertEqual(seed_response.status_code, 200)
                    self.assertEqual(seed_response.json()['status'], 'unreviewed_private_draft')
                    self.assertFalse(seed_response.json()['reviewed'])
                    self.assertNotIn(self.profile.entry_url, seed_response.text)
                    self.assertTrue((self.root / self.checksum / 'entry.json').is_file())
                    self.assertFalse((self.root / 'seed-registered').exists())
                    register_selection = {**seed_selection,
                                          'confirm_sha256': seed_response.json()['draft_sha256']}
                    self.assertEqual((await client.post('/api/tasks/page-draft-register',
                        json={**register_selection, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertEqual((await client.post('/api/tasks/page-draft-register',
                        json={**register_selection, 'confirm_sha256': True})).status_code, 404)
                    registration_response = await client.post('/api/tasks/page-draft-register',
                                                              json=register_selection)
                    self.assertEqual(registration_response.status_code, 200)
                    self.assertEqual(registration_response.json()['status'],
                                     'registered_unreviewed_draft')
                    self.assertEqual(registration_response.json()['knowledge_sha256'],
                                     register_selection['confirm_sha256'])
                    self.assertFalse(registration_response.json()['reviewed'])
                    self.assertEqual(SiteKnowledgeStore(
                        self.root / 'seed-registered', self.profiles).get(
                            register_selection['confirm_sha256']).page_key, 'entry')
                    self.assertEqual((await client.post('/api/tasks/page-draft-register',
                        json=register_selection)).status_code, 409)
                    knowledge_selection = {**seed_selection,
                        'knowledge_sha256': register_selection['confirm_sha256']}
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-preview',
                        json={**knowledge_selection, 'knowledge_sha256': '0' * 64})).status_code, 409)
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-preview',
                        json={**knowledge_selection, 'page_key': 'wrong'})).status_code, 409)
                    candidate_response = await client.post('/api/tasks/page-knowledge-preview',
                                                           json=knowledge_selection)
                    self.assertEqual(candidate_response.status_code, 200)
                    self.assertEqual(candidate_response.json()['candidate_sha256'],
                                     digest(candidate_response.json()['candidate']))
                    self.assertFalse(candidate_response.json()['candidate']['reviewed'])
                    self.assertNotIn(self.profile.entry_url, candidate_response.text)
                    review_selection = {**knowledge_selection,
                        'confirm_candidate_sha256': candidate_response.json()['candidate_sha256'],
                        'acknowledge_metadata_only': True}
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-review',
                        json={**review_selection, 'acknowledge_metadata_only': False})).status_code, 404)
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-review',
                        json={**review_selection, 'confirm_candidate_sha256': '0' * 64})).status_code, 409)
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-review',
                        json={**review_selection, 'after_run_id': 'missing'})).status_code, 404)
                    self.assertEqual((await client.post('/api/tasks/page-knowledge-review?ignore=1',
                        json=review_selection)).status_code, 400)
                    review_response = await client.post('/api/tasks/page-knowledge-review',
                                                        json=review_selection)
                    self.assertEqual(review_response.status_code, 200)
                    self.assertEqual(review_response.json()['status'], 'metadata_review_recorded')
                    self.assertFalse(review_response.json()['task_retrieval_authorized'])
                    self.assertEqual(RemoteRouteKnowledgeReviewStore(
                        self.root / 'seed-reviews').get(review_response.json()['review_sha256']).knowledge_sha256,
                        register_selection['confirm_sha256'])
                    self.assertIsNone(scheduler.status()['remote_routes']['review_sha256'])
                    detail_seed = await client.post('/api/tasks/page-draft-seed',
                        json={**seed_selection, 'route_index': 1, 'page_key': 'details'})
                    self.assertEqual(detail_seed.status_code, 200)
                    detail_file = self.root / self.checksum / 'details.json'
                    detail_confirmation = {**seed_selection, 'route_index': 1,
                                           'page_key': 'details',
                                           'confirm_sha256': detail_seed.json()['draft_sha256']}
                    detail_file.chmod(0o644)
                    self.assertEqual((await client.post('/api/tasks/page-draft-register',
                        json=detail_confirmation)).status_code, 409)
                    detail_file.chmod(0o600)
                    altered = SitePageDraft.model_validate_json(detail_file.read_bytes())
                    altered = SitePageDraft.model_validate({**altered.model_dump(),
                        'page_fingerprint_sha256': '0' * 64})
                    detail_file.write_bytes(canonical(altered.model_dump()).encode())
                    self.assertEqual((await client.post('/api/tasks/page-draft-register',
                        json={**detail_confirmation,
                              'confirm_sha256': digest(altered.model_dump())})).status_code, 409)
                    self.assertEqual(SiteKnowledgeStore(
                        self.root / 'seed-registered', self.profiles).list(
                            profile_sha256=self.checksum, page_key='details'), [])
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        json=seed_selection)).status_code, 409)
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        json={**seed_selection, 'route_index': True})).status_code, 404)
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        json={**seed_selection, 'before_run_id': 'missing'})).status_code, 404)
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        json={**seed_selection, 'page_key': '../outside'})).status_code, 404)
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        json={**seed_selection, 'before_run_id': job['run_id'],
                              'after_run_id': second_run_id, 'route_index': 1,
                              'page_key': 'changed'})).status_code, 409)
                    self.assertEqual((await client.post('/api/tasks/page-draft-seed',
                        content=('{' + ','.join([
                            '"before_run_id":"' + job['run_id'] + '"',
                            '"after_run_id":"' + graph_run_id + '"',
                            '"route_index":0', '"page_key":"entry"',
                            '"page_key":"duplicate"']) + '}'),
                        headers={'Content-Type': 'application/json'})).status_code, 400)
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph?before_run_id=ignored',
                        json=selection)).status_code, 400)
                    self.assertEqual((await client.post(
                        '/api/tasks/navigation-graph',
                        content=('{"before_run_id":"' + job['run_id']
                                 + '","before_run_id":"' + job['run_id']
                                 + '","after_run_id":"' + graph_run_id + '"}'),
                        headers={'Content-Type': 'application/json'})).status_code, 400)
                    started = threading.Event()
                    release = threading.Event()

                    def held_graph(*_arguments, **_keywords):
                        started.set()
                        if not release.wait(10):
                            raise RuntimeError('Graph audit did not release')
                        return graph

                    with patch('aos.desktop_console.preview_remote_navigation_graph',
                               side_effect=held_graph):
                        first_request = asyncio.create_task(client.post(
                            '/api/tasks/navigation-graph', json=selection))
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 10))
                            self.assertEqual((await client.post(
                                '/api/tasks/navigation-graph', json=selection)).status_code, 429)
                            self.assertEqual((await client.post(
                                '/api/tasks/page-draft-seed', json={**seed_selection,
                                    'page_key': 'contended'})).status_code, 429)
                            self.assertEqual((await client.post(
                                '/api/tasks/page-knowledge-preview',
                                json=knowledge_selection)).status_code, 429)
                            self.assertEqual((await client.post(
                                '/api/tasks/page-knowledge-review',
                                json=review_selection)).status_code, 429)
                            self.assertFalse((self.root / self.checksum / 'contended.json').exists())
                        finally:
                            release.set()
                        self.assertEqual((await first_request).json(), graph)
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a></html>')
                self.server.route_bodies['/details'] = (
                    b'<html><title>Relay details changed</title><h1>Synthetic details</h1></html>')
                page_store = SiteKnowledgeStore(self.root / 'site-knowledge', self.profiles)
                page = SitePageDraft.model_validate({
                    'schema_version': '1.0', 'profile_sha256': self.checksum,
                    'application_key': self.profile.application_key,
                    'tenant_key': self.profile.tenant_key,
                    'account_role': self.profile.account_role,
                    'page_key': 'details', 'revision': 1, 'previous_sha256': None,
                    'origin': self.profile.allowed_origins[0], 'route_template': '/details',
                    'page_fingerprint_sha256': unchanged['routes'][1]['after_fingerprint_sha256'],
                    'recorded_at': '2026-09-23T00:00:00Z',
                    'landmark_keys': [], 'outgoing_page_keys': [],
                    'source_kind': 'manual_draft', 'status': 'draft',
                    'execution_authorized': False, 'collection_authorized': False,
                    'training_ready': False})
                page_sha256 = digest(page.model_dump())
                page_store.register(page, confirm_sha256=page_sha256)
                preview_arguments = {'profiles': self.profiles.root, 'store': page_store.root,
                                     'knowledge_sha256': page_sha256,
                                     'selected_profile_sha256': self.checksum,
                                     'selected_plan_sha256': digest(plan.model_dump()),
                                     'route_index': 1}
                preview = preview_remote_route_knowledge(
                    self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                    **preview_arguments)
                self.assertEqual(preview['status'], 'candidate_remote_profile_bound')
                self.assertTrue(preview['profile_bound'])
                self.assertTrue(preview['route_readback_bound'])
                self.assertFalse(preview['task_retrieval_authorized'])
                self.assertNotIn(detail_url, canonical(preview))
                preview_cli_arguments = [
                    '--database', str(self.root / 'routes-trajectory.sqlite'),
                    '--before-run-id', second_run_id, '--after-run-id', third_run_id,
                    '--profiles', str(self.profiles.root), '--store', str(page_store.root),
                    '--knowledge-sha256', page_sha256,
                    '--selected-profile-sha256', self.checksum,
                    '--selected-plan-sha256', digest(plan.model_dump()), '--route-index', '1']
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    remote_route_knowledge_main(preview_cli_arguments)
                self.assertEqual(json.loads(output.getvalue()), preview)
                review_root = self.root / 'route-reviews'
                with self.assertRaisesRegex(ValueError, 'explicit_acknowledgement'):
                    register_remote_route_knowledge_review(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        profiles=self.profiles.root, site_store=page_store.root,
                        review_store=review_root, knowledge_sha256=page_sha256,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()), route_index=1,
                        confirm_candidate_sha256=digest(preview),
                        acknowledge_metadata_only=False)
                with self.assertRaisesRegex(ValueError, 'exact_candidate_hash'):
                    register_remote_route_knowledge_review(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        profiles=self.profiles.root, site_store=page_store.root,
                        review_store=review_root, knowledge_sha256=page_sha256,
                        selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()), route_index=1,
                        confirm_candidate_sha256='0' * 64,
                        acknowledge_metadata_only=True)
                self.assertFalse(review_root.exists())
                receipt = register_remote_route_knowledge_review(
                    self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                    profiles=self.profiles.root, site_store=page_store.root,
                    review_store=review_root, knowledge_sha256=page_sha256,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()), route_index=1,
                    confirm_candidate_sha256=digest(preview),
                    acknowledge_metadata_only=True)
                review_sha256 = receipt['review_sha256']
                self.assertFalse(receipt['task_retrieval_authorized'])
                review_output = io.StringIO()
                with contextlib.redirect_stdout(review_output):
                    remote_route_knowledge_review_main([
                        'register', '--database', str(self.root / 'routes-trajectory.sqlite'),
                        '--before-run-id', second_run_id, '--after-run-id', third_run_id,
                        '--profiles', str(self.profiles.root),
                        '--site-store', str(page_store.root),
                        '--review-store', str(review_root),
                        '--knowledge-sha256', page_sha256,
                        '--selected-profile-sha256', self.checksum,
                        '--selected-plan-sha256', digest(plan.model_dump()),
                        '--route-index', '1',
                        '--confirm-candidate-sha256', digest(preview),
                        '--acknowledge-metadata-only'])
                self.assertEqual(json.loads(review_output.getvalue())['status'],
                                 'metadata_review_recorded')
                self.assertNotIn(detail_url, review_output.getvalue())
                review_file = review_root / (review_sha256 + '.json')
                self.assertEqual(review_file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(detail_url, review_file.read_text())
                entry_page = page.model_copy(update={
                    'page_key': 'entry', 'route_template': '/entry',
                    'page_fingerprint_sha256': unchanged['routes'][0]['after_fingerprint_sha256'],
                    'landmark_keys': ['entry_heading'], 'outgoing_page_keys': ['details']})
                entry_page_sha256 = digest(entry_page.model_dump())
                page_store.register(entry_page, confirm_sha256=entry_page_sha256)
                unlinked_page = entry_page.model_copy(update={
                    'page_key': 'unlinked', 'outgoing_page_keys': []})
                unlinked_sha256 = digest(unlinked_page.model_dump())
                page_store.register(unlinked_page, confirm_sha256=unlinked_sha256)
                orphan_page = entry_page.model_copy(update={
                    'page_key': 'orphan', 'outgoing_page_keys': ['unlinked']})
                orphan_sha256 = digest(orphan_page.model_dump())
                page_store.register(orphan_page, confirm_sha256=orphan_sha256)
                entry_preview_arguments = {**preview_arguments,
                                           'knowledge_sha256': entry_page_sha256,
                                           'route_index': 0}
                with self.assertRaisesRegex(ValueError, 'outgoing_links_unbound'):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        **entry_preview_arguments)
                with self.assertRaisesRegex(ValueError, 'outgoing_key_unbound'):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                        **{**entry_preview_arguments, 'knowledge_sha256': orphan_sha256})
                with patch('aos.remote_route_knowledge.audit_snapshot', wraps=audit_snapshot) as audited:
                    entry_candidate = preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                        **entry_preview_arguments)
                self.assertEqual(audited.call_count, 1)
                self.assertEqual(entry_candidate['link_sample_sha256'],
                                 change['routes'][0]['link_sample_sha256'])
                self.assertEqual(entry_candidate['outgoing_route_indices'], [1])
                self.assertEqual(entry_candidate['outgoing_knowledge_sha256'], [page_sha256])
                entry_receipt = register_remote_route_knowledge_review(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                    profiles=self.profiles.root, site_store=page_store.root,
                    review_store=review_root, knowledge_sha256=entry_page_sha256,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()), route_index=0,
                    confirm_candidate_sha256=digest(entry_candidate),
                    acknowledge_metadata_only=True)
                with patch('aos.local_app.WEB_PROFILES', self.profiles.root):
                    managed_pin = local_app.prepare_managed_remote_route_review(
                        'real', self.checksum, digest(plan.model_dump()),
                        self.root / 'routes-trajectory.sqlite', page_store.root,
                        review_root, entry_receipt['review_sha256'])
                self.assertEqual(managed_pin[3], entry_receipt['review_sha256'])
                task_file = self.root / 'reviewed-task.json'
                task_file.write_text(canonical(self.draft.task.model_dump(mode='json')))
                task_file.chmod(0o600)
                plan_file = self.root / 'reviewed-routes.json'
                plan_file.write_text(canonical(plan.model_dump(mode='json')))
                plan_file.chmod(0o600)
                rejected = subprocess.run([
                    local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                    '--port', '18765', '--engine', 'fixture', '--browser-tasks', '--desktop-browser',
                    '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                    '--web-profiles-root', str(self.profiles.root),
                    '--remote-entry-profile-sha256', self.checksum,
                    '--remote-entry-task-file', str(task_file),
                    '--remote-entry-task-sha256', digest(self.draft.task.model_dump()),
                    '--remote-routes-plan-file', str(plan_file),
                    '--remote-routes-plan-sha256', digest(plan.model_dump()),
                    '--remote-route-review-source-database', str(managed_pin[0]),
                    '--remote-route-review-site-store', str(managed_pin[1]),
                    '--remote-route-review-store', str(managed_pin[2]),
                    '--remote-route-review-sha256', managed_pin[3],
                    '--remote-route-review-source-snapshot-sha256', '0' * 64],
                    capture_output=True, timeout=20)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Read-only route review source is unavailable or stale',
                              rejected.stderr)
                from test_desktop_task_ui import task_server

                reviewed_console = self.root / 'reviewed-console'
                reviewed_console.mkdir(mode=0o700)
                reviewed_arguments = (
                    '--browser-tasks', '--desktop-browser',
                    '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                    '--web-profiles-root', str(self.profiles.root),
                    '--remote-entry-profile-sha256', self.checksum,
                    '--remote-entry-task-file', str(task_file),
                    '--remote-entry-task-sha256', digest(self.draft.task.model_dump()),
                    '--remote-routes-plan-file', str(plan_file),
                    '--remote-routes-plan-sha256', digest(plan.model_dump()),
                    '--remote-route-review-source-database', str(managed_pin[0]),
                    '--remote-route-review-site-store', str(managed_pin[1]),
                    '--remote-route-review-store', str(managed_pin[2]),
                    '--remote-route-review-sha256', managed_pin[3],
                    '--remote-route-review-source-snapshot-sha256', managed_pin[4])
                with task_server(reviewed_console, 'fixture', reviewed_arguments) as (
                        _origin, _token, client, _server):
                    advertised = client.get('/api/tasks').json()['remote_routes']
                    self.assertEqual(advertised['plan_sha256'], digest(plan.model_dump()))
                    self.assertEqual(advertised['review_sha256'], entry_receipt['review_sha256'])
                scheduler.release_completed_runtime()
                reviewed_options = dict(
                    browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                    desktop_browser=True,
                    remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                    remote_entry_profiles=self.profiles,
                    remote_entry_profile_sha256=self.checksum,
                    remote_entry_task=self.draft.task, remote_routes_plan=plan,
                    remote_route_review_source_database=self.root / 'routes-trajectory.sqlite',
                    remote_route_review_site_store=page_store.root,
                    remote_route_review_store=review_root,
                    remote_route_review_sha256=entry_receipt['review_sha256'])
                with self.assertRaises(ValueError):
                    DesktopScheduler(controller, scheduler.settings, FixtureDecisionEngine(),
                                     **{**reviewed_options,
                                        'remote_route_review_sha256': '0' * 64})
                reviewed_scheduler = DesktopScheduler(
                    controller, scheduler.settings, FixtureDecisionEngine(),
                    **reviewed_options)
                self.assertEqual(reviewed_scheduler.status()['remote_routes']['review_sha256'],
                                 entry_receipt['review_sha256'])
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a><a href="/outside">Outside</a></html>')

                async def next_review_approval():
                    for _attempt in range(1500):
                        approval = reviewed_scheduler.status()['approval']
                        if approval is not None or reviewed_scheduler.task.done():
                            return approval
                        await asyncio.sleep(0.02)
                    self.fail('Reviewed route approval did not arrive')

                fourth_job_id = reviewed_scheduler.start(owner['lease_id'], owner['generation'],
                                                         'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_review_approval()
                    self.assertIsNotNone(next_action)
                    reviewed_scheduler.respond(next_action['approval_id'],
                                               next_action['action_sha256'], True)
                await reviewed_scheduler.task
                fourth_run_id = store.connection.execute(
                    "SELECT run_id FROM desktop_tasks WHERE job_id=? AND status='succeeded'",
                    (fourth_job_id,)).fetchone()[0]
                live_events = [json.loads(row[0]) for row in store.connection.execute(
                    "SELECT payload_json FROM observations WHERE run_id=? AND kind='browser.remote_route_knowledge'",
                    (fourth_run_id,)).fetchall()]
                self.assertEqual([event['status'] for event in live_events], ['matched'])
                self.assertEqual(reviewed_scheduler.status()['jobs'][0]['route_knowledge'], {
                    'status': 'matched', 'route_index': 0, 'stale_reason': None})
                self.assertTrue(live_events[0]['link_sample_matched'])
                snapshots = [json.loads(row[0]) for row in store.connection.execute(
                    'SELECT state_json FROM state_snapshots WHERE run_id=?',
                    (fourth_run_id,)).fetchall()]
                self.assertFalse(any('Live-fingerprint-matched historical symbolic metadata:' in
                                     snapshot['observation'] for snapshot in snapshots))
                self.assertEqual(store.connection.execute(
                    "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                    (fourth_job_id,)).fetchone()[0], 2)
                reuse_arguments = dict(
                    profiles=self.profiles.root, site_store=page_store.root,
                    review_store=review_root, review_sha256=review_sha256,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
                reuse = retrieve_remote_route_knowledge(
                    self.root / 'routes-trajectory.sqlite', fourth_run_id,
                    **reuse_arguments)
                self.assertEqual(reuse['status'], 'reviewed_metadata_reused')
                self.assertEqual(reuse['page_key'], 'details')
                self.assertFalse(reuse['task_retrieval_authorized'])
                self.assertNotIn(detail_url, canonical(reuse))
                entry_reuse_arguments = {**reuse_arguments,
                    'review_sha256': entry_receipt['review_sha256']}
                entry_reuse = retrieve_remote_route_knowledge(
                    self.root / 'routes-trajectory.sqlite', fourth_run_id,
                    **entry_reuse_arguments)
                self.assertEqual(entry_reuse['link_sample_sha256'],
                                 entry_candidate['link_sample_sha256'])
                later_review = RemoteRouteKnowledgeReviewStore(review_root).get(
                    review_sha256).model_copy(update={
                        'recorded_at': datetime.now(timezone.utc).strftime(
                            '%Y-%m-%dT%H:%M:%S.%fZ')})
                later_review_sha256 = RemoteRouteKnowledgeReviewStore(
                    review_root).register(later_review)
                with self.assertRaisesRegex(ValueError, 'run_precedes_review'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **{**reuse_arguments, 'review_sha256': later_review_sha256})
                fourth_started_at = store.connection.execute(
                    'SELECT started_at FROM runs WHERE run_id=?',
                    (fourth_run_id,)).fetchone()[0]
                legacy_review = later_review.model_copy(update={
                    'recorded_at': datetime.fromisoformat(fourth_started_at).strftime(
                        '%Y-%m-%dT%H:%M:%SZ')})
                legacy_review_sha256 = RemoteRouteKnowledgeReviewStore(
                    review_root).register(legacy_review)
                with self.assertRaisesRegex(ValueError, 'run_precedes_review'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **{**reuse_arguments, 'review_sha256': legacy_review_sha256})
                with self.assertRaises(ValueError):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', third_run_id,
                        **reuse_arguments)
                with self.assertRaises(ValueError):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **{**reuse_arguments, 'selected_profile_sha256': '0' * 64})
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    remote_route_knowledge_review_main([
                        'retrieve', '--database', str(self.root / 'routes-trajectory.sqlite'),
                        '--current-run-id', fourth_run_id,
                        '--profiles', str(self.profiles.root),
                        '--site-store', str(page_store.root),
                        '--review-store', str(review_root),
                        '--review-sha256', review_sha256,
                        '--selected-profile-sha256', self.checksum,
                        '--selected-plan-sha256', digest(plan.model_dump())])
                self.assertEqual(json.loads(output.getvalue()), reuse)
                self.server.route_bodies['/details'] = (
                    b'<html><title>Relay details changed again</title>'
                    b'<h1>Synthetic details</h1></html>')
                target_changed_job_id = reviewed_scheduler.start(
                    owner['lease_id'], owner['generation'],
                    'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_review_approval()
                    reviewed_scheduler.respond(next_action['approval_id'],
                                               next_action['action_sha256'], True)
                await reviewed_scheduler.task
                target_changed_run_id = store.connection.execute(
                    "SELECT run_id FROM desktop_tasks WHERE job_id=? AND status='succeeded'",
                    (target_changed_job_id,)).fetchone()[0]
                target_events = [json.loads(row[0]) for row in store.connection.execute(
                    "SELECT payload_json FROM observations WHERE run_id=? "
                    "AND kind='browser.remote_route_knowledge' ORDER BY rowid",
                    (target_changed_run_id,)).fetchall()]
                self.assertEqual([event['status'] for event in target_events],
                                 ['matched', 'stale'])
                self.assertEqual(target_events[1]['route_index'], 1)
                self.assertEqual(target_events[1]['stale_reason'],
                                 'target_fingerprint_changed')
                self.assertEqual(reviewed_scheduler.status()['jobs'][0]['route_knowledge'], {
                    'status': 'stale', 'route_index': 1,
                    'stale_reason': 'target_fingerprint_changed'})
                self.assertNotEqual(target_events[1]['expected_fingerprint_sha256'],
                                    target_events[1]['current_fingerprint_sha256'])
                target_snapshots = [json.loads(row[0]) for row in store.connection.execute(
                    'SELECT state_json FROM state_snapshots WHERE run_id=?',
                    (target_changed_run_id,)).fetchall()]
                self.assertFalse(any('Live-fingerprint-matched historical symbolic metadata:' in
                                     snapshot['observation'] for snapshot in target_snapshots))
                with self.assertRaisesRegex(ValueError, 'current_target_stale'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', target_changed_run_id,
                        **entry_reuse_arguments)
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a></html>')
                changed_job_id = reviewed_scheduler.start(owner['lease_id'], owner['generation'],
                                                          'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    next_action = await next_review_approval()
                    reviewed_scheduler.respond(next_action['approval_id'],
                                               next_action['action_sha256'], True)
                await reviewed_scheduler.task
                changed_run_id = store.connection.execute(
                    "SELECT run_id FROM desktop_tasks WHERE job_id=? AND status='succeeded'",
                    (changed_job_id,)).fetchone()[0]
                stale_events = [json.loads(row[0]) for row in store.connection.execute(
                    "SELECT payload_json FROM observations WHERE run_id=? AND kind='browser.remote_route_knowledge'",
                    (changed_run_id,)).fetchall()]
                self.assertEqual([event['status'] for event in stale_events], ['stale'])
                self.assertEqual(stale_events[0]['stale_reason'],
                                 'source_link_sample_changed')
                self.assertFalse(stale_events[0]['link_sample_matched'])
                self.assertEqual(stale_events[0]['expected_fingerprint_sha256'],
                                 stale_events[0]['current_fingerprint_sha256'])
                with self.assertRaisesRegex(ValueError, 'current_route_stale'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', changed_run_id,
                        **entry_reuse_arguments)
                changed_snapshots = [json.loads(row[0]) for row in store.connection.execute(
                    'SELECT state_json FROM state_snapshots WHERE run_id=?',
                    (changed_run_id,)).fetchall()]
                self.assertFalse(any('Live-fingerprint-matched historical symbolic metadata:' in
                                     snapshot['observation'] for snapshot in changed_snapshots))
                reviewed_scheduler.release_completed_runtime()
                await reviewed_scheduler.close()
                self.server.route_bodies['/entry'] = (
                    b'<html><title>Relay fixture</title><h1>Synthetic entry</h1>'
                    b'<a href="/details">Details</a><a href="/outside">Outside</a></html>')
                self.server.route_bodies['/details'] = (
                    b'<html><title>Relay details changed</title>'
                    b'<h1>Synthetic details</h1></html>')
                new_database = self.root / 'reviewed-new-session.sqlite'
                new_store = TrajectoryStore(new_database)
                self.addCleanup(new_store.close)
                new_controller = DesktopController(new_store, desktop)
                new_scheduler = DesktopScheduler(
                    new_controller,
                    Settings(workspace=self.workspace, database=new_database),
                    FixtureDecisionEngine(), **reviewed_options)
                self.assertNotEqual(new_controller.session_id, controller.session_id)
                self.assertNotEqual(new_scheduler.settings.database, scheduler.settings.database)
                new_owner = new_controller.state()
                new_job_id = new_scheduler.start(
                    new_owner['lease_id'], new_owner['generation'],
                    'browser_remote_routes')['job_id']
                for _route_index in range(2):
                    for _attempt in range(1500):
                        new_approval = new_scheduler.status()['approval']
                        if new_approval is not None or new_scheduler.task.done():
                            break
                        await asyncio.sleep(0.02)
                    self.assertIsNotNone(new_approval)
                    new_scheduler.respond(new_approval['approval_id'],
                                          new_approval['action_sha256'], True)
                await new_scheduler.task
                new_run_id = new_store.connection.execute(
                    "SELECT run_id FROM desktop_tasks WHERE job_id=? AND status='succeeded'",
                    (new_job_id,)).fetchone()[0]
                new_events = [json.loads(row[0]) for row in new_store.connection.execute(
                    "SELECT payload_json FROM observations WHERE run_id=? AND kind='browser.remote_route_knowledge'",
                    (new_run_id,)).fetchall()]
                self.assertEqual([event['status'] for event in new_events], ['matched'])
                new_snapshots = [json.loads(row[0]) for row in new_store.connection.execute(
                    'SELECT state_json FROM state_snapshots WHERE run_id=?',
                    (new_run_id,)).fetchall()]
                self.assertFalse(any('Live-fingerprint-matched historical symbolic metadata:' in
                                     snapshot['observation'] for snapshot in new_snapshots))
                self.assertEqual(new_store.connection.execute(
                    "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                    (new_job_id,)).fetchone()[0], 2)
                await new_scheduler.close()
                with self.assertRaisesRegex(ValueError, 'current_route_stale'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', changed_run_id,
                        **reuse_arguments)
                with self.assertRaises(ValueError):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                        **preview_arguments)
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as failure:
                        remote_route_knowledge_main([
                            *preview_cli_arguments, '--before-run-id', job['run_id'],
                            '--after-run-id', second_run_id])
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(output.getvalue(), '')
                self.assertNotIn(str(self.root), errors.getvalue())
                with self.assertRaises(ValueError):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        **{**preview_arguments, 'route_index': 0})
                successor = page.model_copy(update={'revision': 2,
                                                    'previous_sha256': page_sha256})
                successor_sha256 = digest(successor.model_dump())
                page_store.register(successor, confirm_sha256=successor_sha256)
                with self.assertRaises(ValueError):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **reuse_arguments)
                with self.assertRaises(ValueError):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        **preview_arguments)
                self.assertEqual(preview_remote_route_knowledge(
                    self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                    **{**preview_arguments, 'knowledge_sha256': successor_sha256})['draft_revision'], 2)
                updated_entry_candidate = preview_remote_route_knowledge(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                    **entry_preview_arguments)
                self.assertEqual(updated_entry_candidate['outgoing_route_indices'], [1])
                self.assertEqual(updated_entry_candidate['outgoing_knowledge_sha256'],
                                 [successor_sha256])
                self.assertNotEqual(digest(updated_entry_candidate), digest(entry_candidate))
                with self.assertRaisesRegex(ValueError, 'source_changed'):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **entry_reuse_arguments)
                next_profile = self.profile.model_copy(update={
                    'revision': 2, 'previous_sha256': self.checksum})
                next_profile_sha256 = profile_report(next_profile).profile_sha256
                self.profiles.register(next_profile, confirm_sha256=next_profile_sha256)
                with self.assertRaises(ValueError):
                    retrieve_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', fourth_run_id,
                        **reuse_arguments)
                with self.assertRaises(ValueError):
                    preview_remote_route_knowledge(
                        self.root / 'routes-trajectory.sqlite', second_run_id, third_run_id,
                        **{**preview_arguments, 'knowledge_sha256': successor_sha256})
            await scheduler.close()
            evidence = review_remote_route_evidence(
                self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            learning_source = inspect_remote_learning_source(
                self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                selected_profile_sha256=self.checksum,
                selected_plan_sha256=digest(plan.model_dump()))
            self.assertEqual(len(learning_source['source_event_ids_by_role']['system1']),
                             2 if real_model else 0)
            self.assertEqual(learning_source['source_event_ids_by_role']['system2'], [])
            self.assertEqual(learning_source['requested_roles'], ['system1', 'system2'])
            self.assertFalse(learning_source['collection_authorized'])
            self.assertNotIn(self.profile.entry_url, canonical(learning_source))
            cli = subprocess.run([
                sys.executable, '-m', 'aos.remote_learning_source',
                '--database', str(self.root / 'routes-trajectory.sqlite'),
                '--run-id', job['run_id'], '--profiles', str(self.profiles.root),
                '--selected-profile-sha256', self.checksum,
                '--selected-plan-sha256', digest(plan.model_dump())],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout), learning_source)
            self.assertEqual(evidence['route_count'], 2)
            self.assertEqual(evidence['routes'][0]['planned_link_indices'], [1])
            self.assertEqual(evidence['routes'][0]['unregistered_link_count'], 1)
            self.assertTrue(evidence['routes'][0]['link_inventory_readback_matched'])
            self.assertEqual(evidence['routes'][1]['planned_link_indices'], [])
            self.assertNotIn('/outside', '\n'.join(store.connection.iterdump()))
            self.assertNotEqual(evidence['routes'][0]['page_fingerprint_sha256'],
                                evidence['routes'][1]['page_fingerprint_sha256'])
            self.assertNotIn(self.profile.entry_url, canonical(evidence))
            self.assertFalse(evidence['task_retrieval_authorized'])
            with self.assertRaises(ValueError):
                review_remote_route_evidence(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum, selected_plan_sha256='0' * 64)
            with self.assertRaises(ValueError):
                inspect_remote_learning_source(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum, selected_plan_sha256='0' * 64)
            with store.connection:
                store.connection.execute("UPDATE desktop_approvals SET status='rejected' WHERE job_id=?",
                                         (job_id,))
            with self.assertRaises(ValueError):
                review_remote_route_evidence(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
            with self.assertRaises(ValueError):
                inspect_remote_learning_source(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))
            if not fail_remote_learning:
                with self.assertRaises(ValueError):
                    poll_remote_learning_stream(
                        self.root / 'routes-trajectory.sqlite', **stream_selection)
            if second_run_id is not None:
                with self.assertRaises(ValueError):
                    compare_remote_route_change(
                        self.root / 'routes-trajectory.sqlite', job['run_id'], second_run_id,
                        profiles=self.profiles.root, selected_profile_sha256=self.checksum,
                        selected_plan_sha256=digest(plan.model_dump()))
            with store.connection:
                store.connection.execute("UPDATE desktop_approvals SET status='consumed' WHERE job_id=?",
                                         (job_id,))
            with store.connection:
                store.connection.execute('''UPDATE observations SET payload_json='{}'
                    WHERE run_id=? AND action_id IN (SELECT action_id FROM actions
                    WHERE run_id=? AND tool='browser.remote.observe')''',
                    (job['run_id'], job['run_id']))
            with self.assertRaises(ValueError):
                review_remote_route_evidence(
                    self.root / 'routes-trajectory.sqlite', job['run_id'], profiles=self.profiles.root,
                    selected_profile_sha256=self.checksum,
                    selected_plan_sha256=digest(plan.model_dump()))

        def local_connection(address, timeout=None, **kwargs):
            if address[0] == '127.0.0.1':
                return self.original_connection(address, timeout=timeout, **kwargs)
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu route rejection test')
    def test_scheduler_second_route_rejection_stops_without_replay(self):
        detail_url = self.profile.entry_url.replace('/entry', '/details')
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, detail_url])
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        store = TrajectoryStore(self.root / 'routes-rejected.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=self.root / 'routes-rejected.sqlite'),
            FixtureDecisionEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task, remote_routes_plan=plan)

        async def wait_approval(previous=None):
            for _attempt in range(500):
                approval = scheduler.status()['approval']
                if approval is not None and approval['approval_id'] != previous:
                    return approval
                if scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.fail('Expected a fresh route approval')

        async def scenario():
            owner = controller.state()
            job_id = scheduler.start(owner['lease_id'], owner['generation'],
                                     'browser_remote_routes')['job_id']
            first = await wait_approval()
            scheduler.respond(first['approval_id'], first['action_sha256'], True)
            second = await wait_approval(first['approval_id'])
            self.assertEqual(self.server.paths, ['/entry'])
            scheduler.respond(second['approval_id'], second['action_sha256'], False)
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.task
            self.assertEqual(self.server.paths, ['/entry'])
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0],
                'cancelled')
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='consumed'",
                (job_id,)).fetchone()[0], 1)
            self.assertFalse((self.workspace / 'relay.sock').exists())
            await scheduler.close()

        def local_connection(_address, timeout=None):
            return self.original_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote-entry pause test')
    def test_scheduler_pause_before_approval_cancels_without_fetch(self):
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        store = TrajectoryStore(self.root / 'trajectory.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=self.root / 'trajectory.sqlite'),
            FixtureDecisionEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task)

        async def scenario():
            owner = controller.state()
            job_id = scheduler.start(owner['lease_id'], owner['generation'], 'browser_remote_entry')['job_id']
            for _attempt in range(500):
                if scheduler.status()['approval'] is not None or scheduler.task.done():
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(scheduler.status()['approval'])
            await scheduler.pause()
            self.assertEqual(store.connection.execute(
                'SELECT status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()[0], 'cancelled')
            self.assertIsNone(scheduler.status()['approval'])
            self.assertEqual(self.server.paths, [])
            self.assertFalse((self.workspace / 'relay.sock').exists())
            await scheduler.close()

        asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu remote-entry confidence-gate test')
    def test_scheduler_low_confidence_never_opens_relay(self):
        class LowConfidenceEngine(FixtureDecisionEngine):
            async def decide(self, state, options):
                return Prediction(selected_option='open_entry',
                                  probabilities={'open_entry': 0.879, 'ask_human': 0.121})

        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        store = TrajectoryStore(self.root / 'trajectory.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, desktop)
        scheduler = DesktopScheduler(
            controller, Settings(workspace=self.workspace, database=self.root / 'trajectory.sqlite'),
            LowConfidenceEngine(), browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
            desktop_browser=True,
            remote_entry_mcp_manifest=REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
            remote_entry_profiles=self.profiles, remote_entry_profile_sha256=self.checksum,
            remote_entry_task=self.draft.task)

        async def scenario():
            owner = controller.state()
            job_id = scheduler.start(owner['lease_id'], owner['generation'], 'browser_remote_entry')['job_id']
            await scheduler.task
            job = store.connection.execute('SELECT run_id,status FROM desktop_tasks WHERE job_id=?',
                                           (job_id,)).fetchone()
            decision = store.connection.execute('SELECT confidence,policy_result FROM decisions WHERE run_id=?',
                                                (job['run_id'],)).fetchone()
            self.assertEqual(job['status'], 'waiting_human')
            self.assertEqual(decision['policy_result'], 'escalate')
            self.assertLess(decision['confidence'], scheduler.settings.execute_min)
            self.assertIsNone(scheduler.status()['approval'])
            self.assertEqual(self.server.paths, [])
            self.assertFalse((self.workspace / 'relay.sock').exists())
            await scheduler.close()

        asyncio.run(scenario())

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS relay MCP test')
    def test_pinned_mcp_network_none_remote_entry_gate(self):
        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        gate = self.workspace / 'remote_gate.cjs'
        gate.write_text(worker_namespace()['remote_entry_page_gate_source'](
            self.profile.entry_url, self.draft.binding_sha256, self.relay().client_token))
        gate.chmod(0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const {spawn} = require('node:child_process');
const cli = '/workspace/node_modules/@playwright/mcp/cli.js';
const child = spawn('/usr/local/bin/node', [cli, '--isolated', '--executable-path',
  '/opt/chromium/chrome', '--no-sandbox', '--block-service-workers', '--no-webmcp',
  '--codegen', 'none', '--snapshot-mode', 'full', '--output-dir', '/home/agent/mcp-output',
  '--timeout-action', '3000', '--timeout-navigation', '5000', '--timeout-settle', '0',
  '--init-page', '/workspace/remote_gate.cjs'],
  {env: {PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/home/agent', DISPLAY: ':99',
         LANG: 'C.UTF-8', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1'}});
let counter = 0;
let buffer = '';
const pending = new Map();
child.stdout.on('data', chunk => {
  buffer += chunk;
  for (;;) {
    const newline = buffer.indexOf('\\n');
    if (newline < 0) break;
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const reply = JSON.parse(line);
    if (pending.has(reply.id)) {pending.get(reply.id)(reply); pending.delete(reply.id);}
  }
});
function request(method, params) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('timeout'));}, 8000);
    pending.set(id, reply => {clearTimeout(timer); resolve(reply);});
    child.stdin.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\\n');
  });
}
(async () => {
  try {
    const initialized = await request('initialize', {protocolVersion: '2024-11-05',
      capabilities: {}, clientInfo: {name: 'aos-relay-test', version: '1'}});
    if (initialized.result?.serverInfo?.version !== process.argv[2]) throw new Error('version');
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\\n');
    const tools = await request('tools/list', {});
    if (!tools.result.tools.some(tool => tool.name === 'browser_navigate')) throw new Error('tools');
    const opened = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: process.argv[1]}});
    if (opened.result?.isError) throw new Error('navigate');
    const snapshot = await request('tools/call', {name: 'browser_snapshot', arguments: {}});
    const content = snapshot.result?.content?.map(block => block.text || '').join('\\n') || '';
    if (snapshot.result?.isError || !content.includes('Synthetic entry')) throw new Error('snapshot');
    const blocked = await request('tools/call', {name: 'browser_navigate',
      arguments: {url: 'https://blocked.aos-relay.invalid/outside'}});
    if (!blocked.result?.isError) throw new Error('off-entry request was not blocked');
    process.stdout.write(JSON.stringify({version: initialized.result.serverInfo.version,
      heading: content.includes('Synthetic entry') ? 'Synthetic entry' : null}));
  } finally {child.kill('SIGTERM');}
})().catch(error => {process.stderr.write(error.message); child.kill('SIGTERM'); process.exitCode = 1;});'''
        def action():
            result = subprocess.run([*DOCKER, 'exec', '-e', 'DISPLAY=:99', desktop.container_id,
                                     '/usr/local/bin/node', '-e', script,
                                     self.profile.entry_url, '1.64.0-alpha-1789764292000'],
                                    capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(json.loads(result.stdout)['heading'], 'Synthetic entry')

        outcome = self.run_relay(action)
        self.assertNotIn('error', outcome)
        self.assertEqual(self.server.paths, ['/entry'])
        self.assertEqual(outcome['report'].request_attempts, 1)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS relay test')
    def test_network_none_entry_gate_rejects_same_url_subrequests(self):
        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        gate = self.workspace / 'remote_gate.cjs'
        gate.write_text(worker_namespace()['remote_entry_page_gate_source'](
            self.profile.entry_url, self.draft.binding_sha256, self.relay().client_token))
        gate.chmod(0o600)
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const {chromium} = require('/workspace/node_modules/playwright-core');
const gate = require('/workspace/remote_gate.cjs');
const entry = process.argv[1];
(async () => {
  const browser = await chromium.launch({executablePath: '/opt/chromium/chrome', headless: false,
                                         args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const blocked = [];
    page.on('requestfailed', request => {
      if (request.url() === entry) blocked.push(request.resourceType());
    });
    await gate.default({page});
    await page.setContent('<img src="' + entry + '"><iframe src="' + entry + '"></iframe>',
                          {waitUntil: 'load', timeout: 8000});
    const response = await page.goto(entry, {waitUntil: 'domcontentloaded', timeout: 8000});
    process.stdout.write(JSON.stringify({blocked, status: response.status(),
      heading: await page.locator('h1').textContent()}));
  } finally {await browser.close();}
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        def action():
            output = desktop.docker(['exec', '-e', 'DISPLAY=:99', desktop.container_id,
                                     '/usr/local/bin/node', '-e', script, self.profile.entry_url], timeout=25)
            result = json.loads(output)
            self.assertEqual(result['status'], 200)
            self.assertEqual(result['heading'], 'Synthetic entry')
            self.assertIn('image', result['blocked'])
            self.assertIn('document', result['blocked'])

        outcome = self.run_relay(action)
        self.assertNotIn('error', outcome)
        self.assertEqual(self.server.paths, ['/entry'])
        self.assertEqual(outcome['report'].request_attempts, 1)

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                         and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                         'Explicit owned Ubuntu HTTPS relay test')
    def test_network_none_ubuntu_chromium_receives_host_https_entry(self):
        _pins, bundle = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        unpack_bundle(bundle, self.workspace / 'node_modules')
        desktop = DesktopRuntime(self.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(desktop.stop)
        desktop.start()
        self.assertFalse(desktop.status()['network'])
        script = '''const net = require('node:net');
const crypto = require('node:crypto');
const {chromium} = require('/workspace/node_modules/playwright-core');
const socketPath = '/workspace/relay.sock';
const entry = process.argv[1];
const binding = process.argv[2];
const clientToken = process.argv[3];
const call = request => new Promise((resolve, reject) => {
  const connection = net.createConnection(socketPath);
  let answer = '';
  connection.setTimeout(4000, () => connection.destroy(new Error('timeout')));
  connection.on('connect', () => connection.write(JSON.stringify(request) + '\\n'));
  connection.on('data', chunk => {answer += chunk; if (answer.length > 100000) connection.destroy(new Error('size'));});
  connection.on('end', () => {try {resolve(JSON.parse(answer));} catch (error) {reject(error);}});
  connection.on('error', reject);
});
(async () => {
  const browser = await chromium.launch({executablePath: '/opt/chromium/chrome', headless: false,
                                         args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    await page.route('**/*', async route => {
      try {
        const request = route.request();
        const reply = await call({method: request.method(), url: request.url(),
                                  binding_sha256: binding, client_token: clientToken});
        if (reply.status !== 200 || reply.content_type !== 'text/html') throw new Error('denied');
        const body = Buffer.from(reply.body_base64, 'base64');
        if (crypto.createHash('sha256').update(body).digest('hex') !== reply.response_sha256) throw new Error('hash');
        await route.fulfill({status: 200, body, headers: {'content-type': 'text/html',
          'content-security-policy': "default-src 'none'; form-action 'none'; base-uri 'none'",
          'cache-control': 'no-store'}});
      } catch (_error) {await route.abort('blockedbyclient');}
    });
    const response = await page.goto(entry, {waitUntil: 'domcontentloaded', timeout: 8000});
    process.stdout.write(JSON.stringify({status: response.status(),
      heading: await page.locator('h1').textContent()}));
  } finally {await browser.close();}
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        def action():
            output = desktop.docker(['exec', '-e', 'DISPLAY=:99', desktop.container_id,
                                     '/usr/local/bin/node', '-e', script,
                                     self.profile.entry_url, self.draft.binding_sha256,
                                     self.relay().client_token], timeout=25)
            self.assertEqual(json.loads(output), {'status': 200, 'heading': 'Synthetic entry'})

        outcome = self.run_relay(action)
        self.assertNotIn('error', outcome)
        self.assertEqual(self.server.paths, ['/entry'])
        self.assertEqual(outcome['report'].response_bytes, len(SyntheticEntry.body))


if __name__ == '__main__':
    unittest.main()
