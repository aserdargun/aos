import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


SNAPSHOT = """identifiers => {
    window.__aosSnapshot = null;
    const field = document.querySelector('#message');
    const button = document.querySelector('#submit');
    const receipt = document.querySelector('#receipt');
    if (!field || !button || !receipt) throw new Error('ELEMENT_MISSING');
    const fingerprint = JSON.stringify([document.body.innerHTML, field.value]);
    const elements = [field, button].map((element, index) => ({
        element_id: identifiers[index + 1], role: index === 0 ? 'textbox' : 'button',
        label: index === 0 ? 'Message' : 'Save locally'
    }));
    window.__aosSnapshot = {snapshotId: identifiers[0], fingerprint, field, button, elements};
    return {snapshot_id: identifiers[0], elements, value: field.value,
            receipt: receipt.textContent, submissions: Number(receipt.dataset.submissions)};
}"""

PREPARE = """request => {
    const snapshot = window.__aosSnapshot;
    window.__aosSnapshot = null;
    const field = document.querySelector('#message');
    const button = document.querySelector('#submit');
    const receipt = document.querySelector('#receipt');
    if (!snapshot || request.snapshot_id !== snapshot.snapshotId ||
        snapshot.fingerprint !== JSON.stringify([document.body.innerHTML, field?.value]) ||
        snapshot.field !== field || snapshot.button !== button) return {error: 'UI_CHANGED'};
    const target = request.tool === 'browser.fill' ? 0 : 1;
    if (request.element_id !== snapshot.elements[target].element_id)
        return {error: 'ELEMENT_MISSING'};
    if (document.visibilityState !== 'visible' || field.disabled || field.readOnly || button.disabled ||
        !field.checkVisibility() || !button.checkVisibility()) return {error: 'UI_CHANGED'};
    if (target === 0 && (field.value !== '' || request.value !== 'Hello from the local agent.'))
        return {error: 'UNSAFE_ACTION'};
    if (target === 1 && (field.value !== 'Hello from the local agent.' || receipt.dataset.submissions !== '0'))
        return {error: 'UNSAFE_ACTION'};
    const element = target === 0 ? field : button;
    const bounds = element.getBoundingClientRect();
    const point = {x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2};
    if (document.elementFromPoint(point.x, point.y) !== element) return {error: 'UI_CHANGED'};
    return point;
}"""

VERIFY = """() => ({value: document.querySelector('#message').value,
    receipt: document.querySelector('#receipt').textContent,
    submissions: Number(document.querySelector('#receipt').dataset.submissions)})"""


class ProtocolError(Exception):
    pass


class ChromePipe:
    def __init__(self):
        self.process = None
        self.reader = None
        self.writer = None
        self.profile = None
        self.buffer = b''
        self.counter = 0
        self.session = None

    def start(self):
        self.profile = tempfile.mkdtemp(prefix='aos-visible-browser-', dir='/home/agent')
        browser_read, self.writer = os.pipe()
        self.reader, browser_write = os.pipe()
        shim = ('import os,sys; first=os.dup(int(sys.argv[1])); second=os.dup(int(sys.argv[2])); '
                'os.dup2(first,3); os.dup2(second,4); os.closerange(5,1024); '
                'os.execv(sys.argv[3],sys.argv[3:])')
        try:
            self.process = subprocess.Popen(
                ['/usr/bin/python3', '-c', shim, str(browser_read), str(browser_write),
                 '/opt/chromium/chrome', '--no-sandbox', '--disable-dev-shm-usage',
                 '--remote-debugging-pipe', '--disable-background-networking', '--disable-component-update',
                 '--disable-sync', '--no-first-run', '--no-default-browser-check',
                 '--disable-extensions', '--disable-gpu', '--disable-features=Translate,MediaRouter',
                 '--user-data-dir=' + self.profile, '--window-position=0,0', '--window-size=1280,760',
                 '--new-window', 'about:blank'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(browser_read, browser_write), start_new_session=True)
        finally:
            os.close(browser_read)
            os.close(browser_write)

    def call(self, method, parameters=None, *, browser=False):
        self.counter += 1
        request = {'id': self.counter, 'method': method, 'params': parameters or {}}
        if self.session and not browser:
            request['sessionId'] = self.session
        payload = json.dumps(request, allow_nan=False).encode() + b'\0'
        if len(payload) > 32768:
            raise ProtocolError('CDP request exceeded bound')
        os.write(self.writer, payload)
        deadline = time.monotonic() + 8
        for attempt in range(1000):
            while b'\0' not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.reader], [], [], remaining)[0]:
                    raise ProtocolError('CDP timeout')
                chunk = os.read(self.reader, 65536)
                if not chunk:
                    raise ProtocolError('CDP disconnected')
                self.buffer += chunk
                if len(self.buffer) > 1048576:
                    raise ProtocolError('CDP response exceeded bound')
            encoded, self.buffer = self.buffer.split(b'\0', 1)
            response = json.loads(encoded)
            if response.get('id') == self.counter:
                if 'error' in response:
                    raise ProtocolError('CDP rejected operation')
                return response.get('result', {})
        raise ProtocolError('CDP event limit exceeded')

    def evaluate(self, expression, argument=None):
        expression = '(' + expression + ')(' + (json.dumps(argument) if argument is not None else '') + ')'
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if result.get('exceptionDetails') or 'value' not in result.get('result', {}):
            raise ProtocolError('DOM evaluation failed')
        return result['result']['value']

    def close(self):
        for descriptor in (self.writer, self.reader):
            if descriptor is not None:
                os.close(descriptor)
        self.writer = self.reader = None
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
            self.process = None
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None


