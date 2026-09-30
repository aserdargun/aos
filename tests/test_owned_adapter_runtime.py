from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import digest
from aos.owned_adapter_admission import assert_runtime_execution_binding, validate_runtime_admission
from aos.owned_adapter_runtime import OwnedAdapterRuntime
import aos.owned_adapter_runtime as runtime_module


class OwnedAdapterRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.control = {'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease-synthetic', 'generation': 2}
        self.scheduler = SimpleNamespace(closed=False, restart_quiesced=False, paused=False,
            sequences=SimpleNamespace(reserved=False), planning_reserved=False, adaptation_reserved=False,
            reserved=False, busy=False, controller=SimpleNamespace(state=lambda: self.control),
            remote_form_owned_candidate_session=SimpleNamespace(directory=Path('/synthetic/owned')),
            engine=SimpleNamespace(identity={'deployment_id': 'decider-' + 'a' * 64}),
            job_id=None, _active_owned_candidate_execution=None)
        self.base_preview = {'schema_version': '1.3', 'synthetic_fixture': True}
        self.scheduler.preview_owned_form_candidate_execution = Mock(
            return_value=self.base_preview | {'preview_sha256': digest(self.base_preview)})
        self.report = {'authorization': {'candidate_execution_sha256': 'b' * 64,
            'input_sha256': 'c' * 64, 'deployment_manifest_sha256': 'd' * 64,
            'deployment_id': self.scheduler.engine.identity['deployment_id']}, 'artifact_sha256': 'e' * 64}
        self.learning = SimpleNamespace(scheduler=self.scheduler,
            preparation=SimpleNamespace(reserved=False),
            adaptation=SimpleNamespace(inspect=Mock(return_value=self.report)))
        self.runtime = OwnedAdapterRuntime(self.learning)
        self.selection = {'episode_id': 'episode-' + '1' * 32, 'authorization_sha256': 'f' * 64,
                          'case_key': 'synthetic-case', 'development_value': 'synthetic message',
                          'lease_id': self.control['lease_id'], 'generation': self.control['generation']}
        self.manifest = {key: '1' * 64 for key in ('candidate_sha256', 'source_run_ref', 'review_sha256',
            'release_sha256', 'selection_sha256', 'reuse_admission_sha256', 'source_invocation_sha256')}
        self.loader = patch.object(runtime_module, 'load_candidate_execution_bundle',
                                   return_value=({'manifest': self.manifest}, '2' * 64))
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def preview(self):
        return self.runtime.preview(**self.selection)

    def starting(self):
        preview = self.preview()
        self.runtime.active = {'admission': preview['admission'], 'job_id': None,
                               'source_execution': self.report['authorization']['candidate_execution_sha256']}
        self.runtime.starting = (preview['adapter_admission_sha256'], preview['preview']['preview_sha256'],
                                 self.control['lease_id'], self.control['generation'])
        return preview

    def test_preview_binds_base_preview_value_report_without_starting_engine(self):
        result = self.preview()
        admission = validate_runtime_admission(result['admission'])
        self.assertEqual(admission['base_preview_sha256'], digest(self.base_preview))
        self.assertEqual(admission['adaptation_report_sha256'], digest(self.report))
        self.assertEqual(admission['development_value_sha256'], digest({'value': self.selection['development_value']}))
        self.assertEqual(result['preview']['schema_version'], '1.5')
        self.assertNotEqual(result['preview']['preview_sha256'], digest(self.base_preview))
        self.assertIsNone(self.runtime.active)
        self.assertFalse(result['started'])

    def test_admission_exact_schema_rejects_extra_permissions_and_wrong_binding(self):
        admission = self.preview()['admission']
        for changed in (admission | {'promotion_authorized': True}, admission | {'automatic_fallback': True},
                        admission | {'unknown': False}, admission | {'adapter_sha256': '0' * 64}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_runtime_admission(changed)

    def test_execution_admission_does_not_allow_preview_value_or_lease_rebinding(self):
        preview = self.preview()
        arguments = {'manifest': {'schema_version': '1.5', 'adapter_admission_sha256': digest(preview['admission'])},
            'reuse_admission': self.control, 'base_preview': self.base_preview,
            'case_key': self.selection['case_key'], 'development_value': self.selection['development_value']}
        assert_runtime_execution_binding(preview['admission'], **arguments)
        for changed in ({'development_value': 'changed'}, {'case_key': 'other'},
                        {'base_preview': self.base_preview | {'schema_version': '1.5'}},
                        {'reuse_admission': self.control | {'generation': 3}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                assert_runtime_execution_binding(preview['admission'], **(arguments | changed))

    def test_start_requires_separate_consent_exact_preview_and_clears_start_capability(self):
        preview = self.preview()
        self.scheduler.start_owned_form_candidate_execution = Mock(return_value={'job_id': 'job-synthetic'})
        arguments = self.selection | {'confirm_sha256': preview['preview']['preview_sha256'],
                                      'experimental_runtime_authorized': False}
        with self.assertRaises(ValueError):
            self.runtime.start(**arguments)
        arguments['experimental_runtime_authorized'] = True
        with self.assertRaises(ValueError):
            self.runtime.start(**(arguments | {'confirm_sha256': '0' * 64}))
        self.scheduler.start_owned_form_candidate_execution.assert_not_called()
        self.assertEqual(self.runtime.start(**arguments)['job_id'], 'job-synthetic')
        self.assertIsNone(self.runtime.starting)
        self.assertEqual(self.runtime.active['job_id'], 'job-synthetic')
        self.scheduler.start_owned_form_candidate_execution.side_effect = ValueError('synthetic failure')
        with self.assertRaises(ValueError):
            self.runtime.start(**arguments)
        self.assertIsNone(self.runtime.starting)
        self.assertIsNone(self.runtime.active)

    def test_lexical_source_audit_only_exact_running_job_and_terminal_source(self):
        preview = self.starting()
        self.runtime.active['job_id'] = self.scheduler.job_id = 'job-synthetic'
        self.scheduler.busy = True
        self.scheduler.reserved = True
        self.scheduler._active_owned_candidate_execution = {'job_id': 'job-synthetic',
            'candidate_execution_sha256': '3' * 64, 'adapter_admission_sha256': digest(preview['admission'])}
        source = self.runtime.active['source_execution']
        self.assertFalse(self.runtime.permits_audit(source))
        def inspect(*arguments):
            self.assertTrue(self.runtime.permits_audit(source))
            self.assertFalse(self.runtime.permits_audit('3' * 64))
            return self.report
        self.learning.adaptation.inspect.side_effect = inspect
        self.runtime.validate(preview['admission'])
        self.assertFalse(self.runtime.permits_audit(source))
        self.scheduler.job_id = 'job-other'
        with self.assertRaises(ValueError):
            self.runtime.validate(preview['admission'])

    def test_source_revoke_failure_restores_lexical_scope_and_no_fallback(self):
        preview = self.starting()
        self.learning.adaptation.inspect.side_effect = ValueError('revoked')
        with self.assertRaises(ValueError):
            self.runtime.validate(preview['admission'])
        self.assertIsNone(self.runtime._audit_scope)
        self.learning.adaptation.inspect.side_effect = None
        changed = deepcopy(self.report)
        changed['artifact_sha256'] = '0' * 64
        self.learning.adaptation.inspect.return_value = changed
        with self.assertRaises(ValueError):
            self.runtime.validate(preview['admission'])

    def test_control_requires_idle_preview_and_current_agent_lease(self):
        for field, value in (('busy', True), ('reserved', True), ('closed', True), ('paused', True)):
            setattr(self.scheduler, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.preview()
            setattr(self.scheduler, field, False)
        self.control['generation'] += 1
        with self.assertRaises(ValueError):
            self.preview()

    def test_historical_audit_is_not_current_source_or_reuse_authorization(self):
        preview = self.preview()
        historical = {'schema_version': '1.5', 'available': True, 'adapter_admission_verified': True,
                      'verification_scope': 'historical_execution', 'current_source_status': 'unchecked',
                      'runtime_reuse_authorized': False}
        self.scheduler.audit_owned_form_candidate_execution = Mock(side_effect=lambda _checksum: historical.copy())
        arguments = {'episode_id': self.selection['episode_id'],
                     'authorization_sha256': self.selection['authorization_sha256'],
                     'candidate_execution_sha256': '4' * 64}
        with patch.object(runtime_module, 'load_candidate_execution_bundle',
                          return_value=({'adapter-admission': preview['admission']}, '5' * 64)):
            result = self.runtime.audit(**arguments)
            self.assertEqual(result['current_source_status'], 'available')
            self.assertFalse(result['runtime_reuse_authorized'])
            self.learning.adaptation.inspect.side_effect = ValueError('source_revoked')
            result = self.runtime.audit(**arguments)
            self.assertTrue(result['available'])
            self.assertEqual(result['current_source_status'], 'unavailable')
            self.assertFalse(result['runtime_reuse_authorized'])
            with self.assertRaises(ValueError):
                self.runtime.audit(**(arguments | {'authorization_sha256': '0' * 64}))
