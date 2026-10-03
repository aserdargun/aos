"""Exercise the delegated-job runner with fixed synthetic CPU subprocesses only."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from aos.agent_contracts import AgentAdmissionError, AgentAuthority, AgentBudget, AgentJobRequest, AgentRegistration
from aos.agent_orchestration import AgentOrchestrator
from aos.agent_orchestration_store import AgentOrchestrationStore
from aos.agent_synthetic import SyntheticAgentAdapter
from aos.contracts import canonical, now


class LostDispatchAcknowledgement(SyntheticAgentAdapter):
    lose_acknowledgement = False

    async def dispatch(self, prepared):
        handle = await super().dispatch(prepared)
        if self.lose_acknowledgement:
            self.lose_acknowledgement = False
            raise RuntimeError('Explicitly simulated lost dispatch acknowledgement after real CPU process launch')
        return handle


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def run_cpu(root):
    started = time.monotonic()
    root = root.absolute()
    root.mkdir(mode=0o700)
    workspaces = root / 'workspaces'
    workspaces.mkdir(mode=0o700)
    adapter = LostDispatchAcknowledgement(workspaces)
    store = AgentOrchestrationStore(root / 'orchestration.sqlite3')
    authority = AgentAuthority(principal='synthetic-cpu-operator', runtime_id='synthetic-cpu-runtime',
                               lease_id='synthetic-cpu-lease', owner='AGENT', generation=1)

    def orchestrator():
        return AgentOrchestrator(store, adapters={'synthetic.cpu.v1': adapter}, current_authority=lambda request: authority)

    def request(job_id, *, delay_ms=0, dependencies=None):
        return AgentJobRequest(job_id=job_id, agent_id='synthetic-cpu', agent_version='1.0',
            operation='synthetic.write.v1', authority=authority,
            budget=AgentBudget(wall_seconds=5, model_tokens=0, experiments=0),
            dependencies=dependencies or [], payload={'text': 'Explicitly synthetic output for ' + job_id, 'delay_ms': delay_ms})

    checks = []
    try:
        runner = orchestrator()
        runner.register(AgentRegistration(agent_id='synthetic-cpu', version='1.0', adapter_kind='synthetic.cpu.v1',
            capabilities=['synthetic.write.v1'], max_active_instances=2, cleanup_scope='local_process',
            per_job_budget=AgentBudget(wall_seconds=5, model_tokens=0, experiments=0),
            aggregate_budget=AgentBudget(wall_seconds=10, model_tokens=0, experiments=0)))
        for task in (request('cancel-first', delay_ms=1000), request('complete-second', delay_ms=300),
                     request('dependent-third', dependencies=['complete-second']), request('quota-probe')):
            runner.submit(task)
        try:
            await runner.advance('dependent-third')
        except AgentAdmissionError:
            checks.append('dependent_dispatch_denied_before_verified_parent')
        else:
            raise AssertionError('Unverified dependency was dispatched')
        for job_id in ('cancel-first', 'complete-second'):
            await runner.advance(job_id)
            await runner.advance(job_id)
        handles = [runner.status(job_id).handle for job_id in ('cancel-first', 'complete-second')]
        processes = [adapter._owned[handle.handle_id].process for handle in handles]
        require(processes[0].pid != processes[1].pid and all(process.poll() is None for process in processes),
                'Two distinct CPU processes must overlap')
        checks.append('two_real_isolated_cpu_subprocesses_overlap')
        try:
            await runner.advance('quota-probe')
        except AgentAdmissionError:
            checks.append('third_active_instance_denied_by_atomic_capacity')
        else:
            raise AssertionError('Active instance limit was exceeded')
        cancelled = await runner.request_cancel('cancel-first')
        require(cancelled.state == 'cancelled' and not cancelled.reservation_held, 'Cancellation cleanup was not verified')
        require(processes[1].poll() is None, 'Cancelling the first worker affected the second worker')
        await runner.request_cancel('quota-probe')
        checks.append('one_job_cancelled_without_affecting_other')
        async with asyncio.timeout(10):
            while runner.status('complete-second').state != 'succeeded':
                await runner.advance('complete-second')
                await asyncio.sleep(0.02)
            while runner.status('dependent-third').state != 'succeeded':
                await runner.advance('dependent-third')
                await asyncio.sleep(0.02)
        checks.append('dependency_runs_only_after_independent_result_and_cleanup')
        stale = request('stale-probe').model_copy(update={'authority': authority.model_copy(update={'generation': 0})})
        try:
            runner.submit(stale)
        except AgentAdmissionError:
            checks.append('stale_generation_rejected_before_any_effect')
        else:
            raise AssertionError('Stale generation was admitted')
        runner.submit(request('lost-ack-probe', delay_ms=100))
        await runner.advance('lost-ack-probe')
        adapter.lose_acknowledgement = True
        try:
            await runner.advance('lost-ack-probe')
        except RuntimeError:
            pass
        else:
            raise AssertionError('Synthetic lost acknowledgement was not exercised')
        require(runner.status('lost-ack-probe').state == 'uncertain', 'Lost acknowledgement must remain uncertain')
        process_count = len(adapter._owned)
        store.close()
        store = AgentOrchestrationStore(root / 'orchestration.sqlite3')
        runner = orchestrator()
        require(runner.status('lost-ack-probe').reservation_held, 'Store reopen released uncertain capacity')
        try:
            await runner.advance('lost-ack-probe')
        except AgentAdmissionError:
            checks.append('reopened_store_refuses_uncertain_dispatch_replay')
        else:
            raise AssertionError('Uncertain effect was replayed after reopening the store')
        async with asyncio.timeout(5):
            while runner.status('lost-ack-probe').state != 'succeeded':
                await runner.reconcile('lost-ack-probe')
                await asyncio.sleep(0.02)
        require(len(adapter._owned) == process_count, 'Read-only reconciliation launched another process')
        checks.append('same_live_adapter_readback_reconciles_without_new_process')
        jobs = {}
        for job_id in ('cancel-first', 'complete-second', 'dependent-third', 'quota-probe', 'lost-ack-probe'):
            status = runner.status(job_id)
            require(not status.reservation_held, 'Completed CPU scenario still holds capacity')
            jobs[job_id] = {'state': status.state, 'reservation_held': status.reservation_held,
                           'request_sha256': status.request_sha256, 'revision': status.revision}
        require(all(owned.process.poll() is not None for owned in adapter._owned.values()), 'Owned worker remains alive')
        return {'schema_version': 'aos.agent-orchestration-cpu.v1', 'observed_at': now(),
                'synthetic': True, 'actual_cpu_subprocesses': process_count,
                'real_model_or_gpu_test': False, 'scientist_native_acceptance': False,
                'restart_scope': 'SQLite store reopen; original live adapter retains owned process identities',
                'lost_acknowledgement': 'simulated after actual fixed CPU subprocess dispatch',
                'general_agent_sandbox': False, 'checks': checks, 'jobs': jobs,
                'elapsed_seconds': round(time.monotonic() - started, 3)}
    finally:
        await adapter.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu', action='store_true', required=True)
    parser.add_argument('--root', type=Path, required=True, help='New private evidence directory; existing paths are refused')
    arguments = parser.parse_args()
    result = asyncio.run(run_cpu(arguments.root))
    report = arguments.root.absolute() / 'acceptance.private.json'
    descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(canonical(result) + '\n')
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps(result | {'evidence_path': str(report)}, indent=2))


if __name__ == '__main__':
    main()
