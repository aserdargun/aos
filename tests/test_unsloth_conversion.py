from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.unsloth_conversion import MAX_INPUT_BYTES, MAX_RECORD_BYTES, candidate_profile, prepare_jsonl, prepare_record, verify_prepared_record
from scripts.inspect_unsloth_candidate import inspect_candidate


class UnslothMessagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.choice = json.loads((REPO_ROOT / 'examples/system1_choice.jsonl').read_text().splitlines()[0])
        self.supervisor = json.loads((REPO_ROOT / 'examples/system2_supervisor.jsonl').read_text().splitlines()[0])

    def test_choice_uses_corrected_gold_not_prediction_or_rationale(self):
        source = deepcopy(self.choice)
        source['prediction']['selected_option'] = source['options'][1]['id']
        source['target']['rationale'] = 'TARGET_RATIONALE_SENTINEL'
        source['provenance']['usage_rights'] = 'PROVENANCE_SENTINEL'
        result = prepare_record('qwen35_4b_s1', source)
        prompt, answer = result['messages']
        self.assertEqual(json.loads(answer['content']), {'selected_option': source['target']['correct_option']})
        self.assertEqual(set(json.loads(prompt['content'])['input']), {'state', 'question', 'options'})
        self.assertNotIn('TARGET_RATIONALE_SENTINEL', canonical(result))
        self.assertNotIn('PROVENANCE_SENTINEL', canonical(result))
        self.assertEqual(result['source_record_sha256'], digest(source))
        source['options'].reverse()
        permuted = prepare_record('qwen35_4b_s1', source)
        self.assertEqual(permuted['messages'][1], answer)
        self.assertEqual(json.loads(permuted['messages'][0]['content'])['input']['options'], source['options'])

    def test_supervisor_target_stays_out_of_prompt_and_bases_are_separate(self):
        source = deepcopy(self.supervisor)
        source['target']['diagnosis'] = 'SUPERVISOR_TARGET_SENTINEL'
        results = [prepare_record(candidate, source) for candidate in ('gemma4_12b_s2', 'qwen38_27b_s2')]
        for result in results:
            self.assertNotIn('SUPERVISOR_TARGET_SENTINEL', result['messages'][0]['content'])
            self.assertEqual(json.loads(result['messages'][1]['content']), source['target'])
            self.assertEqual(result['split_group'], source['split_group'])
            self.assertFalse(result['training_ready'])
            self.assertFalse(result['training_authorized'])
            self.assertFalse(result['runtime_authority'])
            self.assertFalse(result['tokenizer_applied'])
            self.assertFalse(result['assistant_loss_mask_verified'])
        self.assertNotEqual(results[0]['base'], results[1]['base'])
        self.assertNotEqual(results[0]['converter_id'], results[1]['converter_id'])

    def test_wrong_role_clef_and_invalid_labels_fail_closed(self):
        for candidate, source in [('cloudflare_clef_s1', self.choice), ('unknown', self.choice),
                                  ('gemma4_12b_s2', self.choice), ('qwen35_4b_s1', self.supervisor)]:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                prepare_record(candidate, source)
        for change in ('missing_gold', 'duplicate_options', 'failed', 'unreviewed_policy', 'reasoning'):
            source = deepcopy(self.choice)
            if change == 'missing_gold':
                source['target']['correct_option'] = 'unknown'
            elif change == 'duplicate_options':
                source['options'][1] = source['options'][0]
            elif change == 'failed':
                source['target']['outcome'] = 'failed'
            elif change == 'unreviewed_policy':
                source['target']['outcome'] = 'policy_reviewed'
            else:
                source['target']['hidden_reasoning'] = 'not permitted'
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare_record('qwen35_4b_s1', source)

    def test_real_records_are_not_relabelled_or_authorized_by_conversion(self):
        source = deepcopy(self.choice)
        source['provenance']['synthetic'] = False
        result = prepare_record('qwen35_4b_s1', source)
        self.assertFalse(result['synthetic'])
        self.assertFalse(result['provenance_claims_verified'])
        self.assertFalse(result['training_authorized'])
        self.assertFalse(result['training_ready'])

    def test_source_binding_detects_tampered_model_group_or_messages(self):
        prepared = prepare_record('qwen35_4b_s1', self.choice)
        verify_prepared_record('qwen35_4b_s1', self.choice, prepared)
        for field, replacement in [('candidate_id', 'qwen38_27b_s2'), ('split_group', 'changed'),
                                   ('messages_sha256', '0' * 64), ('training_authorized', True)]:
            wrong = dict(prepared, **{field: replacement})
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_prepared_record('qwen35_4b_s1', self.choice, wrong)
        changed = deepcopy(self.choice)
        changed['state'] += ' A later observation.'
        with self.assertRaises(ValueError):
            verify_prepared_record('qwen35_4b_s1', changed, prepared)

    def test_batch_bounds_duplicates_and_inspector_agreement(self):
        payload = (canonical(self.choice) + '\n').encode()
        self.assertEqual(prepare_jsonl('qwen35_4b_s1', payload), [prepare_record('qwen35_4b_s1', self.choice)])
        for invalid in (b'', payload * 2, b' ' * (MAX_INPUT_BYTES + 1), b'{' * (MAX_RECORD_BYTES + 1), b'not json'):
            with self.assertRaises(ValueError):
                prepare_jsonl('qwen35_4b_s1', invalid)
        duplicate_key = payload.replace(b'"synthetic":true', b'"synthetic":false,"synthetic":true')
        with self.assertRaises(ValueError):
            prepare_jsonl('qwen35_4b_s1', duplicate_key)
        for candidate, source in [('qwen35_4b_s1', self.choice), ('gemma4_12b_s2', self.supervisor),
                                  ('qwen38_27b_s2', self.supervisor)]:
            plan = inspect_candidate(candidate, 'lora')
            self.assertTrue(plan['message_preparation']['available'])
            self.assertEqual(plan['message_preparation']['converter_id'], prepare_record(candidate, source)['converter_id'])
        self.assertFalse(inspect_candidate('cloudflare_clef_s1', 'qlora')['message_preparation']['available'])
        self.assertTrue(inspect_candidate('qwen35_4b_s1', 'qlora')['method_cautions'])

    def test_cli_defaults_to_hashes_and_never_partially_emits_invalid_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            source = deepcopy(self.choice)
            source['state'] = 'PRIVATE_INPUT_SENTINEL'
            path.write_text(canonical(source) + '\n')
            command = [sys.executable, '-m', 'aos.unsloth_conversion', '--model', 'qwen35_4b_s1', '--input', str(path)]
            environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT / 'src'))
            result = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('PRIVATE_INPUT_SENTINEL', result.stdout + result.stderr)
            result = subprocess.run([*command, '--emit-messages'], capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PRIVATE_INPUT_SENTINEL', result.stdout)
            path.write_text(canonical(source) + '\nINVALID_PRIVATE_SENTINEL\n')
            result = subprocess.run([*command, '--emit-messages'], capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('PRIVATE_SENTINEL', result.stderr)

    def test_batch_freezes_one_model_profile_before_conversion(self):
        content = (REPO_ROOT / 'examples/system1_choice.jsonl').read_bytes()
        with patch('aos.unsloth_conversion.candidate_profile', wraps=candidate_profile) as profile:
            records = prepare_jsonl('qwen35_4b_s1', content)
        profile.assert_called_once_with('qwen35_4b_s1')
        self.assertEqual(len(records), 4)
        self.assertEqual(len({digest(record['base']) for record in records}), 1)

    def test_canonical_synthetic_example_matches_converter(self):
        fixture = json.loads((REPO_ROOT / 'examples/unsloth_message_record.json').read_text())
        self.assertEqual(fixture, prepare_record('qwen35_4b_s1', self.choice))


if __name__ == '__main__':
    unittest.main()
