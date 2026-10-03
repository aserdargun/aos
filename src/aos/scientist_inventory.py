import sqlite3

from .scientist_transport import ScientistAdmissionError
from .scientist_intents import scientist_unresolved_predicate


def scientist_control_inventory(store, session_id):
    absent = {'available': True, 'supported': False, 'pending_count': None,
              'current_session_pending_count': None, 'other_session_pending_count': None,
              'metadata_only': True}
    try:
        connection = store.connection
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('scientist_evidence_controls','scientist_evidence_responses')")}
        if not tables:
            return absent
        if len(tables) != 2:
            raise sqlite3.DatabaseError('Control metadata tables are incomplete')
        row = connection.execute(
            'SELECT count(*),coalesce(sum(CASE WHEN controls.session_id=? THEN 1 ELSE 0 END),0) '
            'FROM scientist_evidence_controls AS controls '
            'LEFT JOIN scientist_evidence_responses AS responses USING(control_id) '
            'WHERE responses.control_id IS NULL', (session_id,)).fetchone()
        total, current = row
        return {'available': True, 'supported': True, 'pending_count': total,
                'current_session_pending_count': current, 'other_session_pending_count': total - current,
                'metadata_only': True}
    except (ValueError, TypeError, IndexError, sqlite3.Error):
        return absent | {'available': False}


def _admission_identity(store, request_id):
    absent = {'available': True, 'present': False, 'record_sha256': None, 'binding_sha256': None}
    try:
        connection = store.connection
        if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scientist_admission_history'").fetchone() is None:
            return absent
        if connection.execute('SELECT 1 FROM scientist_admission_history WHERE request_id=?',
                              (request_id,)).fetchone() is None:
            return absent
        from .scientist_admission_history import ScientistAdmissionHistory

        record, record_sha256 = ScientistAdmissionHistory(store).read(request_id)
        return {'available': True, 'present': True, 'record_sha256': record_sha256,
                'binding_sha256': record.admission_binding_sha256}
    except (ScientistAdmissionError, ValueError, TypeError, IndexError, sqlite3.Error):
        return {'available': False, 'present': False, 'record_sha256': None, 'binding_sha256': None}


def scientist_inference_inventory(controller, scheduler=None):
    connection = controller.store.connection
    unresolved = scientist_unresolved_predicate(connection)
    historical_count = connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0]
    total = connection.execute('SELECT count(*) FROM scientist_turn_intents WHERE ' + unresolved).fetchone()[0]
    unresolved_lab = connection.execute(
        "SELECT count(*) FROM scientist_lab_actions WHERE state='intent'").fetchone()[0]
    current_count = connection.execute(
        'SELECT count(*) FROM scientist_turn_intents WHERE session_id=? AND ' + unresolved,
        (controller.session_id,)).fetchone()[0]
    current_history_count = connection.execute(
        'SELECT count(*) FROM scientist_turn_intents WHERE session_id=?',
        (controller.session_id,)).fetchone()[0]
    rows = connection.execute(
        "SELECT request_id,state,request_sha256,created_at,"
        "json_extract(request_json,'$.profile_id') AS profile_id,"
        "json_extract(request_json,'$.deployment_digest') AS deployment_digest,"
        "json_extract(binding_json,'$.owner') AS owner,"
        "json_extract(binding_json,'$.generation') AS generation,"
        "json_extract(receipt_json,'$.generation.unit') AS worker_unit,"
        "json_extract(receipt_json,'$.generation.invocation_id') AS worker_invocation_id "
        ',CASE WHEN ' + unresolved + ' THEN 0 ELSE 1 END AS resolution_recorded '
        'FROM scientist_turn_intents WHERE session_id=? ORDER BY rowid DESC LIMIT 20',
        (controller.session_id,)).fetchall()
    binding = getattr(scheduler, 'scientist_binding', None)
    client = None if binding is None else binding.engine.client
    valid_models = bool(binding is not None and scheduler.engine is binding.engine
                        and scheduler.vision_supervisor is binding.vision_supervisor)
    controls = scientist_control_inventory(controller.store, controller.session_id)
    return {'configured': binding is not None, 'wire_version': 1,
            'joint_runtime_admitted': False, 'gpu_release_verified': False,
            'admission_blocked': (total > 0 or unresolved_lab > 0 or bool(binding and not valid_models)
                                  or not controls['available']
                                  or controls['supported'] and controls['pending_count'] != 0),
            'model_binding_valid': valid_models, 'unresolved_count': total,
            'historical_count': historical_count, 'resolved_count': historical_count - total,
            'unresolved_lab_effect_count': unresolved_lab,
            'evidence_controls': controls,
            'other_session_count': total - current_count, 'current_session_count': current_count,
            'truncated': current_history_count > len(rows),
            'local_cleanup_pending': bool(client and client.cleanup_pending),
            'intents': [dict(row) | {'resolution_recorded': bool(row['resolution_recorded']),
                                   'admission_identity': _admission_identity(controller.store, row['request_id'])}
                        for row in rows]}
