"""Precommitted paired browser runs with explicit starts and historical proof."""

import json

from .contracts import canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .owned_adapter_pair_contract import pair_record, validate_pair, pair_observation
from .owned_episode_learning import HASH
from .owned_form_candidate_execution import load_candidate_execution_bundle
from .reusable_decider import ReusableDeciderEngine


ARMS = ('base', 'adapter')
KIND = 'model.owned_adapter_evaluation'


class PairedBaseEngine(ReusableDeciderEngine):
    def __init__(self, base, validate_source):
        super().__init__(base.manifest, base.python, timeout=base.timeout,
                         cpu_prewarm=False, idle_seconds=75, gpu_idle_seconds=0)
        self.validate_source = validate_source

    async def decide(self, state, options):
        self.validate_source()
        response = await super().decide(state, options)
        self.validate_source()
        return response


class OwnedAdapterEvaluation:
    def __init__(self, learning):
        self.learning = learning
        self.jobs = {}
        self._audit_scope = None

    def permits_audit(self, execution_sha256):
        return self._audit_scope is not None and self._audit_scope == execution_sha256

    def _name(self, pair_sha256, suffix=''):
        if type(pair_sha256) is not str or HASH.fullmatch(pair_sha256) is None:
            raise ValueError('owned_adapter_pair_hash_invalid')
        return 'pair-' + pair_sha256 + suffix + '.json'

    def _read(self, episode_id, pair_sha256, suffix=''):
        with self.learning.store.directory(episode_id) as descriptor:
            return self.learning.store._read(descriptor, self._name(pair_sha256, suffix))

    def _put(self, episode_id, pair_sha256, value, suffix=''):
        with self.learning.store.directory(episode_id) as descriptor:
            self.learning.store._put(descriptor, self._name(pair_sha256, suffix), value)

    def load(self, episode_id, pair_sha256):
        record = validate_pair(self._read(episode_id, pair_sha256))
        if digest(record) != pair_sha256 or record['episode_id'] != episode_id:
            raise ValueError('owned_adapter_pair_changed')
        return record

    def preview(self, episode_id, authorization_sha256, case_key, development_value,
                lease_id, generation):
        scheduler = self.learning.scheduler
        runtime = self.learning.adapter_runtime.preview(episode_id, authorization_sha256,
            case_key, development_value, lease_id, generation)
        record = pair_record(runtime, session_id=scheduler.controller.session_id,
            runtime_id=scheduler.controller.runtime.runtime_id, lease_id=lease_id, generation=generation)
        if scheduler._owned_selected_candidate_execution_session_starts > 2:
            raise ValueError('owned_adapter_pair_requires_two_remaining_starts')
        for arm in ARMS:
            scheduler._assert_owned_candidate_execution_start_available(record[arm + '_preview']['preview_sha256'], True)
        source = self.learning.adaptation.inspect(episode_id, authorization_sha256)['authorization']
        root = scheduler.remote_form_owned_candidate_session.directory / 'candidate-execution-bundles'
        trained, _checksum = load_candidate_execution_bundle(root, source['candidate_execution_sha256'])
        if trained['manifest']['parameter_variant_sha256'] == record['base_preview']['parameter_variant_sha256']:
            raise ValueError('owned_adapter_pair_requires_new_development_input')
        return {'schema_version': '1.0', 'pair_sha256': digest(record), 'record': record,
                'committed': False, 'started': False}

    def commit(self, *, confirm_sha256, experimental_evaluation_authorized, **arguments):
        if experimental_evaluation_authorized is not True:
            raise ValueError('owned_adapter_pair_separate_consent_required')
        preview = self.preview(**arguments)
        if preview['pair_sha256'] != confirm_sha256:
            raise ValueError('owned_adapter_pair_confirmation_changed')
        self._put(arguments['episode_id'], confirm_sha256, preview['record'])
        return {**preview, 'committed': True}

    def _authority(self, record, lease_id, generation, *, running=False):
        scheduler = self.learning.scheduler
        expected = {'session_id': scheduler.controller.session_id,
                    'runtime_id': scheduler.controller.runtime.runtime_id,
                    'lease_id': lease_id, 'generation': generation}
        if record['authority'] != expected:
            raise ValueError('owned_adapter_pair_authority_changed')
        self.learning.adapter_runtime.control(lease_id, generation, running=running)

    def _source(self, record):
        from .owned_adapter_engine import runtime_worker_pin

        report = self.learning.adaptation.inspect(record['episode_id'], record['authorization_sha256'])
        admission = record['adapter_admission']
        if (digest(report) != admission['adaptation_report_sha256']
                or report['artifact_sha256'] != admission['adapter_sha256']
                or self.learning.scheduler.engine.identity['deployment_id'] != record['base_deployment_id']
                or admission['runtime_binding']['runtime_worker_sha256'] != runtime_worker_pin()):
            raise ValueError('owned_adapter_pair_source_changed')
        return report

    def start(self, episode_id, pair_sha256, arm, lease_id, generation,
              confirm_sha256, experimental_runtime_authorized):
        if arm not in ARMS or experimental_runtime_authorized is not True or confirm_sha256 != pair_sha256:
            raise ValueError('owned_adapter_pair_explicit_arm_required')
        record = self.load(episode_id, pair_sha256)
        self._authority(record, lease_id, generation)
        source = self._source(record)
        scheduler = self.learning.scheduler
        if arm == 'base':
            if scheduler._owned_selected_candidate_execution_session_starts > 2:
                raise ValueError('owned_adapter_pair_requires_two_remaining_starts')
        else:
            previous = self.report(episode_id, pair_sha256)
            if previous['arms']['base']['status'] != 'verified':
                raise ValueError('owned_adapter_pair_base_not_verified')
        try:
            self._read(episode_id, pair_sha256, '-' + arm + '-attempt')
        except FileNotFoundError:
            pass
        else:
            raise ValueError('owned_adapter_pair_arm_already_attempted')
        current = self.learning.adapter_runtime.preview(episode_id, record['authorization_sha256'],
            record['arguments']['case_key'], record['arguments']['development_value'], lease_id, generation)
        checked = pair_record(current, **record['authority'])
        if checked != record:
            raise ValueError('owned_adapter_pair_preview_changed')
        attempt = {'pair_sha256': pair_sha256, 'arm': arm, 'created_at': now()}
        self._put(episode_id, pair_sha256, attempt, '-' + arm + '-attempt')
        if arm == 'adapter':
            result = self.learning.adapter_runtime.start(episode_id, record['authorization_sha256'],
                record['arguments']['case_key'], record['arguments']['development_value'], lease_id, generation,
                record['adapter_preview']['preview_sha256'], True)
        else:
            preview_sha256 = record['base_preview']['preview_sha256']
            result = scheduler.start_owned_form_candidate_execution(**record['arguments'],
                preview_sha256=preview_sha256, confirm_sha256=preview_sha256,
                lease_id=lease_id, generation=generation)
        receipt = {'pair_sha256': pair_sha256, 'arm': arm, 'job_id': result['job_id'],
                   'candidate_execution_sha256': result['candidate_execution_sha256']}
        self.jobs[result['job_id']] = {'record': record, 'receipt': receipt,
            'attempt': attempt, 'source_execution': source['authorization']['candidate_execution_sha256']}
        try:
            self._put(episode_id, pair_sha256, receipt, '-' + arm + '-started')
        except BaseException:
            scheduler.task.cancel()
            raise
        return {'schema_version': '1.0', **receipt, 'accepted': True}

    def validate_job(self, job_id):
        active = self.jobs.get(job_id)
        if active is None:
            raise ValueError('owned_adapter_pair_job_missing')
        record, receipt = active['record'], active['receipt']
        scheduler = self.learning.scheduler
        execution = scheduler._active_owned_candidate_execution
        if (scheduler.job_id != job_id or not scheduler.busy
                or execution is None or execution.get('job_id') != job_id
                or execution.get('candidate_execution_sha256') != receipt['candidate_execution_sha256']
                or receipt['candidate_execution_sha256'] == active['source_execution']
                or self.load(record['episode_id'], receipt['pair_sha256']) != record
                or self._read(record['episode_id'], receipt['pair_sha256'], '-' + receipt['arm'] + '-started') != receipt
                or self._read(record['episode_id'], receipt['pair_sha256'], '-' + receipt['arm'] + '-attempt') != active['attempt']):
            raise ValueError('owned_adapter_pair_job_changed')
        self._authority(record, record['authority']['lease_id'], record['authority']['generation'], running=True)
        previous = self._audit_scope
        try:
            self._audit_scope = active['source_execution']
            self._source(record)
        finally:
            self._audit_scope = previous

    def base_engine(self, job_id):
        self.validate_job(job_id)
        active = self.jobs[job_id]
        if active['receipt']['arm'] != 'base':
            raise ValueError('owned_adapter_pair_base_arm_required')
        base = self.learning.scheduler.engine
        engine = PairedBaseEngine(base, lambda: self.validate_job(job_id))
        if engine.identity != base.identity:
            raise ValueError('owned_adapter_pair_base_identity_changed')
        return engine

    def created(self, job_id, state):
        self.validate_job(job_id)
        active = self.jobs[job_id]
        receipt = active['receipt']
        payload = pair_observation(active['record'], receipt['pair_sha256'], receipt['arm'],
            job_id=job_id, run_id=state.run_id, step_id=state.step_id,
            candidate_execution_sha256=receipt['candidate_execution_sha256'])
        if state.deployment_id != payload['deployment_id']:
            raise ValueError('owned_adapter_pair_engine_changed')
        self.learning.scheduler.store.insert('observations', observation_id=identifier('observation'),
            run_id=state.run_id, step_id=state.step_id, action_id=None,
            kind=KIND, payload_json=canonical(payload), created_at=now())

    def _arm_report(self, record, pair_sha256, arm, frozen):
        snapshot, _identity = frozen
        try:
            attempt = self._read(record['episode_id'], pair_sha256, '-' + arm + '-attempt')
        except FileNotFoundError:
            return {'status': 'not_started'}
        if set(attempt) != {'pair_sha256', 'arm', 'created_at'} or attempt['pair_sha256'] != pair_sha256 or attempt['arm'] != arm:
            raise ValueError('owned_adapter_pair_attempt_changed')
        try:
            receipt = self._read(record['episode_id'], pair_sha256, '-' + arm + '-started')
        except FileNotFoundError:
            return {'status': 'attempted_without_receipt'}
        if (set(receipt) != {'pair_sha256', 'arm', 'job_id', 'candidate_execution_sha256'}
                or receipt['pair_sha256'] != pair_sha256 or receipt['arm'] != arm):
            raise ValueError('owned_adapter_pair_receipt_changed')
        task = snapshot.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (receipt['job_id'],)).fetchone()
        if task is None:
            raise ValueError('owned_adapter_pair_task_missing')
        authority = record['authority']
        if any(task[key] != authority[key] for key in ('session_id', 'lease_id', 'generation')):
            raise ValueError('owned_adapter_pair_task_authority_changed')
        if task['status'] != 'succeeded':
            return {'status': task['status'], 'job_id': receipt['job_id'], 'run_id': task['run_id']}
        scheduler = self.learning.scheduler
        bundle, _checksum = load_candidate_execution_bundle(
            scheduler.remote_form_owned_candidate_session.directory / 'candidate-execution-bundles',
            receipt['candidate_execution_sha256'])
        manifest, completion = bundle['manifest'], bundle['completion']
        reuse = bundle['reuse-admission']
        preview = record[arm + '_preview']
        if (manifest['preview_sha256'] != preview['preview_sha256']
                or manifest['schema_version'] != preview['schema_version']
                or completion['job_id'] != receipt['job_id'] or completion['run_id'] != task['run_id']
                or reuse['runtime_id'] != authority['runtime_id']
                or reuse['desktop_session_id'] != authority['session_id']
                or any(reuse[key] != authority[key] for key in ('lease_id', 'generation'))):
            raise ValueError('owned_adapter_pair_execution_changed')
        audited = scheduler.audit_owned_form_candidate_execution(receipt['candidate_execution_sha256'],
                                                                 _audited_snapshot=frozen)
        if audited.get('available') is not True or audited.get('schema_version') != preview['schema_version']:
            raise ValueError('owned_adapter_pair_execution_unverified')
        observations = snapshot.execute('SELECT * FROM observations WHERE run_id=? AND kind=?',
                                        (task['run_id'], KIND)).fetchall()
        first = snapshot.execute('SELECT * FROM state_snapshots WHERE run_id=? ORDER BY state_version LIMIT 1',
                                 (task['run_id'],)).fetchone()
        if len(observations) != 1 or first is None:
            raise ValueError('owned_adapter_pair_admission_missing')
        state = json.loads(first['state_json'])
        expected = pair_observation(record, pair_sha256, arm, job_id=receipt['job_id'],
            run_id=task['run_id'], step_id=state['step_id'],
            candidate_execution_sha256=receipt['candidate_execution_sha256'])
        observation = observations[0]
        from .site_skill_form_invocation_audit import _timestamp
        marker = _timestamp(observation['created_at'])
        if (observation['payload_json'] != canonical(expected) or observation['action_id'] is not None
                or observation['step_id'] != state['step_id'] or state['phase'] != 'CREATED'
                or state['runtime_id'] != task['runtime_id'] or state['owner_lease_id'] != authority['lease_id']
                or not _timestamp(attempt['created_at']) <= _timestamp(task['created_at']) <= _timestamp(first['created_at']) <= marker):
            raise ValueError('owned_adapter_pair_admission_changed')
        for table in ('model_calls', 'actions'):
            rows = snapshot.execute('SELECT created_at FROM ' + table + ' WHERE run_id=?', (task['run_id'],)).fetchall()
            if not rows or any(_timestamp(row['created_at']) < marker for row in rows):
                raise ValueError('owned_adapter_pair_admission_late')
        return {'status': 'verified', **receipt, 'run_id': task['run_id'], 'report_sha256': audited['report_sha256']}

    def report(self, episode_id, pair_sha256):
        record = self.load(episode_id, pair_sha256)
        scheduler = self.learning.scheduler
        if scheduler.busy or scheduler.reserved:
            raise ValueError('owned_adapter_pair_report_requires_idle')
        with audit_snapshot(scheduler.settings.database) as frozen:
            arms = {arm: self._arm_report(record, pair_sha256, arm, frozen) for arm in ARMS}
            verified = all(value['status'] == 'verified' for value in arms.values())
            comparison = None
            if verified:
                from .owned_adapter_comparison import compare_adapter_runs
                from .site_skill_form_invocation_audit import _timestamp

                base = frozen[0].execute('SELECT updated_at FROM desktop_tasks WHERE job_id=?',
                                         (arms['base']['job_id'],)).fetchone()
                adapter = self._read(episode_id, pair_sha256, '-adapter-attempt')
                if _timestamp(base['updated_at']) > _timestamp(adapter['created_at']):
                    raise ValueError('owned_adapter_pair_order_changed')
                comparison = compare_adapter_runs(frozen[0], arms['base']['run_id'], arms['adapter']['run_id'],
                    base_deployment_id=record['base_deployment_id'], adapter_deployment_id=record['adapter_deployment_id'])
        result = {'schema_version': '1.0', 'synthetic': True, 'pair_sha256': pair_sha256, 'verified': verified,
                'arms': arms, 'comparison': comparison, 'scope': 'development_only',
                'independent_held_out': 0, 'promotion_authorized': False, 'runtime_reuse_authorized': False}
        from .owned_episode_learning import _validate

        _validate('owned_adapter_pair_report', result)
        return result
