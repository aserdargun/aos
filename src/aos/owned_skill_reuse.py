"""Read-only reacceptance of a stopped owned learning source, never task replay."""

from contextlib import ExitStack
import os
from pathlib import Path
import re
import stat

from .contracts import REPO_ROOT, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .lifecycle import observe_process, private_directory, read_journal
from .owned_candidate_review import (
    inspect_owned_candidate_review, make_owned_candidate_review)
from .owned_form_candidate_execution import (
    audit_persisted_candidate_execution, load_candidate_execution_bundle,
    load_candidate_execution_replay_inventory)
from .owned_skill_release import (
    current_owned_skill_selection, load_owned_skill_release, make_owned_skill_release)
from .site_knowledge import SiteKnowledgeStore
from .site_skill_form_invocation_audit import audit_site_skill_form_invocation_execution
from .site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from .web_application import WebApplicationProfiles
from .workspace_identity import open_existing_workspace, workspace_identity


_HASH = re.compile(r'[a-f0-9]{64}\Z')
_SESSION = re.compile(r'app-[a-f0-9]{32}\Z')
_LIMIT = 10000


def stable_source_audit_sha256(report):
    if report.get('status') != 'fixed_template_invocation_execution_verified':
        raise ValueError('owned_skill_reuse_source_audit_invalid')
    return digest({key: value for key, value in report.items()
                   if key != 'source_snapshot_sha256'})


def stable_review_evidence_audit_sha256(report):
    if report.get('status') != 'source_bound_development_execution':
        raise ValueError('owned_skill_reuse_evidence_audit_invalid')
    return digest({key: value for key, value in report.items()
                   if key not in {'source_snapshot_sha256', 'recipe_audit_sha256'}})


def _paths(previous_session_directory, previous_state, owned_root, database):
    previous_session_directory = Path(previous_session_directory).absolute()
    owned_root, database = Path(owned_root).absolute(), Path(database).absolute()
    base = previous_session_directory.parent
    project = getattr(previous_state, 'project', None)
    project_base = re.fullmatch(r'local-app-project-([a-z0-9][a-z0-9-]{0,47})', base.name)
    source_session = (getattr(previous_state, 'owned_skill_source_session', None)
                      or previous_state.session)
    if (base.parent != REPO_ROOT / 'data'
            or (project_base is not None and project != project_base.group(1))
            or (project_base is None and project is not None)
            or not (base.name == 'local-app-v1'
                    or re.fullmatch(r'local-app-test-[a-f0-9]{32}', base.name)
                    or re.fullmatch(r'local-app-project-[a-z0-9][a-z0-9-]{0,47}', base.name))
            or previous_session_directory.name != previous_state.session
            or _SESSION.fullmatch(source_session) is None
            or owned_root != base / source_session / 'owned-form'
            or database != base / source_session / 'store.sqlite'):
        raise ValueError('owned_skill_reuse_source_paths_invalid')
    return previous_session_directory, owned_root, database


def _unchanged_directory(path, descriptor, before):
    reopened = open_existing_workspace(path)
    try:
        after, linked = os.fstat(descriptor), os.fstat(reopened)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, field) != getattr(info, field)
               for info in (after, linked) for field in fields):
            raise ValueError('owned_skill_reuse_directory_changed')
    finally:
        os.close(reopened)


