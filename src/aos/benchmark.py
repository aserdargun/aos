import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import HELLO_CONTENT, HELLO_PATH, Phase, REPO_ROOT, Settings, State, TypedModel, canonical, digest


CASES = ('file-roundtrip', 'recovery-path')
SOURCE_MODULES = ('__init__', 'benchmark', 'benchmark_worker', 'bounded_process', 'contracts',
                  'computer', 'browser', 'vision', 'storage', 'operator', 'decision', 'supervisor',
                  'registries', 'workspace_identity', 'write_receipts')
GAPS = ('fixture_engines_only', 'recovery_path_partial_missing_authorized_file_only',
        'ten_catalog_tasks_unexecuted', 'no_independent_held_out_tasks', 'no_model_comparison',
        'no_gpu_or_network_isolation_measurement', 'no_rollback_rehearsal', 'policy_not_frozen')


class BenchmarkSample(TypedModel):
    case: Literal['file-roundtrip', 'recovery-path']
    repeat: int = Field(ge=1, le=5)
    run_id: str = Field(pattern='^run-[a-f0-9]{32}$')
    directory: str = Field(pattern='^sample-[0-9]{2}$')
    duration_seconds: float = Field(ge=0, le=65)
    actions: int = Field(ge=2, le=3)
    failed_probe_actions: int = Field(ge=0, le=1)
    fixture_supervisor_calls: int = Field(ge=0, le=1)
    system2_model_calls: Literal[0] = 0
    human_requests: Literal[0] = 0
    independently_verified: Literal[True] = True
    database_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def consistent_counts(self):
        recovery = int(self.case == 'recovery-path')
        if (self.actions, self.failed_probe_actions, self.fixture_supervisor_calls) != (2 + recovery, recovery, recovery):
            raise ValueError('benchmark_count_mismatch')
        return self


class BenchmarkReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['executed_fixture_workspace_baseline'] = 'executed_fixture_workspace_baseline'
    synthetic: Literal[True] = True
    repeats: int = Field(ge=1, le=5)
    samples: list[BenchmarkSample] = Field(min_length=2, max_length=10)
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    catalog_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    promotion_policy_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    configuration_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    environment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    coverage_gaps: list[str] = Field(json_schema_extra={'const': list(GAPS)})
    independent_held_out_tasks: Literal[0] = 0
    real_model: Literal[False] = False
    statistical_superiority_established: Literal[False] = False
    promotion_authorized: Literal[False] = False
    training_authorized: Literal[False] = False

    @model_validator(mode='after')
    def complete_suite(self):
        expected = [(case, repeat, f'sample-{position:02d}')
                    for position, (repeat, case) in enumerate(
                        ((repeat, case) for repeat in range(1, self.repeats + 1) for case in CASES), start=1)]
        if ([(sample.case, sample.repeat, sample.directory) for sample in self.samples] != expected
                or len({sample.run_id for sample in self.samples}) != len(self.samples)
                or self.coverage_gaps != list(GAPS)):
            raise ValueError('benchmark_suite_incomplete_or_mislabelled')
        return self


def bounded_read(path: Path, limit: int = 8 * 1024 * 1024) -> bytes:
    path = path.absolute()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('benchmark_symlink_parent')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('benchmark_unsafe_file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(descriptor)
        linked = path.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
        if len(data) > limit or any(getattr(before, field) != getattr(after, field)
                                   or getattr(before, field) != getattr(linked, field) for field in fields):
            raise ValueError('benchmark_file_changed')
        return data
    finally:
        os.close(descriptor)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identities() -> dict:
    sources = [f'src/aos/{module}.py' for module in SOURCE_MODULES]
    sources += ['pyproject.toml', 'uv.lock', 'examples/supervisor_plan.json',
                'schemas/benchmark.schema.json']
    sources += [str(path.relative_to(REPO_ROOT)) for path in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))]
    catalog = bounded_read(REPO_ROOT / 'benchmarks/tasks.json')
    policy = bounded_read(REPO_ROOT / 'benchmarks/promotion-policy.json')
    tasks = json.loads(catalog)['tasks']
    if len(tasks) != 12 or not set(CASES).issubset({task['id'] for task in tasks}):
        raise ValueError('benchmark_catalog_changed')
    settings = Settings(workspace=Path('/workspace'), database=Path('/private/trace.sqlite'))
    return {
        'source_sha256': digest({source: sha(bounded_read(REPO_ROOT / source)) for source in sources}),
        'catalog_sha256': sha(catalog), 'promotion_policy_sha256': sha(policy),
        'configuration_sha256': digest({'settings': settings.model_dump(mode='json'), 'cases': CASES,
                                        'timeout_seconds': 60, 'max_output_bytes': 65536,
                                        'engine': 'deterministic_fixture', 'supervisor': 'deterministic_fixture'}),
        'environment_sha256': digest({'python': sys.version, 'executable_sha256': sha(Path(sys.executable).read_bytes()),
                                      'packages': {name: importlib.metadata.version(name)
                                                   for name in ('pydantic', 'jsonschema')}}),
    }


