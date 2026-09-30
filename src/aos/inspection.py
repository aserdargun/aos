from contextlib import closing
from pathlib import Path
import re
import sqlite3


class TrajectoryInspector:
    def __init__(self, path: Path):
        self.path = path.absolute()

    def connection(self):
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        return connection

    def overview(self) -> dict:
        try:
            with closing(self.connection()) as connection:
                runs = [dict(row) for row in connection.execute('''
                    SELECT run_id,status,started_at,training_eligible,
                      (SELECT count(*) FROM model_calls WHERE model_calls.run_id=runs.run_id) AS model_calls,
                      (SELECT count(*) FROM verifications WHERE verifications.run_id=runs.run_id AND result='passed') AS passed,
                      (SELECT count(*) FROM verifications WHERE verifications.run_id=runs.run_id AND result='failed') AS failed
                    FROM runs ORDER BY started_at DESC,run_id DESC LIMIT 30''')]
                models = [dict(row) for row in connection.execute('SELECT model_id,backend,revision,enabled FROM models ORDER BY model_id LIMIT 50')]
                deployments = [dict(row) for row in connection.execute('SELECT deployment_id,model_id,status FROM deployments ORDER BY created_at DESC LIMIT 50')]
                return {'available': True, 'runs': runs, 'models': models, 'deployments': deployments, 'limit': 30}
        except sqlite3.Error:
            return {'available': False, 'reason': 'trajectory_database_unavailable'}

    def trace(self, run_id: str) -> dict | None:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', run_id):
            return None
        try:
            with closing(self.connection()) as connection:
                run = connection.execute('SELECT run_id,status,started_at,ended_at FROM runs WHERE run_id=?', (run_id,)).fetchone()
                if run is None:
                    return None
                selections = {
                    'decisions': 'decision_id,selected_option,confidence,policy_result,created_at',
                    'actions': 'action_id,tool,status,error_code,created_at',
                    'verifications': 'verification_id,method,result,created_at',
                    'model_calls': 'call_id,role,deployment_id,status,latency_ms,input_tokens,output_tokens,created_at',
                }
                return {'run': dict(run), 'limit_per_collection': 100, **{
                    table: [dict(row) for row in connection.execute(
                        f'SELECT {columns} FROM {table} WHERE run_id=? ORDER BY created_at,rowid LIMIT 100', (run_id,))]
                    for table, columns in selections.items()}}
        except sqlite3.Error:
            return None
