# Görünür SAVE — aynı Agent Computer

Sabit sentetik canvas görevi artık aynı owned Docker/XFCE/noVNC masaüstündeki gerçek Chromium penceresinde çalışabilir. Bu genel masaüstü vision ajanı değildir. Fixture motoru ile gerçek Bonsai/Decider kabulü birbirinden ayrılır; ölçülen sonuçlar [STATUS](STATUS.md) içindedir.

## Kullanım

Yeni managed pilot `./scripts/aos-v1 start` ile görünür browser ve vision açık başlar. Eski çalışan session kendiliğinden dönüştürülmez. **Görevler → Görsel SAVE → Vision görevi başlat** seçin. Canvas'ta SAVE ve CANCEL görünür; Bonsai görüntüyü yorumlar, Decider sonlu sembolik hedeflerden seçer. `vision.click` için **tek ayrı Onayla** gerekir. Onaydan önce tıklama yapılmaz. Sonuç `Selected: SAVE` olur ve bağımsız uygulama okuması SAVE/bir tıklama eşitliğini doğrular.

Başarılı pencere sonraki herhangi bir görev veya kontrol devri/çıkış/durdurmaya kadar kalır. Duraklat aynı canlı run ve pencereyi koruyabilir; Devam et **yeni capture, yeni model kararları ve yeni onay** ister. Eski onay veya capture tekrar kullanılmaz. Kontrol devri işi iptal eder; kapatılan görevin pencere/runtime'ı yeniden sahiplenilmez.

```bash
.venv/bin/python scripts/serve_desktop.py --engine decider \
  --browser-tasks --desktop-browser --desktop-vision --vision-engine bonsai
```

`--desktop-vision` hem `--desktop-browser` hem açık vision motoru gerektirir. Flagsiz CLI/headless yolları değişmez. GET `/api/tasks` alanı `vision_display=desktop|headless` gerçek seçimi bildirir; POST yalnız kind/lease/generation kabul eder. UI ayrıntılı gözlem/karar alt-adımını ölçmüyorsa uydurma adım ilerlemesi göstermez. Hello hâlâ dosya görevidir.

## Capture ve yetki sözleşmesi

- Yeni indirme/pin/image değişimi yoktur. Aynı pinned Chromium, ağsız/non-root owned Docker ve private browser profili kullanılır. Host masaüstü veya tarayıcı profili açılmaz; CDP TCP portu yoktur.
- Bonsai'ye verilen PNG, `Page.captureScreenshot` ile gerçekten render edilmiş **640×360 canvas kırpımıdır**; tüm 1280×800 masaüstü değildir. `canvas.toDataURL()` yalnız değişiklik fingerprint'inde kullanılır, model görüntüsünün kaynağı değildir.
- Mevcut canonical Capture/VisionScene/Action sözleşmeleri korunur: PNG boyutu/SHA, capture kimliği, state version, scene digest ve sembolik hedef bağı. Worker ortak browser kaynağını bellekte paketler; birleşik worker hash'i her iki kaynağı kapsar.
- Capture öncesi/sonrası ve click öncesi canvas node, piksel fingerprint'i, DOM markup, hesaplanmış stiller, viewport/DPR/scroll/geometri ve görünürlük bağı doğrulanır. Origin 0,0, boyut 640×360 ve DPR 1 zorunludur. Click öncesi gerçek rendered PNG SHA tekrar eşleşmeli ve hedef noktasında canvas üstte olmalıdır.
- Tıklama modelin typed bbox merkezinden gerçek CDP pointer olaylarıyla yapılır; executor gizli SAVE koordinatı seçmez. Yanlış model lokalizasyonu yanlış sonuca giderse bağımsız verification başarısız olur; başarı uydurulmaz. Browser araçları vision runtime'da reddedilir.
- Parent desktop/container/image/network sahipliği kontrol edilir; eski lease/generation/onay yeni yetki vermez. Eylem ile ayrı `vision.verify` okuması aynı şey değildir. Sabit güvenilir fixture içindir; son kontrol ile CDP input arasında keyfi düşmanca script değişimine atomik garanti veya genel desktop occlusion güvenliği iddia edilmez.

## Doğrulama

```bash
AOS_DESKTOP_TESTS=1 .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_desktop_vision.py -v
AOS_DESKTOP_TESTS=1 AOS_REAL_BROWSER_TASK_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_visible_scheduler.py -v
AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1 .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_visible_task_ui.py -v
```

İkinci komut gerçek yerel GPU modellerini açıkça çalıştırır. Diğerleri fixture kararlarla gerçek Docker/Chromium sınırlarını test eder. Yeni görünür akışın native WebKit/Wayland kabulü, genel web/Office görevleri, crash continuation, eğitim ve installer ayrı kapsamdır. Küçük mobil ekranda masaüstü küçülür; okunabilir deneme için geniş ekran kullanın.
