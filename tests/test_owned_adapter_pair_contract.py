from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.owned_adapter_pair_contract import pair_observation, pair_record, validate_pair
from aos.site_skill_form_recipe import SUPPORTED_RECIPE_ORDERS


def synthetic_runtime_preview():
    admission = json.loads((REPO_ROOT / 'examples/owned_adapter_runtime_admission.json').read_text())
    arguments = {key: digest({'synthetic': key}) for key in (
        'candidate_sha256', 'source_run_ref', 'review_sha256', 'release_sha256',
        'selection_sha256', 'reuse_admission_sha256', 'invocation_sha256')}
    arguments.update({'case_key': admission['case_key'], 'development_value': 'synthetic message'})
    base = {key: digest({'synthetic': key}) for key in (
        'source_group_sha256', 'profile_sha256', 'skill_sha256', 'parameter_variant_sha256',
        'invocation_sha256', 'recipe_sha256', 'form_plan_sha256', 'state_plan_sha256')}
    base.update({key: arguments[key] for key in (
        'candidate_sha256', 'source_run_ref', 'review_sha256', 'release_sha256',
        'selection_sha256', 'reuse_admission_sha256', 'case_key')})
    base.update({'source_invocation_sha256': arguments['invocation_sha256'],
                 'schema_version': '1.3', 'available': True, 'status': 'preview',
                 'purpose': 'development_variation', 'independent_held_out': False, 'report': None,
                 'steps': [{'step_key': operation, 'operation': operation}
                           for operation in SUPPORTED_RECIPE_ORDERS[0]]})
    admission['base_preview_sha256'] = digest(base)
    admission['development_value_sha256'] = digest({'value': arguments['development_value']})
    adapter = base | {'schema_version': '1.5', 'adapter_admission_sha256': digest(admission)}
    adapter['preview_sha256'] = digest(adapter)
    return {'admission': admission, 'adapter_admission_sha256': digest(admission),
            'preview': adapter, 'arguments': arguments, 'started': False}


def synthetic_pair():
    return pair_record(synthetic_runtime_preview(), session_id='session-synthetic',
                       runtime_id='runtime-synthetic', lease_id='lease-synthetic', generation=2)


