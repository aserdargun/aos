# Eşli taban–adapter tarayıcı değerlendirmesi

Bu dilim, aynı yeni sentetik gelişim girdisinde taban Decider ve deneysel S1 adapter'ını iki gerçek tarayıcı koşusunda karşılaştırır. Uygulama ile gerçek kabul ayrı izlenir: güncel kanıt [STATUS](STATUS.md) içindedir. Tek fixture ailesi tek leakage grubudur; held-out, istatistiksel üstünlük, gerçek-site başarısı veya promotion değildir.

## Tasks akışı

1. Owned-reuse oturumunda izinli episode'u, iki-rol review/export/conversion/tokenizer ve ayrı deneysel eğitimi tamamlayın. Kayıtlı deneyi doğrulayın.
2. **Paired base–adapter browser evaluation** altında yeni sentetik mesaj ve vaka girin; gizli bilgi kullanmayın. **Preview pair**, aynı skill/release/selection, giriş, form ve durum planını iki kol için sabitler. Önizleme hiçbir modeli veya görevi başlatmaz.
3. Varsayılan kapalı değerlendirme iznini seçip **Commit pair** kullanın. Bu immutable özel precommit dosyasını kaydeder; çalıştırma izni değildir. Oturumun dört selected-start kotasında iki yer kalmalıdır; kota artırılmaz.
4. Ayrı varsayılan kapalı izni seçip **Start base arm** kullanın. Altı eylemi Tasks'ta ayrı ayrı onaylayın; görev bitince **Audit paired runs** seçin.
5. Yalnız doğrulanmış taban kolundan sonra, ayrı izinle **Start adapter arm** kullanın. Altı eylemi ayrıca onaylayıp tekrar denetleyin. Sonraki kol hiçbir zaman otomatik başlamaz. Kontrol/devretme/lease değişimi eski izni geçersiz kılar.

Her kol yeni model worker'ı ve yeni tek-kullanımlık owned form fixture'ı kullanır. Ana base engine değiştirilmez; yalnız owning engine boşaltılır. Diğer GPU süreçlerine dokunulmaz. İki kolda aynı case/value/form/state/recipe pinleri vardır; execution/admission/deployment kimlikleri farklıdır. Adapter preview schema-1.5, base preview schema-1.3 kalır.

## Kanıt ve ölçüm

- `owned_adapter_pair` precommit kaydı özel episode kökünde içerik-adresli ve değişmezdir. Ayrı attempt dosyası start çağrısından önce yazılır; hata tekrar çalıştırma izni vermez. Started receipt exact job/execution bağını saklar.
- İlk model çağrısından ve eylemden önce `model.owned_adapter_evaluation` observation'ı pair/arm/preview/deployment/controller kimliklerini bağlar. Her eylemde güncel source, precommit, attempt ve receipt tekrar denetlenir.
- Salt okunur rapor aynı frozen SQLite snapshot'ında iki normal kaynak audit'ini, pair marker'larını, gerçek session/lease/runtime bağlarını ve base→adapter sırasını doğrular. Her kolda altı model kararı/onay, altı onaylı eyleme ek deterministik receipt readback ile yedi eylem ve iki sonuç doğrulaması vardır. Adapter'da altı geçerli hook/registry kanıtı, base'de sıfır adapter kanıtı gerekir.
- `call_latency_sum_ms`: kaydedilmiş `engine.decide` çağrılarının toplam duvar süresi; soğuk yükleme ve kaynak denetimi dahildir. Saf GPU/inference zamanı **değildir**.
- `approval_window_sum_ms`: altı onayın oluşturulma→tüketilme aralıklarının toplamı; saf insan bekleme süresi **değildir**.
- İki ham değer ve adapter eksi base farkı verilir. Tek çiftten p95, genelleme veya üstünlük çıkarılmaz. Eksik/başarısız kol görünür, tamamlanmış çift gibi sayılmaz.
- Geçmiş karşılaştırma yeni çalışma izni değildir. Kaynak sonradan geri çekilse bile geçmiş kanıt denetlenebilir; yeni başlangıç güncel kaynağı yeniden doğrular. `runtime_reuse_authorized=false`, `promotion_authorized=false` kalır.

## API ve sınırlar

Authenticated `/api/tasks/owned-episode/pair-preview`, `pair-commit`, `pair-start`, `pair-report` exact gövde ve 2048 bayt sınırını kullanır. Preview/commit/start güncel lease/generation ister; rapor yalnız idle scheduler'da salt okunur çalışır. Her kol için açık runtime onayı ve exact pair hash gerekir. Generic task veya adapter start, pair kanıtı uyduramaz.

UI reload sonrası kayıtlı pair seçimi, çoklu vaka/tekrar istatistiği, sıra dengelemesi, bağımsız test ailesi, S2 eğitimi, üretim aktivasyonu ve gerçek hedef uygulama kabulü bu dilimde yoktur. Aynı-girdi mekanik karşılaştırması bu eksiklerin yerine geçmez.
