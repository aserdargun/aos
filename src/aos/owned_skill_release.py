"""Private immutable releases and CAS selection history for owned skills."""

from contextlib import contextmanager
import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path
from uuid import uuid4

from .contracts import canonical, digest
from .owned_form_candidate_execution import (
    _private_child, _read_private_child, _write_private_child)
from .workspace_identity import open_existing_workspace


_HASH = re.compile(r'[a-f0-9]{64}\Z')
_RELEASE_LIMIT = 64
_SELECTION_LIMIT = 256


def release_family(candidate):
    skill = candidate['skill']
    family = {
        'profile_sha256': candidate['profile_sha256'],
        'application_key': skill['application_key'],
        'tenant_key': skill['tenant_key'],
        'account_role': skill['account_role'],
        'task_key': skill['task_key'],
        'task_sha256': candidate['task_sha256'],
        'page_draft_sha256': skill['page_draft_sha256'],
        'skill_key': skill['skill_key'],
        'model_role': 'system1',
        'field_binding_sha256': candidate['field_binding_sha256'],
    }
    return family, digest(family)


def make_owned_skill_release(candidate, candidate_sha256, review_sha256,
                             review_receipt, parent_release=None):
    family, family_sha256 = release_family(candidate)
    recipe_sha256 = digest(candidate['recipe'])
    if (review_receipt.get('candidate_sha256') != candidate_sha256
            or review_receipt.get('source_run_ref') != candidate['source_run_ref']
            or review_receipt.get('source_group_sha256') != candidate['source_group_sha256']
            or review_receipt.get('source_fingerprint_sha256')
            != candidate['source_fingerprint_sha256']):
        raise ValueError('owned_skill_release_review_binding_changed')
    if parent_release is None:
        revision = 1
        parent_release_sha256 = None
    else:
        if (parent_release['family_sha256'] != family_sha256
                or parent_release['candidate_sha256'] == candidate_sha256
                or parent_release['recipe_sha256'] == recipe_sha256):
            raise ValueError('owned_skill_release_parent_incompatible')
        revision = parent_release['revision'] + 1
        parent_release_sha256 = digest(parent_release)
    release = {
        'schema_version': '1.0', 'synthetic': True,
        'purpose': 'development_release', 'family': family,
        'family_sha256': family_sha256, 'revision': revision,
        'parent_release_sha256': parent_release_sha256,
        'candidate_sha256': candidate_sha256,
        'skill_sha256': candidate['recipe']['skill_sha256'],
        'recipe_sha256': recipe_sha256,
        'review_sha256': review_sha256,
        'source_run_ref': candidate['source_run_ref'],
        'source_group_sha256': candidate['source_group_sha256'],
        'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
        'evidence_execution_sha256': review_receipt['candidate_execution_sha256'],
        'activation_authorized': False, 'training_ready': False,
        'independent_held_out': False,
    }
    _validate_release(release)
    return digest(release), release


def _validate_release(release):
    from .dataset import validator

    try:
        validator('owned_skill_release').validate(release)
    except Exception as error:
        raise ValueError('owned_skill_release_invalid') from error


