# Kalıcı Docker doğum kaydı — 6t

## Uygulanan kapsam

`DesktopRuntime`, Docker `create` çağrısından **önce** private, append-only bir doğum günlüğünü kalıcılaştırır. Böylece SQL run/session kaydı henüz oluşmamışken veya Docker oluşturma yanıtı kaybolmuşken de bağlı runtime salt okunur incelenebilir. Bu günlük yeni execution, adoption, cleanup veya crash continuation yetkisi değildir; SQL trajectory, bağımsız görev doğrulaması ve model deployment kimliğinin yerine geçmez.

Günlük workspace'in içinde değil, `workspace.parent/.aos-lifecycle/<runtime_id>.jsonl` konumundadır; container'a mount edilmez. Dizin mevcut kullanıcıya ait **0700**, dosya **0600** olmalıdır. Dosya exclusive-create/no-follow açılır; mevcut runtime kaydı üzerine yazılmaz. `DesktopRuntime`, günlük/create öncesinde workspace'i ve `REPO_ROOT` dahil bütün üst dizinlerini no-follow descriptor'larla açıp sırayla `fsync` eder; yeni iç içe parent dizinlerin directory entry'leri de bu kalıcılık zincirine dahildir. Günlük oluşturulurken dosya ve ilgili dizinler ayrıca `fsync` edilir. `.aos-lifecycle` önceden mevcut olsa veya önceki başarısız denemeden kalsa bile parent fsync atlanmaz. Bu senkronizasyonlardan biri veya ilk `intent` kalıcılaştırması başarısızsa Docker `create` çağrılmaz.

Doğum kimliği runtime ID, deterministik exact container adı, immutable image ID, source hash, workspace path hash/device/inode/owner UID ile Linux **boot ID, PID, process start ticks, PID namespace inode ve UID** bağını içerir. `com.aos.lifecycle` etiketi bu typed doğum kimliğinin digest'idir; mevcut `com.aos.runtime` etiketi korunur. Ham kimlikler private günlükte kalır, rapora hash referansları çıkar.

| Kalıcı olay | Anlamı |
| --- | --- |
| `intent` | Oluşturma öncesi doğum kimliği; full container ID henüz yoktur. |
| `created` | `create` yanıtındaki full ID doğrulanıp kaydedilmiştir. |
| `started` | `start` yanıtı alınmıştır; masaüstü readiness veya görev başarısı değildir. |
| `removed` | Mevcut owned runtime'ın normal kaldırma işlemi tamamlanmıştır; yeni cleanup yetkisi değildir. |

Her olay değişmeyen doğum kimliği, sıra numarası ve önceki olayın hash'ini taşır; append sonrası `fsync` yapılır. İzinli zincir `intent → created → started → removed` veya başlangıç başarısızlığında `intent → created → removed` olur. Yazma/fsync hatası aynı writer'ın sonraki append işlemlerini kapatır. Normal stop günlüğü silmez; restart yeni runtime ID ve ayrı günlük üretir.

## Salt okunur komut

```bash
.venv/bin/python -m aos.recovery_lifecycle \
  --journal data/.aos-lifecycle/desktop-RUNTIME_ID.jsonl \
  --workspace data/example-workspace \
  --image-id sha256:EXPECTED_IMMUTABLE_IMAGE_ID \
  --source-sha256 EXPECTED_DESKTOP_SOURCE_SHA256
```

Image/source pin'leri hostun beklenen `models/desktop-manifest.json` kaydından sağlanır; mutable tag veya model çıktısı yetki değildir. Komut TTY, model ya da SQL writer açmaz; eksik günlük/workspace oluşturmaz. `schemas/lifecycle_event.schema.json` ve `schemas/lifecycle_inspection.schema.json` canonical sözleşmelerdir; `examples/lifecycle.json` açıkça sentetiktir.

İnceleme önce private dosya/zincir/pin ve mevcut workspace kimliğini doğrular. Process karşılaştırması Linux `/proc` ve `pidfd` üzerinden yapılır; tek başına aynı PID yeterli değildir. Workspace exclusive advisory lock'u alınamıyorsa veya owner `same_process`, `incomparable` ya da `unavailable` ise **Docker sorgulanmaz**. Farklı boot/process veya artık gözlenmeyen process, güvenli cleanup kanıtı sayılmaz.

| Rapor alanı | Sınır |
| --- | --- |
| `owner_observation` | `same_process`, `not_observed`, `different_process`, `different_boot`, `incomparable` veya `unavailable`; gözlem anına aittir. |
| `workspace_busy` | Kilit başka bir descriptor tarafından tutuluyor; process-owner eşliğini tek başına kanıtlamaz. |
| `container_observation` | `not_queried`, `missing`, `created`, `running` veya `exited`; görev sonucunu anlatmaz. |
| Yetki alanları | `orphan_confirmed`, `model_deployment_verified`, execution/resume/cleanup/replay daima `false` kalır. |

