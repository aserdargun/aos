# Eğitim hazırlık kapıları — 7i

**7k:** `--dataset-loss-report` ile [dataset bağlı S1 forward/loss](DATASET_BOUND_LOSS.md) raporu ayrıca doğrulanır. Yalnız loss kapısı kapanır; S1 tokenizer raporu ayrı, S2/veri/review/gradient/eğitim/promotion kapıları açık kalır.

`aos.dataset_readiness` yalnız mevcut **7a immutable fixture dataset** bütünlüğünü, canonical/converted kayıt eşleşmesini, membership/split/quality sayaçlarını ve isteğe bağlı 7g/7h raporlarının deployment pin bağını denetler. Model, eğitim veya DB writer başlatmaz; içerik/izin üretmez. Her durumda **training_ready=false, real_records=0** kalır; gerçek dataset builder yerine geçmez.

```bash
.venv/bin/python -m aos.dataset_readiness --dataset datasets/fixture-v002 \
  --deployment-manifest models/decider-manifest.json \
  --tokenizer-report data/dataset-tokenizer-probe-v1.json \
  --loss-report data/dataset-loss-probe-v1.json
```

Yalnız `--dataset` de kullanılabilir; eksik preflight raporları ayrı engel olur. Rapor sağlanıyorsa deployment manifest zorunludur. Schema/hashes/membership/gold-converter/quality uyumsuzluğu rapor üretmeden exit 1 verir. Tamamlanan envanter, engeller bulunsa da exit 0 verir; bu uygunluk/izin değildir. Çıktı yalnız sayaç/hash/sabit durum taşır; private path veya raw kayıt içermez. Rapor dosyaları en fazla 64 KiB, son bileşenleri normal dosya olmalıdır; kaynak dataset immutable kabul edilir, canlı yazılan corpus için snapshot hizmeti değildir.

## Raporların sınırı

- `reported_tokenizer_binding=matches`: sağlanan raporun şema/semantik yapısı, checkpoint/tokenizer/code revision, tokenizer/kod hash'leri ve üç dependency sürümü seçilen manifest ile eşleşir.
- `reported_forward_loss_binding=matches`: raporun yapısı/finite metrikleri, exact manifest byte hash'i, checkpoint/code revision ve model ağırlık hash'i eşleşir.
- Bu alanlar raporu yeniden çalıştırmaz veya kriptografik olarak doğrulamaz; yerel rapor/pin bağıdır. **7g/7h dört canonical fixture üzerindedir, build edilmiş dataset split'leri üzerinde değildir.** Bu yüzden iki rapor da eşleşse `dataset_bound_preflight_missing` engeli kalır. Benzer model kimliği dataset kabulü yerine geçmez.
- Ayrı [7j dataset-bound tokenizer](DATASET_PREFLIGHT.md) raporu `--dataset-tokenizer-report` ile verilebilir. Tam dataset/input/deployment/runner ve S1 split eşliği doğrulanırsa `reported_dataset_tokenizer_binding=matches` olur; yalnız S1 tokenizer engeli karşılanır. Dataset-bound forward/loss, S2 tokenizer ve bütün veri/hak/eğitim kapıları açık kalır. Eski raporlar veya içerik hash'i tek başına eğitim yetkisi değildir.
- Veri kayıtları canonical şema, sentetik provenance, tekil sample/membership, record hash, split ve converter gold dahil içerik açısından karşılaştırılır. Değişmiş converter çıktısına yeni dosya/manifest hash'i yazmak semantik eşleşmeyi atlatmaz. Tam task-family kalite/near-duplicate discovery veya genel redaction kabulü değildir.

Sabit eksik kapılar: sentetik-only kaynak, gerçek-source review/redaction hattı, lisanslı replay, gradient/optimizer uyumluluğu, explicit training authority ve promotion servisi. Boş validation/test split'leri ayrıca raporlanır. Mevcut corpus iki sentetik train kaydıdır; bunun için keyfi bir minimum örnek eşiği uydurulmaz veya held-out başarı iddia edilmez.

`schemas/dataset_readiness.schema.json` training-ready/gerçek kayıt/raw-content iddialarını yasaklar; `examples/dataset_readiness.json` placeholder sentetik fixture'dır. Hedefli testler `tests/test_dataset_readiness.py`, gerçek yerel envanter sonucu [STATUS](STATUS.md) içindedir.

**Kalan iş:** gerçek veri/hak ve training approval girdileri olmadan eğitim açılmaz. Görev/UI tarafında [bounded Türkçe görev önizlemesi](TASK_INTENT.md) ayrı uygulanmıştır; genel scheduler ve crash recovery açık kalır. Bu rapor onların kabul kanıtı değildir.
