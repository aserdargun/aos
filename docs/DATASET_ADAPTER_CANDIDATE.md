# W5 sentetik S1 adapter adayı ve bağımsız yeniden yükleme

Bu dar teknik dilim, immutable `fixture-v002` dataset'inin dönüştürülmüş S1 **train** satırlarından gerçek pinli CUDA Decider üzerinde rank-4 adapter'a tek SGD adımı uygular. Taban ağırlıkları donar. Aday, yalnız owner-private `data/` altındaki `0600` dosyaya yazılır; ayrı model süreci dosyayı yeniden yükleyerek aynı train girdilerinde base ve candidate NLL ölçer. Candidate ancak hash, dataset/input/deployment/model pinleri, sonlu ağırlıklar ve bağımsız replay eşleştikten sonra özel artifact ve canonical kanıt raporu çifti olarak yayımlanır. Başarısızlıkta geçici dosyalar silinir; çalışan deployment değişmez.

```sh
.venv/bin/python -m aos.dataset_adapter_candidate \
  --dataset datasets/fixture-v002 \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --include-synthetic --write-candidate
```

İki opt-in zorunludur. Varsayılan çıktı `data/decider-adapter-candidates/` içindedir; alternatif kök de yalnız `data/` dizininin doğrudan altında owner-private olabilir. Worker'lar offline çalışır ve en az 8 GiB boş CUDA belleği ister. Çıktı raporu yalnız hash, aggregate train NLL, varsa ayrı validation/test NLL, bölünmüş sonlu-seçim doğruluğu ve sayımları, gradient/bellek ve güvenlik bayraklarını taşır; ağırlıklar, örnek metinleri veya özel dosya yolu rapora girmez. Yeniden yükleme işçisi train/validation/test satırlarını mevcut immutable dataset sırasına göre ayırır; optimizer yalnız train görür, boş bölümlerin NLL ve doğruluğu `null` olur. Doğruluk her bölümde argmax ile doğru seçilen karar oranıdır; bağımsız görev başarısı veya kalibrasyon değildir. `AOSLORA1` dosyası yalnız deneysel işçiler ve [ayrı inference smoke](DATASET_ADAPTER_RUNTIME_PROBE.md) tarafından anlaşılır; canlı worker'a takılamaz ve deployment değildir. Dosyayı kaynak paketine eklemeyin.

Kalıcı dosya adları `<artifact-sha256>.aoslora` ve `<artifact-sha256>.<rapor-baytları-sha256>.json` biçimindedir; ikisi de aynı özel dizindedir. Rapor kanonik JSON ve son satır sonuyla saklanır. Aynı artifact için süre/bellek ölçümü farklı yeni raporlar oluşabilir. Eşleşmiş dosyayı ve güncel dataset, deployment, runner hash'lerini ağsız yeniden denetlemek için:

```sh
.venv/bin/python -m aos.dataset_adapter_candidate \
  --dataset datasets/fixture-v002 \
  --manifest models/decider-manifest.json \
  --verify-report data/decider-adapter-candidates/<artifact-sha256>.<rapor-sha256>.json
```

Bu komut model çalıştırmaz ve dosya yazmaz; yalnız dosya kimliği, özel izinler, canonical rapor, artifact içeriği ve mevcut kaynak pinlerini karşılaştırır. Eksik/bozuk eşte rapor kabul edilmez. İki dosyanın yayımı tek bir dosya sistemi işlemi değildir; ani process kaybı yetim rapor bırakabilir. Eksik çift aday sayılmaz ve otomatik recovery/promotion yapılmaz. Yeni kod sürümü eski runner hash'ini değiştirebilir; eski raporun tekrar doğrulanması için onun pinli kod sürümü gerekir.

**Bu W5 kabulü veya kalite artışı değildir.** Mevcut fixture yalnız bir sentetik S1 train kararı ve sıfır validation/test kararı içerir; kalıcı raporunda iki held-out NLL/doğruluk çifti de `null` kalır. Ayrı gerçek CUDA split-probe testi işçinin dolu sentetik validation/test hesap yolunu sınar, ancak ek satırlar canonical reviewed dataset üyesi değildir ve kalite/bağımsızlık iddiası vermez. Gerçek bağımsız held-out, genel güvenlik/regresyon değerlendirmesi, gerçek veri hakları, açık gerçek eğitim yetkisi, canlı adapter servisi/uzun görev uyumluluğu, promotion veya rollback kanıtı yoktur. NLL eşit kalabilir. `training_ready`, `execution_authorized` ve `promotion_authorized` kapalıdır; `dataset_readiness` genel gerçek-eğitim engelini bu deney yüzünden kaldırmaz. Bonsai/S2 bu dilimde eğitilmez.

Şema `schemas/dataset_adapter_candidate.schema.json`, açıkça ilustratif fixture `examples/dataset_adapter_candidate.json`, birim/negatif ve opt-in gerçek CUDA testi `tests/test_dataset_adapter_candidate.py` içindedir. Gerçek test için ayrıca `AOS_ADAPTER_CANDIDATE_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python` gerekir. Gerçek ölçümler yalnız `docs/STATUS.md` içinde belirtilir.
