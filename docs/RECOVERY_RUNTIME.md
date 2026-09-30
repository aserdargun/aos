# Docker runtime salt okunur incelemesi — 6s

## Kapsam

Yeni **plain CLI Docker hello** kayıtlarında `DesktopRuntime.status()` mevcut workspace dizin kimliğini environment snapshot'ına ekler. Kaydedilmiş run/state/deployment, full container ID, immutable image ID, `com.aos.runtime` etiketi ve workspace kimliği üzerinden salt okunur inceleme yapılır. Yeni SQL migration yoktur; eski kayıtlar geriye dönük doldurulmaz. Kimliği olmayan eski kayıt fail-closed reddedilir.

Bu dilim otomatik orphan detection/adoption/deletion değildir. 6s raporu process PID/start-time/boot kimliğini doğrulamadığı için **`orphan_confirmed=false` ve `process_liveness_verified=false`** kalır. 6t'nin ayrı [lifecycle günlüğü ve inceleyicisi](LIFECYCLE.md) yeni runtime'larda process birth bağını ekler; 6s SQL raporuna geriye dönük process yetkisi kazandırmaz. Container'ın çalışması, modelin veya görevin başarılı/sağlıklı olduğunu göstermez.

## Komut

```bash
.venv/bin/python -m aos.recovery_runtime \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --deployment-sha256 EXPECTED_DEPLOYMENT_SNAPSHOT_SHA256 \
  --image-id sha256:EXPECTED_IMMUTABLE_IMAGE_ID
```

Image ID hostun mevcut `models/desktop-manifest.json` pin'inden, deployment digest ise beklenen kayıt kimliğinden sağlanır; mutable tag veya model çıktısı yetki değildir. Model dosyası indirilmez/yüklenmez; deployment snapshot eşliği canlı model attestation'ı değildir. CLI TTY istemez, yeni DB/workspace oluşturmaz ve writer reconciliation çalıştırmaz.

| Sonuç | Yorum |
| --- | --- |
| `workspace_busy` | Başka bir descriptor kilidi var; Docker sorgulanmaz. Canlı process veya güvenli owner kanıtı değildir. |
| `running_candidate` | Workspace kilidi alınabildi ve bağlı container iki örneklemede running göründü. Yalnız olası orphan inceleme adayıdır. |
| `stopped_container` | Bağlı container created/exited gözlendi; silme/yeniden başlatma izni değildir. |
| `container_missing` | Exact full ID filtresi iki başarılı listede bulunamadı; önceki dış etkinin yokluğu anlamına gelmez. |

Daemon hatası, timeout, bozuk/çoklu çıktı, identity/mount/isolation uyuşmazlığı veya örneklemeler arasında değişim generic hata verir; bunlar `container_missing` diye sunulmaz. Belirsiz eylemler yalnız sayılır; running/uncertain action sonucu, dosya, verification, run status, lease veya approval değiştirilmez.

## Sınırlar

- Frozen canonical v7–v10 SQLite backup, state snapshot/hash ve sabit hello task scope kullanılır. Her backup mevcut 128 MiB/10 saniye sınırındadır. İnceleme sonrası ikinci backup hash'i aynı olmalıdır; SQLite SHM bookkeeping'i dışında DB/committed WAL içeriği değişmez.
- Workspace bileşenleri no-follow açılır; path hash/device/inode/owner UID kaydı mevcut dizinle eşleştirilir. Exclusive advisory lock inceleme boyunca tutulur, sonda dizin kimliği yeniden denetlenir. Aynı yol altında yeni inode veya symlink kabul edilmez.
- Yalnız sabit yerel Docker socket'i üzerinden **exact full ID filtreli `container ls` ve `container inspect`** çalışır. Shell, `exec`, `start`, `stop`, `rm`, adopt veya genel container taraması sunulmaz. Hostun WinBoat gibi ilgisiz container'ları sorgu kapsamına alınmaz/değiştirilmez.
- CLI process başına 10 saniye ve stdout+stderr toplam 64 KiB sınırı vardır; aşımda yalnız başlatılan okuyucu process sonlandırılır. Query env sabittir; raw stderr/inspect çıktısı kullanıcıya veya audit'e çıkarılmaz. Inspect formatı Config.Env, command, log, token veya container dosyalarını istemez.
- Live full ID/image/runtime etiketi, UID:GID, ağsızlık, read-only root, non-privileged, cap-drop/no-new-privileges, publish edilmemiş port ve tek `/workspace` bind bağı kontrol edilir. Ek volume/bind reddedilir; yalnız mevcut `/tmp`, `/run`, `/home/agent` tmpfs mount'ları kabul edilir. Bu alt küme tam güvenlik attestation'ı değildir.
- İki Docker örneklemesinin seçilmiş metadata'sı aynı olmalıdır; restart counter/start/finish zamanları da hash'e girer. Docker ve DB atomik değildir; gözlemden sonra durum değişebilir. Docker daemon/same UID/root ve advisory lock dışı müdahalelere karşı mutlak koruma iddia edilmez. Dizin inode reuse sınırı devam eder.
- Rapor yalnız hash referansları, image pin'i, disposition, belirsiz action sayısı ve zaman içerir. Execution/resume/cleanup/replay bayrakları daima false'tur. Workspace kimliği mevcut bounded exporter tarafından zaten çıkarılır; raw yerel inode/path metadata export edilmez.

**6s kapsamında desteklenmeyenler:** UI scheduler'a bağlı job/session lifecycle, browser/vision/sequence, Supervisor recovery probe, run kaydı oluşmadan container-create crash'i, process-owner liveness, otomatik cleanup/continuation. 6t, create öncesi ayrı günlüğü bulunan yeni runtime'ları SQL'den bağımsız inceler; UI session/run bağı kurmaz. Bunlar mevcut CLI raporundan türetilerek yetkilendirilemez. Persisted workspace kimliği bulunmayan eski kanıt DB'leri writer ile açılıp dönüştürülmez.

## Kabul

`tests/test_recovery_runtime.py`: frozen DB/WAL değişmezliği, busy/no-Docker, eksik/eski/tahrif edilmiş kimlik, inode/symlink, pinned image/deployment, label/user/network/mount uyuşmazlığı, örnekleme/DB race, daemon error, sınırlı subprocess output/timeout, CLI no-create, canonical şema ve yetki genişletme reddi.

`AOS_DESKTOP_TESTS=1` gerçek Docker/XFCE'de iki **fixture** subprocess crash açar: run kaydı commit sonrası ve gerçek dosya yazıldıktan fakat action sonucu commit edilmeden sonra `os._exit(78)`. Salt okunur inceleme running_candidate verir; DB running ve belirsiz action sonucu değişmez. Sonra **yalnız test sahibi**, kendi full-ID/etiket bağlı container'ını stop/rm eder; inspector ayrı ayrı stopped_container ve container_missing gözlemler. Bu test cleanup'ı ürün özelliği veya kullanıcı onayı değildir. Fixture karar gerçek Decider sonucu sayılmaz; model çağrısı yoktur. Kanıtlar ignored `data/runtime-crash-*`, ölçümler [STATUS](STATUS.md) içindedir.

Container oluşturma öncesi/sonrası boşluğu kapsayan kalıcı lifecycle kimliği ve host process birth/boot bağı [6t](LIFECYCLE.md) içinde uygulanır. Sonraki dar kabul UI session/runtime/lifecycle eşliğidir. Cleanup/promotion veya eski onayları canlandırma ayrı explicit yetki sözleşmesi olmadan eklenmez.
