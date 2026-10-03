import copy
import json
import unittest
from pathlib import Path

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.native_exclusion import NativeExclusionEvidence


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


if __name__ == '__main__':
    unittest.main()
