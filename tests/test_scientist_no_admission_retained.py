"""Synthetic retained observations and explicit mock authority; no GPU or recovery execution."""

from copy import deepcopy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import Mock

from aos.contracts import REPO_ROOT
from aos.scientist_no_admission import (
    RECOVERY_SCHEMA_SHA256, SCHEMA_SHA256, ScientistNoAdmissionJournal,
    ScientistNoAdmissionRetainedJournal, ScientistNoAdmissionRetainedVerifier,
)
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_no_admission as observation_cases


class RetainedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = observation_cases.ScientistNoAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.value = deepcopy(self.fixture.value)
        self.raw = canonical(self.value).encode()
        self.clock = 500000000
        self.schema = (REPO_ROOT / 'schemas/scientist_no_admission_recovery.schema.json').read_bytes()
        observer = self.value['observer']
        self.retained = {
            'observation_raw_sha256':hashlib.sha256(self.raw).hexdigest(),
            'observation_sha256':self.value['observation_sha256'], 'observation_schema_sha256':SCHEMA_SHA256,
            'canonical_store':self.value['canonical']['store'], 'canonical_schema_sha256':'f'*64,
            'cleanup_scope_sha256':digest(self.value['cleanup_scope']),
            'observer_generation_sha256':observer['generation_sha256'],
            'producer_source_sha256':observer['source_sha256'], 'producer_config_sha256':observer['config_sha256'],
        }
        generation = {**observer['generation'], 'pid':observer['generation']['pid']+200,
                      'start_ticks':observer['generation']['start_ticks']+200, 'invocation_id':'f'*32,
                      'unit':'synthetic-retained-recovery.service'}
        self.context = {'schema':'aos-scientist-no-admission-observation-recovery.v1','version':1,
            'purpose':'close_retained_no_admission','recovery_request_id':'f'*32,
            'target':self.value['target'], 'retained':self.retained,
            'principal':{'generation':generation,'generation_sha256':digest(generation),
                         'source_sha256':'b'*64,'config_sha256':'c'*64},
            'cleanup_scope':self.value['cleanup_scope'],
            'freshness':{'clock':'CLOCK_BOOTTIME','unit':'microseconds','boot_id':generation['boot_id'],
                         'issued_boottime_us':self.clock,'expires_boottime_us':self.clock+60000000,
                         'max_age_us':60000000},
            'inference_allowed':False,'gpu_release_allowed':False,'observation_reissue_allowed':False}
        self.callbacks = [Mock(return_value=None) for name in ('current','canonical','physical','source')]

    def verifier(self, **changes):
        options = {'reviewed_schema_bytes':self.fixture.schema,'schema_sha256':SCHEMA_SHA256,
            'reviewed_recovery_schema_bytes':self.schema,'recovery_schema_sha256':RECOVERY_SCHEMA_SHA256,
            'expected_retained':self.retained,'verify_current':self.callbacks[0],
            'verify_canonical':self.callbacks[1],'verify_physical':self.callbacks[2],
            'verify_retained_source':self.callbacks[3],'clock':lambda:self.clock,
            'boot_id':lambda:self.value['freshness']['boot_id']}
        return ScientistNoAdmissionRetainedVerifier(self.fixture.fixture.history, **(options | changes))

    def append(self, context=None):
        return ScientistNoAdmissionRetainedJournal(self.verifier()).append(
            self.raw,canonical(context if context is not None else self.context).encode())

    def test_expired_old_proof_closes_only_with_distinct_fresh_authority(self):
        self.fixture.clock = self.clock
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionJournal(self.fixture.verifier()).append(self.raw)
        result = self.append()
        self.assertEqual(result['observation_json'].encode(),self.raw)
        self.assertEqual(json.loads(result['recovery_scope_json']),
                         self.value['cleanup_scope'] | {'retained_recovery':self.context})
        self.assertEqual(self.fixture.original_rows(),self.fixture.before)
        self.assertEqual(self.append(),result)
        for callback in self.callbacks:
            self.assertEqual(callback.call_count,4)

    def test_default_authority_and_unreviewed_schema_deny(self):
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(recovery_schema_sha256='0'*64)
        verifier = ScientistNoAdmissionRetainedVerifier(self.fixture.fixture.history,
            reviewed_schema_bytes=self.fixture.schema,schema_sha256=SCHEMA_SHA256,
            reviewed_recovery_schema_bytes=self.schema,recovery_schema_sha256=RECOVERY_SCHEMA_SHA256,
            expected_retained=self.retained,clock=lambda:self.clock)
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionRetainedJournal(verifier).append(self.raw,canonical(self.context).encode())
        self.assertEqual(self.fixture.count(),0)

    def test_forged_permissions_target_source_or_scope_deny(self):
        for section,key,value in [('', 'inference_allowed', True),('', 'observation_reissue_allowed', True),
                                  ('', 'gpu_release_allowed',True),('', 'version',True),
                                  ('target','request_id','e'*32),('retained','producer_source_sha256','0'*64),
                                  ('cleanup_scope','current_generation',2),('cleanup_scope','lease_id','wrong')]:
            with self.subTest(section=section,key=key):
                context = deepcopy(self.context)
                (context[section] if section else context)[key] = value
                with self.assertRaises(ScientistAdmissionError):
                    self.append(context)
                self.assertEqual(self.fixture.count(),0)

    def test_old_observer_as_recovery_principal_and_expired_context_deny(self):
        context = deepcopy(self.context)
        context['principal']['generation'] = self.value['observer']['generation']
        context['principal']['generation_sha256'] = self.value['observer']['generation_sha256']
        with self.assertRaises(ScientistAdmissionError):
            self.append(context)
        self.clock += 60000000
        with self.assertRaises(ScientistAdmissionError):
            self.append()

    def test_changed_unit_hash_cannot_disguise_original_process_identity(self):
        for previous in (self.value['observer']['generation'],self.value['original']['broker_generation'],
                         self.value['original']['admission_record']['admission_binding']['caller_generation']):
            with self.subTest(pid=previous['pid']):
                context = deepcopy(self.context)
                generation = {key:previous[key] for key in context['principal']['generation']}
                generation['unit'] = 'synthetic-different-unit.service'
                generation['invocation_id'] = 'a'*32
                context['principal'].update(generation=generation,generation_sha256=digest(generation))
                with self.assertRaises(ScientistAdmissionError):
                    self.append(context)
        self.assertEqual(self.fixture.count(),0)

    def test_recovery_context_cannot_predate_historical_observation(self):
        context = deepcopy(self.context)
        self.clock = 150000000
        context['freshness'].update(issued_boottime_us=100000000,expires_boottime_us=160000000)
        with self.assertRaises(ScientistAdmissionError):
            self.append(context)
        self.assertEqual(self.fixture.count(),0)

    def test_current_owner_generation_lease_and_configuration_change_deny(self):
        for column,value in [('owner','AGENT'),('generation',2),('lease_id','synthetic-new-lease')]:
            with self.subTest(column=column):
                self.store.connection.execute('UPDATE desktop_sessions SET '+column+'=?',(value,))
                self.store.connection.commit()
                with self.assertRaises(ScientistAdmissionError):
                    self.append()
                self.store.connection.execute('UPDATE desktop_sessions SET '+column+'=?',
                    (self.fixture.before['desktop_sessions'][0][column],))
                self.store.connection.commit()
        verifier = self.verifier()
        self.callbacks[0].side_effect = lambda proof,context: verifier.expected_retained.update(
            producer_source_sha256='0'*64)
        with self.assertRaises(ScientistAdmissionError):
            ScientistNoAdmissionRetainedJournal(verifier).append(self.raw,canonical(self.context).encode())
        self.assertEqual(self.fixture.count(),0)

    def test_revoke_or_expiry_during_postcheck_rolls_back(self):
        self.callbacks[2].side_effect = [None,ScientistAdmissionError('Synthetic cleanup revoked')]
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.fixture.count(),0)
        self.callbacks[2].side_effect = None
        self.callbacks[3].side_effect = lambda proof,context: setattr(self,'clock',self.clock+60000000)
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.fixture.count(),0)

    def test_callback_commit_and_crash_cannot_escape_original_transaction(self):
        self.callbacks[0].side_effect = lambda proof,context: self.store.connection.commit()
        with self.assertRaises(ScientistAdmissionError):
            self.append()
        self.assertEqual(self.fixture.count(),0)
        self.callbacks[0].side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.append()
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.fixture.original_rows(),self.fixture.before)

    def test_committed_context_is_immutable_not_replaced_by_new_attempt(self):
        result = self.append()
        context = deepcopy(self.context)
        context['recovery_request_id'] = 'd'*32
        with self.assertRaises(ScientistAdmissionError):
            self.append(context)
        self.assertEqual(self.fixture.count(),1)
        self.assertEqual(self.store.connection.execute(
            'SELECT recovery_scope_json FROM scientist_no_admission_closures').fetchone()[0],result['recovery_scope_json'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('UPDATE scientist_no_admission_closures SET recovery_scope_json=?',
                                          (canonical(context),))
        self.store.connection.rollback()
