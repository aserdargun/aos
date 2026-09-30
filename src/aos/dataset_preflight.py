import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import KINDS, SPLITS
from .dataset_readiness import readiness
from .dataset_tokenizer import validate_probe_report
from .workspace_identity import open_existing_workspace, workspace_identity


def bounded_file(path: Path, limit: int = 262144) -> bytes:
    parent = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError('preflight_file_bound')
            content = os.read(descriptor, limit + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if len(content) != before.st_size or any(getattr(before, field) != getattr(value, field)
                                                   for value in (after, linked) for field in fields):
                raise ValueError('preflight_file_changed')
            return content
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def dataset_input(dataset: Path) -> tuple[dict, list[dict], dict]:
    descriptor = open_existing_workspace(dataset)
    try:
        identity = workspace_identity(dataset, descriptor)
        names = ['manifest.json', 'quality_report.json', 'quarantine.jsonl']
        names.extend(f'{kind}/{split}{suffix}.jsonl' for kind in KINDS for split in SPLITS for suffix in ('', '.converted'))
        files = {name: bounded_file(dataset / name) for name in names}
        if sum(map(len, files.values())) > 1048576:
            raise ValueError('preflight_dataset_bound')
        inventory = readiness(dataset)
        manifest = json.loads(files['manifest.json'])
        if inventory['dataset_id'] != manifest['dataset_id']:
            raise ValueError('preflight_manifest_changed')
        examples = []
        counts = {}
        for split in SPLITS:
            rows = [json.loads(line) for line in files[f'system1_choice/{split}.converted.jsonl'].splitlines()]
            counts[split] = len(rows)
            examples.extend(rows)
        if not 1 <= len(examples) <= 32:
            raise ValueError('preflight_examples_bound')
        check = open_existing_workspace(dataset)
        try:
            if workspace_identity(dataset, check) != identity:
                raise ValueError('preflight_dataset_replaced')
        finally:
            os.close(check)
        if any(bounded_file(dataset / name) != content for name, content in files.items()):
            raise ValueError('preflight_dataset_changed')
        binding = {'dataset_id': manifest['dataset_id'],
                   'dataset_manifest_sha256': hashlib.sha256(files['manifest.json']).hexdigest(),
                   'input_sha256': digest(examples), 'split_counts': counts,
                   'workspace_sha256': digest(identity.model_dump())}
        return binding, examples, inventory
    finally:
        os.close(descriptor)


def probe_schema() -> dict:
    schema = json.loads((REPO_ROOT / 'schemas/dataset_tokenizer_probe.schema.json').read_text())

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            return expand(schema['$defs'][value['$ref'].rsplit('/', 1)[-1]])
        return {key: expand(item) for key, item in value.items() if key not in {'$defs', '$schema'}}

    return expand(schema)


class DatasetTokenizerReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['dataset_bound_s1_tokenizer_v1'] = 'dataset_bound_s1_tokenizer_v1'
    dataset_id: str = Field(pattern='^fixture-[a-f0-9]{64}$')
    dataset_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_manifest_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    runner_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    split_counts: dict[Literal['train', 'validation', 'test'], Annotated[int, Field(ge=0, le=32)]] = Field(
        json_schema_extra={'required': list(SPLITS), 'minProperties': 3, 'maxProperties': 3})
    tokenizer_report: dict = Field(json_schema_extra=probe_schema())
    synthetic: Literal[True] = True
    training_ready: Literal[False] = False
    forward_loss_verified: Literal[False] = False
    supervisor_tokenizer_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def valid_counts(self):
        if set(self.split_counts) != set(SPLITS) or any(type(count) is not int or count < 0 for count in self.split_counts.values()):
            raise ValueError('preflight_split_counts')
        validate_probe_report(self.tokenizer_report, sum(self.split_counts.values()), self.tokenizer_report.get('token_budget', 0))
        return self


def runner_digest() -> str:
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in
                   ('src/aos/dataset_preflight.py', 'src/aos/bounded_process.py', 'src/aos/dataset.py', 'src/aos/dataset_readiness.py',
                    'src/aos/dataset_tokenizer.py', 'schemas/dataset_tokenizer_probe.schema.json',
                    'services/decider/tokenizer_probe.py')})


