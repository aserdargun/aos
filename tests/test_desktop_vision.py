import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from aos.browser import BrowserRuntime
from aos.contracts import AOSFault, ErrorCode, REPO_ROOT, Settings, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop import DOCKER, DesktopRuntime
from aos.desktop_browser import DesktopBrowserRuntime
from aos.desktop_vision import DesktopVisionRuntime
from aos.storage import TrajectoryStore
from aos.vision import Capture, FixtureVisionSupervisor, VISION_EXPECTED, VISION_SCOPE, VisionRuntime, VisionScene
from aos.vision_operator import VisionOperator


class DesktopVisionContractTests(unittest.TestCase):
    def runtime(self):
        desktop = Mock(manifest=Path('/synthetic/manifest'), runtime_id='desktop-original', container_id=None)
        return DesktopVisionRuntime(desktop)

    def test_compatible_vision_contract_and_composite_worker_identity(self):
        runtime = self.runtime()
        self.assertIsInstance(runtime, VisionRuntime)
        self.assertIsInstance(runtime, DesktopBrowserRuntime)
        self.assertIsInstance(runtime, BrowserRuntime)
        self.assertEqual(runtime.scope, VISION_SCOPE)
        self.assertEqual(runtime.fixture_file, 'vision_canvas.html')
        self.assertTrue(runtime.status()['vision'])
        self.assertEqual(runtime.status()['scope'], VISION_SCOPE)
        self.assertEqual(runtime.status()['worker_sha256'], hashlib.sha256(runtime.worker_source().encode()).hexdigest())
        self.assertIsNone(runtime.capture)
        self.assertIsNone(runtime.scene)
        runtime.desktop.docker.assert_not_called()

    def test_arbitrary_browser_tools_and_unbound_click_do_not_reach_transport(self):
        runtime = self.runtime()
        with patch.object(BrowserRuntime, 'perform') as perform:
            for tool, arguments in (('browser.observe', {}), ('browser.fill', {'value': 'private'}),
                                    ('vision.click', {'capture_id': '1' * 32, 'x': 470, 'y': 230}),
                                    ('vision.evaluate', {'script': 'anything'})):
                with self.subTest(tool=tool), self.assertRaises(AOSFault):
                    runtime.perform(tool, arguments)
            perform.assert_not_called()

    def test_unstarted_parent_blocks_launch_read_and_scene_binding(self):
        runtime = self.runtime()
        with patch('aos.desktop_browser.subprocess.Popen') as launch:
            for operation in (runtime.start, lambda: runtime.read(VISION_SCOPE), lambda: runtime.bind_scene(None, 0)):
                with self.assertRaises(AOSFault):
                    operation()
            launch.assert_not_called()


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Opt in to real owned desktop vision')
class DesktopVisionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='visible-vision-test-', dir=REPO_ROOT / 'data')
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.desktop = DesktopRuntime(root / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(self.desktop.stop)
        self.desktop.start()
        self.runtime = DesktopVisionRuntime(self.desktop)
        self.addCleanup(self.runtime.stop)
        self.runtime.start()

    def scene(self, state_version=0):
        capture = Capture.model_validate_json(self.runtime.read(VISION_SCOPE))
        scene = asyncio.run(FixtureVisionSupervisor().describe(capture, state_version))
        self.runtime.bind_scene(scene, state_version)
        target = next(key for key, element in scene.targets().items() if element.label == 'SAVE')
        return capture, scene, {'capture_id': capture.capture_id, 'element_id': target,
                                'scene_sha256': digest(scene.model_dump())}

    def test_rendered_capture_symbolic_save_and_independent_verification(self):
        settings = Settings(workspace=Path(self.temporary.name) / 'unused',
                            database=Path(self.temporary.name) / 'test.sqlite')
        store = TrajectoryStore(settings.database)
        self.addCleanup(store.close)
        operator = VisionOperator(settings, store, self.runtime, FixtureDecisionEngine(), FixtureVisionSupervisor())
        result = asyncio.run(operator.canvas())
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(result['real_model'])
        self.assertEqual(self.runtime.perform('vision.verify', {}), VISION_EXPECTED)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
        capture = self.runtime.capture
        self.assertEqual(hashlib.sha256(capture.image_bytes()).hexdigest(), capture.sha256)
        windows = self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/xdotool',
                                      'search', '--onlyvisible', '--name', 'AOS synthetic visual task'])
        self.assertTrue(windows.strip())
        self.assertEqual(self.runtime.status()['isolation']['capture_source'], 'rendered_canvas_crop')
        self.runtime.stop()
        self.assertIsNone(self.runtime.capture)
        self.assertIsNone(self.runtime.scene)
        self.assertTrue(self.desktop.perform('probe', {})['display'])
        processes = self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/ps', '-eo', 'args']).decode()
        self.assertNotIn('--remote-debugging-pipe', processes)

    def test_stale_capture_scene_cancel_target_and_duplicate_click_are_denied(self):
        capture, scene, arguments = self.scene(4)
        cancel = next(key for key, element in scene.targets().items() if element.label == 'CANCEL')
        with self.assertRaises(AOSFault) as caught:
            self.runtime.perform('vision.click', {**arguments, 'element_id': cancel})
        self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
        with self.assertRaises(AOSFault):
            self.runtime.perform('vision.click', {**arguments, 'scene_sha256': '0' * 64})
        self.runtime.read(VISION_SCOPE)
        with self.assertRaises(AOSFault):
            self.runtime.bind_scene(scene, 4)
        with self.assertRaises(AOSFault):
            self.runtime.perform('vision.click', arguments)
        capture, scene, arguments = self.scene(5)
        self.runtime.perform('vision.click', arguments)
        with self.assertRaises(AOSFault):
            self.runtime.perform('vision.click', arguments)
        self.assertEqual(self.runtime.perform('vision.verify', {}), VISION_EXPECTED)

    def test_wrong_scene_localization_is_not_replaced_with_hardcoded_save(self):
        class WrongBoxes(FixtureVisionSupervisor):
            async def describe(self, capture, state_version):
                scene = (await super().describe(capture, state_version)).model_dump()
                scene['elements'][0]['bbox'], scene['elements'][1]['bbox'] = (
                    scene['elements'][1]['bbox'], scene['elements'][0]['bbox'])
                return VisionScene.model_validate(scene)

        settings = Settings(workspace=Path(self.temporary.name) / 'unused',
                            database=Path(self.temporary.name) / 'wrong-scene.sqlite')
        store = TrajectoryStore(settings.database)
        self.addCleanup(store.close)
        operator = VisionOperator(settings, store, self.runtime, FixtureDecisionEngine(), WrongBoxes())
        result = asyncio.run(operator.canvas())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['verified'])
        self.assertEqual(self.runtime.perform('vision.verify', {}), {'selected': 'CANCEL', 'clicks': 1})
        self.assertEqual(store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'failed')

    def test_live_render_binding_rejects_dom_pixels_geometry_overlay_and_capture_races(self):
        self.runtime.stop()
        checks = '''        mutations = [
            "document.querySelector('canvas').replaceWith(document.querySelector('canvas').cloneNode())",
            "document.querySelector('canvas').getContext('2d').fillRect(0, 0, 40, 40)",
            "document.querySelector('canvas').style.transform = 'translateX(1px)'",
            "document.querySelector('canvas').style.visibility = 'hidden'",
            "document.querySelector('canvas').style.opacity = '0.1'",
            "document.body.insertAdjacentHTML('beforeend', '<div style=\\"position:fixed;inset:0;z-index:99\\"></div>')",
            "document.styleSheets[0].insertRule('canvas { filter: invert(1); }', 0)",
        ]
        for mutation in mutations:
            chrome.call('Page.setDocumentContent', {'frameId': frame, 'html': sys.argv[1]})
            captured = session.capture()
            assert 'capture_id' in captured, captured
            chrome.evaluate('() => {' + mutation + '; return true;}')
            assert session.click({'capture_id': captured['capture_id'], 'x': 470, 'y': 230}) == {'error': 'UI_CHANGED'}
            assert chrome.evaluate('() => window.readSyntheticOutcome()') == {'selected': '', 'clicks': 0}
        chrome.call('Page.setDocumentContent', {'frameId': frame, 'html': sys.argv[1]})
        screenshot = session.screenshot
        def changed_capture(geometry):
            result = screenshot(geometry)
            chrome.evaluate("() => {document.querySelector('canvas').getContext('2d').fillRect(0,0,20,20); return true;}")
            return result
        session.screenshot = changed_capture
        assert session.capture() == {'error': 'UI_CHANGED'}
        assert session.capture_id is None
        session.screenshot = screenshot
        chrome.call('Page.setDocumentContent', {'frameId': frame, 'html': sys.argv[1]})
        capture = session.capture()
        actual = base64.b64decode(capture['image_base64'], validate=True)
        assert len(actual) > 1000 and actual[:8] == b'\\x89PNG\\r\\n\\x1a\\n'
        assert hashlib.sha256(actual).hexdigest() == capture['sha256']
        assert session.click({'capture_id': capture['capture_id'], 'x': 640, 'y': 230}) == {'error': 'UNSAFE_ACTION'}
        assert chrome.evaluate('() => window.readSyntheticOutcome()') == {'selected': '', 'clicks': 0}
        emit({'boundary_checks': len(mutations) + 3})
'''
        marker = '        while raw := sys.stdin.buffer.readline(4097):'
        before, after = self.runtime.worker_source().rsplit(marker, 1)
        source = before + checks + marker + after
        result = subprocess.run([*DOCKER, 'exec', '-i', self.desktop.container_id,
                                 '/usr/bin/python3', '-u', '-c', source,
                                 (REPO_ROOT / 'examples/vision_canvas.html').read_text()],
                                input=b'', capture_output=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        payloads = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(payloads[-1], {'boundary_checks': 10})
