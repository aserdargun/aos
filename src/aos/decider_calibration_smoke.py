"""Explicit synthetic-only, transient calibration optimizer diagnostic for pinned Decider."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import convert_record, leakage_groups, select_record
from .dataset_preflight import bounded_file


SOURCE_NAMES = ('examples/system1_choice.jsonl', 'examples/dataset_review.json',
                'examples/evidence_catalog.json')
TRAIN_ID = 's1-write'
HELDOUT_ID = 's1-inventory'
TOKEN_BUDGET = 1536
STEP_SIZE = 0.05


class CalibrationReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['synthetic_decider_transient_calibration_v1'] = 'synthetic_decider_transient_calibration_v1'
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_sample_ref: str = Field(pattern='^[a-f0-9]{64}$')
    heldout_sample_ref: str = Field(pattern='^[a-f0-9]{64}$')
    token_budget: Literal[1536] = 1536
    optimizer_step_count: Literal[1] = 1
    train_nll_before: float = Field(ge=0)
    train_nll_after: float = Field(ge=0)
    heldout_nll_before: float = Field(ge=0)
    heldout_nll_after: float = Field(ge=0)
    scalar_gradient_abs: float = Field(ge=0)
    temperature_before: Literal[1.0] = 1.0
    temperature_after: float = Field(gt=0)
    model_parameters_unchanged: Literal[True] = True
    model_gradient_count: Literal[0] = 0
    transient_scalar_only: Literal[True] = True
    checkpoint_written: Literal[False] = False
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def disjoint_samples(self):
        if self.train_sample_ref == self.heldout_sample_ref:
            raise ValueError('calibration_samples_not_disjoint')
        return self


def synthetic_input() -> tuple[dict, dict]:
    sources = {name: bounded_file(REPO_ROOT / name) for name in SOURCE_NAMES}
    records = [json.loads(line) for line in sources[SOURCE_NAMES[0]].splitlines() if line.strip()]
    reviews = json.loads(sources[SOURCE_NAMES[1]])
    evidence = json.loads(sources[SOURCE_NAMES[2]])
    if (reviews.get('synthetic') is not True or evidence.get('synthetic') is not True
            or len({record['sample_id'] for record in records}) != len(records)):
        raise ValueError('calibration_fixture_invalid')
    review_by_id = {item['sample_id']: item for item in reviews['reviews']}
    evidence_by_id = {item['id']: item for item in evidence['evidence']}
    selected = {}
    for sample_id in (TRAIN_ID, HELDOUT_ID):
        matches = [record for record in records if record['sample_id'] == sample_id]
        if len(matches) != 1 or sample_id not in review_by_id:
            raise ValueError('calibration_sample_missing')
        review = review_by_id[sample_id]
        record = select_record('system1_choice', matches[0], review, evidence_by_id)
        selected[sample_id] = {'record': record, 'review': review, 'kind': 'system1_choice'}
    train, heldout = selected[TRAIN_ID], selected[HELDOUT_ID]
    groups = leakage_groups([train, heldout])
    if (groups[TRAIN_ID] == groups[HELDOUT_ID]
            or train['record']['provenance']['run_id'] == heldout['record']['provenance']['run_id']
            or train['review']['task_family'] == heldout['review']['task_family']
            or train['review']['near_duplicate_cluster'] == heldout['review']['near_duplicate_cluster']):
        raise ValueError('calibration_heldout_leakage')
    examples = {'train': convert_record('system1_choice', train['record'], train['review']['task_family']),
                'heldout': convert_record('system1_choice', heldout['record'], heldout['review']['task_family'])}
    binding = {'source_sha256': digest({name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}),
               'input_sha256': digest(examples),
               'train_sample_ref': digest({'sample_id': TRAIN_ID}),
               'heldout_sample_ref': digest({'sample_id': HELDOUT_ID})}
    return binding, examples


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/decider_calibration_smoke.py', 'services/decider/calibration_smoke.py',
                    'src/aos/bounded_process.py', 'src/aos/dataset.py')})


def calibration_smoke(manifest: Path, model_python: Path, *,
                      include_synthetic: bool = False, optimizer_smoke: bool = False) -> CalibrationReport:
    if not include_synthetic or not optimizer_smoke:
        raise ValueError('explicit_calibration_opt_in_required')
    binding, examples = synthetic_input()
    manifest_bytes = bounded_file(manifest)
    pins = json.loads(manifest_bytes)
    runner = runner_digest()
    request = canonical({'mode': 'synthetic_decider_transient_calibration_v1',
                         'examples': examples, 'source_sha256': binding['source_sha256'],
                         'input_sha256': binding['input_sha256'],
                         'train_sample_ref': binding['train_sample_ref'],
                         'heldout_sample_ref': binding['heldout_sample_ref'],
                         'runner_sha256': runner,
                         'token_budget': TOKEN_BUDGET, 'step_size': STEP_SIZE})
    result = run_bounded(
        [str(model_python.absolute()), str(REPO_ROOT / 'services/decider/calibration_smoke.py'),
         str(manifest.absolute())], input=request.encode(), timeout=180,
        env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
             'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'})
    if result.returncode:
        raise ValueError('calibration_worker_failed')
    report = CalibrationReport.model_validate_json(result.stdout)
    if (any(getattr(report, key) != value for key, value in binding.items())
            or report.deployment_manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest()
            or report.runner_sha256 != runner
            or report.checkpoint_revision != pins['checkpoint_revision']
            or report.code_revision != pins['code_revision']
            or report.weights_sha256 != pins['model_files']['model.safetensors']
            or synthetic_input()[0] != binding or bounded_file(manifest) != manifest_bytes
            or runner_digest() != runner):
        raise ValueError('calibration_source_or_model_changed')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Synthetic frozen-Decider transient calibration diagnostic', allow_abbrev=False)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path, required=True)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--optimizer-smoke', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        report = calibration_smoke(arguments.manifest, arguments.model_python,
                                   include_synthetic=arguments.include_synthetic,
                                   optimizer_smoke=arguments.optimizer_smoke)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Transient calibration unavailable: synthetic input, pinned model or GPU preflight failed. No checkpoint was written.\n')
    print(canonical(report.model_dump()))


if __name__ == '__main__':
    main()