def check_directory(directory: Path) -> Path:
    directory = directory.absolute()
    if '..' in directory.parts or any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise ValueError('benchmark_unsafe_directory')
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('benchmark_directory_not_private')
    return directory


def inspect_sample(directory: Path, case: str, run_id: str) -> dict:
    if case not in CASES or not isinstance(run_id, str) or not re.fullmatch('run-[a-f0-9]{32}', run_id):
        raise ValueError('benchmark_invalid_sample_scope')
    check_directory(directory)
    workspace = check_directory(directory / 'workspace')
    if {path.name for path in directory.iterdir()} != {'workspace', 'trace.sqlite', 'trace.sqlite.lock'}:
        raise ValueError('benchmark_sample_file_set')
    if {path.name for path in workspace.iterdir()} != {'hello.txt'}:
        raise ValueError('benchmark_workspace_file_set')
    artifact = bounded_read(workspace / 'hello.txt', 1024)
    if artifact != HELLO_CONTENT.encode():
        raise ValueError('benchmark_independent_artifact_mismatch')
    database = directory / 'trace.sqlite'
    data = bounded_read(database)
    if len(data) < 100 or data[:16] != b'SQLite format 3\x00' or data[18:20] != b'\x01\x01':
        raise ValueError('benchmark_requires_closed_database')
    recovery = int(case == 'recovery-path')
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.deserialize(data)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('benchmark_database_integrity')
        if connection.execute('SELECT run_id,status,outcome,training_eligible FROM runs').fetchall() != [(run_id, 'succeeded', 'passed', 0)]:
            raise ValueError('benchmark_run_not_verified')
        states = connection.execute('SELECT run_id,state_version,state_json FROM runtime_states').fetchall()
        if len(states) != 1:
            raise ValueError('benchmark_terminal_state_missing')
        state_run, version, state_json = states[0]
        state = State.model_validate_json(state_json)
        expected_supervisor = 'benchmark-fixture-supervisor-v1' if recovery else None
        if (state_run != run_id or state.run_id != run_id or state.state_version != version
                or state.phase != Phase.SUCCEEDED or state.owner != 'AGENT' or state.task_kind != 'hello'
                or state.authorized_path != HELLO_PATH or state.authorized_content != HELLO_CONTENT
                or state.deployment_id != 'fixture-decision-v1' or state.recovery_attempts != recovery
                or state.supervisor_deployment_id != expected_supervisor):
            raise ValueError('benchmark_terminal_state_mismatch')
        run_binding = connection.execute('SELECT task_id,environment_json FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if (run_binding[0] != state.task_id or json.loads(run_binding[1])['runtime_id'] != state.runtime_id
                or connection.execute('SELECT run_id,step_id,state FROM steps').fetchall()
                != [(run_id, state.step_id, 'SUCCEEDED')]):
            raise ValueError('benchmark_terminal_binding_mismatch')
        snapshots = connection.execute(
            'SELECT state_version,state_json,content_sha256 FROM state_snapshots WHERE run_id=? ORDER BY state_version DESC LIMIT 1',
            (run_id,)).fetchall()
        if (len(snapshots) != 1 or snapshots[0][0] != version
                or State.model_validate_json(snapshots[0][1]) != state
                or snapshots[0][2] != digest(state.model_dump(mode='json'))):
            raise ValueError('benchmark_terminal_snapshot_mismatch')
        actions = connection.execute('SELECT tool,status,error_code FROM actions WHERE run_id=? ORDER BY created_at, rowid', (run_id,)).fetchall()
        expected = [('filesystem.read', 'error', 'ELEMENT_MISSING')] if recovery else []
        expected += [('filesystem.write', 'ok', None), ('filesystem.read', 'ok', None)]
        if actions != expected:
            raise ValueError('benchmark_action_evidence_mismatch')
        arguments = [json.loads(row[0]) for row in connection.execute(
            'SELECT arguments_json FROM actions WHERE run_id=? ORDER BY created_at,rowid', (run_id,))]
        expected_arguments = [{'path': HELLO_PATH}] if recovery else []
        expected_arguments += [{'path': HELLO_PATH, 'content': HELLO_CONTENT}, {'path': HELLO_PATH}]
        if arguments != expected_arguments:
            raise ValueError('benchmark_action_scope_mismatch')
        verification = connection.execute(
            'SELECT result,method,expected_json,actual_json FROM verifications WHERE run_id=?', (run_id,)).fetchall()
        if verification != [('passed', 'independent_read_equals', canonical(HELLO_CONTENT), canonical(HELLO_CONTENT))]:
            raise ValueError('benchmark_verification_missing')
        if connection.execute('SELECT count(*) FROM model_calls').fetchone()[0] != 0:
            raise ValueError('benchmark_fixture_model_call')
        escalations = connection.execute('SELECT outcome FROM supervisor_escalations WHERE run_id=?', (run_id,)).fetchall()
        if escalations != [('recovered',)] * recovery:
            raise ValueError('benchmark_recovery_evidence_mismatch')
        human = connection.execute("SELECT count(*) FROM decisions WHERE selected_option='ask_human'").fetchone()[0]
        if human:
            raise ValueError('benchmark_unfinished_human_request')
    if bounded_read(database) != data or bounded_read(workspace / 'hello.txt', 1024) != artifact:
        raise ValueError('benchmark_evidence_changed')
    return {'actions': len(actions), 'failed_probe_actions': recovery, 'fixture_supervisor_calls': len(escalations),
            'human_requests': human, 'database_sha256': sha(data), 'artifact_sha256': sha(artifact)}


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def run_benchmark(output: Path, *, repeats: int = 1, include_synthetic: bool = False) -> BenchmarkReport:
    if include_synthetic is not True or type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError('benchmark_explicit_fixture_scope_required')
    output = output.absolute()
    if output.parent != (REPO_ROOT / 'runs').absolute() or not re.fullmatch(r'benchmark-[a-z0-9][a-z0-9-]{0,63}', output.name):
        raise ValueError('benchmark_requires_new_private_runs_directory')
    if any(parent.is_symlink() for parent in output.parents):
        raise ValueError('benchmark_symlink_parent')
    pinned = identities()
    output.mkdir(mode=0o700)
    check_directory(output)
    samples = []
    for repeat in range(1, repeats + 1):
        for case in CASES:
            sample_name = f'sample-{len(samples) + 1:02d}'
            directory = output / sample_name
            directory.mkdir(mode=0o700)
            started = time.perf_counter()
            result = run_bounded([str(Path(sys.executable).absolute()), '-m', 'aos.benchmark_worker',
                                  '--directory', str(directory), '--case', case], input=b'',
                                 env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(REPO_ROOT / 'src'),
                                      'PYTHONDONTWRITEBYTECODE': '1', 'CUDA_VISIBLE_DEVICES': ''},
                                 timeout=60, max_output=65536)
            duration = time.perf_counter() - started
            if result.returncode != 0:
                raise ValueError('benchmark_child_failed_incomplete_suite')
            response = json.loads(result.stdout)
            if set(response) != {'run_id'}:
                raise ValueError('benchmark_invalid_child_response')
            metrics = inspect_sample(directory, case, response['run_id'])
            samples.append(BenchmarkSample(case=case, repeat=repeat, run_id=response['run_id'],
                                           directory=sample_name, duration_seconds=duration, **metrics))
    if identities() != pinned:
        raise ValueError('benchmark_pins_changed_during_execution')
    report = BenchmarkReport(repeats=repeats, samples=samples, coverage_gaps=list(GAPS), **pinned)
    write_private(output / 'report.json', canonical(report.model_dump()).encode())
    return report


