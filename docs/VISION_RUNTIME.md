# Bonsai vision → symbolic canvas action

6b görev entegrasyonu `serve_desktop.py --engine decider --browser-tasks --vision-engine bonsai` ile bu aynı bounded canvas runtime'ını UI'ye bağlar. Capture/scene-bound click ayrı insan onayı ister; kontrol devri native Bonsai/Decider ve owned browser worker'ını kapatır. noVNC masaüstüsünde OS mouse kullanımı değildir. Ayrıntılar [UI_RUNTIME](UI_RUNTIME.md) içindedir; CLI komutları değişmez.

Bu dilim **tek sentetik 640×360 canvas** içindir. Semantik DOM button/label hedefleri yoktur; yazılar ve düğmeler yalnız canvas pikselidir. DOM form görevi hâlâ DOM yolunu kullanır ve vision çağırmaz. Genel masaüstü, arbitrary web sayfası veya tüm Bonsai vision kalitesi kabulü değildir.

## Çalıştırma

Önce mevcut [browser hazırlığı](BROWSER_RUNTIME.md), [Bonsai hazırlığı](BONSAI_RUNTIME.md) ve [Decider hazırlığı](DEVELOPMENT.md) tamamlanmalıdır. Yeni model veya projector indirilmez; aynı pinned PQ2_0 ve Q8_0 mmproj kullanılır.

```bash
.venv/bin/agentctl vision-canvas \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --bonsai-manifest models/bonsai-manifest.json
AOS_BROWSER_TESTS=1 .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
.venv/bin/agentctl export --run-id RUN_ID
```

CLI'da `--engine fixture` yalnız Decider yerine test motoru seçer; vision komutu yine gerçek Bonsai manifest'i gerektirir. Testlerdeki `FixtureVisionSupervisor` açıkça deterministik fixture'dır; gerçek inference kabulü sayılmaz. Integration testleri browser hazırlığı ister, GPU veya model ağırlığı indirmez. Gerçek model kabul kanıtı [STATUS](STATUS.md) içindedir.

## Akış ve kimlik

1. İzole Playwright/Chromium worker yalnız `examples/vision_canvas.html` açar. Browser dilimindeki ağsız Bubblewrap, temiz environment, tmpfs profile ve sınırlı read-only mount'lar korunur.
2. Gateway OBSERVE aşamasında screenshot alır. Boyutlar, PNG header/trailer, SHA-256 ve en fazla 60,000 karakter base64 sınırı kontrol edilir. Bu kontrol genel amaçlı güvenli PNG decoder sertifikası değildir; girdi yalnız sahip olunan renderer'dan gelir.
3. OBSERVE → SUPERVISOR: Bonsai inline PNG'yi görür ve `schemas/vision_scene.schema.json` biçiminde capture_id, capture state_version, dimensions, iki label/bbox veya açık abstention döndürür. `x/y` üst-sol, `width/height` boyuttur; normalize koordinat veya bottom-right değildir.
4. Host exact capture/state eşleşmesini, integer/in-frame/non-overlap bbox'ları, iki farklı label ve needs_human tutarlılığını doğrular. Host symbolic id'leri scene/capture'dan üretir; modelin URL, tool, komut veya host path üretmesine alan yoktur.
5. SUPERVISOR → DECIDE: native Decider SAVE/CANCEL/ask_human seçeneklerinden seçim yapar. Yalnız SAVE seçimi ve confidence ≥0.88 execute eligibility sağlar. State scene digest'i, capture id ve owner lease saklar.
6. Ortak gateway intent/envelope commit, state/lease/deadline/scope/runtime kontrolünden sonra symbolic `vision.click` uygular. Runtime modelin bbox merkezini hesaplar; gateway serbest koordinat kabul etmez.
7. Ayrı `vision.verify` çağrısı sentetik uygulamanın kendi outcome'unu okur. Yalnız `{selected: SAVE, clicks: 1}` tam eşleşmesi run'ı succeeded yapar. Model confidence veya click acknowledgement başarı kanıtı değildir.

