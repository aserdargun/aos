# W1 — gönderilen form değerinin ayrı JSON okuması

`aos.remote_form_json_submission`, bitmiş ve denetlenmiş **dört-onaylı** HTTPS form koşusunda POST edilen gövdenin exact SHA-256/bayt sayısını, operatörün özel dosyadaki alan değerleriyle yeniden eşleştirir. Seçilen tek alanın string değerinden beklenen JSON hash'ini kendisi türetir; operatör keyfi bir “beklenen sonuç” hash'i sağlayamaz. Ardından [görev sonrası JSON oracle](REMOTE_FORM_JSON_ORACLE.md) ile ayrı aynı-origin public TLS `GET` yapar ve üst düzey JSON scalar alanının **aynı gönderilmiş string değer** olduğunu doğrular. Çerez bağlı public formda kayıtlı statik Cookie SHA-256 ve aynı özel Cookie dosyası ayrıca gerekir; çerezsiz akış değişmez.

Bu **ağsız önizleme** için önceki form görevinde kullanılan değerin owner-private `0600` dosyası gerekir. Dosya veya onu içeren dizin `data/` altında olmalıdır. Tek alanlı form için dosya ham UTF-8 değeri içerir; sondaki newline bile POST gövdesini değiştireceğinden plan reddedilir. Çok alanlı form için onboarding'in canonical `--form-fields-file` belgesi kullanılır. CLI dosya içeriğini, cookie'yi veya yanıt gövdesini yazdırmaz.

Önizleme, kaynak denetimi ve form bağını birer tutarlı SQLite snapshot'ında okur; ikinci bir oracle önizlemesiyle aynı iki denetimi yinelemez. Ağlı probe ise kaynak ve özel alanları yeniden denetler; bu hız değişikliği ayrı plan onayını veya ağ öncesi fail-closed sınırını kaldırmaz.

```sh
.venv/bin/python -m aos.remote_form_json_submission \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --profile-sha256 <exact-profile-sha256> \
  --form-plan-sha256 <exact-form-plan-sha256> \
  --url https://example.com/oracle \
  --field-key message \
  --submitted-field-name message \
  --form-value-file data/<private-value-file>
```

Çok alanlı koşuda `--form-value-file` yerine `--form-fields-file data/<private-fields-file>` seçin. Çerezli koşuda önizlemeye `--cookie-sha256 <run-cookie-sha256>` ekleyin. Önizlemedeki **bütün** URL/alan/body/cookie hash'lerini denetleyin; sonra aynı parametrelerle `--probe --confirm-plan-sha256 <exact-plan-sha256>` çalıştırın. Çerezli probe'a ayrıca `--cookie-file data/<private-cookie-file>` ekleyin. Cookie değeri komut satırına girilmez. Yanlış plan onayı, değişmiş private form girdisi, yanlış çerez veya değişmiş denetlenmiş kaynak **ağdan önce** reddedilir. Timeout/ret sonrası form POST'u tekrar edilmez.

Canonical sözleşmeler `schemas/remote_form_json_submission_plan.schema.json` ve `schemas/remote_form_json_submission_report.schema.json`; `examples/remote_form_json_submission.json` açıkça sentetiktir. Başarılı `submitted_value_readback_bound=true`, sadece ayrı JSON GET'te POST gövdesi hash'iyle bağlı alan değerinin görüldüğünü ispatlar. Yanıtın kalıcı kayıt, doğru tenant/hesap veya gerçek iş semantiği olduğunu ispatlamaz; `account_verified=false`, `site_outcome_verified=false`, `collection_authorized=false` ve `training_ready=false` kalır. Host GET, görünür Chromium'un oturumunu devam ettirmez; yalnız izinli static Cookie değerini yeniden gönderir. Gerçek W1 kabulü için seçilmiş hedef uygulama ve submit'e özgü bağımsız başarı oracle'ı hâlâ gerekir.