def _stopped_source(previous_session_directory, previous_state):
    if (previous_state.phase != 'stopped' or previous_state.backend is None
            or previous_state.token_name is not None or previous_state.mode != 'real'
            or not previous_state.owned_synthetic_form_invocation
            or previous_state.owned_synthetic_form_recipe):
        raise ValueError('owned_skill_reuse_stopped_source_required')
    for process in (previous_state.supervisor, previous_state.backend):
        if observe_process(process) not in {
                'not_observed', 'different_process', 'different_boot'}:
            raise ValueError('owned_skill_reuse_source_process_not_stopped')
    with ExitStack() as stack:
        directory_fd = private_directory(previous_session_directory)
        stack.callback(os.close, directory_fd)
        directory_before = os.fstat(directory_fd)
        names = os.listdir(directory_fd)
        if len(names) > _LIMIT or any(
                re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', name)
                for name in names):
            raise ValueError('owned_skill_reuse_source_token_remaining')
        workspace = previous_session_directory / 'workspace'
        workspace_fd = private_directory(workspace)
        stack.callback(os.close, workspace_fd)
        workspace_before = os.fstat(workspace_fd)
        expected_workspace = workspace_identity(workspace, workspace_fd)
        journals = previous_session_directory / '.aos-lifecycle'
        journal_fd = private_directory(journals)
        stack.callback(os.close, journal_fd)
        journal_before = os.fstat(journal_fd)
        names = sorted(os.listdir(journal_fd))
        if not 1 <= len(names) <= _LIMIT or any(
                re.fullmatch(r'desktop-[a-f0-9]{32}\.jsonl', name) is None
                for name in names):
            raise ValueError('owned_skill_reuse_lifecycle_inventory_invalid')
        records, runtimes, containers = [], set(), set()
        for name in names:
            events, checksum = read_journal(journals / name)
            birth, terminal = events[0].birth, events[-1]
            if (birth.process != previous_state.backend
                    or birth.workspace != expected_workspace
                    or terminal.stage != 'removed' or terminal.container_id is None
                    or terminal.container_id in containers):
                raise ValueError('owned_skill_reuse_lifecycle_not_stopped')
            containers.add(terminal.container_id)
            runtimes.add(birth.runtime_id)
            records.append({'filename': name, 'sha256': checksum,
                            'birth': birth.model_dump(mode='json')})
        for path, descriptor, before in (
                (previous_session_directory, directory_fd, directory_before),
                (workspace, workspace_fd, workspace_before),
                (journals, journal_fd, journal_before)):
            _unchanged_directory(path, descriptor, before)
    return digest(records), runtimes


def _terminal_sessions(snapshot, previous_runtimes):
    sessions = [dict(row) for row in snapshot.execute(
        'SELECT * FROM desktop_sessions ORDER BY session_id LIMIT ?', (_LIMIT + 1,))]
    if (not sessions or len(sessions) > _LIMIT
            or any(row['status'] != 'stopped' for row in sessions)
            or not previous_runtimes.intersection(row['runtime_id'] for row in sessions)):
        raise ValueError('owned_skill_reuse_prior_sessions_not_stopped')
    checks = (
        ('desktop_tasks', "status NOT IN ('succeeded','failed','cancelled')"),
        ('runs', "status NOT IN ('succeeded','failed','cancelled')"),
        ('actions', "status IN ('intent','running','uncertain')"),
        ('desktop_approvals', "status IN ('pending','approved')"),
        ('desktop_inputs', "status IN ('queued','running','uncertain')"),
    )
    for table, predicate in checks:
        if snapshot.execute(f'SELECT 1 FROM {table} WHERE {predicate} LIMIT 1').fetchone():
            raise ValueError('owned_skill_reuse_unresolved_' + table)
    return sessions


def _successful_job(snapshot, run_id):
    rows = snapshot.execute('SELECT * FROM desktop_tasks WHERE run_id=?', (run_id,)).fetchall()
    run = snapshot.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if (len(rows) != 1 or rows[0]['kind'] != 'browser_remote_form'
            or rows[0]['status'] != 'succeeded' or rows[0]['real_model'] != 1
            or run is None or run['status'] != 'succeeded'):
        raise ValueError('owned_skill_reuse_source_job_invalid')
    return dict(rows[0])


def _selected_release(owned_root, release_sha256, selection_sha256):
    release = load_owned_skill_release(owned_root, release_sha256)
    head, selection = current_owned_skill_selection(owned_root, release['family_sha256'])
    if (head is None or selection is None
            or head['selection_sha256'] != selection_sha256
            or digest(selection) != selection_sha256
            or selection['release_sha256'] != release_sha256):
        raise ValueError('owned_skill_reuse_selection_changed')
    review = inspect_owned_candidate_review(owned_root, release['review_sha256'])
    if review['status'] != 'accepted':
        raise ValueError('owned_skill_reuse_review_not_accepted')
    return release, review['receipt']


