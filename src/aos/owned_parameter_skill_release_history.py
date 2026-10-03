"""Append-only manual release authorization in the original trajectory database."""

from contextlib import contextmanager

from .contracts import canonical, digest, now
from .owned_parameter_skill_candidate import _hash
from .owned_parameter_skill_release import MAX_RELEASE_RECORDS, MODELS, _history, _record
from .owned_parameter_skill_review import OwnedParameterSkillReview
from .owned_parameter_skill_review_history import OwnedParameterSkillReviewHistory
from .web_goal_execution_journal import MAX_RECORD_BYTES


TABLE = 'owned_parameter_skill_release_history'
MIGRATION_REQUIRED = 'owned_parameter_skill_release_history_migration_required'


class OwnedParameterSkillReleaseHistory(OwnedParameterSkillReviewHistory):
    @contextmanager
    def _connection(self, *, write=False):
        with super()._connection(write=write) as connection:
            if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
                raise ValueError(MIGRATION_REQUIRED)
            yield connection

    def _records(self, connection, directory_sha256):
        rows = connection.execute(
            'SELECT * FROM owned_parameter_skill_release_history '
            'WHERE release_directory_sha256=? ORDER BY record_sha256 LIMIT ?',
            (_hash(directory_sha256), MAX_RELEASE_RECORDS + 1)).fetchall()
        if len(rows) > MAX_RELEASE_RECORDS:
            raise ValueError('owned_parameter_release_history_full')
        records = {}
        for row in rows:
            if len(row['record_json'].encode()) > MAX_RECORD_BYTES or row['stage'] not in MODELS:
                raise ValueError('owned_parameter_release_history_record_invalid')
            parsed = MODELS[row['stage']].model_validate_json(row['record_json']).model_dump(mode='json')
            record, checksum, stage = _record(parsed)
            sequence = record['revision'] if stage == 'release' else record['sequence']
            release_sha256 = checksum if stage == 'release' else record['release_sha256']
            if (canonical(record) != row['record_json'] or checksum != row['record_sha256']
                    or stage != row['stage'] or sequence != row['sequence']
                    or release_sha256 != row['release_sha256']
                    or record['database_identity'] != self._database_identity
                    or any(record[key] != row[key] for key in (
                        'release_directory_sha256', 'family_sha256', 'review_sha256'))):
                raise ValueError('owned_parameter_release_history_row_changed')
            review_row = connection.execute(
                'SELECT record_json FROM owned_parameter_skill_review_history '
                "WHERE record_sha256=? AND stage='accept'", (record['review_sha256'],)).fetchone()
            if review_row is None:
                raise ValueError('owned_parameter_release_review_anchor_missing')
            review = OwnedParameterSkillReview.model_validate_json(review_row['record_json']).model_dump(mode='json')
            if (digest(review) != record['review_sha256'] or any(record[key] != review[key] for key in (
                    'candidate_sha256', 'source_fingerprint_sha256', 'scope'))
                    or stage == 'release' and any(record[key] != review[key] for key in (
                        'review_directory_sha256', 'manifest_sha256', 'source_run_ref', 'recipe_sha256'))):
                raise ValueError('owned_parameter_release_review_binding_changed')
            records[checksum + '.' + stage + '.json'] = record
        _history(records)
        return records

    def authorize(self, value):
        record, checksum, stage = _record(value)
        filename = checksum + '.' + stage + '.json'
        with self._connection(write=True) as connection:
            records = self._records(connection, record['release_directory_sha256'])
            revoked = connection.execute(
                "SELECT 1 FROM owned_parameter_skill_review_history WHERE review_sha256=? AND stage='revoke'",
                (record['review_sha256'],)).fetchone()
            if revoked:
                raise ValueError('owned_parameter_release_review_not_accepted')
            if filename in records:
                if records[filename] != record:
                    raise ValueError('owned_parameter_release_history_conflict')
                return
            if len(records) >= MAX_RELEASE_RECORDS:
                raise ValueError('owned_parameter_release_history_full')
            _history(records | {filename: record})
            sequence = record['revision'] if stage == 'release' else record['sequence']
            release_sha256 = checksum if stage == 'release' else record['release_sha256']
            connection.execute(
                'INSERT INTO owned_parameter_skill_release_history '
                '(record_sha256,release_directory_sha256,family_sha256,stage,sequence,review_sha256,'
                'release_sha256,record_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (checksum, record['release_directory_sha256'], record['family_sha256'], stage, sequence,
                 record['review_sha256'], release_sha256, canonical(record), now()))
            self._records(connection, record['release_directory_sha256'])
