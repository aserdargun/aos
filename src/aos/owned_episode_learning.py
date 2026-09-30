"""Private opt-in content candidates, explicit role review and development export."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .contracts import canonical, digest
from .dataset import validator
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child


ROLES = ('system1', 'system2')
FIXTURE_GROUP = digest({'fixture_family': 'aos-owned-synthetic-form', 'version': '1'})
EPISODE = re.compile(r'episode-[a-f0-9]{32}\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')
MAX_BYTES = 2 * 1024 * 1024


def _validate(name, value):
    try:
        validator(name).validate(value)
    except Exception:
        raise ValueError('owned_episode_contract_invalid') from None


def consent_record(planning_id, request, authority):
    record = {'schema_version': '1.0', 'episode_id': 'episode-' + uuid4().hex,
              'planning_id': planning_id, 'goal_sha256': digest({'goal': request['goal']}),
              'authority': authority, 'roles': list(ROLES), 'synthetic': True,
              'scope': 'owned_synthetic_episode_content', 'collection_authorized': True,
              'source_group_sha256': FIXTURE_GROUP, 'training_ready': False,
              'training_authorized': False, 'network_export_authorized': False}
    _validate('owned_episode_consent', record)
    return record


class OwnedEpisodeStore:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def directory(self, episode_id, *, create=False):
        if type(episode_id) is not str or EPISODE.fullmatch(episode_id) is None:
            raise ValueError('owned_episode_id_invalid')
        parent = _directory(self.root, create=create)
        descriptor = None
        try:
            if create:
                try:
                    os.mkdir(episode_id, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            descriptor = _directory(self.root / episode_id, create=False)
            linked = os.stat(episode_id, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('owned_episode_directory_changed')
            yield descriptor
            linked = os.stat(episode_id, dir_fd=parent, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('owned_episode_directory_changed')
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def _read(self, descriptor, filename):
        raw = _read_private_child(descriptor, filename, MAX_BYTES)
        value = json.loads(raw)
        if canonical(value).encode() != raw:
            raise ValueError('owned_episode_noncanonical_artifact')
        return value

    def _put_bytes(self, descriptor, filename, raw):
        if len(raw) > MAX_BYTES:
            raise ValueError('owned_episode_size_limit')
        try:
            previous = _read_private_child(descriptor, filename, MAX_BYTES)
        except FileNotFoundError:
            _write_private_child(descriptor, filename, raw)
            previous = _read_private_child(descriptor, filename, MAX_BYTES)
        if previous != raw:
            raise ValueError('owned_episode_artifact_changed')

    def _put(self, descriptor, filename, value):
        self._put_bytes(descriptor, filename, canonical(value).encode())

    def create(self, record):
        _validate('owned_episode_consent', record)
        if record['source_group_sha256'] != FIXTURE_GROUP:
            raise ValueError('owned_episode_group_invalid')
        with self.directory(record['episode_id'], create=True) as descriptor:
            self._put(descriptor, 'consent.json', record)
        return digest(record)

    def consent(self, episode_id):
        with self.directory(episode_id) as descriptor:
            record = self._read(descriptor, 'consent.json')
        _validate('owned_episode_consent', record)
        if record['episode_id'] != episode_id or record['source_group_sha256'] != FIXTURE_GROUP:
            raise ValueError('owned_episode_consent_changed')
        return record

    def assert_plan(self, episode_id, bundle):
        from .owned_skill_planning import validate_planning_bundle

        validate_planning_bundle(bundle)
        consent = self.consent(episode_id)
        if (bundle['schema_version'] != '1.1' or bundle['episode_id'] != episode_id
                or bundle['collection_consent_sha256'] != digest(consent)
                or bundle['planning_id'] != consent['planning_id']
                or bundle['authority'] != consent['authority']
                or digest({'goal': bundle['request']['goal']}) != consent['goal_sha256']):
            raise ValueError('owned_episode_plan_consent_changed')
        return consent

    def bind_execution(self, episode_id, execution_sha256, planning_bundle_sha256):
        if any(type(value) is not str or HASH.fullmatch(value) is None
               for value in (execution_sha256, planning_bundle_sha256)):
            raise ValueError('owned_episode_execution_pin_invalid')
        record = {'candidate_execution_sha256': execution_sha256,
                  'planning_bundle_sha256': planning_bundle_sha256}
        self.consent(episode_id)
        with self.directory(episode_id) as descriptor:
            self._put(descriptor, 'execution.json', record)

    def execution(self, episode_id):
        with self.directory(episode_id) as descriptor:
            record = self._read(descriptor, 'execution.json')
        if (set(record) != {'candidate_execution_sha256', 'planning_bundle_sha256'}
                or any(type(value) is not str or HASH.fullmatch(value) is None for value in record.values())):
            raise ValueError('owned_episode_execution_invalid')
        return record

    def refresh(self, episode_id, candidates):
        consent = self.consent(episode_id)
        if len(candidates) > 64:
            raise ValueError('owned_episode_candidate_limit')
        for candidate in candidates:
            self._check_candidate(candidate, consent)
        with self.directory(episode_id) as descriptor:
            names = [name for name in os.listdir(descriptor) if name.startswith('candidate-')]
            expected = {'candidate-' + item['candidate_id'] + '.json' for item in candidates}
            if len(set(names) | expected) > 64:
                raise ValueError('owned_episode_candidate_limit')
            for candidate in candidates:
                self._put(descriptor, 'candidate-' + candidate['candidate_id'] + '.json', candidate)

    def _check_candidate(self, candidate, consent):
        _validate('owned_episode_candidate', candidate)
        if (candidate['candidate_id'] != digest({key: value for key, value in candidate.items()
                                                if key != 'candidate_id'})
                or candidate['episode_id'] != consent['episode_id']
                or candidate['consent_sha256'] != digest(consent)
                or candidate['source_group_sha256'] != FIXTURE_GROUP):
            raise ValueError('owned_episode_candidate_changed')

    def candidates(self, episode_id):
        consent = self.consent(episode_id)
        result = []
        with self.directory(episode_id) as descriptor:
            names = sorted(name for name in os.listdir(descriptor) if name.startswith('candidate-'))
            if len(names) > 64:
                raise ValueError('owned_episode_candidate_limit')
            for name in names:
                candidate = self._read(descriptor, name)
                self._check_candidate(candidate, consent)
                if name != 'candidate-' + candidate['candidate_id'] + '.json':
                    raise ValueError('owned_episode_candidate_name_changed')
                result.append(candidate)
        return result

    def review_selection(self, episode_id, role, candidates, execution):
        if role not in ROLES or not candidates or any(item['role'] != role for item in candidates):
            raise ValueError('owned_episode_review_role_invalid')
        consent = self.consent(episode_id)
        for candidate in candidates:
            self._check_candidate(candidate, consent)
        return {'episode_id': episode_id, 'consent_sha256': digest(consent), 'role': role,
                'candidate_sha256': sorted(digest(item) for item in candidates),
                'execution': execution, 'source_group_sha256': FIXTURE_GROUP}

    def review(self, episode_id, role, decision, candidates, execution, confirm_sha256):
        selection = self.review_selection(episode_id, role, candidates, execution)
        if decision not in {'accept', 'reject'} or digest(selection) != confirm_sha256:
            raise ValueError('owned_episode_review_confirmation_required')
        record = {'schema_version': '1.0', **selection, 'decision': decision,
                  'reviewer': 'authenticated_local_user',
                  'reference_policy': 'human_reviewed_exact_prediction',
                  'synthetic': True, 'training_ready': False}
        _validate('owned_episode_review', record)
        with self.directory(episode_id) as descriptor:
            self._put(descriptor, 'review-' + role + '.json', record)
        return digest(record)

    def reviews(self, episode_id):
        result = {}
        with self.directory(episode_id) as descriptor:
            for role in ROLES:
                try:
                    receipt = self._read(descriptor, 'review-' + role + '.json')
                except FileNotFoundError:
                    result[role] = None
                    continue
                _validate('owned_episode_review', receipt)
                if receipt['episode_id'] != episode_id or receipt['role'] != role:
                    raise ValueError('owned_episode_review_changed')
                checksum = digest(receipt)
                try:
                    revoked = self._read(descriptor, 'revoked-' + role + '.json')
                except FileNotFoundError:
                    revoked = None
                if revoked is not None and revoked != {'receipt_sha256': checksum, 'episode_id': episode_id, 'role': role}:
                    raise ValueError('owned_episode_revocation_changed')
                result[role] = {'receipt_sha256': checksum, 'decision': receipt['decision'],
                                'revoked': revoked is not None, 'receipt': receipt}
        return result

    def revoke(self, episode_id, role, confirm_sha256):
        if role not in ROLES:
            raise ValueError('owned_episode_review_role_invalid')
        review = self.reviews(episode_id)[role]
        if review is None or review['receipt_sha256'] != confirm_sha256:
            raise ValueError('owned_episode_review_confirmation_required')
        with self.directory(episode_id) as descriptor:
            self._put(descriptor, 'revoked-' + role + '.json', {
                'episode_id': episode_id, 'role': role, 'receipt_sha256': confirm_sha256})

    def export_material(self, episode_id, candidates, execution, review_receipts):
        consent = self.consent(episode_id)
        reviews = self.reviews(episode_id)
        if set(review_receipts) != set(ROLES):
            raise ValueError('owned_episode_two_role_review_required')
        files, counts = {}, {}
        for role in ROLES:
            selected = sorted((item for item in candidates if item['role'] == role), key=lambda item: item['candidate_id'])
            selection = self.review_selection(episode_id, role, selected, execution)
            review = reviews[role]
            if (review is None or review['decision'] != 'accept' or review['revoked']
                    or review['receipt_sha256'] != review_receipts[role]
                    or any(review['receipt'][key] != value for key, value in selection.items())):
                raise ValueError('owned_episode_current_review_required')
            rows = [{'schema_version': '1.0', 'record_kind': 'owned_episode_' + role,
                     'candidate_sha256': digest(candidate), 'source': candidate['source'],
                     'input': candidate['input'], 'target': candidate['prediction'],
                     'review_receipt_sha256': review['receipt_sha256'],
                     'target_provenance': 'human_reviewed_exact_prediction',
                     'split_group': FIXTURE_GROUP, 'split': 'development_only',
                     'synthetic': True, 'training_ready': False} for candidate in selected]
            for row in rows:
                _validate('owned_episode_export_record', row)
            files[role + '.jsonl'] = ''.join(canonical(row) + '\n' for row in rows).encode()
            counts[role] = len(rows)
        manifest = {'schema_version': '1.0', 'format': 'owned-episode-development-v1',
                    'episode_id': episode_id, 'consent_sha256': digest(consent),
                    'execution': execution, 'review_receipts': review_receipts,
                    'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
                    'counts': counts, 'source_group_sha256': FIXTURE_GROUP,
                    'synthetic': True, 'training_ready': False, 'split': 'development_only'}
        _validate('owned_episode_export', manifest)
        return manifest, files

    def load_export(self, episode_id, export_sha256, candidates, execution):
        if type(export_sha256) is not str or HASH.fullmatch(export_sha256) is None:
            raise ValueError('owned_episode_export_pin_invalid')
        with self.directory(episode_id) as descriptor:
            manifest = self._read(descriptor, export_sha256 + '-manifest.json')
            _validate('owned_episode_export', manifest)
            if digest(manifest) != export_sha256 or manifest['episode_id'] != episode_id:
                raise ValueError('owned_episode_export_changed')
            files = {name: _read_private_child(descriptor, export_sha256 + '-' + name, MAX_BYTES)
                     for name in ('system1.jsonl', 'system2.jsonl')}
            if any(hashlib.sha256(raw).hexdigest() != manifest['files'][name]
                   for name, raw in files.items()):
                raise ValueError('owned_episode_export_bytes_changed')
        expected, expected_files = self.export_material(
            episode_id, candidates, execution, manifest['review_receipts'])
        if manifest != expected or files != expected_files:
            raise ValueError('owned_episode_export_source_changed')
        return manifest, files

    def export(self, episode_id, candidates, execution, review_receipts):
        manifest, files = self.export_material(episode_id, candidates, execution, review_receipts)
        checksum = digest(manifest)
        with self.directory(episode_id) as descriptor:
            for name, raw in files.items():
                self._put_bytes(descriptor, checksum + '-' + name, raw)
            self._put(descriptor, checksum + '-manifest.json', manifest)
        return {'available': True, 'episode_id': episode_id, 'export_sha256': checksum,
                'counts': manifest['counts'], 'training_ready': False}
