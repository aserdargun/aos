# Eğitim mimarisi

İlk günden trajectory toplanır; eğitim yeterli ve doğrulanmış veri oluştuğunda offline yürütülür. Ajan çalışma sırasında kendi ağırlıklarını değiştirmez.

```text
Gerçek görev → SQLite trajectory → outcome verification
 → insan/Supervisor düzeltmesi → redaction → normalization
 → label construction → dedup/group split → quality checks
 → immutable JSONL dataset → training → held-out evaluation
 → candidate registry → açık promotion veya reject
```

İki hedef ayrıdır: System-1 `state + finite options → correct choice`; System-2 `problem + evidence + failed trajectory → diagnosis + corrected plan`. Supervisor düzeltmesi ancak sonucu doğrulandıktan veya yetkili reviewer tarafından açıkça değerlendirildikten sonra label adayıdır. Modelin kendi cevabını beğenmesi başarı kanıtı değildir.

Runtime veriyi üretir; dataset builder yalnız eligible veriyi seçer; trainer candidate üretir; evaluator bağımsız held-out set kullanır; registry yalnız açık promotion ile aktif eder. Bu sorumluluklar ayrı process olmak zorunda değildir ama ayrı interface ve audit kaydı olmalıdır.

`examples/` tamamen sentetiktir. Şemayı ve parser'ı öğretir; gerçek eğitim hacmi, başarı veya bilimsel sonuç değildir. İlk recipe'ler synthetic=false filtresi kullanır. Sentetik eğitim daha sonra ayrı provenans, review ve açık recipe değişikliğiyle değerlendirilebilir.

Önce base modellerle gerçek benchmark kur. Daha sonra Decider için tool/action selection, escalation, safety; Bonsai için failure recovery, diagnosis, revised plan üzerine dar deneyler yap. Türkçe input normalizasyonu ayrı regresyon testidir; ilk Decider dataset'i İngilizce canonical state kullanır.
