import json
import subprocess
import time


applications = {
    'chromium': (['chromium', '--no-first-run', 'file:///opt/aos/synthetic.txt'], '[Cc]hromium'),
    'codium': (['codium', '--disable-gpu', '--disable-extensions', '/opt/aos/synthetic.txt'], '[Cc]odium'),
    'thunar': (['thunar', '/workspace'], '[Tt]hunar'),
    'terminal': (['xfce4-terminal', '--disable-server', '--title=AOS synthetic terminal'], '[Xx]fce4-terminal'),
    'libreoffice': (['libreoffice', '-env:UserInstallation=file:///tmp/aos-office-gui', '--writer', '/opt/aos/synthetic.txt'], '[Ll]ibreoffice'),
    'pdf_viewer': (['evince', '/tmp/aos-pdf/synthetic.pdf'], '[Ee]vince'),
}
observed = {}
for name, (arguments, pattern) in applications.items():
    subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--class', pattern], capture_output=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            observed[name] = len(result.stdout.splitlines())
            break
        time.sleep(.2)
    else:
        raise RuntimeError('No real visible application window: ' + name)
print(json.dumps(observed))
