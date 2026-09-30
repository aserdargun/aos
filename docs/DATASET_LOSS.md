# Gerçek-model forward/loss preflight — 7h

`aos.dataset_loss`, mevcut pinned Decider checkpoint'ini dört **sentetik canonical S1 fixture** için yükler; iki upstream prompt layout'u üzerinde toplam sekiz kararda gerçek forward ve cross-entropy/NLL hesaplar. Model eval + requires_grad=false + inference_mode kullanılır. **Backward, gradient, optimizer, checkpoint yazma, adapter veya eğitim yoktur.**

```bash
.venv/bin/python -m aos.dataset_loss --include-synthetic --forward-only \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --upstream-source /tmp/aos-decider-dataset-probe
```

İki explicit flag olmadan worker başlatılmaz. Yeni model/paket indirmez. Mevcut manifest'in **tüm model/kod dosya hash'leri ve dependency kümesi** doğrulanır; tokenizer/checkpoint revision eşitliği, pinned core/prompt ve local offline model yüklemesi korunur. GPU'da en az 8 GiB boş bellek yoksa reddeder; başka süreçleri durdurmaz. Alt süreç süre sınırı 180 saniyedir; çıkışta model/bellek sürece ait olarak serbest kalır.

## Kabulün anlamı

- Gerçek upstream DecisionModel/collate, BF16, seed 42, state-first ve schema-first layout. Her batch dört örnek; prompt ve padding 1536 token sınırına uymalıdır, sessiz truncation yasaktır.
- Valid option logits sonlu, geçersiz option logits -inf; softmax geçersiz seçenekleri sıfırlar ve toplamı birdir. Gold permutation korunur; her gold NLL sonlu olmalıdır.
- İşlem öncesi/sonrası parameter version counter'ları aynı, tüm `.grad` alanları boş olmalıdır. Bu RAM içi update kontrolüdür; worker hiçbir model kaydetme/optimizer kodu çağırmaz. Raporda `parameters_unchanged=true`, `gradient_run=false`, `optimizer_run=false`, `training_ready=false` zorunludur.
- Tepe PyTorch CUDA allocated/reserved byte ve model yükleme/forward süresi ölçülür. Artifact hash doğrulaması ve process startup bu elapsed süreye dahil değildir. Ölçüm genel training belleği, optimizer kapasitesi, 16K context veya ortak Bonsai residency için kullanılamaz.
- `correct_choices` yalnız dört bilinen sentetik fixture üzerindeki gözlemdir. İki layout aynı veriyi tekrarlar; **sekiz bağımsız held-out örnek veya genel accuracy/benchmark başarısı değildir**. Bu örnekler production training corpus'una eklenmez.

Rapor yalnız pin/hash/aggregate metrik içerir; raw input, token dizisi veya model cevabı döndürmez. `schemas/dataset_loss_probe.schema.json` ve ayrı semantik/finite metric doğrulaması kullanılır. `examples/dataset_loss_probe.json` açık placeholder fixture'dır; gerçek model kanıtı değildir. Gerçek ölçümler [STATUS](STATUS.md) ve ignored/private `data/dataset-loss-probe-v1.json` içindedir.

```bash
AOS_LOSS_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python \
  AOS_DECIDER_DATA_SOURCE=/tmp/aos-decider-dataset-probe \
  .venv/bin/python -m unittest discover -s tests -p test_dataset_loss.py -v
```

## Kalan kapılar

Gerçek training başlatılmadı. Hakları incelenmiş yeterli veri, genel redaction/dönüşüm denetimi, task-family/near-duplicate leakage kontrolü, lisanslı replay kaynağı ve bağımsız held-out değerlendirme yoktur. Gradient/optimizer/VRAM smoke ve Bonsai trainable-base/LoRA desteği ayrıca kanıtlanmalıdır; bu forward kabulü otomatik training veya promotion yetkisi değildir.

7i [readiness raporu](DATASET_READINESS.md) eğitim başlatmadan veri/split/converter ve rapor-deployment bağlarını denetler; eksik kapıları fail-closed tutar. Genel görev yürütme/UI/çökme recovery hedefleri de ayrı açık işlerdir.
