# Yetenek kanıtı test havuzu

**30 Eylül kapsama ekleme:** `ui` profili artık `test_task_knowledge_ui` ayrı consent/manual effect akışını ve `test_task_knowledge_report_ui` salt-okunur historical/current-source audit ile bozuk model/request/response/hash/count retlerini içerir. İkisi de fixture model/yanıt kullanır. `real_tasks` içindeki ayrı `AOS_TASK_KNOWLEDGE_REAL_TESTS` kabulü gerçek pinli Decider Hello + iki-kararlı visible Chromium-MCP formunu, kaynak/model proof ve authenticated actual-report UI bağını denetler. Bu flag `core/ui/transport` ortamlarından çıkarılır. Focused kabul bütün real profili geçmiş yapmaz; güncel ölçülen raporlar STATUS içindedir.

`scripts/check_capabilities.py`, testleri etiketli ve ayrı süreçlerde çalıştırır; profil seçimi tek başına ürün veya gerçek-site kabulü değildir. Hiçbir başarılı sonuç varsayılmaz: her çalıştırmanın tarihli JSON raporunu inceleyin.

## Profiller

Knowledge belge/answer sözleşmeleri `core` içinde gerçek-model bayrağı olmadan çalışır. `ui` ayrıca authenticated TCP API + descriptor-workspace fixture üzerindeki yeni model-answer panelini kapsar; fixture sonucu gerçek Bonsai değildir. `real_tasks` ayrı `AOS_KNOWLEDGE_ANSWER_REAL_TESTS` opt-in ile incelenmiş sentetik EN/TR kaynaklarda gerçek Bonsai alıntı/abstention kabulünü içerir. Tek focused testi çalıştırmak bütün profili geçmiş yapmaz; kaynak/citation bağı anlamsal doğruluk veya eğitim etiketi değildir.

- `core`: sözleşme, politika, kalıcılık ve negatif testler; gerçek model kanıtı değildir. Port yaşam döngüsünü etkileyebilen `test_local_app.py` dışarıda kalır; yalnız güvenli `LocalAppTests` grubu eklenir.
- `ui`: gerçek Chromium/Docker arayüz testi, sentetik karar motoru ve fixture API yanıtları kullanır. Başarısız görev metadata önizleme/izin/review/revoke ve eski backend'de yazmama regresyonları da bu profildedir; gerçek model hatası veya eğitim kabulü değildir.
- `transport`: headless ve görünür izole Chromium DOM/görsel taşıma, gerçek MCP süreç yaşam döngüsü ve ret yolları; karar/görsel motorları fixture'dır. Gizli/disabled/örtülü hedef, stale DOM/capture, yanlış görsel konum, MCP çökmesi ve cleanup gibi negatifleri de çalıştırır.
- `real`: sırayla gerçek Decider/Bonsai görevleri, görünür Ubuntu Chromium/Playwright MCP, takeover, seçilmiş skill'in yeni oturumda kullanımı ve owned sentetik öğrenme/adapter çift testi. Görünür form/SAVE, pause sonrası taze onay, görev-içi Decider yeniden kullanımı, GPU idle expiry ve Bonsai öncesi GPU bırakma da kapsanır. CUDA/GPU kullanabilir; canlı kullanıcı görevi sürerken çalıştırmayın.
- `all`: dört havuz türünün tamamını, `real` profili dahil, çalıştırır.

`--case` tek bir kayıtlı vakayı seçer: `contracts`, `ui`, `transport`, `real_tasks`, `real_mcp`, `real_takeover`, `real_reuse` veya `real_learning`. Gerçek-model vakaları sentetik hedef/değer ve testin açtığı izole kaynakları kullanır; insan onayı yerine test harness onayları sürer. Gerçek site kabulü değildir.

`real_tasks`, headless üç-görev zincirinin yanında gerçek görünür Ubuntu form/SAVE ve gerçek model pause/takeover yollarını seçer. Ayrı izinli failure-followup kabulü de bu vakadadır: original hello write onayı reddedilir, incelenmiş metadata'dan yeni exact izinli görev ayrıca manuel onaylanır ve bağımsız readback denetlenir; öneri uygulaması/nedensellik kanıtı değildir. `real_mcp`, gezinmeye ek olarak profile pinli iki koşudan sayfa kanıtı adayını denetler; aday gerçek-site bilgisi veya aktif skill değildir. `real_takeover`, iptalin yanında resident CUDA işçisinin yeniden kullanımını, süre sonunda kapanmasını ve Bonsai vision öncesi bırakılmasını denetler. Test sayısı yalnız çalıştırılmış son rapordan okunur; daha geniş seçici listesi tek başına başarı kanıtı değildir.

