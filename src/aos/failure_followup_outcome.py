"""Content-free, read-only outcome checks; never evidence of guidance causality."""

from datetime import datetime
import json
import re
import sqlite3

from .browser import BROWSER_EXPECTED, BROWSER_SCOPE, BROWSER_VALUE
from .computer import ToolRegistry
from .contracts import (AOSFault, Action, HELLO_CONTENT, HELLO_PATH, LOCAL_NAVIGATION_SCOPE,
                        Phase, Prediction, Option, REMOTE_ENTRY_SCOPE,
                        REMOTE_ROUTES_SCOPE, REMOTE_STATIC_ASSETS_SCOPE, REMOTE_FORM_SCOPE,
                        STAGING_WORKFLOW_MESSAGE, STAGING_WORKFLOW_SCOPE,
                        State, canonical, digest)
from .desktop_tasks import Approval
from .learning_events import LOCAL_PAGE_OUTCOMES
from .staging_workflow_operator import EXPECTED_OUTCOMES, STAGES, staging_outcome
from .vision import VISION_EXPECTED, VISION_SCOPE, VisionScene
from .web_application_binding import WebReadOnlyRoutePlan, WebTaskAdmissionDraft
from .web_readonly_data import WebReadOnlyDataBundlePlan
from .web_static_assets import WebStaticAssetPlan
from .web_https_form_transport import WebHTTPSFormPlan, form_stage_arguments
from .web_https_form_state_probe import (WebHTTPSFormStatePlan, WebHTTPSFormStateReport,
                                         form_state_action_arguments)


def _require(condition):
    if not condition:
        raise ValueError('evidence_missing_or_changed')


def _equal(*values):
    return len({canonical(value) for value in values}) == 1


def _rows(connection, query, arguments=()):
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(query, arguments).fetchmany(501)
    _require(len(rows) <= 500)
    return rows


def _one(connection, query, arguments=()):
    rows = _rows(connection, query, arguments)
    _require(len(rows) == 1)
    return rows[0]


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None)
    return parsed.timestamp()


def _json(value):
    result = json.loads(value)
    _require(canonical(result) == value)
    return result


def _report(status, reason, count=0, scope='none'):
    return {'status': status, 'verification_count': count, 'scope': scope, 'reason': reason}


def _bound_run(connection, run_id):
    for table in ('state_snapshots', 'actions', 'decisions', 'verifications', 'observations',
                  'model_calls', 'human_interventions', 'steps'):
        _rows(connection, f'SELECT 1 FROM {table} WHERE run_id=? LIMIT 501', (run_id,))


def _history(connection, run):
    runtime = _one(connection, 'SELECT * FROM runtime_states WHERE run_id=?', (run['run_id'],))
    terminal = State.model_validate_json(runtime['state_json'])
    _require(terminal.phase == Phase.SUCCEEDED and terminal.state_version == runtime['state_version'])
    states = {}
    snapshots = {}
    previous_time = _time(run['started_at'])
    for row in _rows(connection, 'SELECT * FROM state_snapshots WHERE run_id=? ORDER BY state_version,created_at', (run['run_id'],)):
        state = State.model_validate_json(row['state_json'])
        timestamp = _time(row['created_at'])
        _require(row['state_version'] == state.state_version and row['step_id'] == state.step_id
                 and row['content_sha256'] == digest(state.model_dump(mode='json'))
                 and previous_time <= timestamp <= _time(run['ended_at']))
        for key in ('run_id', 'task_id', 'runtime_id', 'deployment_id', 'task_kind',
                    'authorized_path', 'authorized_content', 'skill_invocation_sha256'):
            _require(getattr(state, key) == getattr(terminal, key))
        _require(state.run_id == run['run_id'] and state.task_id == run['task_id'])
        _one(connection, 'SELECT step_id FROM steps WHERE run_id=? AND step_id=?', (state.run_id, state.step_id))
        _require(state.state_version not in states or states[state.state_version][0] == state)
        states[state.state_version] = (state, timestamp)
        snapshots[row['snapshot_id']] = (state, timestamp)
        previous_time = timestamp
    _require(sorted(states) == list(range(terminal.state_version + 1))
             and states[0][0].phase == Phase.CREATED and states[terminal.state_version][0] == terminal)
    deployment = json.loads(run['deployment_snapshot_json'])
    _require(terminal.deployment_id == deployment['deployment_id'])
    jobs = _rows(connection, 'SELECT * FROM desktop_tasks WHERE run_id=?', (run['run_id'],))
    _require(len(jobs) <= 1)
    job = jobs[0] if jobs else None
    if job is not None:
        _require(job['status'] == 'succeeded' and job['kind'] == terminal.task_kind
                 and job['runtime_id'] == terminal.runtime_id and job['lease_id'] == terminal.owner_lease_id)
    _require(not _rows(connection, "SELECT 1 FROM model_calls WHERE run_id=? AND status IN ('pending','running')", (run['run_id'],)))
    return terminal, states, snapshots, job


