"""Content-free, read-only readiness gaps for recorded S1/S2 learning evidence."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .dataset import validator
from .learning_events import MAX_REVIEW_EVENTS, review_learning_events


def summarize_learning_candidates(evidence_review: dict) -> dict:
    events = evidence_review['events']
    if (evidence_review['schema_version'] != '1.0'
            or evidence_review['mode'] != 'read_only_metadata_review'
            or evidence_review['collection_authorized'] is not False
            or evidence_review['training_ready'] is not False
            or not isinstance(events, list) or len(events) > MAX_REVIEW_EVENTS
            or evidence_review['event_count'] != len(events)):
        raise ValueError('invalid_learning_evidence_review')
    rows = []
    for event in events:
        if not validator('learning_event_v2').is_valid(event):
            raise ValueError('invalid_learning_evidence')
        source = event['source']
        rows.append({'schema_version': '1.0', 'source_event_id': event['event_id'],
                     'role': event['role'], 'has_independent_outcome': event['verified_outcome'],
                     'has_scene_evidence': source['scene_observation_id'] is not None,
                     'has_downstream_verification': bool(source['downstream_verification_ids']),
                     'status': 'blocked', 'label_ready': False, 'training_ready': False,
                     'blockers': sorted(set(event['blockers']) | {'gold_label_missing',
                                                                  'dataset_review_missing'})})
    if len({row['source_event_id'] for row in rows}) != len(rows):
        raise ValueError('duplicate_learning_evidence_event')
    rows.sort(key=lambda row: (row['role'], row['source_event_id']))
    report = {'schema_version': '1.0', 'mode': 'read_only_candidate_gap_review',
              'run_ref': evidence_review['run_ref'], 'snapshot_sha256': evidence_review['snapshot_sha256'],
              'event_count': len(rows), 'system1_count': sum(row['role'] == 'system1' for row in rows),
              'system2_count': sum(row['role'] == 'system2' for row in rows),
              'rows': rows, 'collection_authorized': False, 'training_ready': False}
    if not validator('learning_candidate_report').is_valid(report):
        raise ValueError('invalid_learning_candidate_report')
    return report


def review_learning_candidates(path: Path, run_id: str) -> dict:
    return summarize_learning_candidates(review_learning_events(path, run_id))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Review metadata-only S1/S2 learning candidate gaps')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, 'Learning candidate review unavailable: invalid arguments.\n')
    try:
        report = review_learning_candidates(arguments.database, arguments.run_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Learning candidate review unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
