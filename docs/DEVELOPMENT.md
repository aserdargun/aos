# İlk çalışan dilim

Bu harness sabit hello ve sentetik yerel browser form görevlerinin uçtan uca kabulünü uygular. Genel Türkçe görev normalizer'ı veya genel bilgisayar kullanım uygulaması değildir. Kaynak metin, dosya adı ve satır sonu sabittir; kullanıcıdan serbest komut alıp çalıştırmaz.

`recover-hello` bu görev için gerçek Bonsai structured recovery kabulünü ekler. Hazırlık, komut ve kapsam [BONSAI_RUNTIME](BONSAI_RUNTIME.md) içindedir.

`browser-form` için optional browser extra, pinned Chromium hazırlığı ve gerçek runtime test komutları [BROWSER_RUNTIME](BROWSER_RUNTIME.md) içindedir. Varsayılan unittest komutu browser integration testlerini atlar; gerçek Chromium için `AOS_BROWSER_TESTS=1` gerekir.

`vision-canvas` gerçek Bonsai image input ve Decider symbolic seçimini aynı izolasyon sınırında doğrular; komut, screenshot saklama ve kapsam [VISION_RUNTIME](VISION_RUNTIME.md) içindedir. `AOS_BROWSER_TESTS=1` vision integration testlerini de açar; test fixture modelleri gerçek inference kabulü değildir.

## Kurulum ve test

React kontrol merkezi, Tauri development build'i ve `AOS_UI_TESTS=1` kabulü [UI_RUNTIME](UI_RUNTIME.md) içindedir. Frontend için pnpm lock, native kabuk için Cargo lock tutulur; generated build/cache dosyaları source manifest'e alınmaz.

