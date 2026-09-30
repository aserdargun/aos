# Private yerel inceleme servisi — 7f

**Uygulandı:** explicit `init/serve/review/reconcile` komutları, owner-only policy/socket/dosya kontrolleri, gerçek terminal istemcisi ve fsync'li hash-chain audit journal. 7e'nin sentetik S1/explicit onay sınırı korunur. Otomatik label/hak, eğitim, model çağrısı veya runtime yetkisi üretilmez.

## Hazırlık ve kullanım

DB, policy/request dosyaları ve servis dizininin doğrudan parent'ları mevcut kullanıcıya ait **0700**, dosyalar **0600** olmalıdır. Symlink/hardlink ve başka UID reddedilir; araç mevcut izinleri otomatik gevşetmez. DB **önceden mevcut, exact v8–v12** olmalıdır. Servis v7'yi migrate etmez, DB oluşturmaz ve runtime reconciliation yapmaz. Mevcut runtime writer kilidi varsa açılmaz. Önceki gerçek kabul DB'leri bu komutlarla değiştirilmedi.

Host policy dosyası `schemas/dataset_reviewer_policy.schema.json` biçimindedir: schema_version, grants listesi; her grant mevcut host UID, explicit run_ids, accept/revoke kararları ve Unix epoch expiry taşır. Bu ilk listener yalnız kendi UID'sini kabul eder; root bypass veya wildcard yoktur. `examples/dataset_reviewer_policy.json` placeholder **sentetik şema fixture'ıdır**, kurulacak gerçek yetki değildir. Policy dosyasını repo/model çıktısından otomatik üretmeyin; host yöneticisi kapsamını ayrı belirler.

```bash
.venv/bin/python -m aos.dataset_review_service init --database data/private/store.sqlite --state-dir data/reviewer
.venv/bin/python -m aos.dataset_review_service serve --database data/private/store.sqlite --state-dir data/reviewer --policy data/private/reviewer-policy.json
.venv/bin/python -m aos.dataset_review_service review --socket data/reviewer/reviewer.sock --request data/private/review-request.json
.venv/bin/python -m aos.dataset_review_service reconcile --database data/private/store.sqlite --state-dir data/reviewer
```

Yollar örnektir; otomatik veri/grant oluşturmaz. `init` yalnız yeni private state dizini ve journal başlığı açar, DB'yi değiştirmez; var olan journal'ı sıfırlamaz. `serve --once` bir bağlantı sonrası kapanır. Normal `serve` explicit durdurulana kadar seri oturum işler; her bağlantı başında policy yeniden yüklenir. Aktif oturumun grant snapshot'ı değişmez, en fazla 120 saniyedir; pending onayı iptal etmek için servisi durdurun. Policy bozuksa fail-closed kapanır.

Request dosyası 7e prepare_accept veya prepare_revoke mesajıdır. Accept, önceden incelenmiş source hash, mevcut label ve canonical candidate ile üç content/rights/redaction beyanını gerektirir. Dosya üretmek tek başına insan onayı değildir. Client, hem stdin hem stdout TTY değilse **içerik okumadan** reddeder; `--yes` veya pipe ile onay yoktur. Candidate ve tam receipt terminal kontrol karakterleri kaçırılmış ASCII JSON olarak gösterilir. Client server UID'sini ve receipt'in source/candidate/kind/rights/reviewer bağını doğrular; yalnız `ONAY <tam event hash>` yazılırsa confirm gönderir. Diğer yanıtlar iptaldir. Testler PTY ile sentetik onay verir; bu insan review kanıtı değildir.

## Kalıcı audit ve crash sonucu

