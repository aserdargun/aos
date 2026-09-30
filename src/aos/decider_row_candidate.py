"""Explicit synthetic-only, non-deployable Decider option-row candidate smoke."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import subprocess
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_preflight import bounded_file
from .decider_calibration_smoke import synthetic_input
from .lifecycle import private_directory
from .workspace_identity import open_existing_workspace


ARTIFACT_MAGIC = b'AOSROW1\x00'
ARTIFACT_BYTES = 8 + 3 * 32 + 8 + 2 * 2048 * 4
OUTPUT_ROOT = REPO_ROOT / 'data/decider-row-candidates'


class RowCandidateReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['synthetic_decider_option_rows_v1'] = 'synthetic_decider_option_rows_v1'
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_sample_ref: str = Field(pattern='^[a-f0-9]{64}$')
    heldout_sample_ref: str = Field(pattern='^[a-f0-9]{64}$')
    token_ids: list[int] = Field(min_length=2, max_length=2)
    token_budget: Literal[1536] = 1536
    optimizer_step_count: Literal[1] = 1
    train_nll_before: float = Field(ge=0)
    train_nll_after: float = Field(ge=0)
    heldout_nll_before: float = Field(ge=0)
    heldout_nll_after: float = Field(ge=0)
    gradient_norm: float = Field(gt=0)
    candidate_artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    candidate_artifact_bytes: Literal[16496] = 16496
    candidate_format: Literal['aos-option-rows-v1'] = 'aos-option-rows-v1'
    model_parameters_unchanged: Literal[True] = True
    model_gradient_count: Literal[0] = 0
    candidate_only: Literal[True] = True
    checkpoint_written: Literal[True] = True
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_sources(self):
        if (self.train_sample_ref == self.heldout_sample_ref
                or len(set(self.token_ids)) != 2
                or any(type(token_id) is not int or not 0 <= token_id < 248320 for token_id in self.token_ids)):
            raise ValueError('candidate_sources_invalid')
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/decider_row_candidate.py', 'services/decider/row_candidate.py',
                    'src/aos/decider_calibration_smoke.py', 'src/aos/dataset.py',
                    'src/aos/bounded_process.py')})


def _private_output(root: Path) -> int:
    root = root.absolute()
    if root.parent != REPO_ROOT / 'data' or root.name in ('', '.', '..'):
        raise ValueError('candidate_output_outside_ignored_data')
    parent = open_existing_workspace(root.parent)
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    return private_directory(root)


def _candidate_bytes(path: Path, expected_token_ids: list[int], *,
                     source_sha256: str, input_sha256: str, manifest_sha256: str) -> bytes:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise ValueError('candidate_artifact_not_private')
    payload = bounded_file(path, ARTIFACT_BYTES)
    if len(payload) != ARTIFACT_BYTES or payload[:8] != ARTIFACT_MAGIC:
        raise ValueError('candidate_artifact_format')
    if (payload[8:40] != bytes.fromhex(source_sha256)
            or payload[40:72] != bytes.fromhex(input_sha256)
            or payload[72:104] != bytes.fromhex(manifest_sha256)):
        raise ValueError('candidate_artifact_identity')
    token_ids = list(struct.unpack_from('<II', payload, 104))
    if token_ids != expected_token_ids or any(not math.isfinite(value)
                                              for (value,) in struct.iter_unpack('<f', payload[112:])):
        raise ValueError('candidate_artifact_values')
    return payload


def row_candidate_smoke(manifest: Path, model_python: Path, *,
                        include_synthetic: bool = False, train_candidate: bool = False,
                        output_root: Path = OUTPUT_ROOT) -> RowCandidateReport:
    if not include_synthetic or not train_candidate:
        raise ValueError('explicit_row_candidate_opt_in_required')
    binding, examples = synthetic_input()
    manifest_bytes = bounded_file(manifest)
    pins = json.loads(manifest_bytes)
    runner = runner_digest()
    request = canonical({'mode': 'synthetic_decider_option_rows_v1', 'examples': examples,
                         'source_sha256': binding['source_sha256'],
                         'input_sha256': binding['input_sha256'],
                         'train_sample_ref': binding['train_sample_ref'],
                         'heldout_sample_ref': binding['heldout_sample_ref'],
                         'runner_sha256': runner, 'token_budget': 1536, 'step_size': 0.001})
    directory = _private_output(output_root)
    temporary = '.candidate-' + uuid4().hex + '.aosrows'
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        os.close(descriptor)
        result = run_bounded(
            [str(model_python.absolute()), str(REPO_ROOT / 'services/decider/row_candidate.py'),
             str(manifest.absolute()), str(output_root.absolute() / temporary)],
            input=request.encode(), timeout=180,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
                 'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'})
        if result.returncode:
            raise ValueError('row_candidate_worker_failed')
        report = RowCandidateReport.model_validate_json(result.stdout)
        if (any(getattr(report, key) != value for key, value in binding.items())
                or report.deployment_manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest()
                or report.runner_sha256 != runner
                or report.checkpoint_revision != pins['checkpoint_revision']
                or report.code_revision != pins['code_revision']
                or report.weights_sha256 != pins['model_files']['model.safetensors']
                or synthetic_input()[0] != binding or bounded_file(manifest) != manifest_bytes
                or runner_digest() != runner):
            raise ValueError('row_candidate_source_or_model_changed')
        payload = _candidate_bytes(
            output_root.absolute() / temporary, report.token_ids,
            source_sha256=binding['source_sha256'], input_sha256=binding['input_sha256'],
            manifest_sha256=report.deployment_manifest_sha256)
        if hashlib.sha256(payload).hexdigest() != report.candidate_artifact_sha256:
            raise ValueError('row_candidate_artifact_hash_mismatch')
        final = report.candidate_artifact_sha256 + '.aosrows'
        try:
            os.link(temporary, final, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
        except FileExistsError:
            if _candidate_bytes(output_root.absolute() / final, report.token_ids,
                                source_sha256=binding['source_sha256'],
                                input_sha256=binding['input_sha256'],
                                manifest_sha256=report.deployment_manifest_sha256) != payload:
                raise ValueError('row_candidate_artifact_collision')
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        return report
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Synthetic private Decider option-row candidate diagnostic', allow_abbrev=False)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, default=OUTPUT_ROOT)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--train-candidate', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        report = row_candidate_smoke(arguments.manifest, arguments.model_python,
                                     output_root=arguments.output_root,
                                     include_synthetic=arguments.include_synthetic,
                                     train_candidate=arguments.train_candidate)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        parser.exit(1, 'Row candidate unavailable: synthetic input, pinned model, private output or GPU preflight failed. No deployment changed.\n')
    print(canonical(report.model_dump()))


if __name__ == '__main__':
    main()
