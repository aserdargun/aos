# Protokol ve durum makinesi

## Kimlikler ve canonical state

Her görevde `task_id`, denemede `run_id`, adımda `step_id`, state'te artan `state_version`, yürütmede `action_id` ve `idempotency_key` vardır. UTC ISO-8601 zaman damgası kullanılır. State; original/normalized goal, success criteria, plan/version, current step, workspace/runtime id, observation refs, tool/options, retry budget, risk, progress, artifacts, browser/shell/visual state, ownership, approval ve deployment kimliklerini içerir.

## Geçişler

```text
CREATED → OBSERVE → DECIDE → POLICY → EXECUTE → VERIFY → OBSERVE
                      │         │                    └→ SUCCEEDED
                      └→ SUPERVISOR → REPLAN → OBSERVE
                                └→ WAITING_HUMAN → OBSERVE
Her aktif durum → PAUSED / CANCELLED / FAILED
```

`SUCCEEDED` için görev success criteria'sının tamamı doğrulanmalıdır. Confidence skoru veya çıkış kodu bunu ikame etmez. Tool hata türleri: TOOL_FAILURE, MODEL_FAILURE, INVALID_OUTPUT, TIMEOUT, UI_CHANGED, ELEMENT_MISSING, NETWORK_FAILURE, APP_CRASH, RUNTIME_CRASH, STUCK, UNSAFE_ACTION. Hatalar typed code + sanitized detail + retryable + evidence refs içerir.

## Action envelope (uygulama sözleşmesi)

`schema_version`, `task_id`, `run_id`, `step_id`, `action_id`, `runtime_id`, `state_version`, `owner_lease_id`, `tool`, `arguments`, `expected_effect`, `verification`, `deadline`, `idempotency_key`, `approval_id?`.

Executor tool adı/argument şemasını, path/network policy'yi, state version ve owner lease'i execution anında denetler. Yeniden gelen idempotency key önceki sonucu döndürür; farklı payload ile aynı key reddedilir. Shell gibi dış etkilerde crash sonrası exactly-once garantisi yoktur: intent/result aralığında interruption olursa action `uncertain` kalır, gözlemle reconcile edilir.

## DecisionEngine

İzinli seçenekler code-generated tool/action/element id'leridir. Cevap seçeneği küme içinde olmalı; probability değerleri sonlu ve [0,1], toplam toleransı 1e-6 olmalı. Dağılım eksik, NaN veya geçersizse execute yapılmaz. `selected` model önerisi, `executed` policy sonrası gerçek eylemdir; ikisi ayrı kaydedilir. `ask_supervisor` ve `ask_human` gerektiğinde seçenek olmalıdır.

Başlangıç confidence ≥0.88 execute eligibility; [0.65,0.88) yeni observation/verification; <0.65 Supervisor. Stuck >0.75 ve vision_required >0.70 escalation adayıdır. Bunlar kalibrasyon başlangıcıdır; policy tehlikeli eylemi confidence yüksek olsa da durdurur. Güvenli seçenek listesi kurulamazsa abstain.

## Supervisor ve görsel sahne

Supervisor çıktısı diagnosis, evidence_refs, assumptions, revised_plan, verification_criteria, needs_human içerir; executor'a doğrudan komut göndermez. Gizli chain-of-thought talep edilmez/kaydedilmez; kısa tanı ve denetlenebilir plan yeterlidir.

İlk executable plan sözleşmesi `schemas/supervisor_plan.schema.json` içindedir. Missing-file probe EXECUTE → SUPERVISOR → REPLAN → OBSERVE yolunu uygular; state `plan_version`, `recovery_attempts` ve supervisor deployment kimliğini tutar. Şema ve host kontrolü observe/create/verify sırasını zorunlu kılar; plan yetki ekleyemez. Bu dar runtime planı, geniş `system2_supervisor.schema.json` eğitim kaydından ayrıdır.

