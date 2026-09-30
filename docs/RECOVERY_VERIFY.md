# Uzlaştırılmış hello yazmasının son doğrulaması — 6q

## Ayrı, yalnız okuma yapan devam yolu

[6p uzlaştırması](RECOVERY_RECONCILE.md) eski yazma sonucunu kaydeder, run'ı paused bırakır. 6q, **explicit yeni CLI çağrısı**, yeni lease, taze model kararı ve **iki ayrı exact-action onayı** ile aynı run'ın bağımsız son doğrulamasını yapar. Eski write envelope/receipt/correction audit yürütme yetkisi olarak yeniden oynatılmaz.

Yalnız descriptor-workspace plain hello desteklenir. Mevcut dosya aynı 6o witness nesnesi/sürümüyle eşleşmelidir. Run/state/owner paused, outcome unknown ve eligibility sıfır olmalıdır. Tam bir eski ok write ve tek typed 6p correction gerekir; correction'ın request/hash/run/action/workspace/deployment/receipt bağı, kayıt aktörü/zamanı ve action sonucu denetlenir. Kaynak state hash'i uzlaştırma anındaki paused state ile aynı olmalıdır. Desktop job, Supervisor/recovery probe, verification, label veya daha önceki 6q admission varsa giriş reddedilir.

**6n değişmedi:** eski action olan run'lar hâlâ `aos.recovery_resume` üzerinden devam edemez. 6q ayrı explicit yoldur; receipt'siz uncertain eylem, Docker/browser/vision, sequence veya genel checkpoint recovery değildir.

## Komut

```bash
.venv/bin/python -m aos.recovery_verify \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --action-id ORIGINAL_WRITE_ACTION_ID \
  --reconciliation-id RECONCILIATION_ID \
  --engine decider \
  --manifest models/decider-manifest.json \
  --model-python /PATH/TO/INFERENCE/bin/python
```

Test için `--engine fixture` açıkça seçilebilir; fixture gerçek model değildir. CLI stdin/stdout TTY olmadan DB/workspace açmaz. Mevcut workspace ve writer kilitleri gerekir; normal writer startup eski lease/onay/run durumlarını uzlaştırabilir. Eski gerçek kanıt DB'leri kendiliğinden açılmaz veya dönüştürülmez.

`APPROVE <SHA-256>` iki kez istenir. Her onay farklı read action/idempotency/digest'e bağlıdır ve 60 saniye geçerlidir. Ret, EOF, 256 byte üstü yanıt, yanlış digest, timeout veya cancellation onay değildir. Ortak terminal okuyucusundaki EOF/uzun yanıt reddi 6n için de geçerlidir. Model/JSON actor veya yeni yetki üretemez; programatik callback yalnız trusted host sınırındadır, UI/API/ToolRegistry'ye yeni kontrol yüzeyi eklenmedi.

## Yürütme ve kalıcılık

1. Source snapshot ve bound 6p correction mevcut writer transaction'ında denetlenir. Hash-only `VerificationAdmission`, `human_interventions.kind=resume` olarak kalıcı yazılır. Yeni lease referansı, source state/snapshot, write receipt ve correction hash'leri bağlanır. Admission tek kullanımlık deneme kaydıdır; tek başına okuma onayı değildir.
2. Mevcut `Operator.hello` yalnız bu akışta `verification_only` moduna alınır. Yeni lease/gözlem/model kararı üretilir. Dosya eksik veya farklıysa **write seçeneği üretilmez, dosya onarılmaz**. Sonlu read/yardım seçeneklerinde model abstain edebilir; başarı zorlanmaz. Gerçek Decider worker her çağrıda mevcut pinned artifact/dependency kimliğini denetler.
3. İlk yeni `filesystem.read`, kendi taze onayından sonra gateway'de kalıcı intent/result ile çalışır. Sonra farklı ikinci read action için ikinci onay istenir; ikinci okuma bağımsız readback'tir. İki onay mevcut `ResumeApproval` şemasıyla, bu admission ve yeni lease'e bağlı kaydedilir.
4. Her okuma öncesinde ve onay sonrasında source correction/receipt/file/workspace/deployment, active state/lease, admission ve önceki onay/okuma geçmişi tekrar denetlenir. Eksik/değişmiş approval audit veya fazladan action reddedilir. Gate yalnız iki yeni `filesystem.read` ve sabit hello path'ine izin verir.
5. İkinci readback sonrasında, verification yazılmadan önce aynı binding ve dosya sürümü bir kez daha kontrol edilir. Böylece okuma sonrasında eş içerikli farklı inode'a geçiş de başarı diye kaydedilmez. Tam içerik bağımsız `independent_read_equals` verification ile karşılaştırılır; yalnız passed sonucunda mevcut Operator/store final yolu run'ı succeeded yapar.
6. Deneme bittiğinde bu akışın lease'i iptal edilir. Başka bir owner/lease devralmışsa üzerine yazılmaz. Eski write satırı değişmez; yalnız iki yeni read, yeni model/decision/state/audit ve bir observation/verification eklenir. Otomatik label, review, eligibility veya promotion yoktur.

