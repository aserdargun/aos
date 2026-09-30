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
from urllib.parse import urlencode

from aos_desktop_worker import ProtocolError, emit
from aos_mcp_bundle import MAX_ARCHIVE, PLAYWRIGHT_VERSION, unpack_bundle
from aos_mcp_worker_shared import MCPBrowser, https_form_page_gate_source, reap_owned_children


def observation(browser, expected_url):
    value = browser.evaluate('() => ({url: location.href, title: document.title, '
                             'heading: document.querySelector("h1")?.textContent ?? ""})')
    if (not isinstance(value, dict) or set(value) != {'url', 'title', 'heading'}
            or value['url'] != expected_url or any(not isinstance(value[key], str)
                                                   or len(value[key]) > 200 for key in ('title', 'heading'))):
        raise ProtocolError('Form page identity or bounded DOM differs')
    return {'url': value['url'],
            'title_sha256': hashlib.sha256(value['title'].encode()).hexdigest(),
            'heading_sha256': hashlib.sha256(value['heading'].encode()).hexdigest()}


def form_controls(browser, submit_url, fields):
    controls = browser.evaluate('(expectedValues) => {const forms = document.querySelectorAll("form"); '
        'if (forms.length !== 1) return null; const form = forms[0]; '
        'const buttons = Array.from(form.elements).filter(control => '
        '(control.tagName === "BUTTON" || control.tagName === "INPUT") '
        '&& control.type === "submit"); '
        'if (buttons.length !== 1) return null; const button = buttons[0]; '
        'const inputs = Array.from(form.elements).filter(control => control !== button); '
        'const labels = Array.from(document.querySelectorAll("label")); '
        'return {method: form.method, action: form.action, '
        'form_target: form.target, '
        'inputs: inputs.map(input => {const matches = labels.filter('
        'label => input.id && label.htmlFor === input.id); return {'
        'name: input.name, type: input.tagName === "SELECT" ? "select" : '
        'input.tagName === "TEXTAREA" ? "textarea" : input.type, '
        'required: input.required, '
        'disabled: input.matches(":disabled"), multiple: input.multiple === true, '
        'inside_form: form.contains(input), '
        'hidden_value_match: input.type === "hidden" ? '
        'input.value === expectedValues[inputs.indexOf(input)] : false, '
        'option_match_count: input.tagName === "SELECT" ? '
        'Array.from(input.options).filter(option => option.value === '
        'expectedValues[inputs.indexOf(input)] && !option.disabled && '
        '!(option.parentElement?.tagName === "OPTGROUP" && '
        'option.parentElement.disabled)).length : 0, '
        'label: matches.length === 1 ? '
        'matches[0].textContent.trim() : ""};}), '
        'disabled: button.matches(":disabled"), inside_form: form.contains(button), '
        'submit_name: button.name, '
        'submit_override: ["formaction", "formmethod", "formenctype", "formtarget", '
        '"formnovalidate"].some(name => button.hasAttribute(name)), '
        'button: (button.getAttribute("aria-label") || '
        '(button.tagName === "INPUT" ? button.value : button.textContent) || "").trim()};}',
        [field['value'] for field in fields])
    if (not isinstance(controls, dict) or set(controls) != {
            'method', 'action', 'form_target', 'inputs', 'disabled', 'inside_form',
            'submit_name', 'submit_override', 'button'}
            or controls['method'] != 'post' or controls['action'] != submit_url
            or controls['form_target'] not in ('', '_self')
            or not isinstance(controls['inputs'], list)
            or len(controls['inputs']) != len(fields)
            or any(not isinstance(field, dict) or set(field) != {
                'name', 'type', 'required', 'disabled', 'multiple', 'inside_form',
                'hidden_value_match', 'option_match_count', 'label'}
                or field['name'] != expected['name']
                or field['type'] not in {'text', 'email', 'textarea', 'select', 'hidden'}
                or field['required'] is not (field['type'] != 'hidden')
                or field['disabled'] is not False or field['inside_form'] is not True
                or field['multiple'] is not False
                or field['hidden_value_match'] is not (field['type'] == 'hidden')
                or type(field['option_match_count']) is not int
                or field['option_match_count'] != (1 if field['type'] == 'select' else 0)
                or not isinstance(field['label'], str)
                or (field['label'] != '' if field['type'] == 'hidden'
                    else not 0 < len(field['label']) <= 100)
                for field, expected in zip(controls['inputs'], fields))
            or len({field['label'] for field in controls['inputs'] if field['type'] != 'hidden'})
            != sum(field['type'] != 'hidden' for field in controls['inputs'])
            or controls['disabled'] is not False
            or controls['inside_form'] is not True
            or controls['submit_name'] != '' or controls['submit_override'] is not False
            or not isinstance(controls['button'], str) or not 0 < len(controls['button']) <= 100):
        raise ProtocolError('Form controls differ from exact field plan')
    return controls


