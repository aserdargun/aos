# W4 — HTTPS form için ayrı izinli metadata akışı

Temel `browser_remote_form` görevi için `remote_form_model_metadata_only` kapsamı, rota/statik/JSON izinlerinden ayrıdır. Yerel operatör, ilk manuel görev onayı beklerken mevcut run, kayıtlı profil, exact form planı, istenen S1/S2 rolleri ve en çok 24 saatlik bitiş için ağsız önizleme alır; aynı SHA-256 değerini ikinci çağrıda onaylayınca özel `0600` izin dosyası yazılır. Profilin `data_rights_ref` alanı ve operatör beyanı **harici veri hakkının veya hesabın bağımsız kanıtı değildir**.

```sh
.venv/bin/python -m aos.remote_learning_consent \
  --database data/<private-trajectory.sqlite> --run-id <bound-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-form-plan-sha256> \
  --scope remote_form_model_metadata_only --roles system1 system2 \
  --expires-at <UTC-ISO8601-plus-00:00> --attest-data-rights \
  --store data/remote-learning-consents
# Önizleme hash'ini inceleyip aynı komuta --confirm-sha256 <exact-hash> ekleyin.

.venv/bin/python -m aos.remote_form_learning_stream \
  --database data/<private-trajectory.sqlite> \
  --profiles data/web-applications \
  --consents data/remote-learning-consents \
  --consent-sha256 <exact-consent-sha256> \
  --outbox-dir data/remote-learning-outbox \
  --watch-seconds 300 --interval-seconds 1
```

Poller yalnız ayrı onayı tüketilmiş, başarılı form aşamalarına bağlı gerçek pinli model olaylarını içeriksiz ve tekrar kaydetmeden özel append-only outbox'a alır. İlk onaydan önce sıfır giriş vardır; her aşama için ayrı onay, karar, authenticated insan kaydı ve model kimliği yeniden denetlenir. Son aşama bile stream'de `transport_readback_verified=false` kalır; ayrı [tamamlanmış kaynak denetimi](REMOTE_FORM_LEARNING_SOURCE.md) taşıma readback'ini doğrulayabilir ama bunu uygulama sonucu veya gold etikete çeviremez.

Altı-onaylı durum planında ayrı `remote_form_state_model_metadata_only` kapsamı ve zorunlu `--selected-state-plan-sha256 <exact-state-plan-sha256>` kullanılır; aynı CLI poller ve izin başına özel outbox geçerlidir. Temel form izni durum run'ında, durum izni temel run'da ve yanlış state-plan hash'i yeniden kayıt/attach/poll sırasında reddedilir. State-before ve state-after dahil altı aşama ayrı indekslenir; state hash'i entry/report içinde bulunur, HTML veya marker metni bulunmaz. Cookie bağlı formlar iki kapsamın da dışındadır.

Temel ve durum modlarının ayrı `schemas/remote_form{,_state}_learning_stream_{entry,report}.schema.json` sözleşmeleri ve açıkça sentetik `examples/remote_form{,_state}_learning_stream.json` sözleşmeyi belirler. URL, form değeri, DOM, prompt, model yanıtı, screenshot, cookie ve token outbox'a yazılmaz; bağlantılı hash'ler özel kalmalıdır. Host CLI izlemesi en çok beş dakikadır ve yeniden başlatılabilir. Yeni temel veya durum-planlı form backend oturumunda authenticated Tasks paneli ilk `browser.form.open` onayı beklerken aynı iki aşamalı izin kaydını ve ayrıca exact hash bağlamayı sunar. **Kayıt tek başına toplama başlatmaz.** Ayrı attach işleminden sonra scheduler güvenli onay öncesi ve settle noktalarında aynı poller'ı çağırır; metadata hatası görev sonucundan ayrı görünür. Sonraki form aşamasında yeni attach reddedilir. Cookie planlarında bu panel ve otomatik yol kapalıdır. Çalışan eski backend hot-upgrade edilmez; güvenli yeni oturum gerekir.

Açık [iptal ve retention](REMOTE_LEARNING_CONSENT.md) aynı exact izin/outbox çiftine uygulanır; yedeklerin fiziksel silinmesi garanti edilmez. Owned sentetik TLS/Ubuntu/Chromium/Playwright MCP ve gerçek pinli Decider testleri temel dört aşamada otomatik **0→4 S1**, durum planlı altı aşamada **0→6 S1**, her ikisinde de sıfır S2 kaydını, tekrar-poll dedup ve settled durumunu doğrular. Ayrı mocksuz EN/TR Tasks testi ilk onay beklerken preview→exact kayıt→attach→ret akışında POST öncesi sıfır ağ isteği gösterir. Bunlar gerçek hedef-site, harici hak, redaction, site sonucu, reviewed dataset, eğitim veya W1–W6 kabulü değildir.
