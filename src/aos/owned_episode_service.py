"""Scheduler integration for consented, non-training owned episode content."""

from .contracts import digest
from .dataset_audit import audit_snapshot
from .dataset_reviews import source_fingerprint
from .owned_episode_learning import FIXTURE_GROUP, ROLES, OwnedEpisodeStore, consent_record
from .owned_form_candidate_execution import load_candidate_execution_bundle


class OwnedEpisodeLearning:
    def __init__(self, scheduler, directory):
        from .owned_episode_adaptation import OwnedEpisodeAdaptation
        from .owned_episode_preparation import OwnedEpisodePreparation
        from .owned_adapter_runtime import OwnedAdapterRuntime
        from .owned_adapter_evaluation import OwnedAdapterEvaluation

        self.scheduler = scheduler
        self.store = OwnedEpisodeStore(directory)
        self.current = None
        self.preparation = OwnedEpisodePreparation(self)
        self.adaptation = OwnedEpisodeAdaptation(self)
        self.adapter_runtime = OwnedAdapterRuntime(self)
        self.adapter_evaluation = OwnedAdapterEvaluation(self)

    def begin(self, planning_id, request, authority):
        consent = consent_record(planning_id, request, authority)
        checksum = self.store.create(consent)
        self.current = {'episode_id': consent['episode_id'], 'state': 'collecting',
                        'counts': dict.fromkeys(ROLES, 0)}
        return {'episode_id': consent['episode_id'], 'collection_consent_sha256': checksum}

    def status(self):
        return {'available': True, 'training_ready': False,
                **(self.current or {'state': 'disabled', 'counts': dict.fromkeys(ROLES, 0)}),
                'preparation': self.preparation.status(), 'adaptation': self.adaptation.status()}

    def adaptation_preview(self, **arguments):
        return self.adaptation.preview(**arguments)

    def adaptation_start(self, **arguments):
        return self.adaptation.start(**arguments)

    def adaptation_inspect(self, **arguments):
        return self.adaptation.inspect(**arguments)

    def runtime_preview(self, **arguments):
        return self.adapter_runtime.preview(**arguments)

    def runtime_start(self, **arguments):
        return self.adapter_runtime.start(**arguments)

    def runtime_audit(self, **arguments):
        return self.adapter_runtime.audit(**arguments)

    def pair_preview(self, **arguments):
        return self.adapter_evaluation.preview(**arguments)

    def pair_commit(self, **arguments):
        return self.adapter_evaluation.commit(**arguments)

    def pair_start(self, **arguments):
        return self.adapter_evaluation.start(**arguments)

    def pair_report(self, **arguments):
        return self.adapter_evaluation.report(**arguments)

    def conversion_preview(self, episode_id, export_sha256):
        return self.preparation.preview(episode_id, export_sha256)

    def convert(self, episode_id, export_sha256, confirm_sha256):
        return self.preparation.publish(episode_id, export_sha256, confirm_sha256)

    def readiness(self, episode_id, conversion_sha256):
        return self.preparation.inspect(episode_id, conversion_sha256)

    def tokenizer_start(self, episode_id, conversion_sha256, confirm_sha256, lease_id, generation):
        return self.preparation.start(episode_id, conversion_sha256, confirm_sha256, lease_id, generation)

    def planning_terminal(self, planning):
        if (self.current is not None and planning is not None
                and self.current['state'] == 'collecting'
                and self.current['episode_id'] == planning.get('episode_id')
                and planning.get('status') in {'failed', 'cancelled', 'needs_human'}):
            self.current['state'] = planning['status']

    def proposed(self, bundle):
        from .owned_episode_candidates import derive_system2_candidate

        episode_id = bundle['episode_id']
        try:
            consent = self.store.assert_plan(episode_id, bundle)
            candidate = derive_system2_candidate(bundle, episode_id=episode_id,
                consent_sha256=digest(consent), source_group_sha256=FIXTURE_GROUP)
            self.store.refresh(episode_id, [candidate])
            self.current = {'episode_id': episode_id, 'state': 'collecting',
                            'counts': {'system1': 0, 'system2': 1}}
        except Exception:
            self.current = {'episode_id': episode_id, 'state': 'failed', 'counts': dict.fromkeys(ROLES, 0)}

    def bind(self, bundle, execution_sha256):
        episode_id = bundle['episode_id']
        self.store.assert_plan(episode_id, bundle)
        if self.current is None or self.current['episode_id'] != episode_id or self.current['state'] == 'failed':
            raise ValueError('owned_episode_collection_unavailable')
        self.store.bind_execution(episode_id, execution_sha256, digest(bundle))

    def poll(self, job_id):
        if self.current is None or self.current['state'] == 'failed':
            return
        active = self.scheduler._owned_candidate_execution
        if (active is None or active.get('job_id') != job_id
                or active.get('planning_bundle', {}).get('episode_id') != self.current['episode_id']):
            return
        if active.get('lifecycle') in {'completed', 'audited'} and self.scheduler.reserved:
            return
        try:
            self.inspect(self.current['episode_id'])
        except Exception:
            self.current['state'] = 'failed'

    def inspect(self, episode_id, *, read_only=False):
        from .owned_episode_candidates import derive_system1_candidates, derive_system2_candidate

        consent = self.store.consent(episode_id)
        try:
            selected = self.store.execution(episode_id)
        except FileNotFoundError:
            candidates = self.store.candidates(episode_id)
            if len(candidates) != 1 or candidates[0]['role'] != 'system2':
                raise ValueError('owned_episode_provisional_source_missing')
            checksum = candidates[0]['source']['planning_bundle_sha256']
            authority = consent['authority']
            bundle = self.scheduler.owned_skill_planning.select(
                checksum, authority['lease_id'], authority['generation'])
            self.store.assert_plan(episode_id, bundle)
            derived = derive_system2_candidate(bundle, episode_id=episode_id,
                consent_sha256=digest(consent), source_group_sha256=FIXTURE_GROUP)
            if candidates != [derived]:
                raise ValueError('owned_episode_provisional_source_changed')
            return {'schema_version': '1.0', 'episode_id': episode_id, 'available': True,
                    'reviewable': False, 'candidates': candidates,
                    'reviews': dict.fromkeys(ROLES), 'review_sha256': dict.fromkeys(ROLES),
                    'training_ready': False}
        session = self.scheduler.remote_form_owned_candidate_session
        loaded, _checksum = load_candidate_execution_bundle(
            session.directory / 'candidate-execution-bundles', selected['candidate_execution_sha256'])
        bundle = loaded['planning-bundle']
        self.store.assert_plan(episode_id, bundle)
        if (loaded['manifest']['schema_version'] != '1.4'
                or loaded['manifest']['planning_bundle_sha256'] != selected['planning_bundle_sha256']):
            raise ValueError('owned_episode_execution_changed')
        candidates = [derive_system2_candidate(bundle, episode_id=episode_id,
            consent_sha256=digest(consent), source_group_sha256=FIXTURE_GROUP)]
        with audit_snapshot(self.scheduler.settings.database) as (snapshot, identity):
            matches = snapshot.execute("SELECT * FROM desktop_tasks WHERE kind='browser_remote_form'").fetchall()
            execution = self.scheduler._owned_candidate_execution_history.get(selected['candidate_execution_sha256'])
            completion = loaded.get('completion')
            job_id = completion['job_id'] if completion else execution.get('job_id') if execution else None
            matching = [row for row in matches if row['job_id'] == job_id]
            if len(matching) != 1 or not matching[0]['run_id']:
                raise ValueError('owned_episode_execution_not_started')
            job = matching[0]
            if job['status'] in {'failed', 'cancelled', 'waiting_human'}:
                raise ValueError('owned_episode_execution_failed')
            if job['status'] != 'succeeded':
                self.scheduler.check_remote_form_binding(job['job_id'])
            candidates += derive_system1_candidates(snapshot, job['run_id'], episode_id=episode_id,
                consent_sha256=digest(consent), planning_bundle_sha256=digest(bundle),
                source_group_sha256=FIXTURE_GROUP)
            reviewable = False
            execution_source_sha256 = None
            if job['status'] == 'succeeded':
                audit = self.scheduler.audit_owned_form_candidate_execution(
                    selected['candidate_execution_sha256'], _audited_snapshot=(snapshot, identity))
                if (audit.get('available') is not True or audit.get('schema_version') != '1.4'
                        or audit.get('planning_admission_verified') is not True
                        or audit.get('review_status') != 'accepted'
                        or audit.get('planning_bundle_sha256') != digest(bundle)):
                    raise ValueError('owned_episode_verified_source_required')
                reviewable = True
                execution_source_sha256 = source_fingerprint(snapshot, job['run_id'])
        if not read_only:
            self.store.refresh(episode_id, candidates)
        stored = self.store.candidates(episode_id)
        if {digest(item) for item in stored} != {digest(item) for item in candidates}:
            raise ValueError('owned_episode_candidate_source_changed')
        counts = {role: sum(item['role'] == role for item in candidates) for role in ROLES}
        if self.current is None or self.current['episode_id'] == episode_id:
            self.current = {'episode_id': episode_id, 'state': 'reviewable' if reviewable else 'collecting', 'counts': counts}
        review_source = selected | {'execution_source_sha256': execution_source_sha256}
        selections = {role: digest(self.store.review_selection(episode_id, role,
            [item for item in candidates if item['role'] == role], review_source))
            if reviewable and counts[role] else None for role in ROLES}
        reviews = self.store.reviews(episode_id)
        return {'schema_version': '1.0', 'episode_id': episode_id, 'available': True,
                'reviewable': reviewable, 'candidates': candidates, 'review_sha256': selections,
                'reviews': {role: {key: value for key, value in record.items() if key != 'receipt'}
                            if record is not None else None for role, record in reviews.items()},
                'execution_source_sha256': execution_source_sha256,
                'training_ready': False}

    def review(self, episode_id, role, decision, confirm_sha256):
        inspected = self.inspect(episode_id)
        if not inspected['reviewable']:
            raise ValueError('owned_episode_successful_execution_required')
        selected = [item for item in inspected['candidates'] if item['role'] == role]
        source = self.store.execution(episode_id) | {'execution_source_sha256': inspected['execution_source_sha256']}
        self.store.review(episode_id, role, decision, selected, source, confirm_sha256)
        return self.inspect(episode_id)

    def revoke(self, episode_id, role, confirm_sha256):
        self.store.revoke(episode_id, role, confirm_sha256)
        try:
            inspected = self.inspect(episode_id)
        except Exception:
            reviews = self.store.reviews(episode_id)
            inspected = {'schema_version': '1.0', 'episode_id': episode_id, 'available': False,
                         'reviewable': False, 'candidates': [], 'review_sha256': dict.fromkeys(ROLES),
                         'reviews': {role: {key: value for key, value in record.items() if key != 'receipt'}
                                     if record is not None else None for role, record in reviews.items()},
                         'training_ready': False}
        return inspected | {'revocation_recorded': True}

    def export(self, episode_id, review_receipts):
        inspected = self.inspect(episode_id)
        if not inspected['reviewable']:
            raise ValueError('owned_episode_successful_execution_required')
        source = self.store.execution(episode_id) | {'execution_source_sha256': inspected['execution_source_sha256']}
        return self.store.export(episode_id, inspected['candidates'], source, review_receipts)
