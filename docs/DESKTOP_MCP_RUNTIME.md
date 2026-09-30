# Ubuntu üzerinde görünür Playwright MCP — ilk runtime dilimi

Bu opt-in adaptör, **aynı owned Ubuntu/X11/noVNC masaüstündeki görünür Chromium'u** gerçek `@playwright/mcp` stdio araçlarıyla sürer. Host-headless [geliştirici MCP](PLAYWRIGHT_MCP.md) bağlantısından ayrıdır. Görev yürütmesi yalnız sentetik yerel formu ve sabit iki-sayfa gezinmeyi destekler; internet veya kullanıcı tarafından verilen genel web uygulaması henüz desteklenmez. W1'in browser taşıma dilimidir; onboarding, iki rolün artımlı öğrenme hattı ve bütün W1 kabulü tamamlanmış değildir.

Ek bir **sentetik iki-sayfa gezinme görevi** yalnız explicit `--desktop-mcp-manifest` ile çalışan scheduler'da `browser_local_navigation` olarak açılır. İlk `browser.fixture.open` sabit `/start` sayfasını, ikinci `browser.fixture.follow` taze gözlemlenmiş tek kullanımlık `Details` linkini açar. Her etkili adım mevcut lease ve ayrı manuel onay ister; `approve_all` bu görevde reddedilir. Action intent, karar, taze snapshot, MCP sonucu ve ayrıca okunan tam Details sonucu aynı run'da kaydedilir. Görev UI görev seçicisinde yer alır; Türkçe serbest hedef önizlemesi veya sıra kataloğuna eklenmez. Genel web aracı veya gerçek-site yetkisi sağlamaz. Container'ın ağsız namespace'i içindeki geçici `127.0.0.1` HTTP sunucusu bu görev runtime'ı başlarken bağlanır ve iki sabit HTML sayfası verir. Dış URL/selector/JavaScript argümanı kabul edilmez; origin, tam route, başlık, DOM linki ve geçiş sonrası sonuç ayrı kontrol edilir. Eski referans veya `/start` yeniden okuma hatası `UI_CHANGED` ile snapshot'ı tüketir; aynı link referansı tekrar denenmez. Yanlış yönlendirme veya MCP belirsizliği fail-closed durur, tıklama tekrarlanmaz. Varsayılan managed CDP yolu değişmez.

Gezinti modunda MCP/Chromium başlatılmadan önce worker'a ve alt süreçlerine miras kalan Landlock TCP-connect kuralı yalnız fixture portunu açar. Normal sabit görev portu dinamik seçer; ayrıca seçilen sentetik profil/pin varsa worker exact profile origin portunu bağlar, Chromium HTTP'yi aynı porta proxy üzerinden yollar ve sunucu yalnız exact absolute-form origin/rota isteğini kabul eder. Host attestation port ve proxy modunu ister. Kurulum, izinli porta bağlantı veya farklı porta ret probu başarısızsa runtime açılmaz. Standart MCP formu bu opt-in modla değişmez. Bu HTTP fixture deneyi genel HTTPS/redirect/alt-kaynak/gerçek-site egress çözümü değildir.

Guard'lı yerel görevler ayrıca MCP'nin pinli `--init-page` kancasında Playwright `page.route('**/*')` kullanır: yalnız exact fixture URL+yöntem kombinasyonunu devam ettirir, diğer tarayıcı isteklerini sunucuya ulaşmadan keser. Kaynak işçisi geçici modülü kendisi yazar; başlatma handshake'i ve profil-bağlı her eylem kimlik denetimi bu kurulum bayrağını ister. Pinned Chromium/Playwright ile izinli `/start` yanıtı 200, `/outside` isteği `ERR_BLOCKED_BY_CLIENT` ve fixture sunucusunda sıfır ek istek olarak sınandı. Bu sayfa düzeyi interceptor **tek başına güvenlik sınırı değildir**: bu fixture'da ağsız namespace, Landlock TCP port kuralı, exact HTTP proxy ve sunucu politikası birlikte korunur. Gerçek siteye ağ erişimi/redirect/subresource/UDP yetkisi açılmaz.

Fixture HTTP sunucusu ayrıca yalnız tam `GET /start` ve `GET /details` ile tek doğru loopback `Host` kabul eder; yöntem/rota/gövde/Host sapmaları ve alt-kaynak/yönlendirme girişimleri reddedilir. Dinamik-port kipinde en fazla 16, açıkça pinlenmiş HTTP proxy kipinde Chromium arka plan bağlantıları için ayrı 64 bağlantı sınırı vardır; iki kipte de en fazla 2048 başarılı yanıt gövdesi baytı sunulur. Her kabul edilen bağlantıdaki 1 saniyelik socket idle timeout, yarım bırakılan başlığın tek iş parçacıklı sunucuyu süresiz tutmasını engeller; bu mutlak istek süresi veya tam slowloris koruması değildir. Bu sunucu kuralı genel gerçek-site egress politikası değildir.

