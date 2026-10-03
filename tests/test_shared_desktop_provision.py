import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from pydantic import ValidationError

from aos.contracts import canonical, digest
from aos.shared_desktop_plan import SharedDesktopLimits, SharedDesktopTemplate, prepare_plan
from aos.shared_desktop_provision import (
    LAUNCH_INTENT_NAME, PROVISION_NAME, SharedDesktopLaunchIntent, SharedDesktopProvision,
    _directory_entries, claim_launch, load_provision, provision_scope, verify_launch_intent,
)


class SharedDesktopProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = self.root / 'manager'
        self.manager.mkdir(mode=0o700)
        self.manager.chmod(0o700)
        self.marker = self.manager / 'current.json'
        self.marker.write_bytes(b'{"synthetic":"unchanged existing manager record"}')
        self.marker.chmod(0o600)
        sources = {str(self.root / 'synthetic-python'): 'a' * 64,
                   str(self.root / 'synthetic-launcher.py'): 'b' * 64}
        configs = {str(self.root / 'synthetic-config.json'): 'c' * 64}
        self.template = SharedDesktopTemplate(origin='http://127.0.0.1:8765', manager_base=str(self.manager),
            session_root=str(self.manager), python_path=str(self.root / 'synthetic-python'), python_sha256='a' * 64,
            launcher_path=str(self.root / 'synthetic-launcher.py'), launcher_sha256='b' * 64,
            source_files=sources, source_sha256=digest(sources), config_files=configs, config_sha256=digest(configs),
            broker_socket=str(self.root / 'synthetic-broker.sock'), broker_identity_sha256='d' * 64,
            limits=SharedDesktopLimits(cpu_quota_percent=200, memory_max_bytes=1073741824,
                                       tasks_max=128, stop_timeout_seconds=5))
        self.plan = prepare_plan(self.template, predecessor=None, new_session='app-' + '1' * 32)
        self.boot = '00000000-0000-0000-0000-000000000001'
        self.activation_sha = 'e' * 64

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def receipt_path(self):
        return Path(self.plan.session_directory) / PROVISION_NAME

    @property
    def intent_path(self):
        return Path(self.plan.session_directory) / LAUNCH_INTENT_NAME

    def provision(self):
        return provision_scope(self.plan, current_boot_id=self.boot)

    def load(self, receipt, **options):
        return load_provision(self.plan, self.receipt_path, receipt.provision_sha256(),
                              current_boot_id=self.boot, **options)

    def rewrite(self, value):
        content = canonical(value).encode()
        self.receipt_path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def test_directory_entries_reset_retained_descriptor_after_each_mutation(self):
        directory = self.root / 'synthetic-listing'
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            with patch('aos.shared_desktop_provision.os.lseek', wraps=os.lseek) as reset:
                self.assertEqual(_directory_entries(descriptor), [])
                os.mkdir('workspace', 0o700, dir_fd=descriptor)
                self.assertEqual(set(_directory_entries(descriptor)), {'workspace'})
                os.mkdir('synthetic-second-child', 0o700, dir_fd=descriptor)
                self.assertEqual(set(_directory_entries(descriptor)), {'workspace', 'synthetic-second-child'})
                self.assertEqual(reset.call_args_list, [call(descriptor, 0, os.SEEK_SET)] * 3)
            after = os.fstat(descriptor)
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        finally:
            os.close(descriptor)

    def test_directory_entries_seek_failure_has_no_enumeration_fallback(self):
        with (patch('aos.shared_desktop_provision.os.lseek', side_effect=OSError('synthetic seek failure')),
              patch('aos.shared_desktop_provision.os.listdir') as enumeration):
            with self.assertRaises(OSError):
                _directory_entries(123)
            enumeration.assert_not_called()

    def test_real_private_empty_scope_and_fixed_receipt_without_runtime_actions(self):
        marker = self.marker.read_bytes()
        marker_inode = self.marker.stat().st_ino
        with (patch('sqlite3.connect') as database, patch('socket.socket') as socket,
              patch('subprocess.Popen') as process):
            receipt = self.provision()
            for operation in (database, socket, process):
                operation.assert_not_called()
        for path in [self.manager, Path(self.plan.session_directory), Path(self.plan.workspace)]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.receipt_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(os.listdir(self.plan.session_directory)), {'workspace', PROVISION_NAME})
        self.assertEqual(os.listdir(self.plan.workspace), [])
        self.assertFalse(Path(self.plan.database).exists())
        self.assertFalse(Path(self.template.broker_socket).exists())
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(self.marker.stat().st_ino, marker_inode)
        self.assertTrue(receipt.preparation_only)
        self.assertFalse(receipt.runtime_authority)
        self.assertFalse(receipt.execution_authorized)
        self.assertFalse(receipt.runtime_started)
        self.assertEqual(self.load(receipt), receipt)
        self.assertEqual(hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(), receipt.provision_sha256())
        for identity in (receipt.manager_identity, receipt.session_identity, receipt.workspace_identity):
            self.assertEqual(identity.owner_uid, os.getuid())
            self.assertNotIn('mtime', str(identity.model_dump()))
            self.assertNotIn('ctime', str(identity.model_dump()))

    def test_duplicate_provision_never_adopts_or_rewrites_existing_scope(self):
        receipt = self.provision()
        content = self.receipt_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.provision()
        self.assertEqual(self.receipt_path.read_bytes(), content)
        self.assertEqual(self.load(receipt), receipt)

    def test_invalid_boot_and_nonprivate_manager_deny_before_allocation(self):
        with self.assertRaises(ValueError):
            provision_scope(self.plan, current_boot_id='invalid')
        self.manager.chmod(0o755)
        with self.assertRaises(ValueError):
            self.provision()
        self.assertFalse(Path(self.plan.session_directory).exists())

    def test_wrong_scope_boot_hash_or_fixed_receipt_path_rejected(self):
        receipt = self.provision()
        for path, expected, boot in [(self.receipt_path, 'f' * 64, self.boot),
                                     (self.manager / 'wrong.json', receipt.provision_sha256(), self.boot),
                                     (self.receipt_path, receipt.provision_sha256(), '00000000-0000-0000-0000-000000000002')]:
            with self.assertRaises((ValueError, OSError)):
                load_provision(self.plan, path, expected, current_boot_id=boot)
        changed = self.template.model_copy(update={'limits': self.template.limits.model_copy(update={'tasks_max': 64})})
        wrong_plan = prepare_plan(changed, predecessor=None, new_session=self.plan.app_session)
        with self.assertRaises(ValueError):
            load_provision(wrong_plan, self.receipt_path, receipt.provision_sha256(), current_boot_id=self.boot)

    def test_forged_owner_or_inode_receipt_cannot_replace_physical_identity(self):
        receipt = self.provision()
        original = receipt.model_dump(mode='json')
        for field in ('owner_uid', 'inode', 'device'):
            value = copy.deepcopy(original)
            if field == 'owner_uid':
                for name in ('manager_identity', 'session_identity', 'workspace_identity'):
                    value[name][field] += 1
            else:
                value['workspace_identity'][field] += 1
            expected = self.rewrite(value)
            with self.assertRaises(ValueError):
                load_provision(self.plan, self.receipt_path, expected, current_boot_id=self.boot)
        self.rewrite(original)
        self.assertEqual(self.load(receipt), receipt)

    def test_changed_directory_mode_and_replaced_workspace_inode_rejected(self):
        receipt = self.provision()
        workspace = Path(self.plan.workspace)
        workspace.chmod(0o755)
        with self.assertRaises(ValueError):
            self.load(receipt)
        workspace.chmod(0o700)
        workspace.rename(workspace.parent / 'old-workspace')
        workspace.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.load(receipt, require_pristine=False)

    def test_replaced_manager_inode_cannot_adopt_copied_scope(self):
        receipt = self.provision()
        self.manager.rename(self.root / 'original-manager')
        self.manager.mkdir(mode=0o700)
        session = self.manager / self.plan.app_session
        session.mkdir(mode=0o700)
        (session / 'workspace').mkdir(mode=0o700)
        self.receipt_path.write_bytes(canonical(receipt.model_dump(mode='json')).encode())
        self.receipt_path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.load(receipt)

    def test_receipt_symlink_hardlink_nonprivate_or_duplicate_json_rejected(self):
        receipt = self.provision()
        content = self.receipt_path.read_bytes()
        hard = self.root / 'receipt-hardlink'
        os.link(self.receipt_path, hard)
        with self.assertRaises(ValueError):
            self.load(receipt)
        hard.unlink()
        self.receipt_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load(receipt)
        self.receipt_path.chmod(0o600)
        self.receipt_path.unlink()
        hard.write_bytes(content)
        hard.chmod(0o600)
        self.receipt_path.symlink_to(hard)
        with self.assertRaises(OSError):
            self.load(receipt)
        self.receipt_path.unlink()
        duplicate = b'{"version":"1",' + content[1:]
        self.receipt_path.write_bytes(duplicate)
        self.receipt_path.chmod(0o600)
        with self.assertRaises(ValueError):
            load_provision(self.plan, self.receipt_path, hashlib.sha256(duplicate).hexdigest(), current_boot_id=self.boot)

    def test_workspace_or_manager_symlinks_are_not_scope_identities(self):
        receipt = self.provision()
        workspace = Path(self.plan.workspace)
        workspace.rename(workspace.parent / 'old-workspace')
        workspace.symlink_to(workspace.parent / 'old-workspace', target_is_directory=True)
        with self.assertRaises(OSError):
            self.load(receipt, require_pristine=False)
        alias = self.root / 'manager-alias'
        alias.symlink_to(self.manager, target_is_directory=True)
        template = self.template.model_copy(update={'manager_base': str(alias), 'session_root': str(alias)})
        plan = prepare_plan(template, predecessor=None, new_session='app-' + '2' * 32)
        with self.assertRaises(OSError):
            provision_scope(plan, current_boot_id=self.boot)

    def test_pristine_required_before_launch_but_status_allows_later_runtime_contents(self):
        receipt = self.provision()
        Path(self.plan.workspace, 'synthetic-result.txt').write_bytes(b'Synthetic future content')
        with self.assertRaises(ValueError):
            self.load(receipt)
        self.assertEqual(self.load(receipt, require_pristine=False), receipt)
        Path(self.plan.database).write_bytes(b'Synthetic bytes, not a SQLite database')
        self.assertEqual(self.load(receipt, require_pristine=False), receipt)
        with self.assertRaises(ValueError):
            claim_launch(self.plan, receipt, receipt.provision_sha256(), self.activation_sha, current_boot_id=self.boot)
        self.assertFalse(self.intent_path.exists())

    def test_partial_mkdir_failure_never_cleans_or_adopts(self):
        original = os.mkdir
        def fail_workspace(name, *arguments, **keywords):
            if name == 'workspace':
                raise OSError('Synthetic allocation interruption')
            return original(name, *arguments, **keywords)
        with patch('os.mkdir', side_effect=fail_workspace):
            with self.assertRaises(OSError):
                self.provision()
        self.assertTrue(Path(self.plan.session_directory).is_dir())
        self.assertFalse(Path(self.plan.workspace).exists())
        self.assertFalse(self.receipt_path.exists())
        with self.assertRaises(FileExistsError):
            self.provision()

    def test_allocation_session_replacement_is_not_recaptured_as_fresh(self):
        original = os.mkdir
        displaced = self.manager / 'displaced-original-session'
        def replace_after_workspace(name, *arguments, **keywords):
            result = original(name, *arguments, **keywords)
            if name == 'workspace':
                Path(self.plan.session_directory).rename(displaced)
                original(self.plan.session_directory, mode=0o700)
                original(self.plan.workspace, mode=0o700)
            return result
        with patch('os.mkdir', side_effect=replace_after_workspace):
            with self.assertRaises(ValueError):
                self.provision()
        self.assertTrue(displaced.is_dir())
        self.assertFalse(self.receipt_path.exists())
        self.assertFalse((displaced / PROVISION_NAME).exists())

    def test_empty_workspace_and_receipt_publication_are_fsynced(self):
        original = os.fsync
        flushed = []
        def record(descriptor):
            flushed.append(os.fstat(descriptor).st_ino)
            return original(descriptor)
        with patch('os.fsync', side_effect=record):
            receipt = self.provision()
        for identity in (receipt.manager_identity, receipt.session_identity, receipt.workspace_identity):
            self.assertIn(identity.inode, flushed)
        self.assertIn(self.receipt_path.stat().st_ino, flushed)

    def test_launch_claim_is_exact_exclusive_durable_and_non_authorizing(self):
        receipt = self.provision()
        intent = claim_launch(self.plan, receipt, receipt.provision_sha256(), self.activation_sha,
                              current_boot_id=self.boot)
        self.assertEqual(self.intent_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(intent.execution_authorized)
        self.assertFalse(intent.runtime_authority)
        self.assertFalse(Path(self.plan.database).exists())
        self.assertEqual(verify_launch_intent(self.plan, receipt.provision_sha256(), self.activation_sha,
                                             current_boot_id=self.boot), intent)
        content = self.intent_path.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            claim_launch(self.plan, receipt, receipt.provision_sha256(), self.activation_sha, current_boot_id=self.boot)
        self.assertEqual(self.intent_path.read_bytes(), content)
        with self.assertRaises(ValueError):
            self.load(receipt)

    def test_foreign_provision_or_activation_cannot_rebind_launch_intent(self):
        receipt = self.provision()
        wrong = receipt.model_copy(update={'boot_id': '00000000-0000-0000-0000-000000000002'})
        with self.assertRaises(ValueError):
            claim_launch(self.plan, wrong, receipt.provision_sha256(), self.activation_sha, current_boot_id=self.boot)
        self.assertFalse(self.intent_path.exists())
        claim_launch(self.plan, receipt, receipt.provision_sha256(), self.activation_sha, current_boot_id=self.boot)
        for provision_hash, activation_hash, boot in [(receipt.provision_sha256(), 'f' * 64, self.boot),
                ('f' * 64, self.activation_sha, self.boot),
                (receipt.provision_sha256(), self.activation_sha, '00000000-0000-0000-0000-000000000002')]:
            with self.assertRaises(ValueError):
                verify_launch_intent(self.plan, provision_hash, activation_hash, current_boot_id=boot)

    def test_launch_verification_remains_bound_after_runtime_contents(self):
        receipt = self.provision()
        intent = claim_launch(self.plan, receipt, receipt.provision_sha256(), self.activation_sha, current_boot_id=self.boot)
        Path(self.plan.workspace, 'synthetic-output.txt').write_bytes(b'Synthetic future runtime output')
        self.assertEqual(verify_launch_intent(self.plan, receipt.provision_sha256(), self.activation_sha,
                                             current_boot_id=self.boot), intent)

    def test_receipt_or_intent_cannot_claim_runtime_authority(self):
        receipt = self.provision()
        for field in ['execution_authorized', 'runtime_started', 'runtime_authority']:
            with self.assertRaises(ValidationError):
                SharedDesktopProvision.model_validate({**receipt.model_dump(), field: True}, strict=True)
        with self.assertRaises(ValidationError):
            SharedDesktopProvision.model_validate({**receipt.model_dump(), 'expires_monotonic': 900.0}, strict=True)

    def test_canonical_schemas_and_synthetic_examples_match_models(self):
        root = Path(__file__).resolve().parents[1]
        for name, model in [('shared_desktop_provision', SharedDesktopProvision),
                            ('shared_desktop_launch_intent', SharedDesktopLaunchIntent)]:
            self.assertEqual(json.loads((root / f'schemas/{name}.schema.json').read_text()), model.model_json_schema())
            model.model_validate(json.loads((root / f'examples/{name}.json').read_text()), strict=True)


if __name__ == '__main__':
    unittest.main()