def check_probe_pins(probe: dict, pins: dict) -> None:
    expected = json.loads(bounded_file(REPO_ROOT / 'examples/dataset_converter_pin.json'))
    if (any(probe[key] != pins[key] for key in ('checkpoint_revision', 'tokenizer_revision', 'code_revision'))
            or probe['core_sha256'] != expected['files']['core.py']
            or any(pins['model_files'].get(key) != value for key, value in probe['tokenizer_files'].items())
            or any(pins['code_files'].get(key) != value for key, value in probe['code_files'].items())
            or any(pins['dependencies'].get(key) != value for key, value in probe['dependencies'].items())):
        raise ValueError('preflight_probe_pin_mismatch')


def dataset_tokenizer_preflight(dataset: Path, manifest: Path, python: Path, source: Path, *,
                                include_synthetic: bool = False, max_tokens: int = 1536) -> DatasetTokenizerReport:
    if not include_synthetic:
        raise ValueError('synthetic_opt_in_required')
    if type(max_tokens) is not int or not 64 <= max_tokens <= 1536:
        raise ValueError('preflight_token_budget')
    binding, examples, _ = dataset_input(dataset)
    deployment_bytes = bounded_file(manifest)
    runner = runner_digest()
    request = canonical({'examples': examples, 'max_tokens': max_tokens})
    if len(request.encode()) > 1048576:
        raise ValueError('preflight_request_bound')
    result = run_bounded([str(python.absolute()), str(REPO_ROOT / 'services/decider/tokenizer_probe.py'),
                             str(manifest.absolute()), str(source.absolute())], input=request.encode(), timeout=120,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                                 'TOKENIZERS_PARALLELISM': 'false', 'CUDA_VISIBLE_DEVICES': '', 'OMP_NUM_THREADS': '1'})
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('dataset_tokenizer_failed')
    probe = json.loads(result.stdout)
    validate_probe_report(probe, len(examples), max_tokens)
    check_probe_pins(probe, json.loads(deployment_bytes))
    if (dataset_input(dataset)[0] != binding or bounded_file(manifest) != deployment_bytes or runner_digest() != runner):
        raise ValueError('preflight_sources_changed')
    return DatasetTokenizerReport(**{key: value for key, value in binding.items() if key != 'workspace_sha256'},
                                  deployment_manifest_sha256=hashlib.sha256(deployment_bytes).hexdigest(),
                                  runner_sha256=runner, tokenizer_report=probe)


def verify_dataset_tokenizer_report(report: dict, dataset: Path, manifest: Path) -> DatasetTokenizerReport:
    parsed = DatasetTokenizerReport.model_validate(report)
    binding, _, _ = dataset_input(dataset)
    deployment_bytes = bounded_file(manifest)
    if (any(getattr(parsed, key) != value for key, value in binding.items() if key != 'workspace_sha256')
            or parsed.deployment_manifest_sha256 != hashlib.sha256(deployment_bytes).hexdigest()
            or parsed.runner_sha256 != runner_digest()):
        raise ValueError('preflight_report_binding_mismatch')
    check_probe_pins(parsed.tokenizer_report, json.loads(deployment_bytes))
    return parsed


def main():
    parser = argparse.ArgumentParser(description='Dataset split bağlı S1 tokenizer preflight; ağırlık, eğitim veya promotion yok')
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-python', type=Path)
    parser.add_argument('--upstream-source', type=Path)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--max-tokens', type=int, default=1536)
    parser.add_argument('--verify-report', type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.verify_report is not None:
            report = verify_dataset_tokenizer_report(json.loads(bounded_file(arguments.verify_report)), arguments.dataset, arguments.manifest)
        else:
            if arguments.model_python is None or arguments.upstream_source is None:
                raise ValueError('preflight_local_worker_required')
            report = dataset_tokenizer_preflight(arguments.dataset, arguments.manifest, arguments.model_python, arguments.upstream_source,
                                                include_synthetic=arguments.include_synthetic, max_tokens=arguments.max_tokens)
        print(canonical(report.model_dump()))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Dataset tokenizer denetimi başarısız; immutable dataset ve yerel pin bağlarını kontrol edin. Eğitim yapılmadı.\n')


if __name__ == '__main__':
    main()
