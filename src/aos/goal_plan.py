import argparse
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .task_intent import NEGATION_OR_CONTROL, turkish_lower
from .task_plan import PlanPreview, preview_plan
from .task_sequence import SequencePlan, SequenceStart


class GoalPlanPreview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_compound_catalog'] = 'read_only_compound_catalog'
    parser_version: Literal['bounded-tr-compound-v1'] = 'bounded-tr-compound-v1'
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    status: Literal['recognized', 'unsupported', 'negated']
    reason: Literal['exact_catalog_composition', 'unsupported_goal', 'invalid_connectors',
                    'duplicate_tasks', 'negated_or_control_request', 'opaque_text', 'too_many_tasks']
    tasks: list[PlanPreview] = Field(max_length=3)
    sequence: SequencePlan | None
    composition_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    execution_authorized: Literal[False] = False
    live_state_verified: Literal[False] = False
    requires_action_approval: Literal[True] = True
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def consistent_composition(self):
        if self.status == 'recognized':
            if (self.reason != 'exact_catalog_composition' or not self.tasks
                    or any(task.intent.status != 'recognized' or task.plan is None for task in self.tasks)):
                raise ValueError('invalid_recognized_composition')
            kinds = [task.plan.task_kind for task in self.tasks]
            if len(kinds) != len(set(kinds)):
                raise ValueError('duplicate_composition_tasks')
            if len(kinds) == 1:
                if self.sequence is not None:
                    raise ValueError('single_task_is_not_sequence')
            elif self.sequence is None or self.sequence.kinds != kinds:
                raise ValueError('composition_sequence_mismatch')
        elif (self.tasks or self.sequence is not None or self.reason == 'exact_catalog_composition'
              or (self.status == 'negated') != (self.reason == 'negated_or_control_request')):
            raise ValueError('rejected_composition_has_plan')
        if self.composition_sha256 != digest(self.model_dump(exclude={'composition_sha256'})):
            raise ValueError('composition_hash_mismatch')
        return self


def split_fragments(goal: str) -> list[str] | None:
    fragments = []
    start = 0
    quote = None
    escaped = False
    for position, character in enumerate(goal):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == quote:
                quote = None
        elif character in ('"', "'"):
            quote = character
        elif character == ';':
            fragments.append(goal[start:position].strip())
            start = position + 1
    if quote is not None:
        return None
    fragments.append(goal[start:].strip())
    return fragments


def preview_goal_plan(goal: str) -> GoalPlanPreview:
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 3000:
        raise ValueError('invalid_compound_goal')
    payload = {
        'schema_version': '1.0', 'mode': 'read_only_compound_catalog',
        'parser_version': 'bounded-tr-compound-v1', 'input_sha256': digest({'original_goal': goal}),
        'status': 'unsupported', 'reason': 'unsupported_goal', 'tasks': [], 'sequence': None,
        'execution_authorized': False, 'live_state_verified': False,
        'requires_action_approval': True, 'automatic_replay_allowed': False,
    }

    def report(status, reason, plans=None):
        payload.update(status=status, reason=reason)
        if plans is not None:
            payload['tasks'] = [plan.model_dump() for plan in plans]
            if len(plans) > 1:
                payload['sequence'] = SequencePlan(kinds=[plan.plan.task_kind for plan in plans]).model_dump()
        return GoalPlanPreview.model_validate({**payload, 'composition_sha256': digest(payload)})

    if any(ord(character) < 32 or ord(character) == 127
           or 0x202A <= ord(character) <= 0x202E or 0x2066 <= ord(character) <= 0x2069 for character in goal):
        return report('unsupported', 'opaque_text')
    if NEGATION_OR_CONTROL.search(turkish_lower(goal)):
        return report('negated', 'negated_or_control_request')
    fragments = split_fragments(goal)
    if fragments is None or any(not fragment for fragment in fragments):
        return report('unsupported', 'invalid_connectors')
    if len(fragments) > 3:
        return report('unsupported', 'too_many_tasks')
    if len(fragments) > 1:
        for position, fragment in enumerate(fragments):
            prefix = 'önce ' if position == 0 else 'sonra '
            if not turkish_lower(fragment).startswith(prefix):
                return report('unsupported', 'invalid_connectors')
            fragments[position] = fragment[len(prefix):]
    plans = []
    for fragment in fragments:
        if not fragment.strip() or len(fragment) > 1000:
            return report('unsupported', 'unsupported_goal')
        plan = preview_plan(fragment)
        if plan.intent.status != 'recognized':
            return report('unsupported', 'unsupported_goal')
        plans.append(plan)
    if len(plans) != len({plan.plan.task_kind for plan in plans}):
        return report('unsupported', 'duplicate_tasks')
    return report('recognized', 'exact_catalog_composition', plans)


def verify_goal_plan(goal: str, report: GoalPlanPreview | dict) -> GoalPlanPreview:
    checked = GoalPlanPreview.model_validate(report.model_dump() if isinstance(report, GoalPlanPreview) else report)
    if checked != preview_goal_plan(goal):
        raise ValueError('composition_source_mismatch')
    return checked


class CompoundSequenceStart(TypedModel):
    goal: str = Field(min_length=1, max_length=3000)
    input_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    composition_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)

    def sequence_start(self) -> SequenceStart:
        current = preview_goal_plan(self.goal)
        if (current.status != 'recognized' or current.sequence is None
                or current.input_sha256 != self.input_sha256
                or current.composition_sha256 != self.composition_sha256):
            raise ValueError('compound_source_or_catalog_mismatch')
        return SequenceStart(plan=current.sequence, lease_id=self.lease_id, generation=self.generation)


def main():
    parser = argparse.ArgumentParser(description='Bir ila üç sabit görevin salt okunur Türkçe sıra önizlemesi')
    parser.add_argument('--goal', required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(preview_goal_plan(arguments.goal).model_dump()))
    except ValueError:
        parser.exit(1, 'Geçerli, en fazla 3000 karakterlik görev metni gerekli.\n')


if __name__ == '__main__':
    main()
