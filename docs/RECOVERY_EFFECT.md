# Kalıcı hello yazma kanıtı — 6o

## Dosya eşitliğinden daha dar ve güçlü bir kontrol

6m mevcut dosyayı gözlemler; dosyayı hangi eylemin oluşturduğunu söylemez. 6o, **yeni standart `WorkspaceRuntime` yazmalarında**, gateway'in kalıcı intent/running kaydından sonra exclusive create ile açtığı **aynı dosya descriptor'ından** bounded bir kanıt çıkarır. Böylece yalnız aynı içerik değil, kaydedilen dosya nesnesi ve sürümü de karşılaştırılabilir.

Bu, belirsiz eylemi otomatik başarılı yapmaz veya devam ettirmez. `actions.status`, run sonucu, lease/onay, eligibility ve verification satırları inceleme sırasında değişmez. **6n hâlâ herhangi bir eski action bulunan run'ı reddeder.** Docker/browser/vision veya başka filesystem araçları için kanıt üretilmez. Eski kayıtlara geriye dönük kanıt eklenmez.

## Kaydetme sırası ve crash aralıkları

1. Mevcut gateway action envelope/hash ve intent'i, ardından policy denetimi sonrası running/actual_option'ı kalıcı yazar.
2. `/workspace/hello.txt`, `O_CREAT|O_EXCL|O_NOFOLLOW` ile açılır. Sabit 28 byte yazılır; dosya ve workspace dizini fsync edilir. Farklı içerik üzerine yazılmaz.
3. Hâlâ açık **aynı descriptor** üzerinden `pread` ile tam içerik okunur. Regular/single-link/28 byte koşulları, okuma öncesi-sonrası metadata ve `hello.txt` path'inin aynı nesneyi göstermesi denetlenir. Dosya değişmişse yazma başarısı ilan edilmez; action uncertain olur.
4. `FileWitness`: device/inode/owner UID, boyut/link sayısı, mtime/ctime nanosecond ve tam sabit içerik SHA-256. Gateway bu private runtime sonucunu dış tool sonucundan ayırır; `WriteReceipt` içine run/action referansları, immutable envelope hash'i ve workspace identity hash'i ekler.
5. Kanıt mevcut `observations` tablosunda `kind=filesystem.write_receipt` olarak **ayrı transaction'da** commit edilir. Sonra eski gateway action-result/ok transaction'ı gelir. Yeni SQL migration yoktur; normal bağımsız run verification yolu değişmez.

| Kesinti yeri | Kalıcı kayıt / inceleme |
|---|---|
| Yazma başlamadan | Intent/running olabilir; receipt yok, dosya olmayabilir |
| Yazma fsync sonrası, receipt commit öncesi | Dosya exact olabilir ama receipt yok; eşitlik kanıt yerine geçmez |
| Receipt commit sonrası, action-result commit öncesi | Receipt vardır; writer restart action'ı uncertain yapar; matching receipt sonucu bunu değiştirmez |
| Action-result commit sonrası, bağımsız verification öncesi | Action ok olabilir; run hâlâ doğrulanmış başarı değildir |

Receipt yazımında I/O/SQLite arızası başarı sonucu üretmez. Dosya etkisi geri alınmaz; action running veya uncertain olarak inceleme gerektirebilir. Tarihsel kanıtlar aynı UID/root'un değiştirebildiği yerel DB'dedir; imzalı attestation veya DB+filesystem atomik transaction değildir.

## Salt okunur inceleme

```bash
.venv/bin/python -m aos.recovery_effect \
  --database data/example.sqlite \
  --workspace data/example-workspace \
  --run-id RUN_ID \
  --action-id ACTION_ID \
  --deployment-sha256 EXPECTED_DEPLOYMENT_IDENTITY_SHA256
```

Kaynak ve workspace mevcut olmalıdır. Deployment digest'i tam engine identity içindir; [6m açıklaması](RECOVERY_CHECKPOINT.md) geçerlidir. CLI modeli yüklemez ve canlı deployment sağlığı iddia etmez. Workspace exclusive AOS kilidi alınır; tüm yol bileşenleri no-follow açılır. Aktif writer'ın workspace'i sahiplenilmez.