## Selected-skill S2 belge bağlamı

`ui` ayrıca `test_owned_skill_knowledge_ui` seçer: gerçek Chromium/Docker konsolunda S2 endpoint payload'ları sentetik route fixture'dır. Ayrı consent, canonical start ve report hash bağları test edilir; gerçek Bonsai kabulü değildir. `real_tasks`, `test_owned_skill_knowledge_real.OwnedSkillKnowledgeRealTests` için yalnız gerçek profilin `AOS_OWNED_SKILL_KNOWLEDGE_REAL_TESTS=1` bayrağını açar. Bu test native pinli Bonsai ile EN/TR öneri ve revoke retlerini, sentetik controller/admitted skill üzerinde denetler; S1/effect yürütmez. Diğer havuzlarda bu model bayrağı temizlenir.

İsteğe bağlı aynı native testte `AOS_UI_TESTS=1` ayrıca açılırsa, testin değişmemiş gerçek raporları Chromium'daki kaynak validator'larıyla denetlenir. Bu salt okunur browser-record kabulü managed plan→bind→S1→oracle değildir. UI kaynak server'ı yalnız testin kendi loopback portunu/process group'unu kullanır; kullanıcı backend'i yeniden başlatılmaz. Python `0.0`, negative-zero ve scientific gösterimleri wire 1.1 canonical metinleriyle korunur, model pinleri normalleştirilmez. Doğrudan opt-in çağrılar da ayrıca idle GPU rezervasyonu gerektirir.

`real_reuse` ayrıca `test_owned_skill_knowledge_managed.OwnedSkillKnowledgeManagedTests` ve ayrı `AOS_OWNED_SKILL_KNOWLEDGE_MANAGED_TESTS=1` bayrağını seçer. Gerçek iki isolated manager/backend oturumunda source candidate→review→release→selection→reuse, ardından S2 belge planı→ayrı bind/start→S1→altı test-harness onayı→independent oracle ve revoke-before-fill/replay retlerini sınar. Site ve belge sentetiktir; kullanıcı corpus'u yerine helper'ın private test kökü kullanılır. Bu hedefli vaka sonuçları [STATUS](STATUS.md) içinde ayrı kaydedilir; yeni selector'ın varlığı veya flag yokken skip test başarısı değildir.

## Çalıştırma

Bu bilgisayardaki hazırlanmış `.venv`, Docker, pinli Chromium/MCP ve gerçek profil için yerel model/CUDA kurulumunu kullanır; eksik modelleri indirmez. UI kaynakları değiştiyse önce `pnpm --dir ui build` çalıştırın. Log/raporlar 0600, yeni rapor dizini 0700 izinlidir; testlerin kendi dosya-izin ortamı değiştirilmez.

Önce çalıştırmadan havuz kapsamını görün:

```sh
.venv/bin/python scripts/check_capabilities.py --list
.venv/bin/python scripts/check_capabilities.py --profile real --list
```

Sonra gereken profili veya tek vakayı seçin:

```sh
.venv/bin/python scripts/check_capabilities.py --profile core
.venv/bin/python scripts/check_capabilities.py --profile ui
.venv/bin/python scripts/check_capabilities.py --profile transport
.venv/bin/python scripts/check_capabilities.py --profile real
.venv/bin/python scripts/check_capabilities.py --profile all
.venv/bin/python scripts/check_capabilities.py --case real_reuse
```

Her alt süreç, ortamdan gelen `AOS_*_TESTS` bayraklarını temizler ve yalnız o vakanın gereken açık opt-in bayraklarını ekler; böylece miras kalan test bayrakları istenmeyen işleri başlatmaz. Havuz testleri normal canlı servisi bilerek değiştirmez; gerçek-model vakaları ayrıca özel test supervisor/runtime'ları oluşturur. Gerçek profil bir vaka hata verirse sonraki gerçek vakaları durdurur.

Gerçek-model vakası içeren her public çalıştırma, tüm havuz boyunca sabit özel `data/.capability-real.lock` üzerinde beklemesiz advisory kilit tutar. İkinci gerçek havuz rapor dizini/worker oluşturmadan çıkış kodu 2 ile reddedilir; `core`, `ui`, `transport` ve salt okunur `--list` bu kilidi istemez. Kilit fd'si worker'a miras bırakılır; parent fd'si kapansa bile yaşayan worker kilidi tutar. Symlink/hardlink veya gevşek izinli kilit reddedilir. Kilit dosyası normalde kalır; elle silmek aynı anda iki farklı inode üzerinde kilit açılmasına yol açabileceği için silmeyin.

