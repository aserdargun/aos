# W3 — sentetik skill vakası ve denetlenmiş HTTPS form taşıması

`aos.site_skill_form_execution`, kayıtlı bir S1 skill taslağının **geliştirme** vakasındaki özel parametreleri mevcut exact form planı gövdesine bağlar ve aynı planın tamamlanmış bir temel `browser_remote_form` koşusunda gerçekten dört ayrı insan onayı, tek POST ve bağımsız makbuz readback'iyle kaydedildiğini yeniden denetler. Kapsam yalnız `.invalid` sentetik hedeflerdir; public/gerçek hedef veya held-out vaka bu rapora sentetik diye sokulamaz.

Taslakta ilan edilen bütün S1 kaynak olayları bu koşudaki pinli gerçek model olaylarının alt kümesi olmalı; özellikle submit kararına bağlı S1 olay kimliği bulunmalıdır. Taslağın tek kaynak verification ID'si aynı koşunun denetlenmiş makbuz verification ID'siyle eşleşmelidir. Denetim iki salt okunur trajectory snapshot'ının SHA-256 kimliğini karşılaştırır; arada kaynak değişirse rapor vermez. Taslak önceki veya başka bir run'ın olayını, yanlış task/plan/gövdeyi ya da değiştirilmiş onay/readback'i bu yolla başarı diye gösteremez.

Canonical rapor `schemas/site_skill_form_execution.schema.json`, açıkça sentetik örnek `examples/site_skill_form_execution.json` içindedir. Rapor yalnız hash, olay kimliği ve boolean sınırları taşır; değer, URL, DOM, prompt, model cevabı veya görüntü içermez. Düşük entropili plan/input hash'leri tahmin edilebilir olduğu için özel tutulmalıdır. CLI kaynak dosyaları owner-only `0700` dizin ve tek bağlantılı `0600` canonical JSON olarak okunur; hiçbir dosya, browser görevi veya izin oluşturmaz.

```sh
.venv/bin/python -m aos.site_skill_form_execution \
  --database data/<private-trajectory.sqlite> --run-id <completed-run-id> \
  --profiles data/web-applications --pages data/site-knowledge --store data/site-skills \
  --plan data/<private-plan.json> --plan-sha256 <exact-plan-hash> \
  --cases data/<private-case-inputs.json> --cases-sha256 <exact-input-hash> \
  --skill-sha256 <registered-skill-hash> --case-key <development-case-key> \
  --task-file data/<private-task.json> --task-sha256 <exact-task-hash> \
  --form-plan-file data/<private-form-plan.json> --form-plan-sha256 <exact-form-plan-hash> \
  --field-bindings-file data/<private-field-bindings.json> \
  --field-binding-sha256 <exact-field-bindings-hash>
```

`transport_execution_verified=true`, **skill'in çalıştırıldığı** veya parametrelerin model isteğinden türediği anlamına gelmez: mevcut sabit form görevi, skill executor değildir. `source_parameters_bound`, `page_readback_bound`, `skill_executed`, `site_outcome_verified`, `held_out_independence_verified`, `skill_validated`, `reviewed`, `activation_authorized` ve `training_ready` daima false kalır. Gerçek W3 kabulü için izinli hedefte sürümlü executable skill, farklı girdilerde bağımsız uygulama sonucu, kapsam retleri, ayrı review/aktivasyon ve rollback hâlâ gerekir. W1–W6 ürün kabulü değişmez.

Opsiyonel altı-onaylı sentetik durum koşusu için `--state-plan-file data/<private-state-plan.json> --state-plan-sha256 <exact-state-plan-hash>` birlikte verilir. Planın `submitted_field_name` alanı zorunludur: seçilen özel vaka parametresiyle exact form gövdesi ve beklenen sonra-marker metin hash'i ağsız eşleşmelidir. Kaynak denetimi ayrıca altı tüketilmiş onayı, tek POST'u, makbuz ve durum verification kayıtlarının ikisini, gerçekten çağrılmış S1 submit olayını ve sonra-marker readback'ini aynı frozen trajectory snapshot'ında yeniden doğrular. Skill taslağının `source_verification_ids` listesinde **iki** verification ID'si de olmalıdır. Rapor `audited_form_state_readback_candidate`, `state_plan_sha256`, içeriksiz `state_verification_ref` ve `submitted_value_readback_bound=true` ekler; temel dört-onaylı rapor değişmez. Eksik/yanlış plan veya yalnız bir verification kaynağı reddedilir. Bu bağ **kalıcı uygulama sonucu, skill yürütmesi, hesap/tenant doğrulaması, held-out bağımsızlığı, review veya aktivasyon değildir**. Owned `.invalid` TLS/Ubuntu/Chromium/MCP ve gerçek pinli Decider testinde özel CLI ile doğrulandı; gerçek siteye istek yapılmadı.