- 6m checkpoint denetimi kullanılır. İkinci frozen DB snapshot'ının hash'i ilk snapshot ile eşleşmelidir; arada herhangi bir DB değişirse fail-closed olur. Her snapshot mevcut 128 MiB/10 saniye sınırlarına tabidir; toplam süre tek snapshot süresi değildir.
- Action/run/step/task/runtime, arguments/idempotency, allowed decision, envelope hash ve tarihsel EXECUTE state/hash/lease bağı denetlenir. Sonraki revoked lease eski action'a taşınmaz; geçmiş state yalnız geçmiş bağı denetlemek içindir.
- Tek receipt gerekir; duplicate veya yanlış action/run/workspace/envelope bağı reddedilir. Receipt'li action yalnız running/uncertain/ok olabilir ve actual_option write_file olmalıdır.
- Mevcut `hello.txt` yeni descriptor'dan okunur, aynı bounded witness denetimleri yapılır ve **bütün file identity alanları** eski receipt ile eşleşmelidir. Sonunda workspace yolu da yeniden kontrol edilir. Aynı byte'ları yeniden yazmak veya yeni inode'a kopyalamak eşleşme sayılmaz; mtime geri alınsa bile ctime farkı reddedilir.

Sonuçlar:

- `receipt_missing`: exact dosya bulunsa bile kalıcı yazma kanıtı yoktur.
- `recorded_effect_matches`: kayıtlı witness ile inceleme anındaki bounded dosya nesnesi/sürümü/içeriği eşleşir.
- `file_not_matching`: receipt bağları geçerli olsa da mevcut dosya aynı değildir, yoktur veya güvenli okunamaz.

Rapor raw dosya/DB yolu, file inode/UID/timestamps, lease veya envelope içermez; hash referansları, action status'u ve minimized checkpoint verir. Bütün execution/resume/automatic replay/uncertain resolution bayrakları false kalır. Rapor execution capability olarak tüketilmez; exit 0 geçerli bir inceleme raporudur, görev başarısı değildir. Bozuk binding veya kaynakta generic stderr/exit 1 döner.

## Mahremiyet ve güven sınırları

Raw write receipt yerel DB'de kalır. `export_run` typed receipt'i doğrular ama export'tan çıkarır; normal `bytes_written` tool sonucu yeni private witness alanını içermez. Sabit örnek fixture yalnız şema provasıdır; gerçek inode/hash kanıtı diye sunulmaz. Raw DB backup doğal olarak receipt'i içerir ve önceki private backup kuralları geçerlidir.

Receipt, güvenilen host gateway'in gördüğü bir nesneye ilişkin kayıttır. Aynı UID/root, inode reuse, saatin/filesystem metadata'sının güvenilirliği ve advisory lock'u yok sayan yazıcı için mutlak garanti yoktur. Gözlem sonrası değişiklik engellenmez. İki frozen snapshot ve dosya gözlemi ortak bir transaction değildir; metadata denetimleri gözlenen yarışları reddeder. Exactly-once dış etki, genel action nedenselliği, insan review'u veya otomatik reconciliation iddia edilmez.

**Ayrı 6p akışı:** [açık yazma uzlaştırması](RECOVERY_RECONCILE.md), bu bounded kanıtı taze exact-request terminal onayıyla tüketerek eski action sonucunu atomik audit ile kaydeder. Bu salt okunur 6o komutunun davranışını değiştirmez. Receipt olmayan aralıklar kapalı kalır; 6n sınırı gevşetilmedi ve uzlaştırma resume yetkisi vermez.

## Kabul

`tests/test_recovery_effect.py`: beş ayrı gerçek subprocess crash noktası (write öncesi/sonrası, receipt öncesi/sonrası, result sonrası); source DB/WAL hash değişmezliği, restart uncertain, sıfır replay/verification, 6n red, dosya değiştirme/yeniden yazma/link/FIFO/oversize, envelope/state/receipt bağları, duplicate receipt, iki snapshot arası değişim, audit arızası, FD açıkken path replacement ve export minimizasyonu.

`AOS_REAL_TASK_TESTS=1 AOS_MODEL_PYTHON=/PATH/TO/INFERENCE/bin/python` gerçek pinned Decider kararı → yazma/receipt commit → `os._exit(73)` → writer restart → matching effect inspection kabulünü açar. Run başarıya çevrilmez: bir gerçek S1 çağrısı, uncertain action ve sıfır bağımsız verification beklenir. Kanıt ignored `data/effect-real-*` altındadır. Ölçümler [STATUS](STATUS.md) içindedir.
