# İzole Agent Computer

## Uygulanan sınır

`DesktopRuntime`, mevcut filesystem ComputerGateway ve Operator sözleşmesine Docker backend ekler. Ubuntu 24.04, XFCE/X11, 1280×800 Xvfb ve noVNC gerçek süreçlerdir. Chromium, VSCodium, Thunar, XFCE Terminal, LibreOffice Writer/Calc, Evince, Git, Python, Node ve pnpm image içinde bulunur. Native GPU modelleri container'a taşınmaz.

Bu Aşama 5 ve dar bir kontrol konsoludur; tamamlanmış Tauri/React UI, genel görev normalizer'ı veya serbest masaüstü ajanı değildir. `hello --runtime desktop` aynı finite karar/policy/trajectory döngüsünü kullanır. Konsolun sentetik klavye kontrolü ayrı bir deterministic testtir; Decider/Bonsai çıkarımı diye kaydedilmez. CLI ve konsol aynı workspace'i eşzamanlı sahiplenemez. Genel görev akışının UI'ye bağlanması Aşama 6 kapsamındadır.

## Hazırlık

```bash
.venv/bin/python -m pip install -e '.[browser,desktop]'
PLAYWRIGHT_BROWSERS_PATH="$PWD/models/playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python scripts/prepare_desktop.py
```

Hazırlık açıkça ağ kullanan bir build işlemidir: pinned Ubuntu base digest, hash kontrollü VSCodium/Node arşivleri, Chromium revision 1243 ve pnpm 11.26.0 kullanır. Docker context yalnız `computer/`, iki arşiv ve Chromium dosyalarından oluşur; host home/model ağırlıkları/trajectory gönderilmez. Apt paketleri build anında çözülür, `/opt/aos/packages.txt` envanteri image'a konur. Son immutable image ID, source hash'leri ve indirme hash'leri yerel `models/desktop-manifest.json` içine yazılır. Bit-for-bit tekrarlanabilir apt build veya upstream imza doğrulaması iddia edilmez; mevcut image ve kaynak eşleşmesi startup'ta zorunludur. `computer/` değişirse açık rebuild gerekir.

Bu makinede Docker buildx yoktur; hazırlayıcı daemon'un legacy builder'ını kullanır. Runtime Docker socket'ini yalnız host backend üzerinden kullanır. Mevcut kullanıcı non-root UID/GID 1000 ile doğrulandı; farklı UID ve rootless Docker henüz kabul edilmedi.

## Çalıştırma

```bash
.venv/bin/agentctl hello --runtime desktop --engine decider \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --workspace data/desktop-hello

.venv/bin/python scripts/serve_desktop.py
```

Konsol varsayılan `http://127.0.0.1:8765/` adresinde dinler. Terminalde yazan **0600 izinli yerel token dosyasının** içeriğini giriş formuna verin; token URL'ye, log'a veya kaynak dosyaya yazılmaz. `--port`, `--workspace` ve `--database` açıkça seçilebilir. SIGINT/SIGTERM ile normal kapanış sahip olunan container'ı ve token dosyasını temizler; workspace kalır. Testler token ve container cleanup'ını ayrıca doğrular.

Pause/Stop/Take Control/Return Control/Resume/Restart uygulanmıştır. Her geçiş önce lease'i değiştirir, generation'ı artırır ve bekleyen sentetik agent input'unu iptal eder. Return/Resume taze desktop readiness gözlemi gerektirir. Restart yeni runtime kimliğiyle **PAUSED** başlar; devam açık Resume ister. Stop yalnız kendi runtime label'ı eşleşen container'ı kaldırır. Başka container'lara stop/prune uygulanmaz.

Konsol backend'i aynı event loop'ta tek input yürütür. Başlamış bir input geri alınamaz; kontroller devam eden bounded tool tamamlandıktan sonra işlenir. Kuyruktaki input iptal edilir, yürütme intent'i etkiden önce kalıcıdır, kayıp acknowledgement `uncertain` olur ve tekrar oynatılmaz. Bu tam görev scheduler'ı veya anlık acil kesme garantisi değildir.

## İzolasyon ve VNC

- `--network none`; yalnız container loopback arayüzü. Host ağ erişimi, published VNC portu veya GPU aygıtı yoktur.
- Non-root, read-only rootfs, `cap-drop ALL`, `no-new-privileges`, Docker init/reaper, 3 GiB memory/2 CPU/512 PID-thread sınırı. İlk 256 sınırı gerçek Chromium/Electron pencereleriyle fork reddi üretti; 512 profili bu nedenle seçildi. `/tmp`, `/run` ve agent home geçici tmpfs'tir.
- Tek rw bind mount repo `data/` altında ayrılmış workspace'tir. Host home, X11/Wayland, browser profili ve Docker socket mount edilmez. Docker host kernel'ini paylaşır; VM izolasyonu değildir.
- Chromium/Electron iç sandbox'ı bu container profilinde `--no-sandbox` ile kapalıdır; güvenlik sınırı Docker kısıtlarıdır. Düşmanca/genel web gezintisi için kabul yapılmadı. Dış ağ zaten kapalıdır.
- VNC 5900/5901 yalnız **container loopback** üzerinde açılır. Host API, sahip olduğu `docker exec` stdio bridge üzerinden binary RFB taşır; TCP publish veya container dış ağı gerekmez.
- AGENT/PAUSED için ayrı `x11vnc -viewonly` sunucusu kullanılır. Tarayıcı `viewOnly` bayrağını değiştirmek yetki kazandırmaz. HUMAN için input sunucusu seçilir; lease değişiminde eski WebSocket/bridge kapatılıp temizlenmeden kontrol dönmez.
- HTTP Host ve POST Origin eşleşmesi, WebSocket Host/Origin/cookie doğrulaması zorunludur. Session cookie HttpOnly/SameSite=Strict; JSON 4096 byte, WebSocket frame 65536 byte sınırındadır. CSP yalnız yerel script ve noVNC'nin inline görüntü verisine izin verir; uzak script, unsafe-eval ve framing kapalıdır.
- Kalıcı clipboard senkronizasyonu, dosya yükleme veya keyfi shell API'si yoktur. İnsan terminali yalnız izole container içinde çalışır. Genel Approve/Reject protokolü yoktur; yalnız üç sabit görev için dar onay gate'i UI_RUNTIME içinde tanımlıdır.