def _actions(connection, run, terminal, states, snapshots):
    actions = _rows(connection, 'SELECT * FROM actions WHERE run_id=? ORDER BY created_at,action_id', (run['run_id'],))
    checked = []
    previous_completion = _time(run['started_at'])
    for row in actions:
        envelope = _one(connection, 'SELECT * FROM action_envelopes WHERE action_id=?', (row['action_id'],))
        action = Action.model_validate_json(envelope['envelope_json'])
        ToolRegistry.validate(action)
        _require(envelope['envelope_json'] == canonical(action.model_dump(mode='json'))
                 and envelope['payload_sha256'] == digest(action.model_dump(mode='json')))
        for key in ('run_id', 'step_id', 'action_id', 'tool', 'idempotency_key'):
            _require(row[key] == getattr(action, key))
        _require(_equal(_json(row['arguments_json']), action.arguments) and row['status'] == 'ok'
                 and row['error_code'] is None and row['actual_option'] == action.selected_option)
        execute, execute_time = states[action.state_version]
        _require(execute.phase == Phase.EXECUTE and execute.owner == 'AGENT')
        if action.tool == 'vision.click':
            _require(action.arguments['capture_id'] == execute.capture_id
                     and action.arguments['scene_sha256'] == execute.scene_sha256
                     and action.arguments['element_id'] == action.selected_option)
        for key in ('run_id', 'task_id', 'step_id', 'runtime_id', 'owner_lease_id'):
            _require(getattr(execute, key) == getattr(action, key))
        decision = _one(connection, 'SELECT * FROM decisions WHERE decision_id=? AND run_id=? AND step_id=?',
                        (row['decision_id'], row['run_id'], row['step_id']))
        decide, decide_time = snapshots[decision['snapshot_id']]
        _require(decide.phase == Phase.DECIDE and decide.step_id == action.step_id
                 and decide.state_version < execute.state_version
                 and decide.owner_lease_id == action.owner_lease_id
                 and decision['policy_result'] == 'allow' and decision['selected_option'] == action.selected_option)
        prediction = Prediction(selected_option=decision['selected_option'], probabilities=_json(decision['probabilities_json']))
        prediction.validate_options([Option.model_validate(value) for value in _json(decision['options_json'])])
        if decision['call_id'] is not None:
            call = _one(connection, 'SELECT * FROM model_calls WHERE call_id=? AND run_id=? AND step_id=?',
                        (decision['call_id'], action.run_id, action.step_id))
            _require(call['status'] == 'ok' and call['role'] == 'system1' and call['deployment_id'] == execute.deployment_id)
        created, completed = _time(row['created_at']), _time(row['completed_at'])
        _require(previous_completion <= created <= completed <= _time(run['ended_at'])
                 and decide_time <= _time(decision['created_at']) <= execute_time <= created < action.deadline)
        _json(row['result_json'])
        _source_target(connection, row, action, decide_time)
        previous_completion = completed
        checked.append((row, action))
    return checked


