import argparse
import re
from typing import Literal

from pydantic import Field

from .contracts import BROWSER_GOAL, HELLO_GOAL, HELLO_PATH, TypedModel, VISION_GOAL, canonical, digest


CATALOG = {
    "hello": (HELLO_GOAL, HELLO_PATH),
    "browser_form": (BROWSER_GOAL, "aos://synthetic/form"),
    "vision_canvas": (VISION_GOAL, "aos://synthetic/canvas"),
}
ALIASES = {
    "hello görevini hazırla": "hello", "hello görevini önizle": "hello", "merhaba dosyası görevini hazırla": "hello",
    "yerel form görevini hazırla": "browser_form", "yerel form görevini önizle": "browser_form",
    "görsel save görevini hazırla": "vision_canvas", "görsel save görevini önizle": "vision_canvas",
}
NEGATION_OR_CONTROL = re.compile(r"\b(oluşturma|yazma|başlatma|yapma|silme|istemiyorum|iptal|durdur|duraklat)\b|\bdo not\b|\bdon't\b")


class GoalPreview(TypedModel):
    schema_version: Literal["1.0"] = "1.0"
    normalizer_version: Literal["bounded-tr-v1"] = "bounded-tr-v1"
    input_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    status: Literal["recognized", "unsupported", "negated"]
    task_kind: Literal["hello", "browser_form", "vision_canvas"] | None = None
    normalized_goal: str | None = Field(default=None, max_length=1024)
    scope: str | None = Field(default=None, max_length=128)
    execution_authorized: Literal[False] = False
    requires_action_approval: Literal[True] = True
    reason: Literal["exact_catalog_match", "unsupported_goal", "negated_or_control_request", "opaque_text"]


def turkish_lower(value: str) -> str:
    return value.replace("I", "ı").replace("İ", "i").lower()


def preview_goal(goal: str) -> GoalPreview:
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
        raise ValueError("invalid_goal")
    identity = digest({"original_goal": goal})
    if any(ord(character) < 32 or 0x202A <= ord(character) <= 0x202E or 0x2066 <= ord(character) <= 0x2069 for character in goal):
        return GoalPreview(input_sha256=identity, status="unsupported", reason="opaque_text")
    command = goal.strip()
    lowered = turkish_lower(command)
    if NEGATION_OR_CONTROL.search(lowered):
        return GoalPreview(input_sha256=identity, status="negated", reason="negated_or_control_request")
    kind = ALIASES.get(lowered)
    if command.startswith(HELLO_PATH + " ") and turkish_lower(command[len(HELLO_PATH) + 1:]) == "dosyasını oluştur ve doğrula":
        kind = "hello"
    literal = HELLO_PATH + r' dosyasına "Hello from the local agent.\n" yaz ve doğrula'
    if command == literal:
        kind = "hello"
    if kind is None:
        return GoalPreview(input_sha256=identity, status="unsupported", reason="unsupported_goal")
    normalized, scope = CATALOG[kind]
    return GoalPreview(input_sha256=identity, status="recognized", task_kind=kind, normalized_goal=normalized,
                       scope=scope, reason="exact_catalog_match")


def main():
    parser = argparse.ArgumentParser(description="Sınırlı Türkçe görev önizlemesi; eylem veya yetki üretmez")
    parser.add_argument("--goal", required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(preview_goal(arguments.goal).model_dump()))
    except ValueError:
        parser.exit(1, "Geçerli, en fazla 1000 karakterlik bir görev metni gerekli.\n")


if __name__ == "__main__":
    main()
