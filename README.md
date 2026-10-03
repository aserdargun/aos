# AOS — Agent Operating System

Yerel modellerle bilgisayar ve web uygulaması kullanımı, doğrulanabilir görevler ve kontrollü öğrenme için deneysel bir ajan işletim sistemi.

**Hedef sürüm: v0.1.0.** Paket metadata'sı `0.1.0`; işler ve teslim kapıları kapanmadan Git sürüm etiketi oluşturulmaz. [Sürümleme koşulları](docs/DELIVERY_AND_CONTINUATION.md#requested-release-version).

**3 Ekim 2026 teslim kapsamı: test edilebilir kaynak/prototip. Tamamlanmış üretim ürünü değildir.** Mevcut pilot korunur; AI Scientist ile tek GPU otoritesi üzerinden tam ortak çalışma ve hedef uygulamaya özel gerçek-site kabulü sonraki aşamadır. Bu ayrım eksikleri gizlemek için değil, devralan kişinin neyi güvenle kullanabileceğini göstermek içindir.

Kaynak deposu: [aserdargun/aos](https://github.com/aserdargun/aos). Tanıtım sitesi ayrı depodur. **Lisans seçimi bekliyor:** public kaynak bulunması açık kaynak lisansı veya yeniden kullanım hakkının tamamlandığı anlamına gelmez. Model/adapter ağırlıkları, özel veri ve çalışan ortam bu depoya dahil değildir.

## Hızlı yönlendirme

| İhtiyaç | Başlangıç |
|---|---|
| Mevcut kurulumu denemek | [Pilot rehberi](docs/PILOT_QUICKSTART.md), [kontrol paneli](docs/CONTROL_CENTER.md) |
| Bu teslimde ne var, ne eksik? | [Teslim ve sonraki aşama](docs/DELIVERY_AND_CONTINUATION.md), [güncel test kanıtı](docs/STATUS.md) |
| Temiz bilgisayarda kaynak kurulumu | [Kurulum](docs/LOCAL_SETUP.md), [kaynak paketi](docs/SOURCE_HANDOFF.md) |
| Web uygulamasına özelleştirme | [Uygulama profili](docs/WEB_APPLICATION_PROFILE.md), [web öğrenme](docs/WEB_APPLICATION_LEARNING.md) |
| AI Scientist / yeni eylemciler | [Entegrasyon bileşimleri](docs/INTEGRATION_COMPOSITIONS.md), [genişletme noktaları](docs/EXTENDING_AOS.md) |
| Şirket içi fork veya Claude ile devam | [Devir rehberi](docs/CLAUDE_HANDOFF.md), [Claude kuralları](CLAUDE.md) |
| Kullanım ve maliyet kayıtları | [Muhasebe kapsamı](docs/USAGE_ACCOUNTING.md), aşağıdaki sayaç açıklaması |
| Bütün belgeler | [Dokümantasyon dizini](docs/README.md) |

## Ne çalışıyor, ne tamamlanmadı?

**Kaynakta uygulanmış**, **CPU/sentetik doğrulanmış**, **gerçek modelle tarihli doğrulanmış** ve **mevcut oturuma teslim edilmiş** farklı durumlardır. Birinin geçmesi diğerini kanıtlamaz. Ayrıntılı ölçümler tarihleriyle [STATUS](docs/STATUS.md) içindedir; eski başarı yeni deployment'a otomatik taşınmaz.

| Alan | Mevcut kapsam | Açık sınır |
|---|---|---|
| Yerel pilot | Hello dosyası, sınırlı DOM/form/vision görevleri; geçmiş gerçek Decider/Bonsai kabulleri | Herhangi bir uygulamayı sınırsız kullanma garantisi yok |
| Kontrol paneli | Development, Tasks, Computer, bilgi/skill ve Scientist panelleri; EN/TR | Kaynak derlemesi canlı backend/UI promotion değildir |
| Web kontrolü | Ubuntu Chromium, Playwright MCP, yetkili profiller ve bounded form akışları | Genel login/SSO/MFA, dinamik CSRF ve şirket uygulaması kabulü tamamlanmadı |
| Skill ve bilgi | İncelenen adaylar, açık seçim/revoke, sınırlı retrieval ve görev bağlamı | Genel RAG kalitesi, otomatik ustalaşma ve sınırsız skill aktarımı yok |
| Eğitim | İzinli veri inceleme/export ve dar tarihli S1 deneyleri | Genel S2 LoRA/QLoRA, sürekli otomatik eğitim ve kalite garantisi yok |
| AI Scientist | Typed Lab start/status/stop/report, intent, fencing ve kaynak sözleşmeleri | Gerçek native exclusion producer/consumer ve koordineli GPU kabulü açık |
| Orkestrasyon | Typed görev/policy ve opt-in uzman ajan temelleri | Genel kararlı plugin API ve sınırsız ajan çoğaltma teslim edilmedi |
| Dağıtım | Allowlist + hash manifestli kaynak arşivi | Temiz makinede tek komut GPU kurulumu, üretim SLA'sı ve lisans açık |

Tam ürün kapanışı için tek aşama kaydı [RELEASE_ACCEPTANCE](docs/RELEASE_ACCEPTANCE.md) ve [canonical JSON](docs/release_acceptance.json) dosyalarıdır. Bu kaynak teslimi o aşamaları tamamlandı yapmaz; uydurulmuş bir toplam yüzde verilmez.

**Son kaynak doğrulaması:** önceki yedi regresyon ve socket testindeki takılma düzeltildi. 3426 testlik core koşusunda **3122 PASS, 304 SKIP, 0 FAIL/ERROR**; atlanan gerçek GPU/UI kontrolleri nedeniyle rapor `partial` kalır. Schema-28 yedek manifesti artık canonical schema ile doğrulanıyor. Beş SQLite finalizer uyarısı ayrıca kaydedildi; warning-clean veya genel runtime kabulü iddia edilmez. [Kanıt ve sınırlar](docs/STATUS.md).

## Mimari ve donanım

Hedef referans donanım: Linux/CachyOS, NVIDIA RTX 4070 Ti SUPER **16 GB VRAM**, 32 GB RAM. Her 16 GB kartta bütün kombinasyonlar doğrulanmış değildir; iki modelin aynı anda VRAM'de tutulması zorunlu değildir.

```text
Kullanıcı / AOS kontrol paneli
  -> kapsamlı typed görev + insan yetkisi
  -> System-1 Operator / durum makinesi
  -> deterministik policy + Executor / Computer Gateway
  -> izole Ubuntu masaüstü / Chromium / yetkili uygulama
  -> bağımsız sonuç doğrulaması + SQLite trajectory
       |
       +-> System-2 Supervisor: plan, vision, recovery önerisi
       +-> incelenmiş bilgi/skill/veri -> ayrı değerlendirme ve terfi
       +-> yetkili uzman ajan adaptörü: ilk hedef AI Scientist
```

- **System-1:** Mapika/decider-2b, host-native PyTorch/CUDA; sonlu seçeneklerden karar verir.
- **System-2:** Ternary Bonsai-2 27B/vision, uyumlu pinli PrismML llama.cpp CUDA. Laya deneysel adaydır; varsayılan terfi sayılmaz.
- **Backend:** Python 3.11+, FastAPI/Pydantic/asyncio, SQLite; canonical schemas ve append-only migrations.
- **Agent Computer:** Docker içinde Ubuntu/XFCE/X11/noVNC. Host home/desktop veya Docker socket sınırsız açılmaz.
- **UI:** React/TypeScript; Tauri native kabuk ayrı hazırlık gerektirir. English varsayılan, Türkçe ikinci dil.
- **Paylaşılan GPU hedefi:** Scientist'in mevcut scheduler/broker'ı tek tahsis otoritesidir. İkinci scheduler kurulmaz; idle/quiesce GPU bırakım kanıtı değildir.

[Mimari](docs/ARCHITECTURE.md), [runtime](docs/RUNTIME.md), [model pinleri](docs/MODELS.md), [güvenlik](docs/SECURITY.md).

## Mevcut arayüzü açma

Hazırlanmış AOS hostunda:

```sh
./scripts/aos-v1 status
```

Çalışan varsayılan oturumun yerel adresi **http://127.0.0.1:8765/ui/**. `start` komutunu yalnız ortam hazır ve oturum gerçekten durmuşsa kullanın; inspection isteyen eski oturumu silmeyin veya zorla yeniden başlatmayın.

Bu geliştirme makinesinde ayrıca yalnız yetkili Mac'e izin veren Tailscale bridge hazırlandı. Adresler kullanıcıya özel teslim mesajındadır; genel kurulum sabit kişisel IP gerektirmez. [Bridge ve SSH seçenekleri](docs/CONTROL_CENTER.md). Host kontrolü gerçek Mac tarayıcı kabulü yerine geçmez.

Başka kurulumlarda tek-komut Mac launcher için [pilot rehberini](docs/PILOT_QUICKSTART.md) izleyin. SSH tüneli Mac terminalinden açılır; backend exact Host/Origin denetimi nedeniyle yerel ve uzak **8765** eşlenmelidir. Eski `18765 -> 8765` komutunu bu backend için kullanmayın; güvenlik denetimini kapatmayın.

İlk deneme: Development durumunu okuyun → Tasks'ta Hello seçin → scope/onayı inceleyin → bağımsız readback ve trajectory sonucunu görün. Sonra yetkili sentetik formu deneyin. Kontrolü al/iptal sonrası eski onay kullanılamaz. Scientist ortak GPU koşusunu bu sıraya eklemeyin; ayrı koordinasyon gerektirir.

## Temiz kaynak kurulumu ve doğrulama

Python 3.11+ ile yalnız sözleşme doğrulaması; model, Docker, eğitim veya canlı uygulama başlatmaz:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python scripts/validate_package.py
```

Fixture CLI ve CPU ağırlıklı çekirdek havuzu:

```sh
.venv/bin/python -m pip install -e '.[dataset,browser,desktop]'
.venv/bin/agentctl hello --engine fixture
.venv/bin/python scripts/check_capabilities.py --profile core
```

`fixture` sentetiktir; gerçek model kabulü değildir. CLI ignored yerel workspace/trajectory oluşturur. Core havuzu canlı portlara müdahale eden tam local-app lifecycle testlerini dışlar; skip başarı sayılmaz. Tüm testleri körlemesine discover etmeyin. [Test havuzu ve riskleri](docs/CAPABILITY_TEST_POOL.md).

Locked uygulama kurulumu için **henüz `.venv` bulunmayan ayrı bir checkout'ta** `python3 -m scripts.setup_local --install` alternatifi vardır. Yukarıdaki mevcut venv üzerine tekrar çalıştırmayın. `uv.lock` ve validation requirements kullanılır; [ayrı environment/staged UI seçenekleri](docs/LOCAL_SETUP.md) mevcut kurulumu değiştirmeden hazırlık sağlar.

UI için `ui/package.json` içindeki Node/pnpm gereksinimlerini karşılayın:

```sh
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
```

Bu komutlar kaynak derler; GPU backend veya çalışır masaüstü kurmaz. Canlı checkout'ta UI assetlerini değiştirmek yerine [izole staging](docs/LOCAL_SETUP.md) kullanın. GPU/browser/desktop hazırlığı ayrıca [DEVELOPMENT](docs/DEVELOPMENT.md), [BONSAI_RUNTIME](docs/BONSAI_RUNTIME.md), [DESKTOP_RUNTIME](docs/DESKTOP_RUNTIME.md), [PLAYWRIGHT_MCP](docs/PLAYWRIGHT_MCP.md) rehberlerindedir. Büyük model indirme ve training otomatik yan etki değildir.

## Web uygulamasına özelleştirme

1. Uygulama/origin, tenant, rol, izinli hesap, veri türleri ve yasak etkileri tanımlayın. Ağ erişimi iş yapma yetkisi değildir.
2. Sürümlü uygulama profili ve bağımsız başarı ölçütü hazırlayın; başlangıçta sentetik/owned fixture kullanın.
3. Yetkili görevi bounded adımlara ayırın; durum değiştiren işlemin intent'ini önce kaydedin, sonucu ayrı readback ile doğrulayın. Belirsiz POST'u körlemesine tekrarlamayın.
4. Doğrulanmış deneyimden skill adayı çıkarın; insan review/release/selection ve her yeni görev için güncel scope/onay uygulayın.
5. Uygulama belgelerini ayrı veri hakkıyla private corpus'a alın. Kaynak/revoke/provenance korunur; retrieved metin yetki vermez.
6. Eğitim için ayrı izin, redaction, dedup, held-out split ve S1/S2 format dönüşümü uygulayın. Smoke test öğrenilmiş kalite değildir; yeni modeli açık değerlendirme/terfi/rollback olmadan etkinleştirmeyin.

Komutlar ve **tamamlandı sayılma ölçütleri** [teslim/devam rehberinde](docs/DELIVERY_AND_CONTINUATION.md). SWAPP'a özgü intranet/kimlik/veri entegrasyonu şirket içi fork'ta **en son** yapılır; gerçek SWAPP'a erişim olmadan tamamlandığı iddia edilmez.

## AI Scientist ve gelecekteki eylemciler

AOS kullanıcı görevi, izin, intent, izleme ve bağımsız sonuç doğrulamasını orkestre eder. Scientist deney yaşam döngüsünü ve paylaşılan GPU tahsisini yönetir. Diğer ajanlar aynı typed policy sınırından geçer; ajan çoğaltmak kaynak/yetki sınırını çoğaltmaz.

**Mevcut teslim kısmi entegrasyondur.** Shared-only runtime adayı canlıya uygulanmadı. Maintenance kaynak modülü CPU'da sınanır; gerçek bakım çalıştırılmadı. `native_exclusion` DTO/schema düzeyindedir; canlı producer/consumer yoktur. Yeni source/config/version çifti, ayrı bakım onayı, owner/generation/fencing, fiziksel cleanup ve Scientist'in tek yürütücülü küçük GPU kabulü olmadan ortak sistem hazır denmez.

[Runtime sözleşmesi](docs/SCIENTIST_RUNTIME_INTEGRATION.md), [native handover](docs/NATIVE_HANDOVER.md), [shared-only aday](scripts/shared-only-runtime-v1/README.md), [yeni ajan ekleme](docs/EXTENDING_AOS.md). Sonraki aşama için bunlar çalışma planıdır; endpoint, ortak dosya yolu veya desteklenmeyen adapter uydurulmaz.

## Güvenlik, özel veri ve teslim paketi

- Model çıktısı izin kapsamını genişletemez. Approval, request ID, owner/generation ve deployment kimlikleri korunur.
- Cleanup kanıtlanamıyorsa yeni GPU işi kabul edilmez. Timeout veya idle fiziksel kaynak bırakımı değildir.
- Gerçek trajectory, ekran görüntüsü, DB, token, ağırlık, private corpus ve şirket verisi Git'e girmez.
- İncelenmiş kaynak arşivi `scripts/package_handoff.py` ile oluşturulur; tüm checkout'u tar veya `git add .` ile paketlemeyin.
- Hash manifesti bütünlük denetimidir, tam secret scanner veya imza değildir. Yayın öncesi kaynak/geçmiş/lisans incelemesi gerekir.

[Kaynak teslimi](docs/SOURCE_HANDOFF.md), [katkı kuralları](CONTRIBUTING.md), [güvenlik bildirimi](SECURITY.md).

## Geliştirme kullanımı ve modeller

**Tarihli teslim snapshot'ı: 3 Ekim 2026, 13:14:47 UTC / 16:14:47 Europe/Istanbul.** Authoritative `get_goal`, scope `aos-goal-20261001-062218Z`, active goal `bu son planı uygula`, başlangıç `createdAt=1790835738` (1 Ekim 2026, 06:22:18 UTC). Ham kümülatif `tokensUsed=12298925`, `timeUsedSeconds=72695`: **20 saat 11 dakika 35 saniye / 20,1930556 saat**. Bu gözlem anındaki sayaçtır; bu saatten sonraki işlemleri içermez.

Bu tek goal sayacıdır; tüm proje, insan saati, fatura veya model başına tüketim değildir. Tarihsel snapshot'larla veya JSONL oturum kayıtlarıyla toplanmaz. Gözlenen geliştirme rolleri: **Astra 6/high** orkestrasyon/inceleme, **Sol 6.1/high** uygulama/doğrulama, **Sol 6.1/medium** kayıt/belge. Root model varyantı doğrulanmadı. Bunlar AOS runtime Decider/Bonsai modellerinden ayrıdır.

Yerel geliştirme session kayıtları sağlayıcı/model/effort/zaman kırılımı için ayrı kapsamdır. Gerçek faturalar, abonelik bedeli ve tahmini API maliyeti birbirinin yerine kullanılmaz; bilinmeyen ücret sıfır sayılmaz. Uygulama `model_calls` kayıtları da geliştirme JSONL tüketimine eklenmez. [Muhasebe yöntemi ve düzenli kayıt](docs/USAGE_ACCOUNTING.md).

Bu hostta [saatlik özel kullanım kaydı](docs/USAGE_TIMER.md) etkinleştirildi ve servis ilk kez başarıyla çalıştı. Saatlik Git commit/push otomasyonu bundan ayrıdır ve henüz etkin değildir; özel kayıtlar otomatik yayımlanmaz.

Önceki README, bütün tarihli sayaç gözlemleri ve atıflı model rol geçmişi [historical archive](docs/README_HISTORY_20261003.md) içinde korunur; güncel kabul kaydı değildir. Yeni geliştirici/Claude oturumunda uyumlu sayaç yoksa son doğrulanmış değer historical tutulmalı, proje toplamı uydurulmamalıdır.

## Depo haritası

| Yol | İçerik |
|---|---|
| `src/aos/` | Görev, policy, executor, persistence ve entegrasyonlar |
| `services/`, `adapters/`, `computer/` | Model süreçleri, typed adaptörler ve izole bilgisayar |
| `schemas/`, `database/` | Canonical sözleşmeler ve additive migration'lar |
| `ui/` | React kontrol paneli ve Tauri kabuğu |
| `scripts/`, `tests/` | Kurulum, paketleme, explicit kabul araçları ve testler |
| `docs/`, `examples/` | Tasarım, runbook ve açıkça sentetik örnekler |
| `data/`, `models/`, `runs/` | Yerel/özel çıktılar; dağıtım dışı |

Geliştirmeye başlamadan [AGENTS.md](AGENTS.md), [CODEX_KICKOFF.md](CODEX_KICKOFF.md), [ROADMAP](docs/ROADMAP.md) ve güncel [STATUS](docs/STATUS.md) okunmalıdır. Kickoff başlangıç tasarımını taşır; tamamlanmış dilimler tekrar yapılmaz. Sonraki iş sırası: **ortak Scientist runtime kabulü → hedef web uygulaması kabulü → ölçülmüş özelleştirme/öğrenme → kurumsal fork gereksinimleri**; detaylı kapılar canonical kabul kaydında korunur.
