# Yerel pilot önkoşul kontrolü

`local_preflight` yalnız mevcut host üzerindeki kurulu kaynakları salt okunur
denetler. Uygulamayı, container'ı, tarayıcıyı veya model sunucusunu başlatmaz;
indirme, kurulum, build, migration, eğitim ve promotion yapmaz.

```bash
.venv/bin/python -m aos.local_preflight
.venv/bin/python -m aos.local_preflight --fixture
```

API: `check_local(mode='real') -> dict`. `mode` yalnız `real` veya `fixture`.
`ready`, bütün zorunlu kontroller geçtiğinde true olur. `checks` dizisindeki
her kayıt `name`, `ok`, Türkçe `detail` ve başarısızlıkta önerilen `action`
alanlarını taşır. CLI JSON yazar; hazır olduğunda 0, eksikte 1 döner.
Ham hata çıktıları, token, özel dosya içerikleri ve trajectory sunulmaz.

## Kontroller

- `.venv/bin/python`: Python 3.11+ ve `pyproject.toml` içindeki ana,
  browser, desktop, dataset bağımlılıklarının exact sürümleri; kurulu AOS
  modülünün bu repository kaynağına işaret etmesi. Ayrı Python
  yalnız standart kütüphane metadata sorgusu için, offline/bytecode kapalı
  ortamda çalışır; executable symlink'i resolve edilerek virtualenv kaybedilmez.
- UI: mevcut `ui/dist/index.html`, girişte referans verilen yerel JS/CSS
  ve literal relative import ile yüklenen JS chunk varlığı; kaynak/lock/config
  dosyalarının build'den daha yeni olmaması.
  Bu mtime kontrolü build provenance veya kriptografik source-build bağı değildir.
- Desktop: non-root Linux, source dosya hashleri, immutable Docker image ID
  ve kaynak etiketi. Docker'a yalnız `image inspect` gönderilir; daemon
  başlatılmaz, mevcut container durdurulmaz veya sahiplenilmez.
- Browser: Bubblewrap executable/version erişimi, mevcut sabit Chromium
  revision/version ve bütün dosya hashleri. Browser process ve izolasyon
  handshake çalıştırılmaz; user namespace kullanılabilirliği kanıtlanmaz.
- MCP: yeni managed pilotun sabit iki sayfa görevi için private
  `models/desktop-mcp-v001/manifest.json` ve `packages.zip` arşivinin
  symlink olmaması, exact sürüm/bağımlılık ve içerik hash'i. Paket indirilmez,
  worker/Chromium başlatılmaz. Hata pilotu sessizce üç göreve düşürmez.
- Yalnız real: Decider worker'ın mevcut `verify_files` yordamıyla model/kod
  hashleri; `~/.venv/bin/python` metadata'sının manifest bağımlılıklarıyla
  tam eşleşmesi. Torch import edilmez, CUDA bağlamı/model yüklenmez.
- Yalnız real: `BonsaiSupervisor.verify_pins()` ile model/projector/runtime
  ve native dependency hashleri. `plan()` çağrılmaz, server başlatılmaz.

Dosya hashleri her çağrıda yeniden okunur; büyük yerel model/runtime
dosyaları nedeniyle birkaç saniye sürebilir. Rapor kalıcı izin değildir;
gerçek runtime kendi başlangıç pin/izolasyon kontrollerini yine uygular.
Başka bir process'in daha sonra dosyayı veya ortamı değiştirmesine karşı
atomik snapshot ya da kilit iddiası yoktur.

## Sınırlar ve test

Fixture gerçek Docker ve Chromium önkoşullarını yine gerektirir, yalnız
gerçek model doğrulamasını atlar; gerçek inference başarısı değildir.
Her modda `read_only=true`, `inference_verified=false` ve
`resource_admission_verified=false` kalır. GPU boş belleği, birlikte model
residency, görev başarısı, Wayland veya temiz makine installer kabulü bu
rapordan çıkarsanamaz. Pilot, mevcut hostta tarayıcı konsolu ile denenir;
genel masaüstü ajanı veya eğitime hazır ürün ilan edilmez.

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_local_preflight.py -v
```

Testler sentetik geçici dosyalarla pin farkı, eksik/stale UI, symlink,
metadata farkı, read-only command allowlist, fixture ayrımı ve hata
çıktısı minimizasyonunu sınar. Gerçek host sonuçları [STATUS](STATUS.md)
içinde ayrı tutulur.