Bonsai ve Decider **ardışık** yüklenir; kalıcı ortak residency iddia edilmez. Vision deployment kimliği mevcut model/runtime/projector pin'lerine ek vision schema digest'i ve protocol sürümü içerir. Eski recovery deployment'ı değiştirilmez; yeni vision deployment EXPERIMENTAL kalır ve ACTIVE promotion yapılmaz. Mevcut base model registry metadata'sındaki muhafazakâr reasoning capability kaydı yeniden yazılmaz; vision protokolü ayrı immutable deployment config'inde kayıtlıdır.

## Tazelik ve güvenlik sınırı

Capture sırasında worker bir JSHandle içinde canvas node referansı, pixel data, document markup ve viewport ölçüsünü tutar. Screenshot öncesi/sonrası pixel/markup eşitliği aranır. Eylem anında tek JavaScript çağrısı bu referansları tekrar kontrol eder ve yalnız aynı canvas'a click event dispatch eder. Capture yenilenmesi, piksel değişimi, aynı görünümlü node replacement, markup veya viewport değişimi fail-closed olur. Kullanılmış capture/scene tekrar yürütülemez; idempotency cache önceki sonucu döndürür, tekrar click atmaz.

Bu **canvas'a sentetik browser MouseEvent dispatch** dilimidir; fiziksel OS mouse/keyboard sürücüsü, accessibility automation veya genel screenshot-to-desktop kontrolü değildir. Sabit fixture'da atomic kontrol+dispatch mümkündür; arbitrary sayfa için compositor/animation/transform yarışlarının çözüldüğü iddia edilmez. Worker/Bonsai model çıktısı izinli canvas scope'unu genişletemez. Chromium iç sandbox'ı kapalıdır; izolasyon dış Bubblewrap'tır, VM değildir.

Yanlış bbox modelin SAVE diye etiketlediği yerde CANCEL/MISS sonucu verebilir. Yetki etkisi yine bu etkisiz sentetik canvas içindedir; bağımsız verification hatayı yakalar, false success verilmez. Testte SAVE/CANCEL kutuları kasıtlı değiştirildiğinde gerçek worker CANCEL sonucu üretmiş ve run failed kalmıştır. Bu nedenle tek başarılı hit, pixel-perfect bbox ölçümü veya genel vision benchmark değildir.

Capture/scene uyuşmazlığı, stale owner, düşük confidence ve needs_human input üretmez. Vision sırasında cancellation lease'i iptal eder. Write sonrası acknowledgement kaybı uncertain kalır; otomatik retry yoktur. Genel vision recovery, Resume ve UI takeover kuyruğu sonraki dilimlerdir.

## Yerel kanıt ve veri

- Capture dosyaları DB dizini altında `vision-captures/<capture_id>.png`, mode 0600 olarak saklanır. Varsayılanda `data/vision-captures/` ignore edilir. SQLite artifact kaydı hash, boyut ve `redaction_status=raw` tutar; source manifest screenshot içermez.
- Model call audit PNG'nin yalnız metadata/hash/artifact referansını saklar; base64 veya gizli reasoning kaydetmez. Modelin yapılandırılmış scene yanıtı ve Decider probability'leri yerel DB'dedir.
- Export yalnız bilinen typed metadata/scene/outcome ve artifact referanslarını üretir; görüntü byte'larını gömmez/kopyalamaz. Artifact `redacted=false` kalır, `synthetic=true`, training_eligible=0. Bu bir genel screenshot redactor'ı veya dataset kabulü değildir.
- Mevcut DB schema/migration'ları bu kayıtları desteklediğinden uygulanmış migration değiştirilmedi. Raw capture retention otomatik uygulanmaz; dosyalar yerel kabul kanıtıdır.

## Kaynak

Pinned PrismML runtime'ın [server API belgesi](https://github.com/PrismML-Eng/llama.cpp/blob/9a9394a895b96003ca842a6041cb28ac49a108f7/tools/server/README.md) inline `image_url` içeriğini destekler. AOS yalnız `data:image/png;base64,...` gönderir; remote URL veya file URL kabul etmez. Authenticated ephemeral loopback server, schema-constrained output ve shutdown yolu recovery adapter'ıyla paylaşılır.