Screenshot → Bonsai Vision → structured scene (`capture_id`, `state_version`, image dimensions, application, dialog, elements/id/role/label/bbox) → Decider → symbolic action. Bounding box koordinatları görüntü ölçeğine bağlıdır. Capture değiştiğinde element id/koordinat geçersizleşir; action'dan önce taze state doğrulanır. DOM/accessibility varsa vision çağrılmaz.

İlk çalışan vision sözleşmesi `schemas/vision_scene.schema.json` ile sınırlı 640×360 sentetik canvas'tır. State yolu OBSERVE → SUPERVISOR → DECIDE → POLICY → EXECUTE → VERIFY'dır; recovery REPLAN yolu değişmez. Capture'ın OBSERVE state_version'ı scene içinde korunur; action ayrıca güncel EXECUTE state_version/lease ve scene digest'ine bağlıdır. Native Bonsai yalnız scene verir; host symbolic id üretir ve gateway `vision.click`/`vision.verify` uygular. Ayrıntı ve dispatch sınırı [VISION_RUNTIME](VISION_RUNTIME.md) içindedir.

## Kontrol ve recovery

Browser diliminde `task_kind=browser_form`, fixed scope ve content kullanılır. `schemas/browser_observation.schema.json` taze snapshot ve symbolic element kimliklerini tanımlar. `browser.fill`/`browser.submit` aynı JS çağrısında snapshot/fingerprint/node kontrolü ve effect uygular; `browser.verify` ayrı outcome okur. Stale DOM fail-closed olur; genel otomatik browser recovery yoktur. Ayrıntılar [BROWSER_RUNTIME](BROWSER_RUNTIME.md) içindedir.

AGENT/HUMAN/PAUSED ayrı input sahipliğidir. Take Control atomik lease iptali yapar; kuyruğa girmiş mouse/keyboard işlemleri de geçersiz olur. Return Control yeni observation ve yeni lease gerektirir. Stop terminal cancellation'dır; Resume yalnız paused run'a uygulanır.

6n [plain hello crash-resume](RECOVERY_RESUME.md), yalnız sıfır action/verification ve desktop job olmayan descriptor-workspace checkpoint'inde explicit host admission sağlar. Yeni lease/gözlem/karar/exact-action onayı gerekir; eski approval veya action yeniden oynatılmaz. Belirsiz eylem, Docker/browser/vision ve UI scheduler checkpoint continuation kapsam dışıdır.

6o [write receipt](RECOVERY_EFFECT.md), yalnız standart descriptor hello exclusive-create/fsync/aynı-FD readback'ini, result commit'inden önce ayrı kalıcı observation'a bağlar. Receipt/DB/file eşleşmesi salt okunur denetlenir; receipt, action ok/run success veya resume authority yerine geçmez. Eski uncertain kayıt otomatik değiştirilmez.

6p [explicit write reconciliation](RECOVERY_RECONCILE.md), yalnız paused plain hello ve tek matching receipt'li uncertain action için exact-request host onayıyla action sonucu ve correction audit'ini aynı SQLite transaction'ında kaydeder. Run/state/lease değiştirilmez; yeni verification veya resume authority üretilmez. Kaynak/kanıt değişirse işlem reddedilir; 6n eski-action sınırı korunur.

6q [verification-only continuation](RECOVERY_VERIFY.md), tek uzlaştırılmış hello write için ayrı admission/yeni lease/model kararı ve iki yeni exact-action onaylı read sağlar. Write seçeneği kapalıdır; final readback sonrası source/receipt/approval/lease guard'ı ve bağımsız verification olmadan başarı yoktur. Admission tek denemeliktir, crash sonrası yeniden oynatılmaz; genel recovery veya 6n kapsam genişlemesi değildir.

Başlangıç engineering default: aynı action/state/error üç kez, adım başına en fazla üç attempt, run başına en fazla iki Supervisor recovery döngüsü. Bütçeler config'de değiştirilebilir. Bütçe tükenirse insan/failed; sonsuz retry yok. Progress hash'inde saat gibi değişken alanları filtrele.