def _write_new_private_child(directory_fd: int, filename: str, content: bytes):
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    identity = os.fstat(descriptor)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError('owned_skill_release_file_write_incomplete')
            offset += written
        os.fsync(descriptor)
        os.fsync(directory_fd)
        linked = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if ((identity.st_dev, identity.st_ino) != (linked.st_dev, linked.st_ino)
                or identity.st_uid != os.getuid()
                or stat.S_IMODE(identity.st_mode) != 0o600
                or identity.st_nlink != 1):
            raise ValueError('owned_skill_release_file_changed')
        return identity.st_dev, identity.st_ino
    except Exception:
        try:
            linked = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) == (
                    linked.st_dev, linked.st_ino):
                os.unlink(filename, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _validate_selection(selection):
    from .dataset import validator

    try:
        validator('owned_skill_selection').validate(selection)
    except Exception as error:
        raise ValueError('owned_skill_selection_invalid') from error


def _family_directory(owned_form_directory: Path, family_sha256: str, *, create):
    if _HASH.fullmatch(family_sha256 or '') is None:
        raise ValueError('owned_skill_release_family_invalid')
    root = (_private_child(owned_form_directory, 'owned-skill-release-catalog')
            if create else owned_form_directory / 'owned-skill-release-catalog')
    family_directory = (_private_child(root, family_sha256) if create
                        else root / family_sha256)
    return family_directory


def _private_open_directory(path: Path):
    descriptor = open_existing_workspace(path)
    info = os.fstat(descriptor)
    parent_fd = open_existing_workspace(path.parent)
    try:
        linked = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)
    if (stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
        os.close(descriptor)
        raise ValueError('owned_skill_release_directory_invalid')
    return descriptor


@contextmanager
def _family_lock(owned_form_directory: Path, family_sha256: str, *, create):
    family = _family_directory(owned_form_directory, family_sha256, create=create)
    if create:
        _private_child(family, 'releases')
        _private_child(family, 'selections')
    family_fd = _private_open_directory(family)
    parent_fd = open_existing_workspace(family.parent)
    try:
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if create:
                flags |= os.O_CREAT
            descriptor = os.open('catalog.lock', flags, 0o600, dir_fd=family_fd)
        except FileNotFoundError:
            raise ValueError('owned_skill_release_catalog_missing')
        try:
            info = os.fstat(descriptor)
            linked = os.stat('catalog.lock', dir_fd=family_fd,
                             follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_skill_release_lock_invalid')
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('owned_skill_release_catalog_busy')
                    time.sleep(0.01)
            current = os.fstat(family_fd)
            linked_family = os.stat(family.name, dir_fd=parent_fd,
                                    follow_symlinks=False)
            linked_lock = os.stat('catalog.lock', dir_fd=family_fd,
                                  follow_symlinks=False)
            if ((current.st_dev, current.st_ino)
                    != (linked_family.st_dev, linked_family.st_ino)
                    or (info.st_dev, info.st_ino)
                    != (linked_lock.st_dev, linked_lock.st_ino)):
                raise ValueError('owned_skill_release_catalog_changed')
            inventory_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=family_fd)
            try:
                family_entries = set(os.listdir(inventory_fd))
            finally:
                os.close(inventory_fd)
            if (not {'releases', 'selections', 'catalog.lock'} <= family_entries
                    or family_entries - {'releases', 'selections', 'catalog.lock', 'head.json'}):
                raise ValueError('owned_skill_release_family_inventory_invalid')
            try:
                yield family, family_fd
            finally:
                current = os.fstat(family_fd)
                linked_family = os.stat(family.name, dir_fd=parent_fd,
                                        follow_symlinks=False)
                linked_lock = os.stat('catalog.lock', dir_fd=family_fd,
                                      follow_symlinks=False)
                if ((current.st_dev, current.st_ino)
                        != (linked_family.st_dev, linked_family.st_ino)
                        or (info.st_dev, info.st_ino)
                        != (linked_lock.st_dev, linked_lock.st_ino)):
                    raise ValueError('owned_skill_release_catalog_changed')
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
        os.close(family_fd)


def _read_release_at(family: Path, family_fd: int, release_sha256: str):
    if _HASH.fullmatch(release_sha256 or '') is None:
        raise ValueError('owned_skill_release_hash_invalid')
    releases_fd = _open_private_directory_at(family_fd, 'releases')
    try:
        release_fd = _open_private_directory_at(releases_fd, release_sha256)
        try:
            content = _read_private_child(release_fd, 'release.json', 16384)
            release = json.loads(content)
            _validate_release(release)
            if canonical(release).encode() != content or digest(release) != release_sha256:
                raise ValueError('owned_skill_release_changed')
            return release
        finally:
            os.close(release_fd)
    finally:
        os.close(releases_fd)


def load_owned_skill_release(owned_form_directory: Path, release_sha256: str):
    family_sha256 = None
    root = owned_form_directory / 'owned-skill-release-catalog'
    root_fd = _private_open_directory(root)
    try:
        names = os.listdir(root_fd)
        if any(_HASH.fullmatch(name) is None for name in names):
            raise ValueError('owned_skill_release_family_inventory_invalid')
        matches = []
        for name in names:
            family = _family_directory(owned_form_directory, name, create=False)
            try:
                with _family_lock(owned_form_directory, name, create=False) as (directory, fd):
                    _validate_release_history(directory, fd, name)
                    try:
                        release = _read_release_at(directory, fd, release_sha256)
                    except FileNotFoundError:
                        continue
                    matches.append(release)
            except FileNotFoundError:
                raise ValueError('owned_skill_release_family_changed')
        if len(matches) != 1:
            raise ValueError('owned_skill_release_unavailable')
        family_sha256 = matches[0]['family_sha256']
        if digest(matches[0]['family']) != family_sha256:
            raise ValueError('owned_skill_release_family_changed')
        return matches[0]
    finally:
        os.close(root_fd)


def persist_owned_skill_release(owned_form_directory: Path, release: dict,
                                release_sha256: str):
    _validate_release(release)
    if digest(release) != release_sha256:
        raise ValueError('owned_skill_release_hash_mismatch')
    family_sha256 = release['family_sha256']
    with _family_lock(owned_form_directory, family_sha256, create=True) as (family, family_fd):
        releases_fd = _open_private_directory_at(family_fd, 'releases')
        try:
            names = os.listdir(releases_fd)
            if len(names) >= _RELEASE_LIMIT:
                raise ValueError('owned_skill_release_limit_reached')
            latest = None
            existing_by_hash = {}
            for name in names:
                if _HASH.fullmatch(name) is None:
                    raise ValueError('owned_skill_release_inventory_invalid')
                existing = _read_release_at(family, family_fd, name)
                existing_by_hash[name] = existing
                if latest is None or existing['revision'] > latest['revision']:
                    latest = existing
            if release_sha256 in existing_by_hash:
                if existing_by_hash[release_sha256] != release:
                    raise ValueError('owned_skill_release_conflict')
                if latest is None or latest != release:
                    raise ValueError('owned_skill_release_parent_changed')
                return {'release_sha256': release_sha256, 'release': release,
                        'persisted': True}
            if any(existing['revision'] == release['revision']
                   for existing in existing_by_hash.values()):
                raise ValueError('owned_skill_release_revision_conflict')
            if latest is None:
                if release['revision'] != 1 or release['parent_release_sha256'] is not None:
                    raise ValueError('owned_skill_release_parent_changed')
            elif (release['revision'] != latest['revision'] + 1
                  or release['parent_release_sha256'] != digest(latest)
                  or release['candidate_sha256'] == latest['candidate_sha256']
                  or release['recipe_sha256'] == latest['recipe_sha256']):
                raise ValueError('owned_skill_release_parent_changed')
            created_release = False
            release_identity = None
            try:
                os.mkdir(release_sha256, 0o700, dir_fd=releases_fd)
                created_release = True
            except FileExistsError:
                raise ValueError('owned_skill_release_conflict')
            try:
                child = _open_private_directory_at(releases_fd, release_sha256)
                try:
                    child_info = os.fstat(child)
                    release_identity = (child_info.st_dev, child_info.st_ino)
                    _write_new_private_child(
                        child, 'release.json', canonical(release).encode())
                finally:
                    os.close(child)
                os.fsync(releases_fd)
            except Exception:
                if created_release:
                    try:
                        _remove_unpublished_release(
                            releases_fd, release_sha256, release_identity,
                            canonical(release).encode())
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                raise
            return {'release_sha256': release_sha256, 'release': release,
                    'persisted': True}
        finally:
            os.close(releases_fd)


def _read_head(family_fd: int, family_sha256: str):
    try:
        raw = _read_private_child(family_fd, 'head.json', 4096)
    except FileNotFoundError:
        return None
    head = json.loads(raw)
    from .dataset import validator
    try:
        validator('owned_skill_selection_head').validate(head)
    except Exception as error:
        raise ValueError('owned_skill_selection_head_invalid') from error
    if canonical(head).encode() != raw or head['family_sha256'] != family_sha256:
        raise ValueError('owned_skill_selection_head_changed')
    return head


def _read_selection_at(family_fd: int, selection_sha256: str):
    if _HASH.fullmatch(selection_sha256 or '') is None:
        raise ValueError('owned_skill_selection_hash_invalid')
    root_fd = _open_private_directory_at(family_fd, 'selections')
    try:
        event_fd = _open_private_directory_at(root_fd, selection_sha256)
        try:
            raw = _read_private_child(event_fd, 'selection.json', 8192)
            event = json.loads(raw)
            _validate_selection(event)
            if canonical(event).encode() != raw or digest(event) != selection_sha256:
                raise ValueError('owned_skill_selection_changed')
            return event
        finally:
            os.close(event_fd)
    finally:
        os.close(root_fd)


def _validate_release_history(family: Path, family_fd: int, family_sha256: str):
    releases_fd = _open_private_directory_at(family_fd, 'releases')
    try:
        names = os.listdir(releases_fd)
        if len(names) > _RELEASE_LIMIT or any(_HASH.fullmatch(name) is None for name in names):
            raise ValueError('owned_skill_release_inventory_invalid')
        releases = [_read_release_at(family, family_fd, name) for name in names]
        releases.sort(key=lambda item: item['revision'])
        if not releases:
            return []
        if any(release['family_sha256'] != family_sha256 for release in releases):
            raise ValueError('owned_skill_release_family_changed')
        if [item['revision'] for item in releases] != list(range(1, len(releases) + 1)):
            raise ValueError('owned_skill_release_history_invalid')
        previous = None
        for release in releases:
            if (release['parent_release_sha256'] != (None if previous is None else digest(previous))
                    or previous is not None
                    and (release['candidate_sha256'] == previous['candidate_sha256']
                         or release['recipe_sha256'] == previous['recipe_sha256'])):
                raise ValueError('owned_skill_release_history_invalid')
            previous = release
        return releases
    finally:
        os.close(releases_fd)


def _atomic_head_write(family_fd: int, head: dict):
    from .dataset import validator

    validator('owned_skill_selection_head').validate(head)
    content = canonical(head).encode('utf-8')
    temporary = '.head-' + uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=family_fd)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'head.json', src_dir_fd=family_fd,
                   dst_dir_fd=family_fd)
        os.fsync(family_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=family_fd)
        except FileNotFoundError:
            pass


