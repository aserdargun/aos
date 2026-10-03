import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / 'scripts/shared-only-runtime-v1'


class SharedOnlyRuntimeCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.root.chmod(0o700)
        cls.manifest = json.loads((RECIPE / 'manifest.json').read_text())
        for directory in ('src', 'scripts', 'services', 'schemas'):
            shutil.copytree(ROOT / directory, cls.root / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for file in cls.manifest['files']:
            actual = hashlib.sha256((cls.root / file['path']).read_bytes()).hexdigest()
            if actual != file['before_sha256']:
                raise AssertionError('Candidate original source hash differs: ' + file['path'])
        patch = RECIPE / 'source.patch.txt'
        if hashlib.sha256(patch.read_bytes()).hexdigest() != cls.manifest['patch_sha256']:
            raise AssertionError('Candidate patch hash differs')
        subprocess.run(['git', 'apply', '--check', str(patch)], cwd=cls.root, check=True, capture_output=True)
        subprocess.run(['git', 'apply', str(patch)], cwd=cls.root, check=True, capture_output=True)

    def cpu(self, program, *arguments):
        environment = dict(os.environ, PYTHONPATH=str(self.root / 'src'), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-c', program, *arguments],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_exact_candidate_and_unchanged_live_source_hashes(self):
        for file in self.manifest['files']:
            with self.subTest(path=file['path']):
                self.assertEqual(hashlib.sha256((self.root / file['path']).read_bytes()).hexdigest(), file['after_sha256'])
                self.assertEqual(hashlib.sha256((ROOT / file['path']).read_bytes()).hexdigest(), file['before_sha256'])
                ast.parse((self.root / file['path']).read_text())
        for path, checksum in self.manifest['preserved_files'].items():
            self.assertEqual(hashlib.sha256((self.root / path).read_bytes()).hexdigest(), checksum)
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), checksum)
        self.assertTrue(self.manifest['candidate_only'])
        self.assertFalse(self.manifest['native_exclusion_verified'])
        self.assertFalse(self.manifest['gpu_release_verified'])

    def test_every_declared_native_callable_has_unconditional_first_statement_denial(self):
        for file in self.manifest['files']:
            module = ast.parse((self.root / file['path']).read_text())
            nodes = {}
            for node in module.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes[node.name] = node
                elif isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            nodes[node.name + '.' + member.name] = member
            for entrypoint in file['entrypoints']:
                if entrypoint['policy'] == 'unconditional_native_denial':
                    with self.subTest(path=file['path'], symbol=entrypoint['symbol']):
                        first = nodes[entrypoint['symbol']].body[0]
                        self.assertIsInstance(first, ast.Raise)
                        self.assertEqual(first.exc.func.id, 'RuntimeError')
                        self.assertEqual(first.exc.args[0].value, self.manifest['denial_message'])

    def test_actual_host_callable_denial_before_spawn(self):
        self.cpu('''import asyncio,sys
from unittest.mock import patch
from aos.decision import DeciderEngine
from aos.reusable_decider import ReusableDeciderEngine
from aos.supervisor import BonsaiSupervisor
from aos.owned_adapter_engine import OwnedAdapterDecisionEngine
from aos.laya_candidate import LayaCandidateEngine
from aos.owned_episode_adaptation import OwnedEpisodeAdaptation
from aos import local_app
from aos.native_decider_entry import exec_native_decider_entry
async def check():
    calls=[DeciderEngine.decide(None,None,None),ReusableDeciderEngine.start(None),
           ReusableDeciderEngine.request(None,None),BonsaiSupervisor.plan(None,None,None),
           OwnedAdapterDecisionEngine.start(None),LayaCandidateEngine.start(None),
           OwnedEpisodeAdaptation.worker(None,None,None,None,None)]
    with patch('asyncio.create_subprocess_exec') as spawn:
        for call in calls:
            try:
                await call
            except RuntimeError as error:
                assert 'native/unbrokered entry disabled' in str(error)
            else:
                raise AssertionError('Native effect admitted')
        spawn.assert_not_called()
asyncio.run(check())
for call in [lambda:local_app.start(),lambda:local_app.restart(),lambda:exec_native_decider_entry('',[])]:
    try:
        call()
    except RuntimeError as error:
        assert 'native/unbrokered entry disabled' in str(error)
    else:
        raise AssertionError('Native front door admitted')
assert 'torch' not in sys.modules
''')

    def test_actual_direct_worker_and_gpu_probe_denial_before_model_import(self):
        self.cpu('''import runpy,sys
from pathlib import Path
sys.path.insert(0,str(Path('services/decider').absolute()))
import worker
try:
    worker.ModelSession(None,None)
except RuntimeError:
    pass
else:
    raise AssertionError('Native ModelSession admitted')
for path in ['services/decider/worker.py','services/decider/dataset_loss_probe.py',
             'services/decider/owned_adapter_worker.py','services/decider/owned_episode_adapter_train.py',
             'services/laya/worker.py']:
    module=runpy.run_path(path)
    try:
        module['main']()
    except RuntimeError as error:
        assert 'native/unbrokered entry disabled' in str(error)
    else:
        raise AssertionError('Direct worker admitted')
assert 'torch' not in sys.modules
assert 'laya' not in sys.modules
''')

    def test_scientist_selection_and_bonsai_metadata_constructor_survive(self):
        self.cpu('''import json,runpy,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from aos.scientist_supervisor import ScientistBonsaiSupervisor,ScientistBonsaiVisionSupervisor
with tempfile.TemporaryDirectory() as directory:
    manifest=Path(directory)/'synthetic-manifest.json'
    manifest.write_text('{}')
    assert ScientistBonsaiSupervisor(manifest,None).identity['kind']=='scientist_bonsai_broker'
    assert ScientistBonsaiVisionSupervisor(manifest,None).identity['kind']=='scientist_bonsai_broker'
module=runpy.run_path('scripts/serve_desktop.py')
with patch.object(sys,'argv',['serve_desktop','--engine','fixture']):
    try:
        module['main'](scientist_cpu_grant=object())
    except RuntimeError as error:
        assert 'native/unbrokered entry disabled' in str(error)
    else:
        raise AssertionError('Separate CPU session bypassed shared-only engine policy')
with patch.object(sys,'argv',['serve_desktop','--engine','decider']):
    try:
        module['main']()
    except RuntimeError as error:
        assert 'native/unbrokered entry disabled' in str(error)
    else:
        raise AssertionError('Native selection admitted')
with patch.object(sys,'argv',['serve_desktop','--engine','scientist','--vision-engine','bonsai']):
    try:
        module['main']()
    except SystemExit as error:
        assert error.code==2
    else:
        raise AssertionError('Missing broker authority admitted')
assert 'torch' not in sys.modules
''')

    def test_actual_staged_broker_main_preserves_gate_before_model_order(self):
        self.cpu('''import io,json,runpy,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('services/decider').absolute()))
import worker
events=[]
pins={'synthetic':True}
from aos.contracts import digest
class Gate:
    def __init__(self,ready,profile):
        assert profile=='aos.decider.turn.v1'
        events.append('gate')
        self.value={'deployment_digest':digest(pins)}
    def manifest(self,manifest):
        events.append('manifest_verified')
        return pins
    def check_manifest(self,manifest):
        events.append('manifest_rechecked')
    def admit_inference(self):
        events.append('inference_admitted')
        return 100
def verify(pins):
    events.append('environment_verified')
    return Path('/synthetic/model')
original_init=worker.BrokerModelSession.__init__
def initialize(self,*arguments):
    events.append('model_session')
    original_init(self,*arguments)
def prepare(self):
    events.append('prepare_gpu_mock')
    return {'load_ms':0}
def infer(self,request):
    events.append('infer_mock')
    return {'deployment_digest':digest(pins),'metrics':{}}
module=runpy.run_path('services/decider/broker_worker.py')
assert module['ModelSession'] is worker.BrokerModelSession
payload={'request':{'state':'synthetic','question':'synthetic','options':[{'id':'a','label':'A'},{'id':'b','label':'B'}]}}
stdin=type('Input',(),{'buffer':io.BytesIO(json.dumps(payload).encode())})()
stdout=type('Output',(),{'buffer':io.BytesIO()})()
with patch.dict(module['main'].__globals__,TurnGate=Gate,verify_environment=verify,clock=lambda:1), \\
     patch.object(worker.BrokerModelSession,'__init__',initialize), \\
     patch.object(worker.BrokerModelSession,'prepare_gpu',prepare), \\
     patch.object(worker.BrokerModelSession,'infer',infer), \\
     patch.object(sys,'argv',['broker_worker','synthetic-manifest','synthetic-ready']), \\
     patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout):
    assert module['main']()==0
assert events==['gate','manifest_verified','environment_verified','model_session','prepare_gpu_mock',
                'manifest_rechecked','inference_admitted','manifest_rechecked','infer_mock']
assert json.loads(stdout.buffer.getvalue())['deployment_digest']==digest(pins)
assert 'torch' not in sys.modules
''')


if __name__ == '__main__':
    unittest.main()
