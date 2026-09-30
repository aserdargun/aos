# Kaynağa bağlı dataset inceleme kayıtları — 7c

Bu dilim, açık yetkiyle yapılmış bir incelemeyi içerik ve kapsamına bağlayan yerel kayıt katmanıdır. **İnceleme kararı üretmez, insan kimliği doğrulamaz, otomatik eğitim izni vermez.** Yalnız sentetik testlerle kabul edildi; gerçek run'lara review/eligibility eklenmedi.

## Sözleşme

`schemas/dataset_review_receipt.schema.json` ve `src/aos/dataset_reviews.py` şu alanları bağlar:

- Receipt/run kimliği; source ve canonical redacted candidate SHA-256.
- S1 choice veya S2 supervisor; yalnız `offline_training_text` amacı. Upload, image veya deployment izni yoktur.
- Sentetik/gerçek provenance beyanı; `synthetic_authored` veya `user_authorized` kullanım hakkı beyanı; redaction sürümü.
- Reviewer kimliği ve dış yetkilendirme referansı; accept/revoke kararı, revoke hedefi ve tarih.

Source hash, 7b'nin SQLite backup hash'i **değildir**. `run-content-v1` logical fingerprint: terminal run (`training_eligible` hariç), ilişkili task, steps, snapshots/current state, model calls, decisions, actions/envelopes, observations, verifications, escalation, human interventions, artifact metadata ve trajectory labels. Satırlar deterministik sıralanır; JSON sütunları saklandıkları metinle hash'e girer. `dataset_reviews` hariçtir; receipt eklemek kendi kaynak hash'ini değiştirmez. Aktif run veya intent/running/uncertain action kabul edilmez.

Source fingerprint görüntü dosyalarını açmaz ve içeriğin doğruluğunu yeniden kanıtlamaz. Model/registry kimliği run'ın saklanan deployment snapshot'ıyla bağlıdır; global registry veya desktop session bu run-content hash'ine dahil değildir. Candidate, 7a canonical S1/S2 şema ve semantik kontrolünden geçer. Run/step, candidate hash, provenance, rights ve redaction sürümü receipt ile eşleşmelidir. Bounded hello/browser/vision policy'leri gerçek inference kullanmış olsa da `real` içerik diye etiketlenemez.

Rights eşlemesi: `synthetic_authored` için canonical `usage_rights` alanı `synthetic fixture authored for AOS`; `user_authorized` için aynı adlı değer gerekir. Bu, hakların hukuken doğrulandığı iddiası değil, açık beyan ve içerik tutarlılığı kontrolüdür. Genel secret/PII redaction veya candidate'ın kaynaktan doğru türetildiğini kanıtlama bu dilimde yoktur.

## Yetki sınırı

`append_review(store, receipt, candidate, authority=...)` varsayılan yetkisizdir. `ReviewAuthority` güvenilir host tarafının verdiği, **tam event hash'ine** ve kısa süreli yazma yetkisine bağlı in-process nesnedir. Serbest JSON/dict, model output'u veya UI action approval bu nesne yerine geçmez. Süre hem girişte hem insert öncesi kontrol edilir; scope/reviewer/candidate/decision değişirse hash bağı bozulur.

Bu sınıf bir kriptografik imza, bearer secret veya ayrı process güvenlik sınırı değildir. Güvenilir Python çağırıcısı nesneyi oluşturabilir; çağıranın reviewer authentication/ACL ve explicit onayı sağlaması gerekir. **7e [yerel reviewer adaptörü](DATASET_REVIEWER.md)**, bağlı Linux socket üzerinde kernel UID/host allowlist ve exact-event confirm sağlar; yalnız sentetik S1 kapsamındadır. 7f [private servis ve terminal istemcisi](DATASET_REVIEW_SERVICE.md) explicit CLI ve fsync journal entegrasyonunu ekler; model tool/HTTP/desktop UI yolu açılmaz. Testlerin oluşturduğu authority/authorization_ref tamamen sentetiktir. `created_at` inceleme kaydının beyan edilen tarihidir; receipt sırası veya revoke önceliği bu tarihe göre belirlenmez. Authority expiry yalnız yazma yetkisinin TTL'idir, kullanım hakkı süresi değildir. 7e'nin isteğe bağlı monotonic deadline alanı da girişte ve insert öncesi denetlenir.

