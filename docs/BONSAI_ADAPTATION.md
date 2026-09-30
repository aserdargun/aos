# System-2 / Bonsai LoRA stratejisi

V0.1 base Bonsai ile başlar. İlk adapter deneyi failure recovery, diagnosis ve revised plan üzerine dar tutulur. Sonra computer-use, coding/CUDA, browser, research ve personal-workflow uzmanlıkları düşünülebilir.

## Inference ve training ayrımı

Ternary GGUF deployment artefaktıdır; doğrudan bu dosya üzerinde destekli eğitim yapılabildiği varsayılmaz. Uyumlu trainable checkpoint → LoRA training → adapter weights → destekleniyorsa GGUF LoRA conversion → exact quantized base üzerinde inference smoke/parity testi hedeflenir.

Konuşmada topluluk LoRA örneği anılmıştı; bunun linkli orijinal eki bu oturumda erişilebilir değildi. Bu paket **Bonsai-2 PQ2_0/PTQ1_0 + belirli adapter + belirli llama.cpp build** kombinasyonunu doğrulanmış saymaz. Genel llama.cpp LoRA varlığı bu kombinasyona yeterli kanıt değildir.

## Compatibility gate

- Exact base checkpoint/revision/hash, mimari tensor isimleri ve shape eşleşmesi.
- Tokenizer/vocab/chat template eşleşmesi; training ve serving prompt parity.
- Trainable checkpoint ile deploy edilen ternary base'in gerçek lineage'ı; başka Qwen checkpoint'i otomatik aynı base değildir.
- Target modules, rank/alpha/scaling ve conversion tool revision kaydı.
- Backend'in ilgili quantization + architecture + adapter loading desteği.
- Vision mmproj eşleşmesi; ilk denemede vision tower/projector freeze. Multimodal finetuning ayrıca kapsamlandırılır.
- Adapter etkisinin gerçekten uygulandığını ve disable/enable/switch sonrasında request/cache izolasyonunu gösteren test.

Herhangi biri belirsizse candidate compatibility=unknown/failed kalır ve activate edilmez. Alternatif yüksek hassasiyet serving veya farklı trainer seçimi ADR ile belgelenir; aynı adda uyumsuz ağırlık kullanılarak ilerlenmez.

## Kaynak ve lifecycle

27B adapter inference ile 27B LoRA training farklı kaynak gerektirir. 4070 Ti SUPER/16 GB üstünde eğitim başarısı taahhüt edilmez. Yerel smoke/feasibility ölçülür; daha büyük makine kullanımı gerekirse ayrı aktarım ve maliyet kararı gerekir. Üretim inference yerel kalır.

Bir specialization adapter aktif olabilir. Hot-swap backend'te desteklenmiyorsa request drain + process reload geçerli ilk implementasyondur. Her candidate dataset/recipe/training_run/evaluation bağlantılarını taşır. Recovery kazancı genel coding/reasoning/vision başarısını ve safety'yi bozuyorsa reject edilir.
