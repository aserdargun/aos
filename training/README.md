# Training

Yeni S1/S2 adayları için model başına ayrı Unsloth LoRA/QLoRA taslak profilleri
ve salt okunur inspector vardır: [UNSLOTH_CANDIDATES](../docs/UNSLOTH_CANDIDATES.md).
Bunlar executable trainer, doğrulanmış Unsloth uyumu veya eğitilmiş adaptör değildir.

Recipe YAML dosyaları çalıştırıcı değildir. `null` alanlar bilinmeyen zorunlu bağımlılıkları gösterir; trainer bunlar çözülmeden fail-closed davranmalıdır. Sayısal değerler ilk smoke deneyi önerisidir, önceki konuşmada verilmiş veya optimize edilmiş sonuçlar değildir.

`recipes/fixture-dataset-v001.json` ayrı offline sentetik builder provasıdır; eğitim reçetesi değildir. `python -m aos.dataset` yalnız explicit opt-in ile çalışır, gerçek veriyi kabul etmez ve training-ready çıktısı üretmez. [Çalıştırma ve converter sınırları](../docs/DATASET_FIXTURE_RUNTIME.md).

Dokümanlar: [overview](../docs/TRAINING_OVERVIEW.md), [pipeline](../docs/DATASET_PIPELINE.md), [runbook](../docs/TRAINING_RUNBOOK.md), [evaluation](../docs/EVALUATION_AND_PROMOTION.md).
