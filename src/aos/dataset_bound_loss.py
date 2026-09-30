import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import SPLITS
from .dataset_preflight import bounded_file, dataset_input, runner_digest as tokenizer_runner_digest


class SplitLoss(TypedModel):
    split: Literal['train', 'validation', 'test']
    layout: Literal['state_first', 'schema_first']
    decisions: int = Field(ge=1, le=32)
    max_padded_tokens: int = Field(ge=64, le=1536, multiple_of=64)
    mean_nll: float = Field(ge=0)
    correct_choices: int = Field(ge=0, le=32)


class ForwardReport(TypedModel):
    mode: Literal['dataset_s1_forward_only_v1'] = 'dataset_s1_forward_only_v1'
    checkpoint_revision: str = Field(pattern='^[a-f0-9]{40}$')
    code_revision: str = Field(pattern='^[a-f0-9]{40}$')
    manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    weights_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    token_budget: int = Field(ge=64, le=1536)
    split_metrics: list[SplitLoss] = Field(min_length=2, max_length=6)
    peak_vram_allocated_bytes: int = Field(ge=1)
    peak_vram_reserved_bytes: int = Field(ge=1)
    elapsed_seconds: float = Field(gt=0)
    weights_loaded: Literal[True] = True
    parameters_unchanged: Literal[True] = True
    gradient_run: Literal[False] = False
    optimizer_run: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def valid_metrics(self):
        if (self.peak_vram_reserved_bytes < self.peak_vram_allocated_bytes
                or any(metric.correct_choices > metric.decisions or metric.max_padded_tokens > self.token_budget
                       for metric in self.split_metrics)):
            raise ValueError('dataset_loss_metrics')
        return self


class DatasetLossReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['dataset_bound_s1_loss_v1'] = 'dataset_bound_s1_loss_v1'
    dataset_id: str = Field(pattern='^fixture-[a-f0-9]{64}$')
    dataset_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    split_counts: dict[Literal['train', 'validation', 'test'], Annotated[int, Field(ge=0, le=32)]] = Field(
        json_schema_extra={'required': list(SPLITS), 'minProperties': 3, 'maxProperties': 3})
    forward_report: ForwardReport
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    supervisor_tokenizer_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_binding(self):
        if set(self.split_counts) != set(SPLITS) or not 1 <= sum(self.split_counts.values()) <= 32:
            raise ValueError('dataset_loss_split_counts')
        expected = [(split, layout, self.split_counts[split]) for split in SPLITS if self.split_counts[split]
                    for layout in ('state_first', 'schema_first')]
        actual = [(metric.split, metric.layout, metric.decisions) for metric in self.forward_report.split_metrics]
        if (actual != expected or self.forward_report.input_sha256 != self.input_sha256
                or self.forward_report.manifest_sha256 != self.deployment_manifest_sha256):
            raise ValueError('dataset_loss_binding')
        return self


def runner_digest() -> str:
    return digest({'dataset_tokenizer_runner': tokenizer_runner_digest(), 'files': {
        name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
        ('src/aos/dataset_bound_loss.py', 'services/decider/dataset_loss_probe.py', 'services/decider/worker.py',
         'examples/dataset_converter_pin.json')}})


def check_pins(report: ForwardReport, pins: dict) -> None:
    if (report.checkpoint_revision != pins['checkpoint_revision'] or report.code_revision != pins['code_revision']
            or report.weights_sha256 != pins['model_files']['model.safetensors']):
        raise ValueError('dataset_loss_pin_mismatch')


def dataset_loss_preflight(dataset: Path, manifest: Path, python: Path, source: Path, *,
                           include_synthetic: bool = False, forward_only: bool = False,
                           max_tokens: int = 1536) -> DatasetLossReport:
    if not include_synthetic or not forward_only:
        raise ValueError('explicit_synthetic_forward_opt_in_required')
    if type(max_tokens) is not int or not 64 <= max_tokens <= 1536:
        raise ValueError('dataset_loss_token_budget')
    binding, examples, _ = dataset_input(dataset)
    deployment_bytes = bounded_file(manifest)
    runner = runner_digest()
    request = canonical({'mode': 'dataset_s1_forward_only_v1', 'examples': examples,
                         'split_counts': binding['split_counts'], 'max_tokens': max_tokens})
    result = run_bounded([str(python.absolute()), str(REPO_ROOT / 'services/decider/dataset_loss_probe.py'),
                          str(manifest.absolute()), str(source.absolute())], input=request.encode(), timeout=180,
                         env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
                              'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'})
    if result.returncode:
        raise ValueError('dataset_loss_worker_failed')
    forward = ForwardReport.model_validate(json.loads(result.stdout))
    check_pins(forward, json.loads(deployment_bytes))
    if forward.token_budget != max_tokens:
        raise ValueError('dataset_loss_budget_mismatch')
    if dataset_input(dataset)[0] != binding or bounded_file(manifest) != deployment_bytes or runner_digest() != runner:
        raise ValueError('dataset_loss_sources_changed')
    return DatasetLossReport(**{key: value for key, value in binding.items() if key != 'workspace_sha256'},
                             deployment_manifest_sha256=hashlib.sha256(deployment_bytes).hexdigest(),
                             runner_sha256=runner, forward_report=forward)


def verify_dataset_loss_report(report: dict, dataset: Path, manifest: Path) -> DatasetLossReport:
    parsed = DatasetLossReport.model_validate(report)
    binding, _, _ = dataset_input(dataset)
    deployment_bytes = bounded_file(manifest)
    if (any(getattr(parsed, key) != value for key, value in binding.items() if key != 'workspace_sha256')
            or parsed.deployment_manifest_sha256 != hashlib.sha256(deployment_bytes).hexdigest()
            or parsed.runner_sha256 != runner_digest()):
        raise ValueError('dataset_loss_report_binding_mismatch')
    check_pins(parsed.forward_report, json.loads(deployment_bytes))
    return parsed


def main():
    parser = argparse.ArgumentParser(description='Dataset bağlı gerçek S1 forward/loss; gradient, optimizer veya eğitim yok')
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path)
    parser.add_argument('--upstream-source', type=Path)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--forward-only', action='store_true')
    parser.add_argument('--max-tokens', type=int, default=1536)
    parser.add_argument('--verify-report', type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.verify_report is not None:
            report = verify_dataset_loss_report(json.loads(bounded_file(arguments.verify_report)), arguments.dataset, arguments.manifest)
        else:
            if arguments.model_python is None or arguments.upstream_source is None:
                raise ValueError('dataset_loss_local_worker_required')
            report = dataset_loss_preflight(arguments.dataset, arguments.manifest, arguments.model_python, arguments.upstream_source,
                                            include_synthetic=arguments.include_synthetic, forward_only=arguments.forward_only,
                                            max_tokens=arguments.max_tokens)
        print(canonical(report.model_dump()))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Dataset loss denetimi başarısız; yerel dataset/model pinleri ve GPU bütçesini kontrol edin. Eğitim yapılmadı.\n')


if __name__ == '__main__':
    main()
