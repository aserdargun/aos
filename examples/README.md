# Sentetik örnekler

Tüm JSON/JSONL kayıtları elle oluşturulmuş **sentetik fixture**'dır. Runtime yürütülmüş veya modeller eğitilmiş gibi yorumlanamaz. `latency_ms: null` ölçüm yapılmadığını gösterir; probability değerleri örnektir.

- system1_choice.jsonl: doğru dosya eylemi, yanlış retry sonrası doğru escalation label'ı, insan policy review örneği.
- system2_supervisor.jsonl: yanlış input path kaynaklı hata ve doğrulanmış recovery formatı.
- S1 inventory-routing ve S2 ambiguous-source kayıtları offline builder için eklenmiş sentetik policy/human-required örnekleridir; gerçekleşmiş insan review'u değildir.
- dataset_review.json: fixture içerik hash'i, sentetik review durumu, görev ailesi ve elle tanımlı yakın-kopya kümesi.
- dataset_converter_pin.json: upstream Decider Example/Q ve prompt probe kaynak revision/hash'leri; model veya eğitim verisi içermez.
- dataset_audit.json: placeholder hash'li, açıkça sentetik read-only uygunluk raporu; training-ready değildir.
- dataset_review_receipts.json: placeholder source/candidate hash'li sentetik accept/revoke sözleşmeleri; gerçek izin değildir.
- trajectory_export.json: hello görevinin taşınabilir sanitize edilmiş tek-run örneği.
- evidence_catalog.json: örneklerin verification referanslarını çözen sentetik kanıt kayıtları.
- registry.json: hash/revision bekleyen disabled başlangıç kayıtları; deploy edilmez.
- browser_form.html: ağ ve gerçek submission içermeyen sentetik Message/receipt fixture'ı.
- browser_observation.json: typed symbolic DOM observation örneği; gerçek browser kaydı değildir.
- browser_export.json: pre-action observation ve structured verification içeren elle yazılmış export örneği.
- vision_canvas.html: semantik DOM button içermeyen, dış etkisiz sentetik görsel görev.
- vision_scene.json: capture-bound scene sözleşmesi için elle yazılmış kutular; gerçek Bonsai çıktısı değildir.

JSON Schema formatı doğrular. Probability sum, option membership, referans bütünlüğü ve sentetik işareti scripts/validate_package.py tarafından ek olarak denetlenir. Raw production veriyle bu fixture'ları karıştırma.
