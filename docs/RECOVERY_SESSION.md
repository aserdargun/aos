# UI session/runtime/lifecycle incelemesi (6u)

## Uygulanan sınır

Yeni gerçek `DesktopController` oturumu, durable `started` lifecycle journal'ını
session kaydıyla aynı SQLite transaction'ında `desktop_events.kind=runtime_binding`
olarak bağlar. Payload `SessionRuntimeBinding`: session ID, başlangıç generation,
immutable `LifecycleBirth` ve full container ID. Journal'ın durumu, pinleri,
runtime/container kimliği ve workspace descriptor kimliği eşleşmeden gerçek
oturum açılmaz. Fixture runtime bu kabulü taklit etmez; bağsız kalır.

Explicit restart yeni birth üretir. Yeni binding ve session runtime güncellemesi
aynı SQL transaction'ındadır; önceki kayıt silinmez. Pause/takeover/stop mevcut
birth'i değiştirmez. Container restart ile SQL commit arasındaki crash atomik
değildir: yeni journal eski session'a uymadığı için inceleme reddedilir. Eksik
bağ otomatik onarılmaz, eski DB'lere backfill yapılmaz. Mevcut 0001–0008 SQL
migration'ları değişmez; yeni event payload'ı mevcut JSON sütununu kullanır.

## Salt okunur CLI

```bash
.venv/bin/python -m aos.recovery_session \
  --database data/SESSION/store.sqlite \
  --session-id desktop-session-SESSION_UUID_HEX \
  --journal data/SESSION/.aos-lifecycle/desktop-RUNTIME_UUID_HEX.jsonl \
  --workspace data/SESSION/workspace \
  --image-id sha256:PINNED_IMAGE_ID \
  --source-sha256 PINNED_SOURCE_HASH
```

`SESSION_UUID_HEX` ve `RUNTIME_UUID_HEX` gerçek kayıtlardaki 32 hex kimliktir;
image/source pinleri host tarafından verilen tam değerlerdir. Komut eksik
dosyaları oluşturmaz, writer veya reconciliation açmaz. Veritabanı committed WAL
dahil bounded frozen in-memory snapshot olarak okunur; schema/migration/FK ve
integrity mevcut audit kurallarıyla denetlenir.

Session'ın güncel runtime/image kimliği, en son binding payload'ı, artan binding
generation'ları ve tekil runtime doğumları denetlenir. Journal'ın birth/container
bağı exact olmalıdır. Eski journal yeni restart oturumunu temsil etmez. En fazla
10000 binding/job kabul edilir. SQL snapshot ve journal hash'i inceleme sonunda
yeniden karşılaştırılır; değişiklik gözlenirse sonuç verilmez.

Container/process/workspace gözlemi [6t lifecycle incelemesini](LIFECYCLE.md)
kullanır: canlı veya belirsiz owner ya da busy workspace Docker sorgusunu kapatır.
Diğer uygun durumlarda yalnız exact identity bağlı iki read-only container
örneklemesi yapılır. Bunlar sürekli canlılık veya kesin orphan kanıtı değildir.

## İsteğe bağlı exact hello job/run bağı

İki ek argüman birlikte verilmelidir:

```bash
  --job-id job-JOB_UUID_HEX --deployment-sha256 HOST_PINNED_DEPLOYMENT_HASH
```

Yalnız aynı session ve aynı Docker birth altında oluşmuş `hello` job kabul
edilir. Job runtime/generation, run/state/task/current state snapshot,
workspace/container/image/lifecycle environment ve host-pinned deployment
snapshot hash'i bağlanır. Scope, hello içeriği ve bağımsız başarı ölçütü mevcut
sabit sözleşmeyle eşleşmelidir. Browser/vision job'ları başka headless execution
runtime kullanır ve bu seçenek tarafından reddedilir; kuyrukta olup run'ı henüz
olmayan işler veya eski bağsız run'lar da reddedilir. Tüm job'lar otomatik
doğrulanmaz: report'taki `job_count` yalnız session toplamıdır; `job=null` hiçbir
job/deployment doğrulaması yapılmadığı anlamına gelir.

