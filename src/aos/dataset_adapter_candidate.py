"""Private synthetic S1 adapter candidate with independent pinned-model replay."""

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
from .dataset_gradient_probe import GradientWorkerReport
from .dataset_preflight import bounded_file, dataset_input
from .decider_row_candidate import _private_output
from .workspace_identity import open_existing_workspace


ARTIFACT_MAGIC = b'AOSLORA1'
ARTIFACT_BYTES = 131188
REPORT_BYTES_LIMIT = 65536
OUTPUT_ROOT = REPO_ROOT / 'data/decider-adapter-candidates'
WORKER_ENV = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
              'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'}


class AdapterWorkerReport(GradientWorkerReport):
    mode: Literal['dataset_s1_adapter_candidate_v1'] = 'dataset_s1_adapter_candidate_v1'
    candidate_checkpoint_written: Literal[True] = True
    candidate_artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    candidate_artifact_bytes: Literal[131188] = 131188


class AdapterReplayReport(TypedModel):
    mode: Literal['dataset_s1_adapter_replay_v1'] = 'dataset_s1_adapter_replay_v1'
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_decisions: int = Field(ge=1, le=4)
    token_budget: int = Field(ge=64, le=1536)
    base_train_nll: float = Field(ge=0)
    candidate_train_nll: float = Field(ge=0)
    base_train_accuracy: float = Field(ge=0, le=1)
    candidate_train_accuracy: float = Field(ge=0, le=1)
    validation_decisions: int = Field(ge=0, le=32)
    base_validation_nll: float | None = Field(ge=0)
    candidate_validation_nll: float | None = Field(ge=0)
    base_validation_accuracy: float | None = Field(ge=0, le=1)
    candidate_validation_accuracy: float | None = Field(ge=0, le=1)
    test_decisions: int = Field(ge=0, le=32)
    base_test_nll: float | None = Field(ge=0)
    candidate_test_nll: float | None = Field(ge=0)
    base_test_accuracy: float | None = Field(ge=0, le=1)
    candidate_test_accuracy: float | None = Field(ge=0, le=1)
    candidate_loaded: Literal[True] = True
    base_parameters_unchanged: Literal[True] = True
    base_gradient_count: Literal[0] = 0
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def valid_split_metrics(self):
        if not all(math.isfinite(value) for value in (
                self.base_train_nll, self.candidate_train_nll,
                self.base_train_accuracy, self.candidate_train_accuracy)):
            raise ValueError('adapter_replay_train_metric')
        for split in ('validation', 'test'):
            count = getattr(self, split + '_decisions')
            values = tuple(getattr(self, source + '_' + split + '_' + metric)
                           for source in ('base', 'candidate') for metric in ('nll', 'accuracy'))
            if (count == 0 and any(value is not None for value in values)) or (count > 0 and any(
                    value is None or not math.isfinite(value) for value in values)):
                raise ValueError('adapter_replay_split_metric')
        return self


class DatasetAdapterCandidateReport(TypedModel):
    schema_version: Literal['1.2'] = '1.2'
    mode: Literal['dataset_bound_s1_adapter_candidate_v1'] = 'dataset_bound_s1_adapter_candidate_v1'
    dataset_id: str = Field(pattern='^fixture-[a-f0-9]{64}$')
    dataset_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    split_counts: dict[Literal['train', 'validation', 'test'], int]
    train_report: AdapterWorkerReport
    replay_report: AdapterReplayReport
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    execution_authorized: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_binding(self):
        train = self.train_report
        replay = self.replay_report
        if (set(self.split_counts) != {'train', 'validation', 'test'}
                or any(type(count) is not int or not 0 <= count <= 32 for count in self.split_counts.values())
                or not 1 <= self.split_counts['train'] <= 4
                or train.train_decisions != self.split_counts['train'] or replay.train_decisions != train.train_decisions
                or replay.validation_decisions != self.split_counts['validation']
                or replay.test_decisions != self.split_counts['test']
                or train.input_sha256 != self.input_sha256 or replay.input_sha256 != self.input_sha256
                or train.manifest_sha256 != self.deployment_manifest_sha256
                or replay.manifest_sha256 != self.deployment_manifest_sha256
                or replay.artifact_sha256 != train.candidate_artifact_sha256
                or replay.checkpoint_revision != train.checkpoint_revision
                or replay.code_revision != train.code_revision or replay.weights_sha256 != train.weights_sha256
                or replay.token_budget != train.token_budget
                or not math.isclose(replay.base_train_nll, train.train_nll_before, rel_tol=1e-4, abs_tol=1e-6)
                or not math.isclose(replay.candidate_train_nll, train.train_nll_after, rel_tol=1e-4, abs_tol=1e-6)):
            raise ValueError('adapter_candidate_replay_binding')
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/dataset_adapter_candidate.py', 'src/aos/dataset_gradient_probe.py',
                    'services/decider/dataset_gradient_probe.py', 'services/decider/dataset_adapter_replay.py',
                    'src/aos/dataset.py', 'src/aos/dataset_preflight.py',
                    'services/decider/worker.py', 'examples/dataset_converter_pin.json')})


