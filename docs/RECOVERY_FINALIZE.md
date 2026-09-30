# Kalıcı hello doğrulamasının finalizasyonu — 6r

## Dar kapsam

[6q](RECOVERY_VERIFY.md) iki onaylı read, bağımsız verification, state ve run sonucunu ayrı commit'lerle yazar. 6r yalnız **verification zaten kalıcı ve tam ise**, son state/run metadata aralığını taze açık onayla tamamlar. Yeni model kararı, gateway eylemi, observation veya verification üretmez. Eski yazma, admission ve read onayları tekrar yürütme yetkisi değildir.

Yalnız descriptor-workspace plain hello, tek matching 6o write receipt, tek 6p correction ve tek 6q admission desteklenir. Docker/browser/vision/sequence, receipt'siz uncertain action, eksik read/verification ve genel otomatik recovery kapsam dışıdır. Eski gerçek kanıt DB'leri writer ile açılmaz veya geriye dönük değiştirilmez.

## Salt okunur sınıflandırma

```bash
.venv/bin/python -m aos.recovery_finalize \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --action-id ORIGINAL_WRITE_ACTION_ID \
  --admission-id VERIFICATION_ADMISSION_ID \
  --deployment-sha256 PINNED_DEPLOYMENT_SNAPSHOT_SHA256
```

Varsayılan komut TTY veya writer istemez. No-follow mevcut workspace descriptor'ı ve exclusive advisory lock, 6m/6o denetimleri ve aynı hash'e sahip frozen SQLite backup'lar kullanılır. Her backup 128 MiB/10 saniye ile sınırlıdır. Kaynak DB/committed WAL verisi değişmez; SQLite read-only erişimi SHM okuyucu bookkeeping'i yapabilir. Dizin/state/deployment snapshot eşliği canlı model artifact attestation değildir; model başlatılmaz.

Hash-only rapor ham içerik, path, inode veya lease ID çıkarmaz; execution/resume/automatic replay alanları daima false'tur:

| Disposition | Anlam |
| --- | --- |
| `incomplete` | Kalıcı iki başarılı read ve bağımsız verification henüz tam değil; finalizasyon yok. |
| `file_not_matching` | Tam eski kanıt var, fakat mevcut dosya receipt'in aynı nesne/sürümüyle eşleşmiyor. |
| `requires_startup` | Tam kanıt var; kayıt hâlâ running/AGENT. Bu canlılık veya crash tespiti değildir. |
| `ready_to_finalize` | Tam kanıt ve matching dosya; startup sonrasında run/state/owner paused. |
| `already_finalized` | Bağlı bağımsız kanıt ile succeeded/passed metadata zaten mevcut; yeniden onay/yazım yok. |
| `blocked` | Tam kanıta rağmen cancelled/failed veya desteklenmeyen state/run birleşimi; başarıya zorlanmaz. |

Bozuk/çift/çapraz bağlı kanıt sessizce tamamlanmış sayılmaz; denetim hata ile kapanır. Başarılı tarihsel run'ın dosyası sonradan değişirse rapor `file_not_matching` olabilir; tarihsel DB sonucu yeniden yazılmaz.

## Kanıt kapıları

- Orijinal write envelope/decision/EXECUTE snapshot/receipt ve 6p request digest, actor, zaman, action sonucu birbirine bağlıdır. 6p correction, mode ile filtrelenir; sonraki 6r correction bununla karıştırılmaz.
- 6q admission'ın run/write/correction/receipt/workspace/deployment/source paused state bağı denetlenir. Eski paused snapshot ile yeni lease ref'i ayrı kalır.
- İki farklı read'in envelope hash'i, idempotency, sabit path, başarılı exact-content sonucu, aynı taze allowed read decision'ı, DECIDE/EXECUTE snapshot'ları ve admission lease'i denetlenir.
- İki ayrı approved `ResumeApproval`, aynı admission/actor/lease/state ve exact action hash/deadline'a bağlıdır. Onay action başlangıcından önce ve deadline içinde; ikinci onay ilk read tamamlandıktan sonradır. **Süresi geçmiş tarihsel onay doğrulanabilir ama tekrar kullanılmaz.**
- Tek passed `independent_read_equals` / `aos-exact-bytes-v1` verification, ilk read'e; tek filesystem.read evidence observation ikinci read'e bağlıdır. Exact criterion, expected/actual içerik ve read → VERIFY snapshot → observation → verification zaman sırası gerekir. Sadece `passed` etiketi yeterli değildir.
- Desktop job, supervisor/recovery probe, trajectory label veya training eligibility kabul edilmez. Global State transition sözleşmesi değiştirilmez; normal PAUSED → SUCCEEDED hâlâ yasaktır.

