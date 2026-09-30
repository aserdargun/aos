# Model / Adapter / Deployment / Benchmark registry

Model ağırlığı, adapter ve çalışan deployment farklı kimliklerdir. Registry SQLite tabloları migration 0002'de, taşınabilir snapshot sözleşmesi `schemas/registry.schema.json` içindedir. `examples/registry.json` yalnız disabled tasarım kaydıdır; gerçek hash/revision yokken ACTIVE yapılamaz.

## Kayıtlar

UI'deki registry etkinliği servis sağlığı değildir. `enabled=0` genel kullanıma etkinleştirilmemiş kayıt demektir; açık seçilmiş `EXPERIMENTAL` pilot gerçek Bonsai/Decider çağrılarını yine çalıştırabilir. Çalışma kanıtı ilgili `model_calls` ve bağımsız verification kayıtlarıdır. Yapılandırılmış motor bilgisi GPU'da sürekli resident olunduğu anlamına gelmez.

| Kayıt | Gerekli anlam |
|---|---|
| Model | id, source, immutable revision, architecture, backend, roles/capabilities, precision/quantization, context limit, vision/tool support, weights hash, license, memory estimate ve measured ayrımı |
| Adapter | id/version, exact base hash/revision, type, capabilities, weights hash, dataset/recipe/train run, tokenizer/template, target modules, rank/scaling, conversion/backend compatibility |
| Deployment | id/version, model + en fazla bir specialization adapter + runtime config, projector, context, decoding, backend build, lifecycle, config hash |
| Benchmark | suite/version/hash, dataset split hash, environment/hardware, deployment/config hash, raw metrics, result refs, evaluator version |

SQLite `metadata_json` genişleyebilir ayrıntılar içindir; ilişkisel ID ve compatibility kontrollerinin yerini almaz. Protokol değişimleri schema_version ve migration ile sürümlenir.

İlk uygulama `ModelRegistry` ve `DeploymentRegistry` ile hash'e bağlı EXPERIMENTAL kayıt ekler. Native smoke çalışması `enabled=0` kaydını veya aktif deployment pointer'ını değiştirmez. `AdapterRegistry.record_synthetic_experiment` ayrıca mevcut özel sentetik S1 artifact/rapor çiftini, dataset'i ve exact Decider manifest/identity'sini yeniden doğrulayıp adapter ve ona bağlı `EXPERIMENTAL` deployment'ı idempotent, tek SQLite transaction'ında kaydeder. Bu API yalnız açık verilen `TrajectoryStore` üzerinde çalışır; otomatik managed-session çağrısı veya CLI yoktur. Model `enabled=0`, adapter `compatibility=unknown`, deployment `runtime_enabled=false` kalır; active pointer ve event yazılmaz. İzole runtime probe bu kayıtla otomatik birleşmez ve kalite/uyumluluk terfisi vermez. Benchmark, promotion, drain ve rollback servisleri henüz uygulanmamıştır. Yerel model manifest'i ağırlık/tokenizer/kod SHA-256'larını ve kullanılan Python ortamının paket sürümlerini sabitler.

`AdapterRegistry.inspect_synthetic_experiment` aynı açık kaynak seçimini salt okunur yeniden doğrular: yayımlanmış özel artifact/rapor, güncel dataset/manifest, disabled model satırının bütün alanları, `unknown` adapter metadata'sı ve `EXPERIMENTAL` deployment config'i eşleşmelidir; aktif pointer varsa ret döner. Yalnız bu anda `available_experimental` raporlanır. Önceki kayıt, sonradan silinen artifact'ın erişilebilirliğini kanıtlamaz. İnceleme model çalıştırmaz, registry yazmaz, yükleme veya promotion izni üretmez.

Operatör için `aos.dataset_adapter_registry_inspect`, aynı denetimi explicit DB/dataset/manifest/rapor yollarıyla tutarlı, salt okunur SQLite snapshot'ında çalıştırır; canlı store'u reconcile etmez. Çıktı ve hata sınırı [komut belgesindedir](DATASET_ADAPTER_REGISTRY_INSPECT.md). Kayıt hâlâ otomatik değildir.

## Lifecycle

`EXPERIMENTAL → CANDIDATE → VALIDATED → ACTIVE → DEPRECATED`; evaluation başarısızlığı `REJECTED`. Durum tek başına promotion yetkisi değildir. ACTIVE değişimi ilgili role için active_deployments pointer'ını atomik transaction ile taşır ve deployment_events audit kaydı ekler. Her role bir aktif deployment vardır. Registry service model/adapter hash'lerini, onaylı evaluation'ı ve promotion yetkisini kontrol eder. SQL tek başına bunu sağlamaz.

## Activation / rollback

1. İlgili model lane'ini drain et; devam eden generation'ı bitir veya iptal et.
2. Artifact hash, base/adapter/projector uyumu ve runtime config'i doğrula.
3. Candidate'i yükle; health ve kısa smoke test yap.
4. Benzersiz activation_id ile beklenen previous activation üzerinde compare-and-swap uygula; audit yaz.
5. Yeni çağrılar yeni deployment'a gider. Devam eden run önceki deployment'ını pinleyebilir.
6. Hata/ölçüm regresyonunda önceki bilinen iyi deployment'ı yükle ve smoke testten sonra pointer'ı aynı CAS disipliniyle geri al.

Rollback hedefi, artifact'ları ve config hash'i saklanır. Yalnız `ACTIVE` etiketi değiştirmek yeterli değildir; çalışan process ile DB kimliği uyuşmalıdır. Crash recovery process health/loaded identity'yi sorgular ve mismatch'te çağrıları durdurur.

## Model ve adapter seçimi

CapabilityRouter önce enabled + compatible + VALIDATED/ACTIVE adaylarını ve kaynak bütçesini süzer. Decider yalnız bu sonlu kümeden seçebilir. Deneysel adapter üretim görevi sırasında kendiliğinden yüklenmez. Adapter hot-swap hedefi, backend desteği doğrulanmışsa kullanılır; yoksa controlled restart ve süresi raporlanır. Aktif request'e adapter karıştırılmaz, slot/KV state yeni adapter için temizlenir. Stacking V0.1 dışında.
