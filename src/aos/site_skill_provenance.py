"""Read-only provenance checks for one selected manual site skill draft."""

import argparse
import json
from pathlib import Path
import re
import sqlite3

from .contracts import REPO_ROOT, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .learning_events import review_learning_events_snapshot
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillDraft, SiteSkillStore
from .web_application import WebApplicationProfiles


SHA256 = re.compile(r'[a-f0-9]{64}\Z')
MAX_SOURCE_REQUEST_BYTES = 64 * 1024


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, _message):
        self.exit(2, 'Site skill source inspection unavailable: invalid arguments.\n')


def inspect_site_skill_sources(database: Path, run_id: str, *, profiles: Path, pages: Path,
                               skills: Path, skill_sha256: str) -> dict:
    if not isinstance(skill_sha256, str) or SHA256.fullmatch(skill_sha256) is None:
        raise ValueError('invalid_site_skill_selection')
    profile_store = WebApplicationProfiles(profiles)
    page_store = SiteKnowledgeStore(pages, profile_store)
    skill = SiteSkillStore(skills, profile_store, page_store).get(skill_sha256)
    with audit_snapshot(database) as (snapshot, identity):
        return _inspect_site_skill_sources_snapshot(snapshot, identity, run_id, skill_sha256, skill)


def _inspect_site_skill_sources_snapshot(snapshot: sqlite3.Connection, identity: dict,
                                         run_id: str, skill_sha256: str,
                                         skill: SiteSkillDraft) -> dict:
    review = review_learning_events_snapshot(snapshot, identity, run_id)
    selected_ids = set(skill.source_event_ids)
    events = [event for event in review['events'] if event['event_id'] in selected_ids]
    if len(events) != len(selected_ids) or any(event['role'] != skill.model_role for event in events):
        raise ValueError('skill_source_event_missing_or_wrong_role')
    request_hashes = set()
    for event in events:
        call = snapshot.execute(
            'SELECT request_json FROM model_calls WHERE call_id=? AND run_id=? AND step_id=? AND role=? AND status=?',
            (event['source']['call_id'], run_id, event['source']['step_id'],
             skill.model_role, 'ok')).fetchone()
        if (call is None or not isinstance(call['request_json'], str)
                or len(call['request_json'].encode()) > MAX_SOURCE_REQUEST_BYTES):
            raise ValueError('skill_source_request_unavailable')
        request = json.loads(call['request_json'])
        if not isinstance(request, dict) or canonical(request) != call['request_json']:
            raise ValueError('skill_source_request_not_canonical')
        request_hashes.add(digest(request))
    deployments = {(event['source']['deployment_id'], event['source']['deployment_sha256'],
                    event['model_kind']) for event in events}
    if len(deployments) != 1:
        raise ValueError('skill_source_deployment_mismatch')
    deployment_id, deployment_sha256, model_kind = deployments.pop()
    direct_ids = {verification_id for event in events
                  for verification_id in event['source']['verification_ids']}
    claimed_ids = set(skill.source_verification_ids)
    if skill.model_role == 'system2':
        if claimed_ids or direct_ids or any(event['verified_outcome'] for event in events):
            raise ValueError('skill_supervisor_direct_verification_unsupported')
        verification_status = 'absent_for_system2'
    elif not claimed_ids:
        verification_status = 'not_claimed'
    elif claimed_ids.issubset(direct_ids):
        verification_status = 'claimed_and_matched'
    else:
        raise ValueError('skill_source_verification_missing')
    report = {
        'schema_version': '1.0', 'mode': 'read_only_skill_source_inspection',
        'skill_sha256': skill_sha256, 'profile_sha256': skill.profile_sha256,
        'model_role': skill.model_role, 'candidate_kind': skill.candidate_kind,
        'source_event_ids': skill.source_event_ids,
        'source_event_count': len(events),
        'source_request_sha256': sorted(request_hashes),
        'deployment_id': deployment_id, 'deployment_sha256': deployment_sha256,
        'model_kind': model_kind,
        'claimed_verification_ids': skill.source_verification_ids,
        'direct_verification_status': verification_status,
        'has_direct_verified_outcome': any(event['verified_outcome'] for event in events),
        'has_downstream_verification': any(
            event['source']['downstream_verification_ids'] for event in events),
        'run_ref': review['run_ref'], 'snapshot_sha256': review['snapshot_sha256'],
        'status': 'unreviewed_source_match', 'profile_bound': False, 'reviewed': False,
        'execution_authorized': False, 'collection_authorized': False,
        'activation_authorized': False, 'training_ready': False,
    }
    if not validator('site_skill_provenance').is_valid(report):
        raise ValueError('invalid_site_skill_provenance_report')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Inspect a manual skill draft against one audited trajectory run')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--skills', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--skill-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_site_skill_sources(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            pages=arguments.pages, skills=arguments.skills, skill_sha256=arguments.skill_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site skill source inspection unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
