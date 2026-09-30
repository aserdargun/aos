# Çalışma ortamı

## Üç ortam

1. **CachyOS host:** NVIDIA/CUDA, native Bonsai ve Decider servisleri, uv içindeki Python backend, SQLite, registry, benchmark ve Tauri development.
2. **Ubuntu Agent Computer:** Docker/Compose, XFCE, sanal X11 display, noVNC, Chromium, Terminal, VSCodium, LibreOffice, Thunar, PDF viewer, Git/Python/Node/pnpm. Başlangıçta GPU passthrough gerekmez.
3. **Workspace:** yalnız açıkça seçilen klasörler `/workspace` altında; read-only veya read-write izinli mount.

İlk paket macOS üzerinde hazırlanmıştır. CachyOS/RTX 4070 Ti SUPER ortamında native Decider hello, pinned PrismML CUDA Bonsai recovery ve tek sentetik canvas vision kabulü geçti. Docker Agent Computer lifecycle, XFCE/noVNC ve dar kontrol konsolu ayrıca eklendi; [DESKTOP_RUNTIME](DESKTOP_RUNTIME.md) sınırları tanımlar. Sürümler/kanıtlar STATUS içindedir. Bonsai her çağrıda authenticated loopback server process'i olarak açılıp kapatılır; Decider sonra yüklenir.

İlk implementasyon `WorkspaceRuntime` kullanır: yalnız yetkilendirilmiş hello dosyasına descriptor tabanlı erişim, ayrı SQLite writer kilidi ve workspace kilidi vardır. Genel amaçlı host shell sunmaz. Tek process aracı Bubblewrap içinde ağsız, salt okunur workspace üzerinde sabit `sha256sum` argv'sidir. `DesktopRuntime` alternatifinde aynı hello filesystem gateway'i ağsız pinned Ubuntu container'ında çalışır; checksum descriptor üzerinden container içinde hesaplanır. İki backend de VM olarak sunulmaz.

## Başlangıç servis planı

Uygulanan browser dilimi [BROWSER_RUNTIME](BROWSER_RUNTIME.md) içindeki ağsız Bubblewrap + Playwright/Chromium worker'ıdır. Host home/profile/socket bağlanmaz; CDP pipe kullanır, port açmaz. Yalnız sentetik yerel form desteklenir. Docker desktop ayrı backend'dir; aşağıdaki geniş servis topolojisinin tamamı hâlâ tasarım hedefidir.

| Servis | Yer | Önerilen adres | Not |
|---|---|---|---|
| Orchestrator | host | 127.0.0.1:8000 | FastAPI + authenticated local UI |
| Bonsai | host | 127.0.0.1:8001 | uyumlu PrismML llama.cpp build |
| Decider | host | 127.0.0.1:8002 | PyTorch/CUDA, systemone adaptörü |
| Computer Gateway | container | host publish 127.0.0.1:8003 | container network içinde erişim; ayrı token |
| noVNC | container | host publish 127.0.0.1:6080 | authenticated websocket/view |
| Chromium CDP | container | container içi 9222 | host/public publish yok |

Portlar öneridir; Codex mevcut listener'ları inceleyip çakışmayı çözmeli, başka projelerin süreçlerini kapatmamalıdır. Container'daki `localhost` host değildir. Gateway container içinde dinleyebilir ancak host publish yalnız loopback olmalı; model servislerini container'a açmak gerekmez, orchestrator çağırır.

## İlk ortam keşfi

Kernel/distro, NVIDIA driver, CUDA kullanımı, GPU boş/tepe bellek, RAM/disk, compiler/CMake, Python/uv, Docker/Compose, Node/pnpm, Rust/cargo sürümlerini kaydet. Mevcut llama.cpp checkout ve build revision'ını, Bonsai language/mmproj dosyalarını, Decider cache ve checkpoint revision'ını bul. Hash'leri hesapla. Hedef donanım yoksa model testini blocked olarak kaydet; tahmini sonuç üretme.

Sistem Python'unu değiştirme; uv ve proje virtualenv kullan. Decider bağımlılıkları ile orchestrator bağımlılıkları çakışırsa ayrı virtualenv/service tut. Uygulama lockfile'ları uygulama aşamasında doğrulanmış sürümlerden üretilecek. Node için pnpm, Rust için rustup/cargo kullan.

## Kaynak bütçesi ve shutdown

İlk [vision runtime](VISION_RUNTIME.md) aynı ağsız Chromium worker sınırında canvas capture ve atomic freshness-check/sentetik click dispatch uygular. Screenshot yalnız yerel ignored artifact'tir; Bonsai native projector ile işlenir ve server kapandıktan sonra Decider yüklenir. Docker desktop/OS input lifecycle bununla doğrulanmış sayılmaz.

Başlangıç Bonsai context hedefi 16384 token; eşzamanlılık 1. Language weights, projector, KV/cache, workspace, aktivasyonlar, Decider, CUDA graph ve UI yüklerini ayrı ölç. Resident iki model yalnız measured headroom uygunsa kabul edilir. OOM'de pending action yürütülmez; context/batch azaltma veya serialize/offload/reload denenir. CPU fallback açıkça raporlanır. Eğitim için inference servislerini controlled drain ile boşalt; 27B eğitimini 16 GB kartta garanti etme.

Shutdown: yeni görevleri durdur → aktif tool'u iptal et veya bounded completion bekle → sonuç belirsizse uncertain kaydet → WAL checkpoint/DB close → gateway/runtime stop. Restart sırasında in-flight dış etki yeniden körlemesine oynatılmaz; reconcile ve gerekiyorsa insan incelemesi gerekir.

## Container image ve tekrar üretilebilirlik

Ubuntu 24.04 LTS ilk uyumluluk tabanı önerisidir; konuşmada 24.04/26.04 seçenekleri vardı. Exact image digest ve paket sürümleri build sırasında sabitlenecek. Dockerfile/Compose bu pakette çalışırmış gibi verilmemiştir. İlk implementasyonun smoke test'i XFCE/noVNC/Chromium/Terminal/LibreOffice açılışını, gateway health'i, mount izinlerini ve restart'ı doğrulamalıdır. Docker VM güvenlik sınırı değildir; yüksek riskli işlerde KVM/QEMU sonraki seçenek olarak korunur.