SQLite migration `0009_local_navigation.sql` yalnız yeni görev türü için `desktop_tasks.kind` sınırını genişletir; v7/v8 kayıtları korunur. İlk gerçek pinned Decider denemesinde `open_start` güveni 0,7773 olup 0,88 yürütme eşiğini geçmedi; görev sıfır eylemle güvenli durdu. Eylemi ve ardından yapılacak bağımsız doğrulamayı açıkça anlatan seçenek metniyle yapılan ikinci, ayrı gerçek-model kabulünde iki kararın güveni 0,9892/0,9977 oldu; iki ayrı onaydan sonra Start→Details ve iki bağımsız doğrulama geçti. Eşik, model/deployment pinleri veya araç yetkisi değiştirilmedi. Bu yalnız sabit sentetik görev kabulüdür; gerçek site genellemesi değildir.

## Açık hazırlık

Hazırlık mevcut npm execution cache'indeki MCP **0.0.82** ve Playwright/Playwright-core **1.64.0-alpha-1789764292000** dosyalarını doğrulayıp yeni private yerel arşiv/manifest oluşturur. Paket indirmez, npm script çalıştırmaz, model veya pinned desktop image değiştirmez. `--node-modules` mevcut kurulumun gerçek dizini olmalıdır; aşağıdaki cache yolu bu hosta özeldir.

```sh
.venv/bin/python scripts/prepare_desktop_mcp.py \
  --node-modules /home/cachyos/.npm/_npx/53b5ca06f5b71ebd/node_modules \
  --output models/desktop-mcp-v001
```

Hedef önceden varsa overwrite yapılmaz. Arşiv yerel SHA-256 pin'idir; tek başına bağımsız upstream provenance veya imza kanıtı değildir. Sürüm/dependency eşliği, regular-file/path sınırı ve arşiv boyut/adet/expanded-size limitleri denetlenir. Runtime açılırken hash tekrar kontrol edilir; doğrulanmış aynı byte'lar container'a stdin üzerinden gider ve orada da doğrulanır. Node paketleri yalnız o worker'ın private `/home/agent/aos-desktop-mcp-*` geçici dizinine açılır; host npm cache'i/home'u mount edilmez. Kaynak paketine node_modules, arşiv veya yerel manifest girmez.

## Deneme

Boş bir port ve yeni workspace/DB seçerek ayrı konsol başlatın; çalışan managed kullanıcı oturumu kendiliğinden dönüştürülmez:

```sh
.venv/bin/python scripts/serve_desktop.py \
  --port 8770 --workspace data/mcp-console/workspace --database data/mcp-console/store.sqlite \
  --engine fixture --browser-tasks --desktop-browser \
  --desktop-mcp-manifest models/desktop-mcp-v001/manifest.json
```

`fixture` yalnız karar motorunu sentetik yapar; Ubuntu, MCP ve Chromium gerçektir. Hazır gerçek modellerle `--engine decider --reuse-decider` kullanılabilir; ayrı gerçek-model gezinme kabulü yukarıda kayıtlıdır. Terminaldeki private token ile `http://127.0.0.1:8770/ui/` üzerinden giriş yapın. **Tasks → Local browser form → Start browser task**: önce fill, sonra submit onayı. **Tasks → Local two-page navigation → Start navigation task**: önce Start, sonra Details için iki ayrı manuel onay. Sonuç aynı Computer ekranında görünür; gezinmede Approve all işareti kapalıdır. Stop/Take Control/Pause/Resume mevcut scheduler'dan geçer. Tam backend cleanup için başlatıldığı terminalde Ctrl-C kullanın.

`--desktop-mcp-manifest` için `--desktop-browser --browser-tasks` zorunludur. Flagsiz CDP/headless yolları ve vision canvas taşıması değişmez; MCP seçeneği yalnız sabit form ve iki-sayfa görevi etkiler. `/api/tasks.browser_transport=playwright_mcp` yapılandırmayı, execution runtime'ın MCP identity/isolation kaydı gerçek taşıma yolunu belirtir. Yapılandırma alanı tek başına task başarı kanıtı değildir.

Ayrı opt-in `--desktop-staging-mcp-manifest` seçeneği, mevcut dört-görev managed varsayılanını değiştirmeden yerel üç sayfalı sentetik mini uygulamanın dört ayrı onaylı akışını açar. Sabit GET/POST/303 istek sınırı, tek submit ve bağımsız makbuz doğrulaması [sentetik staging sözleşmesinde](SYNTHETIC_STAGING_WORKFLOW.md) açıklanır; gerçek site erişimi değildir.

Yeni managed pilot, ayrı `--desktop-navigation-mcp-manifest models/desktop-mcp-v001/manifest.json` seçeneğini kullanır: yalnız `browser_local_navigation` Playwright MCP'ye gider; mevcut yerel form CDP'de kalır. İki MCP seçeneği birlikte verilmez. MCP arşivi managed doctor ve scheduler açılışında doğrulanır; eksik veya değişmişse sessizce üç görevli moda düşmez. `/api/tasks.kinds` dördüncü görevi, `navigation_transport=playwright_mcp` gezinme taşımasını bildirir; `browser_transport=cdp` formu anlatır. Mevcut canlı managed oturum kodla otomatik yükselmez; yeni davranış kontrollü sonraki başlangıçta geçerlidir. Runtime handshake, Landlock guard ve her eylem için ayrı manuel onay yine gerekir; bu sentetik görev gerçek-site yetkisi değildir.

