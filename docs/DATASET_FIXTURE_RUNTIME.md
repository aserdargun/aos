# Offline sentetik dataset kabulü — 7a

## Çalıştırma

```bash
.venv/bin/python -m pip install -e '.[dataset]'
.venv/bin/python -m aos.dataset --include-synthetic --output datasets/fixture-v001
.venv/bin/python -m aos.dataset --verify datasets/fixture-v001
```

Çıktı dizini yeni olmalıdır; mevcut veya symlink dizine yazılmaz. Sentetik opt-in varsayılan kapalıdır. Builder hiçbir DB açmaz, runtime/model çağırmaz, eğitim/indirme/upload/promotion başlatmaz. `training/recipes/fixture-dataset-v001.json` ayrı, sabit bir prova reçetesidir; iki gerçek eğitim YAML reçetesinin `synthetic=false` filtresi değişmez. Genel tasarım [DATASET_PIPELINE](DATASET_PIPELINE.md), ölçümler [STATUS](STATUS.md) içindedir.

## Veri sınırı ve adımlar

1. Girdi yalnız canonical S1/S2 fixture dosyaları, sentetik evidence/review katalogları ve benchmark ailesi listesidir. `dataset_review.json` bütün canonical kayıt hash'ini, accepted/pending/rejected durumunu, görev ailesini ve elle tanımlanmış yakın-kopya kümesini bağlar. Bunlar **gerçek insan incelemesi veya gerçekleşmiş verification kanıtı değil, sentetik test verisidir**. Dosyalar belleğe bir kez okunur; canlı SQLite snapshot desteği yoktur.
2. Schema ve semantik kontrolün ardından yalnız hash'i katalogla aynı, accepted, bilinen fixture kullanım hakkına sahip sentetik kayıt seçilir. Run/step/outcome/evidence bağı, policy/human-required için insan-review yöntemi denetlenir. Tahmin gold yapılmaz; incelenmiş `target` korunur. Değişmiş içerik, gerçek veri, bilinmeyen hak/redaction veya eksik kanıt dışlanır. Quarantine yalnız hash ve sabit reason code içerir; ham metin veya kontrolsüz sample id kopyalamaz.
3. Redaction **genel secret/PII temizleyici değildir**: yalnız incelenmiş fixture hash allowlist'i ve bilinen `inventory@fixture.invalid` sentetik literalinin maskelenmesi vardır. CRLF→LF dışında boşluk, dosya adı, option sırası ve metnin anlamı değiştirilmez; çeviri veya genel İngilizce normalizer yoktur. Yeni içerik için katalog/hash ve redaction incelemesi gerekir; gerçek veriyi sentetik diye yeniden etiketlemek yetki vermez.
4. Aynı run, kaynak split group, görev ailesi, reviewer yakın-kopya kümesi veya exact model-input hash'i bağlı bileşen oluşturur; S1/S2 arasında da aynı grup korunur. Yakın-kopya keşfi otomatik değildir. `benchmarks/tasks.json` aileleriyle kesişen **bütün bağlı grup** dışlanır. Elle atanmış aileler bilinen fixture overlap'ını yakalar; genel semantik benchmark contamination taraması değildir.
5. Exact input kopyaları tek kayda indirilir; farklı target varsa build bütünüyle reddedilir. Grup hash'i + seed=42, SHA-256/mod100 ile 80/10/10 split seçer. Az veri için oran zorlanmaz; boş split dosyaları tutulur ve yetersizlik açık raporlanır. Canonical `split_group` üretilen bileşen kimliğidir; kaynak kayıt hash'i manifest'te kalır.
6. Her sistem için `{train,validation,test}.jsonl` ve `.converted.jsonl`, ayrıca `quality_report.json`, içerik içermeyen `quarantine.jsonl`, kaynak/recipe/schema/builder/output hash'leri ve split üyelikli `manifest.json` üretilir. Saat/output path dataset kimliğine girmez; aynı kaynaklar aynı byte ve kimliği üretir. `training_ready` daima false'tur; gerçek kayıt sayısı sıfırdır.

Dosyalar private 0700 çıktı dizinine yazılır, fsync sonrası manifest son tamamlanma işareti olarak rename edilir. Yakalanan hatada yalnız yeni çıktı kaldırılır; SIGKILL yarım ve manifestsiz dizin bırakabilir, üzerine yazılmaz. `--verify` manifest kimliğini, sabit dosya listesini, hash/boyut/satır sayısını ve symlink reddini kontrol eder. Bu bütünlük kontrolüdür, imza/otantiklik veya yeniden insan-review değildir. Aynı kullanıcı dosyaları değiştirebilir; OS düzeyinde immutable storage iddiası yoktur.

Mevcut altı fixture'ın dördü benchmark ailesi nedeniyle dışlanır; inventory-routing ve source-clarification örnekleri kalır. İkisi de seed=42 ile train'e düşer; validation/test boştur. Bu sonuç **eğitime yeterli dataset veya held-out başarı değildir**.

## Converter kabulü ve açık sınırlar

S1 converter, Decider `75b00fade2dd7f353106e3f4683e56fa2481ec28` [Example/Q sözleşmesini](https://github.com/Mapika/decider/blob/75b00fade2dd7f353106e3f4683e56fa2481ec28/decider/data/core.py) JSON-safe temsil eder: context=state, options aynı sırada label listesi, gold=correct_option'ın indeksi, task=reviewed family. Upstream trainer pickle cache yükler; bu JSONL doğrudan train.sh girdisi değildir ve builder pickle yüklemez/üretmez. `examples/dataset_converter_pin.json` upstream core/prompt SHA-256 değerlerini sabitler; mevcut inference deployment'ı değiştirmez.

S2 `aos-supervisor-pair-v1` yalnız problem/evidence/failed_trajectory prompt'u ile diagnosis/corrected_plan/verification_criteria/outcome/needs_human target'ını ayırır. Pinned trainable Bonsai base/chat-template/tokenizer yokken token veya target-only loss mask üretilmez; converter **hazırlık çifti**, trainer adaptörü değildir. Reasoning trace alanı şemada yoktur; target/provenance prompt'a eklenmez. Genel serbest metindeki örtük gelecek bilgi sızıntısını bu dar fixture testi kanıtlamaz.

```bash
.venv/bin/python -m unittest discover -s tests -p test_dataset.py -v
```

Varsayılan testler corrected-gold, option permutation, hedefin prompt'tan ayrılması, redaction/content denetimi, evidence/rights, dedup/conflict, transitive leakage grouping, benchmark dışlama, determinism, opt-in, overwrite/symlink ve bozuk çıktı reddini kontrol eder.

Upstream kabulü için pin dosyasındaki revision'ın `decider/data/core.py` ve `decider/prompt.py` kaynaklarını ayrı yerel dizine `core.py`, `prompt.py` adlarıyla hazırlayın; test kendisi indirme yapmaz:

```bash
AOS_DATASET_UPSTREAM_TESTS=1 AOS_DECIDER_DATA_SOURCE=/path/to/pinned/sources \
  .venv/bin/python -m unittest discover -s tests -p test_dataset.py -v
```

Test hash kontrolünden sonra gerçek Example/Q nesnesi oluşturur; 20 seed ve iki prompt layout'unda shuffle sonrası gold indeksini kontrol eder. **Byte-tokenizer fixture** kullanır; gerçek tokenizer/token bütçesi, loss, gradient veya training compatibility doğrulaması değildir. Kaynak modüller yalnız hash eşleşmesinden sonra import edilir; ağırlık/model veya public dataset yüklenmez. Üçüncü taraf kaynak kodu AOS source manifest'ine kopyalanmaz.