Bu, yalnız bu çalıştırıcının işbirliği yapan süreçleri arasındaki kilittir; canlı kullanıcı görevleri, doğrudan unittest komutları veya başka GPU uygulamaları için global GPU rezervasyonu değildir. Parent ve worker birlikte öldürülürse onların model/desktop alt süreçleri ayrıca incelenmelidir; otomatik orphan temizliği, güç kesintisi sonrası devam veya kör retry yoktur. Gerçek testleri canlı kullanıcı göreviyle çakıştırmama sorumluluğu sürer.

Raporlar artık aynı dizindeki özel geçici dosyada tamamlanıp fsync sonrası atomik yayımlanır; mevcut rapor yazma başında truncate edilmez. İlk yayın mevcut dosyayı ezmez, sonraki güncelleme dosyayı atomik değiştirir. Yayın öncesi yazma hatası eski tamamlanmış baytları korur ve geçici dosya temizlenir. Okuyucunun kaynak-değişimi denetimi bir yarışta yine `unavailable` dönebilir; bu tam bir snapshot veya imzalı kanıt iddiası değildir.

Rapor ve vaka logları `data/capability-check-*` altında özel yerel kanıttır; içeriklerini paylaşmadan önce gizlilik açısından inceleyin. JSON'da `passed` dışındaki `partial`, `not_verified`, `failed` ve `infrastructure_error` başarı değildir. Atlanan (`skipped`) veya beklenen hata (`expected_failure`) içeren `partial` raporu, komutun sıfır çıkış kodu vermesi halinde bile tam geçmiş test havuzu olarak sunmayın.

## Son ölçülmüş kanıt

29 Eylül failure-followup hedefli gerçek Decider/Docker kabulü **1 PASS / 14,591 s**, pool-selection regresyonu **16 PASS / 0,011 s**. Bu doğrudan hedefli kabul yeni tam `real_tasks` havuz raporu değildir; aşağıdaki 27 Eylül altı-test gerçek görev kaydı tarihsel kalır. Kaynak/metadata izni, yeni execution izni ve authenticated test-harness eylem onayı ayrı tutuldu; guidance/causality/gold/training bayrakları false kaldı. Yeni tüm `real_tasks` vaka seçimi henüz topluca yeniden çalıştırılmış sayılmaz.

27 Eylül 2026 son raporları: core **1488 PASS / 239 SKIP**, UI **20 PASS**, gerçek Chromium/fixture karar taşıması **33 PASS**. Gerçek görev havuzu **6**, MCP/profil/sayfa kanıtı **3**, GPU yaşam döngüsü **3** testle tekrar geçti; önceki skill-reuse **1** ve çift-rol/S1 adapter/eşli görev **1** kanıtı korunur. Böylece beş gerçek-model grubunda toplam 14 test sonucu vardır; 14 bağımsız held-out görev veya tek koşu iddiası değildir. Bunlar sentetik hedeflerde ölçüldü; headless/görünür ayrımı ve özel rapor dizinleri [STATUS](STATUS.md) içindedir. Core sonucu atlamalar nedeniyle `partial` kalır; bütün olası testlerin geçtiği veya gerçek web sürümünün tamamlandığı anlamına gelmez.

## Development ekranında kanıt

Yeni backend'de **Development → Historical capability test pool**, her vaka için son kaydedilmiş sonucu, UTC başlangıç zamanını, süreyi ve geçen/atlanan/hatalı test sayılarını gösterir. Panel açılışı ve **Refresh evidence** yalnız authenticated `GET /api/capability-checks` okumasıdır; test, model, eğitim veya restart başlatmaz. Eski backend API'yi sunmuyorsa panel bunu açıkça belirtir; statik bir başarı tablosuyla değiştirmez.

Kaynak yalnız sunucunun sabit `data/capability-check-*` alanındaki özel raporlardır; istemci dosya yolu seçemez. API ham logları, test isimlerini veya özel dosya yollarını döndürmez. Bozuk/okunamayan yeni kaynak eski yeşil sonucun sessizce gösterilmesine neden olmaz. Kaynak taraması, dosya boyutu ve izinler sınırlıdır; bu bir imzalı kanıt deposu veya değişmiş kodun yeniden test edildiğinin kanıtı değildir. Yeni havuz başladığında henüz tamamlanmamış vakalar önceki başarının yerine tamamlanmış kanıt yok olarak görünür.