## Sözleşme ve sınırlar

- Model yalnız mevcut typed form seçeneklerini seçer. Genel navigate/evaluate/run-code/upload veya keyfi selector/function argümanları gateway'e açılmaz. Adaptör içinde sabit güvenilir JavaScript, bağımsız observation/verification ve mevcut DOM freshness denetimini MCP `browser_evaluate` üzerinden yürütür; modelden kod alınmaz.
- Gerçek fill ve click, MCP `browser_fill_form` / `browser_click` araçlarına fresh accessibility snapshot referanslarıyla gider. Tek kullanımlık host snapshot ve sembolik element kimliği, DOM fingerprint/node/visibility/disabled/overlay/expected-value kontrolleri korunur. Hazırlık çağrısı hata dönse veya MCP bağlantısı kopsa bile host referansları tüketilir; hazırlık tarayıcıda çalıştıysa DOM snapshot'ı da kontrol öncesi tüketilir. Kopmada bu ikinci durum bilinmez, bu yüzden yeni observation olmadan aynı eylem tekrarlanmaz. Değişen hedef reddedilir; MCP hatasında eylem körlemesine tekrar edilmez.
- Aynı scheduler, run/state/lease/approval digest, action-intent ve bağımsız readback kayıtları kullanılır. Başarı MCP OK cevabından değil uygulamanın ayrı outcome okumasından gelir. Başarılı pencere sonraki görev veya kontrol devrine kadar korunur; pause/resume yeni onay ister.
- Runtime aynı pinned network-none, non-root, read-only-root Docker sınırındadır. CDP pipe ve MCP stdio kullanılır; CDP/MCP portu publish edilmez. Mevcut Docker Chromium yaklaşımı gibi `--no-sandbox` kullanılır; Chromium sandbox güvenliği iddia edilmez, izolasyon container'a dayanır. Bu durum host geliştirici MCP yapılandırmasını değiştirmez.
- Gezinti guard'ı yalnız fixed sentetik fixture ve Linux x86_64 Landlock TCP-connect kapsamındadır. UDP, Unix socket, `file:` ve diğer tarayıcı şemaları için genel egress çözümü değildir; ağsız container ve sabit gateway/DOM/CSP denetimleri ayrıca korunur. MCP'nin `--allowed-origins` seçeneği redirect'leri kapsamadığından güvenlik sınırı olarak kullanılmaz. Bu, gerçek site ağı veya hesabı için yetki sağlamaz.
- Worker yalnız kendi alt süreçlerini sahiplenmek için Linux subreaper kullanır. MCP kapanışı ve çöküşünde owned descendants pidfd ile temizlenir; desktop/container veya başka tarayıcı kapatılmaz. Host process crash sonrası genel session continuation/orphan yönetimi bu dilimin kabulü değildir.
- Paket snapshot'ları private container geçici dizinindedir ve cleanup ile gider. Yerel iki-sayfa görevinde worker, Chromium başlamadan dinamik fixture portunu açıp yalnız o TCP porta bağlantı izni veren Linux Landlock kuralını process ağacına miras bırakır; kurulum veya attestation başarısızsa görev başlamaz. Normal form modu bu ek kuralı kullanmaz. Bu fixed fixture kabulü, gerçek siteler için hostname/path/UDP politikası, genel DOM/screenshot redaction veya internet güvenliği kanıtı değildir. Mevcut ağsız policy korunur.
- Ek RPC ve DOM kontrolleri maliyetlidir; MCP'nin doğrudan CDP'den hızlı olduğu iddia edilmez. DOM-first yolun Bonsai vision gerektirmemesi ayrı bir özelliktir. Aynı hostta sentetik fixture-model form için eş koşullu CDP/MCP ölçümü yapıldı; gerçek Decider ve hedef uygulama ölçümleri hâlâ açıktır.
- Sabit formun submit dinleyicisi senkrondur; MCP'nin varsayılan 500 ms araç-sonrası beklemesi bu adaptörde `--timeout-settle 0` ile kaldırılır. Taze DOM/element denetimi, MCP eylemi ve ayrı sonuç okuması korunur. Bu ayar dinamik gerçek sitelerde asenkron tamamlanma garantisi vermez; onlar için ayrıca bounded readiness ve sonuç uzlaştırması gerekir.

## Doğrulama

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_desktop_mcp*.py' -v
AOS_DESKTOP_TESTS=1 AOS_DESKTOP_MCP_TESTS=1 AOS_UI_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_desktop_mcp*.py' -v
```

Gerçek Decider testi ayrıca `AOS_REAL_BROWSER_TASK_TESTS=1` ister. Fixture ve gerçek-model sonuçları, noVNC ekran kanıtı ve açık işler [STATUS](STATUS.md) içinde ayrılır. Genel web görevleri, site bilgisi, S1/S2 skill/veri üretimi ve fine-tune için [WEB_APPLICATION_LEARNING](WEB_APPLICATION_LEARNING.md) planı geçerlidir.
