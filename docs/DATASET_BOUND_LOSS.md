# Dataset bağlı S1 forward/loss — 7k

`aos.dataset_bound_loss`, immutable sentetik dataset'in doğrulanmış S1 split içeriklerinde gerçek pinned Decider forward/loss ölçer. Dört genel örnek kullanan eski 7h smoke'unun yerine geçmez; dataset/input/deployment/runner bağı ayrıca doğrulanır. En fazla 32 S1 kayıt ve 1536 token; sessiz truncation, örnek azaltma veya split karıştırma yoktur.

```bash
.venv/bin/python -m aos.dataset_bound_loss \
  --dataset datasets/fixture-v002 --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --upstream-source /tmp/aos-decider-dataset-probe \
  --include-synthetic --forward-only
```

Yollar mevcut yerel kurulum örneğidir. İndirme yok; pinned model/code/tokenizer/dependency ortamı ve en az 8 GiB boş CUDA belleği gerekir. Başka model servisleri durdurulmaz. Parent 180 saniye, 1 MiB stdin, toplam 64 KiB stdout/stderr sınırını uygular; hatada yalnız kendi child'ını durdurup bekler. Ham input/prediction/stderr çıktıya girmez.

Her nonempty split ayrı, iki prompt layout'u ayrı değerlendirilir. GPU microbatch bir örnektir; validation/test kayıtları train'e taşınmaz. Gold permutation, bağlam/padded token bütçesi, sonlu logits/NLL, geçersiz seçenek maskesi ve probability normalization denetlenir. Kayıt başına logits/probability veya içerik saklanmaz; split/layout sayaçları, ortalama NLL ve doğru seçim toplamı raporlanır. Empty split başarı olarak doldurulmaz.

Model `.eval().requires_grad_(False)` ve `torch.inference_mode()` kullanır. Parameter version ve gradient yokluğu tekrar kontrol edilir; `gradient_run=false`, `optimizer_run=false`, `parameters_unchanged=true`, `training_ready=false` korunur. Parametre version kontrolü hostile worker'a karşı attestation değildir. İki layout aynı örneklerdir, bağımsız held-out görev veya genelleme kanıtı sayılmaz.

Dataset kaynakları 7j bounded/no-follow okuyucusuyla korunur; dosya/manifest/converted gold/split/workspace kimliği önce/sonra denetlenir. Model manifest byte hash'i, pinned ağırlık/code kimlikleri, gerçek input hash'i ve ilgili runner kaynak digest'i rapora bağlanır. Kaynak dosyalar değişirse eski rapor geçerli sayılmaz; kaynakların hash'ini yeniden yazmak güvenilir yürütme attestation'ı değildir. Model ağırlıkları veya kaynak dataset değiştirilmez.

## Salt okunur doğrulama ve readiness

```bash
.venv/bin/python -m aos.dataset_bound_loss \
  --dataset datasets/fixture-v002 --manifest models/decider-manifest.json \
  --verify-report data/dataset-bound-loss.json

.venv/bin/python -m aos.dataset_readiness \
  --dataset datasets/fixture-v002 --deployment-manifest models/decider-manifest.json \
  --dataset-tokenizer-report data/dataset-bound-tokenizer.json \
  --dataset-loss-report data/dataset-bound-loss.json
```

Rapor dosyaları komutların gerçek çıktısını içerir; örnek şema fixture'ı yürütme kanıtı yerine kullanılamaz. Verify modeli tekrar çalıştırmaz. Eşleşme yalnız `reported_dataset_loss_binding=matches` verir; dataset-bound loss engeli kalkar, bağımsız S1 tokenizer raporu olmadan tokenizer kapısı kalkmaz. S2 tokenizer, gerçek/lisanslı veri, insan incelemesi, bağımsız validation/test, gradient/optimizer, eğitim ve promotion kapıları açık kalır.

Canonical sözleşme `schemas/dataset_bound_loss.schema.json`, açık sentetik fixture `examples/dataset_bound_loss.json` içindedir. Yeni migration, training run, adapter, deployment activation veya otomatik host yetkisi yoktur. Gerçek kabul sonuçları [STATUS](STATUS.md) içinde; unit mock worker sonuçları açıkça mock'tur.

```bash
AOS_LOSS_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python \
AOS_DECIDER_DATA_SOURCE=/tmp/aos-decider-dataset-probe \
.venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s tests -p test_dataset_bound_loss.py -v
```
