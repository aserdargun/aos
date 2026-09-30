import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from aos.contracts import REPO_ROOT, digest


DOCKER = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock']
DOWNLOADS = {
    'codium.deb': ('https://github.com/VSCodium/vscodium/releases/download/1.135.06055/codium_1.135.06055_amd64.deb',
                   '5f5c00a9da9d232e4c84e9eee68bbdeb4d8737462022626ce3bdb6948f3d8649'),
    'node.tar.xz': ('https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz',
                    'fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6'),
}


def main():
    source = REPO_ROOT / 'computer'
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()}
    source_hash = digest(files)
    chromium = REPO_ROOT / 'models/playwright/chromium-1243/chrome-linux64'
    if not (chromium / 'chrome').is_file():
        raise SystemExit('Prepare full Playwright Chromium revision 1243 before building')
    cache = REPO_ROOT / 'models/desktop-downloads'
    cache.mkdir(exist_ok=True)
    for name, (url, expected) in DOWNLOADS.items():
        target = cache / name
        if not target.exists():
            with urllib.request.urlopen(url, timeout=60) as response, target.open('wb') as output:
                shutil.copyfileobj(response, output)
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise SystemExit('Download SHA-256 mismatch: ' + name)
    with tempfile.TemporaryDirectory(prefix='desktop-build-', dir=REPO_ROOT / 'runs') as temporary:
        context = Path(temporary)
        for name in files:
            shutil.copy2(source / name, context / name)
        for name in DOWNLOADS:
            shutil.copy2(cache / name, context / name)
        shutil.copytree(chromium, context / 'chromium')
        tag = 'aos-desktop:' + source_hash[:16]
        subprocess.run([*DOCKER, 'build', '--force-rm', '--build-arg', 'SOURCE_SHA256=' + source_hash, '-t', tag, str(context)], check=True,
                       env={**os.environ, 'DOCKER_BUILDKIT': '0'})
        inspected = json.loads(subprocess.check_output([*DOCKER, 'image', 'inspect', tag]))[0]
    manifest = {'image_id': inspected['Id'], 'source_sha256': source_hash, 'source_files': files,
                'base_image': 'ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3',
                'downloads': {name: checksum for name, (_, checksum) in DOWNLOADS.items()},
                'chromium_sha256': hashlib.sha256((chromium / 'chrome').read_bytes()).hexdigest()}
    (REPO_ROOT / 'models/desktop-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Pinned desktop image:', inspected['Id'])


if __name__ == '__main__':
    main()
