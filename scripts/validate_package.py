#!/usr/bin/env python3
"""Validate AOS data contracts. Does not run models, tools, training or deployments."""
from pathlib import Path
import copy
import hashlib
import json
import math
import re
import sqlite3
import tempfile
from urllib.parse import unquote, urlencode

import jsonschema
import yaml

if __package__:
    from .package_handoff import validate_source_manifest
else:
    from package_handoff import validate_source_manifest

ROOT = Path(__file__).resolve().parents[1]
CHECKS = []

def source_files(pattern):
    excluded = {'data', 'datasets', 'models', 'adapters', 'runs', '.git', '.agents', '.codex', 'node_modules', '__pycache__', 'target', 'dist', 'gen'}
    return (path for path in ROOT.rglob(pattern)
            if not any(part in excluded or part.startswith('.venv') or part.endswith('.egg-info')
                       for part in path.relative_to(ROOT).parts[:-1]))

def check(condition, description):
    if not condition:
        raise AssertionError(description)
    CHECKS.append(description)

def read_json(path):
    return json.loads((ROOT / path).read_text())

def rejected(fn, description):
    try:
        fn()
    except (AssertionError, jsonschema.ValidationError, sqlite3.IntegrityError):
        CHECKS.append(description)
    else:
        raise AssertionError('Expected rejection: ' + description)

def validate_choice(record):
    ids = [o['id'] for o in record['options']]
    assert len(ids) == len(set(ids)), 'duplicate option id'
    prediction = record.get('prediction', record)
    probs = prediction['probabilities']
    assert set(probs) == set(ids), 'probability keys differ from options'
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1 for v in probs.values())
    assert abs(sum(probs.values()) - 1.0) <= 1e-6, 'probability sum'
    assert prediction['selected_option'] in ids, 'unknown selected option'
    if 'target' in record:
        assert record['target']['correct_option'] in ids, 'unknown gold option'

def validate_export(value):
    steps = value['steps']
    assert len({s['step_id'] for s in steps}) == len(steps)
    assert len({s['ordinal'] for s in steps}) == len(steps)
    all_ids = []
    for step in steps:
        snaps = {x['snapshot_id'] for x in step['snapshots']}
        decisions = {x['decision_id']: x for x in step['decisions']}
        actions = {x['action_id']: x for x in step['actions']}
        obs = {x['observation_id'] for x in step['observations']}
        artifacts = {x['artifact_id'] for x in step['artifacts']}
        verifications = {x['verification_id']: x for x in step['verifications']}
        for collection, key in [('snapshots','snapshot_id'),('decisions','decision_id'),('actions','action_id'),('observations','observation_id'),('verifications','verification_id'),('escalations','escalation_id'),('human_interventions','intervention_id'),('model_calls','call_id'),('artifacts','artifact_id'),('labels','label_id')]:
            all_ids += [x[key] for x in step[collection]]
        for d in decisions.values():
            assert d['snapshot_id'] in snaps
            validate_choice(d)
        assert len({a['idempotency_key'] for a in actions.values()}) == len(actions)
        for a in actions.values():
            assert a['decision_id'] in decisions
            assert a['actual_option'] in {o['id'] for o in decisions[a['decision_id']]['options']}
        for o in step['observations']:
            assert o['action_id'] is None or o['action_id'] in actions
        for v in verifications.values():
            assert v['action_id'] in actions
            assert set(v['evidence_refs']) <= obs | artifacts
            if v['result'] == 'passed' and v['method'] in {'independent_read_equals', 'independent_dom_equals', 'independent_canvas_equals'}:
                assert v['expected'] == v['actual']
        for label in step['labels']:
            assert label['verification_ref'] in verifications
        for call in step['model_calls']:
            assert call['deployment_id'] in value['run']['deployment_ids']
        for a in step['artifacts']:
            p = Path(a['relative_path'])
            assert not p.is_absolute() and '..' not in p.parts
    assert len(set(all_ids)) == len(all_ids), 'duplicate exported record ids'
    if value['run']['status'] == 'succeeded':
        assert value['run']['outcome'] == 'passed'
        assert any(v['result'] == 'passed' for s in steps for v in s['verifications'])