def _source_target(connection, row, action, decision_time):
    if 'snapshot_id' not in action.arguments and action.tool != 'vision.click':
        return
    kind = ('vision.scene' if action.tool == 'vision.click' else
            'browser.dom' if action.tool in {'browser.fill', 'browser.submit'} else 'browser.local_navigation')
    observations = _rows(connection, 'SELECT * FROM observations WHERE run_id=? AND step_id=? AND kind=? AND action_id IS NULL ORDER BY created_at',
                         (action.run_id, action.step_id, kind))
    prior = [observation for observation in observations if _time(observation['created_at']) <= decision_time]
    _require(bool(prior))
    payload = _json(prior[-1]['payload_json'])
    if action.tool == 'vision.click':
        scene = VisionScene.model_validate(payload)
        _require(_equal(payload, scene.model_dump(mode='json')) and digest(payload) == action.arguments['scene_sha256']
                 and scene.capture_id == action.arguments['capture_id']
                 and scene.targets()[action.arguments['element_id']].label == 'SAVE')
    else:
        _require(payload['snapshot_id'] == action.arguments['snapshot_id'])
        targets = [element for element in payload['elements'] if element['element_id'] == action.arguments['element_id']]
        role, label = {'browser.fill': ('textbox', 'Message'), 'browser.submit': ('button', 'Save locally'),
                       'browser.fixture.follow': ('link', 'Details'), 'browser.staging.follow': ('link', 'Draft'),
                       'browser.staging.fill': ('textbox', 'Message'), 'browser.staging.submit': ('button', 'Save draft')}[action.tool]
        _require(len(targets) == 1 and targets[0]['role'] == role and targets[0]['label'] == label)


def _approve(connection, job, row, action):
    if job is None:
        return
    matches = []
    for record in _rows(connection, 'SELECT * FROM desktop_approvals WHERE job_id=?', (job['job_id'],)):
        approval = Approval.model_validate_json(record['envelope_json'])
        _require(record['envelope_json'] == approval.model_dump_json())
        if approval.action.action_id == action.action_id:
            matches.append((record, approval))
    _require(len(matches) == 1)
    record, approval = matches[0]
    execute_time = _one(connection, 'SELECT min(created_at) AS created_at FROM state_snapshots WHERE run_id=? AND state_version=?',
                        (action.run_id, action.state_version))['created_at']
    _require(approval.action == action and approval.job_id == job['job_id']
             and approval.approval_id == record['approval_id'] and record['status'] == 'consumed'
             and record['action_sha256'] == approval.action_sha256 == digest(action.model_dump(mode='json'))
             and record['expires_at'] == approval.expires_at <= action.deadline
             and _time(execute_time) <= _time(record['created_at'])
             and _time(record['created_at']) <= _time(record['updated_at']) < approval.expires_at
             and _time(record['updated_at']) <= _time(row['created_at']))
    humans = _rows(connection, "SELECT * FROM human_interventions WHERE run_id=? AND step_id=? AND kind='approve' AND actor='local_authenticated_user'",
                   (row['run_id'], row['step_id']))
    matching = [human for human in humans if _json(human['payload_json']) == {
        'approval_id': approval.approval_id, 'action_sha256': approval.action_sha256}]
    _require(len(matching) == 1 and _time(record['created_at']) <= _time(matching[0]['created_at']) <= _time(record['updated_at']))