def _selection_event(release: dict, head: dict | None, operation: str,
                     previous_selection: dict | None,
                     rollback_evidence_execution_sha256: str | None):
    event = {
        'schema_version': '1.0', 'synthetic': True,
        'purpose': 'development_selected',
        'family_sha256': release['family_sha256'],
        'sequence': 1 if head is None else head['sequence'] + 1,
        'previous_selection_sha256': None if head is None else head['selection_sha256'],
        'previous_release_sha256': (None if previous_selection is None
                                    else previous_selection['release_sha256']),
        'release_sha256': digest(release), 'operation': operation,
        'rollback_evidence_execution_sha256': rollback_evidence_execution_sha256,
        'actor': 'local_authenticated_user', 'activation_authorized': False,
        'training_ready': False,
    }
    _validate_selection(event)
    return digest(event), event


def preview_owned_skill_selection(owned_form_directory: Path, release_sha256: str,
                                  expected_selection_sha256: str | None,
                                  operation: str,
                                  rollback_evidence_execution_sha256: str | None = None):
    release = load_owned_skill_release(owned_form_directory, release_sha256)
    with _family_lock(owned_form_directory, release['family_sha256'], create=False) as (_family, fd):
        head = _read_head(fd, release['family_sha256'])
        _selection_chain_at(fd, head)
        current = None if head is None else head['selection_sha256']
        if current != expected_selection_sha256:
            raise ValueError('owned_skill_selection_head_stale')
        previous = None if head is None else _read_selection_at(fd, current)
        if previous is not None and previous['release_sha256'] == release_sha256:
            raise ValueError('owned_skill_release_already_selected')
        if operation not in {'select', 'rollback'}:
            raise ValueError('owned_skill_selection_operation_invalid')
        if operation == 'select' and rollback_evidence_execution_sha256 is not None:
            raise ValueError('owned_skill_selection_rollback_evidence_invalid')
        if operation == 'rollback' and rollback_evidence_execution_sha256 is None:
            raise ValueError('owned_skill_selection_rollback_evidence_required')
        selection_sha256, selection = _selection_event(
            release, head, operation, previous,
            rollback_evidence_execution_sha256)
        return {'selection_sha256': selection_sha256, 'selection': selection,
                'release': release, 'head': head}


