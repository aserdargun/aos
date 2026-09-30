# Gerçek tokenizer ve batch preflight — 7g

`aos.dataset_tokenizer`, yalnız repo'nun dört canonical **sentetik S1 fixture'ını** mevcut converter ile upstream Example/Q biçimine dönüştürür ve ayrı pinned Python ortamında gerçek yerel Decider tokenizer/prompt/collate kodunu sınar. **Model ağırlığı yüklemez, CUDA başlatmaz, loss/gradient/optimizer/eğitim çalıştırmaz.** DB, receipt veya eligibility açmaz.

```bash
.venv/bin/python -m aos.dataset_tokenizer --include-synthetic \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --upstream-source /tmp/aos-decider-dataset-probe
```

Yollar mevcut yerel kuruluma aittir; komut indirme/kurulum yapmaz. `--include-synthetic` yoksa worker başlamaz. `--upstream-source` mevcut pinned `core.py` içermelidir; `examples/dataset_converter_pin.json` hash'ine uymayan kod çalıştırılmaz. Sonuç stdout'ta yalnız pin/hash/sürüm/sayaç verir; raw input veya token ID dizisi çıkarmaz. Saklanacak rapor private/ignored alanda tutulmalıdır.

## Denetlenenler

- Checkpoint/tokenizer revision eşitliği ve immutable revision biçimi; pinned tokenizer.json/tokenizer_config/config/chat_template byte hash'leri.
- Upstream `core.py`, model/collate, prompt ve package init hash'leri. Local manifest'in torch/transformers/tokenizers sürümleri mevcut ortamla aynı olmalıdır. Bu üç paket kontrolü ayrı temiz inference venv kurulum kilidi değildir; eski deployment manifest'i değiştirilmez.
- `AutoTokenizer` local-files-only ve remote-code kapalıdır; worker ortamında offline flags, görünmez CUDA ve tek OMP thread kullanılır. **Checkpoint revision raporlanması model.safetensors byte'larının yeniden doğrulandığı anlamına gelmez**; ağırlıklar bu işlemde okunmaz.
- **4 fixture × 20 seed × 2 layout = 160 varyant**, gerçek tokenizer ile A–J ve upstream 255 label token tablosu; option permutation sonrası gold indeksinin korunması.
- State-first/schema-first için context truncation **önceden reddedilir**; tam prompt ve 64'e yuvarlanmış padding ayrıca bütçeye sığmalıdır. Varsayılan bütçe 1536, desteklenen aralık 64–1536'dır. Upstream'in sessiz context kesmesi başarı sayılmaz.
- Gold değiştirildiğinde aynı seed ile input token dizisi değişmemelidir; answer slot sonunda cevap harfi eklenmemiş olmalıdır. Rationale/provenance prompt'a geçirilmez. Bu genel doğal dil hedef sızıntısı/redaction kanıtı değildir.
- **40 gerçek upstream collate batch**: input IDs, right-padding/pad ID, attention mask, slot_idx/slot_batch, gold ve option sayısı birebir denetlenir. Tüm tensor'lar CPU'dadır; model forward yoktur.

Rapor şeması `schemas/dataset_tokenizer_probe.schema.json`, fixture'ı açık placeholder olan `examples/dataset_tokenizer_probe.json` içindedir. `training_ready=false`, `weights_loaded=false`, `loss_verified=false`, `cuda_initialized=false` zorunludur. Semantik sayaç/bütçe/revision eşitliği ayrıca kontrol edilir. Fixture tekrarlarının geçmesi held-out accuracy, trainer, gradient veya VRAM kabulü değildir; eğitim için sentetik benchmark örnekleri seçilmiş sayılmaz.

## Test ve kalan kapı

```bash
AOS_TOKENIZER_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python \
  AOS_DECIDER_DATA_SOURCE=/tmp/aos-decider-dataset-probe \
  .venv/bin/python -m unittest discover -s tests -p test_dataset_tokenizer.py -v
```

Gerçek tokenizer opt-in kapalıyken yerel pin/şema/CLI guard testleri çalışır; gerçek kabul ayrıca raporlanır. Aynı probe tekrarı deterministik hash vermeli; uzun context ve bozuk artifact pin reddedilmelidir. Ölçümler [STATUS](STATUS.md) içindedir.

7h [gerçek-model forward/loss preflight](DATASET_LOSS.md) ayrıca uygulandı; bu tokenizer raporunun `loss_verified=false` alanı değişmez, iki kabul ayrı tutulur. Gradient/optimizer smoke ve gerçek eğitim hâlâ ayrı yetki/veri kapılarıdır. Bonsai trainable-base/LoRA uyumluluğu, yeterli lisanslı/held-out veri ve promotion bu probe ile tamamlanmaz.