def _local_contract(kind, actions):
    if kind == 'hello':
        tool = actions[0][1].tool
        _require(tool in {'filesystem.write', 'filesystem.read'})
        return HELLO_PATH, HELLO_CONTENT, [(tool, 'filesystem.read', 'independent_read_equals',
                'aos-exact-bytes-v1', 'filesystem.read', HELLO_CONTENT)]
    if kind == 'browser_form':
        return BROWSER_SCOPE, BROWSER_VALUE, [
            ('browser.fill', 'browser.verify', 'independent_dom_equals', 'aos-browser-form-v1', 'browser.dom',
             {'value': BROWSER_VALUE, 'receipt': '', 'submissions': 0}),
            ('browser.submit', 'browser.verify', 'independent_dom_equals', 'aos-browser-form-v1', 'browser.dom', BROWSER_EXPECTED)]
    if kind == 'vision_canvas':
        return VISION_SCOPE, 'SAVE', [('vision.click', 'vision.verify', 'independent_canvas_equals',
                'aos-visual-canvas-v1', 'vision.outcome', VISION_EXPECTED)]
    if kind == 'browser_local_navigation':
        return LOCAL_NAVIGATION_SCOPE, 'DETAILS', [(tool, 'browser.fixture.snapshot', 'independent_local_page_equals',
                'aos-local-navigation-v1', 'browser.local_navigation', expected) for tool, expected in LOCAL_PAGE_OUTCOMES.items()]
    return STAGING_WORKFLOW_SCOPE, STAGING_WORKFLOW_MESSAGE, [
        (stage[0], 'browser.staging.snapshot', 'independent_staging_state_equals',
         'aos-synthetic-staging-v1', 'browser.local_navigation', expected)
        for stage, expected in zip(STAGES, EXPECTED_OUTCOMES, strict=True)]


def _local_arguments(terminal, action):
    arguments = action.arguments
    if terminal.task_kind == 'hello':
        _require(arguments == {'path': HELLO_PATH, **({'content': HELLO_CONTENT} if action.tool == 'filesystem.write' else {})})
    elif action.tool in {'browser.fill', 'browser.submit', 'browser.fixture.follow',
                         'browser.staging.follow', 'browser.staging.fill', 'browser.staging.submit'}:
        _require(set(arguments) == {'snapshot_id', 'element_id'} | ({'value'} if action.tool.endswith('.fill') else set()))
        _require(all(isinstance(arguments[key], str) and arguments[key] for key in ('snapshot_id', 'element_id')))
        if action.tool.endswith('.fill'):
            _require(arguments['value'] == BROWSER_VALUE)
    elif action.tool == 'vision.click':
        _require(arguments.get('capture_id') is not None and arguments.get('scene_sha256') is not None)
    else:
        _require(arguments == {})


def _verification_order(connection, action, read_row, observation, verification):
    verify = _one(connection, 'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=?',
                  (action.run_id, action.state_version + 1))
    next_state = _one(connection, 'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=?',
                      (action.run_id, action.state_version + 2))
    state = State.model_validate_json(verify['state_json'])
    _require(state.phase == Phase.VERIFY and state.step_id == action.step_id
             and _time(read_row['completed_at']) <= _time(verify['created_at'])
             <= _time(observation['created_at']) <= _time(verification['created_at'])
             <= _time(next_state['created_at']))


def _remote_binding(connection, terminal, job):
    _require(job is not None)
    kind = terminal.task_kind
    suffix = {'browser_remote_entry': 'entry', 'browser_remote_routes': 'route',
              'browser_remote_static_assets': 'static_asset', 'browser_remote_form': 'form'}[kind]
    binding = _one(connection, f'SELECT * FROM desktop_remote_{suffix}_bindings WHERE run_id=?', (terminal.run_id,))
    draft = WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
    _require(binding['job_id'] == job['job_id'] and binding['browser_runtime_id'] == terminal.runtime_id
             and draft.runtime.runtime_id == terminal.runtime_id)
    for key in ('profile_sha256', 'binding_sha256', 'runtime_sha256'):
        _require(binding[key] == getattr(draft, key))
    _require(draft.task_sha256 == digest(draft.task.model_dump()) and draft.runtime_sha256 == digest(draft.runtime.model_dump())
             and draft.binding_sha256 == digest({'profile_sha256': draft.profile_sha256,
                 'task_sha256': draft.task_sha256, 'runtime_sha256': draft.runtime_sha256}))
    _require(binding['draft_json'] == canonical(draft.model_dump(mode='json')))
    return binding, draft


