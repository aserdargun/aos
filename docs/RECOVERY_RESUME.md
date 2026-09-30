# Sıfır eylemli hello checkpoint devamı — 6n

**6o yazma receipt'i**, ayrı [salt okunur etki incelemesi](RECOVERY_EFFECT.md) için vardır. Receipt eşleşse bile bu modülün sıfır-action sınırı değişmez; eski action bulunan run hâlâ reddedilir.

## Dar yürütme sınırı

`aos.recovery_resume`, **yalnız yeni descriptor-workspace plain hello run'ını**, çökme sonrasında aynı `run_id` ile sürdürebilir. Kalıcı kayıtta hiçbir action, verification, supervisor escalation veya desktop job bulunmamalıdır. `intent`, `running`, `uncertain` kadar geçmiş `ok`, `error`, `denied` ve `cancelled` action da kabulü engeller. Eş dosya içeriği bu engeli kaldırmaz.

Bu dilim, bir dış etkinin gerçekleşip gerçekleşmediğine karar vermeye çalışmaz: **hiçbir action kaydedilmemiş olmasını** ister. Gateway intent'i eylemden önce yazdığı için, sağlam ve güvenilir DB altında bu dar durum güvenli bir başlangıç sınırıdır. DB kaybı/manipülasyonu, aynı UID/root veya advisory lock'u yok sayan harici yazıcıya karşı attestation değildir.

Browser/vision/Docker, Supervisor recovery-probe, sequence ve UI scheduler görevleri reddedilir. Bunların aynı canlı oturumdaki Pause/Resume davranışı değişmez. Genel crash continuation veya uncertain-effect reconciliation uygulanmış sayılmaz.

## İşlem sırası

1. CLI etkileşimli stdin/stdout TTY ister; pipe veya kaydedilmiş onay dosyası kabul edilmez. Eksik terminalde DB/workspace oluşturulmaz. Motor fixture değilse manifest ve inference Python yolu açıkça gereklidir.
2. [6m checkpoint incelemesi](RECOVERY_CHECKPOINT.md) sabit scope/criterion, state/snapshot hash, kayıtlı dizin kimliği ve hostun **tam engine identity** digest'ini denetler. Kaynak veya workspace yoksa oluşturulmaz.
3. `WorkspaceRuntime.start_existing()` bütün yol bileşenlerini no-follow açıp mevcut dizinin exclusive AOS kilidini alır. Normal `TrajectoryStore` writer açılışı mevcut reconciliation'ı çalıştırır; eski lease/onaylar iptal olur. **Bu writer açılışı DB genelindeki kayıtları uzlaştırır**, salt okunur değildir; daha sonra admission reddedilse bile bu startup değişimleri kalabilir.
4. Writer ve workspace hâlâ sahipliyken aynı DB, aynı mevcut dizin, `PAUSED` run/owner, tam deployment, canonical checkpoint ve sıfır-action sınırı tekrar denetlenir. Dosya absent veya tam eş içerikli olmalıdır; farklı/okunamaz dosya korunur ve admission reddedilir.
5. Yeni rastgele lease ve typed `ResumeAdmission` üretilir; mevcut `human_interventions` tablosunda `kind=resume` kaydı kalıcı yazılır. Eski lease/admission/approval payload'u yeniden kullanılmaz. Yeni migration yoktur. Bu kayıt **devam talebidir**, onay veya başarı değildir; replay edilen event bir işi başlatmaz.
6. Mevcut Operator aynı run'da yeni state version, gözlem ve sonlu Decider kararı üretir. Decider native worker ağırlık/kod/dependency pin'lerini yeniden doğrular, gerçek inference yapar ve response digest'i engine pin'iyle eşleşmelidir. Fixture motoru ayrı ve açık seçilir. Çıkarım sonrası worker kapanır; sürekli resident model health'i iddia edilmez.
7. Eylem için yeni action/state/runtime/lease/deadline bağlı digest gösterilir. Terminalde yalnız `APPROVE <tam-digest>` kabul edilir; 60 saniye, yanlış yanıt, EOF veya kontrol kesintisi eylemi engeller. İlk action ve onun sabit bağımsız readback'i bu onayın kapsamıdır. Boolean/result, onay artifact'i veya eski digest CLI argümanı olarak verilemez.
8. Typed `ResumeApproval` kaydı, eylemden **önce** yazılır. Onay sonrası engine identity, sahiplik, sıfır-action koşulu, dizin/checkpoint ve dosya durumu tekrar kontrol edilir; normal SafetyPolicy ve gateway kontrolleri ayrıca uygulanır. Dosya yoksa exclusive create; zaten eşitse yalnız read; başarı için mevcut bağımsız exact-content verification gerekir.

