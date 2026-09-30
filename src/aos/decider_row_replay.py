"""Read-only replay of one private synthetic Decider option-row candidate."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_preflight import bounded_file
from .decider_calibration_smoke import synthetic_input
from .decider_row_candidate import ARTIFACT_BYTES, _candidate_bytes


class RowReplayReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['synthetic_decider_option_rows_replay_v1'] = 'synthetic_decider_option_rows_replay_v1'
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    heldout_sample_ref: str = Field(pattern='^[a-f0-9]{64}$')
    token_ids: list[int] = Field(min_length=2, max_length=2)
    heldout_base_nll: float = Field(ge=0)
    heldout_candidate_nll: float = Field(ge=0)
    heldout_base_choice: int = Field(ge=0, le=1)
    heldout_candidate_choice: int = Field(ge=0, le=1)
    candidate_loaded: Literal[True] = True
    base_parameters_unchanged: Literal[True] = True
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_token_ids(self):
        if len(set(self.token_ids)) != 2 or any(type(token_id) is not int or not 0 <= token_id < 248320
                                                for token_id in self.token_ids):
            raise ValueError('row_replay_token_ids_invalid')
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/decider_row_replay.py', 'services/decider/row_replay.py',
                    'src/aos/decider_row_candidate.py', 'services/decider/row_candidate.py',
                    'services/decider/worker.py', 'src/aos/decider_calibration_smoke.py',
                    'src/aos/dataset_preflight.py', 'src/aos/bounded_process.py')})


def replay_row_candidate(manifest: Path, model_python: Path, artifact: Path, *,
                         include_synthetic: bool = False) -> RowReplayReport:
    if not include_synthetic:
        raise ValueError('explicit_synthetic_row_replay_required')
    artifact = artifact.absolute()
    if artifact.parent.parent != REPO_ROOT / 'data':
        raise ValueError('row_replay_artifact_outside_private_data')
    binding, examples = synthetic_input()
    manifest_bytes = bounded_file(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    pins = json.loads(manifest_bytes)
    candidate = bounded_file(artifact, ARTIFACT_BYTES)
    if len(candidate) != ARTIFACT_BYTES:
        raise ValueError('row_replay_artifact_size')
    token_ids = list(struct.unpack_from('<II', candidate, 104))
    if len(set(token_ids)) != 2 or any(not 0 <= token_id < 248320 for token_id in token_ids):
        raise ValueError('row_replay_token_ids_invalid')
    _candidate_bytes(artifact, token_ids, source_sha256=binding['source_sha256'],
                     input_sha256=binding['input_sha256'], manifest_sha256=manifest_sha256)
    artifact_sha256 = hashlib.sha256(candidate).hexdigest()
    runner_sha256 = runner_digest()
    request = canonical({'mode': 'synthetic_decider_option_rows_replay_v1',
                         'source_sha256': binding['source_sha256'],
                         'input_sha256': binding['input_sha256'],
                         'deployment_manifest_sha256': manifest_sha256,
                         'artifact_sha256': artifact_sha256,
                         'runner_sha256': runner_sha256,
                         'heldout_sample_ref': binding['heldout_sample_ref'],
                         'heldout': examples['heldout'], 'token_ids': token_ids})
    result = run_bounded(
        [str(model_python.absolute()), str(REPO_ROOT / 'services/decider/row_replay.py'),
         str(manifest.absolute()), str(artifact)], input=request.encode(), timeout=180,
        env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
             'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'})
    if result.returncode:
        raise ValueError('row_replay_worker_failed')
    report = RowReplayReport.model_validate_json(result.stdout)
    if (report.source_sha256 != binding['source_sha256']
            or report.input_sha256 != binding['input_sha256']
            or report.deployment_manifest_sha256 != manifest_sha256
            or report.artifact_sha256 != artifact_sha256
            or report.runner_sha256 != runner_sha256
            or report.heldout_sample_ref != binding['heldout_sample_ref']
            or report.token_ids != token_ids
            or report.checkpoint_revision != pins['checkpoint_revision']
            or report.code_revision != pins['code_revision']
            or report.weights_sha256 != pins['model_files']['model.safetensors']
            or synthetic_input()[0] != binding
            or bounded_file(manifest) != manifest_bytes
            or bounded_file(artifact, ARTIFACT_BYTES) != candidate
            or runner_digest() != runner_sha256):
        raise ValueError('row_replay_source_or_model_changed')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Read-only synthetic Decider row candidate replay',
                                     allow_abbrev=False)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path, required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--include-synthetic', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        report = replay_row_candidate(arguments.manifest, arguments.model_python,
                                      arguments.artifact, include_synthetic=arguments.include_synthetic)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        parser.exit(1, 'Row candidate replay unavailable: pinned synthetic source or private artifact differs. No deployment changed.\n')
    print(canonical(report.model_dump()))


if __name__ == '__main__':
    main()
