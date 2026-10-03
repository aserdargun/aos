"""Prepare model-bound text messages without tokenizing, authorizing or training."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import REPO_ROOT, canonical, digest
from .dataset import model_input, validate_record, validator


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024
MAX_RECORDS = 1000
SUPPORTED = {'qwen35_4b_s1': 'system1_choice', 'gemma4_12b_s2': 'system2_supervisor',
             'qwen38_27b_s2': 'system2_supervisor'}


def candidate_profile(candidate_id):
    if candidate_id not in SUPPORTED:
        raise ValueError('Unsupported message converter; Clef requires a separately verified joint-head path')
    catalog = json.loads((REPO_ROOT / 'config/model_candidates.json').read_text())
    if not validator('model_candidates').is_valid(catalog):
        raise ValueError('Invalid candidate catalog')
    matches = [item for item in catalog['candidates'] if item['id'] == candidate_id]
    kind = SUPPORTED[candidate_id]
    role = 'system1' if kind == 'system1_choice' else 'system2'
    if len(matches) != 1 or matches[0]['role'] != role or matches[0]['execution_kind'] != 'bounded_generation':
        raise ValueError('Candidate role or execution contract changed')
    return matches[0], kind


def prepare_record(candidate_id, record):
    candidate, kind = candidate_profile(candidate_id)
    return _prepare_record(candidate, kind, record)


def _prepare_record(candidate, kind, record):
    candidate_id = candidate['id']
    if len(canonical(record).encode()) > MAX_RECORD_BYTES:
        raise ValueError('Record exceeds the preparation limit')
    validate_record(kind, record)
    if kind == 'system1_choice':
        if record['target']['outcome'] == 'failed':
            raise ValueError('An unsuccessful target cannot become a positive choice example')
        if record['target']['outcome'] == 'policy_reviewed' and record['provenance']['source'] != 'human_review':
            raise ValueError('Policy-reviewed choices require human-review provenance')
        instruction = 'Choose exactly one supplied option. Return only JSON with selected_option equal to its exact id. Do not provide reasoning or execute an action.'
        target = {'selected_option': record['target']['correct_option']}
    else:
        if record['target']['needs_human'] and record['provenance']['source'] != 'human_review':
            raise ValueError('Human-required supervision needs human-review provenance')
        instruction = 'Return a JSON supervision record with diagnosis, corrected_plan, verification_criteria, outcome and needs_human. Use concise conclusions, not hidden reasoning. A plan is not execution permission.'
        target = record['target']
    messages = [{'role': 'user', 'content': canonical({'instruction': instruction, 'input': model_input(kind, record)})},
                {'role': 'assistant', 'content': canonical(target)}]
    result = {'schema_version': '1.0', 'format': 'aos.unsloth-messages.v1',
        'candidate_id': candidate_id, 'role': candidate['role'],
        'base': {'repository': candidate['repository'], 'revision': candidate['revision']},
        'converter_id': 'aos-' + candidate_id + '-messages-v1',
        'source_kind': kind, 'source_record_sha256': digest(record),
        'source_schema_sha256': hashlib.sha256((REPO_ROOT / 'schemas' / (kind + '.schema.json')).read_bytes()).hexdigest(),
        'sample_id': record['sample_id'], 'split_group': record['split_group'],
        'synthetic': record['provenance']['synthetic'], 'messages': messages,
        'messages_sha256': digest(messages), 'training_ready': False, 'training_authorized': False,
        'runtime_authority': False, 'tokenizer_applied': False, 'assistant_loss_mask_verified': False,
        'provenance_claims_verified': False}
    if not validator('unsloth_message_record').is_valid(result):
        raise ValueError('Invalid prepared message record')
    return result


def verify_prepared_record(candidate_id, source, prepared):
    if not validator('unsloth_message_record').is_valid(prepared):
        raise ValueError('Invalid prepared record schema')
    if digest(prepare_record(candidate_id, source)) != digest(prepared):
        raise ValueError('Prepared record differs from exact source, model or conversion')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def prepare_jsonl(candidate_id, content):
    candidate, kind = candidate_profile(candidate_id)
    if not isinstance(content, bytes) or len(content) > MAX_INPUT_BYTES:
        raise ValueError('Bounded JSONL bytes required')
    lines = [line for line in content.splitlines() if line.strip()]
    if not 1 <= len(lines) <= MAX_RECORDS:
        raise ValueError('Preparation requires between 1 and 1000 records')
    if any(len(line) > MAX_RECORD_BYTES for line in lines):
        raise ValueError('JSONL record exceeds the preparation limit')
    results, seen = [], set()
    for line in lines:
        record = _prepare_record(candidate, kind, json.loads(line, object_pairs_hook=unique_object))
        if record['sample_id'] in seen:
            raise ValueError('Duplicate sample identity')
        seen.add(record['sample_id'])
        results.append(record)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--emit-messages', action='store_true', help='Explicitly print private prepared records; default output contains hashes only')
    arguments = parser.parse_args()
    try:
        descriptor = os.open(arguments.input, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
                raise ValueError('Bounded regular input file required')
            records = prepare_jsonl(arguments.model, stream.read(MAX_INPUT_BYTES + 1))
        if arguments.emit_messages:
            for record in records:
                print(canonical(record))
        else:
            print(canonical({'candidate_id': arguments.model, 'records': len(records),
                'prepared_records_sha256': digest(records), 'training_ready': False,
                'training_authorized': False, 'runtime_authority': False, 'private_messages_emitted': False}))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        parser.exit(1, 'Message preparation refused; check the candidate, canonical record, outcome and bounded input. No private input content printed.\n')


if __name__ == '__main__':
    main()
