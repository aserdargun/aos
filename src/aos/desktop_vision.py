from .contracts import REPO_ROOT
from .desktop_browser import DesktopBrowserRuntime
from .vision import VisionRuntime


class DesktopVisionRuntime(VisionRuntime, DesktopBrowserRuntime):
    worker_file = 'desktop_vision_worker.py'
    allowed_tools = frozenset({'vision.capture', 'vision.click', 'vision.verify'})

    def __init__(self, desktop):
        DesktopBrowserRuntime.__init__(self, desktop)
        self.capture = None
        self.scene = None

    def worker_source(self):
        common = (REPO_ROOT / 'src/aos/desktop_browser_worker.py').read_text()
        worker = (REPO_ROOT / 'src/aos' / self.worker_file).read_text()
        return ("import sys,types\nshared=types.ModuleType('aos_desktop_worker')\n"
                "sys.modules['aos_desktop_worker']=shared\nexec(" + repr(common) + ",shared.__dict__)\n" + worker)

    def bind_scene(self, scene, state_version):
        self.owned_desktop()
        super().bind_scene(scene, state_version)
