import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tomllib

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, canonical, digest
from .desktop import DOCKER
from .desktop_mcp_bundle import read_bundle
from .supervisor import BonsaiSupervisor


PROCESS_ENV = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': ''}
DEPENDENCIES_PROBE = (
    'import importlib.metadata,json,sys; '
    'assert sys.version_info >= (3,11); '
    'print(json.dumps({item.metadata["Name"].lower().replace("_","-"):item.version '
    'for item in importlib.metadata.distributions()}))'
)


def _require(condition):
    if not condition:
        raise ValueError('local_prerequisite_not_verified')


def _json_file(path):
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1048576)
    result = json.loads(path.read_bytes())
    _require(isinstance(result, dict))
    return result


def _process(arguments, *, timeout=30):
    result = run_bounded(arguments, input=b'', env=PROCESS_ENV, timeout=timeout, max_output=1048576)
    _require(result.returncode == 0)
    return result.stdout


def _dependencies(python):
    _require(python.is_file() and os.access(python, os.X_OK))
    result = json.loads(_process([str(python.absolute()), '-I', '-B', '-c', DEPENDENCIES_PROBE]))
    _require(isinstance(result, dict) and bool(result))
    return result


def _application():
    project = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text())['project']
    requirements = list(project['dependencies'])
    for extra in ('browser', 'desktop', 'dataset'):
        requirements.extend(project['optional-dependencies'][extra])
    actual = _dependencies(REPO_ROOT / '.venv/bin/python')
    for requirement in requirements:
        name, version = requirement.split('==')
        _require(actual.get(name.lower().replace('_', '-')) == version)
    probe = 'import importlib.util,json; print(json.dumps(importlib.util.find_spec("aos").origin))'
    origin = json.loads(_process([str(REPO_ROOT / '.venv/bin/python'), '-I', '-B', '-c', probe]))
    _require(Path(origin).resolve(strict=True) == (REPO_ROOT / 'src/aos/__init__.py').resolve(strict=True))


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == 'script' and 'src' in values:
            self.assets.append(values['src'])
        if tag == 'link' and values.get('rel') == 'stylesheet':
            self.assets.append(values.get('href', ''))


def _frontend(root=None):
    root = REPO_ROOT / 'ui/dist' if root is None else Path(root).absolute()
    index = root / 'index.html'
    _require(index.is_file() and not index.is_symlink() and index.stat().st_size <= 262144)
    parser = _Assets()
    parser.feed(index.read_text())
    _require(any(name.endswith('.js') for name in parser.assets)
             and any(name.endswith('.css') for name in parser.assets))
    pending = list(parser.assets)
    checked = set()
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        _require(len(checked) < 128)
        checked.add(name)
        _require(isinstance(name, str) and name.startswith('/ui/assets/')
                 and not any(character in name for character in ('?', '#', '\\'))
                 and '..' not in Path(name).parts)
        target = root / name.removeprefix('/ui/')
        _require(target.is_file() and 0 < target.stat().st_size <= 16777216
                 and target.resolve().is_relative_to(root.resolve())
                 and not any(parent.is_symlink() for parent in (target, *target.parents) if parent != REPO_ROOT))
        if target.suffix == '.js':
            for _quote, relative in re.findall(r'''(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)(['"`])(\./[^'"`]+)\1''', target.read_text()):
                _require('..' not in Path(relative).parts)
                pending.append('/ui/' + str((target.parent / relative).relative_to(root)))
    inputs = [REPO_ROOT / 'ui/package.json', REPO_ROOT / 'ui/pnpm-lock.yaml',
              REPO_ROOT / 'ui/index.html', *sorted((REPO_ROOT / 'ui/src').rglob('*')),
              *sorted((REPO_ROOT / 'ui').glob('*config*'))]
    _require(all(path.is_file() for path in inputs[:3]) and (REPO_ROOT / 'ui/src/main.tsx').is_file())
    _require(all(path.stat().st_mtime_ns <= index.stat().st_mtime_ns for path in inputs if path.is_file()))


def _desktop():
    _require(os.getuid() != 0 and sys.platform == 'linux')
    pins = _json_file(REPO_ROOT / 'models/desktop-manifest.json')
    _require(re.fullmatch('sha256:[a-f0-9]{64}', pins.get('image_id', '')) is not None)
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (REPO_ROOT / 'computer').iterdir() if path.is_file()}
    _require(sources == pins['source_files'] and digest(sources) == pins['source_sha256'])
    inspected = json.loads(_process([*DOCKER, 'image', 'inspect', pins['image_id']]))
    _require(isinstance(inspected, list) and len(inspected) == 1)
    image = inspected[0]
    _require(image['Id'] == pins['image_id']
             and image['Config']['Labels'].get('com.aos.source-sha256') == pins['source_sha256'])


def _browser():
    _require(Path('/usr/bin/bwrap').is_file() and os.access('/usr/bin/bwrap', os.X_OK))
    _process(['/usr/bin/bwrap', '--version'])
    pins = _json_file(REPO_ROOT / 'models/browser-manifest.json')
    _require(set(pins) == {'playwright_version', 'browser_version', 'revision', 'browser_root', 'files'})
    _require((pins['playwright_version'], pins['revision'], pins['browser_version'])
             == ('1.63.0', '1243', '153.0.8010.12'))
    root = Path(pins['browser_root']).resolve(strict=True)
    _require(root.is_relative_to((REPO_ROOT / 'models').resolve()))
    files = {}
    for path in sorted(root.rglob('*')):
        _require(not path.is_symlink())
        if path.is_file():
            with path.open('rb') as stream:
                files[str(path.relative_to(root))] = hashlib.file_digest(stream, 'sha256').hexdigest()
    _require(bool(files) and files == pins['files'])
    executable = root / 'chrome-headless-shell'
    _require(executable.is_file() and os.access(executable, os.X_OK))


