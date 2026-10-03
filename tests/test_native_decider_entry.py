import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from aos import native_decider_entry as entry
from aos import native_inhibit as inhibit
from aos.contracts import REPO_ROOT, canonical, digest


SYNTHETIC_WORKER = '''import json,os,sys
pins=json.load(open(sys.argv[1]))
if pins.get('grandchild'):
    parent=os.fork()
    if parent:
        os._exit(19)
descriptors=[]
for name in os.listdir('/proc/self/fd'):
    try:
        descriptor=int(name)
        metadata=os.fstat(descriptor)
        if descriptor>2:
            descriptors.append({'fd':descriptor,'inode':metadata.st_ino})
    except OSError:
        pass
print(json.dumps({'pid':os.getpid(),'prefix':sys.prefix,'base_prefix':sys.base_prefix,
                  'executable':sys.executable,'argv':sys.argv,'descriptors':descriptors,
                  'offline':os.environ.get('HF_HUB_OFFLINE'),'pythonpath':os.environ.get('PYTHONPATH')}),flush=True)
sys.stdin.buffer.read(1)
'''


class NativeDeciderEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / 'entry-config.private.json'
        self.worker = self.root / 'synthetic-worker.py'
        self.worker.write_text(SYNTHETIC_WORKER)
        self.worker.chmod(0o600)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.manifest.write_text('{}')
        self.manifest.chmod(0o644)
        self.venv = self.root / 'synthetic-model-venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.venv)
        self.python = self.venv / 'bin/python'
        self.store = inhibit.provision_native_inhibit(self.root / 'store', scope_id='native-default-v1')
        links = {}
        current = self.python
        while current.is_symlink():
            target = os.readlink(current)
            links[str(current)] = target
            current = Path(os.path.normpath(str(current.parent / target)))
        self.real_python = current
        paths = [self.worker, self.manifest, self.real_python, self.venv / 'pyvenv.cfg',
                 REPO_ROOT / 'src/aos/native_decider_entry.py', REPO_ROOT / 'src/aos/native_inhibit.py',
                 REPO_ROOT / 'src/aos/shared_desktop_plan.py', REPO_ROOT / 'src/aos/workspace_identity.py',
                 REPO_ROOT / 'src/aos/contracts.py', REPO_ROOT / 'scripts/native_decider_entry.py']
        sources = {str(path): self.sha(path.read_bytes()) for path in paths}
        configs = {str(self.root / 'store/store.json'): self.store.content_sha256}
        value = dict(store=self.store, python_path=str(self.python), python_real_path=str(self.real_python),
                     python_sha256=sources[str(self.real_python)], python_links=links,
                     worker_path=str(self.worker), worker_sha256=sources[str(self.worker)],
                     manifest_path=str(self.manifest), manifest_sha256=sources[str(self.manifest)],
                     source_files=sources, source_sha256=digest(sources), config_files=configs, config_sha256=digest(configs))
        with self.scope():
            self.config = entry.NativeDeciderEntryConfig(**value)
        self.write_config()
        source_files = {str(self.worker): sources[str(self.worker)]}
        config_files = configs
        current_time = time.clock_gettime(time.CLOCK_BOOTTIME)
        self.request = inhibit.NativeInhibitRequest(
            request_id='native-inhibit-' + 'a' * 32, scope_id='native-default-v1', principal_id='synthetic-reviewer',
            expected_session='app-' + 'b' * 32, controller_session_id='desktop-session-' + 'c' * 32,
            generation=1, handover_sha256='1' * 64, shared_plan_sha256='2' * 64,
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), source_files=source_files,
            source_sha256=digest(source_files), config_files=config_files, config_sha256=digest(config_files),
            issued_boottime=current_time, expires_boottime=current_time + 600)

    def sha(self, content):
        return hashlib.sha256(content).hexdigest()

    def scope(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(entry, 'ENTRY_CONFIG_PATH', self.path))
        stack.enter_context(patch.object(entry, 'ORIGINAL_WORKER_PATH', self.worker))
        stack.enter_context(patch.object(entry, 'ORIGINAL_MANIFEST_PATH', self.manifest))
        return stack

    def write_config(self):
        self.path.write_text(canonical(self.config.model_dump(mode='json')))
        self.path.chmod(0o600)
        self.checksum = self.sha(self.path.read_bytes())

    def program(self):
        return '''import sys
from pathlib import Path
from aos import native_decider_entry as entry
entry.ENTRY_CONFIG_PATH=Path(sys.argv[1])
entry.ORIGINAL_WORKER_PATH=Path(sys.argv[2])
entry.ORIGINAL_MANIFEST_PATH=Path(sys.argv[3])
entry.exec_native_decider_entry(sys.argv[4],sys.argv[5:])
'''

    def command(self, arguments=None, checksum=None):
        return [sys.executable, '-c', self.program(), str(self.path), str(self.worker), str(self.manifest),
                checksum or self.checksum, *(arguments or [str(self.manifest)])]

    def test_exec_preserves_pid_venv_prefix_only_lease_fd_and_offline_guards(self):
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        process = subprocess.Popen(self.command([str(self.manifest), '--idle-seconds=630', '--serve']),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=(read_fd,), env=dict(os.environ, PYTHONHOME='', PYTHONPATH=str(REPO_ROOT / 'src')))
        try:
            value = json.loads(process.stdout.readline())
            self.assertEqual(value['pid'], process.pid)
            self.assertEqual(value['prefix'], str(self.venv))
            self.assertNotEqual(value['prefix'], value['base_prefix'])
            self.assertEqual(value['executable'], str(self.python))
            self.assertEqual(value['argv'], [str(self.worker), str(self.manifest), '--idle-seconds=630', '--serve'])
            self.assertEqual([descriptor['inode'] for descriptor in value['descriptors']], [self.store.record.lock_identity.inode])
            self.assertEqual(value['offline'], '1')
            self.assertIsNone(value['pythonpath'])
            with self.assertRaisesRegex(ValueError, 'contention'):
                inhibit.publish_native_inhibit(self.store, self.request)
            process.communicate(b'x', timeout=5)
            self.assertEqual(process.returncode, 0)
            inhibit.publish_native_inhibit(self.store, self.request)
        finally:
            os.close(read_fd)
            os.close(write_fd)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_inhibited_stale_config_pin_and_symlink_refuse_before_worker(self):
        scenarios = ['config_sha', 'worker_sha', 'missing_config', 'link', 'inhibited']
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                if scenario == 'config_sha':
                    command = self.command(checksum='f' * 64)
                elif scenario == 'worker_sha':
                    self.worker.write_text(SYNTHETIC_WORKER + '\n')
                    command = self.command()
                elif scenario == 'missing_config':
                    self.worker.write_text(SYNTHETIC_WORKER)
                    self.path.rename(self.root / 'held-config')
                    command = self.command()
                elif scenario == 'link':
                    (self.root / 'held-config').rename(self.path)
                    self.python.unlink()
                    self.python.symlink_to('/usr/bin/false')
                    command = self.command()
                else:
                    self.python.unlink()
                    self.python.symlink_to(self.config.python_links[str(self.python)])
                    inhibit.publish_native_inhibit(self.store, self.request)
                    command = self.command()
                result = subprocess.run(command, input=b'x', capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')

    def test_only_exact_current_worker_argument_forms(self):
        with self.scope():
            for arguments in ([str(self.manifest)], [str(self.manifest), '--serve'],
                              [str(self.manifest), '--idle-seconds=1', '--serve']):
                self.assertEqual(entry.worker_argv(self.config, arguments)[2:], arguments)
            for arguments in ([], ['/other'], [str(self.manifest), '--help'],
                              [str(self.manifest), '--idle-seconds=0', '--serve'],
                              [str(self.manifest), '--idle-seconds=631', '--serve'],
                              [str(self.manifest), '--idle-seconds=01', '--serve']):
                with self.assertRaises(ValueError):
                    entry.worker_argv(self.config, arguments)

    def test_exec_failure_closes_lease_resources_once(self):
        acquired = []
        original = inhibit.acquire_native_lease
        def capture(store):
            lease = original(store)
            acquired.append((lease.fd, lease._directory))
            return lease
        with self.scope(), patch.object(entry, 'acquire_native_lease', side_effect=capture), \
                patch.object(entry.os, 'execve', side_effect=OSError('Synthetic exec failure')) as execute, \
                patch.object(entry.os, 'supports_fd', {execute}):
            with self.assertRaisesRegex(OSError, 'Synthetic exec failure'):
                entry.exec_native_decider_entry(self.checksum, [str(self.manifest)])
        for descriptor in acquired[0]:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertIsNone(inhibit.read_native_inhibit(self.store))

    def test_executable_size_bound_is_64_mib_and_rejects_oversize(self):
        self.assertEqual(entry.EXECUTABLE_LIMIT, 64 * 1024 * 1024)
        oversized = SimpleNamespace(st_mode=0o100755, st_uid=os.getuid(), st_nlink=1,
                                    st_size=entry.EXECUTABLE_LIMIT + 1)
        with patch.object(entry.os, 'fstat', return_value=oversized), patch.object(entry.os, 'read') as read:
            with self.assertRaises(ValueError):
                entry._verify_executable(-1, self.config)
        read.assert_not_called()

    def test_synthetic_exec_parent_crash_retains_grandchild_lease(self):
        self.manifest.write_text('{"grandchild":true}')
        sources = dict(self.config.source_files)
        sources[str(self.manifest)] = self.sha(self.manifest.read_bytes())
        self.config = self.config.model_copy(update={'manifest_sha256': sources[str(self.manifest)],
                                                     'source_files': sources, 'source_sha256': digest(sources)})
        self.write_config()
        monitor = '''import ctypes,json,os,subprocess,sys
from aos.native_inhibit import NativeInhibitStore,NativeInhibitRequest,publish_native_inhibit
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0
store=NativeInhibitStore.model_validate_json(sys.argv[1])
request=NativeInhibitRequest.model_validate_json(sys.argv[2])
process=subprocess.Popen(json.loads(sys.argv[3]),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
record=json.loads(process.stdout.readline())
assert process.wait(timeout=5)==19
assert record['pid']!=process.pid
try:
    publish_native_inhibit(store,request)
except ValueError as error:
    assert 'contention' in str(error)
else:
    raise AssertionError('Inherited native lease missing')
process.stdin.write(b'x');process.stdin.flush();process.stdin.close()
assert os.waitpid(record['pid'],0)[1]==0
process.stdout.close();process.stderr.close()
publish_native_inhibit(store,request)
print('exec-grandchild-native-lease-ok')
'''
        result = subprocess.run([sys.executable, '-c', monitor, self.store.model_dump_json(), self.request.model_dump_json(),
                                 json.dumps(self.command())], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exec-grandchild-native-lease-ok', result.stdout)

    def test_canonical_schema_matches_model_and_rejects_shared_authority(self):
        schema = json.loads((REPO_ROOT / 'schemas/native_decider_entry_config.schema.json').read_text())
        self.assertEqual(schema, entry.NativeDeciderEntryConfig.model_json_schema() | {'$schema': 'https://json-schema.org/draft/2020-12/schema'})
        validator = jsonschema.Draft202012Validator(schema)
        value = self.config.model_dump(mode='json')
        validator.validate(value)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(value | {'shared_launch_authorized': True})


if __name__ == '__main__':
    unittest.main()
