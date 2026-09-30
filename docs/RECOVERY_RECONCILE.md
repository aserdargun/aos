# Açık hello yazma uzlaştırması — 6p

## Dar geçiş

[6o](RECOVERY_EFFECT.md) yazma kanıtını yalnız inceler. 6p, **aynı receipt ile aynı dosya sürümü hâlâ eşleşiyorsa**, explicit host onayıyla tek eski hello action'ını `uncertain → ok` geçirir. Yeni araç yürütmez, dosyayı tekrar yazmaz. Geçmiş action sonucu ile gelecekteki execution yetkisi ayrı tutulur.

Bu yalnız descriptor-workspace plain hello kapsamındadır. Run ve state `PAUSED`, owner `PAUSED`, outcome `unknown`, training eligibility sıfır olmalıdır. Tam bir eski action bulunmalı; matching exclusive-create receipt, eski allowed decision/envelope/EXECUTE snapshot bağları geçerli olmalıdır. Action yalnız startup reconciliation'dan kalan `RUNTIME_CRASH`, boş result/completed_at ile kabul edilir. Desktop job, Supervisor/recovery probe, verification veya label bulunan run reddedilir. Eski ok/error/denied/cancelled action'lar yeniden sınıflandırılmaz.

**Sonuç görev başarısı değildir.** Run/state/lease aynı kalır; yeni observation/verification/label, eğitim izni veya deployment promotion üretilmez. [6n](RECOVERY_RESUME.md) hâlâ herhangi bir eski action bulunan run'ı reddeder. Yeni resume yetkisi, bağımsız final verification ve genel recovery bu dilimde yoktur.

## CLI ve onay

```bash
.venv/bin/python -m aos.recovery_reconcile \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --action-id ACTION_ID \
  --deployment-sha256 EXPECTED_DEPLOYMENT_IDENTITY_SHA256
```

Yerel stdin/stdout TTY olmalıdır; pipe veya yönlendirilmiş otomatik cevap kabul edilmez. TTY yoksa DB/workspace açılmadan çıkılır. Mevcut DB/workspace ve deployment snapshot digest'i önce salt okunur denetlenir. Ardından existing workspace kilidi ve mevcut `TrajectoryStore` writer kilidi alınır. **Normal writer startup reconciliation'ı DB genelinde eski lease/onay/run/action durumlarını uzlaştırabilir; CLI salt okunur değildir.** Reddedilen onay bu startup değişikliklerini geri almaz. Eski gerçek kayıtlar üzerinde komut kendiliğinden çalıştırılmaz.

Terminalde minimized typed request ve tam digest gösterilir. Süresi içinde `RECONCILE <gösterilen SHA-256>` yazmak gerekir; diğer yanıt, EOF, 60 saniyelik sürenin dolması veya iptal action sonucunu değiştirmez. Süre request hazırlanmaya başladığında işler. Request run/action, kaynak DB snapshot/state, receipt, workspace ve deployment hash'lerini bağlar; raw yol, içerik, inode, UID veya lease taşımaz. Onay yalnız bu sonucu kaydetmeye ilişkindir, yürütme onayı değildir. Model/JSON kendisine actor veya yetki veremez; API trusted host kodudur ve ToolRegistry/UI route'u olarak sunulmaz.

## Transaction ve yarışlar

1. Owned writer ve exact `WorkspaceRuntime` üzerinde 6o incelemesi yapılır. Workspace kilidi aynı açık descriptor üzerinden korunur.
2. 60 saniye geçerli exact-request onayı beklenir; istemciye bağımsız typed kopya verilir. Ret, truthy ama boolean olmayan yanıt veya değiştirilmiş kopya kabul edilmez.
3. `BEGIN IMMEDIATE` ile SQLite yazma transaction'ı alınır; workspace hâlâ kilitlidir. 6o snapshot/state/action/receipt/file denetimi ve dar run önkoşulları yeniden çalışır. Request, önceki request ile birebir aynı olmalıdır; ilgisiz DB değişiklikleri de source snapshot hash'ini değiştirip reddedebilir.
4. Tek `uncertain` action üzerinde conditional update: `status=ok`, `error_code=NULL`, `result_json={"bytes_written":28}`, uzlaştırma zamanı `completed_at`. Bu zaman eski fiziksel yazmanın zamanı olarak sunulmaz.
5. Aynı SQLite transaction'ında `human_interventions.kind=correction` kaydı eklenir. Typed `WriteReconciliation`, exact request/digest, host actor ve kayıt zamanını içerir. Yeni SQL migration veya eski receipt'e backfill yoktur.

Audit yazılamazsa action update rollback olur. Commit öncesi process crash iki kaydı da bırakmaz; commit sonrası crash ikisini birlikte bırakır. İkinci uzlaştırma artık `ok` olan action'ı reddeder; ikinci onay/audit üretmez. Eski envelope ile gateway çağrısı yalnız cached `bytes_written` döndürebilir; dosyaya tekrar dokunmaz. Bu idempotent sonuç okuması execution yetkisi değildir.

Güvence SQLite içindeki action/audit transaction'ına aittir; filesystem ile ortak atomiklik veya exactly-once genel dış etki iddiası yoktur. İnceleme sonrasındaki dış yazıcılar, aynı UID/root, inode reuse ve advisory lock sınırları 6o'daki gibi kalır. Checkpoint karşılaştırması canlı model/artifact sağlığına attestation değildir. Receipt eksikse exact içerik yeterli olmaz; boşta dosyadan yeni receipt uydurulmaz.

## Kabul ve sonraki adım

`tests/test_recovery_reconcile.py`: gerçek fixture subprocess receipt-crash, aynı-run sonuç/audit atomikliği, commit öncesi/sonrası ikinci crash, gerçek PTY onay/ret, stale DB/file/workspace, expiry/cancel, eksik/yanlış/çift receipt, audit arızası, eşzamanlı onay, private alan minimizasyonu, false authority şemaları, no-replay ve 6n red sınırı.

`AOS_REAL_TASK_TESTS=1 AOS_MODEL_PYTHON=/PATH/TO/INFERENCE/bin/python` ayrı gerçek pinned Decider kabulünü açar: bir gerçek System-1 kararı, receipt commit sonrası subprocess crash, açık `acceptance_test` onayı, action ok/audit kalıcı; run paused, verification sıfır. Bu test insan incelemesi veya başarılı continuation değildir. Ölçümler ve başarısız ortam denemeleri [STATUS](STATUS.md) içindedir; yerel kanıt `data/reconcile-real-*` altındadır ve kaynak paketine alınmaz.

Bu kayıtlı sonucu tüketen **ayrı ve taze onaylı, yalnız bağımsız final verification yapan** hello continuation [6q](RECOVERY_VERIFY.md) içinde uygulanır. 6p komutu kendisi devam etmez; eski yazma tekrar edilmez ve 6n sınırı değişmez. Docker/browser/vision/sequence veya genel görevler ayrı kabul gerektirir.
