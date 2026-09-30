# Dataset bağlı S1 tokenizer preflight — 7j

`aos.dataset_preflight`, dört genel canonical fixture yerine doğrulanmış immutable fixture dataset'in **gerçek S1 split içeriklerini** mevcut pinned tokenizer/prompt/collate worker'ına verir. Her split korunur; validation/test kaydı train'e taşınmaz. Bu yalnız tokenizer kontrolüdür: ağırlık/CUDA, forward/loss, gradient, optimizer, eğitim ve promotion çalıştırılmaz.

```bash
.venv/bin/python -m aos.dataset_preflight \
  --dataset datasets/fixture-v002 --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --upstream-source /tmp/aos-decider-dataset-probe --include-synthetic
```

Model Python ve upstream-source yolları yerel kurulum örnekleridir; araç indirme yapmaz. Aynı kullanıcıya ait mevcut pinned tokenizer/code/dependency ortamı gerekir. En fazla 32 S1 örneği, 1536 token ve 1 MiB toplam dataset içeriği kabul edilir; sessiz truncation veya örnek azaltma yoktur. İki layout ve 20 seed üzerinde gold/slot/padding/no-target-leak kontrolleri mevcut tokenizer worker'ında uygulanır. Empty S1 corpus reddedilir; S2 içerik bu worker'a verilmez.

Worker için stdin en fazla 1 MiB, stdout+stderr birlikte 64 KiB ve timeout 120 saniyedir. Nonblocking selector iki çıktıyı eşzamanlı boşaltır; taşma veya hatada yalnız başlatılan child sonlandırılıp beklenir. Ham stderr rapora çıkarılmaz; parent süreç veya başka model servisleri durdurulmaz.

## Kimlik ve değişmezlik

Dataset manifest/file hash'leri, canonical kayıt/converted gold/membership/split/quality eşliği önce doğrulanır. No-follow dosya ve dizin bileşenleri, regular single-link dosya, boyut ve inode/mtime/ctime eşliği denetlenir. Worker'a doğrulanmış converted byte içeriğinden üretilen kopya verilir; worker çıktı verdikten sonra dataset, deployment manifest ve runner kaynak hash'leri tekrar karşılaştırılır. Değişiklikte rapor üretilmez. Canlı yazılabilir corpus için transaction veya same-UID saldırısına karşı değiştirilemez snapshot iddia edilmez.

Rapor dataset ID/manifest hash, tam S1 input hash, split sayaçları, deployment manifest hash, ilgili runner kaynak hash'leri ve minimize tokenizer raporunu taşır. Ham context/target/provenance, dataset yolu veya yerel model yolu içermez. `schemas/dataset_preflight.schema.json` canonical sözleşmedir; `examples/dataset_preflight.json` açıkça sentetik placeholder, çalıştırma kanıtı değildir.

Kaydedilmiş JSON raporu model çağırmadan tekrar bağlamak için:

```bash
.venv/bin/python -m aos.dataset_preflight \
  --dataset datasets/fixture-v002 --manifest models/decider-manifest.json \
  --verify-report data/dataset-bound-tokenizer.json
```

Bu işlem kaynak/rapor bağını doğrular; geçmiş yürütmeye dijital imza veya attestation değildir. Aynı UID'nin yeniden yazdığı rapor güvenilir yürütme kanıtı diye kabul edilemez. Runner değiştiğinde eski rapor otomatik geçerli sayılmaz.

## Readiness bağlantısı

```bash
.venv/bin/python -m aos.dataset_readiness \
  --dataset datasets/fixture-v002 --deployment-manifest models/decider-manifest.json \
  --dataset-tokenizer-report data/dataset-bound-tokenizer.json
```

Eşleşen rapor yalnız `reported_dataset_tokenizer_binding=matches` sağlar. Önceki genel `dataset_bound_preflight_missing` yerine **dataset_bound_forward_loss_missing** ve **supervisor_tokenizer_missing** açık kalır. Gerçek kaynak/lisans/review, yeterli bağımsız split, genel redaction, gradient/optimizer, explicit eğitim ve promotion engelleri kaldırılmaz. `training_ready=false`, `forward_loss_verified=false`, `supervisor_tokenizer_verified=false` korunur. Eski 7g/7h raporları dört genel fixture kabulüdür; yeni dataset-bound kabul yerine geçmez.

Hedefli test: `AOS_TOKENIZER_TESTS=1 AOS_MODEL_PYTHON=... AOS_DECIDER_DATA_SOURCE=... .venv/bin/python -m unittest discover -s tests -p test_dataset_preflight.py -v`. Opt-in kapalı testlerde worker açıkça mock'tur; gerçek tokenizer kabulü ayrıca etkinleştirilir. Güncel kanıtlar [STATUS](STATUS.md) içindedir.
