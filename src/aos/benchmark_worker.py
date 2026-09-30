import argparse
import asyncio
import json
from pathlib import Path

from .benchmark import CASES, bounded_read, check_directory
from .computer import WorkspaceRuntime
from .contracts import REPO_ROOT, Settings, canonical
from .decision import FixtureDecisionEngine
from .operator import Operator
from .storage import TrajectoryStore
from .supervisor import RecoveryPlan


class BenchmarkFixtureSupervisor:
    identity = {'deployment_id': 'benchmark-fixture-supervisor-v1', 'kind': 'deterministic_fixture', 'real_model': False}
    last_metrics = {}

    async def plan(self, problem, evidence):
        value = json.loads(bounded_read(REPO_ROOT / 'examples/supervisor_plan.json'))['plan']
        value['evidence_refs'] = [item['id'] for item in evidence]
        return RecoveryPlan.model_validate(value)


def execute(directory: Path, case: str) -> str:
    if case not in CASES:
        raise ValueError('benchmark_case_not_allowed')
    directory = check_directory(directory)
    if list(directory.iterdir()):
        raise ValueError('benchmark_worker_requires_fresh_directory')
    settings = Settings(workspace=directory / 'workspace', database=directory / 'trace.sqlite')
    runtime = WorkspaceRuntime(settings.workspace)
    store = None
    try:
        runtime.start()
        store = TrajectoryStore(settings.database)
        supervisor = BenchmarkFixtureSupervisor() if case == 'recovery-path' else None
        result = asyncio.run(Operator(settings, store, runtime, FixtureDecisionEngine(), supervisor).hello(
            recovery_probe=case == 'recovery-path'))
        if result['status'] != 'succeeded' or result['real_model'] or result['system2_calls']:
            raise ValueError('benchmark_fixture_execution_failed')
        store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if store.connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
            raise ValueError('benchmark_snapshot_not_closed')
        return result['run_id']
    finally:
        if store is not None:
            store.close()
        runtime.stop()


def main():
    parser = argparse.ArgumentParser(description='Internal bounded fixture-only benchmark worker')
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--case', required=True, choices=CASES)
    arguments = parser.parse_args()
    print(canonical({'run_id': execute(arguments.directory, arguments.case)}))


if __name__ == '__main__':
    main()
