import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.desktop_control import DesktopController
from aos.scientist_cpu_capability import (
    CPU_SCHEMA_SHA256, ScientistCpuCapability, ScientistCpuCapabilityVerifier,
    ScientistCpuReviewedGrant, cpu_capability_sha256,
)
from aos.scientist_intents import ScientistIntentBinding
from aos.scientist_lab import ScientistLabAction, ScientistLabBudget, ScientistLabClient, ScientistLabStart, ScientistLabTask
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore


class ScientistCpuCapabilityTests(unittest.TestCase):
    def test_canonical_schemas_and_explicitly_synthetic_fixture(self):
        for model, filename in ((ScientistCpuCapability, 'scientist_cpu_capability'),
                                (ScientistCpuReviewedGrant, 'scientist_cpu_reviewed_grant')):
            schema = json.loads((REPO_ROOT / 'schemas' / (filename + '.schema.json')).read_text())
            self.assertEqual(schema, model.model_json_schema())
            jsonschema.Draft202012Validator.check_schema(schema)
        fixture = json.loads((REPO_ROOT / 'examples/scientist_cpu_capability.json').read_text())
        self.assertTrue(fixture['owner_id'].startswith('synthetic-'))
        self.assertTrue(fixture['suite_id'].startswith('synthetic.'))
        schema = json.loads((REPO_ROOT / 'schemas/scientist_cpu_capability.schema.json').read_text())
        jsonschema.validate(fixture, schema)
        self.assertEqual(ScientistCpuCapability.model_validate(fixture, strict=True).model_dump(by_alias=True), fixture)
        grant_schema = json.loads((REPO_ROOT / 'schemas/scientist_cpu_reviewed_grant.schema.json').read_text())
        jsonschema.validate(self.grant.model_dump(by_alias=True), grant_schema)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wire = dict(schema='scientist.lab-cpu-capability.v1', owner_id='synthetic-owner', origin='aos',
            suite_id='synthetic.cpu.v1', track='mode', program_version='mode-grid.v1', provider='mode-grid',
            source_kind='synthetic', suite_manifest_sha256='a' * 64, suite_entry_sha256='b' * 64,
            provider_config_sha256='c' * 64, snapshot_sha256='d' * 64, aos_cpu_study_sha256='e' * 64,
            max_experiments=2, max_wall_seconds=60, model_tokens=0, allowed_purpose='research',
            allocation_authority=False, gpu_release_verified=False, native_inference_authorized=False,
            launch_authorized=False)
        self.capability = ScientistCpuCapability.model_validate(self.wire, strict=True)
        self.client = ScientistLabClient('http://127.0.0.1:19999', self.root / 'synthetic.token',
            principal_id='synthetic-owner', allowed_suites=frozenset({'synthetic.cpu.v1'}))
        self.client.token_file.write_text('synthetic-test-token\n')
        self.client.token_file.chmod(0o600)
        self.grant = ScientistCpuReviewedGrant(profile='scientist-cpu-mode-grid.v1',
            authority_url=self.client.authority_url, principal_id=self.client.principal_id,
            authorization_context_sha256='f' * 64, scientist_source_manifest_sha256='1' * 64,
            aos_source_manifest_sha256='2' * 64, capability_schema_sha256=CPU_SCHEMA_SHA256,
            capability=self.capability, capability_sha256=cpu_capability_sha256(self.wire))
        self.verifier = ScientistCpuCapabilityVerifier(self.client, self.grant)
        self.client._request = Mock(return_value=json.dumps(self.wire).encode())
        self.task = ScientistLabTask(authority_url=self.client.authority_url,
            principal_id=self.client.principal_id, state_version=0,
            binding=ScientistIntentBinding(session_id='synthetic-session', runtime_id='synthetic-runtime',
                owner='AGENT', lease_id='synthetic-lease', generation=0, authorization_context_sha256='f' * 64),
            request=ScientistLabStart(idempotency_key='synthetic-cpu-request-key', track='mode',
                suite='synthetic.cpu.v1', budget=ScientistLabBudget(experiments=1, wall_seconds=30, model_tokens=0),
                program_version='mode-grid.v1', external_task_id='task-' + 'a' * 32,
                external_run_id='run-' + 'b' * 32, external_action_id='action-' + 'c' * 32))

    def action(self, task=None):
        task = task or self.task
        return ScientistLabAction(task_id=task.request.external_task_id, run_id=task.request.external_run_id,
            step_id='synthetic-step', action_id=task.request.external_action_id,
            runtime_id=task.binding.runtime_id, state_version=task.state_version,
            owner_lease_id=task.binding.lease_id, tool='lab.start', selected_option='lab.start',
            arguments={'request_sha256': digest(task.request.model_dump(mode='json'))},
            expected_effect='Synthetic CPU study', deadline=time.time() + 30,
            idempotency_key=task.request.idempotency_key)

    def test_exact_fresh_capability_each_call_without_changing_effect_callbacks(self):
        authority, effect = self.client.verify_authority, self.client.authorize_and_persist
        self.assertIsNone(self.verifier(self.task, self.action()))
        self.verifier(self.task, self.action())
        self.assertEqual(self.client._request.call_count, 2)
        arguments = self.client._request.call_args
        self.assertEqual(arguments.args[:3], ('GET', '/v1/aos-cpu-capability/synthetic.cpu.v1', b''))
        self.assertEqual(arguments.kwargs, {'bound': 16384})
        self.assertIs(self.client.verify_authority, authority)
        self.assertIs(self.client.authorize_and_persist, effect)

    def test_owner_all_pins_version_and_authority_drift_are_rejected(self):
        for field in ('owner_id', 'suite_id', 'program_version', 'suite_manifest_sha256', 'suite_entry_sha256',
                      'provider_config_sha256', 'snapshot_sha256', 'aos_cpu_study_sha256', 'max_experiments'):
            with self.subTest(field=field):
                changed = dict(self.wire)
                changed[field] = 1 if field == 'max_experiments' else ('9' * 64 if field.endswith('sha256') else 'different')
                self.client._request.return_value = json.dumps(changed).encode()
                with self.assertRaises(ScientistAdmissionError):
                    self.verifier(self.task, self.action())

    def test_strict_flags_and_integer_zero_no_extra_or_duplicate_fields(self):
        for field, value in (('launch_authorized', True), ('allocation_authority', 0),
                             ('gpu_release_verified', True), ('native_inference_authorized', True),
                             ('model_tokens', False), ('model_tokens', 1), ('extra', 'synthetic')):
            with self.subTest(field=field, value=value):
                self.client._request.return_value = json.dumps(self.wire | {field: value}).encode()
                with self.assertRaises(ScientistAdmissionError):
                    self.verifier(self.task, self.action())
        self.client._request.return_value = json.dumps(self.wire).replace('{', '{"owner_id":"duplicate",', 1).encode()
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())
        for field in ('allocation_authority', 'gpu_release_verified', 'native_inference_authorized', 'launch_authorized'):
            for value in (0, 1, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        ScientistCpuCapability.model_validate(self.wire | {field: value}, strict=True)

    def test_wrong_budget_program_context_denied_before_get(self):
        for changes in ({'experiments': 3}, {'wall_seconds': 61}, {'model_tokens': 1}):
            task = self.task.model_copy(update={'request': self.task.request.model_copy(update={
                'budget': ScientistLabBudget.model_validate(self.task.request.budget.model_dump() | changes)})})
            with self.assertRaises(ScientistAdmissionError):
                self.verifier(task, self.action(task))
        task = self.task.model_copy(update={'request': self.task.request.model_copy(update={'program_version': 'wrong.v1'})})
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(task, self.action(task))
        task = self.task.model_copy(update={'binding': self.task.binding.model_copy(update={'authorization_context_sha256': '0' * 64})})
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(task, self.action(task))
        self.client._request.assert_not_called()

    def test_stale_action_timeout_and_response_bound_fail_closed(self):
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action().model_copy(update={'deadline': time.time() - 1}))
        self.client._request.assert_not_called()
        self.client._request.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())
        self.client._request.side_effect = None
        self.client._request.return_value = b' ' * 16385
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())

    def test_parent_control_deadline_denies_before_dispatch(self):
        self.client._control_deadline = time.monotonic() - 1
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())
        self.client._request.assert_not_called()

    @patch('aos.scientist_lab.http.client.HTTPConnection')
    def test_existing_transport_uses_bearer_authentication_and_get_only(self, connection_type):
        response = Mock(status=200, fp=object())
        response.read1.side_effect = [json.dumps(self.wire).encode(), b'']
        connection = connection_type.return_value
        connection.getresponse.return_value = response
        self.client._request = ScientistLabClient._request.__get__(self.client)
        self.verifier(self.task, self.action())
        request = connection.request.call_args
        self.assertEqual(request.args, ('GET', '/v1/aos-cpu-capability/synthetic.cpu.v1'))
        self.assertIsNone(request.kwargs['body'])
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer synthetic-test-token')
        connection.close.assert_called_once()

    def test_explicit_profile_hash_and_schema_pair_are_required(self):
        for field, value in (('profile', 'gpu'), ('capability_sha256', '0' * 64),
                             ('capability_schema_sha256', '0' * 64)):
            with self.assertRaises(ValueError):
                ScientistCpuReviewedGrant.model_validate(self.grant.model_dump(by_alias=True) | {field: value})
        with self.assertRaises(ScientistAdmissionError):
            ScientistCpuCapabilityVerifier(self.client, None)

    def test_reviewed_owner_must_equal_principal_before_any_get(self):
        with self.assertRaises(ValueError):
            ScientistCpuReviewedGrant.model_validate(self.grant.model_dump(by_alias=True) | {
                'principal_id': 'synthetic-other-owner'})
        self.client._request.assert_not_called()

    def test_source_pair_is_trusted_review_metadata_not_live_source_attestation(self):
        grant = ScientistCpuReviewedGrant.model_validate(self.grant.model_dump(by_alias=True) | {
            'scientist_source_manifest_sha256': '3' * 64, 'aos_source_manifest_sha256': '4' * 64})
        verifier = ScientistCpuCapabilityVerifier(self.client, grant)
        self.assertIsNone(verifier(self.task, self.action()))
        wire = verifier.grant.capability.model_dump(by_alias=True)
        self.assertNotIn('scientist_source_manifest_sha256', wire)
        self.assertNotIn('aos_source_manifest_sha256', wire)
        self.assertIs(wire['launch_authorized'], False)

    def test_transport_drift_and_closed_client_are_denied(self):
        self.client.port += 1
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())
        self.client.port -= 1
        self.client._closed = True
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(self.task, self.action())
        self.client._request.assert_not_called()

    def test_utf8_canonical_hash_preserves_unicode(self):
        value = {'owner': 'sentetik-ş', 'zero': 0}
        import hashlib
        expected = hashlib.sha256('{"owner":"sentetik-ş","zero":0}'.encode('utf-8')).hexdigest()
        self.assertEqual(cpu_capability_sha256(value), expected)

    def test_existing_service_hook_still_requires_human_approval_before_post(self):
        store = TrajectoryStore(self.root / 'synthetic.sqlite')
        self.addCleanup(store.close)
        controller = DesktopController(store, SimpleNamespace(runtime_id='synthetic-runtime',
                                                               pins={'image_id': 'synthetic-image'}))
        service = ScientistLabService(controller, self.client, authorization_context_sha256='f' * 64,
                                      program_version='mode-grid.v1', verify_capability=self.verifier)
        approval = service.propose(suite='synthetic.cpu.v1', track='mode',
            budget=self.task.request.budget, program_version='mode-grid.v1')
        with self.assertRaises(ScientistAdmissionError):
            service.execute(approval['action_id'])
        self.assertTrue(self.client._request.call_count)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.client._request.call_args_list))
        self.assertEqual(store.connection.execute('SELECT state FROM scientist_lab_actions').fetchone()[0], 'pending')


if __name__ == '__main__':
    unittest.main()
