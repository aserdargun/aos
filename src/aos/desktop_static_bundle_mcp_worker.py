import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from aos_desktop_worker import ProtocolError, emit
from aos_mcp_bundle import MAX_ARCHIVE, PLAYWRIGHT_VERSION, unpack_bundle
from aos_mcp_worker_shared import MCPBrowser, reap_owned_children, static_bundle_page_gate_source


def observation(browser, entry_url):
    value = browser.evaluate('() => ({url: location.href, title: document.title, '
                             'heading: document.querySelector("h1")?.textContent ?? ""})')
    if (not isinstance(value, dict) or set(value) != {'url', 'title', 'heading'}
            or value['url'] != entry_url or any(not isinstance(value[key], str)
                                               or len(value[key]) > 200 for key in ('title', 'heading'))):
        raise ProtocolError('Static bundle entry identity or bounded DOM differs')
    return {'url': value['url'],
            'title_sha256': hashlib.sha256(value['title'].encode()).hexdigest(),
            'heading_sha256': hashlib.sha256(value['heading'].encode()).hexdigest()}


def main():
    os.umask(0o077)
    if (ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0
            or len(sys.argv) != 5):
        emit({'error': 'RUNTIME_CRASH'})
        return
    entry_url, plan_sha256, binding_sha256, bundle_sha256 = sys.argv[1:]
    if (not entry_url.startswith('https://')
            or any(re.fullmatch('[a-f0-9]{64}', checksum) is None
                   for checksum in (plan_sha256, binding_sha256, bundle_sha256))):
        emit({'error': 'RUNTIME_CRASH'})
        return
    browser = MCPBrowser()
    with tempfile.TemporaryDirectory(prefix='aos-static-bundle-mcp-', dir='/home/agent') as directory:
        try:
            socket_info = Path('/workspace/relay.sock').lstat()
            if (not stat.S_ISSOCK(socket_info.st_mode)
                    or stat.S_IMODE(socket_info.st_mode) != 0o600
                    or socket_info.st_uid != os.getuid()):
                raise ProtocolError('Static bundle socket identity differs')
            raw_bundle = sys.stdin.buffer.readline(MAX_ARCHIVE * 2 + 1)
            if len(raw_bundle) > MAX_ARCHIVE * 2 or not raw_bundle.endswith(b'\n'):
                raise ProtocolError('MCP bundle input exceeded bound')
            payload = base64.b64decode(raw_bundle.strip(), validate=True)
            if hashlib.sha256(payload).hexdigest() != bundle_sha256:
                raise ProtocolError('MCP bundle identity differs')
            raw_token = sys.stdin.buffer.readline(66)
            if (len(raw_token) != 65 or not raw_token.endswith(b'\n')
                    or re.fullmatch(b'[a-f0-9]{64}', raw_token[:-1]) is None):
                raise ProtocolError('Static bundle client token differs')
            client_token = raw_token[:-1].decode('ascii')
            raw_plan = sys.stdin.buffer.readline(32000)
            if (len(raw_plan) >= 32000 or not raw_plan.endswith(b'\n')
                    or hashlib.sha256(raw_plan[:-1]).hexdigest() != plan_sha256):
                raise ProtocolError('Static bundle plan identity differs')
            plan = json.loads(raw_plan)
            if (not isinstance(plan, dict)
                    or plan.get('schema_version') not in {'1.0', '2.0'}
                    or set(plan) != ({'schema_version', 'profile_sha256', 'task_sha256',
                                      'assets', 'execution_authorized', 'collection_authorized'}
                                     | ({'data_resources'} if plan.get('schema_version') == '2.0'
                                        else set()))
                    or plan['execution_authorized'] is not False
                    or plan['collection_authorized'] is not False
                    or not isinstance(plan['assets'], list)
                    or not 1 <= len(plan['assets']) <= 8
                    or (plan['schema_version'] == '2.0'
                        and (not isinstance(plan['data_resources'], list)
                             or not 1 <= len(plan['data_resources']) <= 4))):
                raise ProtocolError('Static bundle plan scope differs')
            data_resources = plan.get('data_resources', [])
            root = Path(directory)
            unpack_bundle(payload, root / 'node_modules')
            browser.start(root, '', gate_source=static_bundle_page_gate_source(
                entry_url, plan['assets'], plan_sha256, client_token, data_resources))
            emit({'ready': True, 'headed': True, 'display': os.environ.get('DISPLAY'),
                  'network_namespace': os.readlink('/proc/self/ns/net'),
                  'home_visible': Path('/home/cachyos').exists(),
                  'docker_socket_visible': Path('/var/run/docker.sock').exists(),
                  'chromium_sha256': hashlib.sha256(Path('/opt/chromium/chrome').read_bytes()).hexdigest(),
                  'transport': 'playwright_mcp', 'bundle_sha256': bundle_sha256,
                  'server_version': PLAYWRIGHT_VERSION, 'static_bundle_gate': True,
                  'plan_sha256': plan_sha256, 'binding_sha256': binding_sha256,
                  'asset_count': len(plan['assets']),
                  'data_count': len(data_resources)})
            opened = False
            while raw := sys.stdin.buffer.readline(4097):
                try:
                    if len(raw) > 4096 or not raw.endswith(b'\n'):
                        raise ValueError('Request exceeded bound')
                    request = json.loads(raw)
                    if (not isinstance(request, dict) or set(request) != {'tool', 'arguments'}
                            or request['arguments'] != {}):
                        raise ValueError('Invalid fixed static bundle request')
                    if request['tool'] == 'browser.static.open' and not opened:
                        opened = True
                        browser.tool('browser_navigate', {'url': entry_url})
                        emit(observation(browser, entry_url))
                    elif request['tool'] == 'browser.static.observe' and opened:
                        emit(observation(browser, entry_url))
                    else:
                        raise ValueError('Unauthorized static bundle request')
                except (ValueError, TypeError, KeyError):
                    emit({'error': 'UNSAFE_ACTION'})
        except (ProtocolError, OSError, ValueError, KeyError, TypeError):
            emit({'error': 'RUNTIME_CRASH'})
        finally:
            try:
                browser.close()
            finally:
                reap_owned_children()


if __name__ == '__main__':
    main()
