"""Synthetic original SQLite and explicit mock observation providers; no GPU."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_intents import scientist_unresolved_predicate
from aos.scientist_no_admission import (
    SCHEMA, SCHEMA_SHA256, ScientistNoAdmissionCanonicalVerifier, ScientistNoAdmissionJournal,
    ScientistNoAdmissionObserverVerifier, ScientistNoAdmissionPhysicalVerifier,
    ScientistNoAdmissionRecoveryVerifier, ScientistNoAdmissionVerifier, sqlite_store_identity,
)
from aos.scientist_release_proof import ScientistPhysicalReleaseVerifier
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_terminal as terminal_cases


class ScientistNoAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = terminal_cases.ScientistTerminalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.schema = (REPO_ROOT / 'schemas/scientist_no_admission_observation.schema.json').read_bytes()
        self.clock = 200000000
        self.record = self.fixture.original.model_dump(mode='json')
        self.binding = self.fixture.binding.model_dump(mode='json')
        self.store.connection.execute("UPDATE desktop_sessions SET status='stopped',owner='PAUSED',generation=1,lease_id='synthetic-cleanup-lease'")
        self.store.connection.commit()
        self.callbacks = [Mock(return_value=None) for name in ('observer', 'canonical', 'physical', 'recovery')]
        self.value = self.observation()
        self.before = self.original_rows()

    def original_rows(self):
        return {table: [dict(row) for row in self.store.connection.execute('SELECT * FROM '+table)]
                for table in ('scientist_turn_intents', 'scientist_admission_history', 'desktop_sessions')}

    def observation(self):
        record, binding = self.record, self.binding
        original_binding = record['admission_binding']
        caller, broker = original_binding['caller_generation'], original_binding['server_generation']
        store = sqlite_store_identity(self.store)
        observer_generation = {**broker, 'pid': broker['pid'] + 100, 'start_ticks': broker['start_ticks'] + 100,
                               'unit': 'synthetic-observer.service', 'invocation_id': 'e' * 32}
        capability = {'feature': 'no-admission-observation.v1', 'version': 1, 'schema_sha256': SCHEMA_SHA256,
                      'generation_sha256': digest(observer_generation), 'source_sha256': 'a' * 64,
                      'config_sha256': 'b' * 64, 'purpose': 'observe_no_admission'}
        target = {'request_id': record['request_id'], 'request_sha256': record['request_sha256'],
                  'original_caller_generation_sha256': digest(caller)}
        scope = {'store': store, 'session_id': binding['session_id'], 'runtime_id': binding['runtime_id'],
                 'original_generation': binding['generation'], 'current_generation': 1, 'current_status': 'stopped',
                 'current_owner': 'PAUSED', 'lease_id': 'synthetic-cleanup-lease',
                 'authorization_context_sha256': 'c' * 64, 'purpose': 'observe_no_admission'}
        physical = {'target': target, 'caller_generation': caller, 'caller_generation_sha256': digest(caller),
                    'broker_generation': broker, 'broker_generation_sha256': digest(broker), 'children': [],
                    'caller_absent': True, 'broker_absent': True, 'original_cgroups_absent': True,
                    'children_absent': True, 'provenance_complete': True, 'late_dispatch_fenced': True}
        physical['proof_sha256'] = digest(physical)
        freshness = {'clock': 'CLOCK_BOOTTIME', 'unit': 'microseconds', 'boot_id': broker['boot_id'],
                     'observed_boottime_us': self.clock, 'expires_boottime_us': self.clock + 60000000,
                     'max_age_us': 60000000}
        canonical_store = {**store, 'inode': store['inode'] + 1}
        tombstone = {'id': 'd' * 32, 'target': target, 'store': canonical_store,
                     'admission_record_sha256': digest(record), 'intent_binding_sha256': digest(binding),
                     'observer_generation_sha256': digest(observer_generation),
                     'physical_proof_sha256': physical['proof_sha256'], 'freshness': freshness,
                     'late_admission_fenced': True}
        canonical_proof = {'target': target, 'store': canonical_store, 'admission_absent': True,
                           'gpu_queue_absent': True, 'allocation_absent': True, 'child_binding_absent': True,
                           'deferred_execution_absent': True, 'quarantined_allocation_absent': True,
                           'tombstone': tombstone, 'tombstone_sha256': digest(tombstone)}
        request_json = self.store.connection.execute('SELECT request_json FROM scientist_turn_intents').fetchone()[0]
        capture = {key: record[key] for key in ('admission_binding', 'capability_sha256', 'capability_freshness')}
        original = {'request_canonical': request_json, 'admission_record': record, 'admission_record_sha256': digest(record),
                    'admission_binding_sha256': record['admission_binding_sha256'], 'admission_capture_sha256': digest(capture),
                    'intent_binding': binding, 'intent_binding_sha256': digest(binding),
                    'broker_generation': broker, 'broker_generation_sha256': digest(broker), 'store': store}
        observer = {'generation': observer_generation, 'generation_sha256': digest(observer_generation),
                    'source_sha256': capability['source_sha256'], 'config_sha256': capability['config_sha256'],
                    'capability': capability, 'capability_sha256': digest(capability), 'purpose': 'observe_no_admission'}
        value = {'schema': SCHEMA, 'version': 1, 'target': target, 'original': original, 'observer': observer,
                 'canonical': canonical_proof, 'physical': physical, 'cleanup_scope': scope,
                 'outcome': 'never_received', 'admission_budget': None, 'freshness': freshness}
        value['observation_sha256'] = digest(value)
        return value

    def verifier(self, **changes):
        options = {'reviewed_schema_bytes': self.schema, 'schema_sha256': SCHEMA_SHA256,
                   'verify_observer': self.callbacks[0], 'verify_canonical': self.callbacks[1],
                   'verify_physical': self.callbacks[2], 'verify_recovery': self.callbacks[3],
                   'clock': lambda: self.clock, 'boot_id': lambda: self.record['capability_freshness']['boot_id']}
        return ScientistNoAdmissionVerifier(self.fixture.history, **(options | changes))

    def append(self, value=None):
        return ScientistNoAdmissionJournal(self.verifier()).append(canonical(value or self.value).encode())

    def count(self):
        return self.store.connection.execute('SELECT count(*) FROM scientist_no_admission_closures').fetchone()[0]

    def test_default_denies_without_modifying_original_or_closure(self):
        verifier = ScientistNoAdmissionVerifier(self.fixture.history, reviewed_schema_bytes=self.schema, schema_sha256=SCHEMA_SHA256)
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionJournal(verifier).append(canonical(self.value).encode())
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.before)

    def test_append_preserves_original_and_retry_is_exact_and_append_only(self):
        predicate = scientist_unresolved_predicate(self.store.connection)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_turn_intents WHERE '+predicate).fetchone()[0], 1)
        first = self.append()
        self.assertEqual(self.append(), first)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.original_rows(), self.before)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_turn_intents WHERE '+predicate).fetchone()[0], 0)
        for callback in self.callbacks:
            self.assertEqual(callback.call_count, 4)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('DELETE FROM scientist_no_admission_closures')
        self.store.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('INSERT OR REPLACE INTO scientist_no_admission_closures VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(first.values()))
        self.store.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE scientist_no_admission_closures SET created_at='changed'")
        self.store.connection.rollback()

    def test_independent_observer_source_pin_rejects_consistent_forged_hashes(self):
        value = deepcopy(self.value)
        value['observer']['source_sha256'] = 'f' * 64
        value['observer']['capability']['source_sha256'] = 'f' * 64
        value['observer']['capability_sha256'] = digest(value['observer']['capability'])
        value['observation_sha256'] = digest({key: item for key, item in value.items() if key != 'observation_sha256'})

        def reviewed_observer(proof):
            if proof['observer'] != self.value['observer']:
                raise ScientistAdmissionError('Synthetic independently reviewed observer source differs')

        self.callbacks[0].side_effect = reviewed_observer
        with self.assertRaises(ScientistAdmissionError):
            self.append(value)
        self.assertEqual(self.count(), 0)

    def test_cleanup_failure_and_post_insert_revocation_roll_back(self):
        for callback in self.callbacks:
            callback.side_effect = [None, ScientistAdmissionError('synthetic cleanup authority revoked')]
            with self.assertRaises(ScientistAdmissionError):
                self.append()
            callback.side_effect = None
            self.assertEqual(self.count(), 0)
            self.assertEqual(self.original_rows(), self.before)

    def test_wrong_lease_generation_source_version_or_expired_proof_denied(self):
        for path, replacement in [(('cleanup_scope', 'lease_id'), 'wrong'), (('cleanup_scope', 'current_generation'), 2),
                                  (('original', 'admission_record_sha256'), 'f' * 64),
                                  (('observer', 'capability', 'schema_sha256'), 'f' * 64),
                                  (('version',), 2), (('admission_budget',), {}),
                                  (('canonical', 'admission_absent'), False)]:
            value = deepcopy(self.value)
            container = value
            for key in path[:-1]:
                container = container[key]
            container[path[-1]] = replacement
            value['observation_sha256'] = digest({key: item for key, item in value.items() if key != 'observation_sha256'})
            with self.subTest(path=path), self.assertRaises(ScientistAdmissionError):
                self.append(value)
            self.assertEqual(self.count(), 0)
        self.clock += 60000000
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.count(), 0)

    def test_callback_owner_takeover_and_exception_roll_back(self):
        def takeover(value):
            self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1')
        self.callbacks[3].side_effect = takeover
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.original_rows(), self.before)
        self.assertEqual(self.count(), 0)

    def test_post_insert_callback_cannot_commit_unverified_closure(self):
        calls = []

        def commit(value):
            calls.append(value)
            if len(calls) == 2:
                self.store.connection.commit()

        self.callbacks[3].side_effect = commit
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.before)

    def test_closure_does_not_reauthorize_old_generation(self):
        self.append()
        request = self.fixture.request.model_copy(update={'request_id': 'f' * 32})
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.verify_admission(request)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('UPDATE scientist_turn_intents SET deadline=deadline+1')
        self.store.connection.rollback()

    def test_conflicting_retry_and_late_expiry_roll_back_only_closure(self):
        self.append()
        value = deepcopy(self.value)
        value['cleanup_scope']['authorization_context_sha256'] = 'f' * 64
        value['observation_sha256'] = digest({key: item for key, item in value.items() if key != 'observation_sha256'})
        with self.assertRaises(ScientistAdmissionError):
            self.append(value)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.original_rows(), self.before)

    def test_post_validation_expiry_rolls_back_inserted_closure(self):
        calls = []

        def expire(value):
            calls.append(value)
            if len(calls) == 2:
                self.clock += 60000000

        self.callbacks[3].side_effect = expire
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.before)

    def test_interrupt_rolls_back_without_leaving_sql_authorizer(self):
        self.callbacks[3].side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.append()
        self.assertEqual(self.count(), 0)
        self.store.connection.execute('UPDATE desktop_sessions SET generation=generation')
        self.store.connection.rollback()


class ScientistNoAdmissionPhysicalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScientistNoAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.value = deepcopy(self.fixture.value)
        self.provenance = Mock(return_value=None)
        self.current = Mock(return_value=None)
        self.provider = ScientistNoAdmissionPhysicalVerifier(verify_provenance=self.provenance,
                                                           verify_current=self.current)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.generations = [self.value['physical']['caller_generation'], self.value['physical']['broker_generation']]
        self.write('/proc/sys/kernel/random/boot_id', self.generations[0]['boot_id'])
        self.write('/proc/self/mountinfo', '1 0 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n')
        self.write('/sys/fs/cgroup/cgroup.procs', '')
        self.write('/sys/fs/cgroup/cgroup.controllers', 'cpu memory pids\n')
        real_open = os.open

        def synthetic_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).startswith(('/proc/', '/sys/fs/cgroup')):
                path = self.root / str(path).lstrip('/')
            return real_open(path, flags, mode, dir_fd=dir_fd)

        self.enterContext(patch('aos.scientist_no_admission.os.open', side_effect=synthetic_open))
        self.enterContext(patch.object(ScientistPhysicalReleaseVerifier, '_environment', return_value={}))
        self.commands = self.enterContext(patch.object(ScientistPhysicalReleaseVerifier, '_command',
            return_value='LoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n'))

    def write(self, path, value):
        destination = self.root / path.lstrip('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value, encoding='ascii')

    def test_unconfigured_provider_denies_before_any_observation(self):
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionPhysicalVerifier()(self.value)
        self.commands.assert_not_called()

    def test_absent_originals_read_back_twice_without_gpu_or_mutation(self):
        before = deepcopy(self.value)
        self.assertIsNone(self.provider(self.value))
        self.assertEqual(self.value, before)
        self.assertEqual(self.provenance.call_count, 2)
        self.assertEqual(self.current.call_count, 2)
        self.assertEqual(self.commands.call_count, 4)
        for call in self.commands.call_args_list:
            self.assertEqual(call.args[0][0], '/usr/bin/systemctl')

    def test_journal_composes_physical_readback_before_and_after_insert(self):
        before = self.fixture.original_rows()
        verifier = self.fixture.verifier(verify_physical=self.provider)
        result = ScientistNoAdmissionJournal(verifier).append(canonical(self.value).encode())
        self.assertEqual(result['request_id'], self.value['target']['request_id'])
        self.assertEqual(self.fixture.count(), 1)
        self.assertEqual(self.fixture.original_rows(), before)
        self.assertEqual(self.commands.call_count, 8)

    def test_wrong_uid_and_failed_process_read_deny(self):
        with patch('aos.scientist_no_admission.os.getuid', return_value=self.generations[0]['uid'] + 1):
            with self.assertRaises(ScientistAdmissionError):
                self.provider(self.value)
        with patch.object(self.provider, '_process', side_effect=PermissionError('synthetic unreadable process')):
            with self.assertRaises(ScientistAdmissionError):
                self.provider(self.value)

    def test_existing_or_reused_pid_denies_even_when_all_flags_claim_absence(self):
        self.write('/proc/'+str(self.generations[0]['pid'])+'/stat', 'synthetic reused process')
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)


    def test_empty_original_cgroup_is_not_absent(self):
        (self.root / ('sys/fs/cgroup'+self.generations[0]['control_group'])).mkdir(parents=True)
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)

    def test_cgroup_symlink_is_not_followed(self):
        component = self.generations[0]['control_group'].split('/')[1]
        (self.root / 'sys/fs/cgroup' / component).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)

    def test_unit_replacement_or_late_start_denies(self):
        collected = self.commands.return_value
        self.commands.side_effect = [collected, collected, collected.replace('MainPID=0', 'MainPID=123')]
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)

    def test_post_observation_revoke_denies(self):
        self.current.side_effect = [None, ScientistAdmissionError('synthetic revoke')]
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)

    def test_unavailable_provenance_and_cleanup_deny(self):
        self.provenance.side_effect = ScientistAdmissionError('synthetic unknown child or failed cleanup')
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)
        self.commands.assert_not_called()

    def test_wrong_boot_and_unreadable_mount_deny(self):
        self.write('/proc/sys/kernel/random/boot_id', '22222222-2222-2222-2222-222222222222')
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)
        self.write('/proc/sys/kernel/random/boot_id', self.generations[0]['boot_id'])
        self.write('/proc/self/mountinfo', '')
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)

    def test_deadline_does_not_restart_after_authority_work(self):
        with patch('aos.scientist_no_admission.time.monotonic', side_effect=[100.0, 111.0]):
            with self.assertRaises(ScientistAdmissionError):
                self.provider(self.value)
        self.commands.assert_not_called()

    def test_children_are_observed_and_duplicate_provenance_denies(self):
        child = {**self.generations[1], 'pid': self.generations[1]['pid'] + 200,
                 'unit': 'synthetic-child.service', 'invocation_id': '9' * 32,
                 'control_group': '/synthetic-absent-child'}
        self.value['physical']['children'] = [{'generation': child, 'generation_sha256': digest(child),
                                             'process_absent': True, 'cgroup_absent': True}]
        self.assertIsNone(self.provider(self.value))
        self.assertEqual(self.commands.call_count, 6)
        self.write('/proc/'+str(child['pid'])+'/stat', 'synthetic child still running')
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)
        child['pid'] = self.generations[0]['pid']
        self.value['physical']['children'][0]['generation_sha256'] = digest(child)
        with self.assertRaises(ScientistAdmissionError):
            self.provider(self.value)


class ScientistNoAdmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScientistNoAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.value = deepcopy(self.fixture.value)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'synthetic-cleanup-authority.json'
        self.current = Mock(return_value=None)
        self.artifact = {'schema': 'aos-scientist-no-admission-cleanup-authority.v1', 'version': 1,
                         'target': self.value['target'],
                         'cleanup_scope_without_context': {key: value for key, value in self.value['cleanup_scope'].items()
                                                           if key != 'authorization_context_sha256'},
                         'observer_unit': self.value['observer']['generation']['unit'],
                         'purpose': 'observe_no_admission', 'inference_allowed': False}
        self.write_artifact()

    def write_artifact(self):
        self.raw = canonical(self.artifact).encode()
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)
        self.checksum = digest(self.artifact)
        self.value['cleanup_scope']['authorization_context_sha256'] = self.checksum
        self.value['observation_sha256'] = digest({key: value for key, value in self.value.items()
                                                  if key != 'observation_sha256'})

    def provider(self, **changes):
        options = {'authority_path': self.path, 'authority_sha256': self.checksum,
                   'authority_raw_sha256': self.checksum, 'verify_current': self.current}
        return ScientistNoAdmissionRecoveryVerifier(**(options | changes))

    def test_current_authority_remains_mandatory_even_with_correct_file_and_pins(self):
        provider = ScientistNoAdmissionRecoveryVerifier(authority_path=self.path,
            authority_sha256=self.checksum, authority_raw_sha256=self.checksum)
        with self.assertRaises(ScientistAdmissionError):
            provider(self.value)

    def test_reviewed_artifact_is_read_independently_before_and_after_authority(self):
        before = deepcopy(self.value)
        self.assertIsNone(self.provider()(self.value))
        self.assertEqual(self.value, before)
        self.assertEqual(self.current.call_count, 2)

    def test_recovery_composes_with_original_journal_without_rewriting_intent(self):
        before = self.fixture.original_rows()
        verifier = self.fixture.verifier(verify_recovery=self.provider())
        result = ScientistNoAdmissionJournal(verifier).append(canonical(self.value).encode())
        self.assertEqual(result['request_id'], self.value['target']['request_id'])
        self.assertEqual(self.fixture.count(), 1)
        self.assertEqual(self.fixture.original_rows(), before)
        self.assertEqual(self.current.call_count, 4)

    def test_self_asserted_target_scope_context_or_observer_does_not_authorize(self):
        for field in ('request_id', 'lease_id', 'context', 'observer'):
            with self.subTest(field=field):
                value = deepcopy(self.value)
                if field == 'request_id':
                    value['target']['request_id'] = '8' * 32
                elif field == 'lease_id':
                    value['cleanup_scope']['lease_id'] = 'synthetic-forged-lease'
                elif field == 'context':
                    value['cleanup_scope']['authorization_context_sha256'] = '9' * 64
                else:
                    value['observer']['generation']['unit'] = 'synthetic-other-observer.service'
                with self.assertRaises(ScientistAdmissionError):
                    self.provider()(value)

    def test_wrong_independent_canonical_or_raw_pin_denies(self):
        for key in ('authority_sha256', 'authority_raw_sha256'):
            with self.subTest(key=key), self.assertRaises(ScientistAdmissionError):
                self.provider(**{key: '9' * 64})(self.value)

    def test_noncanonical_bytes_and_private_permission_change_deny(self):
        self.path.write_bytes(self.raw + b'\n')
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)
        self.path.write_bytes(self.raw)
        self.path.chmod(0o644)
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_symlink_hardlink_and_missing_artifact_deny(self):
        alias = self.path.with_name('alias.json')
        alias.symlink_to(self.path)
        with self.assertRaises(ScientistAdmissionError):
            self.provider(authority_path=alias)(self.value)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)
        alias.unlink()
        self.path.unlink()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_post_read_revoke_or_same_byte_inode_replacement_denies(self):
        self.current.side_effect = [None, ScientistAdmissionError('synthetic authorization revoked')]
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

        def replace_after_read():
            replacement = self.path.with_name('replacement.json')
            replacement.write_bytes(self.raw)
            replacement.chmod(0o600)
            replacement.replace(self.path)

        self.current.reset_mock()
        self.current.side_effect = lambda proof: replace_after_read() if self.current.call_count == 2 else None
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_closed_contract_rejects_extra_fields_bool_version_and_inference_permission(self):
        for field, value in (('unexpected', True), ('version', True), ('inference_allowed', True)):
            with self.subTest(field=field):
                original = deepcopy(self.artifact)
                self.artifact[field] = value
                self.write_artifact()
                with self.assertRaises(ScientistAdmissionError):
                    self.provider()(self.value)
                self.artifact = original
                self.write_artifact()

    def test_provider_mutation_cannot_swap_reviewed_context(self):
        provider = self.provider()

        def swap(proof):
            provider.authority_sha256 = '9' * 64

        self.current.side_effect = swap
        with self.assertRaises(ScientistAdmissionError):
            provider(self.value)


class ScientistNoAdmissionCanonicalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScientistNoAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'synthetic-canonical.sqlite'
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.path.chmod(0o600)
        for table, column in ScientistNoAdmissionCanonicalVerifier._surfaces:
            if table == 'gpu_runtime_bindings':
                self.connection.execute('CREATE TABLE gpu_runtime_bindings ('
                    'owner TEXT,request_id TEXT,fencing_token INTEGER,launch_state TEXT)')
            else:
                self.connection.execute(f'CREATE TABLE {table} ({column} TEXT)')
        self.connection.execute('CREATE TABLE aos_no_admission_observations ('
            'request_id TEXT PRIMARY KEY,request_sha256 TEXT,original_caller_generation_sha256 TEXT,'
            'admission_binding_sha256 TEXT,proof_sha256 TEXT,observation_sha256 TEXT,'
            'dispatch_provenance_json TEXT,observation_json TEXT)')
        self.value = deepcopy(self.fixture.value)
        self.identity = sqlite_store_identity(SimpleNamespace(connection=self.connection))
        self.value['canonical']['store'] = self.identity
        self.value['canonical']['tombstone']['store'] = self.identity
        self.value['canonical']['tombstone_sha256'] = digest(self.value['canonical']['tombstone'])
        self.value['observation_sha256'] = digest({key: value for key, value in self.value.items()
                                                  if key != 'observation_sha256'})
        dispatch = {'target':self.value['target'],
                    'source_fingerprints':self.value['original']['admission_record']['admission_binding']['source_fingerprints'],
                    'caller_generation_sha256':self.value['target']['original_caller_generation_sha256'],
                    'broker_generation_sha256':self.value['original']['broker_generation_sha256'],
                    'provenance_complete':True, 'deferred_execution':[], 'quarantined_allocations':[]}
        self.connection.execute('INSERT INTO aos_no_admission_observations VALUES(?,?,?,?,?,?,?,?)',
            (self.value['target']['request_id'],self.value['target']['request_sha256'],
             self.value['target']['original_caller_generation_sha256'],self.value['original']['admission_binding_sha256'],
             digest({'observer':self.value['observer'],'cleanup_scope':self.value['cleanup_scope'],
                     'original':self.value['original'],'physical':self.value['physical'],'dispatch_provenance':dispatch}),
             self.value['observation_sha256'],canonical(dispatch),canonical(self.value)))
        self.connection.commit()
        self.schema_checksum = digest([dict(row) for row in self.connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','trigger') AND name!='sqlite_sequence' ORDER BY name,type")])
        self.current = Mock(return_value=None)

    def provider(self, **changes):
        options = {'database_path':self.path,'store_identity':self.identity,
                   'database_schema_sha256':self.schema_checksum,'verify_current':self.current}
        return ScientistNoAdmissionCanonicalVerifier(**(options | changes))

    def test_unconfigured_authority_denies_even_when_canonical_row_matches(self):
        with self.assertRaises(ScientistAdmissionError):
            self.provider(verify_current=None)
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionCanonicalVerifier(database_path=self.path, store_identity=self.identity,
                database_schema_sha256=self.schema_checksum)(self.value)

    def test_exact_canonical_readback_composes_with_journal_without_external_writes(self):
        before = [dict(row) for row in self.connection.execute('SELECT * FROM aos_no_admission_observations')]
        verifier = self.fixture.verifier(verify_canonical=self.provider())
        self.assertIsNotNone(ScientistNoAdmissionJournal(verifier).append(canonical(self.value).encode()))
        self.assertEqual(self.current.call_count, 4)
        self.assertEqual(before,[dict(row) for row in self.connection.execute('SELECT * FROM aos_no_admission_observations')])

    def test_all_ten_request_surfaces_reject_original_target(self):
        for table,column in ScientistNoAdmissionCanonicalVerifier._surfaces:
            with self.subTest(table=table):
                self.connection.execute(f'INSERT INTO {table} ({column}) VALUES(?)',(self.value['target']['request_id'],))
                self.connection.commit()
                with self.assertRaises(ScientistAdmissionError):
                    self.provider()(self.value)
                self.connection.execute(f'DELETE FROM {table}')
                self.connection.commit()

    def test_runtime_binding_is_not_ignored_for_other_owner_stale_token_or_terminal_state(self):
        for owner in ('aos','lab'):
            for token in (0,17,999):
                for state in ('prepared','created','uncertain','drained'):
                    with self.subTest(owner=owner,token=token,state=state):
                        self.connection.execute('INSERT INTO gpu_runtime_bindings VALUES(?,?,?,?)',
                            (owner,self.value['target']['request_id'],token,state))
                        self.connection.commit()
                        with self.assertRaises(ScientistAdmissionError):
                            self.provider()(self.value)
                        self.connection.execute('DELETE FROM gpu_runtime_bindings')
                        self.connection.commit()

    def test_independent_schema_and_identity_pins_reject_proof_assertions(self):
        for changes in ({'database_schema_sha256':'9'*64}, {'store_identity':{**self.identity,'inode':self.identity['inode']+1}}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.provider(**changes)(self.value)
        self.connection.execute('CREATE TABLE synthetic_unknown_deferred (request_id TEXT)')
        self.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_missing_or_changed_observation_and_uncertain_dispatch_deny(self):
        self.connection.execute("UPDATE aos_no_admission_observations SET observation_sha256=?",('9'*64,))
        self.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

        self.connection.execute('UPDATE aos_no_admission_observations SET observation_sha256=?,dispatch_provenance_json=?',
            (self.value['observation_sha256'],'{}'))
        self.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)
        self.connection.execute('DELETE FROM aos_no_admission_observations')
        self.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_physical_hash_cannot_replace_aggregate_producer_evidence_hash(self):
        self.connection.execute('UPDATE aos_no_admission_observations SET proof_sha256=?',
                                (self.value['physical']['proof_sha256'],))
        self.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_fresh_second_snapshot_detects_late_contradiction(self):
        def late_admission(proof):
            if self.current.call_count == 2:
                self.connection.execute('INSERT INTO gpu_turn_requests VALUES(?)',(self.value['target']['request_id'],))
                self.connection.commit()
        self.current.side_effect = late_admission
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_fresh_second_snapshot_detects_late_runtime_binding(self):
        def late_binding(proof):
            if self.current.call_count == 2:
                self.connection.execute('INSERT INTO gpu_runtime_bindings VALUES(?,?,?,?)',
                    ('lab',self.value['target']['request_id'],999,'drained'))
                self.connection.commit()
        self.current.side_effect = late_binding
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_unrelated_runtime_binding_is_preserved(self):
        self.connection.execute('INSERT INTO gpu_runtime_bindings VALUES(?,?,?,?)',
            ('lab','synthetic-unrelated-request',999,'uncertain'))
        self.connection.commit()
        before = [dict(row) for row in self.connection.execute('SELECT * FROM gpu_runtime_bindings')]
        self.provider()(self.value)
        self.assertEqual(before,[dict(row) for row in self.connection.execute('SELECT * FROM gpu_runtime_bindings')])

    def test_post_read_revoke_and_timeout_deny(self):
        self.current.side_effect = [None,ScientistAdmissionError('synthetic source revocation')]
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)
        self.current.side_effect = None
        with patch('aos.scientist_no_admission.time.monotonic',side_effect=[100.0,106.0]):
            with self.assertRaises(ScientistAdmissionError):
                self.provider()(self.value)


class ScientistNoAdmissionObserverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScientistNoAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'synthetic-observer-config.json'
        self.source = self.root / 'synthetic-observer-source.txt'
        self.source.write_bytes(b'explicit synthetic source; never executed')
        self.value = deepcopy(self.fixture.value)
        self.value['observer']['generation']['unit'] = 'swapp-scientist-no-admission-observer.service'
        self.value['observer']['generation']['control_group'] = '/synthetic/' + self.value['observer']['generation']['unit']
        sources = [{'path':str(self.source),'sha256':hashlib.sha256(self.source.read_bytes()).hexdigest()}]
        self.config = {'schema':'aos-scientist-no-admission-provider.v1','version':1,
            'target':self.value['target'],'cleanup_scope':self.value['cleanup_scope'],
            'observation_schema_sha256':SCHEMA_SHA256,'sources':sources,
            'observer':{'unit':self.value['observer']['generation']['unit'],'source_sha256':digest(sources)}}
        self.path.write_bytes(canonical(self.config).encode())
        self.path.chmod(0o600)
        self.checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        observer = self.value['observer']
        observer['source_sha256'] = digest(sources)
        observer['config_sha256'] = self.checksum
        observer['generation_sha256'] = digest(observer['generation'])
        observer['capability'].update({key:observer[key] for key in ('source_sha256','config_sha256','generation_sha256')})
        observer['capability_sha256'] = digest(observer['capability'])
        self.value['canonical']['tombstone']['observer_generation_sha256'] = observer['generation_sha256']
        self.value['canonical']['tombstone_sha256'] = digest(self.value['canonical']['tombstone'])
        self.value['observation_sha256'] = digest({key:value for key,value in self.value.items() if key!='observation_sha256'})
        self.authority = Mock(return_value=None)
        self.real_generation = ScientistNoAdmissionObserverVerifier._generation
        self.generation = self.enterContext(patch.object(ScientistNoAdmissionObserverVerifier,'_generation',return_value=None))

    def provider(self, **changes):
        return ScientistNoAdmissionObserverVerifier(**({'config_path':self.path,'config_raw_sha256':self.checksum,
                                                       'verify_authority':self.authority} | changes))

    def test_private_file_and_correct_source_do_not_grant_authority(self):
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionObserverVerifier(config_path=self.path,config_raw_sha256=self.checksum)(self.value)
        self.generation.assert_not_called()

    def test_exact_sources_and_live_generation_composes_with_journal(self):
        verifier = self.fixture.verifier(verify_observer=self.provider())
        self.assertIsNotNone(ScientistNoAdmissionJournal(verifier).append(canonical(self.value).encode()))
        self.assertEqual(self.authority.call_count,4)
        self.assertEqual(self.generation.call_count,4)

    def test_changed_source_or_config_pin_denies(self):
        with self.assertRaises(ScientistAdmissionError):
            self.provider(config_raw_sha256='9'*64)(self.value)
        self.source.write_bytes(b'synthetic changed source')
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_post_authority_source_change_or_unit_death_denies(self):
        def change_source(proof):
            if self.authority.call_count==2:
                self.source.write_bytes(b'synthetic late source change')
        self.authority.side_effect = change_source
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)
        self.source.write_bytes(b'explicit synthetic source; never executed')
        self.authority.side_effect = None
        self.generation.side_effect = ScientistAdmissionError('synthetic observer crashed')
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(self.value)

    def test_wrong_generation_or_target_cannot_substitute_for_review(self):
        value = deepcopy(self.value)
        value['target']['request_id'] = '9'*32
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(value)

    def test_actual_generation_checker_rejects_reused_pid_wrong_uid_and_restarted_unit(self):
        from aos.scientist_admission_history import ScientistServerGeneration
        generation = ScientistServerGeneration.model_validate(self.value['observer']['generation'],strict=True)
        identity = (generation.start_ticks,generation.boot_id,generation.control_group)
        output = '\n'.join(key+'='+value for key,value in {'Id':generation.unit,'LoadState':'loaded',
            'ActiveState':'active','MainPID':str(generation.pid),'InvocationID':generation.invocation_id,
            'ControlGroup':generation.control_group}.items())
        with patch('aos.scientist_no_admission._caller_process_identity',return_value=identity) as process, \
             patch.object(Path,'stat',return_value=SimpleNamespace(st_uid=generation.uid)) as info, \
             patch.object(ScientistPhysicalReleaseVerifier,'_environment',return_value={}), \
             patch.object(ScientistPhysicalReleaseVerifier,'_command',return_value=output) as command:
            self.assertIsNone(self.real_generation(generation,1000.0))
            process.side_effect = [identity,(generation.start_ticks+1,generation.boot_id,generation.control_group)]
            with self.assertRaises(ScientistAdmissionError):
                self.real_generation(generation,1000.0)
            process.side_effect = None
            info.return_value = SimpleNamespace(st_uid=generation.uid+1)
            with self.assertRaises(ScientistAdmissionError):
                self.real_generation(generation,1000.0)
            info.return_value = SimpleNamespace(st_uid=generation.uid)
            command.return_value = output.replace('InvocationID='+generation.invocation_id,'InvocationID='+'9'*32)
            with self.assertRaises(ScientistAdmissionError):
                self.real_generation(generation,1000.0)
        value = deepcopy(self.value)
        value['observer']['generation']['start_ticks'] += 1
        with self.assertRaises(ScientistAdmissionError):
            self.provider()(value)
