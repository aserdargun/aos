import copy
import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos import native_exclusion as exclusion
from aos.contracts import canonical
from aos.native_exclusion import NativeExclusionEvidence, NativeExclusionPrelaunchVerifier, NativeExclusionReader
from aos.shared_desktop_host import SharedServiceBinding
from aos.shared_desktop_plan import SharedDesktopLimits, SharedDesktopTemplate, prepare_plan

import test_native_maintenance as maintenance_fixtures


class NativeExclusionContractTests(unittest.TestCase):
    def setUp(self):
        self.boot = '11111111-1111-1111-1111-111111111111'
        process = {'boot_id': self.boot, 'pid': 101, 'start_ticks': 42, 'pid_namespace': 123, 'uid': 1000}
        sources = {'/synthetic/source.py': '1' * 64}
        configs = {'/synthetic/config.json': '2' * 64}
        self.evidence = {
            'recorded_at': '2026-10-03T00:00:00Z', 'request_id': 'native-maintenance-' + 'a' * 32,
            'principal': 'synthetic-operator', 'owner_uid': 1000, 'boot_id': self.boot,
            'issued_boottime': 10.0, 'expires_boottime': 100.0,
            'effective_deadline_boottime': 80.0, 'observed_boottime': 20.0,
            'maintenance_request_sha256': '3' * 64, 'maintenance_receipt_sha256': '4' * 64,
            'handover_sha256': '5' * 64, 'shared_plan_sha256': '6' * 64,
            'candidate_manifest_sha256': '7' * 64, 'candidate_patch_sha256': '8' * 64,
            'source_files': sources, 'source_sha256': digest(sources),
            'config_files': configs, 'config_sha256': digest(configs),
            'legacy': {'manager_session': 'app-' + 'b' * 32, 'original_state_sha256': '9' * 64,
                'stopped_state_sha256': 'a' * 64,
                'controller': {'session_id': 'desktop-session-' + 'c' * 32, 'runtime_id': 'synthetic-runtime',
                    'lease_id': 'synthetic-lease', 'generation': 3, 'owner': 'AGENT', 'status': 'running'},
                'supervisor': process, 'backend': dict(process, pid=102),
                'observed_workers': [dict(process, pid=103)]},
            'shared_caller': {'invocation_id': 'd' * 32, 'process': dict(process, pid=201),
                'control_group': '/synthetic.slice/swapp-aos-gpu-shared-desktop-default.service'},
            'absence_observation_sha256': ['b' * 64, 'c' * 64],
        }

    def test_canonical_schema_and_non_allocating_contract(self):
        schema = json.loads((REPO_ROOT / 'schemas/native_exclusion_evidence.schema.json').read_text())
        self.assertEqual(schema, NativeExclusionEvidence.model_json_schema())
        result = NativeExclusionEvidence.model_validate(self.evidence).model_dump(mode='json')
        jsonschema.Draft202012Validator(schema).validate(result)
        for field in ('allocation_authority', 'shared_launch_authorized', 'gpu_release_verified', 'expiry_reopens_native'):
            self.assertIs(result[field], False)
            with self.subTest(field=field), self.assertRaises(ValueError):
                NativeExclusionEvidence.model_validate(dict(result, **{field: True}))
        with self.assertRaises(ValueError):
            NativeExclusionEvidence.model_validate(dict(result, schema_version='aos.native-exclusion.v2'))

    def test_original_interval_cannot_be_renewed_or_outlived(self):
        for changes in ({'observed_boottime': 80.0}, {'observed_boottime': 9.0},
                        {'effective_deadline_boottime': 101.0}, {'expires_boottime': 911.0},
                        {'expires_boottime': float('inf')}, {'issued_boottime': float('nan')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NativeExclusionEvidence.model_validate(dict(self.evidence, **changes))

    def test_complete_source_and_config_map_digests_are_required(self):
        for field in ('source', 'config'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                NativeExclusionEvidence.model_validate(dict(self.evidence, **{field + '_sha256': 'f' * 64}))
            for path in ('relative.json', '/synthetic/../other.json', '/synthetic/source.py\n'):
                files = {path: 'e' * 64}
                with self.subTest(field=field, path=path), self.assertRaises(ValueError):
                    NativeExclusionEvidence.model_validate(dict(self.evidence,
                        **{field + '_files': files, field + '_sha256': digest(files)}))

    def test_legacy_and_shared_caller_roles_cannot_be_mixed(self):
        for changes in ({'pid': 101}, {'uid': 1001}, {'boot_id': '22222222-2222-2222-2222-222222222222'},
                        {'pid_namespace': 456}):
            changed = copy.deepcopy(self.evidence)
            changed['shared_caller']['process'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NativeExclusionEvidence.model_validate(changed)
        changed = copy.deepcopy(self.evidence)
        changed['legacy']['observed_workers'].append(changed['legacy']['backend'])
        with self.assertRaises(ValueError):
            NativeExclusionEvidence.model_validate(changed)
        changed = copy.deepcopy(self.evidence)
        changed['legacy']['controller']['owner'] = 'HUMAN'
        with self.assertRaises(ValueError):
            NativeExclusionEvidence.model_validate(changed)

    def test_non_ascii_paths_keep_existing_ascii_escaped_native_hash_contract(self):
        changed = copy.deepcopy(self.evidence)
        changed['source_files'] = {'/synthetic/görev.py': '1' * 64}
        changed['source_sha256'] = digest(changed['source_files'])
        result = NativeExclusionEvidence.model_validate(changed)
        self.assertIn('\\u00f6', canonical(result.source_files))
        utf8 = json.dumps(result.source_files, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertNotEqual(hashlib.sha256(utf8).hexdigest(), result.source_sha256)


class NativeExclusionReaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = maintenance_fixtures.NativeMaintenanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        root_patch = patch.object(exclusion, 'REPO_ROOT', fixture.repository)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        for relative in ('src/aos/shared_desktop_host.py', 'src/aos/bounded_process.py',
                         'src/aos/native_inhibit.py', 'src/aos/shared_desktop_provision.py'):
            path = fixture.repository / relative
            shutil.copyfile(REPO_ROOT / relative, path)
            fixture.files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        launcher = fixture.repository / 'synthetic-launcher.py'
        launcher.write_text('Synthetic launcher pin, never executed\n')
        fixture.files[str(launcher)] = hashlib.sha256(launcher.read_bytes()).hexdigest()
        unicode_source = fixture.repository / 'görev.py'
        unicode_source.write_text('Synthetic source pin\n')
        fixture.files[str(unicode_source)] = hashlib.sha256(unicode_source.read_bytes()).hexdigest()
        promoted = dict(fixture.files)
        for entry in fixture.manifest['files']:
            promoted[str(fixture.repository / entry['path'])] = entry['after_sha256']
        shared_base = fixture.repository / 'data/local-app-project-scientist-shared-v1'
        template = SharedDesktopTemplate(origin='http://127.0.0.1:18866', manager_base=str(shared_base),
            project='scientist-shared-v1', session_root=str(shared_base), python_path=str(fixture.repository / 'python'),
            python_sha256=promoted[str(fixture.repository / 'python')], launcher_path=str(launcher),
            launcher_sha256=promoted[str(launcher)], source_files=promoted, source_sha256=digest(promoted),
            config_files=fixture.config, config_sha256=digest(fixture.config),
            broker_socket='/run/user/' + str(fixture.identity.uid) + '/swapp-gpu/broker.sock',
            broker_identity_sha256=None, limits=SharedDesktopLimits(cpu_quota_percent=200,
                memory_max_bytes=1073741824, tasks_max=128, stop_timeout_seconds=5))
        self.plan = prepare_plan(template, predecessor=None, new_session='app-' + 'e' * 32)
        self.plan_path = fixture.root / 'shared-plan.private.json'
        self.plan_path.write_text(json.dumps(self.plan.model_dump(mode='json'), indent=2) + '\n')
        self.plan_path.chmod(0o600)
        configs = dict(fixture.config)
        configs[str(self.plan_path)] = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        fixture.request = fixture.request.model_copy(update={'source_files': fixture.files,
            'source_sha256': digest(fixture.files), 'config_files': configs, 'config_sha256': digest(configs),
            'shared_plan_sha256': self.plan.plan_sha256()})
        with fixture.execution_boundaries():
            self.receipt = fixture.execute()
        self.receipt_path = Path(fixture.store.record.directory) / 'receipt.json'
        self.receipt_raw = self.receipt_path.read_bytes()
        self.caller = SharedServiceBinding(invocation_id='f' * 32,
            process=fixture.identity.model_copy(update={'pid': 2147483010, 'start_ticks': 456}),
            control_group='/synthetic.slice/swapp-aos-gpu-shared-desktop-default.service')
        self.transport = Mock()
        self.transport.read.return_value = self.caller
        self.inventory = Mock(return_value=([{'synthetic': True}], []))
        inventory_patch = patch.object(maintenance_fixtures.maintenance, '_native_inventory', self.inventory)
        inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        original_pin = exclusion.read_pinned_file
        def pinned(path, checksum, **arguments):
            if str(path) in ('/usr/bin/docker', '/usr/bin/nvidia-smi'):
                self.assertEqual(checksum, fixture.files[str(path)])
                return b'Synthetic tool bytes never executed'
            return original_pin(path, checksum, **arguments)
        pin_patch = patch.object(exclusion, 'read_pinned_file', side_effect=pinned)
        pin_patch.start()
        self.addCleanup(pin_patch.stop)
        self.reader = self.make_reader()

    def make_reader(self, **changes):
        arguments = {'legacy_state_path': self.fixture.current,
            'candidate_manifest_path': self.fixture.recipe / 'manifest.json',
            'candidate_patch_path': self.fixture.recipe / 'source.patch.txt',
            'shared_plan_path': self.plan_path, 'transport': self.transport}
        arguments.update(changes)
        return NativeExclusionReader(self.fixture.store, self.fixture.request,
            hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(), self.caller, **arguments)

    def read(self, reader=None, **changes):
        arguments = {'deadline': self.fixture.request.expires_boottime - 1}
        arguments.update(changes)
        with patch('subprocess.Popen', side_effect=AssertionError('No process start in mocked producer read')):
            return (reader or self.reader).read_native_exclusion(self.fixture.request.handover_sha256,
                self.fixture.request.shared_plan_sha256, **arguments)

    def prelaunch_verifier(self):
        return NativeExclusionPrelaunchVerifier(self.fixture.store, self.fixture.request,
            hashlib.sha256(self.receipt_raw).hexdigest(), legacy_state_path=self.fixture.current,
            candidate_manifest_path=self.fixture.recipe / 'manifest.json',
            candidate_patch_path=self.fixture.recipe / 'source.patch.txt', shared_plan_path=self.plan_path)

    def verify_then_fake_launch(self, verifier, launch, **changes):
        arguments = {'deadline': self.fixture.request.expires_boottime - 1}
        arguments.update(changes)
        with patch('subprocess.Popen', side_effect=AssertionError('No process start during prelaunch checks')):
            result = verifier.verify_prelaunch(self.fixture.request.handover_sha256,
                self.fixture.request.shared_plan_sha256, **arguments)
        self.assertIsNone(result)
        launch()

    def test_prelaunch_has_no_future_caller_and_verifies_before_fake_launch(self):
        verifier = self.prelaunch_verifier()
        events = []
        original_read = verifier._read_receipt
        def receipt(*arguments):
            result = original_read(*arguments)
            events.append('verified-receipt')
            return result
        with patch.object(verifier, '_read_receipt', side_effect=receipt):
            self.verify_then_fake_launch(verifier, lambda: events.append('fake-launch'))
        self.assertEqual(events, ['verified-receipt', 'verified-receipt', 'fake-launch'])
        self.assertEqual(self.inventory.call_count, 2)
        self.transport.read.assert_not_called()
        self.assertFalse(hasattr(verifier, 'expected_shared_caller'))

    def test_prelaunch_failed_receipt_source_state_preview_or_deadline_never_launches(self):
        verifier = self.prelaunch_verifier()
        selected = self.fixture.repository / self.fixture.manifest['files'][0]['path']
        source_raw = selected.read_bytes()
        state_raw = self.fixture.current.read_bytes()
        preview_raw = self.fixture.preview_path.read_bytes()
        failure = self.receipt_path.parent / 'failure.json'
        for mode in ('receipt', 'failure', 'source', 'state', 'preview', 'deadline', 'worker'):
            launch = Mock()
            arguments = {}
            if mode == 'receipt': self.receipt_path.write_bytes(self.receipt_raw + b'\n')
            if mode == 'failure': failure.write_text('{}\n')
            if mode == 'source': selected.write_bytes(source_raw + b'\n')
            if mode == 'state': self.fixture.current.write_bytes(state_raw + b'\n')
            if mode == 'preview': self.fixture.preview_path.write_bytes(preview_raw + b'\n')
            if mode == 'deadline': arguments['deadline'] = self.fixture.request.issued_boottime
            if mode == 'worker': self.inventory.return_value = ([], [self.caller.process])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.verify_then_fake_launch(verifier, launch, **arguments)
            launch.assert_not_called()
            self.transport.read.assert_not_called()
            self.receipt_path.write_bytes(self.receipt_raw)
            if failure.exists(): failure.unlink()
            selected.write_bytes(source_raw)
            self.fixture.current.write_bytes(state_raw)
            self.fixture.preview_path.write_bytes(preview_raw)
            self.inventory.return_value = ([], [])

    def test_prelaunch_post_absence_failure_or_expiry_never_launches(self):
        verifier = self.prelaunch_verifier()
        failure = self.receipt_path.parent / 'failure.json'
        original = verifier._observe_absence
        for mode in ('failure', 'expiry'):
            launch = Mock()
            clock_patch = patch.object(exclusion, '_clock', return_value=self.fixture.request.expires_boottime)
            def absence(*arguments):
                result = original(*arguments)
                if mode == 'failure': failure.write_text('{}\n')
                if mode == 'expiry': clock_patch.start()
                return result
            try:
                with patch.object(verifier, '_observe_absence', side_effect=absence):
                    with self.subTest(mode=mode), self.assertRaises(ValueError):
                        self.verify_then_fake_launch(verifier, launch)
                launch.assert_not_called()
            finally:
                clock_patch.stop()
                if failure.exists(): failure.unlink()

    def test_actual_promoted_fixture_two_fresh_observations_and_semantic_plan(self):
        value, checksum = self.read()
        self.assertEqual(checksum, digest(value))
        self.assertEqual(self.inventory.call_count, 4)
        self.assertEqual(self.transport.read.call_count, 2)
        self.assertEqual(value['source_sha256'], digest(self.receipt.source_files))
        self.assertEqual(value['maintenance_receipt_sha256'], hashlib.sha256(self.receipt_raw).hexdigest())
        self.assertNotEqual(hashlib.sha256(self.plan_path.read_bytes()).hexdigest(), self.plan.plan_sha256())
        self.assertIsNone(self.plan.predecessor_session)
        self.assertEqual(value['shared_plan_sha256'], self.plan.plan_sha256())
        self.assertEqual(value['effective_deadline_boottime'], self.fixture.request.expires_boottime - 1)
        for field in ('allocation_authority', 'shared_launch_authorized', 'gpu_release_verified', 'expiry_reopens_native'):
            self.assertIs(value[field], False)

    def test_concrete_shared_transport_uses_only_bounded_read_command(self):
        concrete = exclusion.SystemdSharedDesktopTransport
        def transport(**arguments):
            return concrete(**arguments, identity_reader=lambda _pid: self.caller.process,
                group_reader=lambda _pid: self.caller.control_group, process_observer=lambda _identity: 'same_process')
        observed = []
        def runner(command, **arguments):
            observed.append((command, arguments))
            values = {'Id': self.caller.unit, 'LoadState': 'loaded', 'ActiveState': 'active',
                'SubState': 'running', 'MainPID': str(self.caller.process.pid),
                'InvocationID': self.caller.invocation_id, 'ControlGroup': self.caller.control_group}
            return subprocess.CompletedProcess(command, 0,
                '\n'.join(name + '=' + value for name, value in values.items()), '')
        self.reader.transport = None
        with patch.object(exclusion, 'SystemdSharedDesktopTransport', side_effect=transport), \
                patch.object(exclusion, 'run_bounded', side_effect=runner):
            self.assertEqual(self.reader._shared_caller(exclusion._clock() + 0.5), self.caller)
        self.assertEqual(len(observed), 1)
        command, arguments = observed[0]
        self.assertEqual(command[:5], ['/usr/bin/systemctl', '--user', 'show', self.caller.unit,
            '--property=Id,LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup'])
        self.assertGreater(arguments['timeout'], 0)
        self.assertLessEqual(arguments['timeout'], 0.5)
        self.assertEqual(arguments['input'], b'')

    def test_wrong_receipt_handover_plan_boot_owner_and_deadline_deny(self):
        self.reader.receipt_sha256 = '0' * 64
        with self.assertRaises(ValueError): self.read()
        self.reader.receipt_sha256 = hashlib.sha256(self.receipt_raw).hexdigest()
        for handover, plan in (('0' * 64, self.plan.plan_sha256()), (self.fixture.request.handover_sha256, '0' * 64)):
            with self.subTest(handover=handover), self.assertRaises(ValueError):
                self.reader.read_native_exclusion(handover, plan, deadline=self.fixture.request.expires_boottime)
        for deadline in (False, float('nan'), self.fixture.request.issued_boottime):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError): self.read(deadline=deadline)
        with patch.object(exclusion.os, 'getuid', return_value=self.fixture.identity.uid + 1):
            with self.assertRaises(ValueError): self.read()
        with patch.object(exclusion.Path, 'read_text', return_value='22222222-2222-2222-2222-222222222222'):
            with self.assertRaises(ValueError): self.read()
        with patch.object(exclusion, '_clock', return_value=self.fixture.request.expires_boottime):
            with self.assertRaises(ValueError): self.read()

    def test_missing_after_pin_or_uncovered_template_source_denies(self):
        files = dict(self.receipt.source_files)
        files.pop(str(self.fixture.repository / self.fixture.manifest['files'][0]['path']))
        changed = self.receipt.model_copy(update={'source_files': files, 'source_sha256': digest(files)})
        self.receipt_path.write_text(canonical(changed.model_dump(mode='json')) + '\n')
        with self.assertRaises(ValueError): self.read(self.make_reader())
        self.receipt_path.write_bytes(self.receipt_raw)
        template = self.plan.template.model_copy(update={'source_files': dict(self.plan.template.source_files,
            **{str(self.fixture.root / 'unreviewed.py'): '0' * 64})})
        template = template.model_copy(update={'source_sha256': digest(template.source_files)})
        changed_plan = self.plan.model_copy(update={'template': template,
            'template_sha256': digest(template.model_dump(mode='json'))})
        with patch.object(exclusion.SharedDesktopPlan, 'model_validate', return_value=changed_plan):
            with self.assertRaises(ValueError):
                self.reader._read_scope(self.receipt, self.receipt.source_files, set(),
                    self.fixture.request.expires_boottime, changed_plan.plan_sha256())

    def test_mid_observation_source_drift_prevents_second_absence_observation(self):
        selected = self.fixture.repository / self.fixture.manifest['files'][0]['path']
        def inventory(*_arguments):
            if self.inventory.call_count == 2:
                selected.write_text('Synthetic source drift\n')
            return [], []
        self.inventory.side_effect = inventory
        with self.assertRaises(ValueError): self.read()
        self.assertEqual(self.inventory.call_count, 2)

    def test_receipt_failure_or_replacement_and_new_worker_deny(self):
        failure = self.receipt_path.parent / 'failure.json'
        for mode in ('failure', 'replacement', 'worker'):
            self.inventory.reset_mock()
            def inventory(*_arguments):
                if self.inventory.call_count == 2:
                    if mode == 'failure': failure.write_text('{}\n')
                    if mode == 'replacement': self.receipt_path.write_bytes(self.receipt_raw + b'\n')
                    if mode == 'worker': return [], [self.caller.process]
                return [], []
            self.inventory.side_effect = inventory
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.read()
            if failure.exists(): failure.unlink()
            self.receipt_path.write_bytes(self.receipt_raw)

    def test_shared_generation_drift_and_mutated_absence_deny(self):
        changed = self.caller.model_copy(update={'invocation_id': 'a' * 32})
        for responses in ([None], [changed], [self.caller, changed]):
            self.transport.read.side_effect = responses
            with self.subTest(responses=responses), self.assertRaises(ValueError): self.read()
        self.transport.read.side_effect = None
        observation = maintenance_fixtures.maintenance.observe_native_absence(self.fixture.request,
            self.receipt.retired_processes, deadline=self.fixture.request.expires_boottime)
        changed_observation = observation.model_copy(update={'native_processes_absent': False})
        with patch.object(exclusion, 'observe_native_absence', return_value=changed_observation):
            with self.assertRaises(ValueError): self.read()

    def test_legacy_identity_reuse_and_expiry_during_final_read_deny(self):
        changed = self.plan.model_copy(update={'app_session': self.fixture.state.session})
        with patch.object(exclusion.SharedDesktopPlan, 'model_validate', return_value=changed):
            with self.assertRaises(ValueError):
                self.reader._read_scope(self.receipt, self.receipt.source_files, set(),
                    self.fixture.request.expires_boottime, changed.plan_sha256())
        original = self.reader._read_scope
        calls = []
        clock_patch = patch.object(exclusion, '_clock', return_value=self.fixture.request.expires_boottime)
        def expire(*arguments):
            result = original(*arguments)
            calls.append(True)
            if len(calls) == 3:
                clock_patch.start()
                self.addCleanup(clock_patch.stop)
            return result
        with patch.object(self.reader, '_read_scope', side_effect=expire):
            with self.assertRaises(ValueError): self.read()
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
