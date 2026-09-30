import json
from pathlib import Path
import runpy

from playwright.sync_api import sync_playwright


def main():
    worker = runpy.run_path("/worker.py")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/browser/chrome-headless-shell",
                                             headless=True, chromium_sandbox=False, args=["--disable-gpu"])
        context = browser.new_context(viewport={"width": 640, "height": 360}, device_scale_factor=1)
        context.route("**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        page.set_content(Path("/fixture.html").read_text())
        assert page.get_by_role('button').count() == 0
        frame = page.locator('canvas').evaluate_handle(worker['CAPTURE_STATE'])
        page.locator('canvas').evaluate("canvas => canvas.getContext('2d').fillRect(0, 0, 10, 10)")
        assert frame.evaluate(worker['CLICK'], {"x": 470, "y": 230}) == {"error": "UI_CHANGED"}
        frame.dispose()
        frame = page.locator('canvas').evaluate_handle(worker['CAPTURE_STATE'])
        page.locator('canvas').evaluate("canvas => { const replacement = canvas.cloneNode(); replacement.getContext('2d').drawImage(canvas, 0, 0); canvas.replaceWith(replacement); }")
        assert frame.evaluate(worker['CLICK'], {"x": 470, "y": 230}) == {"error": "UI_CHANGED"}
        frame.dispose()
        page.goto('about:blank')
        page.set_content(Path('/fixture.html').read_text())
        frame = page.locator('canvas').evaluate_handle(worker['CAPTURE_STATE'])
        page.set_viewport_size({"width": 800, "height": 600})
        assert frame.evaluate(worker['CLICK'], {"x": 470, "y": 230}) == {"error": "UI_CHANGED"}
        frame.dispose()
        page.set_viewport_size({"width": 640, "height": 360})
        frame = page.locator('canvas').evaluate_handle(worker['CAPTURE_STATE'])
        assert frame.evaluate(worker['CLICK'], {"x": -1, "y": 230}) == {"error": "UNSAFE_ACTION"}
        assert page.evaluate('window.readSyntheticOutcome()') == {"selected": "", "clicks": 0}
        assert frame.evaluate(worker['CLICK'], {"x": 470, "y": 230}) == {"applied": True}
        assert page.evaluate('window.readSyntheticOutcome()') == {"selected": "SAVE", "clicks": 1}
        assert frame.evaluate(worker['CLICK'], {"x": 470, "y": 230}) == {"error": "UI_CHANGED"}
        frame.dispose()
        context.close()
        browser.close()
    print(json.dumps({"passed": ["no_semantic_dom_target", "pixel_mutation", "node_replacement", "viewport_change",
                                 "out_of_frame", "independent_outcome", "no_repeated_click"]}))


if __name__ == "__main__":
    main()
