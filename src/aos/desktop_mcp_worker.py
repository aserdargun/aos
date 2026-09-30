import base64
import ctypes
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import uuid4

from aos_desktop_worker import PREPARE, SNAPSHOT, VERIFY, ProtocolError, emit
from aos_mcp_bundle import MAX_ARCHIVE, PLAYWRIGHT_VERSION, unpack_bundle


MCP_TOOLS = frozenset({'browser_navigate', 'browser_snapshot', 'browser_evaluate',
                       'browser_fill_form', 'browser_select_option', 'browser_click',
                       'browser_close'})
FIXTURE_PAGES = {
    '/start': ('Start', 'Synthetic start', '<a href="/details">Details</a>'),
    '/details': ('Details', 'Synthetic details', '<p>Local fixture only.</p>'),
}
STAGING_MESSAGE = 'Hello from the local agent.'
STAGING_FORM_BODY = b'message=Hello+from+the+local+agent.'
STAGING_PAGES = {
    '/app': ('App', 'Synthetic app', '<a href="/draft">Draft</a>'),
    '/draft': ('Draft', 'Synthetic draft',
               '<form method="post" action="/submit"><label for="message">Message</label>'
               '<input id="message" name="message" required><button type="submit">Save draft</button></form>'),
    '/receipt': ('Receipt', 'Synthetic receipt', '<p>Saved locally.</p>'),
}
FIXTURE_REQUEST_LIMIT = 16
FIXTURE_PROXY_REQUEST_LIMIT = 64
FIXTURE_RESPONSE_BYTE_LIMIT = 2048
FIXTURE_REQUEST_IDLE_TIMEOUT_SECONDS = 1


def fixture_page_gate_source(origin, staging):
    paths = {'GET': ['/app', '/draft', '/receipt'], 'POST': ['/submit']} if staging else {
        'GET': ['/start', '/details']}
    allowed = {method: [origin + path for path in routes] for method, routes in paths.items()}
    return ('module.exports = {default: async ({page}) => {'
            'const allowed = ' + json.dumps(allowed, sort_keys=True) + ';'
            'await page.route("**/*", async route => {'
            'const request = route.request();'
            'const routes = allowed[request.method()];'
            'if (!routes || !routes.includes(request.url())) {'
            'await route.abort("blockedbyclient"); return;}'
            'await route.continue();'
            '});}};')


def remote_entry_page_gate_source(entry_url, binding_sha256, client_token):
    if (not isinstance(entry_url, str) or not entry_url.startswith('https://')
            or not isinstance(binding_sha256, str)
            or re.fullmatch('[a-f0-9]{64}', binding_sha256) is None
            or not isinstance(client_token, str)
            or re.fullmatch('[a-f0-9]{64}', client_token) is None):
        raise ValueError('Invalid exact remote entry gate')
    return ('''const net = require('node:net');
const crypto = require('node:crypto');
module.exports = {default: async ({page}) => {
  const entry = ''' + json.dumps(entry_url) + ''';
  const binding = ''' + json.dumps(binding_sha256) + ''';
  const clientToken = ''' + json.dumps(client_token) + ''';
  let used = false;
  await page.route('**/*', async route => {
    const request = route.request();
    if (used || request.method() !== 'GET' || request.url() !== entry
        || !request.isNavigationRequest() || request.resourceType() !== 'document'
        || request.frame() !== page.mainFrame()) {
      await route.abort('blockedbyclient'); return;
    }
    used = true;
    try {
      const reply = await new Promise((resolve, reject) => {
        const connection = net.createConnection('/workspace/relay.sock');
        let answer = '';
        connection.setTimeout(4000, () => connection.destroy(new Error('timeout')));
        connection.on('connect', () => connection.write(JSON.stringify({
          method: 'GET', url: entry, binding_sha256: binding,
          client_token: clientToken}) + '\\n'));
        connection.on('data', chunk => {
          answer += chunk;
          if (answer.length > 100000) connection.destroy(new Error('size'));
        });
        connection.on('end', () => {
          try {resolve(JSON.parse(answer));} catch (error) {reject(error);}
        });
        connection.on('error', reject);
      });
      if (reply.status !== 200 || reply.content_type !== 'text/html'
          || typeof reply.body_base64 !== 'string'
          || !/^[a-f0-9]{64}$/.test(reply.response_sha256)) throw new Error('denied');
      const body = Buffer.from(reply.body_base64, 'base64');
      if (body.length > 65536 || crypto.createHash('sha256').update(body).digest('hex')
          !== reply.response_sha256) throw new Error('hash');
      await route.fulfill({status: 200, body, headers: {
        'content-type': 'text/html', 'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; form-action 'none'; base-uri 'none'"}});
    } catch (_error) {await route.abort('blockedbyclient');}
  });
}};''')


def _readonly_query_is_canonical(query, parsed_query):
    if not query or len(query) > 512 or parsed_query != query:
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=8, encoding='utf-8', errors='strict')
    except ValueError:
        return False
    return (1 <= len(pairs) <= 8
            and len({key for key, _value in pairs}) == len(pairs)
            and all(re.fullmatch('[A-Za-z][A-Za-z0-9._~-]{0,63}', key) is not None
                    and len(item) <= 128
                    and all(32 <= ord(character) <= 126 for character in item)
                    for key, item in pairs)
            and urlencode(pairs, quote_via=quote, safe='-._~') == query)


