# W5 izole adapter inference uyumluluk denetimi

`aos.dataset_adapter_runtime_probe`, önceden yayımlanmış sentetik S1 adapter artifact/rapor çiftini ve güncel immutable dataset/deployment/runner pinlerini yeniden doğrular. İki açık opt-in olmadan model çağırmaz. Ayrı gerçek CUDA sürecinde mevcut pinli `ModelSession.infer` yolunu dataset'teki en çok 32 sentetik train/validation/test kararında önce frozen base, sonra yalnız o süreçte son MLP projeksiyonuna takılmış rank-4 adapter ile çalıştırır. Her bölüm için sayı ve doğru sonlu-seçim sayıları ayrılır; boş bölüm sıfırdır. Hook her candidate kararı için gerçekten çağrılmalı, aynı modelin taban parametre sürümleri değişmeden kalmalı; tüm tahminler sonlu, normalize edilmiş ve exact seçenek kümesinde olmalıdır. Hook süreçten çıkmadan kaldırılır. Canlı worker, registry veya deployment dosyası değişmez.

Önce [özel aday çiftini](DATASET_ADAPTER_CANDIDATE.md) üretip rapor yolunu alın. Sonra:

```sh
.venv/bin/python -m aos.dataset_adapter_runtime_probe \
  --dataset datasets/fixture-v002 \
  --manifest models/decider-manifest.json \
  --report data/decider-adapter-candidates/<artifact-sha256>.<rapor-sha256>.json \
  --model-python /home/cachyos/.venv/bin/python \
  --include-synthetic --runtime-smoke
```

Rapor sadece dataset, artifact, deployment, aday raporu ve runner hash'leri ile ilk train kararının seçenek sayısı/base-candidate seçimi/gold olasılığı, üç bölümün örnek/doğru sayıları ve adapter hook çağrı sayısını verir; ham state, soru, seçenek metni, özel dosya yolu veya ağırlık içermez. Düşük entropili kaynak hash'leri nedeniyle gerçek çıktıyı özel tutun. CLI dosya yazmaz, siteye gitmez, görev veya promotion başlatmaz. `runtime_compatible=true` yalnız bu izole sentetik inference smoke'unun geçtiği anlamına gelir; aktif servis loader'ı, uzun görev, gerçek held-out kalite, web uygulaması başarısı veya güvenlik regresyonu değildir. `quality_improved=false` ve `promotion_authorized=false` sabittir. Mevcut `AOSLORA1` dosyası hâlâ canlı worker'ın deployment biçimi değildir.

Canonical 1.1 sözleşme `schemas/dataset_adapter_runtime_probe.schema.json`, yalnız ilustratif sentetik örnek `examples/dataset_adapter_runtime_probe.json` içindedir. Birim/negatif ve opt-in gerçek CUDA kabulü `tests/test_dataset_adapter_candidate.py` içindedir. Gerçek test `AOS_ADAPTER_CANDIDATE_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python` ister. Yayımlanmış fixture 1/0/0'dır; test ayrıca özel, yayımlanmayan 1/1/1 sentetik artifact ile işçinin üç bölümünü sınar. Bu sentetik varyasyon bağımsız held-out dataset veya ürün değerlendirmesi değildir. Başarısızlıkta çıktı sabit hata mesajıdır; çalışan deployment korunur. Gerçek ölçümler `docs/STATUS.md` içindedir.
