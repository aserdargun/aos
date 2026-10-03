from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .contracts import canonical, digest, now
from .dataset_audit import expected_schema, schema_signature
from .owned_parameter_skill_candidate import _hash, _private_database
from .web_goal_execution_journal import MAX_INTENTS, MAX_RECORD_BYTES


TABLE = 'owned_parameter_skill_review_history'
MIGRATION_REQUIRED = 'owned_parameter_skill_review_history_migration_required'


def _record(value):
    from .owned_parameter_skill_review import (
        OwnedParameterSkillReview, OwnedParameterSkillReviewRevocation)

    models = {'owned_parameter_skill_manual_review': ('accept', OwnedParameterSkillReview),
              'owned_parameter_skill_manual_review_revocation': ('revoke', OwnedParameterSkillReviewRevocation)}
    selected = models.get(value.get('kind'))
    if selected is None:
        raise ValueError('owned_parameter_review_history_record_invalid')
    stage, model = selected
    record = model.model_validate_json(canonical(value)).model_dump(mode='json')
    content = canonical(record)
    if len(content.encode()) > MAX_RECORD_BYTES:
        raise ValueError('owned_parameter_review_history_record_too_large')
    checksum = digest(record)
    review_sha256 = checksum if stage == 'accept' else record['review_sha256']
    return record, checksum, stage, review_sha256, content


class OwnedParameterSkillReviewHistory:
    def __init__(self, database):
        self.database = Path(database).absolute()
        self._database_identity = None

    @contextmanager
    def _connection(self, *, write=False):
        with _private_database(self.database) as identity:
            if self._database_identity is not None and identity != self._database_identity:
                raise ValueError('owned_parameter_review_history_database_changed')
            mode = 'rw' if write else 'ro'
            connection = sqlite3.connect(self.database.as_uri() + '?mode=' + mode,
                                         uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('PRAGMA synchronous=FULL')
                if not write:
                    connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                present = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
                if present is None:
                    raise ValueError(MIGRATION_REQUIRED)
                actual_versions = [tuple(row) for row in connection.execute(
                    'SELECT version,name FROM schema_migrations ORDER BY version')]
                if not actual_versions or actual_versions[-1][0] < 21:
                    raise ValueError('owned_parameter_review_history_schema_changed')
                expected_signature, versions, _hashes = expected_schema(actual_versions[-1][0])
                if schema_signature(connection) != expected_signature or actual_versions != versions:
                    raise ValueError('owned_parameter_review_history_schema_changed')
                if self._database_identity is None:
                    self._database_identity = identity
                yield connection
                with _private_database(self.database) as current:
                    if current != identity:
                        raise ValueError('owned_parameter_review_history_database_changed')
                if write:
                    connection.commit()
            finally:
                connection.close()

    def _records(self, connection, directory_sha256):
        rows = connection.execute(
            'SELECT * FROM owned_parameter_skill_review_history '
            'WHERE review_directory_sha256=? ORDER BY record_sha256 LIMIT ?',
            (_hash(directory_sha256), MAX_INTENTS * 2 + 1)).fetchall()
        if len(rows) > MAX_INTENTS * 2:
            raise ValueError('owned_parameter_review_history_full')
        result = {}
        for row in rows:
            from .owned_parameter_skill_review import (
                OwnedParameterSkillReview, OwnedParameterSkillReviewRevocation)

            model = OwnedParameterSkillReview if row['stage'] == 'accept' else OwnedParameterSkillReviewRevocation
            if len(row['record_json'].encode()) > MAX_RECORD_BYTES:
                raise ValueError('owned_parameter_review_history_record_too_large')
            parsed = model.model_validate_json(row['record_json']).model_dump(mode='json')
            record, checksum, stage, review_sha256, content = _record(parsed)
            if (content != row['record_json'] or checksum != row['record_sha256']
                    or stage != row['stage'] or review_sha256 != row['review_sha256']
                    or any(record[key] != row[key] for key in (
                        'review_directory_sha256', 'candidate_sha256', 'source_fingerprint_sha256'))):
                raise ValueError('owned_parameter_review_history_row_changed')
            result[checksum + '.' + stage + '.json'] = record
        for row in rows:
            if row['stage'] == 'revoke':
                review = result.get(row['review_sha256'] + '.accept.json')
                revocation = result[row['record_sha256'] + '.revoke.json']
                if review is None or any(review[key] != revocation[key] for key in (
                        'candidate_sha256', 'source_fingerprint_sha256', 'scope',
                        'review_directory_sha256', 'review_parent_identity')):
                    raise ValueError('owned_parameter_review_history_binding_changed')
        return result

    def records(self, directory_sha256):
        with self._connection() as connection:
            return self._records(connection, directory_sha256)

    def authorize(self, value):
        record, checksum, stage, review_sha256, content = _record(value)
        directory_sha256 = record['review_directory_sha256']
        filename = checksum + '.' + stage + '.json'
        with self._connection(write=True) as connection:
            records = self._records(connection, directory_sha256)
            if filename in records:
                if records[filename] != record:
                    raise ValueError('owned_parameter_review_history_conflict')
                return
            if len(records) >= MAX_INTENTS * 2:
                raise ValueError('owned_parameter_review_history_full')
            if stage == 'accept':
                if any(existing['candidate_sha256'] == record['candidate_sha256']
                       for existing in records.values()):
                    raise ValueError('owned_parameter_review_history_candidate_already_bound')
            else:
                review = records.get(review_sha256 + '.accept.json')
                if review is None or any(review[key] != record[key] for key in (
                        'candidate_sha256', 'source_fingerprint_sha256', 'scope',
                        'review_directory_sha256', 'review_parent_identity')):
                    raise ValueError('owned_parameter_review_history_binding_changed')
            connection.execute(
                'INSERT INTO owned_parameter_skill_review_history '
                '(record_sha256,review_directory_sha256,review_sha256,candidate_sha256,'
                'source_fingerprint_sha256,stage,record_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (checksum, directory_sha256, review_sha256, record['candidate_sha256'],
                 record['source_fingerprint_sha256'], stage, content, now()))
            self._records(connection, directory_sha256)