Docker Agent Computer, `hello --runtime desktop`, authenticated noVNC konsolu ve `AOS_DESKTOP_TESTS=1` kabulü [DESKTOP_RUNTIME](DESKTOP_RUNTIME.md) içindedir. Desktop/browser extra'ları opt-in'dir; normal minimal kurulum Docker başlatmaz.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python -m pip install -e '.[dataset,browser,desktop]'
.venv/bin/python scripts/check_capabilities.py --profile core
.venv/bin/python scripts/validate_package.py
```

Python 3.11 veya üstü gerekir. Core profili canlı portları etkileyebilen tam `test_local_app.py` modülü yerine yalnız güvenli `LocalAppTests` grubunu seçer; opt-in atlamaları gerçek-model başarısı değildir. Extras eager import gereksinimlerini karşılar; bu kurulum Chromium, Docker image veya model indirmez. Tam unittest discovery çalışan pilot yanında varsayılan komut değildir.

`uv.lock` uygulamanın çözülmüş bağımlılıklarını sabitler. Alternatif extras kurulumu `uv sync --locked --all-extras`; doğrulama bağımlılıkları requirements-validation.txt üzerinden ayrıca kurulmalıdır. SQLite migration'ları repo checkout'undan okunur; geliştirme kurulumu editable kullanır. Temiz kaynak teslimi için [SOURCE_HANDOFF](SOURCE_HANDOFF.md).

Kaynak dosyaları değiştirildikten ve incelendikten sonra `python scripts/update_manifest.py` ile kaynak bütünlük listesi yenilenir. Bu script yerel model, DB, trajectory ve cache'leri kapsamaz. Paket doğrulayıcı yerel çalışma verisini taramaz; kaynak sözleşmelerini ve manifest'teki hash'leri denetler.

## Açıkça işaretli test motoru

```bash
.venv/bin/agentctl hello --engine fixture
```

Gateway'deki `/workspace/hello.txt`, varsayılan olarak `data/workspace/hello.txt` dosyasına eşlenir. Yalnız bu dosya ve tam `Hello from the local agent.\n` içeriği yetkilidir. Dosya yoksa exclusive-create yapılır; mevcut doğru içerik yalnız okunur; farklı içerik varsa insanda beklenir. JSON sonucu, structured log, canonical state ve trajectory `data/aos.sqlite` üzerinden incelenebilir.

## Gerçek Decider

İlk makinede mevcut `/home/cachyos/.venv/bin/python` ortamı çalışıyordu; yeniden kurulmadı. Ayrı worker process'i kullanılır, worker ağ erişimini gerektirmez ve Hugging Face offline modunu açar. Model hazırlığı açık bir indirme komutudur:

```bash
/home/cachyos/.venv/bin/python scripts/prepare_decider.py --download
.venv/bin/agentctl hello --engine decider \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --workspace data/acceptance-workspace
```

Hazırlık yaklaşık 3.8 GB model/kod/tokenizer dosyasını `models/` altına indirir. Sabit upstream checkpoint ve kod revision'ları script'tedir. İncelenen eager yolun dört upstream Python dosyası kullanılır; CUDA graphs, FP8 ve servis HTTP uçları yüklenmez. Model/code dosya kümesi veya SHA-256, tokenizer revision'ı, Python paket sürümleri ya da çağrı sonucundaki deployment digest değişirse execution durur. Model worker gerçek tokenizer ile tüm prompt'u sayar; 1536 tokenı aşan girdiyi sessizce kesmez. 2–10 seçenek sınırı korunur.

Bu makineye özel Python yolu başka kurulumlarda uyarlanmalıdır. Mevcut inference ortamının tam paket sürümleri yerel manifest'te kayıtlıdır; sıfırdan kurulabilir ayrı inference environment lock'u henüz hazırlanmadı. Native worker her kararda modeli yükler/boşaltır; kalıcı servis performansı iddia edilmez.

Gerçek model default'tur: `--manifest` yoksa CLI hata verir, sessizce fixture'a dönmez. Fixture için `--engine fixture` zorunludur. Kayıtlar EXPERIMENTAL kalır; hiçbir ACTIVE promotion veya eğitim yapılmaz.

## Trajectory inceleme

```bash
.venv/bin/agentctl export --run-id RUN_ID
```

RUN_ID yerine hello, browser-form veya vision-canvas çıktısındaki kimliği kullan. Export, canonical trajectory şemasına uyan salt okunur bir görünüm üretir; araçları tekrar yürütmez. Yalnız başarılı sabit görevleri destekler; bilinen içerik dışındaki gözlemde reddeder. Vision export screenshot byte'larını içermez; raw artifact hash/referansı redacted=false olarak kalır. Synthetic görev içeriği ile gerçek model/runtime yürütmesi ayrı alanlardır. Raw DB, screenshot, export ve modeller Git ignore kapsamındadır.

## Güvenlik ve recovery sınırları

- Her state geçişi ve action envelope kalıcıdır; intent commit edilmeden araç çalışmaz. Selected option ile actual action ayrı alanlardır.
- Çalıştırma anında task/run/step, runtime, state version, lease, deadline ve exact dosya/içerik kapsamı tekrar denetlenir.
- Aynı idempotency key/payload tamamlanmış sonucu döndürür; farklı payload reddedilir. Restart'ta intent/running işlemler uncertain ve run paused olur; lease iptal edilir. Otomatik replay/resume yoktur.
- İlk dosya API'si serbest shell çalıştırmaz. `process.sha256`, Bubblewrap namespace içinde salt okunur `/usr` ve workspace ile sabit `sha256sum` argv'si kullanır; ağ ve host home yoktur. Bubblewrap başarısızsa host fallback yoktur.
- Verifier ayrı gateway read ile exact içeriği karşılaştırır. Exit code veya model confidence başarı kanıtı değildir.
- Mevcut executor eşzamanlı tek writer'dır. Bonsai recovery, bounded browser/vision ve Docker desktop dilimleri vardır. UI_RUNTIME, tek hello/browser/vision scheduler ve eylem başına onay/takeover akışını açıklar; genel görev API, native E2E, paused-run devamı, genel redaction ve training açık kapsamdır.

Doğrulanmış sonuçlar ve ölçüm sınırları [STATUS](STATUS.md) içindedir.