def preview_owned_skill_reuse(*, previous_session_directory: Path, previous_state,
                             owned_root: Path, database: Path, manifest_sha256: str,
                             release_sha256: str, selection_sha256: str):
    """Audit before creating a new controller; callers hold the learning workspace lock.

    The private context is not a portable authorization. A fresh manager must repeat
    this audit and confirm the exact public preview before its runtime admission.
    No previous task, approval, quota, or runtime authority is restored here.
    """
    if any(_HASH.fullmatch(value or '') is None for value in (
            manifest_sha256, release_sha256, selection_sha256)):
        raise ValueError('owned_skill_reuse_pin_invalid')
    previous_session_directory, owned_root, database = _paths(
        previous_session_directory, previous_state, owned_root, database)
    if previous_state.owned_form_manifest_sha256 != manifest_sha256:
        raise ValueError('owned_skill_reuse_manifest_changed')
    lifecycle_sha256, previous_runtimes = _stopped_source(
        previous_session_directory, previous_state)
    with ExitStack() as stack:
        root_fd = private_directory(owned_root)
        stack.callback(os.close, root_fd)
        root_before = os.fstat(root_fd)
        parent_fd = private_directory(database.parent)
        stack.callback(os.close, parent_fd)
        database_fd = os.open(database.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=parent_fd)
        stack.callback(os.close, database_fd)
        database_before = os.fstat(database_fd)
        if (not stat.S_ISREG(database_before.st_mode)
                or database_before.st_uid != os.getuid() or database_before.st_nlink != 1
                or stat.S_IMODE(database_before.st_mode) != 0o600):
            raise ValueError('owned_skill_reuse_database_identity_invalid')
        release, receipt = _selected_release(owned_root, release_sha256, selection_sha256)
        profiles = WebApplicationProfiles(owned_root / 'profiles')
        candidate_session = OwnedSiteSkillFormRecipeCandidateSession(
            directory=owned_root, manifest_sha256=manifest_sha256,
            candidate_directory=owned_root / 'site-skill-recipe-candidates',
            database=database, profiles=profiles,
            pages=SiteKnowledgeStore(owned_root / 'site-knowledge', profiles))
        candidate, checked = candidate_session.reinspect(
            receipt['source_run_ref'], receipt['source_invocation_sha256'],
            release['candidate_sha256'])
        candidate, source = candidate_session.execution_source(
            candidate['source_run_id'], receipt['source_invocation_sha256'], checked)
        if (source['manifest']['mode'] != 'owned_synthetic_form_invocation'
                or source['invocation_sha256'] != previous_state.owned_form_invocation_sha256):
            raise ValueError('owned_skill_reuse_source_invocation_changed')
        parent = (None if release['parent_release_sha256'] is None
                  else load_owned_skill_release(owned_root, release['parent_release_sha256']))
        reconstructed_sha256, reconstructed = make_owned_skill_release(
            candidate, checked, release['review_sha256'], receipt, parent_release=parent)
        if reconstructed_sha256 != release_sha256 or reconstructed != release:
            raise ValueError('owned_skill_reuse_release_changed')
        bundle_root = owned_root / 'candidate-execution-bundles'
        bundle, _bundle_sha256 = load_candidate_execution_bundle(
            bundle_root, receipt['candidate_execution_sha256'])
        completion, manifest = bundle.get('completion'), bundle['manifest']
        if completion is None or manifest.get('review_sha256') is not None:
            raise ValueError('owned_skill_reuse_evidence_unavailable')
        with audit_snapshot(database) as audited:
            snapshot, _identity = audited
            prior_sessions = _terminal_sessions(snapshot, previous_runtimes)
            source_job = _successful_job(snapshot, candidate['source_run_id'])
            _successful_job(snapshot, completion['run_id'])
            source_audit = audit_site_skill_form_invocation_execution(
                source['skill_store'], source['plan'], source['inputs'],
                source['invocation'].case_key, profiles, source['task'], source['form_plan'],
                source['state_plan'], list(source['bindings']), source['invocation'],
                database, candidate['source_run_id'], _audited_snapshot=audited)
            evidence_audit = audit_persisted_candidate_execution(
                bundle_root, receipt['candidate_execution_sha256'],
                candidate_session=candidate_session, database=database,
                _audited_snapshot=audited)
        execution = {
            'candidate_execution_sha256': receipt['candidate_execution_sha256'],
            'run_ref': completion['run_ref'],
            'invocation_sha256': manifest['invocation_sha256'],
            'recipe_sha256': manifest['recipe_sha256'],
            'skill_sha256': bundle['invocation']['skill_sha256'],
            'profile_sha256': candidate['profile_sha256'],
            'case_key': bundle['invocation']['case_key'],
            'parameter_variant_sha256': manifest['parameter_variant_sha256'],
        }
        current_review_sha256, current_receipt = make_owned_candidate_review(
            candidate, checked, execution)
        if (current_review_sha256 != release['review_sha256'] or current_receipt != receipt
                or source_audit['run_ref'] != candidate['source_run_ref']
                or source_audit['invocation_sha256'] != receipt['source_invocation_sha256']
                or evidence_audit['source_run_ref'] != candidate['source_run_ref']
                or evidence_audit['execution_run_ref'] != completion['run_ref']):
            raise ValueError('owned_skill_reuse_audited_provenance_changed')
        inventory = load_candidate_execution_replay_inventory(
            owned_root, source_run_ref=candidate['source_run_ref'],
            source_invocation_sha256=source['invocation_sha256'],
            source_manifest_sha256=manifest_sha256, allow_missing=False)
        if _selected_release(owned_root, release_sha256, selection_sha256) != (release, receipt):
            raise ValueError('owned_skill_reuse_selection_changed')
        if _stopped_source(previous_session_directory, previous_state) != (
                lifecycle_sha256, previous_runtimes):
            raise ValueError('owned_skill_reuse_lifecycle_changed')
        _unchanged_directory(owned_root, root_fd, root_before)
        database_after = os.fstat(database_fd)
        linked = os.stat(database, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(database_before, field) != getattr(info, field)
               for info in (database_after, linked) for field in fields):
            raise ValueError('owned_skill_reuse_database_changed')
        preview = {
            'schema_version': '1.0', 'synthetic': True,
            'purpose': 'owned_selected_skill_reuse',
            'previous_manager_session': previous_state.session,
            'previous_stopped_state_sha256': digest(previous_state.model_dump(mode='json')),
            'previous_lifecycle_sha256': lifecycle_sha256,
            'source_workspace_identity': workspace_identity(owned_root, root_fd).model_dump(),
            'source_database_identity': {
                'path_sha256': digest({'database': str(database)}),
                'device': database_before.st_dev, 'inode': database_before.st_ino,
                'owner_uid': database_before.st_uid},
            'source_manifest_sha256': manifest_sha256,
            'source_job_id': source_job['job_id'],
            'source_run_ref': candidate['source_run_ref'],
            'source_invocation_sha256': source['invocation_sha256'],
            'source_desktop_session_id': source_job['session_id'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'family_sha256': release['family_sha256'], 'release_sha256': release_sha256,
            'selection_sha256': selection_sha256, 'review_sha256': release['review_sha256'],
            'candidate_sha256': checked, 'recipe_sha256': release['recipe_sha256'],
            'review_evidence_execution_sha256': receipt['candidate_execution_sha256'],
            'source_audit_sha256': stable_source_audit_sha256(source_audit),
            'review_evidence_audit_sha256': stable_review_evidence_audit_sha256(evidence_audit),
            'replay_inventory_sha256': digest({key: sorted(value)
                                               for key, value in inventory.items()}),
            'prior_sessions_sha256': digest(prior_sessions),
            'origin': source['manifest']['origin'],
            'certificate_sha256': source['manifest']['certificate_sha256'],
            'execution_authorized': False, 'old_actions_replayed': False,
            'activation_authorized': False, 'training_ready': False,
        }
        validator('owned_skill_reuse_preview').validate(preview)
        return {'preview': preview, 'preview_sha256': digest(preview), 'context': {
            'candidate_session': candidate_session, 'candidate': candidate, 'source': source,
            'source_job': source_job, 'source_run_id': candidate['source_run_id'],
            'source_run_ref': candidate['source_run_ref'],
            'source_invocation_sha256': source['invocation_sha256'],
            'source_desktop_session_id': source_job['session_id'],
            'source_audit': source_audit, 'review_evidence_audit': evidence_audit,
            'replay_inventory': inventory}}
