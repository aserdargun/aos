import base64
import os
from pathlib import Path
import re
import subprocess

from .contracts import AOSFault, ErrorCode, LOCAL_NAVIGATION_SCOPE, REPO_ROOT, STAGING_WORKFLOW_SCOPE, canonical
from .desktop import DOCKER
from .desktop_browser import DesktopBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle


class DesktopMCPBrowserRuntime(DesktopBrowserRuntime):
    worker_file = 'desktop_mcp_worker.py'
    shared_worker_files = ()
    allowed_tools = DesktopBrowserRuntime.allowed_tools | frozenset({
        'browser.fixture.open', 'browser.fixture.snapshot', 'browser.fixture.follow',
        'browser.staging.open', 'browser.staging.snapshot', 'browser.staging.follow',
        'browser.staging.fill', 'browser.staging.submit'})

    def __init__(self, desktop, mcp_manifest: Path, *, fixture_request_guard: bool = False,
                 staging_workflow: bool = False, fixture_port: int | None = None):
        if (type(fixture_request_guard) is not bool or type(staging_workflow) is not bool
                or staging_workflow and not fixture_request_guard
                or fixture_port is not None and (not fixture_request_guard
                                                  or type(fixture_port) is not int
                                                  or not 1024 <= fixture_port <= 65535)):
            raise ValueError('Fixture request guard selection must be boolean')
        super().__init__(desktop)
        self.mcp_manifest = mcp_manifest
        self.fixture_request_guard = fixture_request_guard
        self.staging_workflow = staging_workflow
        self.fixture_port = fixture_port
        self.probe_opened = False
        self.staging_opened = False

    def worker_source(self):
        source = 'import sys,types\n'
        for module, filename in (('aos_desktop_worker', 'desktop_browser_worker.py'),
                                 ('aos_mcp_bundle', 'desktop_mcp_bundle.py'),
                                 *self.shared_worker_files):
            common = (REPO_ROOT / 'src/aos' / filename).read_text()
            source += (f'shared=types.ModuleType({module!r})\nsys.modules[{module!r}]=shared\n'
                       f'exec({common!r},shared.__dict__)\n')
        return source + (REPO_ROOT / 'src/aos' / self.worker_file).read_text()

    def start(self):
        if self.process is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Visible MCP browser is already started')
        self.owned_desktop()
        try:
            mcp_pins, payload = read_bundle(self.mcp_manifest)
        except (ValueError, OSError, KeyError) as error:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Pinned MCP preparation is invalid') from error
        self.pins = {**self.desktop.pins, 'mcp': mcp_pins}
        worker = self.worker_source()
        fixture = (REPO_ROOT / 'examples' / self.fixture_file).read_text()
        try:
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c', worker,
                 fixture, mcp_pins['bundle_sha256'],
                 'staging_guard' if self.staging_workflow else
                 'fixture_guard' if self.fixture_request_guard else 'standard',
                 str(self.fixture_port) if self.fixture_port is not None else 'auto'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.process.stdin.write(base64.b64encode(payload) + b'\n')
            self.process.stdin.flush()
            self.evidence = self.receive(25)
            if (self.evidence.get('ready') is not True or self.evidence.get('headed') is not True
                    or self.evidence.get('display') != ':99'
                    or self.evidence.get('chromium_sha256') != self.pins['chromium_sha256']
                    or self.evidence.get('network_namespace') == os.readlink('/proc/self/ns/net')
                    or self.evidence.get('home_visible') is not False
                    or self.evidence.get('docker_socket_visible') is not False
                    or self.evidence.get('transport') != 'playwright_mcp'
                    or self.evidence.get('bundle_sha256') != mcp_pins['bundle_sha256']
                    or self.evidence.get('server_version') != PLAYWRIGHT_VERSION
                    or self.evidence.get('page_request_gate') is not self.fixture_request_guard
                    or self.evidence.get('staging_workflow') is not self.staging_workflow
                    or self.evidence.get('fixture_proxy_mode') is not (self.fixture_port is not None)
                    or (self.fixture_request_guard and not self.valid_fixture_request_guard())
                    or (self.fixture_port is not None
                        and self.evidence['fixture_request_guard']['fixture_port'] != self.fixture_port)
                    or (not self.fixture_request_guard and self.evidence.get('fixture_request_guard') is not None)):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Visible MCP isolation or identity handshake differs')
        except BaseException:
            self.stop()
            raise

    def status(self):
        return {**super().status(), 'kind': 'docker_chromium_mcp', 'browser_transport': 'playwright_mcp',
                'fixture_request_guard': self.fixture_request_guard,
                'staging_workflow': self.staging_workflow}

    def valid_fixture_request_guard(self):
        guard = self.evidence.get('fixture_request_guard')
        return (isinstance(guard, dict) and set(guard) == {'kind', 'fixture_port', 'verified'}
                and guard['kind'] == 'landlock_tcp_connect_v1' and guard['verified'] is True
                and type(guard['fixture_port']) is int and 0 < guard['fixture_port'] < 65536)

    def perform(self, tool, arguments):
        if self.staging_workflow and tool not in {
                'browser.staging.open', 'browser.staging.snapshot', 'browser.staging.follow',
                'browser.staging.fill', 'browser.staging.submit'} and not (
                    tool == 'browser.observe' and not self.staging_opened):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging permits only its fixed tools')
        if tool in {'browser.fixture.open', 'browser.fixture.snapshot', 'browser.fixture.follow'}:
            if not self.fixture_request_guard or self.staging_workflow or not self.valid_fixture_request_guard():
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Local fixture requires the attested request guard')
        if tool in {'browser.staging.open', 'browser.staging.snapshot', 'browser.staging.follow',
                    'browser.staging.fill', 'browser.staging.submit'}:
            if not self.staging_workflow or not self.valid_fixture_request_guard():
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging requires the attested request guard')
        result = super().perform(tool, arguments)
        if tool == 'browser.fixture.open':
            self.probe_opened = True
        if tool == 'browser.staging.open':
            self.staging_opened = True
        return result

    def read(self, path):
        if path == STAGING_WORKFLOW_SCOPE:
            if not self.staging_workflow:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging runtime is unavailable')
            if self.staging_opened:
                return canonical(self.perform('browser.staging.snapshot', {}))
            observed = self.perform('browser.observe', {})
            return canonical({'page': 'entry', 'value': observed['value'], 'receipt': observed['receipt'],
                              'submissions': observed['submissions']})
        if path != LOCAL_NAVIGATION_SCOPE:
            return super().read(path)
        if self.probe_opened:
            return canonical(self.perform('browser.fixture.snapshot', {}))
        observed = self.perform('browser.observe', {})
        return canonical({'page': 'entry', 'value': observed['value'], 'receipt': observed['receipt'],
                          'submissions': observed['submissions']})

    def validate_result(self, tool, result):
        if tool in {'browser.staging.open', 'browser.staging.snapshot', 'browser.staging.follow',
                    'browser.staging.fill', 'browser.staging.submit'}:
            if not isinstance(result, dict) or set(result) != {
                    'page', 'heading', 'snapshot_id', 'value', 'receipt', 'submissions', 'elements'}:
                raise ValueError('Invalid synthetic staging snapshot')
            if (result['page'] not in {'app', 'draft', 'receipt'}
                    or result['heading'] != 'Synthetic ' + result['page']
                    or not isinstance(result['snapshot_id'], str)
                    or re.fullmatch('[a-f0-9]{32}', result['snapshot_id']) is None
                    or not isinstance(result['value'], str)
                    or result['value'] not in ('', 'Hello from the local agent.')
                    or not isinstance(result['receipt'], str)
                    or result['receipt'] not in ('', 'Saved locally.')
                    or type(result['submissions']) is not int
                    or result['submissions'] not in (0, 1)):
                raise ValueError('Invalid synthetic staging state')
            expected = ({'app': [('link', 'Draft')],
                         'draft': [('textbox', 'Message'), ('button', 'Save draft')],
                         'receipt': []})[result['page']]
            elements = result['elements']
            if (not isinstance(elements, list) or len(elements) != len(expected)
                    or any(not isinstance(element, dict) or set(element) != {'element_id', 'role', 'label'}
                           or (element['role'], element['label']) != (role, label)
                           or not isinstance(element['element_id'], str)
                           or re.fullmatch('[a-f0-9]{32}', element['element_id']) is None
                           or element['element_id'] == result['snapshot_id']
                           for element, (role, label) in zip(elements, expected))
                    or len({element['element_id'] for element in elements}) != len(elements)):
                raise ValueError('Invalid synthetic staging controls')
            if (result['page'] == 'app' and (result['value'], result['receipt'], result['submissions']) != ('', '', 0)
                    or result['page'] == 'draft' and (result['receipt'], result['submissions']) != ('', 0)
                    or result['page'] == 'receipt' and (result['value'], result['receipt'], result['submissions'])
                    != ('', 'Saved locally.', 1)):
                raise ValueError('Invalid synthetic staging outcome')
            return result
        if tool not in {'browser.fixture.open', 'browser.fixture.snapshot', 'browser.fixture.follow'}:
            return super().validate_result(tool, result)
        if not isinstance(result, dict) or set(result) != {'page', 'heading', 'snapshot_id', 'elements'}:
            raise ValueError('Invalid local fixture snapshot')
        page = result['page']
        if (page not in {'start', 'details'} or result['heading'] != 'Synthetic ' + page
                or not isinstance(result['snapshot_id'], str)
                or re.fullmatch('[a-f0-9]{32}', result['snapshot_id']) is None):
            raise ValueError('Invalid local fixture identity')
        elements = result['elements']
        if page == 'details':
            if elements != []:
                raise ValueError('Unexpected local fixture link')
        elif (not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict)
              or set(elements[0]) != {'element_id', 'role', 'label'}
              or elements[0]['role'] != 'link' or elements[0]['label'] != 'Details'
              or not isinstance(elements[0]['element_id'], str)
              or re.fullmatch('[a-f0-9]{32}', elements[0]['element_id']) is None
              or elements[0]['element_id'] == result['snapshot_id']):
            raise ValueError('Invalid local fixture link')
        return result
