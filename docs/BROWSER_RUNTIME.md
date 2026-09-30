# İzole Chromium / DOM kabulü

6b görev entegrasyonu bu aynı bounded runtime'ı UI scheduler'ına bağlar: `serve_desktop.py --engine decider --browser-tasks`. Fill ve submit ayrı onay ister; takeover runtime'ı kapatır ve sonraki görev temiz instance açar. Bu varsayılan headless yol Docker/noVNC tarayıcısı değildir. Yeni `--desktop-browser` seçimi sabit formu aynı görünen Docker masaüstünde çalıştırır; [görünür form sınırları](VISIBLE_BROWSER.md) ayrıdır. CLI komutları değişmez.

Bu dilim yalnız `examples/browser_form.html` içindeki **sentetik yerel formu** destekler. Genel web gezintisi, gerçek hesap/gönderim, keyfi selector/JavaScript, vision veya tam masaüstü değildir. Hedef: Message alanına tam `Hello from the local agent.` yaz, Save locally düğmesine bir kez bas, alanı ve yerel receipt'i ayrı DOM okumasıyla doğrula.

## Hazırlık ve çalıştırma

Linux, çalışan Bubblewrap user/network/PID namespace'leri ve `/usr` altında Python gereklidir. Host Chromium/profili kullanılmaz. Uygulama extra'sı ve indirme açık hazırlık adımlarıdır:

```bash
.venv/bin/python -m pip install -e '.[browser]'
.venv/bin/python scripts/prepare_browser.py --download
.venv/bin/agentctl browser-form --engine fixture
.venv/bin/agentctl browser-form \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python
AOS_BROWSER_TESTS=1 .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
```

Alternatif dependency kurulumu: `uv sync --locked --extra browser`; validation requirements ayrıca kurulur. `fixture` yalnız deterministik DecisionEngine'dir; bu komutta tarayıcı yine gerçek izole Chromium'dur. Gerçek model kabulü için son komuttan önceki Decider komutu gerekir. Eşik düşürülmez: confidence ≥0.88 ve izinli option şarttır. S2 ve vision kullanılmaz; düşük confidence insanda bekler. CLI serbest görev/URL/gönderilecek metin almaz.

Playwright **1.63.0**, Chromium Headless Shell **153.0.8010.12**, revision **1243** kullanılır. CachyOS resmi desteklenen Playwright dağıtımı olmadığından indirme aracı Ubuntu 24.04 x64 fallback artifact'ini seçti; bu hostta gerçek çalışma ayrıca doğrulandı. Browser extra `uv.lock` içindedir. `models/browser-manifest.json` 287 Chromium dosyasının yerel SHA-256 değerlerini saklar; açılışta küme ve hash eşitliği kontrol edilir. Bu manifest ilk indirilen artifact için yerel integrity pin'idir, bağımsız upstream imza doğrulaması değildir. Manifest hazırlığını tekrar çalıştırmak açık yeniden pinleme işlemidir.

Tarayıcı runtime digest'i ve fixture/worker hash'leri trajectory environment içinde saklanır. Model deployment kimliği mevcut immutable Decider deployment'ıdır; aktif registry promotion yapılmaz. Host `/usr` kütüphaneleri ve Python virtualenv immutable container image değildir; bit-bit hermetik runtime/tedarik zinciri doğrulaması iddia edilmez.

## Yetki sınırı

- Playwright worker ve Chromium aynı ağsız Bubblewrap runtime içinde çalışır. Read-only `/usr`, Python site-packages, pinned Chromium dizini, tek fixture ve tek worker dosyası bağlanır. `/tmp` ve `/run` yeni tmpfs'tir.
- Host home, SSH bilgileri, host browser profili, workspace, DB, Docker socket, X11/Wayland socket ve model servisleri bağlanmaz. Environment temizlenir, capability'ler düşürülür. Yeni PID/user/network namespace'leri ve minimal `/dev` kullanılır.
- Chromium'un iç sandbox'ı kapalıdır; güvenlik sınırı dış Bubblewrap namespace'idir. Sandbox açılmazsa host fallback yoktur. Bu tasarım VM veya genel düşmanca web içeriği kabulü değildir; host kernel'i paylaşılır, cgroup/seccomp kaynak sertleştirmesi bu dilimde doğrulanmadı.
- CDP yalnız worker–Chromium pipe'ındadır; TCP port açılmaz. Tüm browser route'ları reddedilir, service worker/download kapalıdır. Fixture CSP network ve form navigation'ını da kapatır. Ağ namespace'i host namespace'inden farklı olmalıdır.
- Gateway yalnız `browser.fill`, `browser.submit`, `browser.verify` uygular. Observe ayrı salt-okuma kapısıdır. URL, selector, script, host path ve serbest komut runtime protokolünde yoktur.

