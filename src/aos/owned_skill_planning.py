"""Private, cancellable proposals for a host-admitted owned skill; never execution."""

import asyncio
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from .contracts import canonical, digest
from .dataset import validator
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .owned_skill_planner import BonsaiOwnedSkillPlanner, parse_owned_skill_goal
from .workspace_identity import open_existing_workspace


def planning_case_key(goal):
    if parse_owned_skill_goal(goal) is None:
        raise ValueError('owned_skill_goal_unsupported')
    return 'plan-' + digest({'goal': goal})[:32]


def _copy(value):
    return json.loads(canonical(value))


def validate_planning_bundle(bundle, planner=None):
    try:
        validator('owned_skill_planning_bundle').validate(bundle)
        if planner is None:
            identity = bundle['deployment']
            pins = bundle['model_pins']
            if (bundle['real_model'] is not True or identity['real_model'] is not True
                    or identity['kind'] != 'bonsai_native_owned_skill_planner'
                    or identity['deployment_id'] != 'bonsai-' + digest(pins)
                    or identity['pins'] != pins
                    or pins.get('owned_skill_plan_protocol') != 'aos-owned-skill-plan-v2'
                    or pins.get('max_output_tokens') != 768):
                raise ValueError('deployment')
            planner = BonsaiOwnedSkillPlanner.__new__(BonsaiOwnedSkillPlanner)
            planner.identity = identity
            planner.pins = pins
        request = bundle['request']
        evidence = bundle['evidence']
        authority = bundle['authority']
        request_context = None
        if 'knowledge' in bundle:
            from .owned_skill_knowledge import planning_knowledge_payload
            from .owned_skill_knowledge_service import validate_planning_knowledge_binding

            validate_planning_knowledge_binding(bundle, planner)
            request_context = planning_knowledge_payload(bundle['knowledge']['intent']['preview'])
        model_request = (planner.request_body(request['goal'], evidence) if request_context is None else
                         planner.request_body(request['goal'], evidence, request_context=request_context))
        if (request['case_key'] != planning_case_key(request['goal'])
                or evidence[0]['requested_case_key'] != request['case_key']
                or evidence[0]['requested_value'] != parse_owned_skill_goal(request['goal'])
                or request['lease_id'] != authority['lease_id']
                or request['generation'] != authority['generation']
                or bundle['deployment'] != planner.identity
                or bundle['model_pins'] != planner.pins
                or bundle['model_request'] != model_request
                or bundle['real_model'] is not planner.identity['real_model']):
            raise ValueError('binding')
        result = planner.parse_response(canonical(bundle['model_response']), evidence)
        if result.model_dump(mode='json') != bundle['model_response']:
            raise ValueError('response')
    except Exception:
        raise ValueError('owned_skill_planning_bundle_invalid') from None
    return bundle


