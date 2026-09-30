"""Explicit single-task adapter admission without changing the base engine."""

from .contracts import digest
from .owned_adapter_admission import validate_runtime_admission
from .owned_adapter_engine import OwnedAdapterDecisionEngine, PROTOCOL, runtime_worker_pin
from .owned_form_candidate_execution import load_candidate_execution_bundle


class OwnedAdapterRuntime:
    def __init__(self, learning):
        self.learning = learning
        self.active = None
        self.starting = None
        self._audit_scope = None

    def permits_audit(self, execution_sha256):
        return self._audit_scope is not None and self._audit_scope == execution_sha256

    def control(self, lease_id, generation, *, running=False):
        scheduler = self.learning.scheduler
        state = scheduler.controller.state()
        conflicting = (scheduler.sequences.reserved or scheduler.planning_reserved
                       or scheduler.adaptation_reserved or self.learning.preparation.reserved)
        if (scheduler.closed or scheduler.restart_quiesced or scheduler.paused
                or conflicting or scheduler.reserved and not running
                or state['owner'] != 'AGENT' or state['status'] != 'running'
                or state['lease_id'] != lease_id or state['generation'] != generation
                or scheduler.busy and not running):
            raise ValueError('owned_adapter_runtime_control_changed')

    def preview(self, episode_id, authorization_sha256, case_key, development_value,
                lease_id, generation):
        self.control(lease_id, generation)
        scheduler = self.learning.scheduler
        report = self.learning.adaptation.inspect(episode_id, authorization_sha256)
        source = report['authorization']['candidate_execution_sha256']
        root = scheduler.remote_form_owned_candidate_session.directory / 'candidate-execution-bundles'
        bundle, _checksum = load_candidate_execution_bundle(root, source)
        manifest = bundle['manifest']
        arguments = {key: manifest[key] for key in (
            'candidate_sha256', 'source_run_ref', 'review_sha256', 'release_sha256',
            'selection_sha256', 'reuse_admission_sha256')}
        arguments.update({'invocation_sha256': manifest['source_invocation_sha256'],
                          'case_key': case_key, 'development_value': development_value})
        base_preview = scheduler.preview_owned_form_candidate_execution(**arguments)
        if base_preview['schema_version'] != '1.3':
            raise ValueError('owned_adapter_selected_manual_task_required')
        authorization = report['authorization']
        binding = {'protocol': PROTOCOL, 'authorization_sha256': authorization_sha256,
                   'adaptation_report_sha256': digest(report), 'artifact_sha256': report['artifact_sha256'],
                   'input_sha256': authorization['input_sha256'],
                   'deployment_manifest_sha256': authorization['deployment_manifest_sha256'],
                   'base_deployment_id': authorization['deployment_id'],
                   'runtime_worker_sha256': runtime_worker_pin()}
        admission = {'schema_version': '1.0', 'mode': 'owned_adapter_runtime_admission',
                     'episode_id': episode_id, 'authorization_sha256': authorization_sha256,
                     'adaptation_report_sha256': digest(report), 'adapter_sha256': report['artifact_sha256'],
                     'runtime_binding': binding, 'adapter_deployment_id': 'owned-adapter-' + digest(binding),
                     'base_preview_sha256': base_preview['preview_sha256'], 'case_key': case_key,
                     'development_value_sha256': digest({'value': development_value}),
                     'lease_id': lease_id, 'generation': generation,
                     'experimental_runtime_authorized': True, 'automatic_fallback': False,
                     'promotion_authorized': False, 'training_ready': False, 'synthetic': True}
        validate_runtime_admission(admission)
        final_preview = {key: value for key, value in base_preview.items() if key != 'preview_sha256'}
        final_preview.update({'schema_version': '1.5', 'adapter_admission_sha256': digest(admission)})
        final_preview['preview_sha256'] = digest(final_preview)
        return {'admission': admission, 'adapter_admission_sha256': digest(admission),
                'preview': final_preview, 'arguments': arguments, 'started': False}

    def start(self, episode_id, authorization_sha256, case_key, development_value,
              lease_id, generation, confirm_sha256, experimental_runtime_authorized):
        if experimental_runtime_authorized is not True:
            raise ValueError('owned_adapter_runtime_separate_consent_required')
        preview = self.preview(episode_id, authorization_sha256, case_key, development_value,
                               lease_id, generation)
        if preview['preview']['preview_sha256'] != confirm_sha256:
            raise ValueError('owned_adapter_runtime_exact_confirmation_required')
        admission = preview['admission']
        report = self.learning.adaptation.inspect(episode_id, authorization_sha256)
        self.active = {'admission': admission, 'job_id': None,
                       'source_execution': report['authorization']['candidate_execution_sha256']}
        self.starting = (digest(admission), confirm_sha256, lease_id, generation)
        try:
            result = self.learning.scheduler.start_owned_form_candidate_execution(
                **preview['arguments'], preview_sha256=confirm_sha256, confirm_sha256=confirm_sha256,
                lease_id=lease_id, generation=generation, adapter_admission_sha256=digest(admission),
                adapter_admission=admission)
            self.active['job_id'] = result['job_id']
            return result
        except Exception:
            self.active = None
            raise
        finally:
            self.starting = None

    def validate(self, admission):
        validate_runtime_admission(admission)
        scheduler = self.learning.scheduler
        active = self.active
        if active is None or active['admission'] != admission:
            raise ValueError('owned_adapter_runtime_task_binding_changed')
        running = scheduler.busy
        if running:
            execution = scheduler._active_owned_candidate_execution
            if (not active['job_id'] or scheduler.job_id != active['job_id']
                    or execution is None or execution.get('job_id') != active['job_id']
                    or execution.get('adapter_admission_sha256') != digest(admission)
                    or execution.get('candidate_execution_sha256') == active['source_execution']):
                raise ValueError('owned_adapter_runtime_active_job_changed')
        elif self.starting is None:
            raise ValueError('owned_adapter_runtime_not_starting')
        self.control(admission['lease_id'], admission['generation'], running=running)
        previous = self._audit_scope
        try:
            self._audit_scope = active['source_execution']
            report = self.learning.adaptation.inspect(admission['episode_id'], admission['authorization_sha256'])
        finally:
            self._audit_scope = previous
        binding = admission['runtime_binding']
        if (digest(report) != admission['adaptation_report_sha256']
                or report['artifact_sha256'] != admission['adapter_sha256']
                or report['authorization']['candidate_execution_sha256'] != active['source_execution']
                or binding['runtime_worker_sha256'] != runtime_worker_pin()
                or binding['base_deployment_id'] != scheduler.engine.identity['deployment_id']
                or binding['input_sha256'] != report['authorization']['input_sha256']
                or binding['deployment_manifest_sha256'] != report['authorization']['deployment_manifest_sha256']):
            raise ValueError('owned_adapter_runtime_source_changed')

    def engine_for_job(self, job_id):
        if self.active is None or self.active['job_id'] != job_id:
            raise ValueError('owned_adapter_runtime_job_missing')
        admission = self.active['admission']
        self.validate(admission)
        with self.learning.adaptation.directory(admission['episode_id'], admission['authorization_sha256']) as (_, path):
            return OwnedAdapterDecisionEngine(self.learning.scheduler.engine, path / 'candidate.bin',
                admission['runtime_binding'], validate_source=lambda: self.validate(admission))

    def audit(self, episode_id, authorization_sha256, candidate_execution_sha256):
        scheduler = self.learning.scheduler
        root = scheduler.remote_form_owned_candidate_session.directory / 'candidate-execution-bundles'
        bundle, _checksum = load_candidate_execution_bundle(root, candidate_execution_sha256)
        admission = validate_runtime_admission(bundle['adapter-admission'])
        if admission['episode_id'] != episode_id or admission['authorization_sha256'] != authorization_sha256:
            raise ValueError('owned_adapter_runtime_audit_selection_changed')
        result = scheduler.audit_owned_form_candidate_execution(candidate_execution_sha256)
        if result['schema_version'] != '1.5':
            raise ValueError('owned_adapter_runtime_audit_version_changed')
        try:
            report = self.learning.adaptation.inspect(episode_id, authorization_sha256)
            current = (digest(report) == admission['adaptation_report_sha256']
                       and report['artifact_sha256'] == admission['adapter_sha256'])
        except Exception:
            current = False
        result['current_source_status'] = 'available' if current else 'unavailable'
        return result
