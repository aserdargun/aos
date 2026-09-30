# Dataset pipeline

## Deterministik build adımları

1. Tutarlı SQLite snapshot al; canlı değişen DB'yi satır satır karıştırma. Snapshot hash, schema ve cutoff timestamp kaydet.
2. Recipe'ye göre verified/reviewed ve kullanım hakkı bilinen kayıtları seç; unknown/quarantine kayıtları çıkar.
3. Metin source redaction'ını yeniden denetle; secrets, kullanıcı kimlikleri, local path ve hassas içerikleri maskele. İlk sürüm screenshot binary'lerini dışarıda bırakır.
4. Canonical İngilizce state oluştur; option id, order ve error anlamını koru. Gelecekteki observation/label'ı model input'una sızdırma.
5. System-1 gold ve System-2 corrected plan oluştur; label_source ve verification_ref kaydet. Tahmini output gold değildir.
6. Exact content hash ve near-duplicate kümelerini çıkar. Aynı görev ailesi, repo/site fixture'ı, repeated run ve correction dalları tek leakage group'ta tutulur.
7. Grubu tek train/validation/test split'e ata; benchmark görev ailelerini ayrı tut. Başlangıç 80/10/10 ve seed=42 öneridir, veri azsa oranı zorlamak yerine build'i yetersiz say.
8. JSON Schema + semantik kontrolleri çalıştır; quarantine/rejection nedenlerini ayrı raporla.
9. Immutable dataset dizinine JSONL, manifest ve quality report yaz; hash ve split üyeliklerini kilitle.

Genel hedef çıktı: `datasets/<dataset-id>/<version>/{train,validation,test}.jsonl`, `manifest.json`, `quality_report.json`, `quarantine.jsonl`. SQLite kaynaklı gerçek veri hattı tasarım aşamasındadır. Uygulanan **7a sentetik prova**, komutlar ve sınırlar [DATASET_FIXTURE_RUNTIME](DATASET_FIXTURE_RUNTIME.md) içindedir; genel redaction veya trainer kabulü değildir.

## Manifest

**Uygulanan 7b:** `python -m aos.dataset_audit --database ...` kaynak DB'yi değiştirmeden frozen SQLite snapshot, schema/FK kontrolü ve minimize eligibility/review envanteri sağlar. [DATASET_AUDIT](DATASET_AUDIT.md) sınırları tanımlar. Bu rapor gerçek dataset seçimi veya training authorization değildir; rights/redaction review sözleşmesi eksikken ready=false kalır.

dataset id/version, created_at, source snapshot hash, source cutoff, recipe path/hash, builder code revision, schema version/hash, redaction version, tokenizer/checkpoint revision, seed, split group algoritması, dosya sha256 ve satır sayıları, kaynak hakları, sentetik/gerçek sayıları, review/verification dağılımı, excluded counts ve benchmark overlap raporu.

## Semantik validasyon

**Uygulanan 7d:** [DATASET_PREVIEW](DATASET_PREVIEW.md), mevcut accepted S1 choice label'ı ve karar-anı snapshot'ını kullanarak yalnız hash projection/candidate/receipt preflight verir. Gold uydurmaz, içerik export etmez, hak/redaction beyanını doğrulanmış saymaz. Genel redaction ile değişmiş metin exact türetmeyle eşleşmez; gerçek veri hattı için dönüşüm denetimi ayrı açık kapıdır.

Option id'leri unique; gold/selected kümede; probabilities tam anahtar kümesine sahip ve toplamı 1; state ve target birbirine sızmıyor; referenced verification mevcut; synthetic veri doğru etiketli; failed trajectory/correction ilişkisi tutarlı. JSON Schema'nın tek başına yakalayamadığı cross-reference kuralları builder ve paket doğrulayıcısında ayrıca kontrol edilir.

## Trainer formatına dönüştürme

System-1 JSONL AOS canonical formatıdır; doğrudan upstream train.sh input'u değildir. 7a converter, pinned Decider Example/Q yapısının JSON-safe temsilini üretir: context=state, options aynı sıra, gold=correct_option'ın indeksi. Upstream nesne kurulumu ve shuffle sonrası gold eşleşmesi fixture tokenizer ile test edilmiştir. 7g [gerçek tokenizer/batch](DATASET_TOKENIZER.md) ve 7h [gerçek forward/loss](DATASET_LOSS.md) dört sentetik fixture üzerinde ayrı kabul sağlar; gerçek dataset bağı, gradient/optimizer ve trainer kabulü açık kalır.

System-2 JSONL de doğrudan GGUF eğitimi değildir. 7a converter yalnız problem/evidence/failed_trajectory prompt'u ile structured target'ı ayırır. Gelecek trainer modelin pinned chat template ve tokenizer'ını kullanmalı, loss mask yalnız target üzerinde uygulanmalıdır; mevcut kod token veya loss mask üretmez. Reasoning trace veya target/provenance prompt'a eklenmez; genel serbest metindeki örtük gelecek bilgi sızıntısı bu dar fixture kabulüyle kanıtlanmaz.

Human correction'ları yüksek kaliteli kabul etmek için bile review gerekir. Model-teacher verisi ayrı dilim ve provenansla izlenir; tek teacher'ın kendi çıktısını doğrulaması bağımsız değerlendirme sayılmaz.
