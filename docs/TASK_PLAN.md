# Sabit Görev Planı — 6i

`aos.task_plan`, mevcut üç görev için deterministik, salt okunur plan kataloğudur. Bonsai plan üretmez, Decider çağrılmaz; scheduler, gateway ve DB değiştirilmez. Genel görev planlayıcısı, canlı ilerleme veya checkpoint değildir.

## Kullanım

```bash
.venv/bin/python -m aos.task_plan --goal 'yerel form görevini hazırla'
```

Authenticated `POST /api/tasks/plan` yalnız `{"goal":"yerel form görevini hazırla"}` kabul eder. Cookie/Host/Origin ve mevcut bounded body kuralları korunur. Query, client plan, action, lease veya scope alanı alınmaz. Motor kapalıyken de katalog gösterilebilir; bu runtime kullanılabilirliği değildir.

React **Plan** paneli explicit gönderimle aynı endpoint'i kullanır. Input değişiminde, panel kapanmasında ve logout'ta in-flight istek iptal edilir; eski cevap gösterilmez. Local loading kalıcı kontrol çubuğunu kilitlemez. Başlatma/onay düğmesi yoktur; **Görevler** panelinin bağımsız seçimi değişmez.

## Typed sözleşme

`schemas/task_plan.schema.json` / `PlanPreview` canonical rapor biçimidir; `examples/task_plan.json` yalnız sentetik katalog örnekleridir. Şema yürütme/canlılık iddialarını ve bilinmeyen alanları reddeder. Python typed validator ayrıca **tam katalog eşitliğini**, intent/plan bağını ve SHA-256'yı denetler; adımlar veya scope değiştirilip yeniden hash'lense de reddedilir. JSON Schema yapısal kontrolü tek başına bu semantik katalog kontrolünün yerine geçmez.

- Girdi mevcut `bounded-tr-v1` normalizer'dan geçer. Olumsuzluk, bilinmeyen/ek komut, kontrol veya opaque metin için plan/hash yoktur. Ham bilinmeyen girdi response'a yansıtılmaz.
- Üç şablon `fixed-plan-v1` sürümündedir; goal/scope, execution runtime sınıfı, önkoşullar, sıralı observe/decide/act/verify adımları ve beklenen bağımsız verification sonucu içerir.
- Hello: dosya yoksa exact newline dahil sabit içerik; eş içerikte yeniden yazmak yerine okuma; farklı içerik üzerine yazılmaz. Seçilen gerçek eylem taze onay ister, ardından bağımsız readback yapılır.
- Form: fill → bağımsız alan kontrolü → yeni DOM/karar → ayrı submit onayı → receipt/submissions doğrulaması. İlk onay ikinci eyleme taşınmaz. Ağ veya başka sayfa yetkisi yoktur.
- Canvas: taze capture/typed scene → finite symbolic seçim → capture-bound onaylı click → bağımsız selected=SAVE/clicks=1 sonucu. Browser/vision ayrı ağsız headless runtime'dır; noVNC masaüstüsü değildir.
- Beklenen değer JSON metnidir, ölçülmüş sonuç veya kanıt değildir. Adım numaraları yalnız katalog sırasıdır; run/step/action kimlikleri değildir. Model/fixture ayrımı gerçek execution konfigürasyonunda yapılır.
- Plan SHA-256 bir içerik bağıdır; imza, onay, execution capability veya model çalıştırma kanıtı değildir. Normalizer'ın orijinal input hash'i ayrı kalır; alias'lar aynı planı üretir.

## Yetki ve kapsam

`execution_authorized=false`, `live_state_verified=false`, `requires_action_approval=true`. Endpoint hiçbir planı yürütme için almaz. `/api/tasks` hâlâ yalnız sabit kind + güncel lease/generation alır; plan raporu burada reddedilir. Her gerçek eylem için mevcut runtime/state/policy/digest/expiry/one-use approval denetimleri geçerlidir. Önizlemede belirtilen readback ayrı yeni mutation yetkisi değildir.

Migration, dependency veya model pin değişikliği yoktur. Genel Türkçe normalizer, Supervisor-driven çok adımlı scheduler, çökme sonrası devam, genel web/Office ve eğitim ayrı işlerdir. Güncel gerçek test kanıtları [STATUS](STATUS.md) içindedir.