def static_bundle_page_gate_source(entry_url, assets, plan_sha256, client_token,
                                   data_resources=None):
    if data_resources is None:
        data_resources = []
    parsed_entry = urlsplit(entry_url) if isinstance(entry_url, str) else None
    if (parsed_entry is None or parsed_entry.scheme != 'https' or not parsed_entry.hostname
            or not parsed_entry.path.startswith('/')
            or any(character in entry_url for character in ('\\', '%', '?', '#'))
            or not isinstance(assets, list) or not 1 <= len(assets) <= 8
            or not isinstance(data_resources, list) or len(data_resources) > 4
            or not isinstance(plan_sha256, str)
            or re.fullmatch('[a-f0-9]{64}', plan_sha256) is None
            or not isinstance(client_token, str)
            or re.fullmatch('[a-f0-9]{64}', client_token) is None):
        raise ValueError('Invalid exact static bundle gate')
    seen_urls = {entry_url}
    for asset in assets:
        if (not isinstance(asset, dict) or set(asset) != {'url', 'content_type'}
                or not isinstance(asset['url'], str)
                or asset['content_type'] not in {'text/css', 'text/javascript',
                                                 'application/javascript', 'image/png',
                                                 'image/jpeg', 'image/webp', 'image/gif'}):
            raise ValueError('Invalid exact static bundle asset')
        parsed_asset = urlsplit(asset['url'])
        base_url, separator, query = asset['url'].partition('?')
        if separator and not _readonly_query_is_canonical(query, parsed_asset.query):
            raise ValueError('Invalid exact static bundle asset')
        if (parsed_asset.scheme != 'https' or parsed_asset.netloc != parsed_entry.netloc
                or not re.fullmatch('/[A-Za-z0-9._~/-]*', parsed_asset.path)
                or '//' in parsed_asset.path
                or any(part in {'.', '..'} for part in parsed_asset.path.split('/'))
                or base_url == entry_url
                or base_url != (parsed_asset.scheme + '://'
                                + parsed_asset.netloc + parsed_asset.path)
                or any(character in base_url for character in ('\\', '%', '#'))
                or asset['url'] in seen_urls):
            raise ValueError('Invalid exact static bundle asset')
        seen_urls.add(asset['url'])
    for resource in data_resources:
        if (not isinstance(resource, dict)
                or set(resource) != {'url', 'content_type'}
                or resource['content_type'] != 'application/json'
                or not isinstance(resource['url'], str)):
            raise ValueError('Invalid exact read-only data resource')
        parsed_resource = urlsplit(resource['url'])
        base_url, separator, query = resource['url'].partition('?')
        if separator and not _readonly_query_is_canonical(query, parsed_resource.query):
            raise ValueError('Invalid exact read-only data resource')
        if (parsed_resource.scheme != 'https'
                or parsed_resource.netloc != parsed_entry.netloc
                or not re.fullmatch('/[A-Za-z0-9._~/-]*', parsed_resource.path)
                or '//' in parsed_resource.path
                or any(part in {'.', '..'} for part in parsed_resource.path.split('/'))
                or base_url != (parsed_resource.scheme + '://'
                                + parsed_resource.netloc + parsed_resource.path)
                or any(character in base_url for character in ('\\', '%', '#'))
                or resource['url'] in seen_urls):
            raise ValueError('Invalid exact read-only data resource')
        seen_urls.add(resource['url'])
    return ('''const net = require('node:net');
const crypto = require('node:crypto');
module.exports = {default: async ({page}) => {
  const entry = ''' + json.dumps(entry_url) + ''';
  const assets = ''' + json.dumps(assets) + ''';
  const dataResources = ''' + json.dumps(data_resources) + ''';
  const plan = ''' + json.dumps(plan_sha256) + ''';
  const clientToken = ''' + json.dumps(client_token) + ''';
  let entryUsed = false;
  let entryServed = false;
  const usedAssets = new Set();
  const usedData = new Set();
  await page.route('**/*', async route => {
    const request = route.request();
    let mainFrame = false;
    try {mainFrame = request.frame() === page.mainFrame();} catch (_error) {}
    const entryRequest = !entryUsed && request.method() === 'GET'
      && request.url() === entry && request.isNavigationRequest()
      && request.resourceType() === 'document' && mainFrame;
    const assetIndex = assets.findIndex(asset => asset.url === request.url());
    const assetRequest = entryServed && assetIndex >= 0 && !usedAssets.has(assetIndex)
      && request.method() === 'GET' && !request.isNavigationRequest() && mainFrame
      && request.resourceType() === (assets[assetIndex].content_type === 'text/css'
        ? 'stylesheet' : assets[assetIndex].content_type.startsWith('image/')
          ? 'image' : 'script');
    const dataIndex = dataResources.findIndex(resource => resource.url === request.url());
    const dataRequest = entryServed && dataIndex >= 0 && !usedData.has(dataIndex)
      && request.method() === 'GET' && !request.isNavigationRequest() && mainFrame
      && (request.resourceType() === 'fetch' || request.resourceType() === 'xhr');
    if (!entryRequest && !assetRequest && !dataRequest) {
      await route.abort('blockedbyclient'); return;
    }
    if (entryRequest) entryUsed = true;
    else if (assetRequest) usedAssets.add(assetIndex);
    else usedData.add(dataIndex);
    const expectedIndex = entryRequest ? null : assetRequest ? assetIndex
      : assets.length + dataIndex;
    const expectedType = entryRequest ? 'text/html' : assetRequest
      ? assets[assetIndex].content_type : 'application/json';
    try {
      const reply = await new Promise((resolve, reject) => {
        const connection = net.createConnection('/workspace/relay.sock');
        let answer = '';
        connection.setTimeout(12000, () => connection.destroy(new Error('timeout')));
        connection.on('connect', () => connection.write(JSON.stringify({
          method: 'GET', url: request.url(), plan_sha256: plan,
          client_token: clientToken, asset_index: expectedIndex}) + '\\n'));
        connection.on('data', chunk => {
          answer += chunk;
          if (answer.length > 1500000) connection.destroy(new Error('size'));
        });
        connection.on('end', () => {
          try {resolve(JSON.parse(answer));} catch (error) {reject(error);}
        });
        connection.on('error', reject);
      });
      if (reply.status !== 200 || reply.content_type !== expectedType
          || reply.asset_index !== expectedIndex || typeof reply.body_base64 !== 'string'
          || !/^[a-f0-9]{64}$/.test(reply.response_sha256)) throw new Error('denied');
      const body = Buffer.from(reply.body_base64, 'base64');
      if (body.length > (entryRequest ? 65536 : dataRequest ? 262144 : 1048576)
          || crypto.createHash('sha256').update(body).digest('hex')
          !== reply.response_sha256) throw new Error('hash');
      const headers = {'content-type': expectedType, 'cache-control': 'no-store',
        'x-content-type-options': 'nosniff'};
      if (entryRequest) headers['content-security-policy'] =
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src "
        + (dataResources.length ? dataResources.map(resource => resource.url.split('?')[0]).join(' ')
          : "'none'") + "; "
        + "frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'";
      await route.fulfill({status: 200, body, headers});
      if (entryRequest) entryServed = true;
    } catch (_error) {await route.abort('blockedbyclient');}
  });
}};''')


