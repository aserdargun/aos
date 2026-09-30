# Hello checkpoint etki incelemesi — 6m

**6o ek incelemesi:** [kalıcı yazma kanıtı](RECOVERY_EFFECT.md), yeni standart descriptor hello write'ının exclusive-create FD witness'ını aynı action/envelope/workspace ve mevcut dosya sürümüne bağlar. Bu checkpoint'in false yetki/causality/resolution bayrakları değişmez.

**6n ek adaptörü:** [sıfır eylemli plain hello devamı](RECOVERY_RESUME.md), mevcut owned descriptor'ı inceleme boyunca koruyarak bu salt okunur denetimi kullanır; ayrıca yeni lease/karar/taze terminal onayı gerektirir. Bu raporun false yetki/causality/resolution bayrakları değişmez; belirsiz action varsa devam reddedilir.

## Uygulanan sınır

`aos.recovery_checkpoint`, yeni **descriptor workspace** hello run'larının kalıcı başlangıç ortamı ve canonical state'i ile mevcut dosya sonucunu karşılaştırır. Salt okunur CLI'dir; görev başlatmaz, eski run'ı sürdürmez, action/verification/approval/lease yazmaz ve hiçbir belirsiz işlemi tekrar uygulamaz. UI/API veya Docker/browser/vision continuation değildir.

`WorkspaceRuntime.status()` artık açık dizin descriptor'ının device/inode/owner UID ve absolute path hash'ini typed `WorkspaceIdentity` olarak verir. Operator bunu ilk run transaction'ında mevcut `runs.environment_json` içine, deployment snapshot ve state ile birlikte kaydeder; gerçek eylemden öncedir. Yeni SQL migration yoktur. Önceki DB'lere kimlik eklenmez: bu bilgisi olmayan eski run'lar reddedilir. Dizin taşınması/değişmesi veya başka yere restore edilmesi eşdeğer workspace sayılmaz. Bu yerel bağ `export_run` trajectory çıktısından çıkarılır.

## CLI

```bash
.venv/bin/python -m aos.recovery_checkpoint \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --deployment-sha256 EXPECTED_DEPLOYMENT_IDENTITY_SHA256
```

Placeholder'lar gerçek mevcut run ve hostun beklediği kimlikle değiştirilmelidir; komut kaynak/workspace oluşturmaz. Digest, `aos.contracts.digest` ile **tam engine identity** üzerinden hesaplanır; varsa `supervisor` identity'si de aynı sözlükte olmalıdır. Dosya byte hash'i, yalnız model ağırlık hash'i veya deployment ID değildir. Fixture identity için:

```bash
.venv/bin/python -c 'from aos.contracts import digest; from aos.decision import FixtureDecisionEngine; print(digest(FixtureDecisionEngine.identity))'
```

Gerçek Decider/Bonsai identity'si mevcut host engine yapılandırmasından alınır. Digest'i eski DB'den kopyalamak yalnız kendisiyle karşılaştırma olur; mevcut deployment doğrulaması sağlamaz. Bu komut model yüklemez, model dosyalarını yeniden hash'lemez, çalışan inference kimliğini sorgulamaz: `deployment_snapshot_matches=true` yalnız verilen beklentiyle tarihsel tam snapshot'ın eşleşmesidir; **`live_deployment_verified=false`** kalır.

## Denetimler ve sonuç

- Mevcut dizin yolunun her bileşeni descriptor-relative `O_NOFOLLOW` ile açılır; symlink, `..`, eksik dizin ve aktif AOS workspace writer kilidi reddedilir. Workspace oluşturma, sahiplenme veya izin değiştirme yoktur. Dizin sahibi mevcut UID olmalıdır.
- Workspace kilidi tutulurken mevcut v7–v10 SQLite kaynağından `audit_snapshot` alınır; schema/migration/FK/integrity ve 128 MiB/10 saniye sınırları korunur. Kaynakta writer reconciliation veya migration yapılmaz.
- Run/task/state/step, sabit hello goal/path/content/success criterion/policy, runtime ID, dizin kimliği, tam deployment digest ve son state snapshot/hash eşleşir. Docker, browser/vision, eski bağsız run veya tutarsız içerik başarı raporu üretmez.
- Yalnız descriptor-relative `hello.txt` okunur. Regular/single-link/en fazla 4096 byte, symlink/FIFO/nonblocking kontrolleri ve okuma öncesi/sonrası metadata + path/inode eşliği uygulanır. Dizin yolunun hâlâ aynı nesneyi gösterdiği ayrıca kontrol edilir.
- Sonuç `exact_content`, `absent`, `different_content` veya `unreadable` olur. Raw içerik/path/lease/action envelope çıkmaz; rapor hash referansları, zamanlar ve unresolved action sayısı verir. CLI kaynak/kimlik hatasında ayrıntı sızdırmayan stderr ve exit 1; geçerli inceleme raporunda, dosya farklı veya okunamaz olsa da exit 0 döner. **Exit 0 görev başarısı değildir.**

`criterion_observed=true` yalnız inceleme anında tam hello byte'larının bulunduğunu söyler. Dosyayı hangi action'ın veya kişinin oluşturduğunu, bir kez oluşturulduğunu, eski action'ın başarılı olduğunu ya da run'ın tüm geçmişini kanıtlamaz. Eş içerik görülse de `uncertain_effect_resolved=false`, `action_causality_verified=false` ve execution/resume/automatic replay bayrakları false kalır. DB'deki belirsiz action değişmez; yeni writer'ın mevcut reconciliation davranışı ayrıca korunur.

## Güven sınırları ve kalan iş

Bu checkpoint imzalı veya bağımsız attestation değildir. Aynı UID/root'un DB'yi yeniden yazmasına, inode reuse'a veya advisory lock'u yok sayan harici yazıcıya karşı yetkilendirme kanıtı sayılmaz. Snapshot ve filesystem gözlemi iki ayrı kaynaktır; atomik ortak transaction veya sürekli geçerlilik yoktur. Metadata denetimleri gözlenen yarışları reddeder, inceleme sonrası değişimi engellemez. Rapor daha sonra execution capability olarak tüketilemez.

Genel continuation için hâlâ desteklenen effect türlerine özel nedensellik/uzlaştırma sözleşmesi ve runtime kabulü gerekir. 6n yalnız sıfır-action descriptor hello için owned runtime/taze model kararı/yeni lease ve onaylı admission ekler; bu inceleme raporu kendi başına yetki değildir. Eski sequence event'leri yetki üretmez. Orphan container adoption/deletion, genel scheduler, UI entegrasyonu ve otomatik devam bu dilimde uygulanmadı.

## Kabul

`tests/test_recovery_checkpoint.py`: gerçekten ayrı subprocess'te gateway `running` kaydından sonra **write öncesinde** ve **fsync edilmiş write sonrasında** `os._exit(73)`; ilk incelemede absent/exact ayrımı, DB/WAL hash değişmezliği, yeni writer sonrasında revoked lease/uncertain action ve sıfır replay/verification. Modeller açık fixture'dır; gerçek filesystem/process crash kabulü, gerçek Decider/Bonsai crash kabulü değildir. Ayrıca active lock, symlink/hardlink/FIFO/oversize, aynı path'te yeni inode, concurrent replacement/mutation, state/scope/deployment uyumsuzluğu, eski bağsız kayıt, minimizasyon/export ve canonical şema testleri vardır. Ölçümler [STATUS](STATUS.md) içindedir.
