# W5 — deneysel adapter registry'sini salt okunur denetleme

Önceden `AdapterRegistry.record_synthetic_experiment` ile açık verilen bir SQLite store'a kaydedilmiş sentetik S1 adapter'ın **şu anda** aynı özel artifact/rapor çifti ve pinli Decider kaynaklarıyla eşleşip eşleşmediğini denetler. Komut DB'nin tutarlı, salt okunur bellek snapshot'ını alır; model çalıştırmaz, dosya/DB yazmaz ve canlı deployment'a dokunmaz.

```sh
.venv/bin/python -m aos.dataset_adapter_registry_inspect \
  --database data/<private-registry.sqlite> \
  --dataset datasets/fixture-v002 \
  --manifest models/decider-manifest.json \
  --report data/decider-adapter-candidates/<artifact-sha256>.<report-sha256>.json
```

Bu örnekteki özel DB yolu ancak aday ayrıca o DB'ye kayıt edildiyse geçerlidir; candidate dosyası oluşturmak tek başına kayıt değildir. Denetim `data/` altındaki özel rapor/artifact'ın canonical kimliğini, dataset ve manifest pinlerini, disabled modelin tüm alanlarını, `unknown` adapter'ı, `EXPERIMENTAL` deployment config'ini ve aktif pointer yokluğunu yeniden kontrol eder. Eksik/bozuk kaynakta exit 1 ve sabit hata mesajı verir; kaynak yolu, ağırlık veya örnek metnini çıktıya yazmaz.

Başarı raporu yalnız snapshot ve artifact/registry hash'leri ile `available_experimental`, `runtime_enabled=false`, `promotion_authorized=false` içerir. Şema `schemas/dataset_adapter_registry_inspect.schema.json`, yalnız ilustratif fixture `examples/dataset_adapter_registry_inspect.json` içindedir. Bu durum, adayın görevde çalışabildiği, kalite eşiğini geçtiği veya promotion izni aldığı anlamına gelmez. Gerçek/lisanslı held-out veri, bağımsız görev benchmark'ı, kontrollü loader/activation ve rollback hâlâ açık W5 kapılarıdır.