## Crash ve kayıtlar

6t [kalıcı lifecycle günlüğü](LIFECYCLE.md), Docker create öncesinde `workspace.parent/.aos-lifecycle/<runtime_id>.jsonl` private dosyasına fsync edilmiş doğum kaydı yazar. Günlük workspace dışında kalır; image/source/workspace ve host boot/PID/start ticks/namespace/UID bağını taşır. Create/start/remove yanıtları append-only olaylarla izlenir; create yanıtı kaybolduğunda exact ad ve birth digest etiketi üzerinden salt okunur inceleme yapılır. Canlı/belirsiz owner veya busy workspace Docker sorgusunu kapatır. Bu SQL session/run/model deployment bağı veya orphan cleanup yetkisi değildir; normal stop günlüğü silmez.

6s [salt okunur runtime incelemesi](RECOVERY_RUNTIME.md), yeni plain CLI hello environment snapshot'ında dizin kimliğini saklar; full container ID/image/runtime etiketi ve mevcut workspace ile eşleştirir. İki bağlı running gözlemi yalnız orphan adayıdır; process ölümü, session continuation veya cleanup yetkisi değildir. Eski kimliksiz kayıtlar dönüştürülmez.

Append-only migration `0004_desktop_control.sql`, desktop session/input/event kayıtlarını ekler. DB yeniden açılınca queued input cancelled, running input uncertain, aktif sahiplik PAUSED ve lease yenilenmiş olur. Eski input tekrar çalıştırılmaz.

6b-1 ekinde `--engine decider` ile hello görevi React UI'den yönetilir; migration 0005 görev/tek kullanımlık onay kayıtları sağlar. 6b-2 görev entegrasyonu ayrı headless browser/vision runtime'larını aynı kontrol lease'ine bağlar; migration 0006 eylem başına onayları genişletir. Model çağrısı iptali ve worker cleanup kontrol devrine bağlanmıştır. Genel görev veya mouse/keyboard model scheduler'ı değildir; [UI_RUNTIME](UI_RUNTIME.md) motor/DB/approval ve Pause'ın bu dilimde cancellation olması sınırlarını açıklar.

SIGKILL/daemon çökmesi gibi normal shutdown dışındaki durumlarda container kalabilir. Aynı workspace'in deterministik container adı ikinci kopyayı engeller; otomatik orphan adoption/deletion yapılmaz. Müdahale öncesinde container ID, `com.aos.runtime` etiketi ve yerel session kaydı eşleştirilmelidir. Bu katı fail-closed davranışıdır; otomatik crash recovery iddiası değildir.

Desktop kayıtları training dataset/export değildir. Ham VNC frame'leri server tarafından kaydedilmez; test ekran görüntüleri kaynak dışında tutulur. PDF/sentetik note ve browser/IDE profilleri restart'ta silinir; yalnız workspace kalıcıdır.

## Kabul komutları

```bash
AOS_DESKTOP_TESTS=1 AOS_BROWSER_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_package.py
```

Desktop opt-in testleri gerçek Docker image gerektirir; browser opt-in ayrıca gerçek noVNC render kontrolünü açar. Test modeli kullanılan hello testi açıkça fixture'dır; gerçek Decider koşusu ayrı STATUS kanıtıdır. LibreOffice PDF acceptance'ı sabit sentetik metnin gerçek PDF'ye çevrilmesi ve ayrı `pdftotext` eşitliğidir; modelin serbest Office kullanımı değildir.

Kabul kapsamı: lifecycle/mount/isolation, workspace kalıcılığı, uygulama pencereleri, PDF readback, gerçek X11 input, queued cancellation/stale lease, bağımsız verification, RFB view-only/human input, WebSocket auth/origin/size, normal shutdown cleanup ve render/controls. Son sayılar ve image/run kimlikleri [STATUS](STATUS.md) içindedir.

Resmi kaynaklar: [Docker none network](https://docs.docker.com/engine/network/drivers/none/), [noVNC RFB API](https://github.com/novnc/noVNC/blob/master/docs/API.md), [VSCodium release](https://github.com/VSCodium/vscodium/releases/tag/1.135.06055).
