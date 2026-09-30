import base64
import hashlib
import json
import sys
from uuid import uuid4

from aos_desktop_worker import ChromePipe, ProtocolError, emit, initialize


BIND_FRAME = """() => {
    const canvas = document.querySelector('canvas');
    if (!canvas || canvas.width !== 640 || canvas.height !== 360) return {error: 'UI_CHANGED'};
    const rect = canvas.getBoundingClientRect();
    if (rect.x !== 0 || rect.y !== 0 || rect.width !== 640 || rect.height !== 360 ||
        innerWidth < 640 || innerHeight < 360 || devicePixelRatio !== 1 || scrollX !== 0 || scrollY !== 0 ||
        visualViewport.scale !== 1 || document.visibilityState !== 'visible' ||
        !canvas.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return {error: 'UI_CHANGED'};
    for (const point of [[1, 1], [639, 1], [1, 359], [639, 359], [320, 180]]) {
        if (document.elementFromPoint(...point) !== canvas) return {error: 'UI_CHANGED'};
    }
    const styles = [document.documentElement, document.body, canvas].map(element => {
        const style = getComputedStyle(element);
        return Array.from(style).map(name => [name, style.getPropertyValue(name)]);
    });
    const geometry = {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    const fingerprint = JSON.stringify([canvas.toDataURL(), document.documentElement.outerHTML,
        innerWidth, innerHeight, devicePixelRatio, scrollX, scrollY, visualViewport.scale, geometry, styles]);
    return {canvas, geometry, fingerprint};
}"""

CAPTURE_FRAME = """() => {
    const current = (BIND_FRAME)();
    if (current.error) {window.__aosVisionFrame = null; return current;}
    window.__aosVisionFrame = current;
    return current.geometry;
}""".replace('BIND_FRAME', BIND_FRAME)

CHECK_FRAME = """(point = null) => {
    const stored = window.__aosVisionFrame;
    const current = (BIND_FRAME)();
    if (!stored || current.error || stored.canvas !== current.canvas ||
        stored.fingerprint !== current.fingerprint) return {error: 'UI_CHANGED'};
    if (point !== null) {
        if (!Number.isInteger(point.x) || !Number.isInteger(point.y) || point.x < 0 || point.x >= 640 ||
            point.y < 0 || point.y >= 360 || window.readSyntheticOutcome().clicks !== 0)
            return {error: 'UNSAFE_ACTION'};
        if (document.elementFromPoint(current.geometry.x + point.x, current.geometry.y + point.y) !== current.canvas)
            return {error: 'UI_CHANGED'};
    }
    return current.geometry;
}""".replace('BIND_FRAME', BIND_FRAME)


class VisionSession:
    def __init__(self, chrome):
        self.chrome = chrome
        self.capture_id = None
        self.png_sha256 = None

    def screenshot(self, geometry):
        encoded = self.chrome.call('Page.captureScreenshot', {
            'format': 'png', 'fromSurface': True, 'captureBeyondViewport': False,
            'clip': {**geometry, 'scale': 1}})['data']
        if not isinstance(encoded, str) or len(encoded) > 60000:
            raise ProtocolError('Rendered capture exceeded bound')
        payload = base64.b64decode(encoded, validate=True)
        return encoded, hashlib.sha256(payload).hexdigest()

    def capture(self):
        self.capture_id = self.png_sha256 = None
        geometry = self.chrome.evaluate(CAPTURE_FRAME)
        if 'error' in geometry:
            return geometry
        encoded, checksum = self.screenshot(geometry)
        checked = self.chrome.evaluate(CHECK_FRAME, None)
        if 'error' in checked:
            return checked
        self.capture_id = uuid4().hex
        self.png_sha256 = checksum
        return {'capture_id': self.capture_id, 'width': 640, 'height': 360,
                'sha256': checksum, 'image_base64': encoded}

    def click(self, arguments):
        if self.capture_id is None or arguments['capture_id'] != self.capture_id:
            return {'error': 'UI_CHANGED'}
        self.capture_id = None
        point = {'x': arguments['x'], 'y': arguments['y']}
        geometry = self.chrome.evaluate(CHECK_FRAME, point)
        if 'error' in geometry:
            return geometry
        encoded, checksum = self.screenshot(geometry)
        if checksum != self.png_sha256:
            return {'error': 'UI_CHANGED'}
        geometry = self.chrome.evaluate(CHECK_FRAME, point)
        if 'error' in geometry:
            return geometry
        viewport_point = {'x': geometry['x'] + point['x'], 'y': geometry['y'] + point['y']}
        self.chrome.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', **viewport_point})
        self.chrome.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'button': 'left', 'clickCount': 1, **viewport_point})
        self.chrome.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'button': 'left', 'clickCount': 1, **viewport_point})
        return {'applied': True}

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {'tool', 'arguments'}:
            raise ValueError('Invalid request')
        tool, arguments = request['tool'], request['arguments']
        if tool == 'vision.capture' and arguments == {}:
            return self.capture()
        if tool == 'vision.verify' and arguments == {}:
            return self.chrome.evaluate('() => window.readSyntheticOutcome()')
        if tool == 'vision.click' and isinstance(arguments, dict) and set(arguments) == {'capture_id', 'x', 'y'}:
            return self.click(arguments)
        raise ValueError('Only the fixed captured scene is authorized')


def main():
    chrome = ChromePipe()
    try:
        frame, evidence = initialize(chrome, sys.argv[1])
        session = VisionSession(chrome)
        emit({**evidence, 'capture_source': 'rendered_canvas_crop'})
        while raw := sys.stdin.buffer.readline(4097):
            try:
                if len(raw) > 4096 or not raw.endswith(b'\n'):
                    raise ValueError('Request exceeded bound')
                emit(session.dispatch(json.loads(raw)))
            except (ValueError, TypeError, KeyError):
                emit({'error': 'UNSAFE_ACTION'})
    except (ProtocolError, OSError):
        emit({'error': 'RUNTIME_CRASH'})
    finally:
        chrome.close()


if __name__ == '__main__':
    main()