def verify_benchmark(output: Path, expected_sha256: str) -> BenchmarkReport:
    output = check_directory(output)
    if not re.fullmatch('[a-f0-9]{64}', expected_sha256):
        raise ValueError('benchmark_external_checksum_required')
    raw = bounded_read(output / 'report.json', 65536)
    if sha(raw) != expected_sha256:
        raise ValueError('benchmark_report_checksum_mismatch')
    report = BenchmarkReport.model_validate_json(raw)
    pinned = identities()
    if any(getattr(report, key) != value for key, value in pinned.items()):
        raise ValueError('benchmark_pins_differ')
    if {path.name for path in output.iterdir()} != {'report.json', *(sample.directory for sample in report.samples)}:
        raise ValueError('benchmark_output_file_set')
    for sample in report.samples:
        metrics = inspect_sample(output / sample.directory, sample.case, sample.run_id)
        if any(getattr(sample, key) != value for key, value in metrics.items()):
            raise ValueError('benchmark_sample_evidence_changed')
    if identities() != pinned or bounded_read(output / 'report.json', 65536) != raw:
        raise ValueError('benchmark_verification_race')
    return report


def main():
    parser = argparse.ArgumentParser(description='İzole workspace fixture benchmark; model değerlendirmesi değildir')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--include-synthetic', action='store_true')
    parser.add_argument('--verify-sha256')
    arguments = parser.parse_args()
    try:
        if arguments.verify_sha256:
            if arguments.include_synthetic or arguments.repeats != 1:
                raise ValueError('benchmark_verify_is_read_only')
            report = verify_benchmark(arguments.output, arguments.verify_sha256)
        else:
            report = run_benchmark(arguments.output, repeats=arguments.repeats, include_synthetic=arguments.include_synthetic)
        print(canonical(report.model_dump()))
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Benchmark tamamlanmadı: {type(error).__name__}.\n')


if __name__ == '__main__':
    main()