def _remote_contract(connection, terminal, job):
    binding, draft = _remote_binding(connection, terminal, job)
    kind = terminal.task_kind
    common = {'profile_sha256': draft.profile_sha256, 'binding_sha256': draft.binding_sha256}
    if kind == 'browser_remote_entry':
        return REMOTE_ENTRY_SCOPE, draft.binding_sha256, [(
            'browser.remote.open', 'browser.remote.observe', 'independent_remote_entry_readback',
            'aos-remote-entry-v1', 'browser.remote_entry', draft.task.entry_url,
            {**common, 'entry_url': draft.task.entry_url}, {}, None)]
    raw = _json(binding['plan_json'])
    if kind == 'browser_remote_routes':
        plan = WebReadOnlyRoutePlan.model_validate(raw)
    else:
        plan = (WebReadOnlyDataBundlePlan if raw.get('schema_version') == '2.0' else WebStaticAssetPlan).model_validate(raw)
    plan_hash = digest(plan.model_dump())
    _require(_equal(raw, plan.model_dump(mode='json'))
             and binding['plan_sha256'] == plan_hash and plan.profile_sha256 == draft.profile_sha256
             and plan.task_sha256 == draft.task_sha256)
    common['plan_sha256'] = plan_hash
    if kind == 'browser_remote_routes':
        return REMOTE_ROUTES_SCOPE, plan_hash, [(
            'browser.remote.route', 'browser.remote.observe', 'independent_remote_route_readback',
            'aos-remote-routes-v1', 'browser.remote_route', url,
            {**common, 'route_index': index, 'url': url}, {'route_index': index}, None)
            for index, url in enumerate(plan.routes)]
    data = isinstance(plan, WebReadOnlyDataBundlePlan)
    return REMOTE_STATIC_ASSETS_SCOPE, plan_hash, [(
        'browser.static.open', 'browser.static.observe',
        'independent_readonly_data_bundle_readback' if data else 'independent_static_bundle_readback',
        'aos-remote-readonly-data-v1' if data else 'aos-remote-static-assets-v1',
        'browser.remote_readonly_data' if data else 'browser.remote_static_assets', draft.task.entry_url,
        {**common, 'entry_url': draft.task.entry_url}, {}, plan)]


