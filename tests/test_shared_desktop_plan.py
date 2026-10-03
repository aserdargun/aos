import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from aos.contracts import canonical, digest
from aos.shared_desktop_plan import (
    SHARED_DESKTOP_UNIT, SharedDesktopActivation, SharedDesktopLimits, SharedDesktopPlan,
    SharedDesktopTemplate, load_activation, load_plan, load_template, predecessor_identity_sha256,
    prepare_plan, read_pinned_file, read_private_file, verify_activation, verify_python,
    verify_template_files, write_new_private_file,
)


class SharedDesktopPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.python = self.file('synthetic-python', b'Synthetic binary pin; never executed')
        self.launcher = self.file('synthetic-launcher.py', b'Synthetic fixed launcher pin; never imported')
        self.config = self.file('synthetic-config.json', b'{"synthetic":true,"enabled":false}')
        self.launch_input = self.file('synthetic-enabled-input.json', b'{"synthetic":true,"enabled":true}')
        sources = {str(self.python): self.sha(self.python.read_bytes()),
                   str(self.launcher): self.sha(self.launcher.read_bytes())}
        configs = {str(self.config): self.sha(self.config.read_bytes())}
        self.template = SharedDesktopTemplate(origin='http://127.0.0.1:8765',
            manager_base=str(self.root / 'future-manager'), session_root=str(self.root / 'future-manager'),
            python_path=str(self.python), python_sha256=sources[str(self.python)],
            launcher_path=str(self.launcher), launcher_sha256=sources[str(self.launcher)],
            source_files=sources, source_sha256=digest(sources), config_files=configs,
            config_sha256=digest(configs), broker_socket=str(self.root / 'future-broker.sock'),
            broker_identity_sha256='b' * 64,
            limits=SharedDesktopLimits(cpu_quota_percent=200, memory_max_bytes=1073741824,
                                       tasks_max=128, stop_timeout_seconds=5))
        self.new_session = 'app-' + '2' * 32
        self.plan = prepare_plan(self.template, predecessor=None, new_session=self.new_session)
        self.boot_id = '00000000-0000-0000-0000-000000000001'
        self.process = {'boot_id': self.boot_id, 'pid': 23, 'start_ticks': 31, 'pid_namespace': 47, 'uid': 1000}
        configs = {**configs, str(self.launch_input): self.sha(self.launch_input.read_bytes())}
        self.activation = SharedDesktopActivation(plan_sha256=self.plan.plan_sha256(),
            reviewed_launch_input_path=str(self.launch_input), reviewed_launch_input_sha256=configs[str(self.launch_input)],
            source_files=sources, source_sha256=digest(sources), config_files=configs,
            config_sha256=digest(configs), boot_id=self.boot_id, issued_monotonic=100.0,
            expires_monotonic=200.0, capability_proof_sha256='c' * 64, source_proof_sha256='d' * 64)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def sha(content):
        return hashlib.sha256(content).hexdigest()

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def predecessor(self):
        return {'version': '1', 'session': 'app-' + '1' * 32, 'mode': 'real',
                'supervisor': self.process, 'backend': None, 'project': None,
                'url': self.template.url, 'started_at': '2026-10-03T00:00:00Z',
                'phase': 'running', 'token_name': 'synthetic-token'}

    def changed_template(self, **changes):
        return SharedDesktopTemplate.model_validate({**self.template.model_dump(), **changes}, strict=True)

    def test_preparation_is_inert_while_live_predecessor_exists(self):
        predecessor = self.predecessor()
        with (patch('os.open') as opened, patch('sqlite3.connect') as database,
              patch('socket.socket') as socket, patch('subprocess.Popen') as process,
              patch('time.monotonic') as clock):
            plan = prepare_plan(self.template, predecessor=predecessor, new_session=self.new_session,
                                predecessor_snapshot_sha256=self.sha(canonical(predecessor).encode()))
            for operation in [opened, database, socket, process, clock]:
                operation.assert_not_called()
        self.assertFalse(plan.execution_authorized)
        self.assertFalse(Path(plan.session_directory).exists())
        self.assertFalse(Path(plan.workspace).exists())
        self.assertFalse(Path(plan.database).exists())
        self.assertNotIn('expires_monotonic', plan.model_dump())

    def test_absent_predecessor_supported_without_allocating_session(self):
        self.assertIsNone(self.plan.predecessor_session)
        self.assertIsNone(self.plan.predecessor_identity_sha256)
        self.assertEqual(self.plan.session_directory, str(Path(self.template.manager_base) / self.new_session))
        self.assertEqual(self.plan.database, str(Path(self.plan.session_directory) / 'trajectory.sqlite3'))

    def test_stable_predecessor_survives_legitimate_stop_but_not_identity_change(self):
        before = self.predecessor()
        stopped = {**before, 'phase': 'stopped', 'token_name': None, 'cleanup_verified': True}
        self.assertEqual(predecessor_identity_sha256(before), predecessor_identity_sha256(stopped))
        for field, value in [('session', self.new_session), ('url', 'http://127.0.0.1:8766/ui/'),
                             ('supervisor', {**self.process, 'start_ticks': 32})]:
            self.assertNotEqual(predecessor_identity_sha256(before),
                                predecessor_identity_sha256({**before, field: value}))

    def test_v2_predecessor_uses_exact_actual_service_binding(self):
        state = {'version': '2', 'session': 'app-' + '1' * 32, 'mode': 'shared', 'project': None,
            'url': self.template.url, 'started_at': '2026-10-03T00:00:00Z',
            'service_binding': {'unit': SHARED_DESKTOP_UNIT, 'process': self.process,
                                'invocation_id': '3' * 32, 'control_group': '/synthetic/unit'},
            'plan_sha256': 'a' * 64, 'activation_sha256': 'e' * 64, 'phase': 'running'}
        self.assertEqual(predecessor_identity_sha256(state),
                         predecessor_identity_sha256({**state, 'phase': 'stopped', 'task_admission_enabled': False}))
        changed = {**state, 'service_binding': {**state['service_binding'], 'invocation_id': '4' * 32}}
        self.assertNotEqual(predecessor_identity_sha256(state), predecessor_identity_sha256(changed))
        with self.assertRaises(ValueError):
            predecessor_identity_sha256({**state, 'service_binding': None})

    def test_plan_requires_exact_template_scope_and_new_identity(self):
        for changes in [{'template_sha256': 'f' * 64}, {'workspace': '/tmp/foreign'},
                        {'database': str(self.root / 'old.sqlite3')}, {'execution_authorized': True},
                        {'predecessor_session': 'app-' + '1' * 32}]:
            with self.assertRaises((ValueError, ValidationError)):
                SharedDesktopPlan.model_validate({**self.plan.model_dump(), **changes}, strict=True)
        with self.assertRaises(ValueError):
            prepare_plan(self.template, predecessor=self.predecessor(), new_session='app-' + '1' * 32)

    def test_unsupported_version_unit_profile_commands_secrets_and_fallback_deny(self):
        for changes in [{'version': '2'}, {'caller_unit': 'swapp-aos-gpu-joint-acceptance.service'},
                        {'caller_unit': 'aos-shared-desktop-default.service'}, {'launcher_profile': 'arbitrary'},
                        {'native_fallback': True}, {'command': ['sh', '-c', 'anything']},
                        {'env': {'TOKEN': 'synthetic'}}, {'token': 'synthetic'}]:
            with self.assertRaises(ValidationError):
                self.changed_template(**changes)

    def test_origin_paths_maps_and_limits_are_strict(self):
        for changes in [{'origin': 'https://127.0.0.1:8765'}, {'origin': 'http://localhost:8765'},
                        {'origin': 'http://127.0.0.1:00080'}, {'origin': 'http://127.0.0.1:99999'},
                        {'session_root': str(self.root / 'foreign')}, {'manager_base': '/tmp/../foreign'},
                        {'source_sha256': 'f' * 64}, {'python_sha256': 'f' * 64}]:
            with self.assertRaises((ValueError, ValidationError)):
                self.changed_template(**changes)
        with self.assertRaises(ValidationError):
            SharedDesktopLimits(cpu_quota_percent=True, memory_max_bytes=1073741824,
                                tasks_max=128, stop_timeout_seconds=5)

    def test_private_new_write_load_and_raw_hash_binding(self):
        for name, model, loader in [('template', self.template, load_template),
                                    ('plan', self.plan, load_plan), ('activation', self.activation, load_activation)]:
            path = self.root / (name + '.json')
            content = canonical(model.model_dump(mode='json')).encode()
            write_new_private_file(path, content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(loader(path, self.sha(content)), model)
            with self.assertRaises(FileExistsError):
                write_new_private_file(path, content)
            with self.assertRaises(ValueError):
                loader(path, 'f' * 64)

    def test_private_reader_and_writer_deny_symlinks_hardlinks_and_nonprivate_files(self):
        symbolic = self.root / 'symbolic'
        symbolic.symlink_to(self.config)
        with self.assertRaises(OSError):
            read_private_file(symbolic)
        with self.assertRaises(FileExistsError):
            write_new_private_file(symbolic, b'new')
        hard = self.root / 'hard'
        os.link(self.config, hard)
        with self.assertRaises(ValueError):
            read_private_file(hard)
        loose = self.file('loose', b'not private')
        loose.chmod(0o644)
        with self.assertRaises(ValueError):
            read_private_file(loose)
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(ValueError):
            write_new_private_file(public / 'new', b'new')

    def test_reader_deny_parent_symlink_fifo_oversize_and_mutation(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            read_private_file(alias / self.config.name)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, mode=0o600)
        with self.assertRaises(ValueError):
            read_private_file(fifo)
        with self.assertRaises(ValueError):
            read_private_file(self.config, limit=1)
        original = os.read
        def mutate(descriptor, count):
            value = original(descriptor, count)
            self.config.write_bytes(b'changed')
            return value
        with patch('os.read', side_effect=mutate):
            with self.assertRaises(ValueError):
                read_private_file(self.config)

    def test_duplicate_json_fields_rejected_even_under_selected_raw_hash(self):
        original = canonical(self.template.model_dump(mode='json'))
        content = ('{"version":"2",' + original[1:]).encode()
        path = self.file('duplicate.json', content)
        with self.assertRaises(ValueError):
            load_template(path, self.sha(content))

    def python_template(self):
        environment = self.root / '.venv'
        binary = environment / 'bin'
        binary.mkdir(parents=True, mode=0o700)
        config = environment / 'pyvenv.cfg'
        config.write_bytes(b'Synthetic venv config; never executed')
        config.chmod(0o600)
        first = binary / 'python'
        second = binary / 'python3'
        first.symlink_to('python3')
        second.symlink_to(self.python)
        sources = {**self.template.source_files, str(config): self.sha(config.read_bytes())}
        return self.changed_template(python_path=str(first), python_real_path=str(self.python),
            python_links={str(first): 'python3', str(second): str(self.python)},
            source_files=sources, source_sha256=digest(sources))

    def test_explicit_pinned_venv_chain_retains_original_invocation(self):
        template = self.python_template()
        verify_python(template)
        verify_template_files(template)
        self.assertNotEqual(template.python_path, template.python_real_path)
        with self.assertRaises(OSError):
            read_pinned_file(Path(template.python_path), template.python_sha256)

    def test_python_chain_extra_missing_loop_or_changed_target_denied(self):
        template = self.python_template()
        for links in [{}, {**template.python_links, str(self.root / 'extra'): str(self.python)},
                      {template.python_path: template.python_path}]:
            with self.assertRaises(ValueError):
                self.changed_template(python_path=template.python_path, python_real_path=template.python_real_path,
                    python_links=links, source_files=template.source_files, source_sha256=template.source_sha256)
        path = Path(template.python_path)
        path.unlink()
        path.symlink_to(self.launcher)
        with self.assertRaises(ValueError):
            verify_python(template)

    def test_pinned_python_directory_link_is_explicit_and_bounded(self):
        directory = self.root / 'python-alias'
        directory.symlink_to(self.root, target_is_directory=True)
        template = self.changed_template(python_path=str(directory / self.python.name),
            python_real_path=str(self.python), python_links={str(directory): str(self.root)})
        verify_python(template)
        with self.assertRaises(ValidationError):
            self.changed_template(python_links={str(self.root / f'link-{number}'): 'python' for number in range(9)})

    def test_activation_expiry_and_clock_are_finite_suspend_inclusive(self):
        for changes in [{'expires_monotonic': 100.0}, {'expires_monotonic': 1001.0},
                        {'expires_monotonic': float('inf')}, {'issued_monotonic': float('nan')},
                        {'clock': 'CLOCK_MONOTONIC'}, {'version': '2'}]:
            with self.assertRaises(ValidationError):
                SharedDesktopActivation.model_validate({**self.activation.model_dump(), **changes}, strict=True)
        self.assertEqual(self.activation.clock, 'CLOCK_BOOTTIME')

    def test_activation_hashes_and_freshness_do_not_grant_capability(self):
        with self.assertRaisesRegex(ValueError, 'verifier is unavailable'):
            verify_activation(self.plan, self.activation, current_boot_id=self.boot_id, now_monotonic=150.0)
        with self.assertRaises(ValueError):
            verify_activation(self.plan, self.activation, current_boot_id=self.boot_id, now_monotonic=150.0,
                              activation_verifier=lambda plan, activation: True)

    def test_unobserved_broker_allows_only_explicit_inert_preparation(self):
        values = self.template.model_dump()
        values.pop('broker_identity_sha256')
        with self.assertRaises(ValidationError):
            SharedDesktopTemplate.model_validate(values, strict=True)
        values['broker_identity_sha256'] = None
        template = SharedDesktopTemplate.model_validate(values, strict=True)
        plan = prepare_plan(template, predecessor=None, new_session=self.new_session)
        self.assertIsNone(plan.template.broker_identity_sha256)
        self.assertFalse(plan.execution_authorized)
        self.assertFalse(Path(plan.workspace).exists())
        activation = self.activation.model_copy(update={'plan_sha256': plan.plan_sha256()})
        with self.assertRaisesRegex(ValueError, 'verifier is unavailable'):
            verify_activation(plan, activation, current_boot_id=self.boot_id, now_monotonic=150.0)

    def test_stale_wrong_boot_wrong_plan_or_source_deny_before_trusted_hook(self):
        trusted = Mock()
        for boot, current in [(self.boot_id, 99.0), (self.boot_id, 200.0),
                              (self.boot_id, float('nan')), ('00000000-0000-0000-0000-000000000002', 150.0)]:
            with self.assertRaises(ValueError):
                verify_activation(self.plan, self.activation, current_boot_id=boot,
                                  now_monotonic=current, activation_verifier=trusted)
        wrong = self.activation.model_copy(update={'plan_sha256': 'f' * 64})
        with self.assertRaises(ValueError):
            verify_activation(self.plan, wrong, current_boot_id=self.boot_id, now_monotonic=150.0,
                              activation_verifier=trusted)
        trusted.assert_not_called()

    def test_fresh_exact_files_plus_explicit_synthetic_host_hook(self):
        trusted = Mock(return_value=None)
        verify_activation(self.plan, self.activation, current_boot_id=self.boot_id,
                          now_monotonic=150.0, activation_verifier=trusted)
        trusted.assert_called_once()
        self.config.write_bytes(b'{"synthetic":"changed"}')
        with self.assertRaises(ValueError):
            verify_activation(self.plan, self.activation, current_boot_id=self.boot_id,
                              now_monotonic=150.0, activation_verifier=trusted)
        self.assertEqual(trusted.call_count, 1)

    def test_config_closure_must_include_exact_reviewed_launch_input(self):
        changes = {'config_files': self.template.config_files, 'config_sha256': self.template.config_sha256}
        with self.assertRaises(ValidationError):
            SharedDesktopActivation.model_validate({**self.activation.model_dump(), **changes}, strict=True)

    def test_canonical_schemas_and_synthetic_examples_match_models(self):
        root = Path(__file__).resolve().parents[1]
        models = {'shared_desktop_template': SharedDesktopTemplate, 'shared_desktop_plan': SharedDesktopPlan,
                  'shared_desktop_activation': SharedDesktopActivation}
        for name, model in models.items():
            self.assertEqual(json.loads((root / f'schemas/{name}.schema.json').read_text()), model.model_json_schema())
            model.model_validate(json.loads((root / f'examples/{name}.json').read_text()), strict=True)


if __name__ == '__main__':
    unittest.main()