## State, tazelik ve doğrulama

`task_kind=browser_form`, `aos://synthetic/form` scope'u ve exact content state'te sabittir. Ortak gateway state version, ownership/lease, deadline, allowed decision, runtime tipi ve idempotency denetimini korur. Intent ve envelope effect'ten önce commit edilir; seçilen ve dispatch edilen option ayrıdır.

Her observation yeni `snapshot_id` ve iki symbolic `element_id` üretir. Typed DOM sözleşmesi `schemas/browser_observation.schema.json`, sentetik örneği `examples/browser_observation.json` içindedir. IDs modelden gelmez; model yalnız hostun sunduğu fill/submit veya ask_human seçeneklerini seçer.

Worker tek JavaScript hazırlık çağrısında snapshot kimliğini, body/field fingerprint'ini, aynı DOM node referanslarını ve visibility/disabled durumunu kontrol eder; ardından izinli input/click'i uygular. Aynı HTML ile node replacement da stale sayılır. Yeni observation başarısız olsa bile önceki snapshot geçersizleşir; hazırlık denemesi başarılı olsun veya hata dönsün snapshot kontrol öncesi tüketilir. Başarısızlıktan sonra aynı referansla yeniden denemek yerine yeni observation gerekir. Model beklerken yeni observation veya DOM değişirse eylem `UI_CHANGED` ile durur; otomatik replay yoktur. Ayrı hazırlık ve input arasında mutation yarışı hâlâ mümkündür. Bu mekanizma yalnız sabit güvenilir fixture içindir; arbitrary sayfalardan gelen açıklamalar yetki veremez.

Fill ve submit sonrasında ayrı gateway `browser.verify` çağrısı alanı, receipt'i ve submission sayısını okur. Başarı için tam eşitlik ve **bir** submission gerekir; `{applied: true}`, confidence veya click dönüşü yeterli değildir. Verified run, iki karar/dört action/iki verification içeren tek bounded step olarak kaydedilir. DB şeması bu ilişkileri zaten desteklediğinden mevcut migration'lar değiştirilmedi. Canonical export şeması geriye uyumlu biçimde pre-action observation için `action_id=null`, verification için structured object kabul eder; hello string outcome'ları geçerliliğini korur. `examples/browser_export.json` elle yazılmış sentetik sözleşme örneğidir, gerçek run değildir.

Stale/unknown element/value reddi bilinen no-effect hata olarak kaydedilir. Worker çökmesi veya write sonrası acknowledgement kaybı `uncertain` kalır; retry yapılmaz. Timeout sadece sahip olunan runtime process grubunu kapatır; tmpfs profile runtime ile kaybolur. Genel Resume veya tarayıcıya özel Supervisor recovery henüz yoktur.

## Kanıt ve export

`agentctl export --run-id RUN_ID` başarılı hello veya browser fixture run'ını canonical trajectory olarak salt okunur dışa aktarır. Browser export yalnız sınırlı typed DOM ve bilinen sentetik metni kabul eder; `synthetic=true`, `training_eligible=0`, gerçek model/runtime bilgisi ayrı kalır. Genel sayfa redactor'ı değildir. Yerel DB/export `data/`, tarayıcı artifact/manifest `models/` altında ignore edilir; screenshot üretilmez.

Uygulama testleri gerçek Chromium integration'ını varsayılan olarak atlar; `AOS_BROWSER_TESTS=1` açık opt-in'dir ve eksik runtime/manifest varsa test başarısız olur. DOM replacement/mutation, stale IDs, yetkisiz tool/value, ağ/private-path sınırı, idempotency, crash-after-effect, timeout, model confidence/output, takeover ve bağımsız verification mismatch sınanır. Gerçek GPU kabulü bu fixture-engine testlerinden ayrıdır; ölçümler [STATUS](STATUS.md) içindedir.

Resmi API kaynakları: [Playwright browser hazırlığı](https://playwright.dev/python/docs/browsers), [BrowserType launch](https://playwright.dev/python/docs/api/class-browsertype). Buradaki Bubblewrap sınırı proje implementasyonudur; Playwright'ın tek başına host izolasyonu sağladığı iddia edilmez.
