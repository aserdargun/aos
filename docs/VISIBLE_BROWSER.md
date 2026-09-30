# Görünür masaüstü form görevi

Bu dilim yalnız **yerel browser formunu** kullanıcının noVNC ile gördüğü aynı owned Docker/XFCE masaüstünde çalıştırır. Görev ekranındaki görüntü gerçek Chromium penceresidir; screenshot oynatma veya adım animasyonu değildir. CLI/headless browser ve vision davranışları ayrı kalır. Kabul kanıtı ve doğrulanmamış sınırlar `STATUS.md` içindedir.

## Kullanım

Yeni managed pilot `./scripts/aos-v1 start` ile görünür browser etkin başlar. Önceden çalışan headless oturum kendiliğinden dönüştürülmez. Görevler bölümünde **Yerel browser formu** seçili gelir; masaüstü ve görev/onay kontrolleri birlikte görünür.

1. **Browser görevi başlat:** yeni, boş Message alanlı Chromium penceresi açılır. Gerçek model kararı beklenirken alan değişmez.
2. `browser.fill` onayını okuyup **Onayla:** tam `Hello from the local agent.` metni gerçek alana girilir ve ayrı DOM okumasıyla doğrulanır.
3. İkinci model kararı ve `browser.submit` onayı gelir. **Onayla** sonrası Save locally bir kez uygulanır; receipt ve submission sayısı ayrı okunur.
4. Başarılı sonuç penceresi açık kalır. **İzi aç** kalıcı karar/eylem/verification kaydını gösterir. Sonraki herhangi bir görev veya kontrol devri/çıkış/durdurma bu önceki browser penceresini kapatır; Docker masaüstünü ayrıca kapatmaz.

Her eylem eskiyle aynı süreli, exact-action-digest ve tek kullanımlık onaya bağlıdır. “Onayı göster” veya panel değiştirmek onay değildir. Bilgisayar↔Görevler geçişinde aynı noVNC bağlantısı korunur; kontrol değişiminde eski stream kapatılır. Duraklatma aynı canlı run ve browser'ı koruyabilir, Devam et taze gözlem/karar/onay ister. Kontrolü al eski işi iptal edip browser'ı kapatır; eski görevi manuel tamamlanmış saymaz.

Doğrudan backend için:

```bash
.venv/bin/python scripts/serve_desktop.py --engine decider \
  --browser-tasks --desktop-browser --vision-engine bonsai
```

`--desktop-browser`, `--browser-tasks` olmadan reddedilir. Flag verilmezse eski headless yol korunur. GET `/api/tasks` içindeki `browser_display` alanı `desktop` veya `headless` bildirir. POST görev sözleşmesi hâlâ yalnız kind/lease_id/generation'dır; istemci runtime/URL/komut seçemez. Genel görev türü veya migration eklenmedi.

## Çalışma ve güvenlik sınırı

- Var olan pinned image içindeki `/opt/chromium/chrome` kullanılır; yeni indirme, image rebuild, model promotion veya host browser/profile yoktur.
- Dedicated browser, aynı network=none/non-root/cap-drop/read-only container'ın X11 ekranında ve private geçici profilde çalışır. Headless Bubblewrap yerine bu görev için mevcut Docker izolasyonu geçerlidir; VM veya düşmanca masaüstü uygulama izolasyonu iddiası değildir.
- Host–worker bağlantısı yalnız owned container'da `docker exec` stdin/stdout; CDP yalnız container içi Chromium pipe'ındadır. TCP/CDP portu publish edilmez. Worker sabit source ve sentetik fixture kullanır; modelden script, selector, URL, executable veya host path almaz.
- Gateway hâlâ BrowserRuntime tipini, state/runtime/lease/generation, policy ve onayı denetler. Browser child runtime kimliği ile parent desktop/container/image kimliği ayrılır; runtime snapshot bu bağı ve worker/fixture hash'lerini kaydeder.
- DOM snapshot kimliği/node reference/fingerprint, görünürlük, enabled durumu ve üstteki gerçek hedef kontrol edilir. Gerçek CDP pointer/input olayları uygulanır; sonuç bağımsız DOM okumasıyla doğrulanır. Bu sabit güvenilir form içindir; keyfi sayfa script'leriyle eşzamanlı mutation'a atomik genel browser yürütme garantisi değildir.
- Başarılı browser yalnız sonuç görüntülemek için tutulur; bitmiş job'ın onayı/lease'i yeniden kullanılamaz. Yeni görevden önce veya cleanup'ta yalnız owned browser kapatılır. Backend/process crash sonrası sahiplenme/continuation eklenmedi.

## Kabul ve kapsam dışı

Hedefli testler gerçek Docker/Chromium üzerinde açık fixture kararları ile güvenlik/klavye/form akışını, ayrı opt-in gerçek Decider testi ise iki model kararı ve bağımsız verification'ı sınar. Tarayıcı UI kabulü aynı noVNC canvas'ında boş/dolu/kaydedilmiş formu ve iki ayrı onayı gösterir; yalnız frame hash değişmesi başarı kanıtı değildir. Native WebKit, genel web gezintisi, görsel SAVE'in masaüstüne taşınması, gerçek hesaplar ve Office görevleri bu dilimde kabul edilmez.

```bash
AOS_DESKTOP_TESTS=1 .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_desktop_browser.py -v
AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1 .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_visible_task_ui.py -v
```

Sonraki ayrı dilim [görünür SAVE](VISIBLE_VISION.md), `--desktop-vision` ile vision'ı da aynı masaüstüne taşır; flag olmadan headless kalır. Hello bir dosya görevidir; masaüstünde yazma animasyonu göstermez. Görünür form kabulü tek başına vision kabulü değildir.
