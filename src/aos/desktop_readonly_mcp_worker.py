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
from aos_mcp_worker_shared import MCPBrowser, reap_owned_children, readonly_route_page_gate_source


def observation(browser, expected_url, routes):
    script = ('() => { const routes = ' + json.dumps(routes) + ''';
        const anchors = document.querySelectorAll('a[href]');
        const planned = new Set();
        let unregistered = 0;
        for (let index = 0; index < Math.min(anchors.length, 64); index++) {
            const routeIndex = routes.indexOf(anchors[index].href);
            if (routeIndex < 0) unregistered++;
            else planned.add(routeIndex);
        }
        return {url: location.href, title: document.title,
            heading: document.querySelector('h1')?.textContent ?? '',
            planned_link_indices: [...planned].sort((left, right) => left - right),
            unregistered_link_count: unregistered, links_truncated: anchors.length > 64};
    }''')
    value = browser.evaluate(script)
    if (not isinstance(value, dict) or set(value) != {'url', 'title', 'heading',
                                                     'planned_link_indices',
                                                     'unregistered_link_count', 'links_truncated'}
            or value['url'] != expected_url or any(not isinstance(value[key], str)
                                                   or len(value[key]) > 200 for key in ('title', 'heading'))
            or not isinstance(value['planned_link_indices'], list)
            or any(type(index) is not int or index < 0 or index >= len(routes)
                   for index in value['planned_link_indices'])
            or value['planned_link_indices'] != sorted(set(value['planned_link_indices']))
            or type(value['unregistered_link_count']) is not int
            or not 0 <= value['unregistered_link_count'] <= 64
            or type(value['links_truncated']) is not bool):
        raise ProtocolError('Read-only route identity or bounded DOM differs')
    return {'url': value['url'],
            'title_sha256': hashlib.sha256(value['title'].encode()).hexdigest(),
            'heading_sha256': hashlib.sha256(value['heading'].encode()).hexdigest(),
            'planned_link_indices': value['planned_link_indices'],
            'unregistered_link_count': value['unregistered_link_count'],
            'links_truncated': value['links_truncated']}


def main():
    os.umask(0o077)
    if (ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0
            or len(sys.argv) != 3):
        emit({'error': 'RUNTIME_CRASH'})
        return
    plan_sha256, bundle_sha256 = sys.argv[1:]
    if (re.fullmatch('[a-f0-9]{64}', plan_sha256) is None
            or re.fullmatch('[a-f0-9]{64}', bundle_sha256) is None):
        emit({'error': 'RUNTIME_CRASH'})
        return
    browser = MCPBrowser()
    with tempfile.TemporaryDirectory(prefix='aos-readonly-mcp-', dir='/home/agent') as directory:
        try:
            socket_info = Path('/workspace/relay.sock').lstat()
            if (not stat.S_ISSOCK(socket_info.st_mode)
                    or stat.S_IMODE(socket_info.st_mode) != 0o600
                    or socket_info.st_uid != os.getuid()):
                raise ProtocolError('Read-only relay socket identity differs')
            raw_bundle = sys.stdin.buffer.readline(MAX_ARCHIVE * 2 + 1)
            if len(raw_bundle) > MAX_ARCHIVE * 2 or not raw_bundle.endswith(b'\n'):
                raise ProtocolError('MCP bundle input exceeded bound')
            payload = base64.b64decode(raw_bundle.strip(), validate=True)
            if hashlib.sha256(payload).hexdigest() != bundle_sha256:
                raise ProtocolError('MCP bundle identity differs')
            raw_token = sys.stdin.buffer.readline(66)
            if (len(raw_token) != 65 or not raw_token.endswith(b'\n')
                    or re.fullmatch(b'[a-f0-9]{64}', raw_token[:-1]) is None):
                raise ProtocolError('Read-only relay client token differs')
            raw_routes = sys.stdin.buffer.readline(20000)
            if not raw_routes.endswith(b'\n') or len(raw_routes) >= 20000:
                raise ProtocolError('Read-only routes exceeded bound')
            routes = json.loads(raw_routes)
            client_token = raw_token[:-1].decode('ascii')
            gate = readonly_route_page_gate_source(routes, plan_sha256, client_token)
            root = Path(directory)
            unpack_bundle(payload, root / 'node_modules')
            browser.start(root, '', gate_source=gate)
            emit({'ready': True, 'headed': True, 'display': os.environ.get('DISPLAY'),
                  'network_namespace': os.readlink('/proc/self/ns/net'),
                  'home_visible': Path('/home/cachyos').exists(),
                  'docker_socket_visible': Path('/var/run/docker.sock').exists(),
                  'chromium_sha256': hashlib.sha256(Path('/opt/chromium/chrome').read_bytes()).hexdigest(),
                  'transport': 'playwright_mcp', 'bundle_sha256': bundle_sha256,
                  'server_version': PLAYWRIGHT_VERSION, 'readonly_route_gate': True,
                  'plan_sha256': plan_sha256, 'route_count': len(routes),
                  'routes_sha256': hashlib.sha256(raw_routes.strip()).hexdigest()})
            next_index = 0
            opened_index = None
            while raw := sys.stdin.buffer.readline(4097):
                try:
                    if len(raw) > 4096 or not raw.endswith(b'\n'):
                        raise ValueError('Request exceeded bound')
                    request = json.loads(raw)
                    if (not isinstance(request, dict) or set(request) != {'tool', 'arguments'}
                            or not isinstance(request['arguments'], dict)
                            or set(request['arguments']) != {'route_index'}
                            or type(request['arguments']['route_index']) is not int):
                        raise ValueError('Invalid fixed read-only request')
                    route_index = request['arguments']['route_index']
                    if (request['tool'] == 'browser.remote.route'
                            and route_index == next_index and route_index < len(routes)):
                        next_index += 1
                        browser.tool('browser_navigate', {'url': routes[route_index]})
                        opened_index = route_index
                        emit(observation(browser, routes[route_index], routes))
                    elif request['tool'] == 'browser.remote.observe' and route_index == opened_index:
                        emit(observation(browser, routes[route_index], routes))
                    else:
                        raise ValueError('Unauthorized fixed read-only request')
                except (ValueError, TypeError, KeyError, IndexError):
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
