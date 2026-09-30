# Salt okunur source→candidate önizlemesi

**Uygulanan 7d:** `aos.dataset_preview`, mevcut SQLite run ve açıkça seçilmiş accepted choice label'dan bellek içinde canonical S1 projection oluşturur; dışarı yalnız hash, sayaç, eşleşme durumu ve sabit engel kodları verir. Dataset, yeni label, receipt, kullanım hakkı veya eğitim izni üretmez. `training_ready=false` ve `metadata_claims_verified=false` değişmez.

## Kullanım

```bash
.venv/bin/python -m aos.dataset_preview --database data/aos.sqlite --run-id RUN_ID
.venv/bin/python -m aos.dataset_preview --database data/aos.sqlite --run-id RUN_ID --label-id LABEL_ID
.venv/bin/python -m aos.dataset_preview --database data/aos.sqlite --run-id RUN_ID --label-id LABEL_ID --candidate data/candidate.json --receipt-id RECEIPT_ID
```

Yer tutucuları mevcut yerel kayıt kimlikleriyle değiştirin. Candidate dosyası önceden ve ayrı inceleme akışında hazırlanmış canonical S1 JSON olmalıdır; bu CLI candidate içeriğini oluşturup diske yazmaz. Dosya en fazla 1 MiB olabilir; duplicate JSON key, bozuk canonical kayıt ve son bileşeni symlink olan dosya reddedilir. Candidate için label ID, receipt için candidate zorunludur. Otomatik label seçimi yoktur. Tamamlanan rapor engel içerse de exit 0 verir; bu eğitim uygunluğu sonucu değildir. Argüman/dosya/DB/bütünlük hatasında minimize stderr ve exit 1 döner.

[Audit snapshot](DATASET_AUDIT.md) altyapısı committed WAL dahil tutarlı, query-only bellek kopyası kullanır; exact v7–v12 schema, integrity/FK ve kaynak sınırlarını korur. Kaynak migration, reconciliation veya checkpoint yapılmaz. SQLite SHM koordinasyonu filesystem metadata'sını etkileyebilir; ana DB/WAL içerik değişmezliği tüm filesystem metadata'sının değişmezliği demek değildir.

## Dar türetme sözleşmesi

- Yalnız terminal, succeeded/passed `hello-policy-v1` ve `browser-form-policy-v1` run'ları desteklenir. İkisi de **sentetik içeriklidir**, gerçek Decider çalıştırılmış olsa bile. Vision, S2, policy/human-review label dönüşümü ve image artifact içeren run'lar dışlanır; artifact dosyaları açılmaz.
- Mevcut label `accepted/choice/verified_outcome` olmalıdır. Aynı run/step/decision, bilinen bağımsız verifier, passed expected/actual eşitliği, başarılı gerçek action/gold bağı ve çözülebilen evidence referansları kontrol edilir. Target tam `correct_option/outcome/rationale` içerir; eksik rationale veya gold uydurulmaz. `accepted_choice_labels` yalnız ham accepted/choice sayımıdır, geçerli label sayısı değildir.
- Input, decision'ın işaret ettiği **pre-decision** typed state snapshot'ından türetilir. Digest, phase, run/task/step/version/kind ve soru doğrulanır; `decision_request` ile aynı state/question/ordered options kurulur. Güncel/future runtime state veya label rationale model input'una eklenmez.
- Prediction ve target ayrıdır; seçilen seçenek gold diye kopyalanmaz. Model call varsa aynı run/step, system1, deployment, başarılı status, tam request/response eşleşmesi gerekir. Call yokluğu yalnız explicit `real_model=false` fixture kimliğinde kabul edilir. Bu kayıt tutarlılığı kontrolü yeni model inference veya genel görev doğruluğu kanıtı değildir.
- Projection kimliği run/label hash'ine, zamanı mevcut label zamanına bağlıdır. `split_group=run_id` yalnız lineage alanıdır; benchmark dışlama, group split veya yeterli veri kabulü değildir. Hak/redaction alanları `unreviewed` / `preview-unreviewed-v1` kalır.