İki-action sınırı gateway'deki yeni read action'ları içindir. Kaynak witness denetimleri ve karar öncesi taze gözlem de dosyayı okur; bunlar explicit CLI kapsamındaki salt okunur preflight/gözlemlerdir, yeni action veya kullanıcı onayı diye sayılmaz.

Şema `recovery_verify.schema.json` allowed_tool=filesystem.read, maximum_read_actions=2, requires_action_approval=true, write_authorized=false ve automatic_replay_allowed=false sınırlarını sabitler. Örnek açık sentetiktir. Yeni SQL migration veya model/dependency pin değişikliği gerekmez. Başarılı bounded export mevcut redaction/verification kurallarını kullanır; raw receipt/yerel dosya kimliği export edilmez.

## Kesintiler ve sınırlar

- İlk read reddedilirse yeni action yoktur; ikinci reddedilirse yalnız ilk read kalır, verification/success oluşmaz.
- Admission veya ilk read sonrası gerçek process crash'te normal startup owner'ı paused yapar. **Eski admission/approval kullanılarak otomatik devam edilmez; ikinci 6q denemesi reddedilir**, ilk read henüz gerçekleşmemiş olsa bile. Bu muhafazakâr sınır bir recovery deadlock'unu otomatik çözme iddiası değildir.
- SQLite audit arızası read'in onaysız ilerlemesine yol açmaz. Tam final verification/state/run sonucu tek atomik transaction değildir; son yazım aralığında crash olmuş run ayrıca inceleme gerektirebilir.
- İki read ve final guard farklı anları gözlemler; DB+filesystem atomikliği, aynı UID/root, inode reuse veya advisory lock dışı yazıcılara karşı mutlak garanti yoktur. Genel exactly-once, tüm crash aralıklarında continuation veya insan incelemesi iddia edilmez.

## Kabul ve sonraki adım

`tests/test_recovery_verify.py`: aynı-run başarı/iki ayrı onay/no-write, ilk/ikinci ret, cancellation/expiry/takeover, kaynak/correction/admission/approval bağları, eksik veya aynı byte'larla değişmiş dosya, final read sonrası inode değişimi, audit arızası, eşzamanlı giriş, model write önerisinin reddi, gerçek subprocess crash ve PTY olumlu/olumsuz akışlar.

`AOS_REAL_TASK_TESTS=1 AOS_MODEL_PYTHON=/PATH/TO/INFERENCE/bin/python` gerçek Decider write → receipt-sonrası crash → açık test onaylı 6p → taze Decider kararı/iki test onaylı read → bağımsız verified başarı kabulünü açar. Toplam iki gerçek System-1 çağrısı, sıfır System-2, tek eski write ve iki yeni read beklenir. Onay aktörü `acceptance_test`'tir; insan onayı değildir. Kanıtlar ignored `data/verify-real-*` altında, ölçümler [STATUS](STATUS.md) içindedir.

Bu son commit aralıkları için [6r finalizasyonu](RECOVERY_FINALIZE.md) ayrı explicit metadata yoludur: yalnız kalıcı tam bağımsız kanıt varsa state/run/audit birlikte tamamlanır. Eksik verification veya read tamamlanmaz; admission/approval/write replay edilmez. Docker/browser/vision/sequence recovery ve genel görevler ayrı kalır.
