import hashlib
import os
import stat
from typing import Literal

from pydantic import Field

from .contracts import HELLO_CONTENT, TypedModel


class FileWitness(TypedModel):
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=0)
    size: Literal[28] = 28
    links: Literal[1] = 1
    modified_ns: int = Field(ge=0)
    changed_ns: int = Field(ge=0)
    content_sha256: Literal['09308e6af1379087dd0ae56e8bbf85b96f3861fb4c91ef2393edb70229164032']


class WriteReceipt(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    method: Literal['exclusive_create_fsync_fd_read_v1'] = 'exclusive_create_fsync_fd_read_v1'
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    action_ref: str = Field(pattern='^[a-f0-9]{64}$')
    envelope_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    file: FileWitness


def witness_file(directory: int, descriptor: int) -> FileWitness:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != len(HELLO_CONTENT.encode()):
        raise ValueError('write_witness_file_invalid')
    content = os.pread(descriptor, 4097, 0)
    after = os.fstat(descriptor)
    linked = os.stat('hello.txt', dir_fd=directory, follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')
    if content != HELLO_CONTENT.encode() or any(
            getattr(before, field) != getattr(metadata, field) for metadata in (after, linked) for field in fields):
        raise ValueError('write_witness_file_changed')
    return FileWitness(device=after.st_dev, inode=after.st_ino, owner_uid=after.st_uid,
                       modified_ns=after.st_mtime_ns, changed_ns=after.st_ctime_ns,
                       content_sha256=hashlib.sha256(content).hexdigest())
