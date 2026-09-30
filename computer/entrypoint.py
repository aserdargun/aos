import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    processes = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    Path(os.environ['XDG_RUNTIME_DIR']).mkdir(parents=True, mode=0o700, exist_ok=True)

    def launch(command):
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process

    try:
        launch(['Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp', '-ac'])
        for attempt in range(100):
            if subprocess.run(['xdpyinfo'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Virtual display did not start')
        launch(['dbus-run-session', '--', 'xfce4-session'])
        for port in (5900, 5901):
            command = ['x11vnc', '-display', ':99', '-listen', '127.0.0.1', '-rfbport', str(port),
                       '-forever', '-shared', '-nopw', '-noxdamage', '-quiet']
            if port == 5901:
                command.append('-viewonly')
            launch(command)
        launch(['python3', '/opt/aos/note.py'])
        while not stopping and all(process.poll() is None for process in processes):
            time.sleep(0.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