def readonly_route_page_gate_source(routes, plan_sha256, client_token):
    if (not isinstance(routes, list) or not 2 <= len(routes) <= 8
            or any(not isinstance(url, str) for url in routes)
            or len(set(routes)) != len(routes)
            or not isinstance(plan_sha256, str)
            or re.fullmatch('[a-f0-9]{64}', plan_sha256) is None
            or not isinstance(client_token, str)
            or re.fullmatch('[a-f0-9]{64}', client_token) is None):
        raise ValueError('Invalid read-only route gate')
    origin = urlsplit(routes[0]).netloc
    for index, url in enumerate(routes):
        parsed = urlsplit(url)
        base_url, separator, query = url.partition('?')
        if (parsed.scheme != 'https' or parsed.hostname is None
                or parsed.netloc != origin
                or re.fullmatch('/[A-Za-z0-9._~/-]*', parsed.path) is None
                or '//' in parsed.path
                or any(part in {'.', '..'} for part in parsed.path.split('/'))
                or base_url != parsed.scheme + '://' + parsed.netloc + parsed.path
                or any(character in base_url for character in ('\\', '%', '#'))
                or index == 0 and separator
                or separator and not _readonly_query_is_canonical(query, parsed.query)):
            raise ValueError('Invalid read-only route gate')
    return ('''const net = require('node:net');
const crypto = require('node:crypto');
module.exports = {default: async ({page}) => {
  const routes = ''' + json.dumps(routes) + ''';
  const plan = ''' + json.dumps(plan_sha256) + ''';
  const clientToken = ''' + json.dumps(client_token) + ''';
  await page.route('**/*', async route => {
    const request = route.request();
    if (request.method() !== 'GET' || !routes.includes(request.url())
        || !request.isNavigationRequest() || request.resourceType() !== 'document'
        || request.frame() !== page.mainFrame()) {
      await route.abort('blockedbyclient'); return;
    }
    try {
      const reply = await new Promise((resolve, reject) => {
        const connection = net.createConnection('/workspace/relay.sock');
        let answer = '';
        connection.setTimeout(4000, () => connection.destroy(new Error('timeout')));
        connection.on('connect', () => connection.write(JSON.stringify({
          method: 'GET', url: request.url(), plan_sha256: plan,
          client_token: clientToken}) + '\\n'));
        connection.on('data', chunk => {
          answer += chunk;
          if (answer.length > 100000) connection.destroy(new Error('size'));
        });
        connection.on('end', () => {
          try {resolve(JSON.parse(answer));} catch (error) {reject(error);}
        });
        connection.on('error', reject);
      });
      if (reply.status !== 200 || reply.content_type !== 'text/html'
          || typeof reply.body_base64 !== 'string'
          || !/^[a-f0-9]{64}$/.test(reply.response_sha256)) throw new Error('denied');
      const body = Buffer.from(reply.body_base64, 'base64');
      if (body.length > 65536 || crypto.createHash('sha256').update(body).digest('hex')
          !== reply.response_sha256) throw new Error('hash');
      await route.fulfill({status: 200, body, headers: {
        'content-type': 'text/html', 'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; form-action 'none'; base-uri 'none'"}});
    } catch (_error) {await route.abort('blockedbyclient');}
  });
}};''')