- `audit.jsonl` 0600, exclusive writer lock, canonical envelope, sequence/previous hash ve event hash kullanır. Her event append sonrasında `fsync`; ilk oluşturma sonrası parent directory de `fsync` edilir. Challenge/raw candidate/DB path journal'a yazılmaz. UID/grant hash/expiry ve event binding kalır; tam grant yapılandırmasının güvenli arşivi host sorumluluğudur.
- Başlık, DB'nin private path/device/inode digest'ine ve mevcut receipt kümesinin baseline hash'ine bağlıdır. Başka DB/restore kopyasıyla sessiz devam edilmez; taşıma/restore ayrı explicit bakım gerektirir. Journal'a girmeyen yeni receipt veya kaybolan eski receipt uzlaştırmayı durdurur. Önceden mevcut baseline receipt'ler yeni authentication kanıtı sayılmaz.
- Journal en fazla 32 MiB, satır en fazla 16 KiB. Hash/schema/sequence bozukluğu, yarım son satır, short write veya fsync hatası fail-closed olur. Hatalı journal otomatik kesilip yeniden yazılmaz. Hash chain owner/root tarafından yeniden hesaplanabilir; kriptografik immutable storage veya saldırgan dosya sahibine karşı garanti değildir.
- SQLite receipt commit'i ile journal ayrı kaynaklardır; **atomik iki-kaynak commit yoktur**. Önce dayanıklı confirmation, sonra receipt, sonra committed audit yazılır. Receipt var ve matching confirmation varsa, son committed event eksik olsa da reconciliation `recorded` ve `commit_audit_missing` sayar. Bu kayıt varlığıdır; kullanılabilir/güncel eğitim izni değildir. Confirmation var ama receipt yoksa `not_recorded` olur; otomatik yeniden yazma/onay yapılmaz. Committed event var ama receipt yoksa bütünlük hatasıdır.
- `reconcile` journal'ı shared/read-only lock ile, DB'yi frozen read-only snapshot ile denetler; servis aktifken exclusive journal lock nedeniyle reddedilir. Önce servisi durdurun. Rapor yalnız sayaçlar ve training_ready=false içerir; source/rights/current receipt değerlendirmesi için 7d preflight ayrı kalır.
- SIGTERM/interrupt açık socket'i kapatır, uncommitted transaction rollback eder, yalnız owned socket'i temizler. SIGKILL sırasında socket dosyası kalabilir; sonraki start yalnız private/owned ve bağlantısı gerçekten refused olan socket'i siler. Canlı/başka dosyaya dokunmaz. Kaybolan cevap sonucu belirlemez: client'ın gösterdiği receipt ID ve reconciliation ile denetleyin; yeniden onay kendiliğinden verilmez.

`ReceiptStore` yalnız receipt insert sunar; migration/reconcile olmadan owned writer açar. Exact v8–v12 schema/integrity/FK, 128 MiB DB sınırı ve başlangıç SQL deadline denetlenir. Uzun callback/OS fsync hard real-time kesilemez. Aynı UID'deki process'leri ayırt etme, FD transferi, rootless/farklı hesap izolasyonu ve adversarial genel masaüstü kabulü hâlâ ayrıca gerekir. Socket/state/DB dizinleri agent workspace'ine mount edilmez. [7e güven sınırları](DATASET_REVIEWER.md) geçerlidir.

## Kabul

`tests/test_dataset_review_service.py` gerçek servis/istemci alt süreçleri, PTY, receipt öncesi/sonrası `os._exit` crash, stale socket restart, SIGTERM, hash-chain/tail bozulması, fsync hatası, permissions/locks, v7/no-migrate ve client receipt binding kontrollerini içerir. Tüm kaynaklar geçici sentetiktir. Ölçümler [STATUS](STATUS.md) içindedir.

Sonraki kapılar ayrı dilimlerde eklendi: [7g gerçek tokenizer/slot/batch](DATASET_TOKENIZER.md), [7h forward/loss](DATASET_LOSS.md) ve [7i readiness](DATASET_READINESS.md). Bunlar yalnız sentetik fixture kapsamındadır; gradient/optimizer, yeterli lisanslı veri ve eğitim yetkisi açık kalır.
