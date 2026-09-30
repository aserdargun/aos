"""Read-only semantic fingerprints for independently verified synthetic local pages."""

import argparse
import json
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .learning_events import LOCAL_PAGE_OUTCOMES, MAX_REVIEW_EVENTS, RUN_ID, project_learning_events


FINGERPRINT_VERSION = 'synthetic-local-page-v1'


def review_site_page_evidence(path: Path, run_id: str) -> dict:
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError('invalid_run_id')
    with audit_snapshot(path) as (snapshot, identity):
        run = snapshot.execute('SELECT policy_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise ValueError('run_missing')
        call_count = snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?', (run_id,)).fetchone()[0]
        if call_count > MAX_REVIEW_EVENTS:
            raise ValueError('event_count_limit')
        pages = []
        if run['policy_version'] == 'browser-local-navigation-policy-v1':
            for event in project_learning_events(snapshot, run_id):
                if event['role'] != 'system1':
                    continue
                source = event['source']
                for verification_id in source['verification_ids']:
                    row = snapshot.execute('''SELECT v.method,v.actual_json,v.evidence_refs_json,a.tool,a.decision_id
                        FROM verifications v JOIN actions a
                        ON a.action_id=v.action_id AND a.run_id=v.run_id AND a.step_id=v.step_id
                        WHERE v.verification_id=? AND v.run_id=? AND v.step_id=?''',
                        (verification_id, run_id, source['step_id'])).fetchone()
                    if (row is None or row['method'] != 'independent_local_page_equals'
                            or row['decision_id'] != source['decision_id']
                            or row['tool'] not in LOCAL_PAGE_OUTCOMES):
                        continue
                    outcome = json.loads(row['actual_json'])
                    if outcome != LOCAL_PAGE_OUTCOMES[row['tool']]:
                        continue
                    evidence_refs = json.loads(row['evidence_refs_json'])
                    page = {'schema_version': '1.0', 'fingerprint_version': FINGERPRINT_VERSION,
                            'page_key': outcome['page'],
                            'page_fingerprint_sha256': digest({'version': FINGERPRINT_VERSION, 'outcome': outcome}),
                            'source': {'event_id': event['event_id'], 'verification_id': verification_id,
                                       'observation_id': evidence_refs[0]},
                            'profile_bound': False, 'reviewed': False, 'execution_authorized': False,
                            'collection_authorized': False, 'training_ready': False}
                    validator('site_page_evidence').validate(page)
                    pages.append(page)
        pages.sort(key=lambda page: (page['page_key'], page['source']['verification_id']))
        return {'schema_version': '1.0', 'mode': 'read_only_synthetic_page_evidence',
                'run_ref': digest({'run_id': run_id}), 'snapshot_sha256': identity['sha256'],
                'page_count': len(pages), 'pages': pages,
                'profile_bound': False, 'execution_authorized': False,
                'collection_authorized': False, 'training_ready': False}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Review verified synthetic local-page fingerprints')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, 'Site page evidence unavailable: invalid arguments.\n')
    try:
        report = review_site_page_evidence(arguments.database, arguments.run_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site page evidence unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
