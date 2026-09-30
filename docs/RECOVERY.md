# Kurtarma envanteri ve private yedek — 6f–6h

**6n ek dilim:** [sıfır eylemli hello devamı](RECOVERY_RESUME.md) ayrı explicit CLI'dir; mevcut envanter/backup/UI davranışını değiştirmez. Belirsiz veya herhangi bir eski action bulunan run'ı sürdürmez.

**6m ek dilim:** yeni descriptor workspace hello kayıtları için [checkpoint etki incelemesi](RECOVERY_CHECKPOINT.md). Bu CLI dizin/state/deployment-snapshot bağını ve mevcut dosya byte'larını denetler; envanter/API/UI davranışını değiştirmez, belirsiz eylemi çözmez veya yeniden yürütmez.

## Envanter: mevcut kayıt, canlılık kanıtı değil

`aos.recovery`, mevcut v7–v12 DB'nin committed WAL dahil frozen in-memory kopyasını kullanır. Kaynakta migration, writer reconciliation, lease/approval değişimi veya eylem tekrarı yapmaz. Schema/migration/FK/integrity ve 128 MiB/10 saniye sınırları mevcut audit snapshot yordamıyla ortaktır. Oturum, job ve run sayısı ayrı ayrı en fazla 10000 olabilir. Eksik/bozuk/destek dışı kaynak başarı raporu üretmez.

```bash
.venv/bin/python -m aos.recovery --database data/desktop-console.sqlite --limit 30
```

- Tüm DB için intent/running/uncertain eylemler, pending/approved onaylar, sonuçlanmamış masaüstü girdileri ve terminal olmayan run/job sayaçları verir. Scheduler'a bağlı olmayan CLI run'ları da global sayaçlarda görünür.
- Job listesi inceleme gerektirenler önce, her grupta en yeni kayıtlar sırasındadır. Limit 1–100, varsayılan 30; truncation açıktır ve sayaçlar gösterilmeyen kayıtları da kapsar.
- Job/run/session kimlikleri SHA-256 referanslarıdır; goal/state, action arguments, approval envelope/digest/lease, DB yolu veya artifact içeriği çıkmaz. Hash'ler anonimleştirme garantisi değildir.
- `requires_inspection`, terminal olmayan/uyuşmayan kayıt veya belirsiz etki bulunduğunu söyler; **çökme, ölü process veya canlı runtime tespiti değildir**. Aktif sağlıklı bir görev de bu kayıtları taşır. Terminal job status'u bağımsız başarı doğrulamasının yerine geçmez.
- `execution_authorized`, `resume_authorized`, `automatic_replay_allowed`, `live_state_verified` daima false. Runtime/worker/container sorgulanmaz veya sahiplenilmez. Kaynak main DB/WAL içeriklerinin değişmezliği testlidir; SQLite SHM koordinasyonu nedeniyle bütün filesystem metadata'sı için değişmezlik iddia edilmez.

### API ve UI

Authenticated `GET /api/recovery` yalnız server-configured **control DB**'yi okur; konsol motoru kapalıyken ayrı `--trajectory-database` trace kaynağıyla karıştırılmaz. `create_console(..., recovery_database=...)` explicit kaynaktır. Query/body ile path/limit/resume seçilemez; query 400, yazıcı HTTP yöntemi 405, eksik/geçersiz kaynak generic 503 verir. Cookie/Host sınırı korunur. GET yazma etkisi olmadığı için POST Origin onayı yerine mevcut GET auth sınırını kullanır.

Snapshot işi event loop dışında tek worker ile yapılır; eşzamanlı ikinci okuma 429 alır. HTTP isteği iptal edilse bile worker bitene kadar gate korunur. Diğer kontrol endpoint'leri okuma bitmesini beklemez. Her backend instance'ın kendi gate'i vardır; bu process'ler arası global kota değildir.

**Kurtarma** paneli “Envanteri oku” düğmesiyle explicit snapshot alır; pahalı otomatik polling yoktur. Yeni okuma eski raporu kaldırır, panel değişimi/logout isteği iptal eder. Kalıcı kontrol çubuğu etkin kalır. Listede resume/replay/approve düğmesi bulunmaz. Kullanıcı kontrol devrinden sonra yeni snapshot okuyarak revoked onay/cancelled job durumunu görebilir. Bu panel önceki job'ı sürdürmez veya yeni job başlatmaz.

## Yedekleme: tam DB, gizli yerel veri

`aos.recovery_backup` yalnız CLI'dir; raw DB indirme veya restore API'si açılmaz. Backup raw trajectory/receipt/token içeriği taşıyabilir; **redaction/export/eğitim datası değildir ve şifrelenmez**. Source manifest/Git'e alınmaz, upload edilmez. Output/destination parent'ı mevcut kullanıcıya ait 0700 olmalıdır; backup dizini 0700, dosyalar 0600 olur. Symlink/hardlink ve permissive dosyalar reddedilir.

