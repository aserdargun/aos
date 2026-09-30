# Training data foundation

## Dosyalar ve uygulama sırası

| Dosya | Amaç |
|---|---|
| `database/migrations/0001_trajectory_store.sql` | görev/run/step ve tüm trajectory olayları |
| `database/migrations/0002_registries.sql` | model/adapter/deployment/benchmark ve activation audit |
| `database/migrations/0003_execution_state.sql` | canonical güncel state ve değişmez action envelope/hash |
| `schemas/system1_choice.schema.json` | karar supervision kaydı |
| `schemas/system2_supervisor.schema.json` | recovery supervision kaydı |
| `schemas/supervisor_plan.schema.json` | sınırlı executable recovery planı; eğitim kaydı değildir |
| `schemas/browser_observation.schema.json` | sınırlı synthetic form için typed DOM observation |
| `schemas/vision_scene.schema.json` | capture/state-bound synthetic canvas scene; eğitim kaydı değildir |
| `schemas/trajectory_export.schema.json` | taşınabilir tek run export |
| `schemas/registry.schema.json` | registry snapshot |
| `examples/*.json*` | tamamen sentetik canonical fixture'lar |
| `training/recipes/*.yaml` | human-readable deney kontratları |

Migration runner, TrajectoryStore ve sabit hello/browser form kaydı/export'u uygulanmıştır. Export yalnız doğrulanmış bu sentetik görevler için sınırlı içerik kontrolü yapar; genel amaçlı redaction değildir. DOM export'unda pre-action observation `action_id=null` olabilir; verification expected/actual string veya structured object olabilir. İlişki/eşitlik kontrolleri birlikte güncellenmiştir. Selection/redaction/labeling/group split/manifest builder'ın dar sentetik 7a provası uygulanmıştır; [DATASET_FIXTURE_RUNTIME](DATASET_FIXTURE_RUNTIME.md) sınırları tanımlar. SQLite gerçek veri hattı ve trainer entegrasyonu açık kalır. Gerçek Decider ile yürütülen hello ve browser form görevleri de sentetik içeriklidir ve `training_eligible=0` kalır.

## SQL kapsamı

7c `0008_dataset_reviews.sql`, source/candidate/scope bağlı append-only accept/revoke receipt tablosunu ekler; önceki migration'lar değişmez. `aos.dataset_reviews` exact-event host authority olmadan yazmaz ve eligibility açmaz; [DATASET_REVIEWS](DATASET_REVIEWS.md) ayrımı tanımlar. Audit v2 mevcut v7–v12 kaynakları salt okunur destekler. 7d [DATASET_PREVIEW](DATASET_PREVIEW.md) yalnız bounded S1 source→candidate hash/türetme ve receipt preflight sağlar. 7e [DATASET_REVIEWER](DATASET_REVIEWER.md) Linux UID/host allowlist/explicit confirm adaptörüdür; 7f [private servis](DATASET_REVIEW_SERVICE.md) terminal inceleme ve fsync journal/receipt uzlaştırması ekler. Bu veri temelindeki dilimler otomatik eğitim izni üretmez. 7g/7h tokenizer ve forward/loss kontrolleri yalnız sentetik preflight'tır; gerçek veri veya trainer değildir.

7b read-only SQLite envanteri `aos.dataset_audit` içinde uygulanmıştır. Kaynakta migration/reconcile/eligibility update yapmaz; yalnız bellek snapshot'ında schema/FK ve dar label/evidence denetimi yapar. [DATASET_AUDIT](DATASET_AUDIT.md) kalan hak/provenance/redaction kapılarını açıklar; gerçek dataset builder/trainer değildir.

Foreign key, CHECK enum, valid JSON, composite run-step bağları ve idempotency unique index'leri verilir. Probability toplamı, gold membership, evidence doğruluğu, registry hash compatibility ve promotion policy uygulama katmanında ayrıca doğrulanır. DB'ye bağlanan her connection foreign_keys=ON kullanmalı; validator bunun açık olduğunu test eder.

Migration dosyaları sıralı ve bir defa uygulanır. Yeni sürüm için yeni migration ekle; uygulanmış migration'ı değiştirme. Backup + restore drill yapmadan gerçek veriye destructive migration uygulama. Local raw DB dışarı paylaşılmaz; replay export sanitize edilmiş olmalıdır.

## Replay sınırı

Vision-canvas kaydı raw screenshot'ı yalnız ignored yerel artifact olarak tutar. Export image byte'larını çıkarmaz; scene/outcome metadata ve raw artifact hash/referansı içerir, `redacted=false` kalır. Gerçek Bonsai/Decider inference kullanılmış olması sentetik görevi veya screenshot'ı otomatik training-eligible yapmaz.

Export inspect/reconstruct ve dataset build içindir. Replay side-effectful araçları yeniden çalıştırmak anlamına gelmez. Varsayılan replay read-only/offline; canlı yeniden execution ayrı yetki ve idempotency/reconciliation gerektirir. Export'taki model kimlikleri run anındaki snapshot'a dayanır.