def dispatch(chrome, request):
    if not isinstance(request, dict) or set(request) != {'tool', 'arguments'}:
        raise ValueError('Invalid request')
    tool, arguments = request['tool'], request['arguments']
    if tool == 'browser.observe' and arguments == {}:
        return chrome.evaluate(SNAPSHOT, [uuid4().hex for _ in range(3)])
    if tool == 'browser.verify' and arguments == {}:
        return chrome.evaluate(VERIFY)
    if tool not in {'browser.fill', 'browser.submit'} or not isinstance(arguments, dict):
        raise ValueError('Unauthorized tool')
    expected = {'snapshot_id', 'element_id'} | ({'value'} if tool == 'browser.fill' else set())
    if set(arguments) != expected:
        raise ValueError('Invalid arguments')
    point = chrome.evaluate(PREPARE, {'tool': tool, **arguments})
    if 'error' in point:
        return point
    chrome.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', **point})
    chrome.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'button': 'left', 'clickCount': 1, **point})
    chrome.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'button': 'left', 'clickCount': 1, **point})
    if tool == 'browser.fill':
        focused = chrome.evaluate("() => document.activeElement === document.querySelector('#message')")
        if not focused:
            return {'error': 'UI_CHANGED'}
        chrome.call('Input.insertText', {'text': 'Hello from the local agent.'})
    return {'applied': True}


def emit(payload):
    print(json.dumps(payload, allow_nan=False), flush=True)


def initialize(chrome, fixture):
    chrome.start()
    version = chrome.call('Browser.getVersion', browser=True)['product']
    deadline = time.monotonic() + 10
    targets = []
    while time.monotonic() < deadline and not targets:
        targets = [target for target in chrome.call('Target.getTargets', browser=True)['targetInfos']
                   if target['type'] == 'page' and target['url'] == 'about:blank']
        if not targets:
            time.sleep(0.05)
    if len(targets) != 1:
        raise ProtocolError('Expected one owned browser page')
    target = targets[0]['targetId']
    chrome.session = chrome.call('Target.attachToTarget', {'targetId': target, 'flatten': True}, browser=True)['sessionId']
    chrome.call('Network.enable')
    chrome.call('Network.setBlockedURLs', {'urls': ['*']})
    chrome.call('Browser.setDownloadBehavior', {'behavior': 'deny'}, browser=True)
    frame = chrome.call('Page.getFrameTree')['frameTree']['frame']['id']
    chrome.call('Page.setDocumentContent', {'frameId': frame, 'html': fixture})
    chrome.call('Page.bringToFront')
    return frame, {'ready': True, 'browser_version': version, 'headed': True, 'display': os.environ.get('DISPLAY'),
                   'network_namespace': os.readlink('/proc/self/ns/net'),
                   'home_visible': Path('/home/cachyos').exists(),
                   'docker_socket_visible': Path('/var/run/docker.sock').exists(),
                   'chromium_sha256': hashlib.sha256(Path('/opt/chromium/chrome').read_bytes()).hexdigest()}


def main():
    chrome = ChromePipe()
    try:
        frame, evidence = initialize(chrome, sys.argv[1])
        emit(evidence)
        while raw := sys.stdin.buffer.readline(4097):
            try:
                if len(raw) > 4096 or not raw.endswith(b'\n'):
                    raise ValueError('Request exceeded bound')
                emit(dispatch(chrome, json.loads(raw)))
            except (ValueError, TypeError, KeyError):
                emit({'error': 'UNSAFE_ACTION'})
    except (ProtocolError, OSError):
        emit({'error': 'RUNTIME_CRASH'})
    finally:
        chrome.close()


if __name__ == '__main__':
    main()
