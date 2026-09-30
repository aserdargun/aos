# W3 — sentetik form vaka kohortu için salt okunur denetim

`aos.site_skill_form_cohort`, kayıtlı bir S1 skill **taslağının** aynı yapısal plandaki tüm gelişim ve held-out vakalarını farklı, tamamlanmış `.invalid` HTTPS form koşularına bağlar. Her koşu için özel vaka parametreleri exact POST gövdesini yeniden üretir; `submitted_field_name` ile sonra-marker hash'i eşleşir. Var olan uzak form kaynak denetimi altı ayrı onay, tek POST, makbuz ve durum GET readback'i, gerçekten çağrılmış S1 kaynak olayları ve iki verification kaydını aynı frozen SQLite snapshot'ında yeniden denetler. Kaynak skill olay/doğrulama ID'leri gelişim koşularında bulunmalı, held-out koşularda tekrar kullanılmamalıdır. Farklı run ID, run referansı ve form planı zorunludur.

**Bu gerçek skill kabulü değildir.** Koşuları mevcut form görevleri yürütür, skill executor çalıştırmaz. Planın `held_out` etiketi ve farklı değer hash'leri semantik bağımsızlığı kanıtlamaz. Sonra-marker yalnız ilan edilen yanıtı doğrular; kalıcı uygulama sonucunu veya gerçek kullanıcı/tenant hesabını kanıtlamaz. Rapor `source_parameters_bound`, `skill_executed`, `site_outcome_verified`, `held_out_independence_verified`, `skill_validated`, `reviewed`, `activation_authorized` ve `training_ready` alanlarını daima `false` tutar. Public hedef veya S2 skill bu sentetik denetime kabul edilmez. Yeni site isteği, model çağrısı, izin, eğitim, registry veya aktivasyon yaratılmaz.

`schemas/site_skill_form_cohort_selection.schema.json` ve `schemas/site_skill_form_cohort_report.schema.json` canonical sözleşmelerdir. `examples/site_skill_form_cohort.json` yalnız sentetik **rapor örneğidir**, üç gerçek koşu kanıtı değildir. Seçim dosyası owner-only `0700` dizinde tek bağlantılı `0600` canonical JSON'dur; `schema_version`, `synthetic`, exact `skill_sha256`, `plan_sha256`, `case_inputs_sha256`, `field_binding_sha256` ve vaka anahtarına göre sıralı `runs` içerir. Her run'da `case_key`, `run_id` ve kayıtlı görevin exact `task`, `form_plan`, `state_plan` nesneleri vardır. Plan/cases ve field-binding dosyaları da özel canonical kaynak olmalıdır. URL veya özel parametre değerlerini git'e koymayın. Rapor değer veya URL taşımaz; düşük entropili hash'ler de tahmin edilebildiği için raporu özel tutun.

```sh
PYTHONPATH=src .venv/bin/python -m aos.site_skill_form_cohort \
  --database data/<private-trajectory.sqlite> \
  --profiles data/web-applications --pages data/site-knowledge --store data/site-skills \
  --plan data/<private-plan.json> --cases data/<private-case-inputs.json> \
  --field-bindings-file data/<private-field-bindings.json> \
  --selection data/<private-cohort-selection.json> \
  --selection-sha256 <exact-canonical-selection-sha256>
```

Başarılı komut yalnız kaynak durumu o an tutarlıysa içeriksiz rapor yazar. Owned ağsız Ubuntu/Chromium/Playwright MCP testinde üç ayrı parametreyle üç altı-onaylı gerçek pinli Decider koşusu, her birinde 6 S1/0 S2, ayrı POST ve durum/makbuz readback'i gözlendi. Tek snapshot denetimi ve özel CLI aynı raporu verdi; yanlış run, marker hash'i ve seçim hash'i reddedildi (**1 PASS / 28,438 s**). Bu fixture testi skill yürütmesi, gerçek hedef hesap/sonuç, semantik bağımsızlık veya aktivasyon değildir. Sonraki kabul izinli gerçek hedefte executable skill, bağımsız semantik sonuç, açık review/aktivasyon ve rollback'tir. W1–W6 ürün kabulü değişmez.
