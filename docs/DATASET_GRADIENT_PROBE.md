# W5 sentetik S1 gradient uyumluluk denetimi

`aos.dataset_gradient_probe`, yalnız açık `--include-synthetic --gradient-smoke` ile, mevcut immutable fixture dataset'in dönüştürülmüş S1 **train** satırlarını gerçek pinli Decider üzerinde dener. Dataset bütünlüğü, converter/gold eşliği, model ağırlığı, prompt kodu ve kurulu dependency pinleri denetlenir. En çok dört train kararı ve 1536 token kabul edilir; validation/test satırları optimizer'a veya metriklere girmez.

```sh
.venv/bin/python -m aos.dataset_gradient_probe \
  --dataset datasets/fixture-v002 \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --include-synthetic --gradient-smoke
```

İzole, 240 saniye sınırlı ve ağsız worker en az 8 GiB boş CUDA belleği ister. Taban modelin tüm parametrelerini dondurur; son MLP `down_proj` çıkışına yalnız bellekte rank-4, 32.768 parametreli düşük-rank delta bağlar. Sabit seed 42 ile tek SGD adımı uygulanır. Gerçek model ileri/geri yayılımı, sonlu sıfır olmayan adapter gradienti, adapter parametresinin değişmesi, taban parametre sürümlerinin değişmemesi ve taban gradyanlarının yokluğu doğrulanır. Rapor yalnız hash, sayım, aggregate train NLL, gradient ve peak VRAM taşır; örnek metni veya ağırlık taşımaz.

Bu **fine-tune adayı veya W5 kabulü değildir**: adapter/checkpoint kaydedilmez; aktif deployment'a bağlanmaz; geri yükleme, held-out kalite, lisanslı replay, gerçek veri hakları, eğitim izni, promotion ve rollback yoktur. Mevcut `fixture-v002` yalnız bir sentetik S1 train satırı, sıfır validation/test içerir. Train NLL'nin değişmemesi de mümkündür; probe yalnız hesap grafiği/optimizer uyumluluğunu sınar. `dataset_readiness` içindeki `gradient_optimizer_unverified` genel gerçek-eğitim kapısı bu smoke ile kaldırılmaz; W1–W6 ürün kabulü değişmez.

Şema `schemas/dataset_gradient_probe.schema.json`, açıkça ilustratif fixture `examples/dataset_gradient_probe.json` ve negatif/opt-in testler `tests/test_dataset_gradient_probe.py` içindedir. Gerçek GPU testi ayrıca `AOS_GRADIENT_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python` gerektirir. `docs/STATUS.md` yalnız gerçekten yürütülen ölçümleri kaydeder.
