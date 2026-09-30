"""Immutable, source-bound accept and revoke receipts for owned candidates."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .contracts import canonical, digest
from .owned_form_candidate_execution import _private_child, _read_private_child, _write_private_child
from .workspace_identity import open_existing_workspace


_REVIEW_HASH = re.compile(r'[a-f0-9]{64}\Z')
_REVIEWER = 'local_authenticated_user'


def review_summary(candidate):
    skill = candidate['skill']
    recipe = candidate['recipe']
    binding = candidate['field_bindings']
    if len(binding) != 1:
        raise ValueError('owned_candidate_review_parameter_binding_invalid')
    return {
        'skill_key': skill['skill_key'],
        'expected_outcome_key': skill['expected_outcome_key'],
        'parameter_key': binding[0]['parameter_key'],
        'form_field_name': binding[0]['form_field_name'],
        'steps': [{'step_key': item['step_key'], 'operation': item['operation']}
                  for item in recipe['steps']],
    }


def make_owned_candidate_review(candidate, candidate_sha256, execution):
    receipt = {
        'schema_version': '1.0', 'synthetic': True,
        'decision': 'accept', 'purpose': 'development_review',
        'reviewer': _REVIEWER,
        'candidate_sha256': candidate_sha256,
        'source_run_ref': candidate['source_run_ref'],
        'source_invocation_sha256': candidate['source_context']['invocation_sha256'],
        'source_group_sha256': candidate['source_group_sha256'],
        'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
        'candidate_execution_sha256': execution['candidate_execution_sha256'],
        'execution_run_ref': execution['run_ref'],
        'invocation_sha256': execution['invocation_sha256'],
        'recipe_sha256': execution['recipe_sha256'],
        'skill_sha256': execution['skill_sha256'],
        'profile_sha256': execution['profile_sha256'],
        'case_key': execution['case_key'],
        'parameter_variant_sha256': execution['parameter_variant_sha256'],
        'summary': review_summary(candidate),
        'activation_authorized': False, 'training_ready': False,
        'independent_held_out': False,
    }
    _validate_receipt(receipt)
    return digest(receipt), receipt


def _validate_receipt(receipt):
    from .dataset import validator

    try:
        validator('owned_candidate_review_receipt').validate(receipt)
    except Exception as error:
        raise ValueError('owned_candidate_review_receipt_invalid') from error
    if canonical(json.loads(canonical(receipt))) != canonical(receipt):
        raise ValueError('owned_candidate_review_receipt_invalid')


def _validate_revocation(revocation):
    from .dataset import validator

    try:
        validator('owned_candidate_review_revocation').validate(revocation)
    except Exception as error:
        raise ValueError('owned_candidate_review_revocation_invalid') from error


def _receipt_directory(owned_form_directory: Path, review_sha256: str,
                       *, create: bool):
    if _REVIEW_HASH.fullmatch(review_sha256 or '') is None:
        raise ValueError('owned_candidate_review_hash_invalid')
    root = ( _private_child(owned_form_directory, 'candidate-reviews') if create
            else owned_form_directory / 'candidate-reviews')
    if create:
        return _private_child(root, review_sha256)
    parent_fd = open_existing_workspace(root)
    try:
        descriptor = os.open(review_sha256,
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent_fd)
        try:
            metadata = os.fstat(descriptor)
            linked = os.stat(review_sha256, dir_fd=parent_fd,
                             follow_symlinks=False)
            if (metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                    or (metadata.st_dev, metadata.st_ino)
                    != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_candidate_review_directory_invalid')
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    return root / review_sha256


def _read_receipt(owned_form_directory: Path, review_sha256: str):
    directory = _receipt_directory(owned_form_directory, review_sha256, create=False)
    descriptor = open_existing_workspace(directory)
    try:
        return directory, _read_receipt_fd(descriptor, review_sha256)
    finally:
        os.close(descriptor)


def _read_receipt_fd(directory_fd: int, review_sha256: str):
    payload = _read_private_child(directory_fd, 'receipt.json', 16384)
    receipt = json.loads(payload)
    _validate_receipt(receipt)
    if canonical(receipt).encode() != payload or digest(receipt) != review_sha256:
        raise ValueError('owned_candidate_review_receipt_changed')
    return receipt


def _revocations_fd(directory_fd: int, review_sha256: str):
    try:
        try:
            root_fd = os.open('revocations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
        except FileNotFoundError:
            return []
        try:
            root_info = os.fstat(root_fd)
            root_linked = os.stat('revocations', dir_fd=directory_fd,
                                  follow_symlinks=False)
            if (root_info.st_uid != os.getuid()
                    or stat.S_IMODE(root_info.st_mode) != 0o700):
                raise ValueError('owned_candidate_review_revocation_directory_invalid')
            if (stat.S_ISLNK(root_linked.st_mode)
                    or (root_info.st_dev, root_info.st_ino)
                    != (root_linked.st_dev, root_linked.st_ino)):
                raise ValueError('owned_candidate_review_revocation_directory_invalid')
            names = os.listdir(root_fd)
            if any(_REVIEW_HASH.fullmatch(name) is None for name in names):
                raise ValueError('owned_candidate_review_revocation_inventory_invalid')
            records = []
            for name in names:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=root_fd)
                try:
                    child_info = os.fstat(child_fd)
                    child_linked = os.stat(name, dir_fd=root_fd,
                                           follow_symlinks=False)
                    if (child_info.st_uid != os.getuid()
                            or stat.S_IMODE(child_info.st_mode) != 0o700
                            or stat.S_ISLNK(child_linked.st_mode)
                            or (child_info.st_dev, child_info.st_ino)
                            != (child_linked.st_dev, child_linked.st_ino)):
                        raise ValueError('owned_candidate_review_revocation_directory_invalid')
                    content = _read_private_child(child_fd, 'revocation.json', 4096)
                    record = json.loads(content)
                    _validate_revocation(record)
                    if (canonical(record).encode() != content
                            or record['review_sha256'] != review_sha256
                            or digest(record) != name):
                        raise ValueError('owned_candidate_review_revocation_changed')
                    records.append((name, record))
                finally:
                    os.close(child_fd)
            if sorted(os.listdir(root_fd)) != sorted(names):
                raise ValueError('owned_candidate_review_revocation_inventory_changed')
            root_linked_after = os.stat('revocations', dir_fd=directory_fd,
                                        follow_symlinks=False)
            root_after = os.fstat(root_fd)
            if ((root_after.st_dev, root_after.st_ino)
                    != (root_linked_after.st_dev, root_linked_after.st_ino)):
                raise ValueError('owned_candidate_review_revocation_directory_changed')
            return records
        finally:
            os.close(root_fd)
    except FileNotFoundError as error:
        raise ValueError('owned_candidate_review_revocation_directory_changed') from error


def inspect_owned_candidate_review(owned_form_directory: Path,
                                  review_sha256: str):
    directory, receipt = _read_receipt(owned_form_directory, review_sha256)
    descriptor = open_existing_workspace(directory)
    parent_fd = open_existing_workspace(directory.parent)
    try:
        linked = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
        current = os.fstat(descriptor)
        if (stat.S_ISLNK(linked.st_mode)
                or (current.st_dev, current.st_ino) != (linked.st_dev, linked.st_ino)):
            raise ValueError('owned_candidate_review_directory_changed')
        current_receipt = _read_receipt_fd(descriptor, review_sha256)
        if current_receipt != receipt:
            raise ValueError('owned_candidate_review_receipt_changed')
        revocations = _revocations_fd(descriptor, review_sha256)
        linked_after = os.stat(directory.name, dir_fd=parent_fd,
                               follow_symlinks=False)
        current_after = os.fstat(descriptor)
        if (stat.S_ISLNK(linked_after.st_mode)
                or (current_after.st_dev, current_after.st_ino)
                != (linked_after.st_dev, linked_after.st_ino)
                or current_after.st_uid != os.getuid()
                or stat.S_IMODE(current_after.st_mode) != 0o700):
            raise ValueError('owned_candidate_review_directory_changed')
    finally:
        os.close(descriptor)
        os.close(parent_fd)
    return {
        'schema_version': '1.0', 'available': True,
        'status': 'revoked' if revocations else 'accepted',
        'review_sha256': review_sha256, 'receipt': receipt,
        'summary': receipt['summary'],
        'revocation_sha256': (sorted(record[0] for record in revocations)[0]
                              if revocations else None),
    }


def inspect_owned_candidate_review_evidence(owned_form_directory: Path,
                                            review_sha256: str, *,
                                            candidate_session, database: Path):
    """Revalidate an accepted receipt and its source/execution without a live fixture."""
    inspected = inspect_owned_candidate_review(owned_form_directory, review_sha256)
    if inspected['status'] == 'revoked':
        return inspected
    receipt = inspected['receipt']
    from .owned_form_candidate_execution import (
        audit_persisted_candidate_execution, load_candidate_execution_bundle)

    candidate, _checked = candidate_session.reinspect(
        receipt['source_run_ref'], receipt['source_invocation_sha256'],
        receipt['candidate_sha256'])
    bundle_root = owned_form_directory / 'candidate-execution-bundles'
    bundle, _manifest_sha256 = load_candidate_execution_bundle(
        bundle_root, receipt['candidate_execution_sha256'])
    report = audit_persisted_candidate_execution(
        bundle_root, receipt['candidate_execution_sha256'],
        candidate_session=candidate_session, database=database)
    completion = bundle.get('completion')
    manifest = bundle['manifest']
    if completion is None or manifest.get('review_sha256') is not None:
        raise ValueError('owned_candidate_review_evidence_unavailable')
    execution = {
        'candidate_execution_sha256': receipt['candidate_execution_sha256'],
        'run_ref': completion['run_ref'],
        'invocation_sha256': manifest['invocation_sha256'],
        'recipe_sha256': manifest['recipe_sha256'],
        'skill_sha256': bundle['invocation']['skill_sha256'],
        'profile_sha256': candidate['profile_sha256'],
        'case_key': bundle['invocation']['case_key'],
        'parameter_variant_sha256': manifest['parameter_variant_sha256'],
    }
    current_sha256, current_receipt = make_owned_candidate_review(
        candidate, receipt['candidate_sha256'], execution)
    if (current_sha256 != review_sha256 or current_receipt != receipt
            or report.get('status') != 'source_bound_development_execution'
            or report.get('source_run_ref') != receipt['source_run_ref']
            or report.get('execution_run_ref') != completion['run_ref']):
        raise ValueError('owned_candidate_review_evidence_changed')
    return inspected


def persist_owned_candidate_review(owned_form_directory: Path,
                                   receipt: dict, review_sha256: str):
    _validate_receipt(receipt)
    if digest(receipt) != review_sha256:
        raise ValueError('owned_candidate_review_hash_mismatch')
    root = _private_child(owned_form_directory, 'candidate-reviews')
    root_fd = open_existing_workspace(root)
    try:
        slot = digest({key: receipt[key] for key in (
            'candidate_sha256', 'source_run_ref', 'candidate_execution_sha256')})
        for name in os.listdir(root_fd):
            if _REVIEW_HASH.fullmatch(name) is None:
                raise ValueError('owned_candidate_review_inventory_invalid')
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=root_fd)
            try:
                raw = _read_private_child(child_fd, 'receipt.json', 16384)
                existing = json.loads(raw)
                _validate_receipt(existing)
                if digest(existing) != name:
                    raise ValueError('owned_candidate_review_receipt_changed')
                existing_slot = digest({key: existing[key] for key in (
                    'candidate_sha256', 'source_run_ref', 'candidate_execution_sha256')})
                if existing_slot == slot:
                    if name != review_sha256:
                        raise ValueError('owned_candidate_review_slot_already_used')
                    if _revocations_fd(child_fd, name):
                        raise ValueError('owned_candidate_review_cannot_reopen')
                    return inspect_owned_candidate_review(owned_form_directory,
                                                          review_sha256)
            finally:
                os.close(child_fd)
        receipt_directory = _private_child(root, review_sha256)
        receipt_fd = open_existing_workspace(receipt_directory)
        try:
            content = canonical(receipt).encode('utf-8')
            try:
                _write_private_child(receipt_fd, 'receipt.json', content)
            except FileExistsError:
                if _read_private_child(receipt_fd, 'receipt.json', 16384) != content:
                    raise ValueError('owned_candidate_review_receipt_conflict')
        finally:
            os.close(receipt_fd)
    finally:
        os.close(root_fd)
    return inspect_owned_candidate_review(owned_form_directory, review_sha256)


def revoke_owned_candidate_review(owned_form_directory: Path, review_sha256: str,
                                  confirm_sha256: str):
    directory, receipt = _read_receipt(owned_form_directory, review_sha256)
    if confirm_sha256 != review_sha256:
        raise ValueError('owned_candidate_review_confirmation_invalid')
    revocation = {
        'schema_version': '1.0', 'synthetic': True,
        'review_sha256': review_sha256, 'reviewer': _REVIEWER,
        'decision': 'revoke',
    }
    _validate_revocation(revocation)
    revocation_sha256 = digest(revocation)
    revocation_root = _private_child(directory, 'revocations')
    revocation_directory = _private_child(revocation_root, revocation_sha256)
    descriptor = open_existing_workspace(revocation_directory)
    try:
        content = canonical(revocation).encode('utf-8')
        try:
            _write_private_child(descriptor, 'revocation.json', content)
        except FileExistsError:
            if _read_private_child(descriptor, 'revocation.json', 4096) != content:
                raise ValueError('owned_candidate_review_revocation_conflict')
    finally:
        os.close(descriptor)
    return inspect_owned_candidate_review(owned_form_directory, review_sha256)