def commit_owned_skill_selection(owned_form_directory: Path, selection: dict,
                                 selection_sha256: str,
                                 expected_selection_sha256: str | None):
    _validate_selection(selection)
    if digest(selection) != selection_sha256:
        raise ValueError('owned_skill_selection_hash_mismatch')
    family_sha256 = selection['family_sha256']
    with _family_lock(owned_form_directory, family_sha256, create=False) as (family, fd):
        head = _read_head(fd, family_sha256)
        _selection_chain_at(fd, head)
        current = None if head is None else head['selection_sha256']
        if current != expected_selection_sha256:
            raise ValueError('owned_skill_selection_head_stale')
        if selection['sequence'] != (1 if head is None else head['sequence'] + 1):
            raise ValueError('owned_skill_selection_sequence_changed')
        if selection['previous_selection_sha256'] != current:
            raise ValueError('owned_skill_selection_parent_changed')
        previous = None if head is None else _read_selection_at(fd, current)
        if (selection['previous_release_sha256']
                != (None if previous is None else previous['release_sha256'])):
            raise ValueError('owned_skill_selection_parent_release_changed')
        current_selection = current
        release = _read_release_at(family, fd, selection['release_sha256'])
        if release['family_sha256'] != family_sha256:
            raise ValueError('owned_skill_selection_family_changed')
        created_event = False
        event_identity = None
        selections_fd = _open_private_directory_at(fd, 'selections')
        try:
            names = os.listdir(selections_fd)
            if len(names) >= _SELECTION_LIMIT:
                raise ValueError('owned_skill_selection_limit_reached')
            if any(_HASH.fullmatch(name) is None for name in names):
                raise ValueError('owned_skill_selection_inventory_invalid')
            if selection_sha256 in names:
                existing = _read_selection_at(fd, selection_sha256)
                if existing != selection:
                    raise ValueError('owned_skill_selection_conflict')
            else:
                os.mkdir(selection_sha256, 0o700, dir_fd=selections_fd)
                created_event = True
                child_fd = os.open(selection_sha256,
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=selections_fd)
                try:
                    child_info = os.fstat(child_fd)
                    event_identity = (child_info.st_dev, child_info.st_ino)
                    _write_new_private_child(
                        child_fd, 'selection.json', canonical(selection).encode())
                finally:
                    os.close(child_fd)
                os.fsync(selections_fd)
            new_head = {'schema_version': '1.0', 'family_sha256': family_sha256,
                        'sequence': selection['sequence'],
                        'selection_sha256': selection_sha256}
            _atomic_head_write(fd, new_head)
        except Exception:
            if created_event:
                try:
                    observed_head = _read_head(fd, family_sha256)
                    observed_selection = (None if observed_head is None else
                                          observed_head['selection_sha256'])
                    if observed_selection == current_selection:
                        _remove_unpublished_selection(
                            fd, selection_sha256, selection, event_identity)
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            raise
        finally:
            os.close(selections_fd)
        return new_head