def artifact_bytes(path: Path, dataset_manifest_sha256: str, input_sha256: str,
                   deployment_manifest_sha256: str) -> bytes:
    directory = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != ARTIFACT_BYTES):
                raise ValueError('adapter_candidate_artifact_not_private')
            payload = os.read(descriptor, ARTIFACT_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if (len(payload) != ARTIFACT_BYTES
                    or any(getattr(before, field) != getattr(value, field)
                           for value in (after, linked) for field in fields)):
                raise ValueError('adapter_candidate_artifact_changed')
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    if (len(payload) != ARTIFACT_BYTES or payload[:8] != ARTIFACT_MAGIC
            or payload[8:40] != bytes.fromhex(dataset_manifest_sha256)
            or payload[40:72] != bytes.fromhex(input_sha256)
            or payload[72:104] != bytes.fromhex(deployment_manifest_sha256)
            or struct.unpack_from('<III', payload, 104) != (4, 6144, 2048)
            or any(not math.isfinite(value) for (value,) in struct.iter_unpack('<f', payload[116:]))):
        raise ValueError('adapter_candidate_artifact_invalid')
    return payload


def private_report_bytes(path: Path) -> bytes:
    directory = open_existing_workspace(path.parent)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('adapter_candidate_report_parent_not_private')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or not 0 < before.st_size <= REPORT_BYTES_LIMIT):
                raise ValueError('adapter_candidate_report_not_private')
            payload = os.read(descriptor, REPORT_BYTES_LIMIT + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if (len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(value, field)
                           for value in (after, linked) for field in fields)):
                raise ValueError('adapter_candidate_report_changed')
            return payload
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def verify_published_candidate(dataset: Path, manifest: Path,
                               report_path: Path) -> DatasetAdapterCandidateReport:
    report_path = report_path.absolute()
    if report_path.parent.parent != REPO_ROOT / 'data':
        raise ValueError('adapter_candidate_report_outside_private_data')
    report_bytes = private_report_bytes(report_path)
    report = DatasetAdapterCandidateReport.model_validate_json(report_bytes)
    artifact_sha256 = report.train_report.candidate_artifact_sha256
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    if (report_bytes != (canonical(report.model_dump()) + '\n').encode()
            or report_path.name != artifact_sha256 + '.' + report_sha256 + '.json'):
        raise ValueError('adapter_candidate_report_identity')
    binding, _, _ = dataset_input(dataset)
    manifest_bytes = bounded_file(manifest)
    pins = json.loads(manifest_bytes)
    if (any(getattr(report, key) != value for key, value in binding.items() if key != 'workspace_sha256')
            or report.deployment_manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest()
            or report.runner_sha256 != runner_digest()
            or report.train_report.checkpoint_revision != pins['checkpoint_revision']
            or report.train_report.code_revision != pins['code_revision']
            or report.train_report.weights_sha256 != pins['model_files']['model.safetensors']):
        raise ValueError('adapter_candidate_report_binding')
    artifact = report_path.parent / (artifact_sha256 + '.aoslora')
    payload = artifact_bytes(artifact, binding['dataset_manifest_sha256'], binding['input_sha256'],
                             report.deployment_manifest_sha256)
    if (hashlib.sha256(payload).hexdigest() != artifact_sha256
            or private_report_bytes(report_path) != report_bytes
            or dataset_input(dataset)[0] != binding or bounded_file(manifest) != manifest_bytes):
        raise ValueError('adapter_candidate_published_sources_changed')
    return report


