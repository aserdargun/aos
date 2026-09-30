"""Explicit synthetic-only gradient compatibility probe for the pinned Decider."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_preflight import bounded_file, dataset_input


class GradientWorkerReport(TypedModel):
    mode: Literal['dataset_s1_gradient_smoke_v1'] = 'dataset_s1_gradient_smoke_v1'
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_decisions: int = Field(ge=1, le=4)
    token_budget: int = Field(ge=64, le=1536)
    adapter_target: Literal['last_mlp_down_proj'] = 'last_mlp_down_proj'
    adapter_rank: Literal[4] = 4
    adapter_parameter_count: Literal[32768]
    seed: Literal[42]
    optimizer_step_count: Literal[1] = 1
    train_nll_before: float = Field(ge=0)
    train_nll_after: float = Field(ge=0)
    gradient_norm: float = Field(gt=0)
    peak_vram_allocated_bytes: int = Field(ge=1)
    peak_vram_reserved_bytes: int = Field(ge=1)
    elapsed_seconds: float = Field(gt=0)
    base_parameters_unchanged: Literal[True] = True
    base_gradient_count: Literal[0] = 0
    adapter_parameters_changed: Literal[True] = True
    candidate_checkpoint_written: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def valid_memory(self):
        if self.peak_vram_reserved_bytes < self.peak_vram_allocated_bytes:
            raise ValueError('gradient_probe_memory')
        return self


class DatasetGradientReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['dataset_bound_s1_gradient_smoke_v1'] = 'dataset_bound_s1_gradient_smoke_v1'
    dataset_id: str = Field(pattern='^fixture-[a-f0-9]{64}$')
    dataset_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    split_counts: dict[Literal['train', 'validation', 'test'], int]
    gradient_report: GradientWorkerReport
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    execution_authorized: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_binding(self):
        if (set(self.split_counts) != {'train', 'validation', 'test'}
                or any(type(count) is not int or not 0 <= count <= 32 for count in self.split_counts.values())
                or not 1 <= self.split_counts['train'] <= 4
                or self.gradient_report.train_decisions != self.split_counts['train']
                or self.gradient_report.input_sha256 != self.input_sha256
                or self.gradient_report.manifest_sha256 != self.deployment_manifest_sha256):
            raise ValueError('gradient_probe_binding')
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/dataset_gradient_probe.py', 'services/decider/dataset_gradient_probe.py',
                    'src/aos/dataset.py', 'src/aos/dataset_preflight.py',
                    'services/decider/worker.py', 'examples/dataset_converter_pin.json')})


def gradient_probe(dataset: Path, manifest: Path, model_python: Path, *,
                   include_synthetic: bool = False, gradient_smoke: bool = False,
                   max_tokens: int = 1536) -> DatasetGradientReport:
    if not include_synthetic or not gradient_smoke:
        raise ValueError('explicit_synthetic_gradient_opt_in_required')
    if type(max_tokens) is not int or not 64 <= max_tokens <= 1536:
        raise ValueError('gradient_probe_token_budget')
    binding, examples, _ = dataset_input(dataset)
    if not 1 <= binding['split_counts']['train'] <= 4:
        raise ValueError('gradient_probe_train_count')
    deployment_bytes = bounded_file(manifest)
    pins = json.loads(deployment_bytes)
    runner = runner_digest()
    request = canonical({'mode': 'dataset_s1_gradient_smoke_v1', 'examples': examples,
                         'split_counts': binding['split_counts'], 'max_tokens': max_tokens})
    result = run_bounded([str(model_python.absolute()), str(REPO_ROOT / 'services/decider/dataset_gradient_probe.py'),
                          str(manifest.absolute())], input=request.encode(), timeout=240,
                         env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
                              'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'})
    if result.returncode:
        raise ValueError('gradient_probe_worker_failed')
    worker = GradientWorkerReport.model_validate_json(result.stdout)
    if (worker.checkpoint_revision != pins['checkpoint_revision']
            or worker.code_revision != pins['code_revision']
            or worker.weights_sha256 != pins['model_files']['model.safetensors']
            or worker.token_budget != max_tokens):
        raise ValueError('gradient_probe_model_pin_mismatch')
    if dataset_input(dataset)[0] != binding or bounded_file(manifest) != deployment_bytes or runner_digest() != runner:
        raise ValueError('gradient_probe_sources_changed')
    return DatasetGradientReport(**{key: value for key, value in binding.items() if key != 'workspace_sha256'},
                                 deployment_manifest_sha256=hashlib.sha256(deployment_bytes).hexdigest(),
                                 runner_sha256=runner, gradient_report=worker)


def main() -> None:
    parser = argparse.ArgumentParser(description='Synthetic dataset-bound S1 model gradient compatibility; no checkpoint or promotion', allow_abbrev=False)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path, required=True)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--gradient-smoke', action='store_true')
    parser.add_argument('--max-tokens', type=int, default=1536)
    arguments = parser.parse_args()
    try:
        report = gradient_probe(arguments.dataset, arguments.manifest, arguments.model_python,
                                include_synthetic=arguments.include_synthetic,
                                gradient_smoke=arguments.gradient_smoke, max_tokens=arguments.max_tokens)
        print(canonical(report.model_dump()))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Gradient smoke başarısız; sentetik veri, model pinleri ve GPU bütçesini kontrol edin. Deployment değişmedi.\n')


if __name__ == '__main__':
    main()