def https_form_page_gate_source(entry_url, submit_url, receipt_url, plan_sha256,
                                client_token, confirm_public_plan_sha256=None):
    urls = (entry_url, submit_url, receipt_url)
    if (any(not isinstance(url, str) or not url.startswith('https://')
            or any(character in url for character in ('\\', '%', '?', '#'))
            or urlsplit(url).hostname is None
            for url in urls)
            or len(set(urls)) != 3
            or len({urlsplit(url).netloc for url in urls}) != 1
            or not isinstance(plan_sha256, str)
            or re.fullmatch('[a-f0-9]{64}', plan_sha256) is None
            or not isinstance(client_token, str)
            or re.fullmatch('[a-f0-9]{64}', client_token) is None
            or (confirm_public_plan_sha256 is None) != urlsplit(entry_url).hostname.endswith('.invalid')
            or confirm_public_plan_sha256 is not None
            and confirm_public_plan_sha256 != plan_sha256):
        raise ValueError('Invalid exact HTTPS form gate')
    return ('''const net = require('node:net');
const crypto = require('node:crypto');
module.exports = {default: async ({page}) => {
  const routes = ''' + json.dumps([['GET', entry_url], ['POST', submit_url],
                                    ['GET', receipt_url]]) + ''';
  const plan = ''' + json.dumps(plan_sha256) + ''';
  const clientToken = ''' + json.dumps(client_token) + ''';
  let stage = 0;
  await page.route('**/*', async route => {
    const request = route.request();
    if (stage >= routes.length || request.method() !== routes[stage][0]
        || request.url() !== routes[stage][1]
        || !request.isNavigationRequest() || request.resourceType() !== 'document'
        || request.frame() !== page.mainFrame()) {
      await route.abort('blockedbyclient'); return;
    }
    stage += 1;
    try {
      const payload = {method: request.method(), url: request.url(),
        plan_sha256: plan, client_token: clientToken};
      if (request.method() === 'POST') {
        const body = request.postDataBuffer();
        if (!body || body.length > 4096) throw new Error('body');
        payload.body_base64 = body.toString('base64');
      }
      const reply = await new Promise((resolve, reject) => {
        const connection = net.createConnection('/workspace/relay.sock');
        let answer = '';
        connection.setTimeout(5000, () => connection.destroy(new Error('timeout')));
        connection.on('connect', () => connection.write(JSON.stringify(payload) + '\\n'));
        connection.on('data', chunk => {
          answer += chunk;
          if (answer.length > 100000) connection.destroy(new Error('size'));
        });
        connection.on('end', () => {
          try {resolve(JSON.parse(answer));} catch (error) {reject(error);}
        });
        connection.on('error', reject);
      });
      if (request.method() === 'POST') {
        if (reply.status !== 204) throw new Error('denied');
        await route.fulfill({status: 204, body: ''}); return;
      }
      if (reply.status !== 200 || typeof reply.body_base64 !== 'string'
          || !/^[a-f0-9]{64}$/.test(reply.response_sha256)) throw new Error('denied');
      const body = Buffer.from(reply.body_base64, 'base64');
      if (body.length > 65536 || crypto.createHash('sha256').update(body).digest('hex')
          !== reply.response_sha256) throw new Error('hash');
      await route.fulfill({status: 200, body, headers: {
        'content-type': 'text/html', 'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; form-action "
          + (stage === 1 ? "'self'" : "'none'") + "; base-uri 'none'"}});
    } catch (_error) {await route.abort('blockedbyclient');}
  });
}};''')


class LandlockRuleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64),
                ('handled_access_net', ctypes.c_uint64)]


class LandlockNetPort(ctypes.Structure):
    _fields_ = [('allowed_access', ctypes.c_uint64), ('port', ctypes.c_uint64)]


def install_fixture_tcp_guard(port):
    if os.uname().machine != 'x86_64' or type(port) is not int or not 0 < port < 65536:
        raise ProtocolError('Fixture TCP request guard is unavailable')
    libc = ctypes.CDLL(None, use_errno=True)
    connect_access = 1 << 1
    ruleset = LandlockRuleset(0, connect_access)
    descriptor = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if descriptor < 0:
        raise ProtocolError('Fixture TCP request guard is unavailable')
    try:
        rule = LandlockNetPort(connect_access, port)
        if (libc.syscall(445, descriptor, 2, ctypes.byref(rule), 0) != 0
                or libc.prctl(38, 1, 0, 0, 0) != 0
                or libc.syscall(446, descriptor, 0) != 0):
            raise ProtocolError('Fixture TCP request guard could not be installed')
    finally:
        os.close(descriptor)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as allowed:
        allowed.settimeout(1)
        if allowed.connect_ex(('127.0.0.1', port)) != 0:
            raise ProtocolError('Fixture TCP request guard blocked fixture')
    denied_port = 1 if port != 1 else 2
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as denied:
        denied.settimeout(1)
        if denied.connect_ex(('127.0.0.1', denied_port)) not in (errno.EACCES, errno.EPERM):
            raise ProtocolError('Fixture TCP request guard did not block other ports')
    return {'kind': 'landlock_tcp_connect_v1', 'fixture_port': port, 'verified': True}


class FixtureHandler(BaseHTTPRequestHandler):
    def handle(self):
        self.server.fixture_requests = getattr(self.server, 'fixture_requests', 0) + 1
        limit = FIXTURE_PROXY_REQUEST_LIMIT if getattr(self.server, 'proxy_mode', False) else FIXTURE_REQUEST_LIMIT
        if self.server.fixture_requests > limit:
            self.close_connection = True
            return
        self.request.settimeout(FIXTURE_REQUEST_IDLE_TIMEOUT_SECONDS)
        super().handle()

    def send_error(self, code, message=None, explain=None):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_GET(self):
        staging = getattr(self.server, 'staging_mode', False)
        pages = STAGING_PAGES if staging else FIXTURE_PAGES
        route = self.path
        if getattr(self.server, 'proxy_mode', False):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            parsed = urlsplit(self.path)
            if (parsed.scheme != 'http' or parsed.netloc != f'127.0.0.1:{self.server.server_port}'
                    or self.path != origin + parsed.path):
                self.send_error(404)
                return
            route = parsed.path
        if (self.headers.get_all('Host') != [f'127.0.0.1:{self.server.server_port}']
                or route not in pages
                or self.headers.get_all('Content-Length') is not None
                or self.headers.get_all('Transfer-Encoding') is not None
                or staging and route == '/receipt' and getattr(self.server, 'staging_submissions', 0) != 1):
            self.send_error(404)
            return
        title, heading, body = pages[route]
        form_action = "'self'" if staging and route == '/draft' else "'none'"
        document = (f'<!doctype html><html lang="en"><meta charset="utf-8">'
                    f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                    f'base-uri \'none\'; form-action {form_action}">'
                    f'<title>{title}</title><h1>{heading}</h1>{body}</html>').encode()
        sent = getattr(self.server, 'fixture_response_bytes', 0)
        if sent + len(document) > FIXTURE_RESPONSE_BYTE_LIMIT:
            self.send_error(429)
            return
        self.server.fixture_response_bytes = sent + len(document)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(document)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(document)
        if staging and route == '/draft':
            self.server.staging_draft_served = True

    def do_POST(self):
        origin = f'http://127.0.0.1:{self.server.server_port}'
        route = self.path
        if getattr(self.server, 'proxy_mode', False):
            parsed = urlsplit(self.path)
            if (parsed.scheme != 'http' or parsed.netloc != f'127.0.0.1:{self.server.server_port}'
                    or self.path != origin + parsed.path):
                self.send_error(404)
                return
            route = parsed.path
        if (getattr(self.server, 'staging_mode', False) is not True or route != '/submit'
                or self.headers.get_all('Host') != [f'127.0.0.1:{self.server.server_port}']
                or self.headers.get_all('Origin') != [origin]
                or self.headers.get_all('Content-Type') != ['application/x-www-form-urlencoded']
                or self.headers.get_all('Content-Length') != [str(len(STAGING_FORM_BODY))]
                or self.headers.get_all('Transfer-Encoding') is not None
                or getattr(self.server, 'staging_draft_served', False) is not True
                or getattr(self.server, 'staging_submissions', 0) != 0):
            self.send_error(404)
            return
        if self.rfile.read(len(STAGING_FORM_BODY)) != STAGING_FORM_BODY:
            self.send_error(404)
            return
        self.server.staging_submissions = 1
        self.send_response(303)
        self.send_header('Location', origin + '/receipt' if getattr(self.server, 'proxy_mode', False)
                         else '/receipt')
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def log_message(self, format, *arguments):
        pass


