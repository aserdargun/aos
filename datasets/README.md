# datasets

Versioned train/validation/test JSONL + manifest; gerçek içerik Git dışındadır.

Bu paket gerçek veri veya ağırlık içermez.

Sentetik offline prova: `python -m aos.dataset --include-synthetic --output datasets/fixture-v001`. Hedef yeni dizin olmalıdır; bütünlük için `python -m aos.dataset --verify datasets/fixture-v001`. Üretilen JSONL/manifest kaynak manifest'ine dahil edilmez. [Kapsam ve eksikler](../docs/DATASET_FIXTURE_RUNTIME.md).
