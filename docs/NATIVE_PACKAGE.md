# Yerel Native Kabuk Paketi

## Uygulanan kapsam

`scripts/package_native.py`, **önceden derlenmiş** Tauri kabuğunu private, deterministik bir tar arşivine alır ve açıkça verilen dış SHA-256 ile salt okunur doğrular. Yeni build, uygulama çalıştırma, extraction, kurulum, servis, indirme veya promotion yapmaz. Bu bir AppImage/deb/rpm, imzalı release, updater ya da bağımsız inference dağıtımı değildir.

Yalnız repository içindeki `ui/src-tauri/target/{debug|release}/aos-console` okunur. Profil açıkça seçilir; release binary yoksa debug'a fallback yapılmaz. Linux x86-64 ELF başlığı kontrol edilir; bu kontrol binary'nin güvenilir veya çalışabilir olduğunu kanıtlamaz. Synthetic unit test binary'leri minimal başlık fixture'ıdır, çalıştırılabilir uygulama veya gerçek model sonucu diye sunulmaz.

Arşiv tam iki regular üyeden oluşur: `manifest.json` ve `aos-console`. Manifest binary SHA-256/boyut/profil, dört allowlisted Tauri kaynak/config dosyasının hash'leri ve Cargo/pnpm/uv lock hash'lerini içerir. Kaynak, lockfile, frontend build, backend, Python ortamı, sistem kütüphaneleri, Docker image, model/adapter, workspace, token, DB, trajectory veya screenshot arşive kopyalanmaz. Binary içinde zaten derlenmiş byte'ların özel veri içermediğine dair genel içerik taraması yapılmaz; yalnız explicit mevcut build artifact'ı paketlenir ve dışarı gönderilmez.

`source_scope=tauri_shell_configuration_only` mevcut kaynak/config hash envanteridir; binary'nin bu kaynaklardan derlendiğinin attestation'ı değildir. Eski binary ile güncel kaynak farklı olabilir. Bu nedenle **`build_provenance_verified=false` her zaman korunur**. Lock hash'leri derleme veya kurulu sistem bağımlılıklarının doğrulandığı anlamına gelmez. `standalone_inference`, `installation_authorized` ve `promotion_authorized` da false kalır. Canonical sözleşme `schemas/native_package.schema.json`, açık sentetik örnek `examples/native_package.json` içindedir.

## Komutlar

Mevcut repository `.venv` ortamında, repo kökünden:

```bash
.venv/bin/python scripts/package_native.py create \
  --profile debug --output runs/native-shell-debug-v001
```

Çıktı private `runs/native-shell-debug-v001/bundle.tar` ve minimize JSON özetidir. Hedef yalnız `runs/` altında tek yeni dizin olabilir; dizin 0700, dosya 0600 olur. Mevcut hedef üzerine yazılmaz. Fsync hatası başarısızlık sayılır; kalan kısmi hedef otomatik silinmez veya tekrar kullanılmaz. Yeni deneme yeni açık hedef gerektirir.

Oluşturma sonucundaki `archive_sha256` değerini ayrı güvenilir kayıt olarak koruyup doğrulayın:

```bash
.venv/bin/python scripts/package_native.py verify \
  --package runs/native-shell-debug-v001 --sha256 SAVED_ARCHIVE_SHA256
```

Placeholder yerine gerçek 64 karakterli küçük-harf hash verilmelidir. Hash'i doğrulama anında güvenilmeyen arşivden yeniden hesaplamak özgünlüğü doğrulamaz. İmza veya güvenilir dağıtım kanalı yoktur; dış hash güven sınırıdır. Verifier dosya oluşturmaz/değiştirmez, extraction yapmaz, ELF'i veya `ldd` çalıştırmaz.

Private dizin/dosya izinleri, sahiplik, no-follow path bileşenleri, archive hardlink, bounded boyut, dış hash, tam üye kümesi/sırası, içerik hash'i, manifest pin/authority sözleşmesi ve canonical tar byte'ları doğrulanır. Tar path traversal, link/device üyeleri, duplicate/extra üyeler, sonuna eklenen byte'lar ve yeniden hash'lenmiş sahte authority/provenance reddedilir. Mevcut Cargo binary hardlink'leri input olarak kabul edilir; çıktının hardlink'i kabul edilmez. Hash bütünlüğü uygulamaya execution/cleanup veya deployment yetkisi vermez; aynı host UID'sine karşı genel izolasyon iddia edilmez.

## Çalışma önkoşulları ve açık işler

- Native kabuk mevcut sabit `http://127.0.0.1:8765/ui/` backend adresine bağlanır. Backend/React build, authentication token ve başlatma ayrı [UI_RUNTIME](UI_RUNTIME.md) akışıdır; paket bunları başlatmaz veya taşımaz.
- CachyOS/Linux x86-64, uygun dinamik loader/libc, GTK 3, WebKitGTK 4.1, JavaScriptCoreGTK, libsoup 3 ve transitif sistem kütüphaneleri gerekir. Kurulu binary'nin doğrudan kütüphane adları salt okunur `readelf -d` ile incelenmiştir; transitif sürüm/ABI çözümlemesi, başka makine ve temiz OS kabulü yapılmadı.
- Mevcut native kabul profili `GDK_BACKEND=x11 WEBKIT_DISABLE_DMABUF_RENDERER=1` kullanır. Paket ortam değişkenlerini veya host ayarlarını değiştirmez. Default Wayland ve donanım hızlandırma kabulü değildir.
- Gerçek görevler için ayrıca pinned Docker image, açık workspace, Python backend ve ayrı mevcut Decider/Bonsai ortamı/model artifact'ları gerekir. GPU, eğitim, model lisansı veya deployment promotion bu paketle sağlanmaz.
- Byte-identical paket üretimi aynı binary ve aynı allowlisted hash girdileri için doğrulanır; reproducible compiler build, supply-chain attestation, release rebuild, imzalama, kurulabilir OS paketi ve temiz hedefte çalışma ayrı milestone'lardır.

## Doğrulama

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s tests -p test_native_package.py -v
```

Unit testler synthetic artifact'larla deterministik/private çıktı, no-overwrite, ELF/profil, source/output symlink, traversal, hardlink, tam üye kümesi, hash/tamper, sahte provenance/authority, fsync failure ve CLI no-create sınırlarını doğrular. Mevcut gerçek Tauri debug artifact'ının local archive/verify kabulü unit fixture'dan ayrıdır; yeni uygulama veya model çalıştırıldığı anlamına gelmez. Güncel ölçüm ve artifact kimlikleri [STATUS](STATUS.md) içinde tutulur.