def reap_owned_children():
    children_path = Path(f'/proc/self/task/{os.getpid()}/children')
    for child_signal in (signal.SIGTERM, signal.SIGKILL):
        for child in children_path.read_text().split():
            descriptor = None
            try:
                descriptor = os.pidfd_open(int(child))
                if child in children_path.read_text().split():
                    signal.pidfd_send_signal(descriptor, child_signal)
            except ProcessLookupError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                return
            time.sleep(.05)
    if children_path.read_text().strip():
        raise ProtocolError('Owned MCP child cleanup timed out')


class MCPBrowser:
    def __init__(self, fixture_request_guard=False, staging_mode=False, fixture_port=None):
        self.fixture_request_guard = fixture_request_guard
        self.staging_mode = staging_mode
        self.fixture_port = fixture_port
        self.fixture_guard_evidence = None
        self.process = None
        self.buffer = b''
        self.counter = 0
        self.references = None
        self.fixture_server = None
        self.fixture_thread = None
        self.fixture_opened = False
        self.fixture_page = None
        self.fixture_snapshot = None
        self.staging_opened = False
        self.staging_page = None
        self.staging_snapshot = None

    def request(self, method, parameters):
        self.counter += 1
        payload = {'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': parameters}
        self.process.stdin.write(json.dumps(payload).encode() + b'\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 8
        for attempt in range(100):
            while b'\n' not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise ProtocolError('MCP response timeout')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ProtocolError('MCP disconnected')
                self.buffer += chunk
                if len(self.buffer) > 1048576:
                    raise ProtocolError('MCP response exceeded bound')
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                response = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise ProtocolError('Malformed MCP JSON') from error
            if not isinstance(response, dict) or response.get('jsonrpc') != '2.0':
                raise ProtocolError('Malformed MCP response')
            if 'id' not in response and isinstance(response.get('method'), str):
                continue
            if (type(response.get('id')) is not int or response['id'] != self.counter
                    or 'error' in response or 'result' not in response):
                raise ProtocolError('MCP response identity or result differs')
            return response['result']
        raise ProtocolError('MCP notification limit exceeded')

    def tool(self, name, arguments):
        if name not in MCP_TOOLS:
            raise ProtocolError('MCP tool not permitted by the fixed adapter')
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        if not isinstance(result, dict) or result.get('isError'):
            raise ProtocolError('MCP tool failed; no retry')
        try:
            return '\n'.join(block['text'] for block in result['content'] if block.get('type') == 'text')
        except (KeyError, TypeError, AttributeError) as error:
            raise ProtocolError('Malformed MCP tool content') from error

    def evaluate(self, expression, argument=None):
        function = '() => (' + expression + ')(' + json.dumps(argument) + ')'
        output = self.tool('browser_evaluate', {'function': function})
        if not output.startswith('### Result\n'):
            raise ProtocolError('MCP evaluation result is absent')
        try:
            value, end = json.JSONDecoder().raw_decode(output[len('### Result\n'):])
        except ValueError as error:
            raise ProtocolError('Malformed MCP evaluation result') from error
        return value

    def start(self, root, fixture, *, gate_source=None):
        if gate_source is not None and self.fixture_request_guard:
            raise ProtocolError('Conflicting MCP page gates')
        if gate_source is not None:
            gate_file = root / 'remote_entry_page_gate.cjs'
            gate_file.write_text(gate_source)
            gate_file.chmod(0o600)
        if self.fixture_request_guard:
            self.fixture_server = HTTPServer(('127.0.0.1', self.fixture_port or 0), FixtureHandler)
            self.fixture_server.staging_mode = self.staging_mode
            self.fixture_server.proxy_mode = self.fixture_port is not None
            self.fixture_guard_evidence = install_fixture_tcp_guard(self.fixture_server.server_port)
            self.fixture_thread = threading.Thread(target=self.fixture_server.serve_forever, daemon=True)
            self.fixture_thread.start()
            origin = f'http://127.0.0.1:{self.fixture_server.server_port}'
            gate_file = root / 'fixture_page_gate.cjs'
            gate_file.write_text(fixture_page_gate_source(origin, self.staging_mode))
            gate_file.chmod(0o600)
        command = ['/usr/local/bin/node', str(root / 'node_modules/@playwright/mcp/cli.js'),
             '--isolated', '--executable-path', '/opt/chromium/chrome', '--no-sandbox',
             '--block-service-workers', '--no-webmcp', '--codegen', 'none',
             '--snapshot-mode', 'full', '--output-dir', str(root / 'output'),
             '--timeout-action', '3000', '--timeout-navigation', '5000', '--timeout-settle', '0']
        if self.fixture_port is not None:
            command.extend(['--proxy-server', f'http://127.0.0.1:{self.fixture_port}'])
        if self.fixture_request_guard or gate_source is not None:
            command.extend(['--init-page', str(gate_file)])
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, start_new_session=True,
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/agent', 'DISPLAY': ':99',
                 'LANG': 'C.UTF-8', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1'})
        initialized = self.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                                 'clientInfo': {'name': 'aos-fixed-desktop-form', 'version': '1'}})
        if initialized.get('serverInfo', {}).get('version') != PLAYWRIGHT_VERSION:
            raise ProtocolError('Pinned MCP server identity differs')
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        self.process.stdin.flush()
        offered = self.request('tools/list', {})
        if not MCP_TOOLS <= {tool['name'] for tool in offered['tools']}:
            raise ProtocolError('Required MCP tools are absent')
        self.tool('browser_navigate', {'url': 'about:blank'})
        self.evaluate('(html) => {document.open(); document.write(html); document.close(); return true;}', fixture)

    def fixture_state(self, page):
        title, heading, unused = FIXTURE_PAGES['/' + page]
        origin = f'http://127.0.0.1:{self.fixture_server.server_port}'
        expected_links = [{'text': 'Details', 'href': origin + '/details'}] if page == 'start' else []
        state = self.evaluate('() => ({url: location.href, title: document.title, '
                              'heading: document.querySelector("h1")?.textContent, '
                              'links: Array.from(document.querySelectorAll("a"), '
                              'anchor => ({text: anchor.textContent, href: anchor.href})), '
                              'other_controls: document.querySelectorAll("button,input,form,iframe,script").length})')
        if state != {'url': origin + '/' + page, 'title': title, 'heading': heading,
                     'links': expected_links, 'other_controls': 0}:
            raise ProtocolError('Local fixture origin, route, or DOM differs')
        return state

    def fixture_observe(self, page):
        self.fixture_snapshot = None
        self.fixture_state(page)
        snapshot = self.tool('browser_snapshot', {})
        links = []
        if page == 'start':
            found = re.findall(r'- link "Details"[^\n]*?\[ref=([a-zA-Z0-9]+)\]', snapshot)
            if len(found) != 1:
                raise ProtocolError('Local fixture link reference differs')
            links = [{'element_id': uuid4().hex, 'role': 'link', 'label': 'Details'}]
            reference = found[0]
        else:
            if re.search(r'- link ', snapshot):
                raise ProtocolError('Unexpected link on local fixture details')
            reference = None
        observed = {'page': page, 'heading': FIXTURE_PAGES['/' + page][1],
                    'snapshot_id': uuid4().hex, 'elements': links}
        self.fixture_page = page
        self.fixture_snapshot = (observed, reference)
        return observed

    def fixture_open(self):
        if (self.fixture_opened or self.process is None or not self.fixture_request_guard
                or self.fixture_guard_evidence is None):
            raise ProtocolError('Local fixture navigation is not available')
        self.fixture_opened = True
        self.references = None
        self.tool('browser_navigate', {'url': f'http://127.0.0.1:{self.fixture_server.server_port}/start'})
        return self.fixture_observe('start')

    def fixture_follow(self, arguments):
        if (not isinstance(arguments, dict) or set(arguments) != {'snapshot_id', 'element_id'}
                or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value)
                       for value in arguments.values())):
            raise ValueError('Invalid local fixture reference')
        if self.fixture_snapshot is None:
            return {'error': 'UI_CHANGED'}
        observed, reference = self.fixture_snapshot
        if (observed['page'] != 'start' or observed['snapshot_id'] != arguments['snapshot_id']
                or observed['elements'][0]['element_id'] != arguments['element_id']):
            self.fixture_snapshot = None
            return {'error': 'UI_CHANGED'}
        try:
            self.fixture_state('start')
        except ProtocolError:
            self.fixture_snapshot = None
            return {'error': 'UI_CHANGED'}
        self.fixture_snapshot = None
        self.tool('browser_click', {'target': reference})
        return self.fixture_observe('details')

    def staging_state(self, page, *, marker=None, mark=False):
        origin = f'http://127.0.0.1:{self.fixture_server.server_port}'
        title, heading, unused = STAGING_PAGES['/' + page]
        result = self.evaluate('(identity) => {const targets = Array.from(document.querySelectorAll('
                               'identity.page === "app" ? "a" : identity.page === "draft" ? "input,button" : "nosuchnode")); '
                               'if (identity.mark) targets.forEach(target => target.setAttribute("data-aos-staging-node", identity.marker)); '
                               'return {node_identity_matches: identity.marker === null || '
                               'targets.every(target => target.getAttribute("data-aos-staging-node") === identity.marker), '
                               'controls_enabled: targets.every(target => !target.disabled && '
                               'target.getAttribute("aria-disabled") !== "true" && !target.closest("[inert]")), '
                               'page_ready: document.readyState === "complete" && '
                               '!document.querySelector("[aria-busy=true],[role=dialog],dialog[open],[aria-modal=true]"), '
                               'url: location.href, title: document.title, '
                               'heading: document.querySelector("h1")?.textContent, '
                               'links: Array.from(document.querySelectorAll("a"), '
                               'anchor => ({text: anchor.textContent, href: anchor.href})), '
                               'forms: Array.from(document.querySelectorAll("form"), '
                               'form => ({method: form.method, action: form.action})), '
                               'inputs: Array.from(document.querySelectorAll("input"), '
                               'input => ({id: input.id, name: input.name, type: input.type, required: input.required})), '
                               'value: document.querySelector("input")?.value ?? "", '
                               'buttons: Array.from(document.querySelectorAll("button"), button => button.textContent), '
                               'receipt: document.querySelector("p")?.textContent ?? "", '
                               'other_controls: document.querySelectorAll("iframe,script,select,textarea").length};}',
                               {'page': page, 'marker': marker, 'mark': mark})
        expected = {'node_identity_matches': True, 'controls_enabled': True, 'page_ready': True,
                    'url': origin + '/' + page, 'title': title, 'heading': heading,
                    'links': [{'text': 'Draft', 'href': origin + '/draft'}] if page == 'app' else [],
                    'forms': [{'method': 'post', 'action': origin + '/submit'}] if page == 'draft' else [],
                    'inputs': [{'id': 'message', 'name': 'message', 'type': 'text', 'required': True}]
                    if page == 'draft' else [],
                    'value': result.get('value') if page == 'draft' else '',
                    'buttons': ['Save draft'] if page == 'draft' else [],
                    'receipt': 'Saved locally.' if page == 'receipt' else '', 'other_controls': 0}
        if (result != expected or page == 'draft' and expected['value'] not in ('', STAGING_MESSAGE)
                or page == 'receipt' and getattr(self.fixture_server, 'staging_submissions', 0) != 1):
            raise ProtocolError('Synthetic staging origin, route, or DOM differs')
        del result['node_identity_matches']
        del result['controls_enabled']
        del result['page_ready']
        return result

    def staging_observe(self, page):
        self.staging_snapshot = None
        marker = uuid4().hex
        state = self.staging_state(page, marker=marker, mark=True)
        snapshot = self.tool('browser_snapshot', {})
        expected = [('link', 'Draft')] if page == 'app' else (
            [('textbox', 'Message'), ('button', 'Save draft')] if page == 'draft' else [])
        elements = []
        references = []
        for role, label in expected:
            found = re.findall(r'- ' + role + ' "' + label + r'"[^\n]*?\[ref=([a-zA-Z0-9]+)\]', snapshot)
            if len(found) != 1:
                raise ProtocolError('Synthetic staging target reference differs')
            elements.append({'element_id': uuid4().hex, 'role': role, 'label': label})
            references.append(found[0])
        observed = {'page': page, 'heading': STAGING_PAGES['/' + page][1],
                    'snapshot_id': uuid4().hex, 'value': state['value'], 'receipt': state['receipt'],
                    'submissions': getattr(self.fixture_server, 'staging_submissions', 0),
                    'elements': elements}
        self.staging_page = page
        self.staging_snapshot = (observed, references, marker)
        return observed

    def staging_open(self):
        if (self.staging_opened or self.process is None or not self.fixture_request_guard
                or not self.staging_mode or self.fixture_guard_evidence is None):
            raise ProtocolError('Synthetic staging app is unavailable')
        self.staging_opened = True
        self.references = None
        self.tool('browser_navigate', {'url': f'http://127.0.0.1:{self.fixture_server.server_port}/app'})
        return self.staging_observe('app')

    def staging_act(self, tool, arguments):
        expected = {'snapshot_id', 'element_id'} | ({'value'} if tool == 'browser.staging.fill' else set())
        if (not isinstance(arguments, dict) or set(arguments) != expected
                or any(not isinstance(value, str) for value in arguments.values())
                or any(not re.fullmatch('[a-f0-9]{32}', arguments[key])
                       for key in ('snapshot_id', 'element_id'))
                or tool == 'browser.staging.fill' and arguments['value'] != STAGING_MESSAGE):
            raise ValueError('Invalid synthetic staging reference')
        if self.staging_snapshot is None:
            return {'error': 'UI_CHANGED'}
        observed, references, marker = self.staging_snapshot
        expected_page = 'app' if tool == 'browser.staging.follow' else 'draft'
        ordinal = 0 if tool != 'browser.staging.submit' else 1
        if (observed['page'] != expected_page or observed['snapshot_id'] != arguments['snapshot_id']
                or observed['elements'][ordinal]['element_id'] != arguments['element_id']
                or tool == 'browser.staging.fill' and observed['value'] != ''
                or tool == 'browser.staging.submit' and observed['value'] != STAGING_MESSAGE):
            self.staging_snapshot = None
            return {'error': 'UI_CHANGED'}
        try:
            current = self.staging_state(expected_page, marker=marker)
        except ProtocolError:
            self.staging_snapshot = None
            return {'error': 'UI_CHANGED'}
        if (current['value'] != observed['value']
                or getattr(self.fixture_server, 'staging_submissions', 0) != 0):
            self.staging_snapshot = None
            return {'error': 'UI_CHANGED'}
        self.staging_snapshot = None
        if tool == 'browser.staging.fill':
            self.tool('browser_fill_form', {'fields': [{'target': references[0], 'name': 'Message',
                                                       'type': 'textbox', 'value': STAGING_MESSAGE}]})
            return self.staging_observe('draft')
        self.tool('browser_click', {'target': references[ordinal]})
        return self.staging_observe('draft' if tool == 'browser.staging.follow' else 'receipt')

    def close(self):
        if self.fixture_server is not None:
            if self.fixture_thread is not None and self.fixture_thread.is_alive():
                self.fixture_server.shutdown()
                self.fixture_thread.join(timeout=2)
            self.fixture_server.server_close()
            self.fixture_server = None
            self.fixture_thread = None
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        finally:
            process.stdout.close()
            self.references = None