`derive_choice` güvenilir host içi yardımcıdır ve bellekte ham metin içerebilir; redactor, bağımsız yetki kapısı veya public içerik API'si değildir. CLI yalnız hash raporu sunar, content-output seçeneği yoktur. Hash/pseudonym anonimleştirme garantisi değildir; tahmin edilebilir içerik doğrulanabilir ve raporlar da yerel/özel tutulmalıdır.

## Hash ve receipt ayrımı

- `snapshot_sha256`: tüm frozen SQLite snapshot'ı. `source_sha256`: review tablosunu ve eligibility flag'ini dışlayan logical `run-content-v1` hash'i; [review sözleşmesi](DATASET_REVIEWS.md) ile aynıdır.
- `projection_sha256`: unreviewed metadata'lı canonical projection. `candidate_sha256`: sağlanan canonical candidate'ın tam hash'i. Bu iki hash hak/redaction metadata'sı nedeniyle farklı olabilir.
- `derivation_matches`: yalnız `provenance.usage_rights` ve `redaction_version` alanları unreviewed placeholder'a çevrildikten sonra **diğer tüm alanların exact canonical eşitliği**. Metin redaction'ı yapan candidate bu sürümde eşleşmez; genel redaction dönüşümü/map doğrulaması desteklenmez. Metadata eşleşme dışında tutulduğu için beyanlar doğrulanmış sayılmaz.
- `receipt_status`: event digest/schema ve revocation bütünlüğü, tam candidate/source hash, run/kind/purpose/provenance/rights/redaction binding kontrolü. v7 kaynak `unavailable_v7` döndürür; migration yapmaz. Diğer sonuçlar `not_requested/missing/revoked/candidate_changed/source_changed/binding_mismatch/current/invalid` olabilir.
- `current` yalnız receipt bağının güncelliğidir. Türetmesi yanlış ama kendi hash'ine bağlı bir candidate için `current` ile `derivation_matches=false` birlikte raporlanabilir. Hiçbiri reviewer kimliğini, kullanım hakkını veya genel redaction'ı kanıtlamaz. Doğrulama ilk kaynak engelinde durabilir; rapor tüm olası kusurların eksiksiz listesi değildir.

En iyi durumda bile `training_not_authorized`, `reviewer_authentication_unverified`, `redaction_unverified`, `usage_rights_unverified`, `synthetic_content_excluded` engelleri kalır. Runtime/tool/deployment yetkisi genişlemez. Receipt yazma API'si, gerçek reviewer authentication/ACL adaptörü ve model çıktısı birbirine karıştırılmaz.

## Doğrulama ve sonraki kapı

```bash
.venv/bin/python -m unittest discover -s tests -p test_dataset_preview.py -v
AOS_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_dataset_preview.py -v
```

Testler yalnız geçici sentetik DB/label/review üretir. Opt-in test gerçek izole Chromium'da **fixture DecisionEngine** ile fill/submit kaydını türetir; gerçek Decider veya insan review'u değildir. Mevcut gerçek-inference kabul DB'lerinde label bulunmadığı için olumlu projection/receipt sonucu beklenmez. Kanıtlar [STATUS](STATUS.md) içindedir; `examples/dataset_preview.json` placeholder hash'li açık sentetik fixture'dır.

7e [yerel reviewer adaptörü](DATASET_REVIEWER.md), kernel UID/host scope/expiry ve exact-event onayıyla sentetik S1 accept/revoke sağlar. Prefix veya güncel receipt tek başına dış audit/authentication kanıtını doğrulamadığı için bu önizlemenin engelleri kaldırılmaz. 7f [private servis](DATASET_REVIEW_SERVICE.md) terminal istemcisi ve dayanıklı journal/receipt uzlaştırmasını ekler. Eğitim, genel redaction, gerçek veri seçimi ve promotion ayrı kapılardır; sentetik tokenizer/forward-loss preflight'ları bunları açmaz.