class OwnedSkillPlanning:
    def __init__(self, planner, directory, prepare, yield_gpu, *, before_begin=None, on_proposal=None):
        self.planner = planner
        self.directory = Path(directory).absolute()
        self.prepare = prepare
        self.yield_gpu = yield_gpu
        self.before_begin = before_begin
        self.on_proposal = on_proposal
        self.episode_learning = None
        self.knowledge = None
        self.task = None
        self.current = None
        self.calls = 0
        self.closed = False
        self.directory_identity = None
        self.parent_identity = None

    @property
    def reserved(self):
        return self.task is not None and not self.task.done()

    def status(self):
        if self.current is None:
            return {'available': True, 'status': 'idle', 'execution_authorized': False,
                    **({'knowledge_available': True} if self.knowledge is not None else {})}
        return {key: _copy(value) for key, value in self.current.items()
                if key in {'planning_id', 'status', 'bundle_sha256', 'real_model', 'model_called',
                           'bound_preview_sha256', 'episode_id'}} | {
                    'available': True, 'execution_authorized': False,
                    **({'knowledge_available': True} if self.knowledge is not None else {})}

    def begin(self, goal, lease_id, generation, *, collect_learning=False, knowledge=None):
        if (self.closed or self.reserved or self.calls >= 8
                or self.current is not None and self.current['status'] == 'bound'):
            raise ValueError('owned_skill_planning_unavailable')
        if (knowledge is not None and (self.knowledge is None or collect_learning)
                or type(collect_learning) is not bool
                or collect_learning and self.before_begin is None
                or type(goal) is not str or not 1 <= len(goal) <= 256
                or type(lease_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', lease_id) is None
                or type(generation) is not int or generation < 0):
            raise ValueError('owned_skill_planning_request_invalid')
        value = parse_owned_skill_goal(goal)
        case_key = planning_case_key(goal) if value is not None else None
        authority, evidence = self.prepare(case_key, value, lease_id, generation)
        self.current = {'planning_id': 'planning-' + uuid4().hex,
                        'status': 'needs_human' if value is None else 'pending',
                        'model_called': False, 'real_model': False, 'bundle_sha256': None}
        if value is None:
            return self.status()
        request = {'schema_version': '1.0', 'goal': goal, 'case_key': case_key,
                   'lease_id': lease_id, 'generation': generation}
        if collect_learning:
            try:
                collection = self.before_begin(self.current['planning_id'], _copy(request), _copy(authority))
                self.current.update(collection)
            except Exception:
                self.current['status'] = 'failed'
                raise
        if knowledge is not None:
            if (knowledge['intent']['preview']['goal'] != goal
                    or knowledge['intent']['preview']['authority'] != authority
                    or knowledge['intent']['preview']['evidence'] != evidence):
                self.current['status'] = 'failed'
                raise ValueError('owned_skill_knowledge_planning_changed')
            self.knowledge.check_current(knowledge['intent']['preview'])
            self.current['knowledge'] = _copy(knowledge)
        self.calls += 1
        current = self.current
        self.task = asyncio.create_task(self._run(current, _copy(request),
                                                  _copy(authority), _copy(evidence)))
        return self.status()

    async def _run(self, current, request, authority, evidence):
        had_guard = hasattr(self.planner, 'before_model_call')
        previous_guard = getattr(self.planner, 'before_model_call', None)
        guard_installed = False
        knowledge = current.get('knowledge')
        try:
            await self.yield_gpu()
            self._check_current(current, request, authority, evidence)
            current['model_called'] = True
            current['real_model'] = self.planner.identity['real_model']
            if knowledge is None:
                result = await self.planner.plan(request['goal'], evidence)
            else:
                from .owned_skill_knowledge import planning_knowledge_payload

                def before_dispatch():
                    if previous_guard is not None:
                        previous_guard()
                    self._check_current(current, request, authority, evidence)
                    self.knowledge.dispatch(current, knowledge)

                self.planner.before_model_call = before_dispatch
                guard_installed = True
                context = planning_knowledge_payload(knowledge['intent']['preview'])
                result = await self.planner.plan(request['goal'], evidence, request_context=context)
            self._check_current(current, request, authority, evidence)
            bundle = {'schema_version': '1.0', 'synthetic': True,
                      'purpose': 'owned_selected_skill_planning',
                      'planning_id': current['planning_id'], 'request': request,
                      'authority': authority, 'evidence': evidence,
                      'deployment': _copy(self.planner.identity),
                      'model_pins': _copy(self.planner.pins),
                      'model_request': self.planner.request_body(request['goal'], evidence),
                      'model_response': result.model_dump(mode='json'),
                      'metrics': _copy(self.planner.last_metrics),
                      'real_model': self.planner.identity['real_model'],
                      'execution_authorized': False, 'activation_authorized': False,
                      'training_ready': False, 'downstream_verified': False}
            if 'episode_id' in current:
                bundle.update({'schema_version': '1.1', 'episode_id': current['episode_id'],
                               'collection_consent_sha256': current['collection_consent_sha256']})
            if knowledge is not None:
                if (self.planner.last_request != self.planner.request_body(
                        request['goal'], evidence, request_context=context)
                        or self.planner.last_response != result.model_dump(mode='json')
                        or 'knowledge_dispatch' not in current):
                    raise ValueError('owned_skill_knowledge_actual_dispatch_missing')
                bundle.update({'schema_version': '1.2',
                               'synthetic': all(hit['synthetic'] for hit in
                                                knowledge['intent']['preview']['retrieval']['hits']),
                               'model_request': _copy(self.planner.last_request),
                               'knowledge': {**knowledge, 'dispatch': current['knowledge_dispatch']}})
                self.knowledge.check_binding(bundle)
            validate_planning_bundle(bundle, self.planner)
            checksum = self._persist(bundle)
            current['bundle_sha256'] = checksum
            current['status'] = ('ready' if bundle['model_response']['decision']
                                 == 'invoke_selected_skill' else 'needs_human')
            if self.on_proposal is not None and 'episode_id' in current:
                self.on_proposal(_copy(bundle))
        except asyncio.CancelledError:
            current['status'] = 'cancelled'
            raise
        except Exception:
            if current['status'] != 'cancelled':
                current['status'] = 'failed'
        finally:
            if guard_installed:
                if not had_guard:
                    delattr(self.planner, 'before_model_call')
                else:
                    self.planner.before_model_call = previous_guard
            if self.episode_learning is not None:
                self.episode_learning.planning_terminal(current)

    def _check_current(self, current, request, authority, evidence):
        if self.closed or self.current is not current or current['status'] != 'pending':
            raise ValueError('owned_skill_planning_cancelled')
        checked_authority, checked_evidence = self.prepare(
            request['case_key'], parse_owned_skill_goal(request['goal']),
            request['lease_id'], request['generation'])
        if canonical((checked_authority, checked_evidence)) != canonical((authority, evidence)):
            raise ValueError('owned_skill_planning_source_changed')
        if 'knowledge' in current:
            self.knowledge.check_current(current['knowledge']['intent']['preview'])

    def _open_directory(self, create=False):
        parent_fd = open_existing_workspace(self.directory.parent)
        descriptor = None
        try:
            parent = os.fstat(parent_fd)
            parent_identity = (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_mode)
            if (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700
                    or self.parent_identity is not None and self.parent_identity != parent_identity):
                raise ValueError('owned_skill_planning_parent_invalid')
            if create:
                try:
                    os.mkdir(self.directory.name, 0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
            descriptor = os.open(self.directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent_fd)
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode)
            current_fd = open_existing_workspace(self.directory)
            try:
                current = os.fstat(current_fd)
            finally:
                os.close(current_fd)
            linked = os.stat(self.directory.name, dir_fd=parent_fd, follow_symlinks=False)
            if (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700
                    or self.directory_identity is not None and identity != self.directory_identity
                    or any((info.st_dev, info.st_ino, info.st_uid, info.st_mode) != identity
                           for info in (current, linked))):
                raise ValueError('owned_skill_planning_directory_changed')
            self.parent_identity = parent_identity
            self.directory_identity = identity
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(parent_fd)

    def _persist(self, bundle):
        checksum = digest(bundle)
        descriptor = self._open_directory(create=True)
        try:
            _write_private_child(descriptor, checksum + '.json', canonical(bundle).encode())
        finally:
            os.close(descriptor)
        if self.load(checksum) != bundle:
            raise ValueError('owned_skill_planning_publication_changed')
        return checksum

    def load(self, checksum):
        if type(checksum) is not str or re.fullmatch(r'[a-f0-9]{64}', checksum) is None:
            raise ValueError('owned_skill_planning_hash_invalid')
        descriptor = self._open_directory()
        try:
            content = _read_private_child(descriptor, checksum + '.json', 131072)
        finally:
            os.close(descriptor)
        os.close(self._open_directory())
        try:
            bundle = json.loads(content)
            if canonical(bundle).encode() != content or digest(bundle) != checksum:
                raise ValueError('hash')
            return validate_planning_bundle(bundle, self.planner)
        except Exception:
            raise ValueError('owned_skill_planning_bundle_invalid') from None

    def select(self, checksum, lease_id, generation):
        if (self.closed or self.reserved or self.current is None
                or self.current['status'] not in {'ready', 'bound'}
                or self.current['bundle_sha256'] != checksum):
            raise ValueError('owned_skill_planning_not_ready')
        bundle = self.load(checksum)
        request = bundle['request']
        if (request['lease_id'] != lease_id or request['generation'] != generation
                or bundle['model_response']['decision'] != 'invoke_selected_skill'
                or bundle['real_model'] is not True):
            raise ValueError('owned_skill_planning_selection_changed')
        authority, evidence = self.prepare(request['case_key'],
            parse_owned_skill_goal(request['goal']), lease_id, generation)
        if (authority != bundle['authority'] or evidence != bundle['evidence']):
            raise ValueError('owned_skill_planning_source_changed')
        if 'knowledge' in bundle:
            if self.knowledge is None:
                raise ValueError('owned_skill_knowledge_unavailable')
            self.knowledge.check_binding(bundle)
        return bundle

    def bind(self, checksum, preview_sha256):
        if (self.current is None or self.current['bundle_sha256'] != checksum
                or self.current['status'] not in {'ready', 'bound'}
                or self.current.get('bound_preview_sha256', preview_sha256) != preview_sha256):
            raise ValueError('owned_skill_planning_binding_changed')
        self.current.update({'status': 'bound', 'bound_preview_sha256': preview_sha256})

    def consume(self, checksum, preview_sha256):
        if (self.current is None or self.current['status'] != 'bound'
                or self.current['bundle_sha256'] != checksum
                or self.current.get('bound_preview_sha256') != preview_sha256):
            raise ValueError('owned_skill_planning_confirmation_changed')
        self.current['status'] = 'consumed'

    async def cancel(self):
        current, task = self.current, self.task
        if current is not None and current['status'] in {'pending', 'ready', 'bound'}:
            current['status'] = 'cancelled'
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await task
        if self.task is task:
            self.task = None
        if self.episode_learning is not None:
            self.episode_learning.planning_terminal(current)

    async def close(self):
        self.closed = True
        await self.cancel()