## Açık atomik finalizasyon

Aynı komuta **`--finalize`** eklenir. CLI DB/workspace açmadan önce stdin/stdout TTY ister; read-only preflight tam kanıt bulmadan writer açmaz. Sonra mevcut workspace ve normal TrajectoryStore writer kilidi alınır. **Normal writer startup DB genelindeki running kayıtları uzlaştırabilir**; bu mevcut storage davranışıdır. Ret halinde hedefe finalizasyon audit'i/başarı yazılmaz, ancak startup etkileri geri alınmış sayılmaz.

Terminal 60 saniyelik exact-request için `FINALIZE <SHA-256>` ister. Ret, EOF, 256 byte üstü yanıt, yanlış digest, timeout, cancellation veya callback'in request'i değiştirmesi onay değildir. Programatik callback yalnız trusted host entegrasyonudur; actor `local_terminal` veya açık `acceptance_test` olabilir, model/API girdisi yeni yetki vermez.

Onaydan sonra `BEGIN IMMEDIATE` altında snapshot/state/admission/evidence/workspace/deployment yeniden denetlenir ve request'in aynılığı aranır. Tek SQLite transaction:

1. CAS ile paused runtime state'i SUCCEEDED yapar; owner PAUSED kalır, yeni **revoked** lease ID yazılır.
2. Aynı step ve run'ı succeeded/passed olarak bitirir; training_eligible sıfır kalır.
3. State snapshot ve typed `FinalizationReceipt` (`human_interventions.kind=correction`) ekler.

Bu yol `save_state`/`finish` gibi iç commit yapan yardımcıları çağırmaz. Audit arızası veya commit öncesi process crash tüm finalizasyon yazımlarını geri alır; commit sonrası crash'te state/step/run/audit birlikte kalır. Sonraki çağrı already_finalized olur ve yeni onay/audit üretmez. Yeni SQL migration, model/dependency pin veya eğitim/promotion yoktur.

## Kabul ve sınırlar

`tests/test_recovery_finalize.py` gerçek fixture subprocess kesintilerini admission, ilk read, verification öncesi/sonrası, SUCCEEDED state ve run finish aralıklarında üretir. Finalizer commit öncesi/sonrası ikinci process crash, audit rollback, salt okunur DB/WAL hash'i, stale source/file, farklı inode, eksik/çift/sahte kanıt, cancelled state, onay ret/expiry/cancel/değiştirme, eşzamanlı teklifler ve gerçek PTY onay/ret/EOF/oversize sınanır.

`AOS_REAL_TASK_TESTS=1 AOS_MODEL_PYTHON=/PATH/TO/INFERENCE/bin/python` ayrı gerçek pinned Decider kabulünü açar: write receipt crash → explicit test onaylı 6p → taze gerçek karar/iki read/kalıcı verification → process crash → explicit test onaylı metadata finalizasyonu. İki System-1 çağrısı ve üç action değişmez; finalizer model veya gateway çağırmaz. `acceptance_test` insan onayı değildir. Yerel kanıt ignored `data/finalize-real-*` içinde; gerçek ölçümler [STATUS](STATUS.md) içindedir.

DB ve filesystem atomik değildir; witness preflight anını gösterir. Aynı UID/root, advisory lock dışı yazar, inode reuse ve kötü niyetli DB sahibi tehditleri çözülmüş sayılmaz. Hiçbir eksik doğrulama tamamlanmış gibi üretilmez. Sonraki runtime işi orphan lifecycle için salt okunur, deployment/runtime kimliğine bağlı sınıflandırma kabulüdür; otomatik container sahiplenme/silme veya checkpoint replay değildir. Genel scheduler, native dağıtım ve gerçek veri/eğitim kapıları ayrıca açıktır.