def _verify_pairs(connection, terminal, actions, job, contracts, remote):
    verifications = _rows(connection, 'SELECT * FROM verifications WHERE run_id=? ORDER BY created_at,verification_id', (terminal.run_id,))
    _require(len(actions) == 2 * len(contracts) and len(verifications) == len(contracts))
    for index, contract in enumerate(contracts):
        tool, read_tool, method, verifier, observation_kind, expected = contract[:6]
        effect_row, effect = actions[2 * index]
        read_row, readback = actions[2 * index + 1]
        verification = verifications[index]
        _require(effect.tool == tool and readback.tool == read_tool and effect.action_id != readback.action_id
                 and effect_row['decision_id'] == read_row['decision_id'] and effect.state_version == readback.state_version
                 and verification['action_id'] == effect.action_id and verification['step_id'] == effect.step_id
                 and verification['method'] == method and verification['verifier'] == verifier and verification['result'] == 'passed'
                 and effect.verification == method and readback.verification == method)
        choice = {'filesystem.write': 'write_file', 'filesystem.read': 'read_file',
                  'browser.fill': 'fill_message', 'browser.submit': 'submit_form',
                  'browser.fixture.open': 'open_start', 'browser.fixture.follow': 'follow_details',
                  **{stage[0]: stage[1] for stage in STAGES}}.get(tool, 'open_entry' if remote else effect.selected_option)
        _require(effect.selected_option == readback.selected_option == choice)
        _approve(connection, job, effect_row, effect)
        evidence = _json(verification['evidence_refs_json'])
        _require(isinstance(evidence, list) and len(evidence) == 1)
        observation = _one(connection, 'SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
                           (evidence[0], terminal.run_id, effect.step_id))
        payload = _json(observation['payload_json'])
        _require(observation['action_id'] == readback.action_id and observation['kind'] == observation_kind
                 and _equal(payload, _json(read_row['result_json']))
                 and _time(read_row['completed_at']) <= _time(observation['created_at']) <= _time(verification['created_at']))
        _verification_order(connection, effect, read_row, observation, verification)
        actual = _json(verification['actual_json'])
        _require(_equal(actual, _json(verification['expected_json'])))
        if remote:
            _require(_equal(effect.arguments, contract[6]) and _equal(readback.arguments, contract[7]))
            opened = _json(effect_row['result_json'])
            for key in ('url', 'title_sha256', 'heading_sha256'):
                _require(actual[key] == opened[key] == payload[key])
            _require(actual['url'] == expected)
            for key in ('title_sha256', 'heading_sha256'):
                _require(re.fullmatch('[a-f0-9]{64}', actual[key]) is not None)
            plan = contract[8]
            if plan is None:
                _require(set(actual) == {'url', 'title_sha256', 'heading_sha256', 'response_sha256'}
                         and re.fullmatch('[a-f0-9]{64}', actual['response_sha256']) is not None)
            else:
                _require(set(actual) == {'url', 'title_sha256', 'heading_sha256', 'responses'})
                responses = actual['responses']
                keys = {'entry_response_sha256', 'asset_response_sha256'}
                hashes = [responses['entry_response_sha256'], *responses['asset_response_sha256']]
                _require(len(responses['asset_response_sha256']) == len(plan.assets))
                if isinstance(plan, WebReadOnlyDataBundlePlan):
                    keys.add('data_response_sha256')
                    _require(len(responses['data_response_sha256']) == len(plan.data_resources))
                    hashes.extend(responses['data_response_sha256'])
                _require(set(responses) == keys and all(re.fullmatch('[a-f0-9]{64}', value) for value in hashes))
        else:
            _local_arguments(terminal, effect)
            _local_arguments(terminal, readback)
            if terminal.task_kind == 'hello':
                measured = payload['content']
                _require(set(payload) == {'content'})
            elif terminal.task_kind == 'browser_local_navigation':
                measured = {'page': payload['page'], 'heading': payload['heading'],
                            'links': [{'role': element['role'], 'label': element['label']} for element in payload['elements']]}
            elif terminal.task_kind == 'browser_staging_workflow':
                measured = staging_outcome(payload)
            else:
                measured = payload
            _require(_equal(actual, expected, measured))
    return len(verifications)


def _form_verification(connection, terminal, effect, readback, method, verifier, expected):
    row, action = effect
    read_row, read_action = readback
    verification = _one(connection, 'SELECT * FROM verifications WHERE run_id=? AND method=?', (terminal.run_id, method))
    _require(verification['action_id'] == action.action_id and verification['step_id'] == action.step_id
             and verification['result'] == 'passed' and verification['verifier'] == verifier
             and _equal(_json(verification['expected_json']), expected, _json(verification['actual_json'])))
    references = _json(verification['evidence_refs_json'])
    _require(isinstance(references, list) and len(references) == 1)
    observation = _one(connection, 'SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
                       (references[0], terminal.run_id, action.step_id))
    _require(observation['action_id'] == read_action.action_id and observation['kind'] == 'browser.https_form'
             and _equal(_json(observation['payload_json']), _json(read_row['result_json']))
             and _time(read_row['completed_at']) <= _time(observation['created_at']) <= _time(verification['created_at']))
    _verification_order(connection, action, read_row, observation, verification)


