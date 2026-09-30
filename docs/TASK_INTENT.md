# Sınırlı Türkçe görev önizlemesi — 6d–6e

`aos.task_intent` yalnız mevcut üç sentetik görev için deterministik İngilizce hedef şablonu seçer. Genel çeviri, LLM planlama veya görev başlatma değildir. `execution_authorized=false` ve `requires_action_approval=true` değişmezdir. Operator hedefleriyle aynı sabitleri kullanır; model/runtime/deployment kimliği değişmez.

```bash
.venv/bin/python -m aos.task_intent --goal 'hello görevini hazırla'
.venv/bin/python -m aos.task_intent --goal 'yerel form görevini önizle'
.venv/bin/python -m aos.task_intent --goal 'görsel save görevini hazırla'
```

Desteklenen alias'lar `src/aos/task_intent.py` içinde açık katalogdur. Türkçe I/İ dönüşümü yalnız komut sözcüklerine uygulanır. Ayrıca `/workspace/hello.txt dosyasını oluştur ve doğrula` veya `/workspace/hello.txt dosyasına "Hello from the local agent.\n" yaz ve doğrula` tanınır. Son örnekte `\n` iki literal karakterdir; hedef dosya içeriğinde gerçek satır sonunu temsil eden sabit şablon seçilir. Dosya yolu ve tırnak içeriği değiştirilmez; farklı harf, path veya içerik reddedilir.

- Yalnız tam katalog eşleşmesi `recognized` olur. Ek talimat/izin/shell/path veya bilinmeyen cümle için tahmin yapılmaz.
- Tanınan olumsuzluk ve kontrol ifadeleri `negated` döner; bilinmeyen olumsuz biçimler `unsupported` kalır, eyleme dönüşmez. Kontrol komutları bu API üzerinden uygulanmaz.
- Boş, string olmayan veya 1000 karakteri aşan girdiler hatadır. C0/bidi kontrol karakterleri `opaque_text` olarak destek dışıdır.
- Özgün metnin canonical JSON SHA-256 kimliği tutulur; raw giriş çıktı/log/DB'ye yazılmaz. Hash anonimleştirme garantisi değildir ve kısa metinler tahmin edilebilir. Komut satırı girdisi shell geçmişinde/process listesinde görünebilir; secret girmeyin.
- Çıktı yalnız sabit İngilizce hedef/scope içerir; bir eylem listesi, imzalı capability veya yürütülebilir plan değildir.

Authenticated konsolda `POST /api/tasks/preview`, yalnız `{"goal":"hello görevini hazırla"}` kabul eder. Mevcut cookie/Host/Origin ve 4096-byte body sınırları geçerlidir. Preview motor kapalıyken de kataloğu tanıyabilir; tanınma, görev runtime'ının kullanılabilir olduğu anlamına gelmez. Scheduler/controller/DB/runtime değiştirilmez; lease veya approval oluşturulmaz. `/api/tasks` yalnız mevcut kind/lease/generation sözleşmesini kabul eder ve her gerçek eylem ayrıca onay ister. Preview cevabı bu endpoint'e görev olarak gönderilemez.

6e, React **Görevler → Türkçe görev önizlemesi** formunu ekler. “Yalnız önizle” cevabı gösterir; mevcut görev seçimini otomatik değiştirmez ve başlatma endpoint'ini çağırmaz. Başlatma ayrı katalog/lease akışında kalır. Metin değişince sonuç temizlenir ve in-flight istek iptal edilir; panel unmount/logout sonrası cevap yeniden görünmez. Giriş localStorage/DB'ye yazılmaz. Hatalar mevcut konsol error/session mekanizmasını kullanır.

Şema `schemas/task_intent.schema.json`, sentetik örnekler `examples/task_intent.json` içindedir. Unit/API kabulü kapsam, negation, path/content değişmezliği, yetki genişletmeme ve yan etkisizliği denetler. Playwright kabulü gerçek konsolda login → tanınan/olumsuz/destek dışı önizleme → görev seçimi ve boş job listesi değişmezliği → ayrı hello başlat/onay-reddet → logout akışını sınar. 1440×1000 ve 390×844 görünüm kontrolü vardır; model motoru fixture'dır. Genel Chat/Plan, serbest hedef yürütümü, çok adımlı planner veya çökme sonrası devam sağlanmaz.