def _remove_unpublished_selection(family_fd: int, selection_sha256: str,
                                  expected: dict, expected_identity):
    if expected_identity is None:
        raise ValueError('owned_skill_selection_cleanup_identity_missing')
    selections_fd = _open_private_directory_at(family_fd, 'selections')
    try:
        event_fd = _open_private_directory_at(selections_fd, selection_sha256)
        try:
            info = os.fstat(event_fd)
            if (info.st_dev, info.st_ino) != expected_identity:
                raise ValueError('owned_skill_selection_cleanup_changed')
            try:
                raw = _read_private_child(event_fd, 'selection.json', 8192)
            except FileNotFoundError:
                raw = None
            if raw is not None:
                actual = json.loads(raw)
                if (canonical(actual).encode() != raw or actual != expected
                        or digest(expected) != selection_sha256):
                    raise ValueError('owned_skill_selection_cleanup_changed')
                os.unlink('selection.json', dir_fd=event_fd)
        finally:
            os.close(event_fd)
        os.rmdir(selection_sha256, dir_fd=selections_fd)
        os.fsync(selections_fd)
    finally:
        os.close(selections_fd)


def _remove_unpublished_release(releases_fd: int, release_sha256: str,
                                expected_identity, expected_content: bytes):
    if expected_identity is None:
        raise ValueError('owned_skill_release_cleanup_identity_missing')
    release_fd = _open_private_directory_at(releases_fd, release_sha256)
    try:
        info = os.fstat(release_fd)
        if (info.st_dev, info.st_ino) != expected_identity:
            raise ValueError('owned_skill_release_cleanup_changed')
        try:
            raw = _read_private_child(release_fd, 'release.json', 16384)
        except FileNotFoundError:
            raw = None
        if raw is not None:
            if raw != expected_content:
                raise ValueError('owned_skill_release_cleanup_changed')
            os.unlink('release.json', dir_fd=release_fd)
    finally:
        os.close(release_fd)
    os.rmdir(release_sha256, dir_fd=releases_fd)
    os.fsync(releases_fd)