def validate_registry(value):
    for key in ['models','adapters','deployments','benchmarks']:
        assert len({r['id'] for r in value[key]}) == len(value[key])
    models = {r['id']:r for r in value['models']}
    adapters = {r['id']:r for r in value['adapters']}
    deployments = {r['id']:r for r in value['deployments']}
    for a in adapters.values():
        assert a['model_id'] in models
        if a['compatibility'] == 'passed':
            assert a['base_sha256'] == models[a['model_id']]['sha256']
    for d in deployments.values():
        assert d['model_id'] in models
        expected = hashlib.sha256(json.dumps(d['config'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert d['config_sha256'] == expected
        if d['adapter_id']:
            assert d['adapter_id'] in adapters
            assert adapters[d['adapter_id']]['model_id'] == d['model_id']
        if d['status'] in ['VALIDATED','ACTIVE']:
            assert models[d['model_id']]['enabled']
            assert models[d['model_id']]['sha256'] and models[d['model_id']]['revision']
            if d['adapter_id']:
                assert adapters[d['adapter_id']]['compatibility'] == 'passed'
    for b in value['benchmarks']:
        assert b['deployment_id'] in deployments

def validate_lifecycle_event(event):
    assert event['birth']['workspace']['owner_uid'] == event['birth']['process']['uid'], 'lifecycle owner mismatch'
    assert (event['stage'] == 'intent') == (event['sequence'] == 0), 'lifecycle intent sequence'
    assert (event['sequence'] == 0) == (event['previous_sha256'] is None), 'lifecycle previous event binding'
    assert (event['stage'] == 'intent') == (event['container_id'] is None), 'lifecycle container binding'
    assert event['sequence'] in {'intent':{0}, 'created':{1}, 'started':{2}, 'removed':{2,3}}[event['stage']], 'lifecycle stage sequence'

def validate_files():
    required = ['README.md','CODEX_KICKOFF.md','AGENTS.md','CLAUDE.md','CONTRIBUTING.md','SECURITY.md','docs/README.md',
        'docs/CLAUDE_HANDOFF.md','docs/EXTENDING_AOS.md','docs/CONTINUOUS_IMPROVEMENT.md','docs/SOURCE_HANDOFF.md',
        'docs/INTEGRATION_COMPOSITIONS.md',
        'scripts/package_handoff.py',
        'database/migrations/0001_trajectory_store.sql','database/migrations/0002_registries.sql',
        'database/migrations/0004_desktop_control.sql','computer/Dockerfile','docs/DESKTOP_RUNTIME.md',
        'ui/package.json','ui/pnpm-lock.yaml','ui/src-tauri/Cargo.lock','docs/UI_RUNTIME.md',
        'schemas/system1_choice.schema.json','schemas/system2_supervisor.schema.json','schemas/trajectory_export.schema.json',
        'training/recipes/decider-computer-v001.yaml','training/recipes/bonsai-computer-recovery-v001.yaml']
    for p in required:
        check((ROOT / p).is_file(), 'Required file: ' + p)
    ui_config = read_json('ui/src-tauri/tauri.conf.json')
    check(ui_config['app']['security']['capabilities']==[], 'Native UI grants no IPC capabilities')
    check(ui_config['app']['windows']==[], 'Native window is created through guarded Rust builder')
    validators = {}
    for p in sorted((ROOT/'schemas').glob('*.schema.json')):
        s = json.loads(p.read_text())
        jsonschema.Draft202012Validator.check_schema(s)
        validators[p.name] = jsonschema.Draft202012Validator(s, format_checker=jsonschema.FormatChecker())
        CHECKS.append('Valid JSON Schema: ' + p.name)
    knowledge = read_json('examples/knowledge.json')
    check(knowledge['synthetic'] is True, 'Uploaded document knowledge fixture is explicitly synthetic')
    for operation, request in knowledge['requests'].items():
        validators['knowledge_' + operation.replace('-', '_') + '_request.schema.json'].validate(request)
    for operation, response in knowledge['responses'].items():
        validators['knowledge_' + operation.replace('-', '_') + '_response.schema.json'].validate(response)
        if operation in {'publish', 'review', 'inspect', 'catalog', 'search'}:
            check(response['untrusted'] is True and response['execution_authorized'] is False
                  and response['training_ready'] is False and response['gold'] is False,
                  'Document knowledge evidence cannot grant execution or training: ' + operation)
    def knowledge_digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    publication = knowledge['responses']['publish-preview']
    document = publication['document']
    check(publication['document_sha256'] == knowledge_digest(document)
          and publication['preview_sha256'] == knowledge_digest({key: value for key, value in publication.items() if key != 'preview_sha256'}),
          'Document publication binds exact canonical content and preview hashes')
    check(document['rights_attested'] is True and document['storage_consent'] is True
          and document['content_sha256'] == hashlib.sha256(document['text'].encode()).hexdigest(),
          'Document knowledge binds separately attested rights and uploaded UTF8 content')
    for index, chunk in enumerate(document['chunks']):
        check(chunk['index'] == index and chunk['start'] == index * 1024
              and chunk['end'] == min((index + 1) * 1024, len(document['text']))
              and chunk['text'] == document['text'][chunk['start']:chunk['end']]
              and chunk['chunk_sha256'] == hashlib.sha256(chunk['text'].encode()).hexdigest(),
              'Document chunk citation binds exact Unicode span and UTF8 hash')
    search = knowledge['responses']['search']
    check(search['method'] == 'deterministic_lexical'
          and search['used_context_chars'] == sum(len(hit['text']) for hit in search['hits'])
          and search['used_context_chars'] <= search['context_chars']
          and len(search['hits']) <= search['top_k'], 'Knowledge lexical retrieval respects explicit context and hit budgets')
    for hit in search['hits']:
        check(hit['document_sha256'] == publication['document_sha256']
              and hit['review_sha256'] == knowledge['responses']['review']['review_sha256']
              and hit['text'] == document['chunks'][hit['chunk_index']]['text']
              and hit['chunk_sha256'] == hashlib.sha256(hit['text'].encode()).hexdigest(),
              'Knowledge search citations retain whole reviewed immutable chunks')
    upload_validator = validators['knowledge_publish_preview_request.schema.json']
    for field, value in (('rights_attested', 1), ('storage_consent', False), ('source_path', '/synthetic/outside'),
                         ('url', 'https://example.invalid'), ('text', 'x' * 32769)):
        rejected(lambda field=field, value=value: upload_validator.validate(knowledge['requests']['publish-preview'] | {field: value}),
                 'Document ingestion rejects implicit rights, paths, URL fetches or text overflow: ' + field)
    rejected(lambda: validators['knowledge_search_response.schema.json'].validate(search | {'execution_authorized': True}),
             'Retrieval response cannot claim execution authority')
    knowledge_answer = read_json('examples/knowledge_answer.json')
    check(knowledge_answer['synthetic'] is True, 'Knowledge answer fixture is explicitly synthetic')
    for operation, request in knowledge_answer['requests'].items():
        validators['knowledge_answer_' + operation + '_request.schema.json'].validate(request)
    for operation, response in knowledge_answer['responses'].items():
        validators['knowledge_answer_' + operation + '_response.schema.json'].validate(response)
        check(response['untrusted'] is True and response['execution_authorized'] is False
              and response['training_ready'] is False and response['gold'] is False,
              'Knowledge answer cannot grant execution or training: ' + operation)
    planning_knowledge = read_json('examples/owned_skill_knowledge.json')
    check(planning_knowledge['synthetic'] is True, 'S2 planning context example is explicitly synthetic')
    for operation, request in planning_knowledge['requests'].items():
        validators['owned_skill_knowledge_' + operation + '_request.schema.json'].validate(request)
    planning_preview = planning_knowledge['responses']['preview']
    validators['owned_skill_knowledge_preview_response.schema.json'].validate(planning_preview)
    validators['owned_skill_knowledge_payload.schema.json'].validate(planning_knowledge['payload'])
    planning_wire = planning_knowledge['wire']
    validators['owned_skill_knowledge_preview_transport.schema.json'].validate(planning_wire['preview'])
    validators['owned_skill_knowledge_canonical_start_request.schema.json'].validate(planning_wire['start'])
    planning_canonical = json.dumps(planning_preview, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    check(planning_wire['preview']['preview_canonical'] == planning_wire['start']['preview_canonical'] == planning_canonical,
          'S2 canonical wire preserves the exact preview bytes')
    check(planning_wire['preview']['preview'] == planning_preview
          and planning_wire['start']['confirm_sha256'] == planning_preview['confirm_sha256'],
          'S2 wire and historical inner preview retain their identities')
    for field in ('inference_consent', 'storage_consent'):
        rejected(lambda: validators['owned_skill_knowledge_canonical_start_request.schema.json'].validate(
            planning_wire['start'] | {field: 1}), 'S2 canonical wire requires exact boolean ' + field)
    check(planning_preview['confirm_sha256'] == knowledge_digest({key: value for key, value in planning_preview.items()
          if key != 'confirm_sha256'}), 'S2 planning context binds exact preview confirmation')
    check(hashlib.sha256(planning_preview['context_text'].encode()).hexdigest() == planning_preview['context_sha256'],
          'S2 planning context hashes exact untrusted source bytes')
    for field in ('execution_authorized', 'training_ready', 'gold', 'causality_verified', 'scope_authorization_verified'):
        rejected(lambda: validators['owned_skill_knowledge_preview_response.schema.json'].validate(
            planning_preview | {field: True}), 'S2 planning context cannot assert ' + field)
    for field in ('inference_consent', 'storage_consent'):
        rejected(lambda: validators['owned_skill_knowledge_start_request.schema.json'].validate(
            planning_knowledge['requests']['start'] | {field: 1}), 'S2 planning requires exact boolean ' + field)
    answer_report = knowledge_answer['responses']['report']
    answer_preview = knowledge_answer['responses']['preview']
    answer_bundle = answer_report['bundle']
    check(answer_preview['confirm_sha256'] == knowledge_digest({key: value for key, value in answer_preview.items()
          if key != 'confirm_sha256'}) and answer_report['bundle_sha256'] == knowledge_digest(answer_bundle),
          'Knowledge answer binds exact preview and immutable request-response bundle hashes')
    check(answer_bundle['preview'] == answer_report['preview'] == answer_preview
          and answer_bundle['model_response'] == answer_report['model_response']
          and answer_bundle['model_request'] == answer_report['model_request']
          and answer_bundle['consent'] == {'inference_consent': True, 'storage_consent': True,
                                         'confirm_sha256': answer_preview['confirm_sha256']}
          and answer_report['real_model'] is False and answer_report['semantic_relevance_verified'] is False,
          'Synthetic extractive answer preserves separate consent and never claims real model or relevance proof')
    for quote in answer_report['model_response']['quotes']:
        citation = answer_bundle['evidence'][int(quote['citation_id'][1:]) - 1]
        check(quote['text'] in citation['text'], 'Knowledge answer quote is verbatim cited evidence')
    rejected(lambda: validators['knowledge_answer_start_request.schema.json'].validate(
        knowledge_answer['requests']['start'] | {'consent': 1}), 'Model inference requires exact boolean consent')
    rejected(lambda: validators['knowledge_answer_report_response.schema.json'].validate(
        answer_report | {'execution_authorized': True}), 'Knowledge answer report cannot claim action authority')
    failure = read_json('examples/failure_improvement.json')
    failure_validator = validators['failure_improvement.schema.json']
    failure_validator.validate(failure)
    check(failure['candidate']['synthetic'] is True, 'Failure improvement fixture is explicitly synthetic')
    for field in ('training_ready', 'gold', 'execution_authorized', 'failure_attribution_verified'):
        changed = copy.deepcopy(failure)
        changed['candidate'][field] = True
        rejected(lambda: failure_validator.validate(changed), 'Failure review cannot assert ' + field)
    rejected(lambda: failure_validator.validate(failure | {'raw_prompt': 'synthetic hidden content'}),
             'Failure improvement rejects raw content fields')
    for name in ('failure_followup_preview', 'failure_followup_start', 'failure_followup_report'):
        fixture = read_json('examples/' + name + '.json')
        selected_validator = validators[name + '.schema.json']
        selected_validator.validate(fixture)
        check(fixture['schema_version'] == '1.0', 'Failure follow-up fixture version: ' + name)
        check(fixture['manual_approval_required'] is True,
              'Failure follow-up requires manual approval: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'manual_approval_required': False}),
                 'Failure follow-up rejects delegated approval: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'raw_prompt': 'private'}),
                 'Failure follow-up rejects raw content: ' + name)
        for field in ('guidance_applied', 'causality_verified', 'gold', 'training_ready'):
            if field in fixture:
                check(fixture[field] is False, 'Failure follow-up fixture denies ' + field + ': ' + name)
                rejected(lambda field=field: selected_validator.validate(fixture | {field: True}),
                         'Failure follow-up rejects ' + field + ': ' + name)
    for name in ('failure_guidance_preview', 'failure_guidance_start', 'failure_guidance_report'):
        fixture = read_json('examples/' + name + '.json')
        selected_validator = validators[name + '.schema.json']
        selected_validator.validate(fixture)
        check(fixture['manual_approval_required'] is True, 'Failure guidance requires manual approval: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'raw_correction': 'private'}),
                 'Failure guidance rejects raw correction text: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'manual_approval_required': False}),
                 'Failure guidance rejects delegated approval: ' + name)
        for field in ('causality_verified', 'gold', 'training_ready'):
            if field in fixture:
                check(fixture[field] is False, 'Guidance fixture denies ' + field + ': ' + name)
                rejected(lambda field=field: selected_validator.validate(fixture | {field: True}),
                         'Guidance cannot certify ' + field + ': ' + name)
        if name == 'failure_guidance_report':
            rejected(lambda: selected_validator.validate(fixture | {'guidance_applied': True}),
                     'Fixture context alone cannot certify actual-model guidance')
    for name in ('hello_guidance_entry', 'hello_reuse_publication_preview', 'hello_reuse_entry_response',
                 'hello_reuse_preview', 'hello_reuse_start', 'hello_reuse_report'):
        fixture = read_json('examples/' + name + '.json')
        selected_validator = validators[name + '.schema.json']
        selected_validator.validate(fixture)
        check(fixture['schema_version'] == '1.0', 'Finite hello reuse fixture version: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'raw_guidance': 'private'}),
                 'Finite hello reuse rejects arbitrary guidance content: ' + name)
        for field in ('execution_authorized', 'causality_verified', 'gold', 'training_ready'):
            if field in fixture:
                check(fixture[field] is False, 'Finite hello reuse denies ' + field + ': ' + name)
                rejected(lambda field=field: selected_validator.validate(fixture | {field: True}),
                         'Finite hello reuse cannot certify ' + field + ': ' + name)
        if 'manual_approval_required' in fixture:
            rejected(lambda: selected_validator.validate(fixture | {'manual_approval_required': False}),
                     'Finite hello reuse requires fresh manual approval: ' + name)
        entry = fixture if name == 'hello_guidance_entry' else fixture.get('entry')
        if entry is not None:
            check(entry['scope']['image_id'] == 'synthetic-image'
                  and entry['source']['source_job_id'] == 'job-fixture',
                  'Finite hello guidance source is explicitly synthetic: ' + name)
            if 'entry_sha256' in fixture:
                check(fixture['entry_sha256'] == hashlib.sha256(json.dumps(
                    entry, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
                    'Finite hello guidance fixture binds exact entry: ' + name)
            for field, value in (('guidance_code', 'repair_environment'), ('entry_version', 2),
                                 ('context_version', 'unimplemented')):
                changed = copy.deepcopy(fixture)
                selected_entry = changed if name == 'hello_guidance_entry' else changed['entry']
                selected_entry[field] = value
                rejected(lambda: selected_validator.validate(changed),
                         'Finite hello guidance rejects unsupported ' + field + ': ' + name)
            changed = copy.deepcopy(fixture)
            selected_entry = changed if name == 'hello_guidance_entry' else changed['entry']
            selected_entry['scope']['role'] = 'system2'
            rejected(lambda: selected_validator.validate(changed),
                     'Finite hello guidance rejects foreign role: ' + name)
        if 'confirm_sha256' in fixture:
            check(fixture['confirm_sha256'] == hashlib.sha256(json.dumps(
                {key: value for key, value in fixture.items() if key != 'confirm_sha256'},
                sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
                'Finite hello reuse fixture binds exact preview: ' + name)
        if name == 'hello_reuse_report':
            changed = copy.deepcopy(fixture)
            changed['guidance']['guidance_applied'] = True
            rejected(lambda: selected_validator.validate(changed),
                     'Finite hello fixture cannot assert actual-model guidance from context alone')
    for name in ('owned_adapter_runtime_admission', 'owned_adapter_runtime_observation', 'owned_adapter_inference',
                 'owned_adapter_pair', 'owned_adapter_pair_observation', 'owned_adapter_pair_report'):
        fixture = read_json('examples/' + name + '.json')
        selected_validator = validators[name + '.schema.json']
        selected_validator.validate(fixture)
        check(fixture['synthetic'] is True, 'Owned adapter fixture is explicitly synthetic: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'promotion_authorized': True}),
                 'Owned adapter fixture cannot authorize promotion: ' + name)
        rejected(lambda: selected_validator.validate(fixture | {'raw_content': 'private'}),
                 'Owned adapter fixture rejects private content: ' + name)
    rows = {}
    for name in ['system1_choice','system2_supervisor']:
        rows[name] = [json.loads(line) for line in (ROOT/f'examples/{name}.jsonl').read_text().splitlines()]
        for row in rows[name]:
            validators[name+'.schema.json'].validate(row)
            check(row['provenance']['synthetic'] is True, 'Fixture synthetic: ' + row['sample_id'])
            if name == 'system1_choice':
                validate_choice(row)
            else:
                plan = row['target']['corrected_plan']
                check([p['order'] for p in plan] == list(range(1,len(plan)+1)), 'Supervisor plan ordering')
                check((row['target']['outcome']=='human_required') == row['target']['needs_human'], 'Supervisor human outcome consistency')
    reuse_preview = read_json('examples/owned_skill_reuse_preview.json')
    validators['owned_skill_reuse_preview.schema.json'].validate(reuse_preview)
    check(reuse_preview['synthetic'] is True, 'Owned skill reuse preview is synthetic')
    for field in ('execution_authorized', 'old_actions_replayed', 'activation_authorized', 'training_ready'):
        rejected(lambda field=field: validators['owned_skill_reuse_preview.schema.json'].validate(
            {**reuse_preview, field: True}), 'Reject authorizing owned reuse preview: ' + field)
    for filename, entries in (
            ('recovery_session', [('session_runtime_binding', 'binding'), ('recovery_session', 'report')]),
            ('native_package', [('native_package', 'manifest')]),
            ('dataset_preflight', [('dataset_preflight', 'report')]),
            ('dataset_bound_loss', [('dataset_bound_loss', 'report')]),
            ('dataset_gradient_probe', [('dataset_gradient_probe', 'report')]),
            ('dataset_adapter_candidate', [('dataset_adapter_candidate', 'report')]),
            ('dataset_adapter_registry_inspect', [('dataset_adapter_registry_inspect', 'report')]),
            ('dataset_adapter_runtime_probe', [('dataset_adapter_runtime_probe', 'report')]),
            ('benchmark', [('benchmark', 'report')]),
            ('compound_execution', [('compound_sequence_start', 'request'), ('sequence_start', 'sequence_request')]),
            ('local_app', [('local_app_state', 'state')]),
            ('owned_form_invocation_session', [('owned_form_invocation_session', 'manifest')]),
            ('owned_form_recipe_session', [('owned_form_recipe_session', 'manifest')]),
            ('web_application_profile', [('web_application_profile', 'profile'), ('web_application_report', 'report')]),
            ('web_application_draft_capabilities', [('web_application_draft_capabilities', 'capabilities')]),
            ('web_https_preflight', [('web_https_preflight', 'report')]),
            ('web_bound_https_preflight', [('web_bound_https_preflight', 'report')]),
            ('web_readonly_route_plan', [('web_readonly_route_plan', 'plan'),
                                         ('web_https_readonly_route', 'report')]),
            ('web_static_asset_plan', [('web_static_asset_plan', 'plan'),
                                       ('web_static_asset_fetch', 'report'),
                                       ('web_static_bundle_relay', 'relay_report')]),
            ('web_static_image_asset_plan', [('web_static_asset_plan', 'plan'),
                                             ('web_static_asset_fetch', 'report')]),
            ('web_readonly_data_bundle', [('web_readonly_data_bundle_plan', 'plan'),
                                          ('web_readonly_data_fetch', 'report'),
                                          ('web_readonly_data_bundle_relay', 'relay_report')]),
            ('web_https_relay', [('web_https_relay', 'report')]),
            ('web_https_form_transport', [('web_https_form_plan', 'plan'),
                                          ('web_https_form_transport', 'report')]),
            ('web_https_form_fields', [('web_https_form_fields', 'configuration')]),
            ('web_https_form_state', [('web_https_form_state_plan', 'plan'),
                                      ('web_https_form_state_report', 'report')]),
            ('web_remote_entry_observation', [('web_remote_entry_observation', 'observation')]),
            ('web_remote_route_observation', [('web_remote_route_observation', 'observation')]),
            ('site_page_draft', [('site_page_draft', 'page')]),
            ('site_page_evidence', [('site_page_evidence', 'page')]),
            ('site_page_change', [('site_page_change', 'report')]),
            ('site_page_retrieval', [('site_page_retrieval', 'report')]),
            ('site_page_retrieval_bound', [('site_page_retrieval_bound', 'report')]),
            ('remote_route_evidence', [('remote_route_evidence', 'report')]),
            ('remote_static_learning_source', [('remote_static_learning_source', 'report')]),
            ('remote_form_learning_source', [('remote_form_learning_source', 'report')]),
            ('remote_form_cookie_learning_source', [
                ('remote_form_cookie_learning_source', 'report')]),
            ('remote_form_json_oracle', [
                ('remote_form_json_oracle_plan', 'plan'),
                ('remote_form_json_oracle_report', 'report')]),
            ('remote_form_json_submission', [
                ('remote_form_json_submission_plan', 'plan'),
                ('remote_form_json_submission_report', 'report')]),
            ('remote_form_state_learning_source', [('remote_form_state_learning_source', 'report')]),
            ('remote_form_repeat', [('remote_form_repeat', 'report')]),
            ('remote_form_state_repeat', [('remote_form_state_repeat', 'report')]),
            ('remote_form_learning_stream', [
                ('remote_form_learning_stream_entry', 'entry'),
                ('remote_form_learning_stream_report', 'report')]),
            ('remote_form_state_learning_stream', [
                ('remote_form_state_learning_stream_entry', 'entry'),
                ('remote_form_state_learning_stream_report', 'report')]),
            ('remote_readonly_data_source', [('remote_readonly_data_source', 'report')]),
            ('remote_readonly_data_change', [('remote_readonly_data_change', 'report')]),
            ('remote_readonly_data_knowledge', [('remote_readonly_data_knowledge', 'report')]),
            ('remote_readonly_data_page_draft_seed', [
                ('remote_readonly_data_page_draft_seed', 'report')]),
            ('remote_readonly_data_page_draft_registration', [
                ('remote_readonly_data_page_draft_registration', 'report')]),
            ('remote_readonly_data_knowledge_ui_preview', [
                ('remote_readonly_data_knowledge_ui_preview', 'preview')]),
            ('remote_readonly_data_knowledge_review', [
                ('remote_readonly_data_knowledge_review', 'record'),
                ('remote_readonly_data_knowledge_review_receipt', 'receipt'),
                ('remote_readonly_data_knowledge_recheck', 'recheck')]),
            ('remote_readonly_data_knowledge_live', [
                ('remote_readonly_data_knowledge_live_pin', 'pin'),
                ('remote_readonly_data_knowledge_live_event', 'matched_event'),
                ('remote_readonly_data_knowledge_live_event', 'stale_event')]),
            ('remote_learning_consent', [('remote_learning_consent', 'consent')]),
            ('remote_learning_revocation', [('remote_learning_revocation', 'revocation'),
                                            ('remote_learning_revocation', 'retention_revocation'),
                                            ('remote_learning_revocation_report', 'report'),
                                            ('remote_learning_retention_report', 'retention_report'),
                                            ('remote_learning_managed_retention_report', 'managed_retention_report'),
                                            ('remote_learning_retention_status', 'retention_status')]),
            ('remote_learning_stream', [('remote_learning_stream_entry', 'entry'),
                                        ('remote_learning_stream_report', 'report')]),
            ('remote_static_learning_stream', [('remote_static_learning_stream_entry', 'entry'),
                                               ('remote_static_learning_stream_report', 'report')]),
            ('remote_readonly_data_stream', [('remote_readonly_data_stream_entry', 'entry'),
                                             ('remote_readonly_data_stream_report', 'report')]),
            ('remote_route_change', [('remote_route_change', 'report')]),
            ('remote_page_draft_seed', [('remote_page_draft_seed', 'report')]),
            ('remote_page_draft_registration', [('remote_page_draft_registration', 'report')]),
            ('remote_navigation_graph', [('remote_navigation_graph', 'report')]),
            ('remote_route_knowledge', [('remote_route_knowledge', 'report')]),
            ('remote_route_knowledge_ui_preview', [('remote_route_knowledge_ui_preview', 'preview')]),
            ('remote_route_knowledge_review', [
                ('remote_route_knowledge_review', 'record'),
                ('remote_route_knowledge_review_receipt', 'receipt'),
                ('remote_route_knowledge_live_pin', 'live_pin'),
                ('remote_route_knowledge_live_event', 'live_event'),
                ('remote_route_knowledge_reuse', 'reuse')]),
            ('site_page_comparison', [('site_page_comparison', 'comparison')]),
            ('learning_candidate_report', [('learning_candidate_report', 'report')]),
            ('learning_event_outbox', [('learning_event_outbox', 'checkpoint')]),
            ('learning_event_stream', [('learning_event_stream_entry', 'entry'),
                                       ('learning_event_stream_entry', 'downstream_entry'),
                                       ('learning_event_stream_report', 'report')]),
            ('decider_calibration_smoke', [('decider_calibration_smoke', 'report')]),
            ('decider_row_candidate', [('decider_row_candidate', 'report')]),
            ('staging_skill_candidate', [('staging_skill_candidate', 'candidate')]),
            ('vision_skill_candidate', [('vision_skill_candidate', 'candidate')]),
            ('vision_dual_role_candidate', [('vision_dual_role_candidate', 'candidate')]),
            ('site_skill_draft', [('site_skill_draft', 'skill')]),
            ('site_skill_provenance', [('site_skill_provenance', 'report')]),
            ('site_skill_validation_plan', [('site_skill_validation_plan', 'plan')]),
            ('site_skill_case_binding', [('site_skill_validation_plan', 'plan'),
                                         ('site_skill_case_inputs', 'case_inputs'),
                                         ('site_skill_case_report', 'report')]),
            ('site_skill_form_case_binding', [('web_task_contract', 'task'),
                                              ('web_https_form_plan', 'form_plan'),
                                              ('site_skill_form_case_report', 'report')]),
            ('site_skill_form_cohort', [('site_skill_form_cohort_report', 'report')]),
            ('site_skill_form_invocation_audit', [('site_skill_form_invocation_audit', 'report')]),
            ('site_skill_form_recipe', [('site_skill_form_recipe', 'recipe')]),
            ('site_skill_form_recipe_invocation', [('site_skill_form_recipe_invocation', 'invocation')]),
            ('site_skill_form_recipe_audit', [('site_skill_form_recipe_audit', 'report')]),
            ('site_skill_form_recipe_candidate', [('site_skill_form_recipe_candidate', 'candidate')]),
            ('site_skill_form_recipe_candidate_owned', [('site_skill_form_recipe_candidate', 'candidate')]),
            ('owned_candidate_execution_bundle', [('owned_candidate_execution_bundle', 'manifest'),
                                                   ('owned_candidate_execution_completion', 'completion')]),
            ('owned_candidate_planned_execution_bundle', [('owned_candidate_execution_bundle', 'manifest')]),
            ('owned_candidate_review', [('owned_candidate_review_receipt', 'receipt'),
                                         ('owned_candidate_review_revocation', 'revocation')]),
            ('site_skill_recipe_candidate_review_admission', [('site_skill_recipe_candidate_review_admission', 'admission')]),
            ('owned_skill_release', [('owned_skill_release', 'release'),
                                     ('owned_skill_selection', 'selection'),
                                     ('owned_skill_selection_head', 'head')]),
            ('owned_skill_reuse_admission', [('owned_skill_reuse_admission', 'admission')]),
            ('owned_skill_plan', [('owned_skill_plan', 'plan')]),
            ('owned_skill_planning_bundle', [('owned_skill_planning_bundle', 'bundle')]),
            ('owned_episode_candidate', [('owned_episode_candidate', 'candidate')]),
            ('owned_episode_export_record', [('owned_episode_export_record', 'record')]),
            ('owned_episode_preparation', [('owned_episode_conversion_manifest', 'manifest'),
                                           ('owned_episode_readiness', 'readiness'),
                                           ('owned_episode_tokenizer', 'tokenizer')]),
            ('owned_episode_adaptation', [('owned_episode_adaptation_authorization', 'authorization'),
                                          ('owned_episode_adaptation_train', 'train'),
                                          ('owned_episode_adaptation_replay', 'replay'),
                                          ('owned_episode_adaptation', 'report')]),
            ('owned_episode_learning', [('owned_episode_consent', 'consent'),
                                         ('owned_episode_review', 'review'), ('owned_episode_export', 'export')]),
            ('site_skill_owned_planning_admission', [('site_skill_owned_planning_admission', 'admission')]),
            ('site_skill_owned_reuse_admission', [('site_skill_owned_reuse_admission', 'admission')]),
            ('site_skill_recipe_candidate_release_admission', [('site_skill_recipe_candidate_release_admission', 'admission')]),
            ('site_skill_form_recipe_candidate_admission', [('site_skill_form_recipe_candidate_admission', 'admission')]),
            ('site_skill_form_recipe_candidate_execution', [('site_skill_form_recipe_candidate_execution', 'report')]),
            ('site_skill_validation_report', [('site_skill_validation_report', 'report')]),
            ('site_skill_rehearsal_report', [('site_skill_rehearsal_report', 'report')]),
            ('synthetic_navigation_pin', [('web_application_profile', 'profile'),
                                          ('synthetic_navigation_pin', 'pin')]),
            ('synthetic_staging_pin', [('web_application_profile', 'profile'),
                                       ('synthetic_staging_pin', 'pin')]),
            ('web_application_binding', [('web_task_contract', 'task'), ('web_runtime_pin', 'runtime'),
                                         ('web_task_admission_draft', 'draft'),
                                         ('web_runtime_attestation', 'attestation')])):
        fixture = read_json('examples/'+filename+'.json')
        check(fixture['synthetic'] is True, 'Parallel milestone fixture is synthetic: '+filename)
        for schema, key in entries:
            payload = fixture[key]
            validators[schema+'.schema.json'].validate(payload)
            CHECKS.append('Parallel milestone canonical fixture: '+schema)
            rejected(lambda:validators[schema+'.schema.json'].validate({**payload,'raw_content':'private'}), 'Reject private milestone content: '+schema)
            for field in ('execution_authorized','collection_authorized','resume_authorized','cleanup_authorized','automatic_replay_allowed',
                          'lease_restored','approval_restored','training_ready','forward_loss_verified',
                          'supervisor_tokenizer_verified','promotion_authorized','installation_authorized',
                          'build_provenance_verified','runtime_dependencies_bundled','standalone_inference',
                          'training_authorized','real_model','statistical_superiority_established',
                          'network_access','network_access_authorized','runtime_identity_verified',
                          'activation_authorized','reviewed','profile_bound',
                          'supervisor_outcome_verified'):
                if field in payload:
                    if schema == 'owned_episode_consent' and field == 'collection_authorized':
                        check(payload[field] is True and payload['synthetic'] is True
                              and payload['scope'] == 'owned_synthetic_episode_content'
                              and payload['training_authorized'] is False
                              and payload['network_export_authorized'] is False,
                              'Illustrative consent is limited to owned synthetic local collection')
                        rejected(lambda:validators[schema+'.schema.json'].validate({**payload,field:False}),
                                 'Reject absent explicit episode collection consent')
                        continue
                    if schema in {'site_page_retrieval_bound', 'remote_route_evidence',
                                  'remote_route_change', 'remote_readonly_data_change',
                                  'remote_readonly_data_knowledge',
                                  'remote_page_draft_seed',
                                  'remote_readonly_data_page_draft_seed',
                                  'remote_navigation_graph',
                                  'remote_route_knowledge'} and field == 'profile_bound':
                        check(payload[field] is True, 'Selected historical run provenance is explicit')
                        rejected(lambda:validators[schema+'.schema.json'].validate({**payload,field:False}),
                                 'Reject missing historical run provenance')
                        continue
                    check(payload[field] is False, 'Milestone fixture grants no authority: '+schema+'/'+field)
                    rejected(lambda:validators[schema+'.schema.json'].validate({**payload,field:True}), 'Reject milestone authority expansion: '+schema+'/'+field)
    conversion_fixture = read_json('examples/owned_episode_conversion.json')
    check(conversion_fixture['synthetic'] is True, 'Episode conversion fixture is synthetic')
    for record in conversion_fixture['records']:
        validators['owned_episode_conversion.schema.json'].validate(record)
        check(record['training_ready'] is False, 'Episode conversion does not authorize training')
        rejected(lambda:validators['owned_episode_conversion.schema.json'].validate({**record, 'training_ready': True}),
                 'Reject episode conversion training authority')
    web_fixture = read_json('examples/web_application_profile.json')
    case_fixture = read_json('examples/site_skill_case_binding.json')
    case_plan, case_inputs, case_report = (case_fixture[key]
                                           for key in ('plan', 'case_inputs', 'report'))
    canonical_hash = lambda value: hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    case_variant_hash = lambda value: canonical_hash({
        'domain': 'site_skill_parameter_variant_v1', 'parameters': value})
    check(case_fixture['synthetic'] is True
          and case_inputs['plan_sha256'] == case_report['plan_sha256'] == canonical_hash(case_plan)
          and case_report['case_inputs_sha256'] == canonical_hash(case_inputs)
          and case_report['skill_sha256'] == case_plan['skill_sha256']
          and case_report['model_role'] == case_plan['model_role']
          and len(case_inputs['cases']) == len(case_plan['cases']) == case_report['case_count']
          and all(actual['case_key'] == expected['case_key']
                  and sorted(actual['parameters']) == expected['parameter_keys']
                  and case_variant_hash(actual['parameters']) == expected['parameter_variant_sha256']
                  for expected, actual in zip(case_plan['cases'], case_inputs['cases'], strict=True)),
          'Synthetic skill case inputs bind exact plan and variants')
    form_case_fixture = read_json('examples/site_skill_form_case_binding.json')
    form_task, form_plan, form_bindings, form_report = (
        form_case_fixture[key] for key in ('task', 'form_plan', 'field_bindings', 'report'))
    for field_binding in form_bindings:
        validators['site_skill_form_field_binding.schema.json'].validate(field_binding)
        CHECKS.append('Synthetic skill form field binding schema')
    selected_case = next((case for case in case_inputs['cases']
                          if case['case_key'] == form_report['case_key']), None)
    form_fields = ([(item['form_field_name'], selected_case['parameters'][item['parameter_key']])
                    for item in form_bindings] if selected_case is not None else [])
    body = urlencode(form_fields).encode()
    check(form_case_fixture['synthetic'] is True
          and selected_case is not None
          and len(form_bindings) == len(selected_case['parameters']) == form_report['field_count']
          and len({item['parameter_key'] for item in form_bindings}) == len(form_bindings)
          and len({item['form_field_name'] for item in form_bindings}) == len(form_bindings)
          and form_report['skill_sha256'] == case_plan['skill_sha256']
          and form_report['skill_plan_sha256'] == canonical_hash(case_plan)
          and form_report['case_inputs_sha256'] == canonical_hash(case_inputs)
          and form_report['task_sha256'] == form_plan['task_sha256'] == canonical_hash(form_task)
          and form_report['form_plan_sha256'] == canonical_hash(form_plan)
          and form_report['field_binding_sha256'] == canonical_hash(form_bindings)
          and form_task['task_key'] == case_plan['task_key']
          and form_task['profile_sha256'] == form_plan['profile_sha256'] == case_plan['profile_sha256']
          and form_plan['entry_url'] == form_task['entry_url']
          and form_plan['body_sha256'] == hashlib.sha256(body).hexdigest()
          and form_plan['body_bytes'] == len(body),
          'Synthetic skill case values bind exact HTTPS form task and body')
    route_fixture = read_json('examples/web_readonly_route_plan.json')
    route_plan, route_report = route_fixture['plan'], route_fixture['report']
    check(route_report['plan_sha256'] == hashlib.sha256(json.dumps(
          route_plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and route_report['request_sha256'] == hashlib.sha256(json.dumps(
              {'method': 'GET', 'url': route_plan['routes'][route_report['route_index']]},
              sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
          'Read-only route report binds exact plan')
    static_fixture = read_json('examples/web_static_asset_plan.json')
    static_plan, static_report = static_fixture['plan'], static_fixture['report']
    check(static_fixture['synthetic'] is True and static_report['plan_sha256'] == hashlib.sha256(
          json.dumps(static_plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and static_report['profile_sha256'] == static_plan['profile_sha256']
          and static_report['task_sha256'] == static_plan['task_sha256']
          and static_report['content_type'] == static_plan['assets'][static_report['asset_index']]['content_type']
          and static_report['request_sha256'] == hashlib.sha256(json.dumps(
              {'method': 'GET', 'url': static_plan['assets'][static_report['asset_index']]['url']},
              sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
          'Static asset report binds exact synthetic plan')
    image_fixture = read_json('examples/web_static_image_asset_plan.json')
    image_plan, image_report = image_fixture['plan'], image_fixture['report']
    check(image_fixture['synthetic'] is True
          and image_plan['assets'][0]['content_type'] == image_report['content_type'] == 'image/png'
          and image_report['plan_sha256'] == hashlib.sha256(json.dumps(
              image_plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and image_report['request_sha256'] == hashlib.sha256(json.dumps(
              {'method': 'GET', 'url': image_plan['assets'][0]['url']},
              sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and image_report['execution_authorized'] is False
          and image_report['collection_authorized'] is False
          and image_report['training_ready'] is False,
          'Static image asset fixture binds exact synthetic plan without authority')
    static_relay = static_fixture['relay_report']
    check(static_relay['plan_sha256'] == static_report['plan_sha256']
          and static_relay['profile_sha256'] == static_plan['profile_sha256']
          and static_relay['task_sha256'] == static_plan['task_sha256']
          and static_relay['asset_response_sha256'] == [static_report['response_sha256']]
          and static_relay['request_attempts'] >= len(static_plan['assets']) + 1,
          'Static bundle relay report binds exact synthetic assets')
    data_fixture = read_json('examples/web_readonly_data_bundle.json')
    data_plan, data_report, data_relay = (data_fixture[key]
                                          for key in ('plan', 'report', 'relay_report'))
    data_plan_hash = hashlib.sha256(json.dumps(
        data_plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    check(data_fixture['synthetic'] is True
          and data_plan['schema_version'] == '2.0'
          and data_report['plan_sha256'] == data_relay['plan_sha256'] == data_plan_hash
          and data_report['profile_sha256'] == data_relay['profile_sha256'] == data_plan['profile_sha256']
          and data_report['task_sha256'] == data_relay['task_sha256'] == data_plan['task_sha256']
          and data_report['content_type'] == data_plan['data_resources'][data_report['data_index']]['content_type']
          and data_report['request_sha256'] == hashlib.sha256(json.dumps(
              {'method': 'GET', 'url': data_plan['data_resources'][data_report['data_index']]['url']},
              sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and data_relay['data_response_sha256'] == [data_report['response_sha256']]
          and len(data_relay['asset_response_sha256']) == len(data_plan['assets'])
          and data_relay['request_attempts'] >= 1 + len(data_plan['assets']) + len(data_plan['data_resources']),
          'Read-only JSON relay report binds exact synthetic data bundle')
    route_evidence = read_json('examples/remote_route_evidence.json')['report']
    navigation_graph = read_json('examples/remote_navigation_graph.json')['report']
    stable_indices = {page['route_index'] for page in navigation_graph['stable_pages']}
    check(len(stable_indices) == len(navigation_graph['stable_pages']) and
          stable_indices.isdisjoint(navigation_graph['changed_route_indices']) and
          set(navigation_graph['incomplete_link_sample_indices']) <= stable_indices and
          all(edge['source_route_index'] in stable_indices and
              edge['target_route_index'] in stable_indices for edge in navigation_graph['observed_edges']),
          'Synthetic observed navigation graph remains internally consistent')
    check(route_evidence['route_count'] == len(route_evidence['routes']) == 2 and
          [row['route_index'] for row in route_evidence['routes']] == [0, 1],
          'Remote route evidence has exact ordered synthetic routes')
    check(route_evidence['readback_verified'] is True and route_evidence['account_verified'] is False
          and route_evidence['site_outcome_verified'] is False,
          'Remote route evidence distinguishes readback from site acceptance')
    for row, url in zip(route_evidence['routes'],
                        ('https://fixture.invalid/entry', 'https://fixture.invalid/details')):
        check(hashlib.sha256(json.dumps({'url': url}, sort_keys=True,
              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
              == row['url_sha256'], 'Remote route evidence uses only synthetic URL hash')
        fingerprint = {'version': route_evidence['fingerprint_version'],
                       'url_sha256': row['url_sha256'],
                       'title_sha256': row['title_sha256'],
                       'heading_sha256': row['heading_sha256']}
        check(hashlib.sha256(json.dumps(fingerprint, sort_keys=True,
              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
              == row['page_fingerprint_sha256'],
              'Remote route evidence fingerprint binds metadata')
    json_review = read_json('examples/remote_readonly_data_knowledge_review.json')
    check(hashlib.sha256(json.dumps(json_review['record'], sort_keys=True,
                                  separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          == json_review['receipt']['review_sha256']
          == json_review['recheck']['review_sha256'],
          'JSON metadata review fixture binds record and receipt')
    json_ui_preview = read_json('examples/remote_readonly_data_knowledge_ui_preview.json')['preview']
    check(hashlib.sha256(json.dumps(json_ui_preview['candidate'], sort_keys=True,
                                  separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          == json_ui_preview['candidate_sha256'],
          'JSON metadata UI candidate fixture binds exact canonical digest')
    json_change = read_json('examples/remote_readonly_data_change.json')['report']
    check(json_change['status'] == 'changed_profile_bound'
          and json_change['data_changed_indices'] == [0]
          and json_change['asset_changed_indices'] == []
          and json_change['page_identity_changed'] is False
          and json_change['entry_response_changed'] is False,
          'JSON bundle change fixture isolates content-only data drift')
    for marker, fingerprint in (('e', json_change['before_fingerprint_sha256']),
                                ('f', json_change['after_fingerprint_sha256'])):
        transport = {'title_sha256': 'a' * 64, 'heading_sha256': 'b' * 64,
                     'responses': {'entry_response_sha256': 'c' * 64,
                                   'asset_response_sha256': ['d' * 64],
                                   'data_response_sha256': [marker * 64]}}
        check(hashlib.sha256(json.dumps({'version': json_change['fingerprint_version'],
                                        **transport}, sort_keys=True,
                                       separators=(',', ':'), allow_nan=False).encode()).hexdigest()
              == fingerprint, 'JSON bundle transport fingerprint binds metadata')
    route_change = read_json('examples/remote_route_change.json')['report']
    check(route_change['route_count'] == len(route_change['routes']) == 2 and
          [row['route_index'] for row in route_change['routes']] == [0, 1] and
          [row['changed'] for row in route_change['routes']] == [False, True] and
          route_change['status'] == 'changed_profile_bound',
          'Remote route change fixture has one changed synthetic route')
    for previous, current in zip(route_evidence['routes'], route_change['routes']):
        check(previous['route_index'] == current['route_index'] and
              previous['url_sha256'] == current['url_sha256'] and
              previous['page_fingerprint_sha256'] == current['before_fingerprint_sha256'] and
              current['changed'] == (current['before_fingerprint_sha256'] !=
                                     current['after_fingerprint_sha256']),
              'Remote route change compares matching plan metadata')
        if 'link_sample_sha256' in current:
            sample = {'version': 'remote-route-link-sample-v1',
                      'planned_link_indices': previous['planned_link_indices'],
                      'unregistered_link_count': previous['unregistered_link_count']}
            check(previous['link_inventory_readback_matched'] is True
                  and previous['links_truncated'] is False
                  and current['sampled_link_inventory_changed'] is False
                  and current['planned_link_indices'] == previous['planned_link_indices']
                  and hashlib.sha256(json.dumps(sample, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                  == current['link_sample_sha256'],
                  'Stable synthetic link sample hash binds route evidence')
    page_seed = read_json('examples/remote_page_draft_seed.json')['report']
    selected_route = route_change['routes'][page_seed['route_index']]
    check(not selected_route['changed'] and
          all(page_seed[field] == route_change[field] for field in
              ('snapshot_sha256', 'profile_sha256', 'plan_sha256',
               'before_run_ref', 'after_run_ref')) and
          page_seed['url_sha256'] == selected_route['url_sha256'] and
          page_seed['page_fingerprint_sha256'] == selected_route['after_fingerprint_sha256'] and
          page_seed['semantic_page_key_verified'] is False and
          page_seed['reviewed'] is False,
          'Remote page seed fixture binds only stable synthetic route metadata')
    page_registration = read_json('examples/remote_page_draft_registration.json')['report']
    check(all(page_registration[field] == page_seed[field] for field in
              ('snapshot_sha256', 'profile_sha256', 'plan_sha256',
               'before_run_ref', 'after_run_ref', 'route_index', 'page_key')) and
          page_registration['knowledge_sha256'] == page_seed['draft_sha256'] and
          page_registration['source_bound'] is True and
          page_registration['reviewed'] is False,
          'Remote page registration fixture binds the unreviewed synthetic seed')
    json_page_seed = read_json('examples/remote_readonly_data_page_draft_seed.json')['report']
    json_page_registration = read_json(
        'examples/remote_readonly_data_page_draft_registration.json')['report']
    check(all(json_page_registration[field] == json_page_seed[field] for field in
              ('snapshot_sha256', 'profile_sha256', 'plan_sha256',
               'before_run_ref', 'after_run_ref', 'page_key')) and
          json_page_registration['knowledge_sha256'] == json_page_seed['draft_sha256'] and
          json_page_seed['transport_readback_bound'] is True and
          json_page_registration['transport_readback_bound'] is True and
          json_page_seed['semantic_page_key_verified'] is False and
          json_page_registration['reviewed'] is False,
          'JSON page registration fixture binds only the unreviewed synthetic seed')
    json_live = read_json('examples/remote_readonly_data_knowledge_live.json')
    check(all(json_live['pin'][field] == json_live['matched_event'][field] for field in
              ('review_sha256', 'page_key', 'draft_revision', 'landmark_keys')) and
          json_live['pin']['page_fingerprint_sha256'] ==
          json_live['matched_event']['current_fingerprint_sha256'] and
          json_live['stale_event']['review_sha256'] == json_live['pin']['review_sha256'] and
          json_live['stale_event']['current_fingerprint_sha256'] !=
          json_live['pin']['page_fingerprint_sha256'] and
          'page_key' not in json_live['stale_event'] and
          json_live['pin']['task_retrieval_authorized'] is False,
          'JSON live metadata fixture reveals symbolic keys only on exact match')
    route_knowledge = read_json('examples/remote_route_knowledge.json')['report']
    route_knowledge_ui = read_json('examples/remote_route_knowledge_ui_preview.json')['preview']
    check(route_knowledge_ui['candidate'] == route_knowledge and
          hashlib.sha256(json.dumps(route_knowledge, sort_keys=True,
              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          == route_knowledge_ui['candidate_sha256'],
          'UI metadata candidate preview binds canonical synthetic report')
    check(route_knowledge['route_readback_bound'] is True and
          route_knowledge['page_fingerprint_sha256'] ==
          route_change['routes'][route_knowledge['route_index']]['after_fingerprint_sha256'],
          'Remote route knowledge fixture binds the changed route readback')
    mapped_knowledge = {**route_knowledge, 'link_sample_sha256': 'a' * 64,
                        'outgoing_route_indices': [0],
                        'outgoing_knowledge_sha256': ['b' * 64]}
    check(validators['remote_route_knowledge.schema.json'].is_valid(mapped_knowledge),
          'Synthetic outgoing route candidate binds target draft checksum')
    rejected(lambda: validators['remote_route_knowledge.schema.json'].validate({
        key: value for key, value in mapped_knowledge.items()
        if key != 'outgoing_knowledge_sha256'}),
        'Reject outgoing route candidate without target draft checksum')
    rejected(lambda: validators['remote_route_knowledge.schema.json'].validate({
        **route_knowledge, 'route_readback_bound': False}),
        'Reject remote route knowledge without readback binding')
    review_fixture = read_json('examples/remote_route_knowledge_review.json')
    check(review_fixture['live_pin']['outgoing_route_indices'] == []
          and review_fixture['live_pin']['outgoing_fingerprint_sha256'] == [],
          'Synthetic empty outgoing review pin has no target claims')
    rejected(lambda: validators['remote_route_knowledge_live_pin.schema.json'].validate({
        **review_fixture['live_pin'], 'outgoing_page_keys': ['entry'],
        'link_sample_sha256': 'a' * 64, 'outgoing_route_indices': [0],
        'outgoing_fingerprint_sha256': []}),
        'Reject outgoing review pin without target fingerprint')
    review_record, review_receipt, review_reuse = (
        review_fixture['record'], review_fixture['receipt'], review_fixture['reuse'])
    check(review_fixture['synthetic'] is True
          and review_receipt['review_sha256'] == hashlib.sha256(json.dumps(
              review_record, sort_keys=True, separators=(',', ':'), allow_nan=False
          ).encode()).hexdigest()
          and review_reuse['review_sha256'] == review_receipt['review_sha256']
          and review_reuse['knowledge_sha256'] == review_record['knowledge_sha256']
          and review_reuse['profile_sha256'] == review_record['profile_sha256']
          and review_reuse['plan_sha256'] == review_record['plan_sha256']
          and review_reuse['page_fingerprint_sha256'] == review_record['page_fingerprint_sha256']
          and review_reuse['task_retrieval_authorized'] is False,
          'Synthetic remote route review and reuse stay metadata-only')
    rejected(lambda: validators['remote_route_knowledge_reuse.schema.json'].validate({
        **review_reuse, 'execution_authorized': True}),
        'Reject remote route reuse claiming execution authority')
    check(route_report['route_index'] < len(route_plan['routes']) and
          route_report['request_sha256'] == hashlib.sha256(json.dumps(
          {'method': 'GET', 'url': route_plan['routes'][route_report['route_index']]},
          sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
          'Read-only route report binds selected exact GET')
    form_fixture = read_json('examples/web_https_form_transport.json')
    multi_form = read_json('examples/web_https_form_fields.json')['configuration']
    multi_body = urlencode([(field['name'], field['value'])
                            for field in multi_form['fields']]).encode()
    check(len(multi_body) == multi_form['body_bytes']
          and hashlib.sha256(multi_body).hexdigest() == multi_form['body_sha256']
          and multi_form['fields'][-1] == {'name': 'csrf_token', 'value': 'synthetic-token'}
          and len({field['name'] for field in multi_form['fields']}) == len(multi_form['fields']),
          'Synthetic ordered multi-field body binds exact names and values')
    form_plan, form_report = form_fixture['plan'], form_fixture['report']
    check(form_report['plan_sha256'] == hashlib.sha256(json.dumps(
          form_plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and form_report['profile_sha256'] == form_plan['profile_sha256']
          and form_report['task_sha256'] == form_plan['task_sha256'],
          'Synthetic HTTPS form transport binds exact plan and task')
    check(form_plan['body_sha256'] == hashlib.sha256(b'message=hello').hexdigest()
          and form_plan['body_bytes'] == len(b'message=hello')
          and form_report['submit_request_sha256'] == hashlib.sha256(json.dumps(
              {'method': 'POST', 'url': form_plan['submit_url'],
               'body_sha256': form_plan['body_sha256']}, sort_keys=True,
              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and form_report['account_verified'] is False
          and form_report['site_outcome_verified'] is False,
          'Synthetic HTTPS form report never claims account or site outcome')
    state_fixture = read_json('examples/web_https_form_state.json')
    state_plan, state_report = state_fixture['plan'], state_fixture['report']
    check(state_fixture['synthetic'] is True
          and state_plan['form_plan_sha256'] == form_report['plan_sha256']
          and state_plan['profile_sha256'] == form_plan['profile_sha256']
          and state_plan['task_sha256'] == form_plan['task_sha256']
          and state_report['state_plan_sha256'] == hashlib.sha256(json.dumps(
              state_plan, sort_keys=True, separators=(',', ':'), allow_nan=False
          ).encode()).hexdigest()
          and state_report['state_url_sha256'] == hashlib.sha256(json.dumps(
              {'url': state_plan['state_url']}, sort_keys=True,
              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
          and state_report['receipt_response_sha256'] == form_report['receipt_response_sha256']
          and state_report['before_response_sha256'] == state_plan['expected_before_sha256']
          and state_report['after_response_sha256'] == state_plan['expected_after_sha256']
          and state_report['before_response_sha256'] != state_report['after_response_sha256']
          and state_report['account_verified'] is False
          and state_report['site_outcome_verified'] is False
          and state_report['training_ready'] is False,
          'Synthetic HTTPS state readback binds form and keeps site outcome unverified')
    rejected(lambda: validators['web_https_form_state_report.schema.json'].validate({
        **state_report, 'site_outcome_verified': True}),
        'Reject HTTPS state readback claiming site outcome')
    rejected(lambda: validators['web_https_form_state_plan.schema.json'].validate({
        **state_plan, 'marker_id': 'outcome'}),
        'Reject incomplete HTTPS state marker plan')
    rejected(lambda: validators['web_https_form_state_report.schema.json'].validate({
        **state_report, 'declared_marker_transition_observed': True}),
        'Reject incomplete HTTPS state marker report')
    learning_event = read_json('examples/learning_event.json')
    validators['learning_event.schema.json'].validate(learning_event)
    check(learning_event['metadata_only'] is True and learning_event['training_ready'] is False,
          'Learning event fixture is metadata-only and not training-ready')
    rejected(lambda: validators['learning_event.schema.json'].validate({**learning_event, 'request_json': {'secret': 'private'}}),
             'Learning event schema rejects raw request content')
    learning_event_v2 = read_json('examples/learning_event_v2.json')
    validators['learning_event_v2.schema.json'].validate(learning_event_v2)
    check(learning_event_v2['metadata_only'] is True and learning_event_v2['training_ready'] is False,
          'Learning evidence v2 fixture remains metadata-only and not training-ready')
    rejected(lambda: validators['learning_event_v2.schema.json'].validate(
        {**learning_event_v2, 'request_json': {'secret': 'private'}}),
        'Learning evidence v2 rejects raw request content')
    rejected(lambda: validators['learning_event_v2.schema.json'].validate(
        {**learning_event_v2, 'source': {**learning_event_v2['source'],
                                        'scene_observation_id': None,
                                        'downstream_verification_ids': ['verification-test']}}),
        'Learning evidence v2 rejects unbound downstream verification')
    web_profile, web_report = web_fixture['profile'], web_fixture['report']
    page_draft = read_json('examples/site_page_draft.json')['page']
    check(page_draft['profile_sha256'] == web_report['profile_sha256'], 'Site page draft binds exact profile revision')
    check((page_draft['application_key'], page_draft['tenant_key'], page_draft['account_role']) ==
          (web_profile['application_key'], web_profile['tenant_key'], web_profile['account_role']),
          'Site page draft preserves application tenant and role')
    check(page_draft['status'] == 'draft' and page_draft['source_kind'] == 'manual_draft',
          'Site page fixture does not claim observed or active knowledge')
    web_digest = hashlib.sha256(json.dumps(web_profile, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    check(web_report['profile_sha256'] == web_digest, 'Web profile report binds exact canonical content')
    check(web_report['application_key'] == web_profile['application_key'] and web_report['revision'] == web_profile['revision'],
          'Web profile report preserves application and revision')
    check(web_report['status'] == 'draft' and web_report['requested_learning_roles'] == ['system1', 'system2'],
          'Web profile records both role requests without activation')
    check(web_report['blockers'] == ['site_runtime_not_integrated', 'task_contracts_not_bound',
                                   'rights_review_not_verified', 'learning_pipeline_not_integrated'],
          'Web profile exposes runtime and learning blockers')
    for field in ('raw_screenshots', 'automatic_training', 'automatic_promotion'):
        check(web_profile['learning'][field] is False, 'Web profile learning privilege is disabled: '+field)
        invalid = {**web_profile, 'learning': {**web_profile['learning'], field: True}}
        rejected(lambda: validators['web_application_profile.schema.json'].validate(invalid),
                 'Reject web profile learning privilege: '+field)
    for field, value in (('password', 'synthetic-secret'), ('revision', 0), ('previous_sha256', '../outside'),
                         ('allowed_origins', []), ('task_keys', []), ('application_key', '../outside')):
        rejected(lambda: validators['web_application_profile.schema.json'].validate({**web_profile, field: value}),
                 'Reject invalid web profile field: '+field)
    local_state = read_json('examples/local_app.json')['state']
    for field,value in (('url','http://example.com/'),('token_name','../../private.token'),
                        ('phase','authorized'),('remote_form_plan_sha256','wrong'),
                        ('remote_form_field_name','not a field'),
                        ('remote_form_state_plan_sha256','wrong'),
                        ('remote_form_cookie_sha256','wrong'),('raw_token','secret')):
        rejected(lambda:validators['local_app_state.schema.json'].validate({**local_state,field:value}),
                 'Reject unsafe local pilot state: '+field)
    check((ROOT/'scripts/aos-v1').is_file(), 'Local pilot launcher is included')
    bound_loss = read_json('examples/dataset_bound_loss.json')['report']
    forward = bound_loss['forward_report']
    check(forward['input_sha256']==bound_loss['input_sha256'] and forward['manifest_sha256']==bound_loss['deployment_manifest_sha256'],
          'Dataset loss exact input and deployment binding')
    expected_metrics = [(split,layout,count) for split in ('train','validation','test')
                        if (count:=bound_loss['split_counts'][split]) for layout in ('state_first','schema_first')]
    check([(item['split'],item['layout'],item['decisions']) for item in forward['split_metrics']]==expected_metrics,
          'Dataset loss split layout and decision counts')
    check(forward['peak_vram_reserved_bytes']>=forward['peak_vram_allocated_bytes'] and all(
          math.isfinite(item['mean_nll']) and 0<=item['correct_choices']<=item['decisions'] for item in forward['split_metrics']),
          'Dataset loss bounded finite metrics')
    for field,value in (('gradient_run',True),('optimizer_run',True),('parameters_unchanged',False),('training_ready',True),('raw_prediction','private')):
        rejected(lambda:validators['dataset_bound_loss.schema.json'].validate({**bound_loss,'forward_report':{**forward,field:value}}),
                 'Reject unsafe dataset loss worker report: '+field)
    benchmark = read_json('examples/benchmark.json')['report']
    check(benchmark['independent_held_out_tasks']==0 and len(benchmark['samples'])==2*benchmark['repeats'],
          'Fixture benchmark repetitions are not independent heldout tasks')
    check([(item['case'],item['repeat']) for item in benchmark['samples']]==[
          (case,repeat) for repeat in range(1,benchmark['repeats']+1) for case in ('file-roundtrip','recovery-path')],
          'Fixture benchmark exact ordered suite')
    check(all(item['actions']==(3 if item['case']=='recovery-path' else 2) and item['system2_model_calls']==0
              for item in benchmark['samples']), 'Fixture benchmark action and model counters')
    rejected(lambda:validators['benchmark.schema.json'].validate({**benchmark,'coverage_gaps':[]}), 'Reject hidden benchmark coverage gaps')
    compound_execution = read_json('examples/compound_execution.json')
    compound_request = compound_execution['request']
    check(compound_request['input_sha256']==hashlib.sha256(json.dumps({'original_goal':compound_request['goal']},
          sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
          'Compound execution preserves original goal hash')
    check(compound_execution['sequence_request']['plan']['requires_action_approval'] is True,
          'Compound execution never bypasses per-action approval')
    session_fixture = read_json('examples/recovery_session.json')
    session_binding, session_report = session_fixture['binding'], session_fixture['report']
    session_digest = hashlib.sha256(json.dumps(session_binding,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    check(session_report['binding_sha256']==session_digest, 'Session fixture binds exact persistent birth event')
    check(session_report['lifecycle']['image_id']==session_binding['birth']['image_id'], 'Session fixture preserves lifecycle image pin')
    nested = copy.deepcopy(session_report)
    nested['lifecycle']['cleanup_authorized'] = True
    rejected(lambda:validators['recovery_session.schema.json'].validate(nested), 'Reject nested lifecycle cleanup authority')
    preflight = read_json('examples/dataset_preflight.json')['report']
    check(sum(preflight['split_counts'].values())==preflight['tokenizer_report']['examples'], 'Dataset tokenizer split counts match actual probe size')
    check(preflight['tokenizer_report']['variants']==40*preflight['tokenizer_report']['examples'], 'Dataset tokenizer variant counts')
    for field,value in (('weights_loaded',True),('training_ready',True),('raw_input','private')):
        nested = copy.deepcopy(preflight)
        nested['tokenizer_report'][field] = value
        rejected(lambda:validators['dataset_preflight.schema.json'].validate(nested), 'Reject nested dataset preflight claim: '+field)
    for change in ({'split_counts':{'train':-1,'validation':0,'test':0}}, {'input_sha256':'raw'}, {'runner_sha256':'unbound'}):
        rejected(lambda:validators['dataset_preflight.schema.json'].validate({**preflight,**change}), 'Reject dataset preflight unbound input')
    compound_fixture = read_json('examples/goal_plan.json')
    check(compound_fixture['synthetic'] is True, 'Compound goal fixtures are synthetic')
    for entry in compound_fixture['cases']:
        preview = entry['preview']
        validators['goal_plan.schema.json'].validate(preview)
        expected = hashlib.sha256(json.dumps({key:value for key,value in preview.items() if key!='composition_sha256'},
                                             sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        check(preview['composition_sha256']==expected, 'Compound preview exact content digest')
        expected_input = hashlib.sha256(json.dumps({'original_goal':entry['goal']},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        check(preview['input_sha256']==expected_input, 'Compound preview preserves original goal digest')
        if preview['status']=='recognized':
            kinds = [task['plan']['task_kind'] for task in preview['tasks']]
            check(1<=len(kinds)<=3 and len(kinds)==len(set(kinds)), 'Compound preview bounded distinct task kinds')
            check(preview['sequence'] is None if len(kinds)==1 else preview['sequence']['kinds']==kinds, 'Compound preview preserves exact sequence')
        else:
            check(preview['tasks']==[] and preview['sequence'] is None, 'Unsupported compound preview has no partial plan')
        for field,value in (('execution_authorized',True),('live_state_verified',True),('automatic_replay_allowed',True),('requires_action_approval',False)):
            rejected(lambda:validators['goal_plan.schema.json'].validate({**preview,field:value}), 'Reject compound preview authority: '+field)
    lifecycle_fixture = read_json('examples/lifecycle.json')
    check(lifecycle_fixture['synthetic'] is True, 'Lifecycle fixture is explicitly synthetic')
    lifecycle_event = lifecycle_fixture['event']
    lifecycle_report = lifecycle_fixture['report']
    for name, payload in (('lifecycle_event',lifecycle_event), ('lifecycle_inspection',lifecycle_report)):
        validators[name+'.schema.json'].validate(payload)
        CHECKS.append('Lifecycle fixture matches canonical schema: '+name)
        rejected(lambda:validators[name+'.schema.json'].validate({**payload,'raw_content':'private'}), 'Reject raw lifecycle content: '+name)
    validate_lifecycle_event(lifecycle_event)
    CHECKS.append('Lifecycle fixture preserves event sequence and owner binding')
    birth_hash = hashlib.sha256(json.dumps(lifecycle_event['birth'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    check(lifecycle_report['birth_ref']==birth_hash, 'Lifecycle inspection binds exact birth')
    check(all(lifecycle_report[field]==lifecycle_event['birth'][field] for field in ('image_id','source_sha256')), 'Lifecycle fixture preserves image and source pins')
    check(lifecycle_report['recorded_stage']==lifecycle_event['stage'], 'Lifecycle report preserves durable stage')
    for field in ('execution_authorized','resume_authorized','cleanup_authorized','automatic_replay_allowed',
                  'orphan_confirmed','model_deployment_verified'):
        rejected(lambda:validators['lifecycle_inspection.schema.json'].validate({**lifecycle_report,field:True}), 'Reject lifecycle inspection authority: '+field)
    for change in ({'image_id':'latest'},{'journal_sha256':'unbound'},{'birth_ref':'raw-runtime'},
                   {'owner_observation':'dead'},{'container_observation':'safe_to_delete'},
                   {'workspace_busy':'false'},{'process':lifecycle_event['birth']['process']},
                   {'raw_environment':{}},{'workspace_path':'/private'}):
        rejected(lambda:validators['lifecycle_inspection.schema.json'].validate({**lifecycle_report,**change}), 'Reject unsafe lifecycle inspection report')
    for change in ({'sequence':True},{'sequence':4},{'stage':'adopted'},{'container_id':'short-id'},
                   {'previous_sha256':'unbound'}):
        rejected(lambda:validators['lifecycle_event.schema.json'].validate({**lifecycle_event,**change}), 'Reject invalid lifecycle event shape')
    for change in ({'runtime_id':'desktop-unknown'},{'container_name':'foreign'},{'image_id':'latest'},
                   {'source_sha256':None},{'deployment_id':'unverified'}):
        changed = {**lifecycle_event,'birth':{**lifecycle_event['birth'],**change}}
        rejected(lambda:validators['lifecycle_event.schema.json'].validate(changed), 'Reject unpinned lifecycle birth')
    for change in ({'boot_id':'unknown'},{'pid':0},{'pid':2147483648},{'pid':True},
                   {'start_ticks':0},{'pid_namespace':0},{'uid':0},{'command_line':'private'}):
        changed = copy.deepcopy(lifecycle_event)
        changed['birth']['process'].update(change)
        rejected(lambda:validators['lifecycle_event.schema.json'].validate(changed), 'Reject invalid lifecycle process identity')
    for change in ({'sequence':1},{'stage':'created'},{'previous_sha256':'a'*64},{'container_id':'b'*64}):
        rejected(lambda:validate_lifecycle_event({**lifecycle_event,**change}), 'Reject lifecycle intent binding mismatch')
    mismatched_owner = copy.deepcopy(lifecycle_event)
    mismatched_owner['birth']['process']['uid'] += 1
    rejected(lambda:validate_lifecycle_event(mismatched_owner), 'Reject lifecycle workspace/process owner mismatch')
    for stage, sequence in (('created',1),('started',2),('removed',2),('removed',3)):
        event = {**lifecycle_event,'stage':stage,'sequence':sequence,'previous_sha256':'a'*64,'container_id':'b'*64}
        validators['lifecycle_event.schema.json'].validate(event)
        validate_lifecycle_event(event)
        CHECKS.append('Lifecycle bounded event shape: '+stage+'/'+str(sequence))
        for change in ({'previous_sha256':None},{'container_id':None},{'sequence':0}):
            rejected(lambda:validate_lifecycle_event({**event,**change}), 'Reject missing lifecycle durable binding: '+stage)
    runtime_fixture = read_json('examples/recovery_runtime.json')
    check(runtime_fixture['synthetic'] is True, 'Runtime inspection fixture is explicitly synthetic')
    runtime_report = runtime_fixture['report']
    validators['recovery_runtime.schema.json'].validate(runtime_report)
    for field in ('execution_authorized','resume_authorized','cleanup_authorized','automatic_replay_allowed',
                  'orphan_confirmed','process_liveness_verified'):
        rejected(lambda:validators['recovery_runtime.schema.json'].validate({**runtime_report,field:True}), 'Reject runtime inspection authority: '+field)
    for change in ({'image_id':'latest'},{'container_ref':'raw-container'},{'raw_environment':{}},
                   {'disposition':'safe_to_delete'},{'unresolved_actions':-1}):
        rejected(lambda:validators['recovery_runtime.schema.json'].validate({**runtime_report,**change}), 'Reject unsafe runtime inspection report')
    finalization_fixture = read_json('examples/recovery_finalize.json')
    check(finalization_fixture['synthetic'] is True, 'Finalization fixture is explicitly synthetic')
    finalization = finalization_fixture['receipt']
    finalization_request = finalization['request']
    finalization_report = finalization_request['source']
    for name, payload in (('recovery_finalize',finalization),('recovery_finalize_request',finalization_request),
                          ('recovery_finalize_report',finalization_report)):
        validators[name+'.schema.json'].validate(payload)
        for field in ('execution_authorized','resume_authorized','automatic_replay_allowed'):
            rejected(lambda:validators[name+'.schema.json'].validate({**payload,field:True}), 'Reject finalization authority: '+name+'/'+field)
        rejected(lambda:validators[name+'.schema.json'].validate({**payload,'raw_content':'private'}), 'Reject raw finalization content: '+name)
    finalization_hash = hashlib.sha256(json.dumps(finalization_request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    check(finalization['request_sha256']==finalization_hash, 'Finalization receipt binds exact request')
    check(finalization_report['disposition']=='ready_to_finalize' and finalization_report['read_actions']==2
          and finalization_report['verification_ref'] is not None and finalization_report['evidence_sha256'] is not None,
          'Finalization fixture requires two reads and complete independent evidence')
    for change in ({'read_actions':3},{'disposition':'resume_allowed'},{'evidence_sha256':'not-a-hash'}):
        rejected(lambda:validators['recovery_finalize_report.schema.json'].validate({**finalization_report,**change}), 'Reject unsafe finalization report')
    for change in ({'actor':'model'},{'run_success_declared':False},{'finalized_state_sha256':None}):
        rejected(lambda:validators['recovery_finalize.schema.json'].validate({**finalization,**change}), 'Reject invalid finalization receipt')
    rejected(lambda:validators['recovery_finalize_request.schema.json'].validate({**finalization_request,'expires_at':0}), 'Reject expired finalization request shape')
    verification_fixture = read_json('examples/recovery_verify.json')
    check(verification_fixture['synthetic'] is True, 'Verification continuation fixture is explicitly synthetic')
    verification_admission = verification_fixture['admission']
    validators['recovery_verify.schema.json'].validate(verification_admission)
    for change in ({'write_authorized':True},{'automatic_replay_allowed':True},{'maximum_read_actions':3},
                   {'allowed_tool':'filesystem.write'},{'requires_action_approval':False},{'scope':'/'},
                   {'reconciliation_sha256':None}):
        rejected(lambda:validators['recovery_verify.schema.json'].validate({**verification_admission,**change}), 'Reject expanded verification-only admission')
    reconciliation_fixture = read_json('examples/write_reconciliation.json')
    check(reconciliation_fixture['synthetic'] is True, 'Write reconciliation fixture is explicitly synthetic')
    reconciliation = reconciliation_fixture['receipt']
    validators['write_reconciliation.schema.json'].validate(reconciliation)
    request = reconciliation['request']
    validators['write_reconciliation_request.schema.json'].validate(request)
    request_hash = hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    check(reconciliation['request_sha256']==request_hash, 'Reconciliation audit binds exact request')
    for field in ('execution_authorized','resume_authorized','automatic_replay_allowed'):
        rejected(lambda:validators['write_reconciliation.schema.json'].validate({**reconciliation,field:True}), 'Reject reconciliation authority: '+field)
        rejected(lambda:validators['write_reconciliation_request.schema.json'].validate({**request,field:True}), 'Reject reconciliation request authority: '+field)
    for change in ({'previous_status':'running'},{'recorded_status':'succeeded'},{'receipt_sha256':None},{'scope':'/'},{'expires_at':0}):
        rejected(lambda:validators['write_reconciliation_request.schema.json'].validate({**request,**change}), 'Reject unsafe reconciliation request')
    for change in ({'run_success_declared':True},{'actor':'model'},{'raw_content':'private'}):
        rejected(lambda:validators['write_reconciliation.schema.json'].validate({**reconciliation,**change}), 'Reject expanded reconciliation audit')
    write_fixture = read_json('examples/write_receipt.json')
    check(write_fixture['synthetic'] is True, 'Write receipt fixture is explicitly synthetic')
    validators['write_receipt.schema.json'].validate(write_fixture['receipt'])
    validators['write_effect.schema.json'].validate(write_fixture['report'])
    check(write_fixture['receipt']['action_ref']==write_fixture['report']['action_ref'], 'Write report binds action reference')
    for field in ('execution_authorized','resume_authorized','automatic_replay_allowed','uncertain_effect_resolved'):
        rejected(lambda:validators['write_effect.schema.json'].validate({**write_fixture['report'],field:True}), 'Reject write inspection authority: '+field)
    for change in ({'content_sha256':'a'*64},{'links':2},{'size':29},{'raw_content':'private'}):
        rejected(lambda:validators['write_receipt.schema.json'].validate({**write_fixture['receipt'],'file':{**write_fixture['receipt']['file'],**change}}), 'Reject unbounded write witness')
    resume_fixture = read_json('examples/recovery_resume.json')
    check(resume_fixture['synthetic'] is True, 'Resume fixture is explicitly synthetic')
    validators['recovery_resume.schema.json'].validate(resume_fixture['admission'])
    validators['recovery_resume_approval.schema.json'].validate(resume_fixture['approval'])
    check(resume_fixture['admission']['admission_id']==resume_fixture['approval']['admission_id'], 'Resume approval binds admission')
    check(resume_fixture['admission']['fresh_lease_ref']==resume_fixture['approval']['fresh_lease_ref'], 'Resume approval binds fresh lease')
    for change in ({'automatic_replay_allowed':True},{'previously_recorded_actions':1},{'requires_action_approval':False},{'scope':'/'}):
        rejected(lambda:validators['recovery_resume.schema.json'].validate({**resume_fixture['admission'],**change}), 'Reject unsafe resume admission')
    for change in ({'decision':'automatic'},{'action_sha256':'old-approval'},{'raw_content':'private'}):
        rejected(lambda:validators['recovery_resume_approval.schema.json'].validate({**resume_fixture['approval'],**change}), 'Reject unbound resume approval')
    checkpoint_fixture = read_json('examples/recovery_checkpoint.json')
    check(checkpoint_fixture['synthetic'] is True, 'Checkpoint fixture is explicitly synthetic')
    validators['recovery_checkpoint.schema.json'].validate(checkpoint_fixture['report'])
    validators['workspace_identity.schema.json'].validate(checkpoint_fixture['workspace_identity'])
    for field in ('execution_authorized','resume_authorized','automatic_replay_allowed','action_causality_verified',
                  'uncertain_effect_resolved','live_deployment_verified'):
        rejected(lambda:validators['recovery_checkpoint.schema.json'].validate({**checkpoint_fixture['report'],field:True}), 'Reject checkpoint authority claim: '+field)
    rejected(lambda:validators['recovery_checkpoint.schema.json'].validate({**checkpoint_fixture['report'],'raw_content':'private'}), 'Reject raw checkpoint content')
    rejected(lambda:validators['workspace_identity.schema.json'].validate({**checkpoint_fixture['workspace_identity'],'inode':True}), 'Reject non-integer workspace identity')
    sequence_fixture = read_json('examples/task_sequence.json')
    check(sequence_fixture['synthetic'] is True, 'Sequence fixture is explicitly synthetic')
    validators['sequence_start.schema.json'].validate(sequence_fixture['request'])
    validators['sequence_status.schema.json'].validate(sequence_fixture['status'])
    rejected(lambda:validators['sequence_start.schema.json'].validate({**sequence_fixture['request'], 'plan':{'kinds':['hello','hello']}}), 'Reject repeated sequence steps')
    for change in ({'scope':'/'}, {'resume':True}, {'generation':True}):
        rejected(lambda:validators['sequence_start.schema.json'].validate({**sequence_fixture['request'], **change}), 'Reject sequence admission expansion')
    for change in ({'automatic_replay_allowed':True}, {'status':'resumed'}, {'completed':4}):
        rejected(lambda:validators['sequence_status.schema.json'].validate({**sequence_fixture['status'], **change}), 'Reject sequence progress expansion')
    plan_fixture = read_json('examples/task_plan.json')
    check(plan_fixture['synthetic'] is True, 'Fixed plan fixture is explicitly synthetic')
    for entry in plan_fixture['cases']:
        report = entry['preview']
        validators['task_plan.schema.json'].validate(report)
        check(report['execution_authorized'] is False, 'Fixed plan grants no execution authority')
        if report['plan']:
            plan = report['plan']
            check([step['order'] for step in plan['steps']] == list(range(1, len(plan['steps'])+1)), 'Fixed plan step order')
            check(plan['scope']==report['intent']['scope'] and plan['task_kind']==report['intent']['task_kind'], 'Fixed plan intent binding')
            encoded = json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()
            check(hashlib.sha256(encoded).hexdigest()==report['plan_sha256'], 'Fixed plan digest binding')
            for field in ('execution_authorized', 'live_state_verified'):
                rejected(lambda:validators['task_plan.schema.json'].validate({**report, 'plan':{**plan, field:True}}), 'Reject fixed plan authority: '+field)
        else:
            check(report['plan_sha256'] is None and report['intent']['status']!='recognized', 'Unsupported intent has no plan')
        for change in ({'execution_authorized':True}, {'requires_action_approval':False}, {'action':{}}):
            rejected(lambda:validators['task_plan.schema.json'].validate({**report, **change}), 'Reject expanded fixed plan report')
    catalog = {r['id']:r for r in read_json('examples/evidence_catalog.json')['evidence']}
    recovery_fixture = read_json('examples/recovery_inventory.json')
    recovery_report = recovery_fixture['report']
    validators['recovery_inventory.schema.json'].validate(recovery_report)
    check(recovery_fixture['synthetic'] is True, 'Recovery inventory fixture is explicitly synthetic')
    check(recovery_report['totals']['jobs']==len(recovery_report['jobs']), 'Recovery fixture job count')
    for field in ('execution_authorized','resume_authorized','live_state_verified','automatic_replay_allowed'):
        rejected(lambda:validators['recovery_inventory.schema.json'].validate({**recovery_report,field:True}), 'Reject recovery authority claim: '+field)
    rejected(lambda:validators['recovery_inventory.schema.json'].validate({**recovery_report,'raw_state':'secret'}), 'Reject recovery raw content')
    backup_fixture = read_json('examples/recovery_backup.json')
    backup_manifest = backup_fixture['manifest']
    validators['recovery_backup.schema.json'].validate(backup_manifest)
    check(backup_fixture['synthetic'] is True, 'Backup manifest fixture is synthetic, not backup evidence')
    for change in ({'execution_authorized':True}, {'resume_authorized':True}, {'database_file':'../outside'}, {'raw_state':'secret'}):
        rejected(lambda:validators['recovery_backup.schema.json'].validate({**backup_manifest,**change}), 'Reject unsafe backup manifest')
    intent_fixture = read_json('examples/task_intent.json')
    check(intent_fixture['synthetic'] is True, 'Goal preview fixture is explicitly synthetic')
    for entry in intent_fixture['cases']:
        validators['task_intent.schema.json'].validate(entry['preview'])
        check(entry['preview']['execution_authorized'] is False, 'Goal preview grants no execution authority')
        for change in ({'execution_authorized':True}, {'requires_action_approval':False}, {'shell':'sh'}):
            rejected(lambda:validators['task_intent.schema.json'].validate({**entry['preview'], **change}), 'Reject goal preview authority expansion')
    readiness_fixture = read_json('examples/dataset_readiness.json')
    readiness_report = readiness_fixture['report']
    validators['dataset_readiness.schema.json'].validate(readiness_report)
    check(readiness_fixture['synthetic'] is True, 'Readiness fixture is explicitly synthetic')
    check(readiness_report['records']==sum(sum(counts.values()) for counts in readiness_report['split_counts'].values()), 'Readiness fixture split counts')
    for field,value in [('training_ready',True),('real_records',1),('raw_state','not allowed')]:
        rejected(lambda:validators['dataset_readiness.schema.json'].validate({**readiness_report,field:value}), 'Reject readiness authorization/raw content: '+field)
    loss_fixture = read_json('examples/dataset_loss_probe.json')
    loss_report = loss_fixture['report']
    validators['dataset_loss_probe.schema.json'].validate(loss_report)
    check(loss_fixture['synthetic'] is True, 'Loss report fixture is synthetic, not model evidence')
    for field in ('training_ready','gradient_run','optimizer_run'):
        rejected(lambda:validators['dataset_loss_probe.schema.json'].validate({**loss_report,field:True}), 'Reject expanded loss claim: '+field)
    tokenizer_fixture = read_json('examples/dataset_tokenizer_probe.json')
    tokenizer_report = tokenizer_fixture['report']
    validators['dataset_tokenizer_probe.schema.json'].validate(tokenizer_report)
    check(tokenizer_fixture['synthetic'] is True, 'Tokenizer report fixture is synthetic, not execution evidence')
    check(tokenizer_report['variants']==40*tokenizer_report['examples'], 'Tokenizer fixture variant counts')
    for field in ('training_ready','loss_verified','weights_loaded','cuda_initialized'):
        rejected(lambda:validators['dataset_tokenizer_probe.schema.json'].validate({**tokenizer_report,field:True}),
                 'Reject expanded tokenizer claim: '+field)
    reviewer_fixture = read_json('examples/dataset_reviewer_audit.json')
    reviewer_policy = read_json('examples/dataset_reviewer_policy.json')
    check(reviewer_policy['synthetic'] is True, 'Reviewer policy fixture is synthetic, not installed authority')
    validators['dataset_reviewer_policy.schema.json'].validate(reviewer_policy['policy'])
    bad_policy = copy.deepcopy(reviewer_policy['policy']); bad_policy['grants'][0]['decisions']=['upload']
    rejected(lambda:validators['dataset_reviewer_policy.schema.json'].validate(bad_policy), 'Reject expanded reviewer policy')
    journal_header = {'journal_version':'1.0','sequence':0,'previous_sha256':'0'*64,'sha256':'1'*64,
                      'kind':'header','database_binding':'2'*64,'baseline_receipts_sha256':'3'*64,'created_at':'2026-09-20T12:00:00Z'}
    validators['dataset_review_journal.schema.json'].validate(journal_header)
    CHECKS.append('Reviewer journal header contract')
    rejected(lambda:validators['dataset_review_journal.schema.json'].validate({**journal_header,'candidate':{}}), 'Reject raw journal envelope content')
    reviewer_response = {'status':'recorded','receipt_id':'synthetic-receipt','event_sha256':'0'*64,'training_ready':False}
    validators['dataset_reviewer_response.schema.json'].validate(reviewer_response)
    CHECKS.append('Reviewer result response contract')
    rejected(lambda:validators['dataset_reviewer_response.schema.json'].validate({**reviewer_response,'training_ready':True}), 'Reject reviewer training authorization')
    reviewer_event = reviewer_fixture['event']
    validators['dataset_reviewer_audit.schema.json'].validate(reviewer_event)
    check(reviewer_fixture['synthetic'] is True, 'Reviewer audit fixture is explicitly synthetic')
    check(reviewer_event['training_ready'] is False, 'Reviewer authentication does not authorize training')
    for field,value in [('training_ready',True),('challenge','synthetic-secret'),('reason','raw exception')]:
        rejected(lambda:validators['dataset_reviewer_audit.schema.json'].validate({**reviewer_event,field:value}),
                 'Reject unsafe reviewer audit field: '+field)
    reviewer_request = {'operation':'prepare_accept','run_id':'synthetic-run','label_id':'synthetic-label',
                        'source_sha256':'0'*64,'candidate':rows['system1_choice'][0],
                        'attestations':['content_reviewed','usage_rights_reviewed','redaction_reviewed']}
    validators['dataset_reviewer_request.schema.json'].validate(reviewer_request)
    CHECKS.append('Reviewer prepare request has explicit attestations')
    for field,value in [('uid',0),('purpose','remote_upload'),('attestations',[])]:
        rejected(lambda:validators['dataset_reviewer_request.schema.json'].validate({**reviewer_request,field:value}),
                 'Reject client-supplied reviewer authority: '+field)
    preview_fixture = read_json('examples/dataset_preview.json')
    preview = preview_fixture['report']
    validators['dataset_preview.schema.json'].validate(preview)
    check(preview_fixture['synthetic'] is True, 'Dataset preview fixture is explicitly synthetic')
    check(preview['mode']=='hash_only' and preview['training_ready'] is False and preview['metadata_claims_verified'] is False,
          'Dataset preview exposes no content or authorization')
    check(preview['accepted_choice_labels']==0 and preview['projection_sha256'] is None
          and 'no_accepted_choice_labels' in preview['blockers'], 'Dataset preview does not invent labels')
    for field,value in [('training_ready',True),('metadata_claims_verified',True),('raw_state','synthetic private content')]:
        bad_preview = {**preview, field:value}
        rejected(lambda:validators['dataset_preview.schema.json'].validate(bad_preview), 'Reject expanded preview field: '+field)
    audit_fixture = read_json('examples/dataset_audit.json')
    receipt_fixture = read_json('examples/dataset_review_receipts.json')
    check(receipt_fixture['synthetic'] is True, 'Dataset review receipt fixtures are synthetic')
    for receipt in receipt_fixture['receipts']:
        validators['dataset_review_receipt.schema.json'].validate(receipt)
        check(receipt['purpose']=='offline_training_text', 'Dataset review scope is text-only and offline')
    bad_receipt = copy.deepcopy(receipt_fixture['receipts'][0]); bad_receipt['purpose']='remote_upload'
    rejected(lambda:validators['dataset_review_receipt.schema.json'].validate(bad_receipt), 'Reject expanded dataset review scope')
    audit = audit_fixture['report']
    validators['dataset_audit.schema.json'].validate(audit)
    check(audit_fixture['synthetic'] is True, 'Dataset audit fixture is explicitly synthetic')
    check(audit['run_count']==len(audit['runs']), 'Dataset audit run count matches inventory')
    check(audit['opted_in_runs']==sum(row['training_eligible_flag'] for row in audit['runs']), 'Dataset audit flags are inventory only')
    check(audit['ready_runs']==0 and audit['training_ready'] is False, 'Dataset audit cannot authorize training')
    for reason,count in audit['blocker_counts'].items():
        check(count==sum(reason in row['blockers'] for row in audit['runs']), 'Dataset audit blocker count: '+reason)
    bad_audit = copy.deepcopy(audit); bad_audit['training_ready']=True
    rejected(lambda:validators['dataset_audit.schema.json'].validate(bad_audit), 'Reject training-ready audit report')
    bad_audit = copy.deepcopy(audit); bad_audit['runs'][0]['raw_goal']='synthetic private content'
    rejected(lambda:validators['dataset_audit.schema.json'].validate(bad_audit), 'Reject raw content in audit report')
    reviews = read_json('examples/dataset_review.json')
    validators['dataset_review.schema.json'].validate(reviews)
    review_by_id = {r['sample_id']:r for r in reviews['reviews']}
    check(len(review_by_id)==len(reviews['reviews']), 'Unique synthetic dataset review ids')
    check(set(review_by_id)=={r['sample_id'] for group in rows.values() for r in group}, 'Review covers canonical fixtures exactly')
    for group in rows.values():
        for r in group:
            expected = hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            check(review_by_id[r['sample_id']]['record_sha256']==expected, 'Reviewed fixture content hash: '+r['sample_id'])
            p = r['provenance']
            e = catalog[p['verification_ref']]
            check(e['run_id']==p['run_id'] and e['step_id']==p['step_id'], 'Label evidence linkage: '+r['sample_id'])
    exported = read_json('examples/trajectory_export.json')
    validators['trajectory_export.schema.json'].validate(exported)
    validate_export(exported)
    CHECKS.append('Export schema and cross-reference semantics')
    registry = read_json('examples/registry.json')
    validators['registry.schema.json'].validate(registry)
    validate_registry(registry)
    CHECKS.append('Registry schema, hashes and disabled unresolved models')
    recovery = read_json('examples/supervisor_plan.json')
    validators['supervisor_plan.schema.json'].validate(recovery['plan'])
    check(recovery['synthetic'] is True, 'Recovery plan fixture is synthetic')
    browser = read_json('examples/browser_observation.json')
    validators['browser_observation.schema.json'].validate(browser['observation'])
    check(browser['synthetic'] is True, 'Browser DOM fixture is synthetic')
    vision = read_json('examples/vision_scene.json')
    progress = read_json('examples/task_progress.json')
    validators['task_progress.schema.json'].validate(progress['progress'])
    check(progress['synthetic'] is True, 'Task progress example is explicitly synthetic')
    for change in ({'phase': 'GUESSING'}, {'elapsed_ms': -1}, {'model_calls': [
            {'role': 'system1', 'status': 'running', 'latency_ms': 1}]},
            {'model_call_totals': {'system1': {'ok': -1, 'total': 1},
                                   'system2': {'ok': 0, 'total': 0}}}):
        rejected(lambda: validators['task_progress.schema.json'].validate({**progress['progress'], **change}),
                 'Reject unrecorded or malformed task progress')
    approval = read_json('examples/desktop_approval.json')
    validators['desktop_approval.schema.json'].validate(approval['approval'])
    check(approval['synthetic'] is True, 'Desktop approval fixture is synthetic')
    action_hash = hashlib.sha256(json.dumps(approval['approval']['action'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    check(action_hash == approval['approval']['action_sha256'], 'Approval digest binds entire action envelope')
    route_approval = read_json('examples/desktop_remote_route_approval.json')
    validators['desktop_approval.schema.json'].validate(route_approval['approval'])
    check(route_approval['synthetic'] is True and
          route_approval['approval']['action']['tool'] == 'browser.remote.route' and
          type(route_approval['approval']['action']['arguments']['route_index']) is int,
          'Read-only route approval fixture is synthetic and index-bound')
    check(hashlib.sha256(json.dumps(route_approval['approval']['action'], sort_keys=True,
                                separators=(',', ':')).encode()).hexdigest()
          == route_approval['approval']['action_sha256'],
          'Read-only route approval digest binds full action')
    form_approval = read_json('examples/desktop_remote_form_approval.json')
    validators['desktop_approval.schema.json'].validate(form_approval['approval'])
    check(form_approval['synthetic'] is True
          and form_approval['approval']['action']['tool'] == 'browser.form.submit'
          and form_approval['approval']['action']['arguments']['stage'] == 2
          and form_approval['approval']['action']['arguments']['url'] == 'https://app.example.invalid/submit'
          and form_approval['approval']['action']['arguments']['field_name'] == 'message'
          and form_approval['approval']['action']['arguments']['body_sha256'] == hashlib.sha256(b'message=hello').hexdigest()
          and not any(key in form_approval['approval']['action']['arguments']
                      for key in ('value', 'body', 'token')),
          'HTTPS form approval fixture is synthetic and body-free')
    check(hashlib.sha256(json.dumps(form_approval['approval']['action'], sort_keys=True,
                                separators=(',', ':')).encode()).hexdigest()
          == form_approval['approval']['action_sha256'],
          'HTTPS form approval digest binds full action')
    bad_approval = copy.deepcopy(approval['approval']); bad_approval['action_sha256'] = 'not-a-digest'
    rejected(lambda: validators['desktop_approval.schema.json'].validate(bad_approval), 'Reject malformed approval digest')
    paired = read_json('examples/desktop_browser_approvals.json')
    check(paired['synthetic'] is True, 'Browser approval pair is synthetic')
    for approval_item in paired['approvals']:
        validators['desktop_approval.schema.json'].validate(approval_item)
        checksum = hashlib.sha256(json.dumps(approval_item['action'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        check(checksum == approval_item['action_sha256'], 'Browser approval binds each full action')
    check([item['action']['tool'] for item in paired['approvals']] == ['browser.fill','browser.submit'], 'Browser fill and submit require distinct approvals')
    validators['vision_scene.schema.json'].validate(vision['scene'])
    check(vision['synthetic'] is True, 'Visual scene fixture is synthetic')
    bad_vision = copy.deepcopy(vision['scene'])
    bad_vision['elements'][0]['bbox']['x'] = -1
    rejected(lambda: validators['vision_scene.schema.json'].validate(bad_vision), 'Reject negative visual coordinates')
    browser_export = read_json('examples/browser_export.json')
    validators['trajectory_export.schema.json'].validate(browser_export)
    validate_export(browser_export)
    check(browser_export['synthetic'] is True, 'Browser export fixture with pre-action observation is synthetic')
    bad_browser = copy.deepcopy(browser_export)
    bad_browser['steps'][0]['verifications'][0]['actual']['value'] = 'mismatch'
    rejected(lambda: validate_export(bad_browser), 'Reject passing DOM verification with unequal outcomes')
    bad_visual_export = copy.deepcopy(browser_export)
    bad_visual_export['steps'][0]['verifications'][0].update(
        method='independent_canvas_equals', expected={'selected': 'SAVE', 'clicks': 1}, actual={'selected': 'CANCEL', 'clicks': 1})
    rejected(lambda: validate_export(bad_visual_export), 'Reject passing canvas verification with wrong target')
    check(set(recovery['plan']['evidence_refs']) == {item['id'] for item in recovery['evidence']}, 'Recovery evidence references')
    check([step['action'] for step in recovery['plan']['revised_plan']] == ['observe_workspace','create_authorized_file','verify_exact_content'], 'Recovery plan bounded order')

    bad = copy.deepcopy(rows['system1_choice'][0]); bad['target']['correct_option']='unknown'
    rejected(lambda:validate_choice(bad),'Reject gold option outside candidate set')
    bad = copy.deepcopy(rows['system1_choice'][0]); bad['prediction']['probabilities']['write_file']=0.2
    rejected(lambda:validate_choice(bad),'Reject probability sum mismatch')
    bad = copy.deepcopy(rows['system1_choice'][0]); bad['options'][1]['id']=bad['options'][0]['id']
    rejected(lambda:validate_choice(bad),'Reject duplicate option ids')
    bad = copy.deepcopy(rows['system1_choice'][0]); bad['created_at']='not-a-date'
    rejected(lambda:validators['system1_choice.schema.json'].validate(bad),'Reject malformed timestamp')
    bad = copy.deepcopy(exported); bad['steps'][0]['verifications'][0]['action_id']='missing-action'
    rejected(lambda:validate_export(bad),'Reject dangling export verification')
    bad = copy.deepcopy(exported); bad['steps'][0]['verifications'][0]['actual']='wrong content'
    rejected(lambda:validate_export(bad),'Reject inconsistent passing readback')
    bad = copy.deepcopy(registry); bad['models'][0]['enabled']=True
    rejected(lambda:validators['registry.schema.json'].validate(bad),'Reject enabled model without pinned artifact')
    bad = copy.deepcopy(registry); bad['deployments'][0]['status']='ACTIVE'
    rejected(lambda:validate_registry(bad),'Reject ACTIVE unresolved model')
    for p in source_files('*.json'):
        json.loads(p.read_text())
    CHECKS.append('All JSON parses')
    for p in source_files('*.yaml'):
        data = yaml.safe_load(p.read_text())
        if p.relative_to(ROOT).parts[0] == 'ui':
            check(isinstance(data,dict),'UI dependency YAML parsed: '+p.name)
            continue
        check(isinstance(data,dict) and data['schema_version']=='1.0','YAML parsed: '+p.name)
        if p.parent.name=='recipes':
            check((ROOT/data['schema']).is_file(),'Recipe schema reference: '+p.name)
            check(abs(sum(data['dataset']['split'][k] for k in ['train','validation','test'])-1)<1e-8,'Recipe split sum: '+p.name)
            check(data['dataset']['synthetic'] is False and data['promotion']['automatic'] is False,'Recipe excludes fixtures and silent promotion: '+p.name)
            check((ROOT/data['promotion']['policy']).is_file(),'Recipe promotion policy ref: '+p.name)
    for p in source_files('*.md'):
        content = p.read_text()
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',content):
            if '://' in target or target.startswith('#'):
                continue
            path = target.split('#',1)[0]
            check((p.parent/unquote(path)).exists(),'Markdown link: '+p.name+' → '+target)
    return exported, registry

def validate_database(exported, registry):
    with tempfile.TemporaryDirectory(prefix='aos-contract-') as tmp:
        con = sqlite3.connect(str(Path(tmp)/'test.sqlite'))
        con.execute('PRAGMA foreign_keys=ON')
        for sql in sorted((ROOT/'database/migrations').glob('*.sql')):
            con.executescript(sql.read_text())
        check(con.execute('PRAGMA foreign_keys').fetchone()[0]==1,'SQLite FK enforcement enabled')
        check(con.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]==len(list((ROOT/'database/migrations').glob('*.sql'))),'All migrations applied')
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected={'tasks','runs','steps','state_snapshots','decisions','actions','observations','verifications','supervisor_escalations','human_interventions','model_calls','artifacts','trajectory_labels','models','adapters','deployments','benchmarks','deployment_events','active_deployments'}
        expected.update({'desktop_sessions','desktop_inputs','desktop_events','desktop_tasks','desktop_approvals',
                         'desktop_web_profile_bindings','desktop_remote_entry_bindings',
                         'desktop_remote_route_bindings','desktop_remote_form_bindings',
                         'desktop_remote_form_state_bindings',
                         'desktop_remote_form_cookie_bindings',
                         'dataset_reviews'})
        check(expected<=tables,'All trajectory and registry tables created')
        stamp='2026-09-19T09:00:00Z'
        def insert(table,row):
            columns=','.join(row)
            con.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in row)})',list(row.values()))
        task=exported['task']; run=exported['run']; step=exported['steps'][0]
        insert('tasks',{'task_id':task['task_id'],'original_goal':task['goal_original'],'normalized_goal':task['goal_normalized'],'success_criteria_json':json.dumps(task['success_criteria']),'workspace_scope_json':'["/workspace"]','created_at':stamp})
        for rid in [run['run_id'],'run-other']:
            insert('runs',{'run_id':rid,'task_id':task['task_id'],'status':'running','policy_version':'v1','environment_json':'{}','deployment_snapshot_json':'{}','started_at':stamp})
        insert('steps',{'step_id':step['step_id'],'run_id':run['run_id'],'ordinal':0,'state':'VERIFY','started_at':stamp})
        insert('steps',{'step_id':'step-other','run_id':'run-other','ordinal':0,'state':'OBSERVE','started_at':stamp})
        links={'run_id':run['run_id'],'step_id':step['step_id']}
        snap=step['snapshots'][0]
        insert('state_snapshots',{**links,'snapshot_id':snap['snapshot_id'],'state_version':0,'state_json':json.dumps({'state':snap['state']}),'content_sha256':hashlib.sha256(snap['state'].encode()).hexdigest(),'created_at':stamp})
        d=step['decisions'][0]
        insert('decisions',{**links,'decision_id':d['decision_id'],'snapshot_id':snap['snapshot_id'],'question':d['question'],'options_json':json.dumps(d['options']),'probabilities_json':json.dumps(d['probabilities']),'selected_option':d['selected_option'],'confidence':0.96,'policy_result':'allow','created_at':stamp})
        a=step['actions'][0]
        action={**links,'action_id':a['action_id'],'decision_id':d['decision_id'],'idempotency_key':a['idempotency_key'],'tool':a['tool'],'arguments_json':json.dumps(a['arguments']),'status':'ok','actual_option':a['actual_option'],'created_at':stamp}
        insert('actions',action)
        o=step['observations'][0]
        insert('observations',{**links,'observation_id':o['observation_id'],'action_id':a['action_id'],'kind':o['kind'],'payload_json':json.dumps({'summary':o['summary']}),'created_at':stamp})
        v=step['verifications'][0]
        vr={**links,'verification_id':v['verification_id'],'action_id':a['action_id'],'criterion':v['criterion'],'method':v['method'],'result':'passed','expected_json':json.dumps(v['expected']),'actual_json':json.dumps(v['actual']),'evidence_refs_json':json.dumps(v['evidence_refs']),'verifier':'fixture','created_at':stamp}
        insert('verifications',vr)
        insert('trajectory_labels',{**links,'label_id':'label-0','decision_id':d['decision_id'],'verification_id':v['verification_id'],'label_type':'choice','label_json':json.dumps({'correct_option':'write_file'}),'source':'verified_outcome','review_status':'accepted','created_at':stamp})
        con.commit()
        check(con.execute('SELECT count(*) FROM verifications WHERE result="passed"').fetchone()[0]==1,'Synthetic action-observation-verification-label roundtrip')
        rejected(lambda:insert('actions',{**action,'action_id':'duplicate-action'}),'Reject duplicate idempotency key')
        rejected(lambda:insert('actions',{**action,'action_id':'cross-run','run_id':'run-other','step_id':'step-other','idempotency_key':'other-key'}),'Reject cross-run decision action link')
        rejected(lambda:insert('verifications',{**vr,'verification_id':'cross-verification','run_id':'run-other','step_id':'step-other'}),'Reject cross-run verification action link')
        rejected(lambda:insert('actions',{**action,'action_id':'bad-json','idempotency_key':'bad-json','arguments_json':'{' }),'Reject malformed SQL JSON payload')
        rejected(lambda:insert('verifications',{**vr,'verification_id':'bad-result','result':'maybe'}),'Reject invalid verification enum')
        for m in registry['models']:
            insert('models',{'model_id':m['id'],'source':m['source'],'backend':m['backend'],'metadata_json':json.dumps(m['metadata'])})
        for d in registry['deployments']:
            insert('deployments',{'deployment_id':d['id'],'model_id':d['model_id'],'status':d['status'],'config_json':json.dumps(d['config']),'config_sha256':d['config_sha256'],'created_at':stamp})
        rejected(lambda:con.execute('UPDATE models SET enabled=1 WHERE model_id=?',(registry['models'][0]['id'],)),'Reject SQL enabled model without revision/hash')
        insert('adapters',{'adapter_id':'adapter-a','model_id':registry['models'][0]['id'],'base_sha256':'0'*64,'sha256':'1'*64,'compatibility':'unknown','metadata_json':'{}'})
        rejected(lambda:insert('deployments',{'deployment_id':'mismatch-adapter','model_id':registry['models'][1]['id'],'adapter_id':'adapter-a','status':'EXPERIMENTAL','config_json':'{}','config_sha256':'2'*64,'created_at':stamp}),'Reject adapter attached to another model')
        session={'session_id':'synthetic-desktop','runtime_id':'fixture-runtime','image_id':'sha256:'+'0'*64,
                 'owner':'AGENT','lease_id':'fixture-lease','generation':0,'status':'running','created_at':stamp,'updated_at':stamp}
        insert('desktop_sessions',session)
        queued={'input_id':'synthetic-input','session_id':session['session_id'],'lease_id':session['lease_id'],
                'generation':0,'tool':'type_note','status':'queued','created_at':stamp}
        insert('desktop_inputs',queued)
        insert('desktop_events',{'event_id':'synthetic-event','session_id':session['session_id'],'kind':'pause','payload_json':'{"synthetic":true}','created_at':stamp})
        check(con.execute('SELECT count(*) FROM desktop_inputs').fetchone()[0]==1,'Synthetic desktop session/input/event roundtrip')
        rejected(lambda:insert('desktop_sessions',{**session,'session_id':'bad-owner','owner':'MODEL'}),'Reject model desktop ownership')
        rejected(lambda:insert('desktop_inputs',{**queued,'input_id':'bad-tool','tool':'shell'}),'Reject arbitrary desktop queue tool')
        rejected(lambda:insert('desktop_inputs',{**queued,'input_id':'bad-session','session_id':'missing'}),'Reject orphan desktop input')
        rejected(lambda:insert('desktop_inputs',{**queued,'input_id':'bad-status','status':'replay'}),'Reject invalid desktop input status')
        job={'job_id':'synthetic-job','session_id':session['session_id'],'run_id':run['run_id'],'kind':'hello',
             'lease_id':session['lease_id'],'generation':0,'status':'waiting_approval','real_model':0,'created_at':stamp,'updated_at':stamp}
        insert('desktop_tasks',job)
        rejected(lambda:insert('desktop_tasks',{**job,'job_id':'second-job','run_id':'run-other'}),'Reject concurrent desktop tasks in SQL')
        rejected(lambda:insert('desktop_tasks',{**job,'job_id':'bad-kind','run_id':'run-other','status':'failed','kind':'shell'}),'Reject arbitrary scheduled task kind')
        approval=read_json('examples/desktop_approval.json')['approval']
        approval_row={'approval_id':approval['approval_id'],'job_id':job['job_id'],'envelope_json':json.dumps(approval),
                      'action_sha256':approval['action_sha256'],'expires_at':approval['expires_at'],'status':'pending','created_at':stamp,'updated_at':stamp}
        insert('desktop_approvals',approval_row)
        check(con.execute('SELECT status FROM desktop_approvals').fetchone()[0]=='pending','Synthetic scheduled approval roundtrip')
        rejected(lambda:insert('desktop_approvals',{**approval_row,'approval_id':'orphan-approval','job_id':'missing'}),'Reject orphan approval')
        rejected(lambda:con.execute("UPDATE desktop_approvals SET status='replay'"),'Reject invalid approval state')
        second_approval={**approval_row,'approval_id':'synthetic-second-approval','action_sha256':'1'*64}
        rejected(lambda:insert('desktop_approvals',second_approval),'Reject simultaneous pending approvals for one task')
        con.execute("UPDATE desktop_approvals SET status='consumed'")
        insert('desktop_approvals',second_approval)
        check(con.execute('SELECT count(*) FROM desktop_approvals').fetchone()[0]==2,'Distinct per-action approvals persist for multi-action tasks')
        con.execute("UPDATE desktop_tasks SET kind='browser_form'")
        con.execute("UPDATE desktop_tasks SET kind='vision_canvas'")
        con.execute("UPDATE desktop_tasks SET status='paused'")
        check(con.execute('SELECT status FROM desktop_tasks').fetchone()[0]=='paused','Paused scheduled task persists')
        rejected(lambda:insert('desktop_tasks',{**job,'job_id':'while-paused','run_id':'run-other'}),'Paused task reserves the session')
        con.execute("UPDATE desktop_tasks SET kind='browser_staging_workflow',runtime_id='synthetic-browser'")
        con.execute("UPDATE runs SET policy_version='browser-staging-workflow-policy-v1' WHERE run_id=?",(run['run_id'],))
        pin=read_json('examples/synthetic_staging_pin.json')['pin']
        binding={'job_id':job['job_id'],'run_id':run['run_id'],'profile_sha256':pin['profile_sha256'],
                 'task_key':pin['task_key'],'pin_json':json.dumps(pin,sort_keys=True,separators=(',',':')),
                 'pin_sha256':'1'*64,'browser_runtime_id':'synthetic-browser','created_at':stamp}
        insert('desktop_web_profile_bindings',binding)
        check(con.execute('SELECT count(*) FROM desktop_web_profile_bindings').fetchone()[0]==1,
              'Synthetic profile-to-run metadata binding persists')
        rejected(lambda:con.execute("UPDATE desktop_tasks SET runtime_id='other'"),
                 'Reject bound desktop runtime identity mutation')
        rejected(lambda:con.execute('DELETE FROM desktop_web_profile_bindings'),
                 'Reject append-only profile-to-run binding deletion')
        check(con.execute('PRAGMA foreign_keys').fetchone()[0]==1,'Rebuilding scheduled tables restores FK enforcement')
        receipts=read_json('examples/dataset_review_receipts.json')['receipts']
        for receipt in receipts:
            payload={**receipt,'run_id':exported['run']['run_id']}
            event_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            insert('dataset_reviews',{key:value for key,value in payload.items() if key!='schema_version'} | {'event_sha256':event_hash})
        check(con.execute('SELECT count(*) FROM dataset_reviews').fetchone()[0]==2,'Dataset acceptance and revocation append separately')
        rejected(lambda:con.execute("UPDATE dataset_reviews SET reviewer_id='changed'"),'Reject dataset review mutation')
        rejected(lambda:con.execute('DELETE FROM dataset_reviews'),'Reject dataset review deletion')
        rejected(lambda:con.execute('INSERT OR REPLACE INTO dataset_reviews SELECT * FROM dataset_reviews'),'Reject dataset review replacement')
        check(con.execute('SELECT sum(training_eligible) FROM runs').fetchone()[0]==0,'Dataset receipts never enable runs')
        check(con.execute('PRAGMA foreign_key_check').fetchall()==[],'SQLite foreign_key_check clean')
        check(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLite integrity_check clean')
        con.close()

def validate_manifest():
    manifest=ROOT/'MANIFEST.sha256'
    if not manifest.exists():
        print('MANIFEST not present yet; generation-stage validation only.')
        return
    members = validate_source_manifest(ROOT)
    tracked = {member.name for member in members}
    for member in members:
        check(True, 'Manifest source policy and integrity: ' + member.name)
    check('README.md' in tracked and 'scripts/validate_package.py' in tracked,'Manifest contains core artifacts')

def main():
    exported,registry=validate_files()
    validate_database(exported,registry)
    validate_manifest()
    print(f'PASS: {len(CHECKS)} package checks. No model/runtime/training tests were run.')

if __name__=='__main__':
    main()