def dispatch(browser, request):
    if not isinstance(request, dict) or set(request) != {'tool', 'arguments'}:
        raise ValueError('Invalid request')
    tool, arguments = request['tool'], request['arguments']
    if tool == 'browser.staging.open' and arguments == {}:
        return browser.staging_open()
    if tool == 'browser.staging.snapshot' and arguments == {} and getattr(browser, 'staging_opened', False) is True:
        return browser.staging_observe(browser.staging_page)
    if tool in {'browser.staging.follow', 'browser.staging.fill', 'browser.staging.submit'} and getattr(browser, 'staging_opened', False) is True:
        return browser.staging_act(tool, arguments)
    if getattr(browser, 'staging_opened', False) is True:
        raise ValueError('The fixed form is unavailable after synthetic staging navigation')
    if getattr(browser, 'staging_mode', False) is True and tool != 'browser.observe':
        raise ValueError('Only the synthetic staging entry observation is available')
    if tool == 'browser.fixture.open' and arguments == {}:
        return browser.fixture_open()
    if tool == 'browser.fixture.snapshot' and arguments == {} and getattr(browser, 'fixture_opened', False) is True:
        return browser.fixture_observe(browser.fixture_page)
    if tool == 'browser.fixture.follow' and getattr(browser, 'fixture_opened', False) is True:
        return browser.fixture_follow(arguments)
    if getattr(browser, 'fixture_opened', False) is True:
        raise ValueError('The fixed form is unavailable after local fixture navigation')
    if tool == 'browser.observe' and arguments == {}:
        browser.references = None
        snapshot = browser.tool('browser_snapshot', {})
        references = []
        for role, label in (('textbox', 'Message'), ('button', 'Save locally')):
            found = re.findall(r'- ' + role + ' "' + label + r'"[^\n]*?\[ref=([a-zA-Z0-9]+)\]', snapshot)
            if len(found) != 1:
                raise ProtocolError('MCP form targets differ')
            references.append(found[0])
        observed = browser.evaluate(SNAPSHOT, [uuid4().hex for index in range(3)])
        browser.references = references
        return observed
    if tool == 'browser.verify' and arguments == {}:
        return browser.evaluate(VERIFY)
    if tool not in {'browser.fill', 'browser.submit'} or not isinstance(arguments, dict):
        raise ValueError('Unauthorized tool')
    expected = {'snapshot_id', 'element_id'} | ({'value'} if tool == 'browser.fill' else set())
    if (set(arguments) != expected or any(not isinstance(value, str) for value in arguments.values())
            or not all(re.fullmatch('[a-f0-9]{32}', arguments[key]) for key in ('snapshot_id', 'element_id'))
            or tool == 'browser.fill' and arguments['value'] != 'Hello from the local agent.'):
        raise ValueError('Invalid arguments')
    if browser.references is None:
        return {'error': 'UI_CHANGED'}
    references, browser.references = browser.references, None
    checked = browser.evaluate(PREPARE, {'tool': tool, **arguments})
    if 'error' in checked:
        return checked
    if tool == 'browser.fill':
        browser.tool('browser_fill_form', {'fields': [{'target': references[0], 'name': 'Message',
                                                      'type': 'textbox', 'value': arguments['value']}]})
    else:
        browser.tool('browser_click', {'target': references[1]})
    return {'applied': True}


