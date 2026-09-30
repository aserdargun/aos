# Native panel kabulü — 6u / 6v

Bu dilim yeni bileşik katalog önizlemesi ve oturum/runtime bağı panellerini mevcut gerçek Tauri/WebKitGTK kabuğunda sınar. Tarayıcı kabulünün native kabul yerine sayılması önlenir. Geniş ürün, serbest görev yürütme veya kurulabilir release tamamlanmış sayılmaz. Güncel koşu süreleri ve sonuçlar [STATUS](STATUS.md) içindedir.

## Akış ve sınırlar

`tests/test_native_ui.py` mevcut `tests/native_ui_checks.py` AT-SPI sürücüsünü, değişmemiş native binary'yi ve gerçek Docker backend'ini kullanır. Fixture motoru açıkça sentetiktir; GPU/model çalıştırmaz. Browser plugin mevcut değildir; bu native test için Chromium yerine gerçek WebKitGTK pencerenin erişilebilir kontrolleri kullanılır. Giriş ve Türkçe metin, yalnız cookie-korumalı ayrı Xvfb ekranında XTest ile yazılır. Host ekranına klavye/fare gönderilmez.

- `/ui/` URL/başlık/giriş → canlı noVNC → mevcut üç görev/onay/kontrol akışına ek yeni panel etkileşimleri.
- Plan → üç farklı katalog görevi önizlemesi → bağımsız verification ölçütleri. Önizleme sonrası SQL job/action sayıları sıfır kalır; yeni admission düğmesine bu test basmaz.
- Girdi değişince eski bileşik sonuç silinir; olumsuzluk tüm birleşimi reddeder; panel değişiminde sonuç temizlenir.
- Kurtarma → explicit oturum bağı okuması → gerçek kalıcı SQL/lifecycle eşliği. Canlı host süreci varken container sorgulanmaz; bu orphan/cleanup/continuation yetkisi değildir.
- Duraklat/Devam et generation değişiklikleri, panel değişimi ve Durdur/Yeniden başlat eski sonucu temizler. Yeniden başlatma yeni kalıcı runtime binding üretir ve yeni explicit okuma bunu eşleştirir.
- 1440×980 ve 800×600 native pencere görüntüleri `/tmp/aos-native-compound-plan.png`, `/tmp/aos-native-session-binding.png`, `/tmp/aos-native-compact.png` altındadır. Ekran görüntüleri, token ve yerel DB kaynak paketine alınmaz.

SQL sorguları yalnız UI eylemleri sonrasındaki durumu bağımsız doğrular; UI yerine API/SQL yazarak işlem yapılmaz. Native process stdout/stderr denetlenir; özel Xvfb'nin mevcut DRI3/GLX render uyarıları tek tek allowlist'tedir. Bu denetim JavaScript console/error event toplama değildir; native JS console ölçümü hâlâ ayrı açık kabul sınırıdır. Tarayıcı console ölçümü native'e genellenmez.

## Tekrar çalıştırma

Hazır image, pinned Chromium, mevcut native binary ve AT-SPI/Xvfb gerekir; test yeni bağımlılık/model indirmez. Frontend değiştiyse önce build yapılır; binary loopback backend'in güncel `/ui/` içeriğini yükler.

```bash
cd ui && pnpm build && cd ..
AOS_NATIVE_UI_TESTS=1 .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s tests -p test_native_ui.py -v
```

Bu komutta fixture native testi çalışır; gerçek Decider/Bonsai testi explicit `AOS_NATIVE_REAL_TASK_TESTS=1` olmadığı için skip olur. Gerçek model testi ayrıca seri GPU kabulünde çalıştırılır. Fixed port 8765 nedeniyle iki native test suite aynı anda çalıştırılmaz. Test yalnız kendi process group, Xvfb, token ve Docker container'larını temizler.

## Dağıtım ve Wayland

X11 uyumluluk profili `GDK_BACKEND=x11 WEBKIT_DISABLE_DMABUF_RENDERER=1` olarak kalır. Host Wayland oturumu X11 kabulünden farklıdır; bir Wayland process'inin sekiz saniye açık kalması bile login/noVNC/approval/keyboard kabulü değildir. Default Wayland ve temiz makine kurulabilir release kapıları ayrıca açıkça doğrulanmalıdır. [Native arşiv](NATIVE_PACKAGE.md) yalnız private mevcut ELF snapshot'ıdır; installer, imzalı release veya backend/model bağımlılıklarını içeren bağımsız uygulama değildir.

21 Eylül 2026 bounded Wayland probe'u mevcut binary ile, yalnız test-owned pencere/process group ve fixture backend üzerinde tekrarlandı. Default `GDK_BACKEND=wayland` process'i page-load sonrasında `Error 71 (Protocol error) dispatching to Wayland display` ile exit 1 verdi. Yalnız bu process'e `WEBKIT_DISABLE_DMABUF_RENDERER=1` eklenince sekiz saniye açık kaldı ve page-load kaydı üretildi; ilgili stdout/stderr'de hata görülmedi. Host compositor/driver ayarı değiştirilmedi, host input gönderilmedi, bağımlılık kurulmadı. Bu dar startup ölçümü native Wayland etkileşim kabulünü kapatmaz ve varsayılan başlatma profilini değiştirmez.
