"""Explicit, isolated inference smoke for a published synthetic S1 adapter."""

import argparse
import hashlib
from pathlib import Path
import subprocess
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_adapter_candidate import (WORKER_ENV, private_report_bytes,
                                        verify_published_candidate)
from .dataset_preflight import bounded_file, dataset_input


class RuntimeChoiceMetrics(TypedModel):
    selected_index: int = Field(ge=0, le=9)
    gold_probability: float = Field(ge=0, le=1)


class RuntimeSplitMetrics(TypedModel):
    count: int = Field(ge=0, le=32)
    base_correct: int = Field(ge=0, le=32)
    candidate_correct: int = Field(ge=0, le=32)

    @model_validator(mode='after')
    def valid_correct_counts(self):
        if self.base_correct > self.count or self.candidate_correct > self.count:
            raise ValueError('adapter_runtime_correct_count')
        return self


class AdapterRuntimeWorkerReport(TypedModel):
    mode: Literal['synthetic_adapter_runtime_probe_v1']
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_sample_index: Literal[0]
    option_count: int = Field(ge=2, le=10)
    base: RuntimeChoiceMetrics
    candidate: RuntimeChoiceMetrics
    split_results: dict[Literal['train', 'validation', 'test'], RuntimeSplitMetrics]
    adapter_loaded: Literal[True]
    adapter_hook_calls: int = Field(ge=1, le=65536)
    base_parameters_unchanged: Literal[True]
    active_deployment_changed: Literal[False]


class AdapterRuntimeProbeReport(TypedModel):
    schema_version: Literal['1.1'] = '1.1'
    mode: Literal['synthetic_adapter_runtime_probe_v1'] = 'synthetic_adapter_runtime_probe_v1'
    dataset_id: str = Field(pattern='^fixture-[a-f0-9]{64}$')
    dataset_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    candidate_report_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    train_sample_index: Literal[0] = 0
    option_count: int = Field(ge=2, le=10)
    base: RuntimeChoiceMetrics
    candidate: RuntimeChoiceMetrics
    split_results: dict[Literal['train', 'validation', 'test'], RuntimeSplitMetrics]
    adapter_loaded: Literal[True] = True
    adapter_hook_calls: int = Field(ge=1, le=65536)
    base_parameters_unchanged: Literal[True] = True
    active_deployment_changed: Literal[False] = False
    synthetic: Literal[True] = True
    runtime_compatible: Literal[True] = True
    quality_improved: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_choices(self):
        if (self.base.selected_index >= self.option_count or self.candidate.selected_index >= self.option_count
                or set(self.split_results) != {'train', 'validation', 'test'}
                or self.split_results['train'].count < 1
                or self.adapter_hook_calls < sum(result.count for result in self.split_results.values())):
            raise ValueError('adapter_runtime_choice_out_of_range')
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in (
        'src/aos/dataset_adapter_runtime_probe.py',
        'services/decider/dataset_adapter_runtime_probe.py',
        'services/decider/dataset_adapter_replay.py',
        'services/decider/worker.py')})


def probe_runtime(dataset: Path, manifest: Path, report_path: Path, model_python: Path, *,
                  include_synthetic: bool = False, runtime_smoke: bool = False) -> AdapterRuntimeProbeReport:
    if not include_synthetic or not runtime_smoke:
        raise ValueError('adapter_runtime_explicit_opt_in_required')
    candidate = verify_published_candidate(dataset, manifest, report_path)
    binding, examples, _ = dataset_input(dataset)
    decisions = []
    gold_option_ids = []
    for source in examples:
        question = source['qs'][0]
        decisions.append({'state': source['context'], 'question': question['text'],
                          'options': [{'id': 'option-' + str(index), 'label': label}
                                      for index, label in enumerate(question['options'])]})
        gold_option_ids.append('option-' + str(question['gold']))
    artifact_sha256 = candidate.train_report.candidate_artifact_sha256
    artifact = report_path.absolute().parent / (artifact_sha256 + '.aoslora')
    manifest_bytes = bounded_file(manifest)
    report_bytes = private_report_bytes(report_path.absolute())
    runner_sha256 = runner_digest()
    request = {'mode': 'synthetic_adapter_runtime_probe_v1',
               'artifact_sha256': artifact_sha256,
               'dataset_manifest_sha256': binding['dataset_manifest_sha256'],
               'input_sha256': binding['input_sha256'],
               'deployment_manifest_sha256': candidate.deployment_manifest_sha256,
               'decisions': decisions, 'gold_option_ids': gold_option_ids,
               'split_counts': binding['split_counts']}
    result = run_bounded([str(model_python.absolute()),
                          str(REPO_ROOT / 'services/decider/dataset_adapter_runtime_probe.py'),
                          str(manifest.absolute()), str(artifact)],
                         input=canonical(request).encode(), timeout=300, env=WORKER_ENV)
    if result.returncode:
        raise ValueError('adapter_runtime_worker_failed')
    worker = AdapterRuntimeWorkerReport.model_validate_json(result.stdout)
    if (worker.artifact_sha256 != artifact_sha256
            or worker.deployment_manifest_sha256 != candidate.deployment_manifest_sha256
            or worker.input_sha256 != binding['input_sha256']
            or worker.option_count != len(decisions[0]['options'])
            or worker.base.selected_index >= worker.option_count
            or worker.candidate.selected_index >= worker.option_count
            or set(worker.split_results) != {'train', 'validation', 'test'}
            or any(worker.split_results[split].count != binding['split_counts'][split]
                   for split in ('train', 'validation', 'test'))
            or worker.adapter_hook_calls < len(decisions)):
        raise ValueError('adapter_runtime_worker_binding')
    report = AdapterRuntimeProbeReport(
        **{key: binding[key] for key in ('dataset_id', 'dataset_manifest_sha256', 'input_sha256')},
        deployment_manifest_sha256=candidate.deployment_manifest_sha256,
        candidate_report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        artifact_sha256=artifact_sha256, runner_sha256=runner_sha256,
        option_count=len(decisions[0]['options']), base=worker.base, candidate=worker.candidate,
        split_results=worker.split_results,
        adapter_hook_calls=worker.adapter_hook_calls)
    if (dataset_input(dataset)[0] != binding
            or bounded_file(manifest) != manifest_bytes
            or private_report_bytes(report_path.absolute()) != report_bytes
            or runner_digest() != runner_sha256
            or verify_published_candidate(dataset, manifest, report_path) != candidate):
        raise ValueError('adapter_runtime_sources_changed')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='Isolated synthetic S1 adapter runtime smoke', allow_abbrev=False)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--model-python', type=Path, required=True)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--runtime-smoke', action='store_true')
    arguments = parser.parse_args()
    try:
        report = probe_runtime(arguments.dataset, arguments.manifest, arguments.report,
                               arguments.model_python, include_synthetic=arguments.include_synthetic,
                               runtime_smoke=arguments.runtime_smoke)
        print(canonical(report.model_dump()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Adapter runtime smoke unavailable: private synthetic candidate or pinned model failed. Deployment unchanged.\n')


if __name__ == '__main__':
    main()
