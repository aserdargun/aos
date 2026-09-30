import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error, sync_playwright


SNAPSHOT = """identifiers => {
    const field = document.querySelector('#message');
    const button = document.querySelector('#submit');
    const receipt = document.querySelector('#receipt');
    if (!field || !button || !receipt) throw new Error('ELEMENT_MISSING');
    const snapshotId = identifiers[0];
    const fingerprint = JSON.stringify([document.body.innerHTML, field.value]);
    const elements = [field, button].map((element, index) => ({
        element_id: identifiers[index + 1],
        role: element === field ? 'textbox' : 'button',
        label: element === field ? 'Message' : 'Save locally'
    }));
    window.__aosSnapshot = {snapshotId, fingerprint, field, button, elements};
    return {snapshot_id: snapshotId, elements, value: field.value,
            receipt: receipt.textContent, submissions: Number(receipt.dataset.submissions)};
}"""

APPLY = """request => {
    const snapshot = window.__aosSnapshot;
    const field = document.querySelector('#message');
    const button = document.querySelector('#submit');
    const fingerprint = JSON.stringify([document.body.innerHTML, field?.value]);
    if (!snapshot || request.snapshot_id !== snapshot.snapshotId ||
        snapshot.fingerprint !== fingerprint || snapshot.field !== field ||
        snapshot.button !== button) return {error: 'UI_CHANGED'};
    const target = request.tool === 'browser.fill' ? 0 : 1;
    if (request.element_id !== snapshot.elements[target].element_id)
        return {error: 'ELEMENT_MISSING'};
    if (field.disabled || button.disabled || !field.checkVisibility() ||
        !button.checkVisibility()) return {error: 'UI_CHANGED'};
    if (request.tool === 'browser.fill') {
        if (field.value !== '' || request.value !== 'Hello from the local agent.')
            return {error: 'UNSAFE_ACTION'};
        field.value = request.value;
        field.dispatchEvent(new Event('input', {bubbles: true}));
    } else {
        if (field.value !== 'Hello from the local agent.' ||
            document.querySelector('#receipt').dataset.submissions !== '0')
            return {error: 'UNSAFE_ACTION'};
        button.click();
    }
    window.__aosSnapshot = null;
    return {applied: true};
}"""

VERIFY = """() => ({
    value: document.querySelector('#message').value,
    receipt: document.querySelector('#receipt').textContent,
    submissions: Number(document.querySelector('#receipt').dataset.submissions)
})"""


def emit(payload):
    print(json.dumps(payload, allow_nan=False), flush=True)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/browser/chrome-headless-shell",
                                             headless=True, chromium_sandbox=False,
                                             args=["--disable-gpu"], timeout=15000)
        context = browser.new_context(accept_downloads=False, service_workers="block")
        context.route("**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        page.set_default_timeout(5000)
        page.set_content(Path("/fixture.html").read_text(), wait_until="load")
        emit({"ready": True, "browser_version": browser.version,
              "network_namespace": os.readlink("/proc/self/ns/net"),
              "home_visible": Path("/home/cachyos").exists(),
              "docker_socket_visible": Path("/var/run/docker.sock").exists()})
        while raw := sys.stdin.buffer.readline(65537):
            try:
                if len(raw) > 65536:
                    raise ValueError("Oversized request")
                request = json.loads(raw)
                tool = request.get("tool")
                arguments = request.get("arguments", {})
                if set(request) != {"tool", "arguments"}:
                    raise ValueError("Invalid request")
                if tool == "browser.observe" and arguments == {}:
                    result = page.evaluate(SNAPSHOT, [uuid4().hex for _ in range(3)])
                elif tool == "browser.verify" and arguments == {}:
                    result = page.evaluate(VERIFY)
                elif tool in {"browser.fill", "browser.submit"}:
                    expected = {"snapshot_id", "element_id"} | ({"value"} if tool == "browser.fill" else set())
                    if set(arguments) != expected:
                        raise ValueError("Invalid arguments")
                    result = page.evaluate(APPLY, {"tool": tool, **arguments})
                else:
                    raise ValueError("Unauthorized tool")
                emit(result)
            except (ValueError, TypeError):
                emit({"error": "UNSAFE_ACTION"})
            except Error:
                emit({"error": "TOOL_FAILURE"})
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