def current_owned_skill_selection(owned_form_directory: Path,
                                  family_sha256: str):
    with _family_lock(owned_form_directory, family_sha256, create=False) as (family, fd):
        head = _read_head(fd, family_sha256)
        chain = _selection_chain_at(fd, head)
        if head is None:
            return None, None
        return head, chain[-1][1]


def owned_skill_selection_chain(owned_form_directory: Path, family_sha256: str):
    with _family_lock(owned_form_directory, family_sha256, create=False) as (_family, fd):
        head = _read_head(fd, family_sha256)
        return _selection_chain_at(fd, head)


def _selection_chain_at(family_fd: int, head: dict | None):
    chain = []
    seen = set()
    selection_sha256 = None if head is None else head['selection_sha256']
    while selection_sha256 is not None:
        if selection_sha256 in seen or len(chain) >= _SELECTION_LIMIT:
            raise ValueError('owned_skill_selection_history_invalid')
        seen.add(selection_sha256)
        selection = _read_selection_at(family_fd, selection_sha256)
        chain.append((selection_sha256, selection))
        selection_sha256 = selection['previous_selection_sha256']
    chain.reverse()
    if (head is not None and
            ([item[1]['sequence'] for item in chain] != list(range(1, len(chain) + 1))
             or len(chain) != head['sequence'])):
        raise ValueError('owned_skill_selection_history_invalid')
    selections_fd = _open_private_directory_at(family_fd, 'selections')
    try:
        names = os.listdir(selections_fd)
    finally:
        os.close(selections_fd)
    if (len(names) > _SELECTION_LIMIT or any(_HASH.fullmatch(name) is None for name in names)
            or set(names) != seen):
        raise ValueError('owned_skill_selection_history_invalid')
    return chain


def list_owned_skill_release_families(owned_form_directory: Path):
    root = owned_form_directory / 'owned-skill-release-catalog'
    try:
        root_fd = _private_open_directory(root)
    except FileNotFoundError:
        return []
    try:
        family_names = os.listdir(root_fd)
        if len(family_names) > _RELEASE_LIMIT or any(
                _HASH.fullmatch(name) is None for name in family_names):
            raise ValueError('owned_skill_release_family_inventory_invalid')
        result = []
        for family_sha256 in sorted(family_names):
            with _family_lock(owned_form_directory, family_sha256, create=False) as (family, fd):
                release_history = _validate_release_history(family, fd, family_sha256)
                head = _read_head(fd, family_sha256)
                _selection_chain_at(fd, head)
                selected = None if head is None else _read_selection_at(
                    fd, head['selection_sha256'])
                releases = release_history
                if any(release['family_sha256'] != family_sha256 for release in releases):
                    raise ValueError('owned_skill_release_family_changed')
                result.append({'family_sha256': family_sha256,
                               'family': releases[0]['family'] if releases else None,
                               'selection_sha256': None if head is None else head['selection_sha256'],
                               'selected_release_sha256': None if selected is None else selected['release_sha256'],
                               'sequence': 0 if head is None else head['sequence'],
                               'selection': selected,
                               'releases': [{'release_sha256': digest(release),
                                             'release': release}
                                            for release in releases]})
        return result
    finally:
        os.close(root_fd)


def _open_private_directory_at(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (metadata.st_dev, metadata.st_ino) != (linked.st_dev, linked.st_ino)):
            raise ValueError('owned_skill_release_directory_changed')
        return descriptor
    except Exception:
        os.close(descriptor)
        raise
