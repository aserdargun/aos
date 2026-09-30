# Karar kaydı

| ID | Karar | Durum / köken |
|---|---|---|
| ADR-001 | AOS — Agent Operating System / aos-aserdargun-com | önceki konuşmanın son isim kararı korunur |
| ADR-002 | iki mantıksal ajan: Supervisor + Operator | kabul edilmiş ana yaklaşım |
| ADR-003 | Bonsai-2 + Decider; Operator'da ikinci chat LLM yok | son model tasarımı; Qwen-VL/Jev başlangıç önerilerini supersede eder |
| ADR-004 | native CachyOS inference; Ubuntu Docker agent desktop | son ortam kararı |
| ADR-005 | API/FS/shell/DOM/accessibility/vision sıralaması | ana execution ilkesi |
| ADR-006 | capability routing + dört registry | model bağımsızlığı |
| ADR-007 | S2 tek LoRA; S1 delta/continued training | son adaptation yaklaşımı |
| ADR-008 | verified trajectory → curated dataset → offline training | veri döngüsü |
| ADR-009 | benchmark + açık promotion + rollback | sessiz model değiştirme yok |
| ADR-010 | canonical English state, Türkçe kullanıcı deneyimi | Decider başlangıç sınırlamasına yanıt |
| ADR-011 | Tauri/React, Python/FastAPI/SQLite/uv | son teknoloji yönü |
| ADR-012 | minimal sandbox/gateway'i ilk file testine çek | bu pakette güvenli uygulama sıralaması netleştirmesi |
| ADR-013 | Ubuntu 24.04 başlangıç tabanı; image digest sonra | bu paketin önerisi, eski 24.04/26.04 seçeneği daraltıldı |
| ADR-014 | source/version uyumsuzluğunda model limits fail-closed | 19 Eylül kaynak kontrolüyle netleştirme |
| ADR-015 | İlk file runtime descriptor tabanlı; tek process aracı ağsız Bubblewrap checksum | Docker'da yalnız ilgisiz Windows imajı bulundu. Hello için `O_NOFOLLOW`/`O_EXCL`, tek dosya kapsamı ve kilitler doğrulandı; tam Ubuntu backend sonraki milestone. Geri dönüş: aynı gateway sözleşmesine Docker backend ekle. |
| ADR-016 | İlk gerçek inference mevcut Python 3.12/PyTorch ortamını ayrı worker process'inde kullanır | Çalışan Torch 2.13.0+cu132/Transformers 5.15.0 tekrar kurulmadı. NumPy 2.3.5, upstream `numpy<2` önerisinden farklı; kullanılan eager hello yolu deneyle geçti. Bu ortamın tüm paket sürümleri manifest'te pinli; temiz ayrı inference virtualenv ve kurulum lock'u henüz doğrulanmadı. |
| ADR-017 | İlk harness düşük confidence/escalation durumunda insanda bekler | Bonsai henüz yok; <0.88 durumda execution yapılmaz. Orta-confidence otomatik reobserve/recovery döngüsü sonraki milestone'da uygulanacak; eşikler düşürülmedi. |

## Açık kararlar / deneyler

ADR-018: Bonsai için resmi `prism-b10709-9a9394a` CUDA 13.3 binary'si seçildi. Release SHA-256, HF PQ2_0/Q8 LFS hash'leri, runtime/native kütüphane hash'leri ve output schema digest'i pinlenir. Mevcut CachyOS CUDA 13.4 ortamında gerçek yükleme/structured recovery doğrulandı; kaynak build veya tüm model limitleri iddia edilmez. Geri dönüş yeni manifest ve açık yeniden kabul gerektirir.

ADR-019: İlk Supervisor kabulü read-before-create sentetik senaryosu, gerçek missing-file hatası ve gerçek Bonsai/Decider çağrılarıdır. Sıra kısıtı JSON grammar ve host policy'de birlikte uygulanır; model başarısı genel kurtarma yeteneği olarak genellenmez. Modeller sırayla yüklenir; ortak residency sonraki ölçümdür.

ADR-020: İlk browser kabulü tam Docker masaüstünü beklemeden ağsız Bubblewrap içinde Playwright 1.63.0 + pinned Chromium Headless Shell ile yapılır. Host Chromium bulunmadı; mevcut host profili kullanılmaz. Worker ve browser namespace içindedir; CDP pipe, tmpfs profile ve tek sentetik fixture kullanılır. Chromium iç sandbox'ı kapalı, güvenlik sınırı dış Bubblewrap'tır; arbitrary hostile web ve VM izolasyonu iddia edilmez. Gerçek model fill/submit ve bağımsız DOM verification geçti. Sonraki Docker backend aynı authority/lease/envelope sınırını koruyacaktır; bu değişiklik Ubuntu/XFCE/noVNC mimari hedefini değiştirmez.

Exact backend/checkpoint revisions, trainable Bonsai checkpoint lineage, adapter conversion/hot-swap desteği, iki modelin peak VRAM'i, Decider sürüm/context/option sayısı, dependency locks, gerçek eğitim hiperparametreleri, kalibrasyon eşikleri, Ubuntu image digest ve retention ayarları hedef ortamda doğrulanacak.

ADR-021: İlk vision kabulü semantik DOM hedefi olmayan sabit canvas'ta yapılır. Bonsai bbox'ı host tarafından symbolic capture-bound id'ye çevrilir; bağımsız sentetik uygulama sonucu başarıyı belirler. Atomic freshness-check/canvas event dispatch seçildi; fiziksel OS mouse garantisi verilmez. PNG yalnız ignored local artifact'tir, export metadata-only'dir. Yeni vision schema/protocol ayrı EXPERIMENTAL deployment digest'i üretir; mevcut recovery veya ACTIVE kimlikleri değiştirilmez. Yanlış model kutusunda verification failure testi gerçek canvas üzerinde geçti; genel desktop/vision genellemesi için ek kabul gerekir.

Yeni karar; trigger, kanıt, seçilen alternatif, etkiler ve rollback ile kaydedilir. `STATUS` gerçek uygulama durumudur; bu karar tablosu test raporu değildir.

ADR-022: Ubuntu 24.04 base digest ve son image ID pinli Docker/XFCE/noVNC backend eklendi. Runtime `network none` kullanır; VNC publish etmek yerine authenticated loopback FastAPI → owned Docker exec stdio → container-loopback VNC köprüsü seçildi. AGENT/PAUSED için sunucu seviyesinde view-only, HUMAN için ayrı input listener vardır. Chromium/Electron pencereleri ilk 256 PID/thread sınırında gerçek EAGAIN üretti; profile 512 PID/thread, 3 GiB ve 2 CPU ile sınırlandırıldı. Browser/Electron sandbox kapalı olduğundan Docker dış sınırı korunur; genel hostile web kabulü iddia edilmez. Console QA, CSP data-image izni ve bağlantı kapama sırasını; SIGTERM testi lifespan cleanup gereğini ortaya çıkardı ve regresyon testleri eklendi. Rollback başka image'a sessiz tag taşıma değildir: bilinen source/image manifest'iyle açık yeniden kabul gerekir. Bu web konsolu Tauri/React kararını değiştirmez.