Bu metadata/deployment snapshot eşliği modelin yeniden çalıştırıldığını,
ağırlıkların yeniden hash'lendiğini veya görev sonucunun yeniden doğrulandığını
iddia etmez. Nested lifecycle `model_deployment_verified=false` kalır.

## Çıktı ve yetki

`schemas/recovery_session.schema.json` yalnız hash referansları, sabit enum,
sayaç, generation ve minimize lifecycle raporu içerir. Ham session/job/run/runtime
kimliği, container ID, host yolu, PID/boot kimliği, lease veya approval envelope
çıktıya konmaz. `schemas/session_runtime_binding.schema.json` kalıcı private SQL
payload sözleşmesidir; kaynak örneği `examples/recovery_session.json` açıkça
sentetiktir. Kimlik/hash bütünlüğü host hesabına karşı kriptografik imza değildir.

`execution_authorized`, `resume_authorized`, `cleanup_authorized`,
`automatic_replay_allowed`, `lease_restored`, `approval_restored` daima false.
Pending approval ve unresolved action/input kayıtları değiştirilmez; eski onay,
lease, worker veya task sequence canlandırılmaz. Cleanup, kesin orphan kararı ve
Docker/browser/vision checkpoint continuation ayrı kabul gerektirir.

## Authenticated API ve UI

`GET /api/session/binding` yalnız mevcut controller oturumunu inceler. Database
hostun gerçek main SQLite bağlantısından, workspace/journal/image/source mevcut
runtime'dan türetilir; istemci path, session, job veya deployment seçemez. Query
parametreleri reddedilir. Fixture/legacy/eksik bağ generic 409 üretir; ham hata
veya host yolu response'a konmaz. Cookie/Host sınırı diğer console API'leriyle
aynıdır. Tek eşzamanlı inceleme sınırı 429 üretir. Bounded snapshot incelemesi
worker thread'de yapılır; kontrol kilidi boyunca tutulmaz. Son controller
state/runtime tekrar kontrolü pause/restart gibi yarışları reddeder.

React Kurtarma panelinin altındaki **Oturum ve runtime bağı** yalnız explicit
düğmeyle okur. Session-only rapor görüntülenir, isteğe bağlı CLI job/deployment
incelemesi bu API'de açılmaz. Runtime veya control generation değişince sonuç
temizlenir; unmount/değişiklik bekleyen isteği iptal eder. Kalıcı kontrol çubuğu
kullanılabilir kalır; panelden resume/cleanup çağrısı yapılamaz.

## Doğrulama

`tests/test_recovery_session.py` SQL/event transaction failure, restart boşluğu,
legacy binding, exact job/state/environment/deployment, journal/DB race,
workspace replacement, minimize çıktı, canonical schema ve yetki redlerini sınar.
`AOS_DESKTOP_TESTS=1` gerçek Docker subprocess testini açar: session commit,
hello pending approval ve yeni restart birth / eski SQL binding aralıklarında
process kesilir. Karar motoru fixture'dır; model kabulü veya insan onayı değildir.
Kaynak DB/WAL/journal byte hash'leri aynı kalmalı, hello dosyası oluşmamalıdır.
Cleanup yalnız test sahibinin full-ID/runtime etiketli container'larına aittir.
Gerçek çalıştırma sonucu ve ortam kanıtı [STATUS](STATUS.md) içinde tutulur.
`tests/test_parallel_ui.py` authenticated no-query API, yanlış kaynak/redaction,
eşzamanlı inceleme ve kontrol yarışını; gerçek Docker/Chromium'da explicit okuma,
pause/restart sonrası eski sonucun temizlenmesi ve desktop/mobile görünümü sınar.
Bu Chromium kabulüdür; yeni panellerin native/Wayland kabulü değildir.
