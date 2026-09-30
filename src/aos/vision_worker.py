import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from playwright.sync_api import Error, sync_playwright


CAPTURE_STATE = """canvas => ({canvas, pixels: canvas.toDataURL(),
    markup: document.documentElement.outerHTML, width: innerWidth, height: innerHeight})"""
CLICK = """(frame, point) => {
    const canvas = document.querySelector('canvas');
    if (frame.canvas !== canvas || frame.pixels !== canvas.toDataURL() ||
        frame.markup !== document.documentElement.outerHTML || frame.width !== innerWidth ||
        frame.height !== innerHeight || innerWidth !== 640 || innerHeight !== 360 ||
        !canvas.checkVisibility()) return {error: 'UI_CHANGED'};
    if (!Number.isInteger(point.x) || !Number.isInteger(point.y) ||
        point.x < 0 || point.x >= 640 || point.y < 0 || point.y >= 360 ||
        window.readSyntheticOutcome().clicks !== 0) return {error: 'UNSAFE_ACTION'};
    canvas.dispatchEvent(new MouseEvent('click', {clientX: point.x, clientY: point.y, bubbles: true}));
    return {applied: true};
}"""


def emit(payload):
    print(json.dumps(payload, allow_nan=False), flush=True)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/browser/chrome-headless-shell",
                                             headless=True, chromium_sandbox=False,
                                             args=["--disable-gpu"], timeout=15000)
        context = browser.new_context(viewport={"width": 640, "height": 360}, device_scale_factor=1,
                                      accept_downloads=False, service_workers="block")
        context.route("**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        page.set_default_timeout(5000)
        page.set_content(Path("/fixture.html").read_text(), wait_until="load")
        emit({"ready": True, "browser_version": browser.version,
              "network_namespace": os.readlink("/proc/self/ns/net"),
              "home_visible": Path("/home/cachyos").exists(),
              "docker_socket_visible": Path("/var/run/docker.sock").exists()})
        frame = None
        capture_id = None
        while raw := sys.stdin.buffer.readline(65537):
            try:
                request = json.loads(raw)
                if len(raw) > 65536 or set(request) != {"tool", "arguments"}:
                    raise ValueError("Invalid request")
                tool, arguments = request["tool"], request["arguments"]
                if tool == "vision.capture" and arguments == {}:
                    if frame is not None:
                        frame.dispose()
                    frame = page.locator('canvas').evaluate_handle(CAPTURE_STATE)
                    png = page.screenshot(type="png", animations="disabled")
                    unchanged = frame.evaluate("frame => frame.pixels === frame.canvas.toDataURL() && frame.markup === document.documentElement.outerHTML")
                    if not unchanged:
                        emit({"error": "UI_CHANGED"})
                        capture_id = None
                        continue
                    capture_id = uuid4().hex
                    result = {"capture_id": capture_id, "width": 640, "height": 360,
                              "sha256": hashlib.sha256(png).hexdigest(), "image_base64": base64.b64encode(png).decode()}
                elif tool == "vision.click" and set(arguments) == {"capture_id", "x", "y"}:
                    if frame is None or capture_id is None or arguments["capture_id"] != capture_id:
                        result = {"error": "UI_CHANGED"}
                    else:
                        result = frame.evaluate(CLICK, {"x": arguments["x"], "y": arguments["y"]})
                        capture_id = None
                elif tool == "vision.verify" and arguments == {}:
                    result = page.evaluate("window.readSyntheticOutcome()")
                else:
                    raise ValueError("Unauthorized tool")
                emit(result)
            except (ValueError, TypeError):
                emit({"error": "UNSAFE_ACTION"})
            except Error:
                emit({"error": "TOOL_FAILURE"})
        if frame is not None:
            frame.dispose()
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
