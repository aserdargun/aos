import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.lifecycle import observe_process
from aos.shared_drain import (SavedSharedDrainObservation, SharedAdmissionDrain,
                              SharedDrainObservation, SharedDrainRequest, read_shared_drain_receipt)


class SyntheticController:
    def __init__(self):
        self.lock = threading.RLock()
        self.session_id = 'desktop-session-' + '1' * 32
        self.runtime = SimpleNamespace(runtime_id='runtime-synthetic')
        self.current = {'session_id': self.session_id, 'runtime_id': self.runtime.runtime_id,
                        'owner': 'AGENT', 'lease_id': 'lease-synthetic', 'generation': 0, 'status': 'running'}

    def state(self):
        return dict(self.current)


class SyntheticDrainComponent:
    def __init__(self, controller):
        self.controller = controller
        self.latched = False
        self.latches = 0
        self.blockers = []
        self.failure = False
        self.peer = None

    def latch_shared_drain(self):
        self.latched = True
        self.latches += 1

    def shared_drain_status(self):
        if self.failure:
            raise OSError('Synthetic observation failure')
        if self.peer is not None and not self.peer.latched:
            raise AssertionError('Both components must latch before observation')
        return {'admission_closed': self.latched, 'blockers': list(self.blockers)}


class SharedDrainTests(unittest.TestCase):
    def setUp(self):
        self.controller = SyntheticController()
        self.scheduler = SyntheticDrainComponent(self.controller)
        self.lab = SyntheticDrainComponent(self.controller)
        self.scheduler.peer = self.lab
        self.lab.peer = self.scheduler
        self.drain = SharedAdmissionDrain(self.controller, self.scheduler, self.lab)
        self.request = SharedDrainRequest(request_id='synthetic-drain',
            **{key: value for key, value in self.controller.state().items() if key != 'status'})

    def test_both_latches_precede_observation_and_no_gpu_or_remote_claim(self):
        result = self.drain.drain(self.request)
        self.assertTrue(result.admission_closed)
        self.assertTrue(result.local_controls_drained)
        self.assertFalse(result.gpu_release_verified)
        self.assertFalse(result.remote_jobs_stopped_verified)
        self.assertFalse(result.native_gpu_excluded)

    def test_same_request_rechecks_busy_status_without_rearming(self):
        self.scheduler.blockers = ['reserved']
        self.lab.blockers = ['active_control']
        result = self.drain.drain(self.request)
        self.assertTrue(result.admission_closed)
        self.assertFalse(result.local_controls_drained)
        self.assertEqual(result.blockers, ['lab.active_control', 'scheduler.reserved'])
        self.scheduler.blockers = []
        self.lab.blockers = []
        self.assertTrue(self.drain.drain(self.request).local_controls_drained)
        self.assertEqual((self.scheduler.latches, self.lab.latches), (1, 1))

    def test_foreign_owner_runtime_lease_session_or_generation_has_no_effect(self):
        for field, value in [('session_id', 'desktop-session-' + '2' * 32),
                             ('runtime_id', 'other-runtime'), ('lease_id', 'other-lease'), ('generation', 1)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.drain.drain(self.request.model_copy(update={field: value}))
        self.controller.current['owner'] = 'HUMAN'
        with self.assertRaises(ValueError):
            self.drain.drain(self.request)
        self.assertFalse(self.scheduler.latched or self.lab.latched)

    def test_conflicting_request_and_changed_current_generation_cannot_reopen(self):
        self.drain.drain(self.request)
        with self.assertRaises(ValueError):
            self.drain.drain(self.request.model_copy(update={'request_id': 'other-request'}))
        self.controller.current['generation'] = 1
        with self.assertRaises(ValueError):
            self.drain.drain(self.request)
        self.assertTrue(self.scheduler.latched and self.lab.latched)

    def test_observation_failure_remains_latched_and_can_only_be_reobserved(self):
        self.lab.failure = True
        result = self.drain.drain(self.request)
        self.assertFalse(result.admission_closed)
        self.assertFalse(result.local_controls_drained)
        self.assertIn('lab.observation_unavailable', result.blockers)
        self.assertTrue(self.scheduler.latched and self.lab.latched)
        self.lab.failure = False
        self.assertTrue(self.drain.drain(self.request).local_controls_drained)
        self.assertEqual(self.lab.latches, 1)

    def test_partial_latch_failure_still_latches_peer_and_never_claims_success(self):
        def fail():
            raise OSError('Synthetic latch failure')
        self.scheduler.latch_shared_drain = fail
        result = self.drain.drain(self.request)
        self.assertTrue(self.lab.latched)
        self.assertFalse(result.admission_closed)
        self.assertIn('scheduler.latch_unproven', result.blockers)
        self.scheduler.latched = True
        self.assertFalse(self.drain.drain(self.request).local_controls_drained)

    def test_current_binding_is_rechecked_after_observation(self):
        original = self.lab.shared_drain_status
        def change_generation():
            self.controller.current['generation'] = 1
            return original()
        self.lab.shared_drain_status = change_generation
        with self.assertRaises(ValueError):
            self.drain.drain(self.request)
        self.assertTrue(self.scheduler.latched and self.lab.latched)

    def test_canonical_schema_fixture_and_strict_untrusted_fields(self):
        fixture = json.loads((REPO_ROOT / 'examples/shared_drain.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model in [('request', SharedDrainRequest), ('observation', SharedDrainObservation)]:
            schema = json.loads((REPO_ROOT / ('schemas/shared_drain_' + name + '.schema.json')).read_text())
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'}, model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(fixture[name])
            model.model_validate(fixture[name], strict=True)
        for field, value in [('generation', True), ('owner', 'HUMAN'), ('unknown', True)]:
            invalid = {**fixture['request'], field: value}
            with self.assertRaises(ValueError):
                SharedDrainRequest.model_validate(invalid, strict=True)
        invalid = copy.deepcopy(fixture['observation'])
        invalid['gpu_release_verified'] = True
        with self.assertRaises(ValueError):
            SharedDrainObservation.model_validate(invalid, strict=True)
        invalid['gpu_release_verified'] = False
        invalid['local_controls_drained'] = True
        with self.assertRaises(ValueError):
            SharedDrainObservation.model_validate(invalid, strict=True)

    def test_receipt_survives_original_cpu_writer_process_exit(self):
        program = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from aos.desktop_control import DesktopController
from aos.shared_drain import SharedAdmissionDrain, SharedDrainRequest
from aos.storage import TrajectoryStore
from test_shared_drain import SyntheticDrainComponent
store = TrajectoryStore(Path(sys.argv[1]))
try:
    controller = DesktopController(store, SimpleNamespace(runtime_id='runtime-synthetic', pins={'image_id':'synthetic'}))
    scheduler = SyntheticDrainComponent(controller)
    lab = SyntheticDrainComponent(controller)
    selected = {key: controller.state()[key] for key in ('session_id','runtime_id','owner','lease_id','generation')}
    request = SharedDrainRequest(request_id='synthetic-writer-exit', **selected)
    saved = SharedAdmissionDrain(controller, scheduler, lab).persist_observation(request)
    print(saved.model_dump_json())
finally:
    store.close()
'''
        with tempfile.TemporaryDirectory(prefix='synthetic-drain-writer-') as directory:
            database = Path(directory) / 'synthetic.sqlite3'
            environment = {**os.environ, 'PYTHONPATH': os.pathsep.join(
                [str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')])}
            result = subprocess.run([sys.executable, '-c', program, str(database)],
                env=environment, cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = SavedSharedDrainObservation.model_validate_json(result.stdout, strict=True)
            self.assertEqual(observe_process(saved.receipt.process), 'not_observed')
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                readback = read_shared_drain_receipt(connection,
                    session_id=saved.receipt.observation.request.session_id,
                    event_id=saved.receipt_id, expected_sha256=saved.receipt_sha256)
                self.assertEqual(readback, saved)
                self.assertFalse(readback.receipt.observation.gpu_release_verified)
            finally:
                connection.close()