`source_fingerprint` düşük seviyeli helper'ı çağıran tutarlı bir okuma transaction'ı kullanmalıdır. `append_review` kendi `BEGIN IMMEDIATE` transaction'ında hash'i yeniden hesaplar; hazırlık ve yazma arasındaki değişiklik fail-closed olur. Başkasının açık writer transaction'ına katılmaz veya onu rollback etmez. Writer sahipliği mevcut TrajectoryStore kilidiyle korunur; readonly store ile append reddedilir.

## Kalıcılık ve iptal

Yeni `database/migrations/0008_dataset_reviews.sql` yalnız tablo/index/trigger ekler. 0001–0007 değiştirilmedi. Normal TrajectoryStore writer'ı sonraki açılışta 0008'i uygular; read-only audit bunu yapmaz. Bu milestone'da migration yalnız geçici sentetik DB'lerde uygulandı; mevcut gerçek kabul DB'leri v7 bırakıldı.

- Receipt alanları ve tam event hash'i saklanır; candidate/ham trajectory içeriği receipt tablosuna kopyalanmaz.
- UPDATE, DELETE ve INSERT OR REPLACE ile aynı ID/acceptance/revocation değiştirme trigger'larla reddedilir. DB dosyasına sahip kullanıcı trigger silebilir; OS veya kriptografik immutable storage garantisi yoktur.
- Aynı ID + aynı payload retry idempotenttir; farklı payload reddedilir. Idempotent dönüş yalnız geçmişteki kaydın varlığını bildirir; iptal edilmiş/stale yetkiyi canlandırmaz.
- Revoke ayrı satırdır, aynı run/source/candidate/kind/purpose/provenance/rights/redaction bağıyla accept receipt'e referans verir. Kaynak sonradan değişse veya eski candidate artık elde olmasa da, yeni exact-event authority ile revoke yapılabilir; candidate byte'ları revoke için gerekli değildir.
- Aynı binding için ikinci accept ve aynı receipt için ikinci farklı revoke engellenir. Revocation terminaldir; aynı içeriğe otomatik yenileme/re-accept yapılmaz. Gelecek yeniden inceleme yaşam döngüsü ayrı sözleşme gerektirir.
- Hata rollback eder; `runs.training_eligible`, model deployment veya aktif iş akışı değiştirilmez.

`review_status(store, receipt_id, candidate)` tutarlı read transaction'ında `missing/current/candidate_changed/source_changed/revoked` ve daima `training_ready=false` verir. `current` yalnız metadata bağı güncel demektir; label doğruluğu, kullanım hakkı kanıtı, genel redaction, benchmark overlap veya eğitim yeterliliği kabulü değildir. Receipt event hash'leri okunurken yeniden kontrol edilir.

## Audit uyumluluğu

7b okuyucu artık `sqlite-eligibility-v2` üretir ve exact v7–v12 schema profillerini destekler. v7 kaynaklarına migration uygulamaz. Receipt olmayan run'larda eksik hak/redaction nedenleri korunur. Herhangi bir receipt bulunan run'da, receipt aktif/stale/revoked ayrımı yapmadan `dataset_reviews_require_validation` engeli raporlanır; aday içeriği olmadan izin sonucu uydurulmaz. Eski v1 sentetik audit fixture'ı hâlâ şemadan geçer. Tüm sürümlerde ready=0'dır.

## Doğrulama

```bash
.venv/bin/python -m unittest discover -s tests -p test_dataset_reviews.py -v
```

Sentetik testler exact authority/expiry, source-candidate-scope değişimi, bounded provenance, append-only/replace reddi, atomik rollback, revoke, retry, restart/read-only erişim ve additive v7→v8 geçişini sınar. Placeholder hash'li `examples/dataset_review_receipts.json` yalnız sözleşme fixture'ıdır; gerçek inceleme veya izin kanıtı değildir. Son ölçümler STATUS'tadır.

7d [DATASET_PREVIEW](DATASET_PREVIEW.md), mevcut accepted label ve pre-decision snapshot üzerinden salt okunur bounded S1 türetme ve receipt preflight ekler; `current` receipt ile yanlış türetmenin birlikte bulunmasını ayrıca raporlar. İçerik export'u veya izin üretmez. 7e adaptörü kimliği bağlantı anında kontrol eder; yalnız receipt prefix'inden sonradan authentication kanıtı çıkarılmaz. 7f kalıcı journal/receipt uzlaştırması ekler. Genel redaction, gerçek kayıt seçimi, training ve promotion henüz yoktur; sentetik tokenizer/forward-loss kabulü ayrı preflight'larda tutulur.
