import argparse
from typing import Literal

from pydantic import Field, model_validator

from .contracts import HELLO_CONTENT, TypedModel, canonical, digest
from .task_intent import CATALOG, GoalPreview, preview_goal


class PlanStep(TypedModel):
    order: int = Field(ge=1, le=8)
    phase: Literal["observe", "decide", "act", "verify"]
    description: str = Field(min_length=1, max_length=512)
    approval: Literal["fresh_action", "covered_readback", "not_applicable"]


class BoundedPlan(TypedModel):
    catalog_version: Literal["fixed-plan-v1"] = "fixed-plan-v1"
    task_kind: Literal["hello", "browser_form", "vision_canvas"]
    normalized_goal: str = Field(max_length=1024)
    scope: str = Field(max_length=128)
    runtime: Literal["isolated_workspace", "separate_networkless_browser"]
    preconditions: list[str] = Field(min_length=1, max_length=4)
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    verification_method: Literal["independent_read_equals", "independent_dom_equals", "independent_canvas_equals"]
    expected_json: str = Field(max_length=512)
    execution_authorized: Literal[False] = False
    live_state_verified: Literal[False] = False

    @model_validator(mode="after")
    def exact_catalog(self):
        if self.model_dump() != plan_payload(self.task_kind):
            raise ValueError("plan_catalog_mismatch")
        return self


class PlanPreview(TypedModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["read_only_fixed_plan"] = "read_only_fixed_plan"
    intent: GoalPreview
    plan: BoundedPlan | None
    plan_sha256: str | None = Field(pattern="^[a-f0-9]{64}$")
    execution_authorized: Literal[False] = False
    requires_action_approval: Literal[True] = True

    @model_validator(mode="after")
    def consistent_preview(self):
        if self.intent.status != "recognized":
            if self.plan is not None or self.plan_sha256 is not None or any(
                    value is not None for value in (self.intent.task_kind, self.intent.normalized_goal, self.intent.scope)):
                raise ValueError("unsupported_plan")
        elif (self.plan is None or self.intent.task_kind != self.plan.task_kind
              or self.intent.normalized_goal != self.plan.normalized_goal or self.intent.scope != self.plan.scope
              or self.intent.reason != "exact_catalog_match" or self.plan_sha256 != digest(self.plan.model_dump())):
            raise ValueError("plan_intent_mismatch")
        return self


def plan_payload(kind: str) -> dict:
    if kind not in CATALOG:
        raise ValueError("unsupported_plan_kind")
    goal, scope = CATALOG[kind]
    conditions = ["Güncel runtime, AGENT sahipliği, lease ve policy her gerçek eylemde ayrıca denetlenir.",
                  "Ret, süre aşımı, eski durum veya kontrol devri yürütmeyi engeller; bu önizleme bunları denetlemez."]
    if kind == "hello":
        conditions.append("Yalnız hello.txt; mevcut farklı içerik üzerine yazılmaz. Eş içerik varsa yeniden yazmak yerine okunabilir.")
        stages = [
            ("observe", "İzinli dosyanın mevcut durumunu taze gözlemle.", "not_applicable"),
            ("decide", "Decider yalnız sabit yaz/oku/yardım seçeneklerinden seçer; policy ayrı denetlenir.", "not_applicable"),
            ("act", "Dosya yoksa sabit içeriği yaz; zaten eşitse oku. Seçilen gerçek eylem ayrı onay ister.", "fresh_action"),
            ("verify", "Dosyayı bağımsız yeniden oku; son satır sonu dahil tam içerik eşitliğini doğrula.", "covered_readback"),
        ]
        method, expected = "independent_read_equals", HELLO_CONTENT
    elif kind == "browser_form":
        conditions.append("Yalnız yeni sentetik form; ilk alan boş, receipt boş, submissions=0. Başka sayfa veya ağ yoktur.")
        stages = [
            ("observe", "Message ve Save locally öğelerini taze DOM snapshot'ında gözlemle.", "not_applicable"),
            ("decide", "Decider sonlu doldurma/yardım seçimini yapar; policy ve DOM güncelliği ayrıca denetlenir.", "not_applicable"),
            ("act", "Message alanına tam olarak Hello from the local agent. yaz; ilk eylem onayını al.", "fresh_action"),
            ("verify", "DOM'u bağımsız oku: tam alan içeriği, boş receipt ve submissions=0 beklenir.", "covered_readback"),
            ("observe", "İlk doğrulama geçerse yeni DOM snapshot'ı al; önceki snapshot'ı tekrar kullanma.", "not_applicable"),
            ("decide", "Decider sonlu kaydet/yardım seçimini yapar; policy tekrar denetlenir.", "not_applicable"),
            ("act", "Yeni, ayrı eylem onayıyla Save locally düğmesine bir kez bas; ilk onay buraya taşınmaz.", "fresh_action"),
            ("verify", "Alan ve receipt tam eşit, submissions=1 olmalı; bağımsız DOM sonucunu doğrula.", "covered_readback"),
        ]
        method = "independent_dom_equals"
        expected = {"value": "Hello from the local agent.", "receipt": "Hello from the local agent.", "submissions": 1}
    else:
        conditions.append("Yalnız yeni sentetik canvas; taze capture ve doğrulanmış SAVE/CANCEL sahnesi gerekir.")
        stages = [
            ("observe", "Taze ekran görüntüsünden Bonsai ile capture-bound typed scene çıkar; belirsizlikte insan gerekir.", "not_applicable"),
            ("decide", "Decider yalnız sembolik hedef/yardım seçeneklerinden seçer; model koordinatı yetki değildir.", "not_applicable"),
            ("act", "Capture/scene/state/lease bağlı taze onayla SAVE hedefini bir kez seç; CANCEL izinli sonuç değildir.", "fresh_action"),
            ("verify", "Görüntü modelinden bağımsız uygulama sonucu selected=SAVE ve clicks=1 olmalı.", "covered_readback"),
        ]
        method, expected = "independent_canvas_equals", {"selected": "SAVE", "clicks": 1}
    return {
        "catalog_version": "fixed-plan-v1", "task_kind": kind, "normalized_goal": goal, "scope": scope,
        "runtime": "isolated_workspace" if kind == "hello" else "separate_networkless_browser",
        "preconditions": conditions,
        "steps": [{"order": order, "phase": phase, "description": description, "approval": approval}
                  for order, (phase, description, approval) in enumerate(stages, 1)],
        "verification_method": method, "expected_json": canonical(expected),
        "execution_authorized": False, "live_state_verified": False,
    }


def preview_plan(goal: str) -> PlanPreview:
    intent = preview_goal(goal)
    plan = BoundedPlan.model_validate(plan_payload(intent.task_kind)) if intent.status == "recognized" else None
    return PlanPreview(intent=intent, plan=plan, plan_sha256=digest(plan.model_dump()) if plan else None)


def main():
    parser = argparse.ArgumentParser(description="Sabit görev planını gösterir; yürütme veya onay üretmez")
    parser.add_argument("--goal", required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(preview_plan(arguments.goal).model_dump()))
    except ValueError:
        parser.exit(1, "Geçerli, en fazla 1000 karakterlik bir görev metni gerekli.\n")


if __name__ == "__main__":
    main()
