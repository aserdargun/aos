# Trajectory toplama sözleşmesi

## Ne zaman ne kaydedilir?

| An | Kayıt |
|---|---|
| Task açılışı | original/normalized goal, language, success criteria, workspace scope |
| Run açılışı | policy, environment, code revision, pinned deployment/config kimlikleri |
| Observation | DOM/accessibility/shell özeti, artifact refs, state_version |
| Decision öncesi | tam candidate listesi, sırası, option id'leri, state snapshot |
| Model cevabı | probabilities, chosen option, confidence, latency, model call id |
| Policy | allow/deny/approval sonucu, kapsam ve gerekçe |
| Execution | intent önce; actual tool/arguments/status/latency sonra; idempotency key |
| Verification | kriter, method, expected/actual, passed/failed/unknown, evidence refs |
| Escalation | tetikleyici, failed step refs, Supervisor planı, recovery sonucu |
| İnsan | correction/approval/rejection/takeover/resume; actor ve scope |
| Run sonu | verified outcome, failure taxonomy, bütçe, model/resource toplamları |

Migration 0001: tasks, runs, steps, state_snapshots, decisions, actions, observations, verifications, supervisor_escalations, human_interventions, model_calls, artifacts, trajectory_labels. Model registry snapshot run metadata içinde tutulur; çağrının deployment kimliği o günkü exact yapılandırmaya çözülür.

Model_call giriş/çıkışları önce sanitize edilir. Ayrıntılı chain-of-thought saklanmaz; kısa output/plan ve required evidence yeterlidir. Screenshot ve büyük binary DB blob'u yerine local artifact path + sha256 ile saklanır. Artifact erişimi runtime/workspace kapsamıyla sınırlandırılır.

## Tutarlılık

Her child record aynı run'daki step'e composite FK ile bağlanır. Decision state snapshot'ı aynı step'tedir. Action ve verification aynı step'te olmalıdır. Her bağlantıda foreign_keys=ON; DB bootstrap sırasında WAL/busy_timeout uygulanır. Intent kayıt transaction'ı tool çağrısından önce commit edilir; ağ/GUI çağrısı DB transaction'ı açık tutmaz.

Migration one-shot'tır; schema_migrations sürüm ve checksum takibi implementasyon aşamasında runner tarafından kullanılmalıdır. Kısmi migration commit edilmez. Raw kayıtlar append-oriented tutulur; correction eski sonucu üzerine yazmaz, yeni label/review olayıdır.

## Etiket kalitesi

Başarılı eylem için bağımsız verification; başarısız eylem için failed evidence; safety/refusal/escalation için açık policy judgement veya insan review gerekir. Wrong selected action ile verified correct action ayrı tutulur. Correct option başlangıç candidate listesinde yoksa yeni System-1 gold uydurulmaz; candidate-generation hatası işaretlenir, corrected state/options yeni örnek olarak türetilir.

`training_eligible` varsayılan false'tur. Reason codes: secret_detected, missing_verification, ambiguous_label, unknown_license, duplicate, split_leakage, invalid_schema, source_deleted. Kayıtların dataset'e dahil edilmesi bağımsız builder kontrolüne bağlıdır.