```bash
mkdir -m 700 data/private-backups
.venv/bin/python -m aos.recovery_backup create --database data/desktop-console.sqlite --output data/private-backups/session-v1
.venv/bin/python -m aos.recovery_backup verify --backup data/private-backups/session-v1
.venv/bin/python -m aos.recovery_backup restore --backup data/private-backups/session-v1 --destination data/private-backups/restored.sqlite
.venv/bin/python -m aos.recovery --database data/private-backups/restored.sqlite
```

Örnek parent oluşturma komutu yalnız mevcut olmayan dizin içindir; var olan izinleri otomatik gevşetmeyin. `create` source'u migrate/reconcile etmeden snapshot alır; yeni dizinde yalnız `store.sqlite` ve `manifest.json` yazar. Manifest içerik/byte/schema/migration pin'leri ve source snapshot hash'ini içerir. Çıktı dosyaları ve ilgili dizinler fsync edilir. Var olan backup dizini üzerine yazılmaz. Hata/çökme sonrası partial dizin korunur; otomatik silme/onarım/overwrite yapılmaz.

WAL içeriği frozen SQLite backup ile önce bellekte birleştirilir; salt `.sqlite` dosyasını kopyalamak değildir. Standalone restore için **yalnız kopyanın** header byte 18/19'u rollback formatına alınır; source snapshot hash'i ile üretilen dosya hash'i ayrı tutulur ve aralarındaki izinli iki-byte dönüşüm doğrulanır. Bu, SQLite'ın belgelenmiş deserialize sınırlamasına uygundur: [SQLite deserialize](https://sqlite.org/c3ref/deserialize.html). Kaynak journal mode değiştirilmez veya checkpoint edilmez.

`verify`, exact iki-dosya seti, owner/private mode, dosya boyutu/hash, kaynak hash bağı, canonical migration/schema ve in-memory integrity/FK denetimini yapar. Hash'leri değiştirilmiş yabancı schema da reddedilir. Manifest imzalı/harici attestation değildir; aynı UID/root'un güvenilir dosyaları yeniden üretmesine karşı garanti sunmaz. Şema fixture'ı placeholder'dır, geçerli backup örneği değildir.

`restore` yalnız **yeni** DB dosyasına yazar. Var olan hedef veya `-wal/-shm/-journal/.lock` sidecar'ı reddedilir. AOS writer kilidi restore boyunca tutulur; boş private lock dosyası kalır. Var olan DB, WAL, başka runtime veya container değiştirilmez. Restored DB'de eski approval/lease satırları tarihsel olarak hâlâ bulunabilir: bunlar **yetki değildir**. Normal `TrajectoryStore` writer açılışı mevcut reconciliation ile onayları iptal edip belirsiz eylemleri işaretler; testte bu geçiş ayrıca doğrulanır. CLI restore runtime başlatmaz ve v7'yi v8'e migrate etmez.

### Yedek kapsamı dışında

Workspace dosyaları, browser profili/process state, Docker filesystem/container, model/adapter weights, raw capture dosyaları, host grant policy ve review audit journal bu DB yedeğinde **yoktur**. DB referansları bunları yeniden oluşturmaz. Schema/FK/integrity kontrolü, JSON alanlarının tüm uygulama semantiğini veya harici artifact varlığını doğrulamaz. Reviewer journal'ı önceki DB path/device/inode'una bağlıdır; restored DB'ye sessizce bağlanamaz. Bu yedek tam sistem disaster recovery, exact-once dış etki, checkpoint continuation veya yeniden eğitim izni değildir. Gerçek restore'dan önce harici etkiler/artifact'lar ve bağımsız private arşivler ayrıca değerlendirilmelidir.

## Kabul

Canonical şemalar `schemas/recovery_inventory.schema.json` ve `schemas/recovery_backup.schema.json`; sentetik fixture'lar `examples/` altındadır. `tests/test_recovery_inventory.py` gerçek alt süreçte onay beklerken `os._exit(73)` → değişmez envanter → yeni writer'ın revoke/cancel, no-replay kabulünü içerir. WAL/frozen/uncommitted snapshot, minimizasyon ve bound testleri vardır. `tests/test_recovery_backup.py` WAL dahil private create/verify/restore/reconcile, no-overwrite/sidecar, hash/schema/permission, v7 ve I/O failure davranışını sınar. Eski `tests/test_recovery.py` Supervisor recovery regresyonları ayrıca korunur. UI testi gerçek Chromium/Docker üzerinde fixture motoruyla ayrı yürür; ölçümler STATUS'tadır.