def _form_outcome(connection, terminal, actions, job):
    binding, draft = _remote_binding(connection, terminal, job)
    plan = WebHTTPSFormPlan.model_validate_json(binding['plan_json'])
    plan_hash = digest(plan.model_dump())
    _require(binding['plan_json'] == canonical(plan.model_dump())
             and binding['plan_sha256'] == plan_hash == terminal.authorized_content
             and terminal.authorized_path == REMOTE_FORM_SCOPE and plan.entry_url == draft.task.entry_url
             and plan.profile_sha256 == draft.profile_sha256 and plan.task_sha256 == draft.task_sha256)
    cookies = _rows(connection, 'SELECT * FROM desktop_remote_form_cookie_bindings WHERE run_id=?', (terminal.run_id,))
    _require(len(cookies) <= 1)
    cookie_hash = None
    if cookies:
        cookie = cookies[0]
        _require(cookie['job_id'] == job['job_id'] and cookie['form_plan_sha256'] == plan_hash
                 and cookie['browser_runtime_id'] == terminal.runtime_id)
        cookie_hash = cookie['cookie_sha256']
    state_bindings = _rows(connection, 'SELECT * FROM desktop_remote_form_state_bindings WHERE run_id=?', (terminal.run_id,))
    _require(len(state_bindings) <= 1)
    state_plan = None
    if state_bindings:
        state_binding = state_bindings[0]
        state_plan = WebHTTPSFormStatePlan.model_validate_json(state_binding['state_plan_json'])
        _require(state_binding['state_plan_json'] == canonical(state_plan.model_dump(mode='json'))
                 and state_binding['job_id'] == job['job_id'] and state_binding['browser_runtime_id'] == terminal.runtime_id
                 and state_binding['form_plan_sha256'] == state_plan.form_plan_sha256 == plan_hash
                 and state_binding['state_plan_sha256'] == digest(state_plan.model_dump())
                 and state_plan.profile_sha256 == plan.profile_sha256 and state_plan.task_sha256 == plan.task_sha256)
    base = ['browser.form.open', 'browser.form.fill', 'browser.form.submit', 'browser.form.receipt', 'browser.form.observe']
    orders = [base] if state_plan is None else [
        [base[0], 'browser.form.state_before', *base[1:], 'browser.form.state_after'],
        [*base[:2], 'browser.form.state_before', *base[2:], 'browser.form.state_after']]
    _require([action.tool for row, action in actions] in orders)
    by_tool = {action.tool: (row, action) for row, action in actions}
    fields = by_tool['browser.form.fill'][1].arguments
    names = fields.get('field_name') if 'field_name' in fields else tuple(fields.get('field_names', '').split(','))
    for row, action in actions:
        if action.tool in base:
            stage = base.index(action.tool)
            expected = form_stage_arguments(plan, draft.binding_sha256, names, stage, cookie_hash)
            choice = ('open_entry', 'fill_form', 'submit_form', 'read_receipt', 'read_receipt')[stage]
        else:
            phase = 'before' if action.tool.endswith('before') else 'after'
            expected = form_state_action_arguments(state_plan, draft.binding_sha256, phase, cookie_hash)
            choice = 'read_state_' + phase
        _require(_equal(action.arguments, expected) and action.selected_option == choice
                 and action.verification == 'independent_https_form_transport_readback')
        if action.tool != 'browser.form.observe':
            _approve(connection, job, row, action)
    receipt = by_tool['browser.form.receipt']
    observed = by_tool['browser.form.observe']
    _require(receipt[0]['decision_id'] == observed[0]['decision_id']
             and receipt[1].state_version == observed[1].state_version
             and _json(by_tool['browser.form.open'][0]['result_json'])['url'] == plan.entry_url)
    payload = _json(receipt[0]['result_json'])
    readback = _json(observed[0]['result_json'])
    expected = {'url': plan.receipt_url, 'plan_sha256': plan_hash,
                'submit_request_sha256': digest({'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256})}
    for key in ('title_sha256', 'heading_sha256'):
        _require(re.fullmatch('[a-f0-9]{64}', payload[key]) is not None)
        expected[key] = payload[key]
    _require(all(payload[key] == readback[key] == expected[key] for key in ('url', 'title_sha256', 'heading_sha256')))
    _form_verification(connection, terminal, receipt, observed, 'independent_https_form_transport_readback', 'aos-remote-form-v1', expected)
    if state_plan is not None:
        state_hash = digest(state_plan.model_dump())
        before = _json(by_tool['browser.form.state_before'][0]['result_json'])
        after_pair = by_tool['browser.form.state_after']
        after = WebHTTPSFormStateReport.model_validate(_json(after_pair[0]['result_json']))
        _require(after_pair[0]['result_json'] == canonical(after.model_dump(mode='json'))
                 and _equal(before, {'status': 'before_response_matched', 'state_plan_sha256': state_hash,
                                     'response_sha256': state_plan.expected_before_sha256})
                 and after.profile_sha256 == plan.profile_sha256 and after.task_sha256 == plan.task_sha256
                 and after.state_plan_sha256 == state_hash and after.form_plan_sha256 == plan_hash
                 and after.before_response_sha256 == state_plan.expected_before_sha256
                 and after.after_response_sha256 == state_plan.expected_after_sha256
                 and after.state_url_sha256 == digest({'url': state_plan.state_url})
                 and after.marker_id_sha256 == (digest({'marker_id': state_plan.marker_id}) if state_plan.marker_id else None)
                 and after.before_marker_sha256 == state_plan.expected_before_marker_sha256
                 and after.after_marker_sha256 == state_plan.expected_after_marker_sha256
                 and after.submitted_field_name_sha256 == (digest({'field_name': state_plan.submitted_field_name}) if state_plan.submitted_field_name else None)
                 and after.submitted_value_readback_verified == (True if state_plan.submitted_field_name else None))
        expected = {'state_plan_sha256': state_hash, 'form_plan_sha256': plan_hash,
                    'before_response_sha256': state_plan.expected_before_sha256,
                    'after_response_sha256': state_plan.expected_after_sha256,
                    'receipt_response_sha256': after.receipt_response_sha256}
        _form_verification(connection, terminal, after_pair, after_pair,
                           'declared_https_form_state_readback', 'aos-remote-form-state-v1', expected)
    count = 2 if state_plan else 1
    _require(len(_rows(connection, 'SELECT * FROM verifications WHERE run_id=?', (terminal.run_id,))) == count)
    return count


def audit_followup_outcome(connection: sqlite3.Connection, run_id: str) -> dict:
    """Audit persisted evidence in the caller's snapshot, without changing it."""
    try:
        runs = _rows(connection, 'SELECT * FROM runs WHERE run_id=?', (run_id,))
        if len(runs) != 1 or runs[0]['status'] != 'succeeded' or runs[0]['outcome'] != 'passed' or not runs[0]['ended_at']:
            return _report('not_verified', 'run_not_settled_success')
        run = runs[0]
        _bound_run(connection, run_id)
        supported = {kind.replace('_', '-') + '-policy-v1' for kind in (
            'hello', 'browser_form', 'vision_canvas', 'browser_local_navigation',
            'browser_staging_workflow', 'browser_remote_entry', 'browser_remote_routes', 'browser_remote_static_assets', 'browser_remote_form')}
        if run['policy_version'] not in supported:
            return _report('unsupported', 'task_contract_unsupported')
        terminal, states, snapshots, job = _history(connection, run)
        _require(run['policy_version'] == terminal.task_kind.replace('_', '-') + '-policy-v1')
        actions = _actions(connection, run, terminal, states, snapshots)
        remote = terminal.task_kind.startswith('browser_remote_')
        if terminal.task_kind == 'browser_remote_form':
            count = _form_outcome(connection, terminal, actions, job)
            return _report('verified', 'independent_evidence_verified', count, 'transport_readback')
        scope, content, contracts = (_remote_contract(connection, terminal, job) if remote else _local_contract(terminal.task_kind, actions))
        _require(terminal.authorized_path == scope and terminal.authorized_content == content)
        count = _verify_pairs(connection, terminal, actions, job, contracts, remote)
        return _report('verified', 'independent_evidence_verified', count,
                       'transport_readback' if remote else 'fixed_task_outcome')
    except (AOSFault, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        return _report('not_verified', 'evidence_missing_or_changed')
