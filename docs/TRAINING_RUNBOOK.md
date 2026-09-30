# Eğitim koşusu runbook

Bu adımlar eğitim için operasyonel kontrol listesidir. Mevcut `agentctl` bounded runtime görevlerini çalıştırır; genel dataset/train/evaluate/promote/rollback alt komutları yoktur. Ayrı `python -m aos.dataset` yalnız [sentetik offline prova](DATASET_FIXTURE_RUNTIME.md) sağlar. [Dataset bağlı S1 forward/loss](DATASET_BOUND_LOSS.md) ve [fixture benchmark](BENCHMARK_RUNTIME.md) ayrı dar komutlardır; trainer veya genel evaluation servisi değildir. Otomatik eğitim komutu bulunmaz.

1. Scope: hedef capability, base model hash, training yöntemi, hedef ölçüm ve kaynak bütçesini training run kaydına yaz.
2. Snapshot: source DB backup consistency ve sha256 doğrula; kullanım hakkı ve kullanıcı veri scope'unu kontrol et.
3. Build: recipe, schema, builder revision ve redaction sürümüyle dataset üret. Quality/quarantine/overlap raporunu incele.
4. Freeze: dataset version, split manifest ve baseline benchmark'ı kilitle. Hold-out kayıtlarını trainer erişiminden ayır.
5. Preflight: dependency/checkpoint/adapter uyumu, disk ve GPU ölç; çalışan inference lane'lerini güvenli drain et.
6. Smoke: birkaç örnekte format round-trip, target mask/index, loss ve checkpoint save/load doğrula. Overfit smoke gerçek genelleme kanıtı değildir.
7. Train: seed/hyperparameter/code/environment/metrics/checkpoint hash kaydet. Interrupted koşu yeni attempt olarak sürer; eski checkpoint üstüne sessiz yazma.
8. Convert (Bonsai gerekiyorsa): conversion revision ve pre/post parity testleri. Uyumsuzlukta stop/reject.
9. Evaluate: exact candidate deployment ile held-out, safety, recovery ve genel regression. Kaynak/latency ölç.
10. Register: CANDIDATE; model/adapter/dataset/train run/evaluation lineage eksiksiz olsun.
11. Review: kapıları geçiyorsa VALIDATED; geçmiyorsa REJECTED. Kullanıcıya ölçümleri ve belirsizlikleri göster.
12. Promote: ayrı açık yetkiyle activation + rollback rehearsal; aktif state'i tekrar oku.

Her koşu `runs/<training-run-id>/` altında sanitized config, metrics, environment, dataset manifest ref, checkpoint ref, evaluation report ve karar taşır. Ağırlıkları Git'e koyma. Remote resource kullanımı default değildir ve export yetkisi olmadan veri gönderilmez.
