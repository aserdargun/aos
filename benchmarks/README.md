# Benchmark planı

İlk [fixture benchmark çalıştırıcısı](../docs/BENCHMARK_RUNTIME.md) iki dar mevcut akışı gerçek private child/workspace ile ölçer. Katalog tamamını, gerçek model karşılaştırmasını veya promotion kabulünü sağlamaz; 30 bağımsız held-out görevin yerini fixture tekrarları almaz.

`tasks.json` kabul senaryosu kataloğudur, çalıştırılmış benchmark değildir. Codex deterministik fixture, verifier, reset ve timeout'ları uygulamalıdır. `promotion-policy.json` ilk proposed eşikleri içerir; gerçek baseline'dan önce review/freeze edilir.

Training örnekleri bu benchmark'lara dahil edilmez. Ayrı fixture aileleri ve near-duplicate group kimlikleri kullanılır. Her rapor suite hash, environment, code revision, deployment/model/adapter/config hash, raw outcomes ve uncertainty içermelidir. Latency veya success yüzdeleri test yürütülmeden doldurulmaz.