CLI dışındaki Python adaptörü hostun güvendiği `approve(Action) -> Awaitable[bool]` callback'ini zorunlu ister; model çıktısı bu callback değildir. Kabul testleri açık `actor=acceptance_test` kullanır. Yerel terminal otomasyonu insan incelemesi olarak raporlanmaz.

## Kullanım

```bash
.venv/bin/python -m aos.recovery_resume \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --engine decider \
  --manifest models/decider-manifest.json \
  --model-python /PATH/TO/EXISTING/INFERENCE/bin/python
```

Yer tutucular uygun mevcut kaynaklarla değiştirilir; Python symlink'i resolve edilerek venv dışına çıkarılmaz. Fixture denemesinde son üç model seçeneği yerine `--engine fixture` kullanılır. Uygun checkpoint, ancak önceki bir plain hello işi action kaydetmeden kesilmişse vardır; mevcut succeeded/cancelled/failed run'lar yeniden açılmaz. Komut otomatik model indirme, servis kurulumu, training veya promotion yapmaz.

## Crash ve audit sınırları

- Resume talebi, state geçişi, approval kaydı ve dış etki tek bir atomik işlem değildir. Her aşamada crash mümkündür. Admission veya approved receipt **tek başına hiçbir action'ı çalıştırmaz**.
- Approval kalıcı olduktan sonra fakat gateway intent'inden önce crash olursa action sayısı hâlâ sıfır olabilir. Sonraki **explicit** devam yeni lease, model kararı, action digest ve ayrı onay ister; tarihsel approved receipt kullanılmaz.
- Intent kaydedildikten sonra crash olursa, write henüz başlamamış veya dosya zaten tam eş olsa bile devam reddedilir. Startup kaydı uncertain yapar; bu dilim onu çözmez, silmez veya ok saymaz.
- Ret/timeout mevcut Operator üzerinden failed; async cancellation ownership revoke ve cancelled sonucu üretir. Audit I/O hatası eylemi engeller; kısmen ilerlemiş run kaydı sonraki normal writer startup reconciliation'ına kalabilir. CLI uygulama hatalarında generic hata verir; ham DB/lease/model verisi stderr'e dökülmez.
- Terminal onayı ile son policy denetimi arasında workspace veya deployment değişirse onay kaydı tarihsel kalır ama action uygulanmaz. `approve` kaydı yürütme başarısı değildir.
- `resume_hello` canlı bir host writer/runtime adaptörüdür; exported checkpoint/receipt dosyalarını execution capability olarak tüketmez. Yeni HTTP endpoint veya UI Resume düğmesi eklenmedi.

## Kabul

`tests/test_recovery_resume.py`, gerçek subprocess'te ilk onay öncesi crash → writer reconcile → aynı run/yeni lease/yeni karar/yeni onay → independent verification; write öncesi/sonrası uncertain red; ikinci crash sonrası eski approved receipt'i kullanmama; ret, expiry, cancellation, concurrent admission, stopped/replaced workspace, changed content/deployment, audit failure ve gerçek PTY exact digest akışını sınar.

`AOS_REAL_TASK_TESTS=1 AOS_MODEL_PYTHON=/PATH/TO/INFERENCE/bin/python` ayrıca gerçek pinned Decider ile iki inference arasında subprocess crash ve taze onay callback'li aynı-run completion testini açar. Bu testin onayı otomatik **test onayıdır**, gerçek insan onayı değildir. Kalıcı kanıt yalnız ignored `data/resume-real-*` altında tutulur; modeller veya trajectory kaynak manifest'ine girmez. Ölçümler [STATUS](STATUS.md) içindedir.
