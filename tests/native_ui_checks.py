import argparse
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import gi

gi.require_version('Atspi', '2.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkX11', '3.0')
from gi.repository import Atspi, Gdk, GdkX11, GLib


class NativeUI:
    def __init__(self, process, database, engine='fixture'):
        self.process = process
        self.database = database
        self.engine = engine

    def persisted(self, query, expected):
        self.wait(lambda: self.scalar(query) == expected, 'persisted ' + str(expected))

    def scalar(self, query):
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        try:
            row = connection.execute(query).fetchone()
            return row[0] if row is not None else None
        finally:
            connection.close()

    def job_status(self, expected):
        self.persisted('SELECT status FROM desktop_tasks ORDER BY rowid DESC LIMIT 1', expected)

    def nodes(self):
        assert self.process.poll() is None, 'Native process exited'
        roots = [app for app in Atspi.get_desktop(0) if app is not None and app.get_process_id() == self.process.pid]
        result = []
        while roots:
            node = roots.pop()
            if node is None:
                continue
            node.clear_cache()
            result.append(node)
            roots.extend(node.get_child_at_index(index) for index in reversed(range(node.get_child_count())))
        return result

    def wait(self, predicate, description, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
            try:
                result = predicate()
            except GLib.GError:
                result = None
            if result:
                return result
            time.sleep(.1)
        self.screenshot('aos-native-timeout.png')
        raise AssertionError('Native UI timed out: ' + description + '\n' + self.text())

    def find(self, role, name):
        return next((node for node in self.nodes() if node.get_role_name() == role and node.get_name() == name), None)

    def text(self):
        result = []
        for node in self.nodes():
            if node.get_role_name() == 'password text':
                continue
            result.append(node.get_role_name() + ': ' + node.get_name())
            if 'Text' in node.get_interfaces():
                result.append(Atspi.Text.get_text(node, 0, -1))
        return '\n'.join(result)

    def contains(self, text, timeout=30):
        self.wait(lambda: text in self.text(), text, timeout)

    def click(self, name, role='button'):
        node = self.wait(lambda: self.find(role, name), name)
        self.wait(lambda: node.get_state_set().contains(Atspi.StateType.ENABLED), name + ' enabled')
        assert node.get_action_iface().do_action(0), 'Native action rejected: ' + name

    def fill(self, name, value):
        entry = self.wait(lambda: next((node for node in self.nodes()
                                       if node.get_name() == name and node.get_role_name() == 'entry'), None), name)
        assert entry.get_component_iface().grab_focus()
        self.type_into(entry, value, clear=True)
        self.wait(lambda: Atspi.Text.get_text(entry, 0, -1) == value, name + ' value')

    def select_task(self, initial, button):
        entry = self.wait(lambda: self.find('combo box', 'Görev türü'), 'task selector')
        self.type_into(entry, initial + '\n')
        self.wait(lambda: self.find('button', button), 'selected task button')

    def show_paragraph(self, text):
        node = self.wait(lambda: next((candidate for candidate in self.nodes()
                                      if candidate.get_role_name() == 'paragraph' and 'Text' in candidate.get_interfaces()
                                      and text in Atspi.Text.get_text(candidate, 0, -1)), None), text)
        assert node.get_component_iface().scroll_to(Atspi.ScrollType.ANYWHERE), 'Native scroll rejected: ' + text
        time.sleep(.2)

    def remote_canvas(self):
        canvas = self.wait(lambda: next((node for node in self.nodes()
                                        if node.get_name() == 'İzole masaüstü tuvali'
                                        and 'Component' in node.get_interfaces()), None), 'owned remote canvas')
        assert canvas.get_component_iface().scroll_to(Atspi.ScrollType.TOP_EDGE), 'Native canvas scroll rejected'

        def visible_bounds():
            canvas.clear_cache()
            bounds = canvas.get_component_iface().get_extents(Atspi.CoordType.WINDOW)
            geometry = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), self.window_id()).get_geometry()
            return (bounds.width > 64 and bounds.height > 64
                    and abs(bounds.width / bounds.height - 1280 / 800) < .02
                    and 0 <= bounds.x and bounds.x + bounds.width <= geometry.width
                    and 0 <= bounds.y and bounds.y + bounds.height <= geometry.height)

        self.wait(visible_bounds, 'remote canvas visible in owned native window')
        return canvas

    def window_id(self):
        library = ctypes.CDLL('libX11.so.6')
        library.XOpenDisplay.restype = ctypes.c_void_p
        library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        library.XDefaultRootWindow.restype = ctypes.c_ulong
        library.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint)]
        library.XFree.argtypes = [ctypes.c_void_p]
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = library.XOpenDisplay(None)
        assert display
        children = ctypes.POINTER(ctypes.c_ulong)()
        try:
            root, parent, count = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_uint()
            assert library.XQueryTree(display, library.XDefaultRootWindow(display), ctypes.byref(root), ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count))
            for index in range(count.value):
                window_id = children[index]
                owner = subprocess.check_output(['xprop', '-id', hex(window_id), '_NET_WM_PID'], text=True)
                if owner.strip().endswith('= ' + str(self.process.pid)):
                    candidate = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), window_id)
                    geometry = candidate.get_geometry()
                    if geometry.width >= 800 and geometry.height >= 600:
                        return window_id
        finally:
            library.XFree(children)
            library.XCloseDisplay(display)
        raise AssertionError('Owned native X11 window not found')

    def type_into(self, entry, text, clear=False):
        assert os.environ.get('AOS_NATIVE_PRIVATE_DISPLAY') == '1', 'Keyboard input requires private X11'
        assert entry is not None, 'Keyboard input requires an explicit accessible target'
        assert entry.get_component_iface().grab_focus()
        self.wait(lambda: entry.get_state_set().contains(Atspi.StateType.FOCUSED), 'input focus')
        library = ctypes.CDLL('libX11.so.6')
        library.XOpenDisplay.restype = ctypes.c_void_p
        library.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        library.XKeysymToKeycode.restype = ctypes.c_uint
        library.XKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        library.XKeycodeToKeysym.restype = ctypes.c_ulong
        library.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        library.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        library.XFlush.argtypes = [ctypes.c_void_p]
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        library.XGetKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        library.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        library.XChangeKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        library.XFree.argtypes = [ctypes.c_void_p]
        keyboard = ctypes.CDLL('libXtst.so.6')
        keyboard.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        display = library.XOpenDisplay(None)
        assert display
        width = ctypes.c_int()
        original_mapping = library.XGetKeyboardMapping(display, 255, 1, ctypes.byref(width))
        assert original_mapping
        try:
            target = self.window_id()
            library.XSetInputFocus(display, target, 2, 0)
            library.XFlush(display)
            time.sleep(.2)
            shift_code = library.XKeysymToKeycode(display, 0xffe1)
            if clear:
                control_code = library.XKeysymToKeycode(display, 0xffe3)
                select_code = library.XKeysymToKeycode(display, ord('a'))
                keyboard.XTestFakeKeyEvent(display, control_code, 1, 0)
                keyboard.XTestFakeKeyEvent(display, select_code, 1, 0)
                keyboard.XTestFakeKeyEvent(display, select_code, 0, 0)
                keyboard.XTestFakeKeyEvent(display, control_code, 0, 0)
            for character in text:
                focused, revert = ctypes.c_ulong(), ctypes.c_int()
                library.XGetInputFocus(display, ctypes.byref(focused), ctypes.byref(revert))
                assert focused.value == target, f'Native window lost keyboard focus; typing stopped ({focused.value:#x} != {target:#x})'
                keysym = 0xff0d if character == '\n' else ord(character) if ord(character) <= 255 else 0x01000000 | ord(character)
                keycode = library.XKeysymToKeycode(display, keysym)
                if not keycode:
                    mapping = (ctypes.c_ulong * width.value)(*([keysym] * width.value))
                    library.XChangeKeyboardMapping(display, 255, width.value, mapping, 1)
                    library.XFlush(display)
                    time.sleep(.05)
                    keycode, shift = 255, False
                else:
                    shift = library.XKeycodeToKeysym(display, keycode, 0) != keysym
                    assert library.XKeycodeToKeysym(display, keycode, int(shift)) == keysym
                if shift:
                    keyboard.XTestFakeKeyEvent(display, shift_code, 1, 0)
                keyboard.XTestFakeKeyEvent(display, keycode, 1, 0)
                keyboard.XTestFakeKeyEvent(display, keycode, 0, 0)
                if shift:
                    keyboard.XTestFakeKeyEvent(display, shift_code, 0, 0)
                library.XFlush(display)
                time.sleep(.01)
        finally:
            library.XChangeKeyboardMapping(display, 255, width.value, original_mapping, 1)
            library.XFree(original_mapping)
            library.XFlush(display)
            library.XCloseDisplay(display)

    def screenshot(self, name):
        window = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), self.window_id())
        geometry = window.get_geometry()
        pixels = Gdk.pixbuf_get_from_window(window, 0, 0, geometry.width, geometry.height)
        assert pixels is not None
        path = Path('/tmp') / (name.replace('aos-native-', 'aos-native-real-') if self.engine == 'decider' else name)
        pixels.savev(str(path), 'png', [], [])
        return {'path': str(path), 'width': geometry.width, 'height': geometry.height}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--engine', choices=('fixture', 'decider'), default='fixture')
    arguments = parser.parse_args()
    token = sys.stdin.read().strip()
    assert token and os.environ.get('AOS_NATIVE_PRIVATE_DISPLAY') == '1', 'Local token and private test X11 display are required'
    environment = {**os.environ, 'GDK_BACKEND': 'x11', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1', 'GTK_A11Y': 'always'}
    environment.pop('TAURI_WEBVIEW_AUTOMATION', None)
    with arguments.log.open('w') as output:
        process = subprocess.Popen([str(arguments.binary)], env=environment, stdout=output, stderr=output)
        try:
            ui = NativeUI(process, arguments.database, arguments.engine)
            ui.click('Türkçe', role='toggle button')
            entry = ui.wait(lambda: ui.find('password text', 'Yerel oturum anahtarı'), 'login form')
            document = ui.find('document web', 'AOS · Kontrol merkezi')
            assert document is not None
            attributes = document.get_document_iface().get_document_attributes()
            assert 'http://127.0.0.1:8765/ui/' in attributes.values(), attributes
            ui.type_into(entry, token)
            time.sleep(.3)
            ui.click('Giriş yap')
            ui.contains('Canlı · 1280 × 800')
            screenshots = [ui.screenshot('aos-native-computer.png')]
            ui.click('Sentetik giriş testi')
            ui.contains('type_note')
            ui.persisted('SELECT status FROM desktop_inputs ORDER BY rowid DESC LIMIT 1', 'ok')
            ui.click('Kontrolü al')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'HUMAN')
            ui.contains('Klavye ve fare yalnız izole masaüstüne gider.')
            ui.contains('Canlı · 1280 × 800')
            ui.type_into(ui.remote_canvas(), ' native WebKit')

            def remote_note_matches():
                result = subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'exec', '-i', arguments.container,
                                         '/usr/bin/python3', '/opt/aos/tools.py'], input='{"tool":"read_note","arguments":{}}\n',
                                        text=True, capture_output=True, check=True, timeout=5)
                return json.loads(result.stdout)['text'] == 'AOS desktop input native WebKit'

            ui.wait(remote_note_matches, 'native VNC keyboard independent readback')
            screenshots.append(ui.screenshot('aos-native-human-input.png'))
            ui.click('Ajana geri ver')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'AGENT')
            ui.click('Görevler')
            ui.contains('SENTETİK TEST MOTORU' if arguments.engine == 'fixture' else 'Gerçek yerel Decider')
            ui.fill('Türkçe görev metni', 'hello görevini hazırla')
            ui.click('Yalnız önizle')
            ui.contains('Şablon tanındı:')
            ui.contains('Görev başlatılmadı.')
            ui.persisted('SELECT count(*) FROM desktop_tasks', 0)
            ui.fill('Türkçe görev metni', 'hello görevini başlatma')
            ui.click('Yalnız önizle')
            ui.contains('Olumsuzluk veya kontrol isteği algılandı.')
            ui.click('Plan')
            ui.fill('Plan için Türkçe görev', 'yerel form görevini hazırla')
            ui.click('Planı önizle')
            ui.contains('independent_dom_equals')
            ui.contains('ilk onay buraya taşınmaz')
            ui.persisted('SELECT count(*) FROM desktop_tasks', 0)
            screenshots.append(ui.screenshot('aos-native-plan.png'))
            ui.fill('Plan için Türkçe görev', 'hello görevini başlatma')
            ui.wait(lambda: 'independent_dom_equals' not in ui.text(), 'stale plan removed')
            ui.click('Planı önizle')
            ui.contains('plan üretilmedi.')
            ui.fill('Birleşik Türkçe görev', 'önce hello görevini hazırla; sonra yerel form görevini hazırla; sonra görsel save görevini hazırla')
            ui.click('Birleşimi önizle')
            ui.contains('3 sabit görev tanındı')
            ui.contains('independent_canvas_equals')
            ui.persisted('SELECT count(*) FROM desktop_tasks', 0)
            ui.persisted('SELECT count(*) FROM actions', 0)
            ui.show_paragraph('Birleşim SHA-256:')
            screenshots.append(ui.screenshot('aos-native-compound-plan.png'))
            ui.fill('Birleşik Türkçe görev', 'hello görevini başlatma')
            ui.wait(lambda: 'independent_canvas_equals' not in ui.text(), 'stale compound plan removed')
            ui.click('Birleşimi önizle')
            ui.contains('Olumsuzluk veya kontrol isteği: birleşik plan üretilmedi.')
            ui.click('Kurtarma')
            ui.click('Plan')
            ui.wait(lambda: 'Olumsuzluk veya kontrol isteği: birleşik plan üretilmedi.' not in ui.text(), 'compound result cleared on panel switch')
            ui.click('Kurtarma')
            ui.contains('Otomatik okunmaz.')
            ui.click('Oturum bağını oku')
            binding_message = 'Kalıcı oturum bağı eşleşti; yürütme veya kurtarma yetkisi verilmedi.'
            ui.contains(binding_message)
            ui.contains('Aynı host süreci gözlendi')
            ui.contains('Container: Sorgulanmadı')
            ui.persisted('SELECT count(*) FROM desktop_tasks', 0)
            ui.persisted('SELECT count(*) FROM actions', 0)
            ui.show_paragraph('Snapshot SHA-256:')
            screenshots.append(ui.screenshot('aos-native-session-binding.png'))
            ui.click('Duraklat')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'PAUSED')
            ui.wait(lambda: binding_message not in ui.text(), 'session binding cleared on generation change')
            ui.click('Oturum bağını oku')
            ui.contains(binding_message)
            ui.contains('PAUSED')
            ui.click('Devam et')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'AGENT')
            ui.wait(lambda: binding_message not in ui.text(), 'session binding cleared on resume')
            ui.click('Oturum bağını oku')
            ui.contains(binding_message)
            ui.click('Plan')
            ui.click('Kurtarma')
            ui.wait(lambda: binding_message not in ui.text(), 'session binding cleared on panel switch')
            ui.click('Görevler')
            ui.click('Hello görevi başlat')
            ui.contains('filesystem.write')
            ui.click('Reddet')
            ui.job_status('cancelled')
            ui.click('Hello görevi başlat')
            ui.contains('filesystem.write')
            pending_query = "SELECT approval_id FROM desktop_approvals WHERE status='pending'"
            pending = ui.scalar(pending_query)
            assert pending is not None
            ui.click('Kurtarma')
            ui.click('Envanteri oku')
            ui.contains('waiting_approval')
            assert ui.scalar(pending_query) == pending, 'Inventory changed pending approval'
            ui.persisted('SELECT count(*) FROM actions', 0)
            screenshots.append(ui.screenshot('aos-native-recovery.png'))
            ui.click('Kontrolü al')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'HUMAN')
            ui.click('Envanteri oku')
            ui.contains('cancelled')
            ui.wait(lambda: 'waiting_approval' not in ui.text(), 'cancelled recovery snapshot')
            ui.persisted("SELECT count(*) FROM desktop_approvals WHERE status IN ('pending','approved')", 0)
            ui.click('Görevler')
            ui.wait(lambda: 'Eylem onayı bekleniyor' not in ui.text(), 'revoked approval')
            ui.click('Ajana geri ver')
            ui.persisted('SELECT owner FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'AGENT')
            ui.click('Hello görevi başlat')
            ui.contains('filesystem.write')
            ui.click('Duraklat')
            ui.contains('Görev duraklatıldı.')
            ui.click('Devam et')
            ui.contains('filesystem.write')
            screenshots.append(ui.screenshot('aos-native-approval.png'))
            ui.click('Onayla')
            ui.job_status('succeeded')
            ui.click('İzi aç')
            ui.contains('independent_read_equals')
            ui.contains('passed')
            ui.click('Görevler')
            ui.select_task('y', 'Browser görevi başlat')
            ui.click('Browser görevi başlat')
            ui.contains('browser.fill')
            ui.click('Onayla')
            ui.contains('browser.submit')
            browser_run = ui.scalar('SELECT run_id FROM desktop_tasks ORDER BY rowid DESC LIMIT 1')
            old_digest = ui.scalar("SELECT action_sha256 FROM desktop_approvals WHERE status='pending'")
            ui.click('Duraklat')
            ui.job_status('paused')
            ui.contains('Görev duraklatıldı.')
            ui.click('Devam et')
            ui.contains('browser.submit')
            assert ui.scalar('SELECT run_id FROM desktop_tasks ORDER BY rowid DESC LIMIT 1') == browser_run
            assert ui.scalar("SELECT action_sha256 FROM desktop_approvals WHERE status='pending'") != old_digest
            screenshots.append(ui.screenshot('aos-native-browser-approval.png'))
            ui.click('Onayla')
            ui.job_status('succeeded')
            ui.click('İzi aç')
            ui.contains('independent_dom_equals')
            ui.contains('passed')
            ui.persisted("SELECT count(*) FROM verifications WHERE run_id=(SELECT run_id FROM desktop_tasks ORDER BY rowid DESC LIMIT 1) AND result='passed'", 2)
            ui.click('Görevler')
            ui.select_task('g', 'Vision görevi başlat')
            ui.contains('Sentetik fixture; görüntü modeli çalışmaz' if arguments.engine == 'fixture' else 'Gerçek Bonsai')
            ui.click('Vision görevi başlat')
            ui.contains('vision.click', timeout=120 if arguments.engine == 'decider' else 30)
            screenshots.append(ui.screenshot('aos-native-vision-approval.png'))
            ui.click('Onayla')
            ui.job_status('succeeded')
            ui.click('İzi aç')
            ui.contains('independent_canvas_equals')
            ui.contains('passed')
            ui.persisted("SELECT count(*) FROM verifications WHERE run_id=(SELECT run_id FROM desktop_tasks ORDER BY rowid DESC LIMIT 1) AND result='passed'", 1)
            ui.click('Bilgisayar')
            ui.contains('Canlı · 1280 × 800')
            ui.click('Kurtarma')
            ui.click('Oturum bağını oku')
            ui.contains(binding_message)
            binding_before_restart = ui.scalar("SELECT payload_json FROM desktop_events WHERE kind='runtime_binding' ORDER BY rowid DESC LIMIT 1")
            ui.click('Durdur')
            ui.persisted('SELECT status FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'stopped')
            ui.wait(lambda: binding_message not in ui.text(), 'session binding cleared on stop')
            ui.click('Yeniden başlat')
            ui.persisted('SELECT status FROM desktop_sessions ORDER BY rowid DESC LIMIT 1', 'paused')
            ui.wait(lambda: ui.scalar("SELECT payload_json FROM desktop_events WHERE kind='runtime_binding' ORDER BY rowid DESC LIMIT 1") != binding_before_restart,
                    'new runtime birth committed')
            ui.wait(lambda: binding_message not in ui.text(), 'old binding absent after runtime restart')
            ui.click('Oturum bağını oku')
            ui.contains(binding_message)
            ui.contains('Aynı host süreci gözlendi')
            assert ui.scalar("SELECT payload_json FROM desktop_events WHERE kind='runtime_binding' ORDER BY rowid DESC LIMIT 1") != binding_before_restart
            ui.click('Bilgisayar')
            ui.contains('Canlı · 1280 × 800')
            ui.click('Kurtarma')
            ui.click('Oturum bağını oku')
            ui.contains(binding_message)
            window = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), ui.window_id())
            window.resize(800, 600)
            ui.wait(lambda: window.get_geometry().width == 800, 'compact native window')
            ui.wait(lambda: (document := ui.find('document web', 'AOS · Kontrol merkezi')) is not None
                    and document.get_component_iface().get_extents(Atspi.CoordType.WINDOW).width == 800,
                    'compact WebKit viewport')
            time.sleep(.3)
            ui.show_paragraph('Snapshot SHA-256:')
            screenshots.append(ui.screenshot('aos-native-compact.png'))
            ui.click('Çıkış')
            ui.wait(lambda: ui.find('password text', 'Yerel oturum anahtarı'), 'logout')
            print(json.dumps({'engine': arguments.engine, 'native': 'Tauri/WebKitGTK', 'automation': 'AT-SPI',
                              'incognito': True, 'native_vnc_keyboard': True, 'goal_plan_recovery': True,
                              'compound_plan_read_only': True, 'session_binding_stale_clear_restart': True,
                              'native_bounded_tasks': ['hello', 'browser_form', 'vision_canvas'],
                              'screenshots': screenshots, 'result': 'passed'}), flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