def reference(snapshot, role, label):
    match = re.search(r'- ' + re.escape(role) + r' "' + re.escape(label)
                      + r'"[^\n]*?\[ref=([a-zA-Z0-9]+)\]', snapshot)
    if match is None:
        raise ProtocolError('Fresh form reference is absent')
    return match.group(1)


def main():
    os.umask(0o077)
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0 or len(sys.argv) != 3:
        emit({'error': 'RUNTIME_CRASH'})
        return
    plan_sha256, bundle_sha256 = sys.argv[1:]
    if (re.fullmatch('[a-f0-9]{64}', plan_sha256) is None
            or re.fullmatch('[a-f0-9]{64}', bundle_sha256) is None):
        emit({'error': 'RUNTIME_CRASH'})
        return
    browser = MCPBrowser()
    with tempfile.TemporaryDirectory(prefix='aos-form-mcp-', dir='/home/agent') as directory:
        try:
            socket_info = Path('/workspace/relay.sock').lstat()
            if (not stat.S_ISSOCK(socket_info.st_mode)
                    or stat.S_IMODE(socket_info.st_mode) != 0o600
                    or socket_info.st_uid != os.getuid()):
                raise ProtocolError('Form relay socket identity differs')
            raw_bundle = sys.stdin.buffer.readline(MAX_ARCHIVE * 2 + 1)
            if len(raw_bundle) > MAX_ARCHIVE * 2 or not raw_bundle.endswith(b'\n'):
                raise ProtocolError('MCP bundle input exceeded bound')
            payload = base64.b64decode(raw_bundle.strip(), validate=True)
            if hashlib.sha256(payload).hexdigest() != bundle_sha256:
                raise ProtocolError('MCP bundle identity differs')
            raw_token = sys.stdin.buffer.readline(66)
            if (len(raw_token) != 65 or not raw_token.endswith(b'\n')
                    or re.fullmatch(b'[a-f0-9]{64}', raw_token[:-1]) is None):
                raise ProtocolError('Form relay client token differs')
            raw_form = sys.stdin.buffer.readline(8193)
            if not raw_form.endswith(b'\n') or len(raw_form) > 8192:
                raise ProtocolError('Form input exceeded bound')
            form = json.loads(raw_form)
            common = {'entry_url', 'submit_url', 'receipt_url', 'body_sha256',
                      'body_bytes', 'confirm_public_plan_sha256'}
            if (not isinstance(form, dict)
                    or set(form) not in (common | {'field_name', 'value'}, common | {'fields'})
                    or type(form['body_bytes']) is not int
                    or form['confirm_public_plan_sha256'] is not None
                    and form['confirm_public_plan_sha256'] != plan_sha256):
                raise ProtocolError('Form input differs')
            fields = ([{'name': form['field_name'], 'value': form['value']}]
                      if 'field_name' in form else form['fields'])
            if (not isinstance(fields, list) or not 1 <= len(fields) <= 8
                    or 'fields' in form and len(fields) < 2
                    or any(not isinstance(field, dict) or set(field) != {'name', 'value'}
                           or not isinstance(field['name'], str)
                           or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', field['name']) is None
                           or not isinstance(field['value'], str)
                           or not 0 < len(field['value']) <= 2048 for field in fields)
                    or len({field['name'] for field in fields}) != len(fields)):
                raise ProtocolError('Form fields differ')
            body = urlencode([(field['name'], field['value']) for field in fields]).encode()
            if (len(body) != form['body_bytes']
                    or hashlib.sha256(body).hexdigest() != form['body_sha256']):
                raise ProtocolError('Form body differs from plan')
            gate = https_form_page_gate_source(
                form['entry_url'], form['submit_url'], form['receipt_url'],
                plan_sha256, raw_token[:-1].decode('ascii'),
                form['confirm_public_plan_sha256'])
            root = Path(directory)
            unpack_bundle(payload, root / 'node_modules')
            browser.start(root, '', gate_source=gate)
            emit({'ready': True, 'headed': True, 'display': os.environ.get('DISPLAY'),
                  'network_namespace': os.readlink('/proc/self/ns/net'),
                  'home_visible': Path('/home/cachyos').exists(),
                  'docker_socket_visible': Path('/var/run/docker.sock').exists(),
                  'chromium_sha256': hashlib.sha256(Path('/opt/chromium/chrome').read_bytes()).hexdigest(),
                  'transport': 'playwright_mcp', 'bundle_sha256': bundle_sha256,
                  'server_version': PLAYWRIGHT_VERSION, 'https_form_gate': True,
                  'plan_sha256': plan_sha256, 'body_sha256': form['body_sha256'],
                  'public_plan_sha256': form['confirm_public_plan_sha256']})
            stage = 0
            tools = ('browser.form.open', 'browser.form.fill', 'browser.form.submit',
                     'browser.form.receipt', 'browser.form.observe')
            while raw := sys.stdin.buffer.readline(4097):
                try:
                    if len(raw) > 4096 or not raw.endswith(b'\n'):
                        raise ValueError('Request exceeded bound')
                    request = json.loads(raw)
                    if (not isinstance(request, dict) or set(request) != {'tool', 'arguments'}
                            or request['arguments'] != {} or request['tool'] != tools[stage]):
                        raise ValueError('Invalid fixed form request')
                    if stage == 0:
                        browser.tool('browser_navigate', {'url': form['entry_url']})
                        emit(observation(browser, form['entry_url']))
                    elif stage == 1:
                        controls = form_controls(browser, form['submit_url'], fields)
                        pending_text_fields = []
                        for control, field in zip(controls['inputs'], fields):
                            if control['type'] == 'select':
                                if pending_text_fields:
                                    snapshot = browser.tool('browser_snapshot', {})
                                    browser.tool('browser_fill_form', {'fields': [{
                                        'target': reference(snapshot, 'textbox', item['label']),
                                        'name': item['label'], 'type': 'textbox',
                                        'value': item['value']}
                                        for item in pending_text_fields]})
                                    pending_text_fields = []
                                snapshot = browser.tool('browser_snapshot', {})
                                browser.tool('browser_select_option', {
                                    'target': reference(snapshot, 'combobox', control['label']),
                                    'values': [field['value']]})
                            elif control['type'] != 'hidden':
                                pending_text_fields.append({**control, 'value': field['value']})
                        if pending_text_fields:
                            snapshot = browser.tool('browser_snapshot', {})
                            browser.tool('browser_fill_form', {'fields': [{
                                'target': reference(snapshot, 'textbox', item['label']),
                                'name': item['label'], 'type': 'textbox', 'value': item['value']}
                                for item in pending_text_fields]})
                        emit({'stage': 'filled', 'body_sha256': form['body_sha256']})
                    elif stage == 2:
                        controls = form_controls(browser, form['submit_url'], fields)
                        filled = browser.evaluate('() => {const controls = Array.from(document.forms[0].elements)'
                                                  '.filter(control => control.tagName === "INPUT" '
                                                  '|| control.tagName === "TEXTAREA" '
                                                  '|| control.tagName === "SELECT")'
                                                  '.filter(control => control.type !== "submit"); '
                                                  'return {values: controls.map(control => control.value), '
                                                  'valid: controls.every(control => control.validity.valid)};}')
                        if filled != {'values': [field['value'] for field in fields], 'valid': True}:
                            raise ProtocolError('Form value changed before submit')
                        snapshot = browser.tool('browser_snapshot', {})
                        browser.tool('browser_click', {
                            'target': reference(snapshot, 'button', controls['button'])})
                        emit({'stage': 'submitted', 'body_sha256': form['body_sha256']})
                    elif stage == 3:
                        browser.tool('browser_navigate', {'url': form['receipt_url']})
                        emit(observation(browser, form['receipt_url']))
                    else:
                        emit(observation(browser, form['receipt_url']))
                    stage += 1
                except (ValueError, TypeError, KeyError, IndexError, ProtocolError):
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