Yalnız `intent` varsa exact deterministik container adıyla arama yapılır; bulunan tek full ID'nin image/runtime/workspace/izolasyon bağları ve **exact birth etiketi** doğrulanır. Böylece create öncesi kesinti ile create tamamlanıp yanıtın kaybolduğu kesinti ayrılabilir; aynı adlı yabancı container benimsenmez. `created` kalıcıysa arama artık yalnız kaydedilmiş **full ID** üzerinden yapılır. İki Docker örneklemesi eşleşmeli; owner, günlük ve workspace kimliği sonda değişmemiş olmalıdır. `removed` kaydına rağmen bağlı container bulunması hata verir.

## Güvenlik ve atomiklik sınırları

- Günlük en fazla **4 satır / 16 KiB** kabul eder. Son newline'ı eksik kayıt, bozuk/tekrarlı/sırası değişmiş zincir, değişen dosya, symlink/hardlink, yanlış sahiplik veya geniş izinler fail-closed reddedilir; kısmi satır otomatik onarılmaz.
- Salt okunur Docker erişimi 6s okuyucusunun sabit yerel socket, bounded timeout/output ve dar inspect formatını kullanır. Genel container taraması, `exec`, start/stop/rm veya adopt yoktur. Daemon hatası/timeout `missing` değildir.
- Günlük ile Docker daemon arasında atomik transaction yoktur. `intent`, oluşturmanın olmadığı anlamına gelmez; `created`, start'ın gerçekleşmediği anlamına gelmez. `started` de mevcut container veya başarılı görev garantisi değildir. `removed` append öncesi kesintide önceki olay kalabilir.
- Gerçek subprocess kesintileri test kapsamındadır; fiziksel makinenin güç kaybı testi yapılmamıştır. `fsync` kalıcılığı filesystem ve depolama aygıtının sağladığı garantilere bağlıdır; donanım güç kesintisi dayanıklılığı kabulü iddia edilmez.
- Günlük hash zinciri imza ya da same-UID/root saldırısına karşı değiştirilemez kayıt değildir. Docker daemon, host `/proc`, filesystem ve advisory-lock dışı müdahalelere güven sınırı devam eder. Inode reuse ve gözlem sonrası değişim mümkündür; `orphan_confirmed=false` bilinçli olarak korunur.
- Günlük SQL run/session/deployment bağı kurmaz; status içindeki `lifecycle_ref` tek başına böyle bir bağı kanıtlamaz. Eski günlük/kimliksiz runtime'lar geriye dönük doldurulmaz. Lease/onay yenilenmez, belirsiz action çözülmez, model/gateway çağrılmaz, eğitim verisi/izni üretilmez.

## Kabul ve sonraki sınır

`tests/test_lifecycle.py` private izinler, append/hash sırası, short-write/fsync failure, kısmi/bozuk kayıt, symlink/hardlink, canlı/belirsiz owner, gerçek child-process çıkışı, PID/boot/namespace ayrımı, workspace/pin/race kontrolleri, no-create CLI, canonical fixture ve yetki genişletme reddi için testler içerir. Dosya fsync hatası, önceden mevcut günlük dizininde tekrarlanan parent fsync hatası ve yeni iç içe workspace üst dizininin fsync hatası için container oluşturulmaması ayrıca sınanır.

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_lifecycle.py' -v
AOS_DESKTOP_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -p 'test_lifecycle.py' -v
```

Opt-in testler pinned gerçek Docker image ile create öncesi, create yanıtı sonrası, `created` kaydı sonrası, start yanıtı sonrası, `started` kaydı sonrası ve `removed` kaydı sonrası gerçek `os._exit(79)` kesintilerini kapsar. SQL run veya model eylemi gerektirmez; **gerçek Docker kabulü, gerçek Decider/Bonsai kabulü değildir**. Test sahibi yalnız kendi runtime etiketi bağlı full-ID container'ını temizler; bu ürünün cleanup özelliği değildir. Gerçek yürütme sonuçları, kalan uyarılar ve private kanıt konumları [STATUS](STATUS.md) içinde tutulur; test varlığı tek başına kabul sonucu sayılmaz.

Sonraki dar milestone **UI session/runtime/lifecycle eşliğini salt okunur doğrulamak** ve eksik bağları görünür tutmaktır. Otomatik deletion/adoption, eski lease/onay canlandırma, Docker/browser/vision/sequence continuation ve genel scheduler ayrı sözleşme ve kabul gerektirir; 6t tüm ürünün tamamlandığı anlamına gelmez.
