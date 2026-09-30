# System-1 / Decider eğitimi

İlk hedef genel sohbet değil, tool/action selection, escalation ve güvenli abstention'dır. Girdi compact English state + finite options; gold doğru option id. `schemas/system1_choice.schema.json` platform formatıdır.

Önce base checkpoint'e inference baseline al. Kendi verisinde choice accuracy, NLL, Brier, ECE, risk-coverage/AURC ve escalation precision/recall ölç. Confidence=0.88 evrensel doğruluk garantisi değildir; yalnız validation split'te threshold/temperature ayarla, test setini bir kez raporla.

[Mapika deposu](https://github.com/Mapika/decider) full ve mevcut checkpoint üzerinden delta training yollarını belgeliyor; delta/replay ilk tercihtir. Repo ayrıca full recipe'nin tek koşuda uçtan uca çalıştırılmadığı uyarısını içeriyordu. Upstream komutun varlığı AOS dataset'inin hazır train formatı olduğu anlamına gelmez.

Uygulama sırası: checkpoint+code pin → upstream converter → küçük smoke batch → loss/gradient/VRAM kontrolü → lisansı uygun eski görev replay'i ile continued run → held-out eval → candidate. 2B inference belleğinden training belleği çıkarılamaz; optimizer, gradients ve sequence length ölçülür. 16 GB'da sığmıyorsa grad accumulation/checkpointing ve desteklenen precision değerlendirilir; başarısızlık gizlenmez.

**7g uygulandı:** [gerçek tokenizer/batch preflight](DATASET_TOKENIZER.md), dört sentetik fixture üzerinde pinned tokenizer, Example/Q, iki prompt layout'u, gold/slot eşleşmesi, no-truncation ve gerçek collate padding/mask doğrulaması sağlar. Model ağırlığı, forward/loss veya gradient çalıştırmaz; eğitim reçetesi hâlâ tasarımdır ve yeterli gerçek veri/replay kaynağı yoktur.

**7h uygulandı:** [forward/loss preflight](DATASET_LOSS.md), gerçek checkpoint üzerinde sonlu NLL ve VRAM ölçer; eval/no-grad, parameter version değişmezliği ve optimizer yokluğu denetlenir. Dört sentetik fixture genel accuracy veya bağımsız held-out set değildir; gradient/trainer/replay/veri kapıları açık kalır.

LoRA zorunlu değildir. İlk yaklaşım upstream'in delta yoludur; PEFT ancak ayrı uyumluluk/kalite deneyi olarak eklenir. Dataset recipe hyperparameter'ları deneme başlangıcıdır; kanıtlanmış eğitim ayarı değildir.

Teacher/human düzeltmelerinde doğru choice başlangıç listesinde yoksa örnek choice training'e kabul edilmez. Action seçimi, risk ve completion ayrı sorulardır; birinin yüksek skoru diğerini doğrulamaz. Yanlış güvenli prediction'lara özel safety slice tutulur. TR input normalizasyonunun dosya adları, olumsuzluk ve izin kapsamını koruduğu ayrıca test edilir.
