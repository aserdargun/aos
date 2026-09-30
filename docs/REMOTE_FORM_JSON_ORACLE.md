# W1 — açık, görev sonrası JSON değer okuması

`aos.remote_form_json_oracle`, başarıyla bitmiş ve dört ayrı insan onayıyla denetlenmiş tek HTTPS form koşusundan **sonra**, aynı origin'de ayrı bir `GET` yapar. Yanıttaki yalnız bir üst düzey JSON scalar değerinin kullanıcının önceden bildirdiği SHA-256 ile eşleşmesini raporlar. Bu, formun makbuz HTML taşıma readback'inden ayrı bir gözlemdir; gerçek uygulama sonucunu veya hesabı kendi başına doğrulamaz.

Önkoşullar: kayıtlı exact profil ve form planı, tamamlanmış `browser_remote_form` run'ı, [W4 form kaynak denetiminin](REMOTE_FORM_LEARNING_SOURCE.md) geçmesi, profilin izinli public HTTPS origin'inde ayrı canonical bir JSON URL'si ve beklenecek scalar değerin yerel hash'i. Durum-planlı altı-onaylı koşular bu dilime dahil değildir. Çerezsiz koşuda cookie gönderilmez. Çerez bağlı public koşuda ise ancak kayıtlı run'ın **exact cookie SHA-256** seçimi ve aynı owner-private statik Cookie değeriyle ayrı opt-in host GET yapılabilir; varsayılan seçim reddeder.

Beklenen değeri terminal geçmişine yazmadan hash'lemek için örnek:

```sh
.venv/bin/python -c 'import getpass; from aos.contracts import digest; print(digest({"value": getpass.getpass("Expected value: ")}))'
```

Önce **ağsız önizleme** alın. URL'de sır, token, özel ID veya query kullanmayın:

```sh
.venv/bin/python -m aos.remote_form_json_oracle \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --profile-sha256 <exact-profile-sha256> \
  --form-plan-sha256 <exact-form-plan-sha256> \
  --url https://example.com/oracle \
  --field-key status \
  --expected-value-sha256 <local-value-sha256>
```

Önizlemedeki `plan_sha256` değerini ve URL'yi kontrol ettikten sonra aynı parametrelerle `--probe --confirm-plan-sha256 <exact-plan-sha256>` ekleyin. Çerezli koşuda **önizlemeye** `--cookie-sha256 <run-cookie-sha256>` ekleyin; **probe** için aynı seçime ek olarak `--cookie-file data/<owner-private-cookie-file>` kullanın. Dosya `data/` altında `0600`, dizini `0700` olmalı; değeri argv'ye veya repoya yazmayın. Dosya bayt hash'i planla eşleşmezse ağ isteği yoktur. Bu komut yeni bir görev veya tarayıcı eylemi başlatmaz; **ayrı bir host ağ isteği** yapar. Önce kaynak snapshot/plan/onay, sonra public DNS/IP ve TLS hostname, tek 200 yanıt, `application/json`, yönlendirmesiz/encoding'siz en çok 64 KiB, tekil JSON anahtarları ve scalar değer eşliği denetlenir. Cookie'nin response içinde sınırlı yansıması da reddedilir. Kaynak yeniden değişmişse kapalı kalır; başarısızlık form POST'unu tekrar etmez. Başarılı rapor yalnız hash/byte sayısı ve varsa cookie hash'i içerir, response gövdesini saklamaz.

`schemas/remote_form_json_oracle_plan.schema.json` ve `schemas/remote_form_json_oracle_report.schema.json` canonical; `examples/remote_form_json_oracle.json` açıkça sentetiktir. `declared_value_matched=true` yalnız bu ayrı GET'in **beyan edilen** değerle eşleştiğidir. Aynı statik Cookie'nin yeniden gönderilmesi, tarayıcı oturumunun devamı veya doğrulanmış hesap/tenant kimliği değildir. Yanıtın submit edilen form verisine ait olduğu, site-spesifik semantik sonuç, gerçek hedef veri hakkı, collector, eğitim veya W1 ürün kabulü ispatlanmaz; raporda `site_outcome_verified=false` ve `account_verified=false` kalır. Gerçek kabul için uygulamaya özgü, submit'e bağlanan bağımsız oracle ve izinli kullanıcı test girdisi gerekir.
