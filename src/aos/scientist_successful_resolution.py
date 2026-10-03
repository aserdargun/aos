from typing import Literal

from pydantic import Field

from .contracts import TypedModel
from .scientist_intents import ScientistIntentBinding


class ScientistRetainedResolutionResult(TypedModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    admission_record_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    reconcile_control_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    response_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    terminal_receipt_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    current_binding: ScientistIntentBinding
    state: Literal['resolved'] = 'resolved'
    original_intent_unchanged: Literal[True] = True
