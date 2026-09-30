import json
import sqlite3
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, REMOTE_FORM_SCOPE, Phase, State, digest
from aos.site_skill_form_invocation_audit import (
    SiteSkillFormInvocationAuditReport,
    _timestamp,
    _state_history,
)


class SiteSkillFormInvocationAuditTests(unittest.TestCase):
    def test_synthetic_report_fixture_matches_canonical_schema(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation_audit.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_invocation_audit.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillFormInvocationAuditReport.model_json_schema())
        report = fixture['report']
        jsonschema.Draft202012Validator(schema).validate(report)
        self.assertEqual(SiteSkillFormInvocationAuditReport.model_validate_json(
            json.dumps(report)).model_dump(mode='json'), report)
        for field in ('symbolic_skill_steps_executed', 'skill_executed',
                      'site_outcome_verified', 'held_out_independence_verified',
                      'reviewed', 'activation_authorized', 'training_ready'):
            self.assertFalse(report[field])
        with self.assertRaises(ValueError):
            SiteSkillFormInvocationAuditReport.model_validate_json(json.dumps({
                **report, 'stages': list(reversed(report['stages']))}))

    def test_malformed_timestamps_fail_closed(self):
        for value in ('not-a-date', '999999999999-01-01T00:00:00+00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _timestamp(value)

    def test_state_history_requires_initial_and_every_intermediate_invocation_pin(self):
        invocation_sha256 = 'a' * 64
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript('''CREATE TABLE state_snapshots(
            snapshot_id TEXT,run_id TEXT,step_id TEXT,state_version INTEGER,state_json TEXT,
            content_sha256 TEXT,created_at TEXT);
            CREATE TABLE runtime_states(run_id TEXT,state_version INTEGER,state_json TEXT);
            CREATE TABLE runs(run_id TEXT,task_id TEXT,status TEXT,outcome TEXT,
                policy_version TEXT,ended_at TEXT,deployment_snapshot_json TEXT);''')
        states = []
        phases = ['CREATED', *(phase for _ in range(6) for phase in (
            'OBSERVE', 'DECIDE', 'POLICY', 'EXECUTE', 'VERIFY')), 'SUCCEEDED']
        for version in range(32):
            state = State(task_kind='browser_remote_form', task_id='task-audit',
                          run_id='run-audit', step_id='step-audit', runtime_id='runtime-audit',
                          deployment_id='deployment-audit', owner_lease_id='lease-audit',
                          state_version=version,
                          phase=Phase(phases[version]),
                          authorized_path=REMOTE_FORM_SCOPE,
                          authorized_content='b' * 64,
                          skill_invocation_sha256=invocation_sha256)
            states.append(state)
            connection.execute('INSERT INTO state_snapshots VALUES(?,?,?,?,?,?,?)', (
                f'snapshot-{version}', state.run_id, state.step_id, version,
                state.model_dump_json(), digest(state.model_dump(mode='json')),
                           f'2026-09-26T00:00:{version:02d}+00:00'))
        connection.execute('INSERT INTO runtime_states VALUES(?,?,?)',
                           ('run-audit', 31, states[-1].model_dump_json()))
        connection.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
                           ('run-audit', 'task-audit', 'succeeded', 'passed',
                            'browser-remote-form-policy-v1', '2026-09-26T00:00:32+00:00',
                            '{"deployment_id":"deployment-audit"}'))
        self.assertEqual(len(_state_history(connection, 'run-audit', invocation_sha256,
                                            'b' * 64)), 32)

        missing_initial = states[0].model_copy(update={'skill_invocation_sha256': None})
        connection.execute('UPDATE state_snapshots SET state_json=?,content_sha256=? WHERE state_version=0',
                            (missing_initial.model_dump_json(), digest(missing_initial.model_dump(mode='json'))))
        with self.assertRaises(ValueError):
            _state_history(connection, 'run-audit', invocation_sha256, 'b' * 64)
        connection.execute('UPDATE state_snapshots SET state_json=?,content_sha256=? WHERE state_version=0',
                            (states[0].model_dump_json(), digest(states[0].model_dump(mode='json'))))

        missing_middle = states[14].model_copy(update={'skill_invocation_sha256': None})
        connection.execute('UPDATE state_snapshots SET state_json=?,content_sha256=? WHERE state_version=14',
                            (missing_middle.model_dump_json(), digest(missing_middle.model_dump(mode='json'))))
        with self.assertRaises(ValueError):
            _state_history(connection, 'run-audit', invocation_sha256, 'b' * 64)
        connection.close()


if __name__ == '__main__':
    unittest.main()