def create_candidate(dataset: Path, manifest: Path, model_python: Path, *,
                     include_synthetic: bool = False, write_candidate: bool = False,
                     output_root: Path = OUTPUT_ROOT, max_tokens: int = 1536) -> DatasetAdapterCandidateReport:
    if not include_synthetic or not write_candidate:
        raise ValueError('explicit_synthetic_candidate_opt_in_required')
    if type(max_tokens) is not int or not 64 <= max_tokens <= 1536:
        raise ValueError('adapter_candidate_token_budget')
    binding, examples, _ = dataset_input(dataset)
    if not 1 <= binding['split_counts']['train'] <= 4:
        raise ValueError('adapter_candidate_train_count')
    manifest_bytes = bounded_file(manifest)
    pins = json.loads(manifest_bytes)
    deployment_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    runner = runner_digest()
    directory = _private_output(output_root)
    temporary = '.candidate-' + uuid4().hex + '.aoslora'
    temporary_report = '.candidate-' + uuid4().hex + '.json'
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        os.close(descriptor)
        train_request = canonical({'mode': 'dataset_s1_adapter_candidate_v1', 'examples': examples,
                                   'split_counts': binding['split_counts'], 'max_tokens': max_tokens,
                                   'dataset_manifest_sha256': binding['dataset_manifest_sha256']})
        result = run_bounded([str(model_python.absolute()), str(REPO_ROOT / 'services/decider/dataset_gradient_probe.py'),
                              str(manifest.absolute()), str(output_root.absolute() / temporary)],
                             input=train_request.encode(), timeout=240, env=WORKER_ENV)
        if result.returncode:
            raise ValueError('adapter_candidate_train_failed')
        train = AdapterWorkerReport.model_validate_json(result.stdout)
        if (train.checkpoint_revision != pins['checkpoint_revision'] or train.code_revision != pins['code_revision']
                or train.weights_sha256 != pins['model_files']['model.safetensors']
                or train.manifest_sha256 != deployment_sha256 or train.input_sha256 != binding['input_sha256']
                or train.train_decisions != binding['split_counts']['train'] or train.token_budget != max_tokens):
            raise ValueError('adapter_candidate_train_binding')
        artifact = output_root.absolute() / temporary
        payload = artifact_bytes(artifact, binding['dataset_manifest_sha256'], binding['input_sha256'], deployment_sha256)
        artifact_sha256 = hashlib.sha256(payload).hexdigest()
        if artifact_sha256 != train.candidate_artifact_sha256:
            raise ValueError('adapter_candidate_artifact_hash')
        replay_request = canonical({'mode': 'dataset_s1_adapter_replay_v1', 'examples': examples,
                                    'split_counts': binding['split_counts'], 'max_tokens': max_tokens,
                                    'dataset_manifest_sha256': binding['dataset_manifest_sha256'],
                                    'deployment_manifest_sha256': deployment_sha256,
                                    'artifact_sha256': artifact_sha256})
        replay_result = run_bounded([str(model_python.absolute()), str(REPO_ROOT / 'services/decider/dataset_adapter_replay.py'),
                                     str(manifest.absolute()), str(artifact)], input=replay_request.encode(),
                                    timeout=180, env=WORKER_ENV)
        if replay_result.returncode:
            raise ValueError('adapter_candidate_replay_failed')
        replay = AdapterReplayReport.model_validate_json(replay_result.stdout)
        report = DatasetAdapterCandidateReport(
            **{key: value for key, value in binding.items() if key != 'workspace_sha256'},
            deployment_manifest_sha256=deployment_sha256, runner_sha256=runner,
            train_report=train, replay_report=replay)
        if (dataset_input(dataset)[0] != binding or bounded_file(manifest) != manifest_bytes
                or runner_digest() != runner
                or artifact_bytes(artifact, binding['dataset_manifest_sha256'], binding['input_sha256'], deployment_sha256) != payload):
            raise ValueError('adapter_candidate_sources_changed')
        final = artifact_sha256 + '.aoslora'
        report_bytes = (canonical(report.model_dump()) + '\n').encode()
        if len(report_bytes) > REPORT_BYTES_LIMIT:
            raise ValueError('adapter_candidate_report_size')
        final_report = artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json'
        descriptor = os.open(temporary_report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(report_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if (dataset_input(dataset)[0] != binding or bounded_file(manifest) != manifest_bytes
                or runner_digest() != runner
                or artifact_bytes(artifact, binding['dataset_manifest_sha256'], binding['input_sha256'], deployment_sha256) != payload):
            raise ValueError('adapter_candidate_sources_changed')
        created_report = False
        created_artifact = False
        try:
            try:
                os.link(temporary_report, final_report, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                created_report = True
            except FileExistsError:
                if private_report_bytes(output_root.absolute() / final_report) != report_bytes:
                    raise ValueError('adapter_candidate_report_collision')
            os.unlink(temporary_report, dir_fd=directory)
            try:
                os.link(temporary, final, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                created_artifact = True
            except FileExistsError:
                if artifact_bytes(output_root.absolute() / final, binding['dataset_manifest_sha256'],
                                  binding['input_sha256'], deployment_sha256) != payload:
                    raise ValueError('adapter_candidate_artifact_collision')
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            if verify_published_candidate(dataset, manifest, output_root.absolute() / final_report) != report:
                raise ValueError('adapter_candidate_published_report_changed')
        except BaseException:
            if created_artifact:
                os.unlink(final, dir_fd=directory)
            if created_report:
                os.unlink(final_report, dir_fd=directory)
            raise
        return report
    finally:
        for name in (temporary, temporary_report):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description='Synthetic private S1 adapter candidate with independent replay', allow_abbrev=False)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path)
    parser.add_argument('--output-root', type=Path, default=OUTPUT_ROOT)
    parser.add_argument('--verify-report', type=Path)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--write-candidate', action='store_true')
    parser.add_argument('--max-tokens', type=int, default=1536)
    arguments = parser.parse_args()
    try:
        if arguments.verify_report is not None:
            if arguments.model_python is not None or arguments.include_synthetic or arguments.write_candidate:
                raise ValueError('adapter_candidate_verify_selection')
            report = verify_published_candidate(arguments.dataset, arguments.manifest, arguments.verify_report)
        else:
            if arguments.model_python is None:
                raise ValueError('adapter_candidate_model_python_required')
            report = create_candidate(arguments.dataset, arguments.manifest, arguments.model_python,
                                      include_synthetic=arguments.include_synthetic, write_candidate=arguments.write_candidate,
                                      output_root=arguments.output_root, max_tokens=arguments.max_tokens)
        print(canonical(report.model_dump()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Adapter candidate unavailable: synthetic dataset, pinned model, private output or replay failed. Deployment unchanged.\n')


if __name__ == '__main__':
    main()
