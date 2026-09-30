# Güvenlik ve kullanıcı kontrolü

## Yetki sınırları

SAFE izole bilgisayarda düşük etkili işlemler; WORKSPACE açıkça mount edilmiş projelerde düzenleme/build/test; HOST gerçek makine yönetimidir. İlk autonomous runtime HOST aracı sunmaz. Host değişikliği gerekiyorsa ayrı, dar kapsamlı ve açık kullanıcı yetkisine bağlı yönetim akışı tasarlanır. Model risk tahmini güvenlik sınırı değildir.

Agent container'a host home, SSH keys, browser profile, Docker socket, host X11/Wayland socket veya privileged mod verilmez. Mümkün olduğunca non-root, drop capabilities, no-new-privileges, resource/process limits, sınırlı mount ve network kullanılır. Paket kurma build-time yapılır; sürekli agent root yetkisi gerektirmez. Container host kernel'ini paylaşır; VM kadar izolasyon iddia edilmez.

Shell bir string blacklist ile güvenli hale gelmez. Typed tool'lar, argv/cwd/env allowlist, timeout/output limit ve process sandbox tercih edilir. `shell=True`, nested shell, substitution, symlink/path traversal, destructive Git, `curl | bash`, disk araçları, privilege escalation, servis/ağ değişiklikleri ve silme davranışları ayrıca ele alınır. Workspace realpath kontrolü, mount sınırı ve race koruması gerekir. Kuralı çözümlenemeyen eylem fail-closed olur.

## Yetki ve onay

Kullanıcı görevi kapsamındaki geri alınabilir workspace işlemleri policy dahilinde yürütülebilir. Yıkıcı dış etki, gerçek hesap üzerinden mesaj/gönderim/yayınlama, hassas veri aktarımı veya privilege artışı için mevcut açık yetki yoksa insan onayı gerekir. Onay; action digest, scope, runtime, expiry ve one-use token'a bağlı olmalı, changed payload'a taşınmamalı. Approve/Reject kararları trajectory'ye kaydedilir. Önceden verilen geçerli yetki tekrar sorulmaz.

## Prompt injection ve servis yüzeyi

Web sayfası, repo metni, log ve model çıktıları untrusted veridir; yetki veya system policy değiştiremez. Browser/shell agent process'i host model servislerine ulaşmak zorunda değildir. API/noVNC yalnız loopback publish edilir, authentication gerekir; websocket origin ve request size sınırları uygulanır. CDP dışarı yayınlanmaz. Token'lar `.env` veya OS secret store'da tutulur, kayıt ve dataset'e girmez.

## Veri minimizasyonu

7e [yerel reviewer adaptörü](DATASET_REVIEWER.md), kernel Unix-socket UID'sini host-configured run/decision/expiry allowlist'ine bağlar; JSON/model çıktısı reviewer kimliği veya yetki veremez. Yalnız sentetik S1 ve exact-event explicit confirm desteklenir. 7f owner-only listener, terminal onayı ve fsync journal ekler. Aynı UID'deki kötü niyetli process veya socket FD transferi insan kimliği gibi ayırt edilemez; farklı account/process izolasyonu ayrıca kabul edilmelidir. Bu adaptör agent runtime'a socket/DB erişimi veya eğitim yetkisi açmaz.

Cookie, Authorization header, API key, parola, private key ve session token source'ta maskelenir. Raw screenshot/log da hassas veri olabilir. DB yerel erişim izinleriyle korunur; SQLite kendiliğinden şifreli değildir. Hassas workload için OS disk encryption veya ek encryption kararı gerekir. Secret tarayıcısı görsel redaction'ın yerini tutmaz; görüntüler ilk dataset sürümünden dışarıda tutulur. Kişisel içerik dışarı aktarılmaz.

İlk retention önerisi: raw 30 gün, redacted adaylar 90 gün; kullanıcı gereksinimi ve disk bütçesine göre ayarlanır. Dataset manifest artifact lineage saklar. Silme talebi raw/curated/dataset kopyalarında tombstone ve yeniden build gerektirir; eğitilmiş modelden bir satır silmek için yeniden eğitim/withdrawal değerlendirilir. Eğitim/corpus kullanım hakkı bilinmeyen veri eligible değildir.

Offline test ağ erişimi kapalı yerel fixture üzerinde yapılır; gerekli ilk bağımlılık ve model indirmesi hazırlık aşamasıdır. İsteğe bağlı remote training ayrı proje kararıdır; sanitized dataset bile açık aktarım yetkisi olmadan gönderilmez.
