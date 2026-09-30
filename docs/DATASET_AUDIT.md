# Salt okunur veri uygunluk envanteri — 7b

```bash
.venv/bin/python -m pip install -e '.[dataset]'
.venv/bin/python -m aos.dataset_audit --database data/aos.sqlite
```

Komut açıkça seçilen yerel SQLite veritabanını inceler, stdout'a sınırlı JSON raporu verir. UI endpoint'i değildir; recursive DB araması yapmaz. Raporu saklamak gerekiyorsa yalnız yerel ignored alanda private izin kullanın:

```bash
(umask 077; .venv/bin/python -m aos.dataset_audit \
  --database data/aos.sqlite > data/dataset-audit.json)
```

Shell yönlendirmesi mevcut raporu değiştirebilir; yeni dosya adı seçin. Hata halinde CLI sıfır olmayan kod ve içeriksiz genel hata verir, kısmi JSON raporu basmaz. Boş dosyayı başarı saymayın. Ham DB veya rapor dışarı gönderilmez; export/dataset build/eğitim/promote çalışmaz.

## Snapshot ve kaynak güvenliği

- Kaynak SQLite URI'si `mode=ro`, `query_only=ON`, `foreign_keys=ON`, `trusted_schema=OFF` ile açılır. URI path kaçışı korunur; dosya yoksa oluşturulmaz, final path symlink'i reddedilir. TrajectoryStore writer/reconcile, checkpoint, migration veya eligibility update çağrılmaz.
- Read transaction ilk schema okumasıyla sabitlenir; SQLite backup API bunu **yalnız bellekteki** ayrı DB'ye kopyalar. Committed WAL dahil, henüz commit edilmemiş değişiklikler hariçtir. Kaynak transaction backup sonrası bırakılır; kalan denetim frozen kopyadadır. Canlı WAL için `immutable=1` kullanılmaz. [Python backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup) ve [SQLite WAL read-only sınırları](https://www.sqlite.org/wal.html) temel alınır.
- Salt okunur uygulama sorguları kaynak kayıtlarını değiştirmez. SQLite, WAL okuması için kendi `-shm` koordinasyon/yan dosyalarına ihtiyaç duyabilir veya bunları oluşturabilir; dosya sistemi metadata'sının tamamen değişmeyeceği iddia edilmez. Snapshot SHA-256, bellekteki backup'ın serialized byte'larıdır; ana DB dosyasının hash'iyle aynı olmak zorunda değildir. `captured_at` snapshot'ın alınma zamanıdır, event timestamp filtresi değildir.
- **v7–v12** schema/migration profili birebir doğrulanır; trigger/view/ek index dahil schema drift ve diğer sürümler reddedilir. Karşılaştırma için migration'lar yalnız boş bellek DB'sinde uygulanır; kaynak güncellenmez. Integrity ve FK kontrolleri başarısızsa rapor üretilmez. 7c append-only receipt tablosu için [DATASET_REVIEWS](DATASET_REVIEWS.md).
- Snapshot 128 MiB, run sayısı 10.000 ile sınırlıdır. SQLite sorguları ve backup için 10 saniyelik deadline kontrolü, lock beklemesi için 1 saniye kullanılır; bu process-level hard realtime garantisi değildir. Daha büyük kaynaklar için ayrı streaming/denetlenmiş snapshot tasarımı gerekir. Diskte ham snapshot oluşturulmaz; ham içerik OS process belleğinde geçici bulunur.

## Neler raporlanır?

`schemas/dataset_audit.schema.json`: snapshot hash/boyut/zaman, schema ve migration hash'leri, auditor hash'i, run sayıları, flag envanteri, review/action/verification/artifact sayaçları ve sabit engel kodları. Okuyucu artık `sqlite-eligibility-v2` üretir; önceki v1 fixture'ı da kabul edilir. Run kimliği yalnız deterministik SHA-256 `run_ref` olarak çıkar; bu anonimleştirme garantisi değildir. ID'sini bilen kişi hash'i eşleştirebilir. Dosya yolu, hedef, gözlem, seçenek metni, model cevabı, credential, artifact path/byte veya ham label çıktıya alınmaz.

| Denetim | Anlam |
|---|---|
| `run_not_opted_in` | Kaynaktaki `training_eligible=0`; flag değiştirilmez |
| `synthetic_content` | Hello/browser/vision bounded policy'si sentetik içeriklidir; gerçek inference bunu değiştirmez |
| `content_provenance_unclassified` | Diğer policy'ler otomatik gerçek/sentetik sınıflandırılmaz |
| `usage_rights_unrecorded`, `redaction_review_unrecorded` | v7 kaynağı veya v8–v12'de receipt bulunmayan run |
| `dataset_reviews_require_validation` | v8–v12 receipt kaydı var; candidate/authority doğrulaması yapılmadan izin varsayılmaz |
| `run_not_verified`, `no_passed_verification` | Succeeded/passed durumu veya passed verification eksik |
| `incomplete_actions` | Intent/running/uncertain eylem vardır; yeniden yürütülmez |
| `no_accepted_labels`, `unreviewed_labels` | Accepted label yok veya pending/rejected label var |
| `invalid_choice_labels`, `unsupported_label_types` | Dar choice-link denetimi başarısız ya da policy/supervisor label denetimi bu sürümde desteklenmiyor |
| `unredacted_artifacts`, `image_artifacts_excluded` | Raw/quarantined artifact veya image metadata'sı var; dosyalar açılmaz |

Dar accepted choice denetimi: `source=verified_outcome`, aynı run/step decision ve verification bağı, gold'un 2–10 unique option içinde olması, bilinen method/verifier çifti, passed ve canonical expected/actual eşitliği, başarılı action'ın aynı decision ve actual option'a bağlı olması, nonempty evidence referanslarının aynı run/step içinde tekil çözülmesi. Independent readback ayrı action olabilir; evidence için yanlışlıkla write action_id eşitliği zorlanmaz.

`choice_labels_linked` yalnız **yapısal ilişki kontrolüdür**; gerçek dünyanın sonucunu tekrar doğrulamaz, tüm canonical training şemasını veya probability kalibrasyonunu kontrol etmez, içeriği sanitize etmez. Policy/supervisor review kaynakları otomatik choice kabulüne çevrilmez. UI eylem onayı dataset kullanım izni değildir. Environment/model metadata içindeki `usage_rights`/`synthetic` gibi serbest alanlar yetki sağlamaz.

**Her run `training_ready=false`, rapor `ready_runs=0` kalır.** Flag=1, linked accepted label veya receipt varlığı bile provenance/rights/redaction kapılarını açmaz. Bu milestone bir envanterdir; gerçek veri seçme veya eğitim yetkilendirme servisi değildir. 7a sentetik builder ile otomatik bağlantısı yoktur; bu rapor onun girdi dosyası değildir.

## Kabul

```bash
.venv/bin/python -m unittest discover -s tests -p test_dataset_audit.py -v
```

Testler sentetik fixture DB kullanır: WAL/commit snapshot tutarlılığı, eşzamanlı writer sonrası frozen görünüm, query-only yazma reddi, no-reconcile/no-create, DB/WAL byte değişmezliği, URI kaçışı, secret minimizasyonu, review/label/evidence/gold, raw image metadata'sı, schema/FK bozukluğu, kaynak limitleri ve CLI hataları. `examples/dataset_audit.json` yalnız sentetik, placeholder hash'li rapor sözleşmesi örneğidir. Gerçek yerel kabul DB'siyle yapılan ayrı audit ve test sayıları STATUS içinde tutulur.
