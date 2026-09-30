# Modeller ve kaynak doğrulama

## Başlangıç seçimi

Decider/Bonsai başlangıç implementasyonlarıdır; iki mantıksal rol ayrı değiştirilebilir olmalıdır. Yeni model için typed adapter, immutable pin, model-özel tokenizer/adapter uyumu ve çift-rol regresyonu gerekir; config'te ad değiştirmek yeterli değildir. Sürekli öğrenme, taşınabilir veri ve kontrollü model geçişi hedefi [CONTINUOUS_IMPROVEMENT](CONTINUOUS_IMPROVEMENT.md) içindedir. Genel model değiştirme/promotion servisi henüz uygulanmış değildir.

System-1 `Mapika/decider-2b`: finite option routing ve action selection. System-2 `prism-ml/Ternary-Bonsai-2-27B-gguf`: reasoning, planning, vision, recovery. Ajan mantığı bu adlara değil deployment/capability sözleşmesine bağlıdır. Bonsai her küçük action için çağrılmaz; Operator ayrı generative LLM kullanmaz.

## 19 Eylül 2026 kaynak kontrolü

- [Bonsai model deposu](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/tree/main): PQ2_0 7.21 GB, PTQ1_0 5.95 GB; Q8_0 mmproj 629 MB, BF16 mmproj 931 MB olarak listeleniyor. Bunlar dosya boyutudur, GPU peak memory değildir.
- [Resmi demo](https://github.com/PrismML-Eng/Bonsai-demo/blob/main/README.md) Bonsai-2 için PrismML llama.cpp build'ini gerektiriyor. Rastgele upstream binary uyumlu kabul edilmez. İlk PQ2_0 kararı korunur; PTQ1_0 daha sonra aynı benchmark ile kıyaslanabilir.
- [Decider model kartı](https://huggingface.co/Mapika/decider-2b) English-only sınırlaması ve sürüme bağlı seçenek/context sınırları içeriyor. [Kod deposu](https://github.com/Mapika/decider) daha yeni davranışlar bildiriyor. İncelenen kart 2–10 seçenek ve 1536 token eğitim sınırı, repo 255 seçenek/32K desteği gösteriyordu. Bu fark çözülmeden bu limitler aynı checkpoint'e ait varsayılmaz.

İlk contract için 2–10 seçenek ve 1536 token üst sınırı muhafazakâr engineering default'tur; seçilen tokenizer ile token sayılır. Runtime kod commit'i, HF checkpoint revision'ı, tokenizer, config ve bağımlılık lock'u birlikte sabitlenmeli; sonra gerçek limit ve protocol smoke test yapılmalıdır. Şemadaki karakter sayısı token sınırı yerine geçmez.

## Yerel kabul matrisi

| Deney | Kanıt | Şimdiki durum |
|---|---|---|
| Decider finite choices | exact revision, probabilities, latency, VRAM | CachyOS/4070 Ti SUPER üzerinde 3 seçenekli hello kabulü iki kez geçti; STATUS |
| Bonsai text/structured output | backend revision, schema acceptance | Pinned PrismML CUDA ile missing-file recovery geçti; STATUS |
| Bonsai screenshot + mmproj | dosya hash eşleşmesi ve scene doğruluğu | Tek sentetik canvas'ta capture-bound scene → Decider → bağımsız SAVE outcome geçti; genel vision benchmark değildir |
| İki model resident | idle/peak GPU, OOM, p95 latency | Yapılmadı |
| LoRA attach/switch | exact base/adapter/backend ile parity ve isolation | Yapılmadı |

~4 GB Decider BF16 ve Bonsai ağırlık boyutlarından hesaplanan toplam yalnız planlama içindir. CUDA graph, workspace, KV ve görsel aktivasyonlar eklenir. Bonsai için 16K context başlangıcı ölçüm hedefidir; Decider'ın bağlamıyla karıştırılmaz. FP8 ve CUDA graph optimizasyonları kalite/kalibrasyon testinden sonra etkinleşir.

Kaynak performans rakamları farklı GPU'larda ölçülmüş olabilir; AOS veya 4070 Ti SUPER başarısı olarak aktarılmaz. Model lisansı, executable remote code ve dependencies kurulumdan önce incelenir. Paket lisans veya model kullanım hakkı vermez.

## İlk gerçek yerel probe

19 Eylül 2026'da resmi repo kodu `75b00fade2dd7f353106e3f4683e56fa2481ec28`, HF checkpoint/tokenizer `7789eb65d5cf519737608e218fa88819bddea0af` kullanıldı. Bu checkpoint'in `decider_config.json` dosyası **v8**, temperature=1.3, max_options=255 ve max_state_tokens=32768 bildiriyor; repo README'sindeki v9 sonuçları bu checkpoint'in AOS ölçümü değildir. HF metadata lisansı Apache-2.0 olarak bildirdi.

Adaptör incelenen upstream `Decider.decide` eager API'sini kullanır. Üç seçenek ve gerçek tokenizer ile 90 tokenlık hello prompt'u başarıyla çalıştı. Bu sonuç, yalnız seçilen revision çiftinin dar görev uyumluluğunu doğrular; 10/255 seçenek veya 1536/32768 token sınırında başarı iddia edilmez. Adaptör toplam prompt için 1536 token ve 2–10 seçenek fail-closed sınırını korur.

Code, ağırlık, tokenizer ve config dosyalarının SHA-256'ları ve kullanılan inference ortamının tüm Python paket sürümleri `models/decider-manifest.json` içinde yerel olarak sabitlenir. Model başına 3,811,927,040 byte PyTorch allocated peak ölçüldü; bu sayı tüm sürücü/desktop GPU peak belleği değildir. Bonsai PQ2_0 ve Q8 projektör pinned PrismML CUDA runtime ile ayrıca yüklendi; 16K buffer içinde kısa structured recovery ve daha sonra tek canvas screenshot/scene kabulü geçti. Vision için yeni schema/protocol digest'li ayrı EXPERIMENTAL deployment kullanılır; eski recovery kimliği değiştirilmez. Dolu 16K bağlam, genel vision kalitesi ve ortak residency doğrulanmadı. Ayrıntılar BONSAI_RUNTIME ve VISION_RUNTIME içindedir.
