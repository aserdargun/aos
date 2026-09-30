# W4 — HTTPS form görevi için salt okunur kaynak denetimi

`aos.remote_form_learning_source`, tamamlanmış tek bir `browser_remote_form` koşusunu kayıtlı profil ve exact form planı SHA-256 değerleriyle karşılaştırır. Değişmez trajectory snapshot'ında job/run/runtime/draft/plan bağı, dört ayrı tüketilmiş eylem onayı, bunlara ait authenticated insan müdahaleleri, sıralı form eylemleri, tek POST ve bağımsız makbuz readback'i yeniden denetlenir. Yalnız profilde talep edilmiş rollere ait, gerçekten pinli S1 kararlarına veya bağlı S2 escalation'larına ait içeriksiz öğrenme olayı kimlikleri raporlanır. Bonsai çağrılmadıysa S2 listesi boş kalır.

```sh
.venv/bin/python -m aos.remote_form_learning_source \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-form-plan-sha256>
```

`schemas/remote_form_learning_source.schema.json` canonical temel rapor sözleşmesidir; `examples/remote_form_learning_source.json` açıkça sentetiktir. Rapor ham form değeri, URL, DOM, screenshot, prompt, model cevabı, cookie değeri veya token içermez. CLI ağ isteği yapmaz, outbox/dataset yazmaz ve yeni görev/onay başlatmaz. Varsayılan seçim yalnız dört onaylı temel form planını kapsar; durum planlı run ayrı exact state-plan hash'i ve [altı-aşama kaynak denetimi](REMOTE_FORM_STATE_LEARNING_SOURCE.md) gerektirir.

Çerez bağlı public HTTPS form koşusu **varsayılan seçimde reddedilir**. Operatör ayrı `--selected-cookie-sha256 <exact-run-cookie-hash>` eklerse migration 0016'daki job/run/plan/runtime/çerez hash'i, dört eylemin ve bağımsız gözlemin cookie-hash bağlı argümanlarıyla birlikte yeniden denetlenir. Sonuç farklı `read_only_remote_form_cookie_source` modudur; yalnız çerez hash'i raporlanır, değeri okunmaz veya yazılmaz. Bu mod gerçek model olaylarını içeride denetlese de olay ID'lerini öğrenme adayı olarak dışarı vermez. Durum-planı + çerez birlikte bu kaynak yolunda desteklenmez. Canonical sözleşme `schemas/remote_form_cookie_learning_source.schema.json`, örnek `examples/remote_form_cookie_learning_source.json` içindedir. Bu kaynak modu öğrenme izni vermez; mevcut metadata consent/stream çerezli koşuyu reddetmeye devam eder.

Makbuz readback'i bir **taşıma doğrulamasıdır**: site semantiği, hesap/tenant, bağımsız uygulama sonucu, harici veri hakkı, redaction, veri toplama ve eğitim hazır oluşu doğrulanmaz. Çerez bağlı run, hash seçilmedikçe kaynak projection'ından açıkça reddedilir; seçildiğinde de hesap doğrulandı denmez. Fixture motoruyla geçen owned TLS/Ubuntu/Chromium/MCP testi gerçek Decider/Bonsai veya gerçek hedef-site sonucu sayılmaz. Gerçek W1–W6 kabulü açık kalır.

Ayrı [izinli form metadata akışı](REMOTE_FORM_LEARNING_STREAM.md) görev sürerken yalnız onaylı aşamaların gerçek model olaylarını özel outbox'a alabilir; bu tamamlanmış-run denetiminin yetki bayraklarını değiştirmez. Owned gerçek pinli Decider testinde dört S1 olayı ve sıfır S2 olayı görüldü; hedef yine sentetik TLS fixture idi.