def _mcp():
    manifest = REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'
    archive = manifest.parent / 'packages.zip'
    _require(manifest.is_file() and not manifest.is_symlink()
             and archive.is_file() and not archive.is_symlink())
    read_bundle(manifest)


def _decider():
    pins = _json_file(REPO_ROOT / 'models/decider-manifest.json')
    for key in ('checkpoint_revision', 'code_revision', 'tokenizer_revision'):
        _require(re.fullmatch('[a-f0-9]{40}', pins[key]) is not None)
    verifier = runpy.run_path(str(REPO_ROOT / 'services/decider/worker.py'))['verify_files']
    for prefix in ('model', 'code'):
        root = Path(pins[prefix + '_path']).resolve(strict=True)
        _require(root.is_relative_to((REPO_ROOT / 'models').resolve()))
        _require(bool(pins[prefix + '_files']))
        verifier(root, pins[prefix + '_files'])
    _require(_dependencies(Path.home() / '.venv/bin/python') == pins['dependencies'])


def _bonsai():
    manifest = REPO_ROOT / 'models/bonsai-manifest.json'
    pins = _json_file(manifest)
    for key in ('checkpoint_revision', 'tokenizer_revision', 'projector_revision', 'code_revision'):
        _require(re.fullmatch('[a-f0-9]{40}', pins[key]) is not None)
    for prefix in ('model', 'runtime'):
        _require(Path(pins[prefix + '_path']).resolve(strict=True).is_relative_to((REPO_ROOT / 'models').resolve()))
        _require(bool(pins[prefix + '_files']))
    _require(pins['weights_file'] in pins['model_files'] and pins['projector_file'] in pins['model_files'])
    _require(Path(pins['server_path']).is_file() and os.access(pins['server_path'], os.X_OK))
    BonsaiSupervisor(manifest).verify_pins()


def check_local(mode='real', *, frontend_root=None) -> dict:
    if mode not in ('real', 'fixture'):
        raise ValueError('mode must be real or fixture')
    checks = []
    specifications = [
        ('application', _application, 'Yerel .venv uygulama konumu ve bağımlılık sürümleri sabit gereksinimlerle eşleşiyor.',
         '.venv ve pyproject.toml içindeki browser/desktop/dataset bağımlılıklarını açıkça hazırlayın.'),
        ('frontend', _frontend if frontend_root is None else lambda: _frontend(frontend_root), 'Yerel UI giriş dosyası ve referansları mevcut; kaynak tarihleri build tarihini aşmıyor.',
         ('ui dizininde pnpm build çalıştırın; doctor kendiliğinden build yapmaz.'
          if frontend_root is None else
          'Durdurulmuş proje için yalnız kendi private ui dizinine açık staging yapın; '
          'docs/ISOLATED_LEARNING_PROJECTS.md yolunu izleyin. Shared ui/dist değiştirilmez; doctor build yapmaz.')),
        ('desktop', _desktop, 'Non-root Linux ve mevcut Docker image/source kimliği doğrulandı; container başlatılmadı.',
         'Docker erişimini ve models/desktop-manifest.json sabit image/source eşleşmesini inceleyin; otomatik rebuild yok.'),
        ('browser', _browser, 'Bubblewrap ve sabit Chromium dosya hashleri mevcut; izolasyon handshake henüz çalıştırılmadı.',
         'Mevcut Bubblewrap/Chromium kurulumu ve models/browser-manifest.json eşleşmesini inceleyin.'),
        ('mcp', _mcp, 'Sabit Playwright MCP paketi ve arşiv hash/sürümleri doğrulandı; worker/Chromium başlatılmadı.',
         'models/desktop-mcp-v001/manifest.json ve packages.zip dosyalarını açıkça hazırlayıp kimliklerini inceleyin.'),
    ]
    if mode == 'real':
        specifications.extend([
            ('decider', _decider, 'Decider ağırlık/kod hashleri ve ayrı model Python bağımlılıkları eşleşiyor; model yüklenmedi.',
             'models/decider-manifest.json ve ~/.venv/bin/python sabit yerel kurulumu inceleyin; otomatik indirme yok.'),
            ('bonsai', _bonsai, 'Bonsai ağırlık/projector/runtime/native bağımlılık hashleri eşleşiyor; sunucu başlatılmadı.',
             'models/bonsai-manifest.json içindeki mevcut yerel dosyaları inceleyin; otomatik pin değişimi yok.'),
        ])
    for name, operation, success, action in specifications:
        try:
            operation()
        except Exception:
            checks.append({'name': name, 'ok': False,
                           'detail': 'Yerel önkoşul doğrulanamadı; eksik, değişmiş veya erişilemeyen kaynak olabilir.',
                           'action': action})
        else:
            checks.append({'name': name, 'ok': True, 'detail': success, 'action': ''})
    return {'mode': mode, 'ready': all(check['ok'] for check in checks), 'checks': checks,
            'read_only': True, 'inference_verified': False, 'resource_admission_verified': False,
            'notice': ('Yalnız bu host için anlık önkoşul kontrolü. GPU boş belleği, gerçek inference, '
                       'çalışan izolasyon, görev başarısı veya temiz makine kurulumu garantisi değildir. '
                       + ('Fixture modu gerçek model kontrolünü atlar.' if mode == 'fixture' else ''))}


def main():
    parser = argparse.ArgumentParser(description='Salt okunur yerel AOS pilot önkoşul kontrolü')
    parser.add_argument('--fixture', action='store_true', help='Gerçek model kontrolünü atlayan açık test modu')
    arguments = parser.parse_args()
    report = check_local('fixture' if arguments.fixture else 'real')
    print(canonical(report))
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