def main():
    os.umask(0o077)
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        emit({'error': 'RUNTIME_CRASH'})
        return
    guard_mode = sys.argv[3] if len(sys.argv) == 5 else None
    port_argument = sys.argv[4] if len(sys.argv) == 5 else None
    fixture_port = None
    if port_argument != 'auto':
        if (guard_mode not in {'fixture_guard', 'staging_guard'} or not isinstance(port_argument, str)
                or not re.fullmatch('[0-9]{4,5}', port_argument)
                or not 1024 <= int(port_argument) <= 65535
                or str(int(port_argument)) != port_argument):
            emit({'error': 'RUNTIME_CRASH'})
            return
        fixture_port = int(port_argument)
    if guard_mode not in {'fixture_guard', 'staging_guard', 'standard'}:
        emit({'error': 'RUNTIME_CRASH'})
        return
    browser = MCPBrowser(fixture_request_guard=guard_mode in {'fixture_guard', 'staging_guard'},
                         staging_mode=guard_mode == 'staging_guard', fixture_port=fixture_port)
    with tempfile.TemporaryDirectory(prefix='aos-desktop-mcp-', dir='/home/agent') as directory:
        try:
            raw = sys.stdin.buffer.readline(MAX_ARCHIVE * 2 + 1)
            if len(raw) > MAX_ARCHIVE * 2 or not raw.endswith(b'\n'):
                raise ProtocolError('MCP bundle input exceeded bound')
            payload = base64.b64decode(raw.strip(), validate=True)
            if hashlib.sha256(payload).hexdigest() != sys.argv[2]:
                raise ProtocolError('MCP bundle identity differs')
            root = Path(directory)
            unpack_bundle(payload, root / 'node_modules')
            browser.start(root, sys.argv[1])
            emit({'ready': True, 'headed': True, 'display': os.environ.get('DISPLAY'),
                  'network_namespace': os.readlink('/proc/self/ns/net'),
                  'home_visible': Path('/home/cachyos').exists(),
                  'docker_socket_visible': Path('/var/run/docker.sock').exists(),
                  'chromium_sha256': hashlib.sha256(Path('/opt/chromium/chrome').read_bytes()).hexdigest(),
                  'transport': 'playwright_mcp', 'bundle_sha256': sys.argv[2],
                  'server_version': PLAYWRIGHT_VERSION,
                  'fixture_request_guard': browser.fixture_guard_evidence,
                  'page_request_gate': browser.fixture_request_guard,
                  'fixture_proxy_mode': browser.fixture_port is not None,
                  'staging_workflow': browser.staging_mode})
            while raw := sys.stdin.buffer.readline(4097):
                try:
                    if len(raw) > 4096 or not raw.endswith(b'\n'):
                        raise ValueError('Request exceeded bound')
                    emit(dispatch(browser, json.loads(raw)))
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
