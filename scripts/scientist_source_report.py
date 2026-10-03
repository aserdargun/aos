"""Read-only source identity for coordination, never runtime admission."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.package_handoff import read_source_member


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    'scripts/serve_desktop.py',
    'services/broker_runtime.py',
    'services/decider/broker_worker.py',
    'services/bonsai/broker_worker.py',
    'src/aos/scientist_protocol.py',
    'src/aos/scientist_transport.py',
    'src/aos/scientist_async.py',
    'src/aos/scientist_decision.py',
    'src/aos/scientist_intents.py',
    'src/aos/scientist_lab.py',
    'src/aos/scientist_lab_journal.py',
    'src/aos/scientist_lab_service.py',
    'src/aos/scientist_desktop.py',
    'src/aos/scientist_supervisor.py',
    'src/aos/scientist_inventory.py',
)
SOURCE_PROFILES = {
    'runtime_v1': SOURCE_FILES,
    'admission_v2': SOURCE_FILES + (
        'scripts/scientist_source_report.py',
        'services/bonsai_projection.py',
        'src/aos/contracts.py',
        'src/aos/supervisor.py',
        'src/aos/vision.py',
        'src/aos/scientist_admission_history.py',
        'src/aos/scientist_decider_receipt.py',
        'src/aos/scientist_bonsai_receipt.py',
        'src/aos/scientist_profile_output.py',
        'schemas/scientist_bonsai_wire_projection.schema.json',
        'schemas/scientist_admission_binding.schema.json',
        'schemas/scientist_admission_capture.schema.json',
        'schemas/scientist_admission_record.schema.json',
        'schemas/scientist_admission_binding_v2.schema.json',
        'schemas/scientist_admission_capture_v2.schema.json',
        'schemas/scientist_admission_record_v2.schema.json',
        'database/migrations/0018_scientist_turn_intents.sql',
        'database/migrations/0023_scientist_admission_history.sql',
    ),
}
SOURCE_PROFILES['terminal_candidate_v1'] = SOURCE_PROFILES['admission_v2'] + (
    'src/aos/scientist_terminal.py',
    'schemas/scientist_terminal_evidence.schema.json',
)
SOURCE_PROFILES['evidence_transport_candidate_v2'] = SOURCE_PROFILES['terminal_candidate_v1'] + (
    'src/aos/scientist_evidence_transport.py',
    'schemas/scientist_evidence_transport.schema.json',
    'schemas/scientist_evidence_transport_request.schema.json',
)
SOURCE_PROFILES['evidence_client_candidate_v2'] = SOURCE_PROFILES['evidence_transport_candidate_v2'] + (
    'src/aos/scientist_evidence_client.py',
)
SOURCE_PROFILES['evidence_journal_candidate_v2'] = SOURCE_PROFILES['evidence_client_candidate_v2'] + (
    'src/aos/scientist_evidence_journal.py',
    'database/migrations/0024_scientist_evidence_controls.sql',
    'src/aos/storage.py',
    'src/aos/dataset_audit.py',
    'schemas/dataset_audit.schema.json',
)
SOURCE_PROFILES['release_proof_candidate_v1'] = SOURCE_PROFILES['evidence_journal_candidate_v2'] + (
    'src/aos/scientist_release_proof.py',
    'schemas/scientist_release_proof.schema.json',
    'src/aos/scientist_budget_witness.py',
    'schemas/scientist_original_budget_witness.schema.json',
)
SOURCE_PROFILES['retained_evidence_candidate_v3'] = SOURCE_PROFILES['release_proof_candidate_v1'] + (
    'src/aos/scientist_retained_evidence_transport.py',
    'schemas/scientist_retained_evidence_transport.schema.json',
    'schemas/scientist_retained_evidence_transport_request.schema.json',
    'database/migrations/0025_scientist_retained_evidence_controls.sql',
)
SOURCE_PROFILES['resolution_candidate_v3'] = SOURCE_PROFILES['retained_evidence_candidate_v3'] + (
    'src/aos/scientist_resolution.py',
    'database/migrations/0026_scientist_turn_resolutions.sql',
)
SOURCE_PROFILES['retained_host_candidate_v3'] = SOURCE_PROFILES['resolution_candidate_v3'] + (
    'src/aos/scientist_retained_host.py',
)
SOURCE_PROFILES['physical_observer_candidate_v1'] = SOURCE_PROFILES['retained_host_candidate_v3'] + (
    'src/aos/bounded_process.py',
)
SOURCE_PROFILES['bootstrap_capture_candidate_v1'] = SOURCE_PROFILES['physical_observer_candidate_v1'] + (
    'src/aos/scientist_bootstrap.py',
)
SOURCE_PROFILES['retained_provider_candidate_v1'] = SOURCE_PROFILES['bootstrap_capture_candidate_v1'] + (
    'src/aos/scientist_retained_provider.py',
)
SOURCE_PROFILES['bootstrap_factory_candidate_v1'] = SOURCE_PROFILES['retained_provider_candidate_v1'] + (
    'src/aos/scientist_bootstrap_factory.py',
)
SOURCE_PROFILES['configured_source_candidate_v1'] = SOURCE_PROFILES['bootstrap_factory_candidate_v1'] + (
    'src/aos/scientist_source_authority.py',
)
SOURCE_PROFILES['lab_readback_history_candidate_v1'] = SOURCE_PROFILES['configured_source_candidate_v1'] + (
    'src/aos/scientist_lab_readbacks.py',
    'schemas/scientist_lab_readback.schema.json',
    'database/migrations/0027_scientist_lab_readbacks.sql',
)
SOURCE_PROFILES['no_admission_observation_candidate_v1'] = SOURCE_PROFILES['lab_readback_history_candidate_v1'] + (
    'src/aos/scientist_no_admission.py',
    'schemas/scientist_no_admission_observation.schema.json',
    'database/migrations/0028_scientist_no_admission_closures.sql',
)
SOURCE_PROFILES['no_admission_retained_recovery_candidate_v1'] = SOURCE_PROFILES['no_admission_observation_candidate_v1'] + (
    'schemas/scientist_no_admission_recovery.schema.json',
)


def git_snapshot(root):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment['GIT_OPTIONAL_LOCKS'] = '0'

    def git(*arguments):
        return subprocess.run(
            ['git', '-c', 'core.fsmonitor=false', '-C', str(root), *arguments],
            env=environment, check=True, capture_output=True, timeout=10,
        ).stdout

    if Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip()).resolve() != root:
        raise ValueError('Expected the exact AOS checkout root')
    return {
        'head': git('rev-parse', 'HEAD').decode('ascii').strip(),
        'tracked_diff_sha256': hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest(),
        'status': git('status', '--porcelain=v1', '-z', '--untracked-files=all'),
    }


def source_report(root=ROOT, *, source_profile='runtime_v1', expected_selected_source_sha256=None):
    if type(source_profile) is not str or source_profile not in SOURCE_PROFILES:
        raise ValueError('Unsupported source observation profile')
    if expected_selected_source_sha256 is not None and (
            type(expected_selected_source_sha256) is not str
            or re.fullmatch(r'[a-f0-9]{64}', expected_selected_source_sha256) is None):
        raise ValueError('Expected selected source pin must be lowercase SHA-256')
    source_files = SOURCE_PROFILES[source_profile]
    root = Path(root).resolve(strict=True)
    before = git_snapshot(root)
    sources = {name: hashlib.sha256(read_source_member(root, name).data).hexdigest()
               for name in source_files}
    after = git_snapshot(root)
    repeated = {name: hashlib.sha256(read_source_member(root, name).data).hexdigest()
                for name in source_files}
    if before != after or sources != repeated:
        raise ValueError('Checkout changed during source observation; rerun without concurrent edits')
    canonical = json.dumps(sources, sort_keys=True, separators=(',', ':')).encode('utf-8')
    selected_sha256 = hashlib.sha256(canonical).hexdigest()
    if (expected_selected_source_sha256 is not None
            and expected_selected_source_sha256 != selected_sha256):
        raise ValueError('Selected source differs from the explicitly expected pin')
    return {
        'schema': 'aos.scientist.source-observation.v1',
        'source_profile': source_profile,
        'head': before['head'],
        'tracked_diff_sha256': before['tracked_diff_sha256'],
        'checkout_dirty': bool(before['status']),
        'source_sha256': sources,
        'selected_source_sha256': selected_sha256,
        'scope': 'Selected runtime source only; includes untracked selected files, not the whole checkout',
        'contract_proposal': 'aos-scientist-runtime.v1',
        'admission_allowed': False,
        'gpu_release_verified': False,
        'next_gate': 'Scientist source preflight, joint capability/principal and trusted reconciliation/drain/release',
        'gpu_test_runner': 'Scientist session only',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aos-root', type=Path, default=ROOT)
    parser.add_argument('--source-profile', choices=tuple(SOURCE_PROFILES), default='runtime_v1')
    parser.add_argument('--expected-selected-source-sha256')
    arguments = parser.parse_args()
    try:
        report = source_report(arguments.aos_root, source_profile=arguments.source_profile,
                               expected_selected_source_sha256=arguments.expected_selected_source_sha256)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f'Source observation failed: {type(error).__name__}\n')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
