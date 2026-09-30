from typing import Literal

from pydantic import Field

from .contracts import TypedModel
from .lifecycle import LifecycleBirth, read_journal
from .workspace_identity import workspace_identity


class SessionRuntimeBinding(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    session_id: str = Field(pattern='^desktop-session-[a-f0-9]{32}$')
    generation: int = Field(ge=0)
    birth: LifecycleBirth
    container_id: str = Field(pattern='^[a-f0-9]{64}$')


def runtime_binding(runtime, session_id: str, generation: int) -> SessionRuntimeBinding:
    journal = runtime.lifecycle
    if journal is None or journal.failed or journal.descriptor is None or runtime.descriptor is None:
        raise ValueError('session_lifecycle_unavailable')
    events, journal_hash = read_journal(journal.path)
    latest = events[-1]
    if (latest != journal.last or latest.stage != 'started' or latest.birth != journal.birth
            or latest.birth.runtime_id != runtime.runtime_id or latest.container_id != runtime.container_id
            or latest.birth.image_id != runtime.pins['image_id']
            or latest.birth.source_sha256 != runtime.pins['source_sha256']
            or latest.birth.workspace != workspace_identity(runtime.root, runtime.descriptor)):
        raise ValueError('session_lifecycle_binding_invalid')
    return SessionRuntimeBinding(session_id=session_id, generation=generation, birth=latest.birth,
                                 container_id=latest.container_id)
