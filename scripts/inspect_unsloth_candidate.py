"""Inspect a model-specific LoRA/QLoRA design; never install, train or acquire GPUs."""

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]


def validate_recipe(recipe, candidate):
    schema = json.loads((ROOT / 'schemas/unsloth_candidate_recipe.schema.json').read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(recipe)
    expected_schema = 'schemas/' + ('system1_choice' if candidate['role'] == 'system1' else 'system2_supervisor') + '.schema.json'
    if (recipe['candidate_id'] != candidate['id'] or recipe['role'] != candidate['role']
            or recipe['recipe_id'] != 'unsloth-' + candidate['id'] + '-v001'
            or recipe['base']['repository'] != candidate['repository']
            or recipe['base']['revision'] != candidate['revision']
            or recipe['dataset']['canonical_schema'] != expected_schema):
        raise ValueError('Recipe must match the exact candidate, role, base and dataset contract')
    custom_head = candidate['execution_kind'] == 'typed_choice_probabilities'
    if recipe['compatibility']['custom_joint_head_required'] != custom_head:
        raise ValueError('Typed decision head cannot use an unreviewed generative training path')
    required = {'base_artifact_review', 'unsloth_backend_probe', 'dataset_rights_and_split_review',
        'role_converter_probe', 'target_modules_probe', 'measured_training_memory',
        'scientist_gpu_reservation', 'explicit_training_authorization', 'adapter_reload_and_rollback'}
    if custom_head:
        required.add('joint_head_loss_and_serialization_probe')
    if not required.issubset(recipe['blocked_until']):
        raise ValueError('Training design is missing acceptance gates')


def inspect_candidate(candidate_id, method):
    catalog = json.loads((ROOT / 'config/model_candidates.json').read_text())
    schema = json.loads((ROOT / 'schemas/model_candidates.schema.json').read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(catalog)
    matches = [candidate for candidate in catalog['candidates'] if candidate['id'] == candidate_id]
    if len(matches) != 1 or method not in {'lora', 'qlora'}:
        raise ValueError('Select one known candidate and lora or qlora')
    candidate = matches[0]
    recipe = json.loads((ROOT / 'training/recipes' / ('unsloth-' + candidate_id + '-v001.json')).read_text())
    validate_recipe(recipe, candidate)
    return {'candidate_id': candidate_id, 'method': method, 'base': recipe['base'],
        'role': recipe['role'], 'dataset_schema': recipe['dataset']['canonical_schema'],
        'recipe_id': recipe['recipe_id'], 'training_ready': False, 'runtime_authority': False,
        'adapter_namespace_template': f'data/training-candidates/{candidate_id}/{method}/{{run_id}}',
        'quantization_plan': '4-bit training loader compatibility probe' if method == 'qlora' else 'unquantized training base compatibility probe',
        'blocked_until': recipe['blocked_until'], 'automatic_promotion': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--method', choices=['lora', 'qlora'], required=True)
    options = parser.parse_args()
    print(json.dumps(inspect_candidate(options.model, options.method), indent=2))


if __name__ == '__main__':
    main()
