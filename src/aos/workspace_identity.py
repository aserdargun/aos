import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, digest


class WorkspaceIdentity(TypedModel):
    version: Literal['descriptor-workspace-v1'] = 'descriptor-workspace-v1'
    path_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=0)


def workspace_identity(root: Path, descriptor: int) -> WorkspaceIdentity:
    metadata = os.fstat(descriptor)
    return WorkspaceIdentity(path_sha256=digest({'workspace': str(root.absolute())}),
                             device=metadata.st_dev, inode=metadata.st_ino, owner_uid=metadata.st_uid)


def open_existing_workspace(root: Path) -> int:
    parts = root.absolute().parts
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            if part == '..':
                raise ValueError('workspace_parent_traversal')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