class OwnedAdapterPairContractTests(unittest.TestCase):
    def test_precommit_reconstructs_exact_same_case_and_distinct_previews_without_io(self):
        preview = synthetic_runtime_preview()
        original = deepcopy(preview)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('worker forbidden')):
            record = pair_record(preview, session_id='session-synthetic',
                                 runtime_id='runtime-synthetic', lease_id='lease-synthetic', generation=2)
            self.assertIs(validate_pair(record), record)
        self.assertEqual(preview, original)
        self.assertEqual(record['base_preview']['preview_sha256'], preview['admission']['base_preview_sha256'])
        self.assertNotEqual(record['base_preview']['preview_sha256'], record['adapter_preview']['preview_sha256'])
        for key in ('case_key', 'invocation_sha256', 'parameter_variant_sha256', 'form_plan_sha256',
                    'state_plan_sha256', 'recipe_sha256', 'steps'):
            self.assertEqual(record['base_preview'][key], record['adapter_preview'][key])
        self.assertFalse(record['automatic_second_arm'])
        self.assertFalse(record['promotion_authorized'])
        preview['arguments']['development_value'] = 'changed'
        self.assertEqual(record['arguments']['development_value'], 'synthetic message')

    def test_schema_is_closed_at_every_authority_and_preview_boundary(self):
        record = synthetic_pair()
        for path in ((), ('authority',), ('arguments',), ('base_preview',), ('adapter_preview',),
                     ('adapter_admission',), ('adapter_admission', 'runtime_binding')):
            changed = deepcopy(record)
            target = changed
            for key in path:
                target = target[key]
            target['execution_authorized'] = True
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, '^owned_adapter_pair_invalid$'):
                validate_pair(changed)

    def test_every_required_field_and_claim_is_validated(self):
        record = synthetic_pair()
        for key in record:
            changed = deepcopy(record)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_pair(changed)
        for key, value in (('synthetic', False), ('scope', 'held_out'), ('independent_held_out', False),
                           ('independent_held_out', 0.0), ('promotion_authorized', True),
                           ('automatic_second_arm', True), ('cold_worker_each_arm', False)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_pair(record | {key: value})

    def test_all_source_arguments_and_private_value_are_bound(self):
        record = synthetic_pair()
        for key in record['arguments']:
            changed = deepcopy(record)
            changed['arguments'][key] = ('changed' if key in {'case_key', 'development_value'} else '0' * 64)
            with self.subTest(argument=key), self.assertRaises(ValueError):
                validate_pair(changed)
        for value in (' secret ', 'secret\n', 'secret/command', ''):
            changed = deepcopy(record)
            changed['arguments']['development_value'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_pair(changed)

    def test_preview_hashes_authority_deployments_and_admission_cannot_drift(self):
        record = synthetic_pair()
        mutations = [
            ('base_preview', 'preview_sha256', '0' * 64),
            ('adapter_preview', 'preview_sha256', '0' * 64),
            ('adapter_preview', 'adapter_admission_sha256', '0' * 64),
            ('adapter_preview', 'schema_version', '1.3'),
            ('base_preview', 'schema_version', '1.5'),
            ('authority', 'lease_id', 'lease-other'), ('authority', 'generation', 3),
            ('authority', 'generation', 2.0), ('authority', 'generation', True),
            ('adapter_admission', 'generation', 2.0),
            ('adapter_admission', 'base_preview_sha256', '0' * 64),
            ('adapter_admission', 'adapter_sha256', '0' * 64),
        ]
        for section, key, value in mutations:
            changed = deepcopy(record)
            changed[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                validate_pair(changed)
        for key in ('episode_id', 'authorization_sha256', 'base_deployment_id', 'adapter_deployment_id'):
            changed = deepcopy(record)
            changed[key] = changed[key][:-1] + ('0' if changed[key][-1] != '0' else '1')
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_pair(changed)

    def test_invalid_recipe_order_rejected_even_after_rehashing_all_envelopes(self):
        preview = synthetic_runtime_preview()
        steps = preview['preview']['steps']
        steps[0], steps[1] = steps[1], steps[0]
        base = {key: value for key, value in preview['preview'].items()
                if key not in {'adapter_admission_sha256', 'preview_sha256'}}
        base['schema_version'] = '1.3'
        preview['admission']['base_preview_sha256'] = digest(base)
        preview['adapter_admission_sha256'] = digest(preview['admission'])
        preview['preview']['adapter_admission_sha256'] = preview['adapter_admission_sha256']
        preview['preview']['preview_sha256'] = digest({key: value for key, value in preview['preview'].items()
                                                       if key != 'preview_sha256'})
        with self.assertRaises(ValueError):
            pair_record(preview, session_id='session-synthetic', runtime_id='runtime-synthetic',
                        lease_id='lease-synthetic', generation=2)

    def test_runtime_preview_must_be_complete_unstarted_and_exactly_pinned(self):
        preview = synthetic_runtime_preview()
        for changes in ({'started': True}, {'started': 0}, {'extra': False},
                        {'adapter_admission_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pair_record(preview | changes, session_id='session-synthetic', runtime_id='runtime-synthetic',
                            lease_id='lease-synthetic', generation=2)

    def test_content_free_observations_bind_each_arm_and_exact_pair(self):
        record = synthetic_pair()
        for arm in ('base', 'adapter'):
            observation = pair_observation(record, digest(record), arm, job_id='job-synthetic',
                run_id='run-synthetic', step_id='step-synthetic', candidate_execution_sha256='1' * 64)
            validator('owned_adapter_pair_observation').validate(observation)
            self.assertEqual(observation['deployment_id'], record[arm + '_deployment_id'])
            self.assertEqual(observation['preview_sha256'], record[arm + '_preview']['preview_sha256'])
            self.assertNotIn(record['arguments']['development_value'], canonical(observation))
            self.assertTrue(observation['cold_worker'])
        for checksum, arm in (('0' * 64, 'base'), (digest(record), 'other')):
            with self.assertRaises(ValueError):
                pair_observation(record, checksum, arm, job_id='job-synthetic', run_id='run-synthetic',
                                 step_id='step-synthetic', candidate_execution_sha256='1' * 64)
        with self.assertRaises(ValueError):
            pair_observation(record, digest(record), 'base', job_id='', run_id='run-synthetic',
                             step_id='step-synthetic', candidate_execution_sha256='invalid')

    def test_published_synthetic_examples_are_canonical_contracts(self):
        record = json.loads((REPO_ROOT / 'examples/owned_adapter_pair.json').read_text())
        self.assertEqual(validate_pair(record), synthetic_pair())
        observation = json.loads((REPO_ROOT / 'examples/owned_adapter_pair_observation.json').read_text())
        self.assertEqual(observation, pair_observation(record, digest(record), 'base',
            job_id='job-synthetic', run_id='run-synthetic', step_id='step-synthetic',
            candidate_execution_sha256='1' * 64))


if __name__ == '__main__':
    unittest.main()
