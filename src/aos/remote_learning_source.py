"""Read-only, content-free learning source audit for one completed HTTPS route run."""

import argparse
from contextlib import nullcontext
from pathlib import Path
import sqlite3

from .contracts import canonical
from .dataset import validator
from .dataset_audit import audit_snapshot
from .learning_events import review_learning_events_snapshot
from .remote_route_evidence import _review_remote_route_evidence
from .web_application import WebApplicationProfiles


def inspect_remote_learning_source(database: Path, run_id: str, *, profiles: Path,
                                   selected_profile_sha256: str,
                                   selected_plan_sha256: str) -> dict:
    profile = WebApplicationProfiles(profiles).get(selected_profile_sha256)
    requested = [role for role in ('system1', 'system2')
                 if getattr(profile.learning, role) == 'requested']
    with audit_snapshot(database) as (snapshot, identity):
        evidence = _review_remote_route_evidence(
            database, run_id, profiles=profiles,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            source=nullcontext((snapshot, identity)))
        if evidence['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_learning_source_snapshot_changed')
        actions = snapshot.execute(
            "SELECT step_id,decision_id FROM actions WHERE run_id=? AND tool='browser.remote.route'",
            (run_id,)).fetchall()
        route_decisions = {(action['step_id'], action['decision_id']) for action in actions}
        if len(route_decisions) != evidence['route_count']:
            raise ValueError('remote_learning_source_route_decisions_ambiguous')
        route_steps = {step_id for step_id, _decision_id in route_decisions}
        review = review_learning_events_snapshot(snapshot, identity, run_id)
        if review['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_learning_source_model_snapshot_changed')
        events = {'system1': [], 'system2': []}
        for event in review['events']:
            role = event['role']
            source = event['source']
            if role not in requested or source['step_id'] not in route_steps:
                continue
            if role == 'system1' and (source['step_id'], source['decision_id']) not in route_decisions:
                continue
            if role == 'system2' and source['escalation_id'] is None:
                continue
            events[role].append(event['event_id'])
        if any(len(references) > 32 for references in events.values()):
            raise ValueError('remote_learning_source_event_limit')
        report = {
            'schema_version': '1.0', 'mode': 'read_only_remote_learning_source',
            'status': 'unreviewed_metadata_candidate',
            'profile_sha256': selected_profile_sha256,
            'plan_sha256': selected_plan_sha256,
            'run_ref': evidence['run_ref'],
            'snapshot_sha256': identity['sha256'],
            'requested_roles': requested,
            'source_event_ids_by_role': events,
            'transport_readback_verified': True,
            'site_outcome_verified': False,
            'account_verified': False,
            'rights_reviewed': False,
            'redaction_reviewed': False,
            'collection_authorized': False,
            'training_ready': False,
        }
        if not validator('remote_learning_source').is_valid(report):
            raise ValueError('remote_learning_source_report_invalid')
        return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Inspect unreviewed remote learning metadata sources',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_learning_source(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote learning source unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
