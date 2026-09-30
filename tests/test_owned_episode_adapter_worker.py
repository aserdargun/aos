import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT

import sys
DECIDER_SERVICE = REPO_ROOT / 'services' / 'decider'
if str(DECIDER_SERVICE) not in sys.path:
    sys.path.insert(0, str(DECIDER_SERVICE))

import owned_episode_adapter_core as core
import owned_episode_adapter_replay as replay
import owned_episode_adapter_train as train


def example(index=0):
    return {
        'context': 'Synthetic development case ' + str(index),
        'qs': [{'text': 'What should happen?', 'options': ['Ask a human', 'Save the record'],
                'gold': index % 2}],
        'task': 'aos-owned-synthetic-form',
    }


def training_request(examples=None):
    return {'mode': 'owned_episode_adapter_train_v1',
            'examples': examples if examples is not None else [example()],
            'authorization_sha256': 'a' * 64, 'conversion_sha256': 'b' * 64,
            'max_tokens': 1536}


def replay_request(examples=None):
    return training_request(examples) | {
        'mode': 'owned_episode_adapter_replay_v1', 'artifact_sha256': 'c' * 64,
        'deployment_manifest_sha256': 'd' * 64}


class OwnedEpisodeAdapterWorkerTests(unittest.TestCase):
    def test_train_and_replay_requests_are_strict_before_model_import(self):
        for request, is_replay in ((training_request(), False), (replay_request(), True)):
            with self.subTest(replay=is_replay):
                self.assertEqual(core.validate_request(request, replay=is_replay), request)
        invalid = (
            training_request([]),
            training_request([example(index) for index in range(33)]),
            training_request([example()] + [example(1)]) | {'max_tokens': 1537},
            training_request([example()] + [example(1)]) | {'max_tokens': True},
            training_request() | {'split_counts': {'train': 1}},
            training_request() | {'authorization_sha256': 'z' * 64},
            replay_request() | {'deployment_manifest_sha256': None},
            replay_request() | {'unexpected': False},
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                core.validate_request(request, replay=request['mode'] == 'owned_episode_adapter_replay_v1')

    def test_all_development_rows_are_required_and_example_schema_is_closed(self):
        rows = [example(index) for index in range(32)]
        self.assertEqual(len(core.validate_request(training_request(rows), replay=False)['examples']), 32)
        mutations = []
        for key, value in (
            ('extra', True),
            ('task', 'foreign-task'),
            ('context', ''),
        ):
            changed = copy.deepcopy(example())
            changed[key] = value
            mutations.append(changed)
        changed = copy.deepcopy(example())
        changed['qs'][0]['gold'] = True
        mutations.append(changed)
        changed = copy.deepcopy(example())
        changed['qs'][0]['options'] = ['duplicate', 'duplicate']
        mutations.append(changed)
        for row in mutations:
            with self.subTest(row=row), self.assertRaises(ValueError):
                core.validate_examples([row])

    def test_invalid_request_fails_before_manifest_or_model_access(self):
        with self.assertRaisesRegex(ValueError, 'request_invalid'):
            train.run({'mode': 'wrong'}, REPO_ROOT / 'missing-manifest', REPO_ROOT / 'missing-output')
        with self.assertRaisesRegex(ValueError, 'request_invalid'):
            replay.run({'mode': 'wrong'}, REPO_ROOT / 'missing-manifest', REPO_ROOT / 'missing-artifact')

    def test_private_candidate_roundtrip_and_replay_artifact_binding(self):
        with tempfile.TemporaryDirectory(prefix='owned-adapter-', dir=REPO_ROOT / 'data') as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            output = directory / 'candidate.bin'
            output.touch(mode=0o600)
            output.chmod(0o600)
            payload = (
                core.ARTIFACT_MAGIC + bytes.fromhex('a' * 64) + bytes.fromhex(core.checksum([example()]))
                + bytes.fromhex('b' * 64) + (4).to_bytes(4, 'little')
                + (6144).to_bytes(4, 'little') + (2048).to_bytes(4, 'little')
                + b'\0' * (32768 * 4))
            self.assertEqual(len(payload), core.ARTIFACT_BYTES)
            core.validate_empty_candidate(output)
            core.write_private_candidate(output, payload)
            self.assertEqual(core.read_private_artifact(output), payload)
            request = replay_request()
            request.update(artifact_sha256=hashlib.sha256(payload).hexdigest(),
                           deployment_manifest_sha256='b' * 64)
            weights = core.validate_artifact_binding(payload, request, 'b' * 64)
            self.assertEqual(len(weights), 32768)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_nlink, 1)
            with self.assertRaisesRegex(ValueError, 'artifact_binding'):
                core.validate_artifact_binding(payload, request | {'authorization_sha256': 'f' * 64},
                                               'b' * 64)

    def test_private_artifact_rejects_symlinks_hardlinks_wrong_modes_and_partial_bytes(self):
        with tempfile.TemporaryDirectory(prefix='owned-adapter-', dir=REPO_ROOT / 'data') as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            artifact = directory / 'candidate.bin'
            artifact.write_bytes(b'x' * core.ARTIFACT_BYTES)
            artifact.chmod(0o600)
            linked = directory / 'linked.bin'
            os.link(artifact, linked)
            with self.assertRaises(ValueError):
                core.read_private_artifact(artifact)
            linked.unlink()
            artifact.chmod(0o644)
            with self.assertRaises(ValueError):
                core.read_private_artifact(artifact)
            artifact.chmod(0o600)
            artifact.write_bytes(b'x')
            with self.assertRaises(ValueError):
                core.read_private_artifact(artifact)
            artifact.unlink()
            target = directory / 'target.bin'
            target.write_bytes(b'x' * core.ARTIFACT_BYTES)
            target.chmod(0o600)
            artifact.symlink_to(target.name)
            with self.assertRaises(OSError):
                core.read_private_artifact(artifact)

    def test_private_data_path_rejects_outside_and_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory(prefix='owned-adapter-', dir=REPO_ROOT / 'data') as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            child = directory / 'child'
            child.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                core.open_private_data_parent(REPO_ROOT / 'schemas' / 'not-an-artifact')
            alias = directory / 'alias'
            alias.symlink_to(child, target_is_directory=True)
            with self.assertRaises(OSError):
                core.open_private_data_parent(alias / 'candidate.bin')

    def test_manifest_loader_preserves_pinned_raw_bytes_and_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory(prefix='owned-adapter-manifest-') as temporary:
            manifest = Path(temporary) / 'manifest.json'
            expected = json.loads((REPO_ROOT / 'examples/dataset_converter_pin.json').read_text())
            content = {'code_revision': expected['revision'], 'checkpoint_revision': '1' * 40,
                       'tokenizer_revision': '1' * 40,
                       'code_files': {'prompt.py': expected['files']['prompt.py']}, 'dependencies': {}}
            for encoded in (json.dumps(content, indent=2).encode(),
                            (json.dumps(content, indent=2) + '\n').encode()):
                manifest.write_bytes(encoded)
                raw = core.read_manifest(manifest)
                self.assertEqual(raw, encoded)
                self.assertEqual(core.private_manifest_pins(raw)[0], content)
            code_revision = content['code_revision']
            duplicate = core.canonical(content).replace(
                '"code_revision":"' + code_revision + '"',
                '"code_revision":"' + code_revision + '","code_revision":"' + code_revision + '"')
            manifest.write_text(duplicate)
            with self.assertRaises(ValueError):
                core.private_manifest_pins(core.read_manifest(manifest))


if __name__ == '__main__':
    unittest.main()
