# AOS — Agent Operating System

**Proje adı:** `aos-aserdargun-com` · **Paket:** mimari ve geliştirme başlangıcı v0.1 · **Tarih:** 19 Eylül 2026

**Uygulama kaynak deposu:** [aserdargun/aos](https://github.com/aserdargun/aos). `aserdargun/aos-aserdargun-com` ayrı tanıtım sitesi deposudur; çalışan uygulama kaynakları onun üzerine yazılmaz. Public kaynak paylaşımı, henüz seçilmemiş açık kaynak lisansı veya tamamlanmış ürün kabulü anlamına gelmez.

Tamamen yerel çıkarım hedefleyen, kendi izole Linux masaüstüne sahip iki mantıksal ajanlı bilgisayar kullanım ve öğrenme platformu. **Supervisor düşünür ve planlar; Operator sonlu seçeneklerden karar verir; deterministik Executor işlemi uygular ve sonucu doğrular.**

Bu README mimari sözleşmenin girişidir. Ayrıntılar bağlantılı `docs/` dosyalarındadır. Çalışan dilimler: `agentctl`, typed Operator, güvenli gateway, SQLite trajectory, gerçek yerel Decider/Bonsai recovery, DOM/vision kabulü ve Docker XFCE/noVNC Agent Computer. Authenticated FastAPI/React konsolu masaüstü kontrolü, salt okunur trace/registry ve kaynak limitlerini gösterir; Tauri native kabuğunun ilk build/startup smoke'u vardır. Genel görev API'si, tamamlanmış UI ve eğitim hâlâ geliştirme kapsamındadır. Model ağırlıkları ve yerel trajectory dosyaları kaynak paketine dahil değildir. Başlangıç kaynak sınırları [SOURCE_NOTES](docs/SOURCE_NOTES.md), güncel kanıtlar [STATUS](docs/STATUS.md) içindedir.

## Geliştirme süresi, token kullanımı ve modeller

**Gözlem: 30 Eylül 2026, 09:55:03 (Europe/Istanbul; 06:55:03 UTC).** Kaynak: ana geliştirme konuşmasının aktif Codex goal kullanım sayacı (`get_goal`, thread `01a0bad9-51a3-75b1-b8b9-b00f2fdffc86`). Aşağıdaki kümülatif değerler bu gözlemde doğrulanmıştır; önceki sayaç kaydı güncel gözlemle değiştirilmiştir. Alt coding worker sayaçları toplanmaz. Sayılar tamamlanmış proje maliyeti değildir ve bu hedef dışındaki geliştirme toplamını kanıtlamaz.

| Ölçüm | Kaydedilen değer | Kapsam |
|---|---|---|
| Sayaçtaki süre | **59 saat 42 dakika 33 saniye** — yaklaşık **59,71 saat** | `timeUsedSeconds = 214953`; insan çalışma saati, GPU saati veya paralel ajan-saat toplamı olarak yorumlanmaz |
| Sayaçtaki token | **49.013.953 token** — yaklaşık **49,01 milyon** | `tokensUsed = 49013953`; sayaç girdi/çıktı/cache ve model başına dağılım vermiyor |
| Ölçülen hedefin başlangıcı | **23 Eylül 2026, 16:46:45 (Europe/Istanbul)** | `createdAt = 1790171205`; bu hedef öncesindeki geliştirme ve başka konuşmalar kapsanmış kabul edilmez |
| Projenin tüm geçmişi / parasal maliyet | **Tam toplam doğrulanamıyor** | Eksik oturum geçmişi ve faturalandırma dökümü nedeniyle sayı uydurulmaz |

Geliştirmede kullanılan/istenen modeller ile AOS'un çalıştırdığı modeller farklıdır. Aşağıdaki kayıt doğrulanabilen geçmişi ve açıkça işaretlenmiş tercihleri gösterir; bütün geçmiş çağrıların eksiksiz dökümü değildir:

| Katman | Model ve rol | Kanıt / sınır |
|---|---|---|
| Geliştirme | **GPT-6 Astra · high** — orkestrasyon, mimari ve inceleme | Kick-off ve tarihsel geliştirme kayıtlarında yer alır |
| Geliştirme | **GPT-6 Luna · high** — uygulama işçileri | STATUS içinde Luna/high worker kullanımı kaydedilmiştir |
| Önceki geliştirme tercihi | **GPT-6 Sol · high** — thinker olarak kullanıcı tarafından istendi | Tam çağrı envanteri ve model başına token/süre dağılımı doğrulanmadı; istek, ölçülmüş kullanım sayılmaz |
| Önceki geliştirme tercihi ve işçiler | **GPT-6.1 Sol · medium** — 29 Eylül dilimleri ve worker'ları | Kullanıcı talimatı, gözlenen Codex config ve açık worker model/effort seçimi; geçmiş tüm çağrılar veya model-başına kullanım bununla kanıtlanmaz |
| 30 Eylül tamamlanan geliştirme işçileri | **GPT-6.1 Sol · high** — paralel knowledge backend/UI, answer model/service ve task-context service/engine/API/UI tests | Worker model/high seçimi gözlendi; ana turun backend kimliği config değişikliğiyle geriye dönük değiştirilmiş sayılmaz |
| Güncel geliştirme tercihi ve işçiler | **GPT-6.1 Sol · medium** — son kullanıcı isteği ve task-context report, local login, Mac launcher, selected-skill S2 context model/service/transport/UI işçileri | Worker model/medium seçimi metadata'da gözlendi; yerel config `model = "gpt-6.1-sol"`, `model_reasoning_effort = "medium"`. Ana oturumun canlı model değişimi veya geçmiş çağrılar bununla kanıtlanmaz |
| AOS runtime | **Mapika/decider-2b** — System-1; **Ternary Bonsai-2 27B** — System-2/vision | Pinli yerel kabul kayıtları; bu modellerin yerel inference/eğitim tüketimi Codex sayacına eklenmiş varsayılmaz |
| AOS deneysel aday | **convaiinnovations/laya-typed-decisions** — alternatif System-1 GPU karşılaştırması | STATUS'ta gerçek deneme kayıtlı; aktif modele terfi edilmedi |
| Gelecek özel fork | **Claude ile geliştirme, AI-Scientist entegrasyonu** | Planlanan devam yolu; bu tablodaki geçmiş geliştirme kullanımına dahil edilmez |

Model başına ücret, toplam faturalandırılan token veya her alt ajanın exact backend kimliği bu kayıttan türetilemez. Proje tamamlandı iddiası yoktur. Güncel uygulama/test kanıtı [STATUS](docs/STATUS.md), rol/model geçişi sözleşmesi [CONTINUOUS_IMPROVEMENT](docs/CONTINUOUS_IMPROVEMENT.md) içindedir.

**Güncelleme kuralı:** her geliştirme tesliminde ve son sürümde sayaç yeniden okunur; tarih, süre, token ve doğrulanmış model geçmişi revize edilir. [AGENTS.md](AGENTS.md) ve [CLAUDE.md](CLAUDE.md) bu kuralı sonraki geliştiricilere taşır. Sayaç erişilemiyorsa son doğrulanmış kayıt tarihli olarak korunur; yeni toplam uydurulmaz, örtüşen oturumlar toplanmaz. Bu tablo canlı/otomatik telemetri veya fatura değildir.

## Codex ile başla

**Kalan işler ve çalışma sırası:** [kabul ölçütlü iş kuyruğu](docs/REMAINING_WORK.md). Çalışan yerel temel, dar sentetik kabuller ve henüz tamamlanmamış genel web/skill/S2-eğitim/entegrasyon/teslim kapıları ayrı listelenir; SWAPP intranet kabulü en son kalır.

**Birden çok kombinasyon:** AOS genel çekirdektir; şirket içi SWAPP web uygulaması + AI-Scientist, gelecekteki özel fork kombinasyonlarından biridir. Farklı uygulamalar ve ek uzman ajanlar typed, ayrı yetkili entegrasyonlar olarak hedeflenir; mevcut iki-rol yürütme çekirdeği ve tek runtime input sahipliği korunur. Genel plugin/multi-agent hizmeti henüz tamamlanmış değildir. [Entegrasyon sözleşmesi](docs/INTEGRATION_COMPOSITIONS.md).

**Ürün hedefi — öğrenen ve modelden bağımsız AOS:** başkalarının kendi yetkili sistemlerine uyarlayabildiği, başarısızlıklardan incelenmiş skill, RAG bilgisi ve ayrı S1/S2 eğitim verisi üreten bir platform. Daha iyi Operator/Supervisor modellerine ayrı ayrı geçiş, uyumlu fine-tune/LoRA/QLoRA backend'leri ve ölçülmüş terfi/geri alma hedeflenir. Genel RAG, QLoRA ve otomatik model değiştirme bugün tamamlanmış değildir. [Sürekli gelişim ve model geçişi sözleşmesi](docs/CONTINUOUS_IMPROVEMENT.md).

**Başarısız görevden gelişim incelemesi:** Tasks, mevcut oturumdaki başarısız/iptal edilmiş görev için salt okunur önizleme, ayrı metadata izniyle özel aday kaydı ve insan accept/reject/revoke akışı sunar. Kaynak, model ve onay kimlikleri yeniden denetlenir; ham görev/model içeriği kopyalanmaz. Sonradan verilen bu sınırlı izin sürekli veri toplama değildir; öneri eğitim etiketi, doğrulanmış düzeltme veya tekrar yürütme yetkisi olmaz. [Kapsam ve kullanım](docs/FAILURE_IMPROVEMENT.md); bu I1'in ilk tanısal dilimidir, bütün öğrenme döngüsü değildir.

**İncelenmiş başarısızlıktan ayrı takip görevi:** Tasks ayrıca exact preview hash'i ve açık yürütme izniyle aynı görev sözleşmesine bağlı yeni iş oluşturur; kaynak/review/configuration ve kontrol kimliği model/onay sınırlarında yeniden denetlenir. Her eylem manuel onay ister; ayrı rapor tarihsel bağı, güncel kaynak/review ve bağımsız sonuç kanıtını ayırır. Gerçek pinli Decider/izole Docker hello kabulü iptal-before-effect → ayrı yeni izin/onay → bağımsız readback ile geçti; onaylar test harness'inden geldi. Başarı önerinin uygulandığını, nedensel düzeltme veya gold/eğitim olduğunu kanıtlamaz. [Kullanım ve sınır](docs/FAILURE_FOLLOWUP.md).

**İncelenmiş yönlendirmeyi kararda kullanma:** Tasks'ın ayrı izinli Hello akışı, kabul edilmiş “taze gözlemi kullan” veya “yeniden denemeden önce incele” talimatını yeni DECIDE gözlemine ve model isteğine bağlar. Ayrı rapor bağlam hazırlığını, başarılı gerçek S1 girdisi kanıtını ve görev sonucunu denetler; yönlendirmenin nedensel iyileşme veya eğitim etiketi olduğu ayrıca değerlendirilmelidir. [Akış ve kanıt sınırı](docs/FAILURE_GUIDANCE.md).

**Sonlu Hello yönlendirmesini yeniden kullanma:** ayrı yayın izniyle değişmez, kaynak/review/workspace/model bağlı kayıt oluşturulur; aynı oturumdaki yeni görevler exact kayıt seçimi, yeni yürütme izni ve manuel onay ister. Bu genel RAG, farklı sistemlere otomatik skill taşıma veya kanıtlanmış kalite artışı değildir. [Sözleşme](docs/HELLO_GUIDANCE_REUSE.md); doğrulanmış test kapsamı [STATUS](docs/STATUS.md) içindedir.

**Fork ve geliştirici devri:** [katkı/kurulum](CONTRIBUTING.md), [Claude çalışma kuralları](CLAUDE.md), [şirket içi devam rehberi](docs/CLAUDE_HANDOFF.md), [genişletme noktaları](docs/EXTENDING_AOS.md) ve [kaynak teslimi](docs/SOURCE_HANDOFF.md). Kaynak paketi model, özel veri veya çalışan ortamı içermez. Açık kaynak lisansı henüz seçilmedi; yayımlama ve yeniden kullanım lisansı tamamlanmış sayılmaz.

**Knowledge / Bilgi:** uygulama/tenant/rol kapsamlı metin veya UTF-8 dosyası, ayrı yayın ve inceleme izinleriyle özel corpus'a eklenebilir. Güncel/kabul edilmiş/süresi geçmemiş kaynaklar bounded EN/TR lexical aramada hash ve chunk atıflarıyla gösterilir; revoked/yabancı kapsamlar sıralamadan önce dışlanır. Model çağrısı, otomatik yürütme, eğitim veya tamamlanmış RAG kalite kabulü değildir. [Kullanım](docs/DOCUMENT_KNOWLEDGE.md).

**Tek görevin S1 kararında belge kullanımı:** Tasks'ta sıradan görev için incelenmiş kaynakların exact önizlemesi, ayrı inference/özel trajectory izni ve manuel eylem onayları vardır. Bağlam immutable DECIDE snapshot'ına ve gerçek dispatch/model request ile source/review/control pinlerine bağlanır; fixture hazırlığı gerçek model kullanımı değildir. Yerel scope harici hesap yetkisi sayılmaz; S2 görev planlaması, keyfi görevler, kalite ve eğitim açık kalır. [Sözleşme](docs/TASK_KNOWLEDGE.md).

**Belgenin gerçekten kullanıldığını arayüzde denetle:** Tasks'taki salt-okunur rapor, prepared/dispatched/başarılı gerçek-model proof sayıları, exact kaynak/Prediction hash'leri, bağımsız sonuç ve historical/current-source ayrımını gösterir. Ayrı Refresh task/model başlatmaz; fixture hazırlığı actual model use gösteremez. Gerçek Hello ve visible MCP form raporları authenticated Chromium'da doğrulandı. Development ayrı knowledge kabul kartlarını gösterir; gerçek-site kabulü **0/6** kalır. [Kanıt](docs/STATUS.md).

**S2 skill önerisinde belge bağlamı:** yeni context-capable owned-reuse oturumunda ayrı preview, inference/private-storage izinleri ve exact hash ile incelenmiş kaynak metni Bonsai planlama girdisine bağlanır. Native model/service kabulüne ek olarak izole managed oturumda iki EN/TR hedef, ayrı bind/start ve gerçek Decider ile altışar taze onay/bağımsız readback'ten geçti; üçüncü koşu kaynak iptaliyle fill/POST öncesinde durdu. Site/belge sentetiktir; bu genel hedef veya gerçek-site kabulü değildir. Kaynak/dispatch/request/response ve revocation denetlenir; plan önerisi yürütme veya eğitim yetkisi değildir. Development bu ayrı dilimle **56/65** dar checklist gösterir, W1–W6 **0/6** kalır. [Akış ve sınırlar](docs/OWNED_SKILL_KNOWLEDGE.md).

**İncelenmiş belgeden ayrı izinli Bonsai yanıtı:** Knowledge bölüm 4 exact bağlam önizlemesi, yeni inference/storage izni ve ayrı hash onayıyla yerel S2'yi çağırır. Yanıt yalnız doğrulanmış kaynak alıntıları veya abstention'dır; EN/TR iki sentetik olgu ve yanıtlanamayan bir soru gerçek pinli Bonsai ile geçti. Fixture/gerçek, tarihsel kaynak/güncel yetki ayrı raporlanır. Genel task-context RAG, serbest yanıt doğruluğu veya eğitim/promotion kabulü değildir. [Sözleşme](docs/DOCUMENT_KNOWLEDGE_ANSWERS.md).

**Güncel çalışma sırası:** SWAPP şirket intranetine yetkili tünel bağlantısı ve gerçek-site kabulü kullanıcı isteğiyle **en son aşamaya** alındı. Yerel AOS runtime/arayüz, skill ve S1/S2 veri-değerlendirme geliştirmeleri erişimi beklemeden ilerler. Bu erteleme W1–W6 kabulünü tamamlanmış yapmaz. [Çalışma sırası](docs/ROADMAP.md).

**Çalışan pilotu şimdi dene:** [Mac bağlantısı ve iki kısa görev](docs/PILOT_QUICKSTART.md). Mevcut port-8765 oturumunda gerçek Decider ile dosya ve görünür tarayıcı görevleri yeniden doğrulandı; ilk kullanıcı denemesi için yeni öğrenme altyapısının tamamlanmasını beklemeyin. Bu sınırlı pilot, genel gerçek-site ustalığının tamamlandığı anlamına gelmez.

**Mac'ten kolay bağlantı:** `scripts/aos-connect-macos.sh`, hazırlanmış sunucuda güvenli `start`, tek özel SSH tüneli ve tarayıcı açılışını birleştirir. Standart yerel managed UI token girişi istemez; public/çok-kullanıcılı erişim için uygun değildir. SSH kimlik doğrulaması, exact loopback port/Host/Origin ve görev izinleri korunur. [Kurulum ve kullanım](docs/PILOT_QUICKSTART.md).

**Aynı girdide taban–adapter tarayıcı karşılaştırması:** Tasks ayrı precommit ve iki açık başlangıçla aynı yeni sentetik vaka/skill'i önce taban, sonra adapter ile yürütür. Gerçek kabulde her kol altı karar/onay, yedi eylem (deterministik receipt readback dahil), tek POST ve iki sonuç doğrulamasıyla geçti. Yeni model süreçleri, tek frozen snapshot denetimi ve kapanış sonrası ağsız bozulma retleri doğrulandı. Çağrı duvar süresi ve onay pencereleri ayrı gösterilir; tek çift kalite/hız üstünlüğü, held-out veya promotion değildir. [Kullanım](docs/OWNED_ADAPTER_EVALUATION.md), [ölçülmüş kanıt](docs/STATUS.md).

**S1 adapter ile ayrı izinli görev — gerçek sentetik kabul geçti:** Tasks'ta eğitimden ayrı tek-görev runtime izni, schema-1.5 admission ve görev-başına adapter engine uygulanmıştır. Gerçek GPU/Ubuntu Chromium kabulünde altı adapter kararı ve manuel onay, tek POST/readback, sonraki açık base görevi ve kaynak bozulduğunda sıfır POST doğrulandı. Çağrı-başına hook/kimlik kanıtı ve historical/current-source ayrımı korunur; global base engine veya active pointer değiştirilmez. Bu bağımsız kalite artışı veya üretim promotion değildir. [Kullanım ve sınırlar](docs/OWNED_ADAPTER_RUNTIME.md), [test kanıtı](docs/STATUS.md).

**Episode'dan açık izinli deneysel S1 adapter:** CPU tokenizer sonrasında Tasks ayrı veri-hakları/hassas-içerik beyanı ve exact eğitim bütçesi onayı ister. Bütün S1 gelişim kayıtlarında tek rank-4 GPU güncellemesi ve ikinci süreçte taban/adapter karşılaştırması uygulanır; bağımsız held-out, otomatik deployment veya promotion değildir. Gerçek kabul kanıtını [STATUS](docs/STATUS.md), kapsam ve kullanımı [OWNED_EPISODE_ADAPTATION](docs/OWNED_EPISODE_ADAPTATION.md) içinde izleyin.

**Episode export → dönüşüm ve hazırlık:** Tasks açık önizleme/onay ile S1 kararlarını pinli Decider Example/Q girdisine, S2 planını ayrı messages/target biçimine dönüştürür. Ayrı gerçek CPU tokenizer kontrolü bütün S1 satırlarında token/gold/mask bağlarını sınar; kaynak, receipt, export ve deployment pinleri yeniden denetlenir. Ağırlık yükleme, eğitim veya promotion yoktur; S2 trainer uyumluluğu açık kalır. [Kullanım ve sınır](docs/OWNED_EPISODE_PREPARATION.md), [kanıt](docs/STATUS.md).

**İzinli çift-rol içerik öğrenmesi:** yeni owned-reuse oturumunda planlamadan önce varsayılan kapalı bir seçim, gerçek Bonsai plan girdisi/tahmini ve görev sürerken gerçek Decider karar girdisi/tahminlerini özel adaylara bağlar. Tasks açık içerik incelemesi, başarılı kaynak denetiminden sonra ayrı S1/S2 accept/reject/revoke ve iki kabul ile yerel immutable gelişim export'u sunar. Tahmin otomatik gold olmaz; gerçek site, bağımsız held-out ve eğitim/promotion açık kalır. [Sözleşme](docs/OWNED_EPISODE_LEARNING.md), [ölçülmüş kabul sınırı](docs/STATUS.md).

**Seçilmiş skill için Bonsai → Decider akışı:** yeni owned-reuse backend'inde iki tam TR/EN hedef grameri, pinli Bonsai'ye tek admitted skill ile bağlanır. Tasks panelinde öneri üretme, ayrı bağlama/önizleme ve açık başlangıç; ardından Decider ile altı manuel onaylı yürütme ve plan-bağlı audit vardır. Şema-1.4 öneri/preview/admission pinlerini korur; öneri tek başına eylem veya eğitim başlatmaz. Genel doğal dil veya gerçek-site kabulü değildir. [Sözleşme ve doğrulama sınırı](docs/OWNED_SKILL_PLANNING.md), [test kanıtı](docs/STATUS.md).

**Konsol açılışı:** `./scripts/aos-v1 open` yerel `/ui/` adresini açar; derlenmiş UI varsa kök `/` adresi de buraya yönlenir. Oturum anahtarıyla girişten sonra ve her sayfa açılışı/yenilemede varsayılan **Development / Geliştirme** paneli görünür; dar uygulama yüzdeleri gerçek W1–W6 ürün kabulünden ayrı gösterilir. İlk web uygulaması kartı canlı profil/pin durumuna göre sıradaki güvenli kurulum adımını da gösterir; kendi başına görev veya site isteği başlatmaz. [Arayüz sınırı](docs/UI_RUNTIME.md).

**Konsoldan HTTPS form hazırlığı:** Web applications ekranı kayıtlı profilden tek veya 2–8 sıralı alanlı, stateless form görevini ve exact POST/makbuz planını ağsız önizler; ayrı SHA-256 onaylarıyla özel dosyalara kaydeder. Opsiyonel altı-onaylı önce/sonra durum GET planı ayrı exact hash ile kaydedilir; bu seçim görev kaydından önce yapılır. Public hedef için yalnız yeni oturum manager komutları gösterilir, otomatik site isteği veya görev yetkisi verilmez. Gizli değer girmeyin. [Kullanım ve sınır](docs/WEB_FORM_ONBOARDING.md).

**W4 HTTPS form kaynak denetimi:** tamamlanmış dört ayrı insan onaylı temel form görevi, exact plan ve bağımsız makbuz readback'iyle salt okunur denetlenebilir. Fixture başarısı gerçek Decider/Bonsai, site sonucu, veri toplama veya eğitim izni değildir. [Sınır ve CLI](docs/REMOTE_FORM_LEARNING_SOURCE.md).

**W1 görev sonrası JSON okuması:** denetlenmiş temel HTTPS form koşusundan sonra ayrı exact onayla aynı-origin public TLS JSON `GET` ve beyan edilen üst düzey scalar değer hash'i karşılaştırılabilir. Çerezli run yalnız exact kayıtlı çerez hash'i ve özel dosyayla ayrı opt-in okunur; değer rapora girmez. Bu host okuması hesap/submit bağı veya site sonucu kabulü değildir. [Kapsam ve CLI](docs/REMOTE_FORM_JSON_ORACLE.md).

**W1 gönderilen değer bağı:** ayrı opt-in CLI, önceki POST'un exact gövde hash'ini özel form değerleriyle doğrular; ayrı JSON GET'te seçilen string alan değerinin geri okunduğunu kanıtlar. Owned sentetik uygulamada çalışır; kalıcı site sonucu veya hesap doğrulaması değildir. [Kapsam ve CLI](docs/REMOTE_FORM_JSON_SUBMISSION.md).

**W6 tekrar ölçümü:** aynı private trajectory DB'deki exact temel veya durum-planlı formun tüm terminal, bağlanmış denemeleri ayrı salt okunur sayılır; taşıma/doğrulanmış beyan edilmiş durum, toplam süre, onay-penceresi dışı süre ve ilk eylem intent'i p50/p95'i ayrı raporlanır. Başarılı taşıma koşularında her onaylı eylemin tüketimden tamamlanmaya kayıtlı p50/p95'i de ayrılır. Pinli yeni backend'de Development bu özeti yalnız authenticated ve kaynak geçerliyse gösterir; CLI de kullanılabilir. Bu aralıklar saf model/tarayıcı zamanı, gerçek site/uygulama sonucu veya W6 kabulü değildir. [Sınır ve CLI](docs/REMOTE_FORM_REPEAT.md).

**W4 izinli form metadata akışı:** ayrı iki aşamalı exact yerel izinle, görev sürerken onaylı form aşamalarındaki gerçek S1/S2 olayları içeriksiz özel outbox'a alınabilir. Yeni temel form-plan backend'inde Tasks ayrı attach ve scheduler yoklaması sunar; CLI alternatifi vardır. Gerçek site hakları ve eğitim hâlâ açık. [Kapsam](docs/REMOTE_FORM_LEARNING_STREAM.md).

**W4 altı-onaylı durum formu metadata akışı:** ayrı state-plan hash'ine bağlı yerel izin, ilk form onayı sırasında kaydedilip ayrıca bağlanır; scheduler altı onaylı aşamadaki gerçekten çağrılmış model olaylarını içeriksiz/dedup özel outbox'a alır. Temel form izni durum run'ında, Cookie planı her iki kapsamda reddedilir. Sentetik owned Chromium/Playwright MCP ve gerçek pinli Decider kanıtı gerçek hedef-site, bağımsız sonuç veya eğitim kabulü değildir. [Kapsam](docs/REMOTE_FORM_LEARNING_STREAM.md).

**Planlanan ilk web sürümü:** kullanıcı tarafından verilen uygulamayı Ubuntu'daki görünür Chromium/Playwright MCP ile sürme; görevler devam ederken hem Decider hem Bonsai için sürümlü site bilgisi, skill ve fine-tune veri adayları biriktirme. Review edilmiş ayrı S1/S2 dataset'leri ve readiness; ilk çalıştırılabilir S1 eğitimi/değerlendirme/promotion/rollback, S2 için ayrıca trainer/adapter uyumluluk kapısı. Bu kapsam mevcut sabit-görev pilotunda uygulanmış değildir. [Ürün sözleşmesi ve kabul kapıları](docs/WEB_APPLICATION_LEARNING.md).

**W5 sentetik S1 adapter adayı:** açık opt-in ile tek sentetik train kararından özel adapter dosyası üretilip ayrı gerçek CUDA sürecinde yeniden yüklenir; içerik-adresli özel raporla birlikte sonradan ağsız yeniden denetlenebilir. Rapor train ile varsa validation/test NLL ve sonlu-seçim doğruluğunu ayrı sayar; mevcut fixture'da held-out satır yoktur. Train karşılaştırması runtime deployment, promotion veya W5 ürün kabulü değildir. [Kapsam ve komut](docs/DATASET_ADAPTER_CANDIDATE.md).

**W5 izole inference smoke:** aynı özel aday, açık opt-in ile ayrı gerçek CUDA Decider inference sürecinde frozen base ile karşılaştırılır. Dataset'teki train/validation/test kararları ayrı sayılır; boş bölüm sıfırdır. Mevcut yayımlanmış fixture yalnız bir train kararı içerir; üç bölümlü ayrıca oluşturulmuş sentetik işçi probu gerçek held-out kalite, aktif serviste adapter deployment veya promotion değildir. [Kapsam ve komut](docs/DATASET_ADAPTER_RUNTIME_PROBE.md).

**W5 deneysel adapter registry kaydı:** `AdapterRegistry` özel sentetik candidate çiftini ve exact Decider pinlerini yeniden doğrulayıp yalnız açık verilen SQLite store'a disabled model/unknown-compatibility adapter/EXPERIMENTAL deployment kaydeder. Ayrı salt okunur CLI, kayıt sonrası artifact veya registry değişimini reddeder. Canlı oturuma bağlanmaz, active pointer değiştirmez; kalite veya promotion kabulü değildir. [Komut](docs/DATASET_ADAPTER_REGISTRY_INSPECT.md), [sınır](docs/REGISTRIES.md).

**W3 sentetik skill vaka bağı:** özel canonical vaka parametreleri, kayıtlı taslak/yapısal planın ilan edilen hash'lerine ve seçili S1 vakasında mevcut HTTPS form planının exact gövde hash'ine salt okunur bağlanabilir. Kaynak isteğini, form submit'ini veya sonucu doğrulamaz; review, aktivasyon ve eğitim açmaz. [Sınır ve CLI](docs/SITE_SKILL_CASE_BINDING.md).

**W3 owned sentetik form denemesi:** yeni oturum için `start --owned-synthetic-form-invocation`, private kaynak paketi ve TLS fixture'ıyla tek kullanımlık sabit formu Tasks'a bağlar. Gerçek Decider/Ubuntu Chromium-MCP, altı manuel onay, açık audit düğmesi ve kapanış izole managed supervisor testinde geçti; public port-8765 start yolu yalnız birim düzeyindedir. Sembolik skill, gerçek site sonucu veya eğitim kabulü değildir. [Başlatma](docs/FIRST_RELEASE.md), [kapsam](docs/SITE_SKILL_FORM_INVOCATION.md).

**W3 çalıştırılabilir sentetik S1 recipe:** ayrı sıralı artifact, skill'in ilan ettiği adımları mevcut form araçlarına bağlar; operatör ve MCP runtime iki izinli sıradan seçileni gerçekten uygular. Tek recipe farklı parametre vakalarında yeniden kullanılabilir; version-2 invocation her vakayı exact planlara bağlar. Yeni oturum için `start --owned-synthetic-form-recipe`, Tasks'ta sıralı adımları, bekleyen manuel onayı ve ayrı kaynak-bağlı audit sonucunu gösterir; eski v1 modu değişmez. Gerçek site, skill aktivasyonu ve eğitim değildir; ölçülmüş kabul sınırı STATUS'tadır. [Kapsam](docs/SITE_SKILL_FORM_RECIPE.md).

**W3 deneyimden executable recipe adayı:** doğrulanmış altı-aşamalı sentetik form koşusunun gerçek S1 olayları ve eylem sırası, açık operatör anlam/parametre eşlemesiyle özel değişmez skill/recipe adayına bağlanır. Kaynak yeniden denetlenerek aynı aday yeni gelişim girdilerinde kullanılabilir; ilk eylem öncesindeki admission kaydı sonradan source-bound audit ile doğrulanır. Semantik isimler manuel, bağımsız held-out/eğitim/aktivasyon kapalıdır. [Kapsam ve kabul](docs/SITE_SKILL_FORM_RECIPE_CANDIDATE.md).

**W3 Tasks'tan aday kaydı ve yeniden yürütme:** owned-v1 sentetik görev başarıyla bitip ayrıca denetlenince açık kaynak yükleme, düzenlenebilir semantik adlar, salt okunur aday önizleme, exact hash ile ayrı kayıt ve kaynak yeniden incelemesi sunulur. Kaydedilmiş aday, ayrı yeni gelişim girdisi/önizleme/onay ve altı manuel eylem onayıyla aynı recipe'yi yeni koşuda yürütür; ayrı audit kaynak kökenini doğrular. Özgün gösterim değişmez, özel execution bundle'ı sonradan ağsız denetlenebilir. Mevcut canlı oturum yükseltilmez; v2 recipe'den yeni gösterim türetilmez. Uygulama/test kanıtı [STATUS](docs/STATUS.md), [aday sözleşmesi](docs/SITE_SKILL_FORM_RECIPE_CANDIDATE.md) ve [yürütme sınırı](docs/OWNED_CANDIDATE_EXECUTION.md) içindedir.

**W3 kanıta bağlı insan incelemesi:** Tasks, kaydedilmiş aday ve denetlenmiş gelişim koşusu için ayrı exact-hash accept/revoke kaydı sunar. Yeni koşuya bağlama ayrı seçimdir; altı manuel onay korunur ve pending fill sırasında geri çekme POST öncesi durdurur. Receipt yeni süreçte ağsız yeniden incelenebilir; tarihsel başarı ile güncel kullanım durumu ayrılır. Bu sentetik review, skill aktivasyonu veya eğitim değildir. [Kullanım](docs/OWNED_CANDIDATE_REVIEW.md), [ölçülmüş kanıt](docs/STATUS.md).

**W3 sürümlü gelişim seçimi ve geri alma:** Tasks, ayrı incelenmiş immutable skill/recipe sürümlerini yayımlar; exact hash onayıyla kalıcı `development_selected` seçimini yeni koşuya bağlar. Gerçek managed kabulde A → B → açık rollback A, üç ayrı seçilmiş koşuda doğru recipe pinleriyle yürütüldü. Pending fill sırasında revoke POST'u engeller; seçim otomatik başka sürüme geçmez. Kapanmış supervisor sonrası yeni ağsız süreçte katalog ve tarihsel admission yeniden denetlendi. Bu üretim aktivasyonu, bağımsız kalite, eğitim veya yeniden başlatılmış backend'de görev devamı değildir. [Kullanım ve sınır](docs/OWNED_SKILL_RELEASES.md), [kanıt](docs/STATUS.md).

**W3 ayrı yeni oturumda seçilmiş skill kullanımı:** `preview-owned-skill-reuse` ve üç exact hash ile yeni `start`, temiz kapanmış owned-v1 kaynağını aynı özel DB ve sürüm geçmişiyle yeni runtime/controller'a bağlar. Eski görev/eylem/onay oynatılmaz; yeni Tasks koşusu ayrı seçim ve altı manuel onay ister. Gerçek pinli Decider/Chromium-MCP ile iki supervisor subprocess, yeni oturum UI yürütmesi, pending revoke ve kapanış sonrası audit geçti. Public port-8765 start handoff'u birim düzeyindedir; sentetik kabul gerçek site veya eğitim değildir. [Kurulum ve sınır](docs/OWNED_SKILL_REUSE.md), [güncel test sonuçları](docs/STATUS.md).

**W3 sentetik form yürütme bağı:** ayrı salt okunur CLI, gelişim vakasının exact gövdesini onaylı tamamlanmış `.invalid` form koşusuna, gerçek pinli S1 submit karar olayına ve taşıma readback'ine bağlar. Skill'in kendisi çalışmaz; site sonucu, bağımsız varyasyon, review ve eğitim açık kalır. [Kapsam](docs/SITE_SKILL_FORM_EXECUTION.md).

**W3 sentetik form kohortu denetimi:** ayrı salt okunur CLI, bir taslağın ilan edilen gelişim ve held-out vakalarını farklı altı-onaylı `.invalid` form koşularına tek frozen trajectory snapshot'ında bağlar; kaynak olay/doğrulama ID'leri held-out koşularda tekrar kullanılamaz. Bu farklı girdili taşıma/readback kanıtıdır, skill yürütmesi, semantik held-out bağımsızlığı, gerçek site sonucu veya aktivasyon değildir. [Kapsam](docs/SITE_SKILL_FORM_COHORT.md).

**İlk Ubuntu MCP dilimi:** `--desktop-mcp-manifest` ile aynı görünür Chromium'da mevcut yerel formu gerçek Playwright MCP üzerinden yürütme seçeneği eklendi. Mevcut onay/kontrol/bağımsız doğrulama korunur; genel web uygulaması veya öğrenme hattı henüz değildir. [Hazırlık, kullanım ve sınırlar](docs/DESKTOP_MCP_RUNTIME.md).

**Yeni opt-in sentetik uygulama akışı:** `--desktop-staging-mcp-manifest` veya güvenli kapatma sonrası yeni managed oturum için `./scripts/aos-v1 start --synthetic-staging` ile ağsız App → Draft → sabit Message → makbuz akışı görünür Ubuntu/Chromium/Playwright MCP'de dört ayrı manuel onay ve bağımsız doğrulamayla çalışır. Ayrı test konsolundaki explicit `--staging-fixture-port`, aynı sabit GET/POST akışını pinli yerel HTTP proxy'den geçirir. Tek POST, exact rota/gövde/origin ve TCP port guard sınırındadır; managed varsayılanı veya gerçek site yetkisi değişmez. Çalışan oturum sessizce yükseltilmez. [Kapsam](docs/SYNTHETIC_STAGING_WORKFLOW.md), [kanıt](docs/STATUS.md).

**Yeni W2/W4 sentetik kanıt dilimleri:** iki exact audited run ile değişmez sayfa taslağından yalnız yetki vermeyen retrieval adayı üretilir. Ayrı açık CLI, görev sürerken mevcut sentetik S1/S2 metadata çağrılarını private outbox'a artımlı ve restart-dedup biçiminde yazabilir; sentetik canvas'ta Bonsai sahnesi sonraki doğrulanmış Decider sonucuna gold olmayan ayrı S2 metadata bağıyla eklenir. Bu otomatik gerçek-site collector'ı, gerçek site verisi veya eğitim örneği değildir. [W2](docs/SITE_PAGE_RETRIEVAL.md), [W4](docs/LEARNING_EVENT_STREAM.md).

**W4 uzak rota kaynak denetimi:** tamamlanmış HTTPS rota görevinin exact profil/plan, ayrı onay ve readback kanıtı aynı frozen snapshot'ta gerçek S1/S2 model çağrısı kaynaklarıyla içeriksiz eşlenebilir. İstenen fakat çağrılmayan Bonsai rolü boş kalır. Bu salt okunur inceleme izinli canlı toplama, hak/redaction review veya training-ready veri değildir. [Kapsam](docs/REMOTE_LEARNING_SOURCE.md).

**W4 yerel metadata izin temeli:** exact bağlı rota run'ı için yerel operatör, en çok 24 saatlik ve yalnız içeriksiz S1/S2 metadata kapsamını iki aşamalı hash onayıyla özel dosyaya kaydedebilir. CLI yanında yeni uzak rota oturumunda authenticated Tasks paneli de aynı iki aşamalı kaydı sunar; hash'i göreve bağlamak ayrı eylemdir. Bu hak/hesap bağımsız doğrulaması veya eğitim izni değildir. [Kapsam](docs/REMOTE_LEARNING_CONSENT.md).

**W4 açık yerel metadata iptali:** host CLI'si exact izin hash'iyle değişmez iptal kaydı üretir; aktif poll bitince aynı özel outbox kökündeki yalnız bağlı SQLite'ı mantıksal olarak siler. Yanlış/bozuk kaynakta otomatik başka dosya silmez; otomatik retention veya fiziksel secure erase değildir. [Kapsam](docs/REMOTE_LEARNING_CONSENT.md).

**W4 host saklama süresi süpürücüsü:** ayrı CLI, her özel iznin `expires_at + retention_days` sınırını UTC'de denetler; vadesi dolmuş izin için operatör beyanı uydurmadan kalıcı iptal kaydı yazar ve yalnız verilen exact özel outbox kökündeki bağlı SQLite'ı mantıksal siler. Yeni managed backend, özel önceki oturum köklerini açılışta ve çalışırken saatte bir tarar; ayrı managed-root CLI kapalı dönem sonrası elle telafi sağlar. Başka kopyalar, kapalı makinede takvimsel çalışma veya fiziksel secure erase kapsanmaz. [Kapsam](docs/REMOTE_LEARNING_CONSENT.md).

**W4 retention çalışma görünümü:** yeni backend authenticated `/api/retention` yanıtında yalnız son tarama zamanı, durum ve toplam sayıları verir; özel izin hash'i/yolunu vermez. Development kartı bu canlı özeti tarihli geliştirme listesinden ayırır; eski backend'de endpoint yoksa durumu `unavailable` gösterir. [Sınır](docs/UI_RUNTIME.md).

**W4 açık uzak metadata poller'ı:** aynı exact izin hash'iyle ayrı CLI, uzak rota görevi sürerken gerçek pinli S1 karar/onay ve varsa S2 escalation metadata adaylarını özel append-only outbox'a artımlı kaydeder. Her izinli run kendi özel outbox'ına gider; önceki kök-v1 kayıtları exact izinine bağlı kalır. Bu incelenmiş dataset veya eğitim değildir. [Kapsam](docs/REMOTE_LEARNING_STREAM.md).

**W4 görev-başı metadata hook'u:** yeni uzak rota backend oturumunda, görev ilk manuel onayı beklerken exact özel izin hash'i authenticated Tasks panelinden bir kez bağlanabilir. Scheduler sonraki onay ve settle güvenli noktalarında aynı içeriksiz outbox'ı otomatik yoklar; hata görev sonucunu değiştirmez. Hak/hesap doğrulaması, retention, dataset ve eğitim değildir. [Kapsam](docs/REMOTE_LEARNING_STREAM.md).

**W2 uzak rota kanıtı önizlemesi:** kayıtlı görev için owner-only URL listesinden ağsız `plan-remote-routes` ile exact sıralı plan üretilebilir; tamamlanmış sıralı HTTPS okuma run'ından profil/plan ve ayrı onay/doğrulama bağları yeniden denetlenerek yalnız URL/title/H1 hash'leri salt okunur projekte edilir. Bu site belleği aktivasyonu, öğrenme izni veya gerçek hedef kabulü değildir. [Kapsam](docs/REMOTE_ROUTE_EVIDENCE.md).

**W2 sınırlı link gözlemi:** salt okunur rota okuması her sayfada en çok 64 bağlantıyı yalnız önceden izinli rota indeksleri ve kayıt dışı bağlantı sayısı olarak gözler; iki tarayıcı readback'i eşleşirse tarihsel rapora ekler. Bağlantı hedefleri dışarı çıkarılmaz, yeni rota açılmaz, fingerprint ve W2 kabulü değişmez. [Kapsam](docs/REMOTE_ROUTE_EVIDENCE.md).

**W2 link örneği karşılaştırması:** iki tarihsel rota koşusunda tam ve eşleşmiş tarayıcı örnekleri varsa, sayfa fingerprint'inden ayrı içeriksiz link-örneği değişim biti gösterilir. Kayıt dışı hedef eşitliği, güncellik veya yeni yürütme yetkisi ispatlanmaz. [Kapsam](docs/REMOTE_ROUTE_CHANGE.md).

**W2 çıkış bağı güncellik kapısı:** manuel sayfa taslağında giden sayfa anahtarları varsa, iki tarihsel ve yeni görevdeki tam link örnekleri eşleşmeden sembolik bilgi tekrar kullanılmaz. Aynı başlık/H1 altında link değişimi `stale` olur; aday her anahtarı gözlenen izinli rota indeksine ve exact hedef taslak hash'ine bağlar; bu, anahtarların semantik anlamını kanıtlamaz. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**W2 iki-koşu uzak rota değişim önizlemesi:** aynı kayıtlı profil ve exact planla tamamlanmış iki HTTPS okuma run'ının içeriksiz parmak izleri aynı audited kaynak durumunda karşılaştırılır; değişen indeksler gösterilir. Bu güncel site bilgisi, görev-içi retrieval veya eğitim yetkisi değildir. [Kapsam](docs/REMOTE_ROUTE_CHANGE.md).

**W2 gözlenen gezinme grafiği adayı:** iki tarihsel onaylı/readback'li rota koşusundan değişmeyen planlı sayfalar ve yalnız tam/eşit sınırlı link örneğiyle gözlenen planlı kenarlar otomatik çıkarılır. Aynı managed oturumdaki iki tamamlanmış rota görevi authenticated Tasks ekranında EN/TR, salt okunur karşılaştırılabilir. Manuel semantik sayfa anahtarı, site kapsamı, review veya yeni yetki oluşmaz. [Kapsam](docs/REMOTE_NAVIGATION_GRAPH.md).

**W2 uzak rota/taslak aday bağı:** değişmeyen iki tarihsel HTTPS rota readback'i, exact profil/tenant/rol/origin ve güncel değişmez manuel sayfa taslağıyla salt okunur eşlenebilir. Aday inceleme, güncellik, görev-içi retrieval veya eğitim yetkisi vermez. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE.md).

**W2 özel metadata inceleme/yeniden kullanım:** exact aday hash'i ve açık metadata-only onayıyla tarihsel iki koşu için owner-only, değişmez review kaydı tutulabilir. Ayrı tamamlanmış taze rota koşusu aynı profil/plan/sayfa ve varsa eşlenmiş hedef rota parmak izlerini yeniden doğrularsa yalnız sembolik sayfa anahtarları döner; sayfa/profil ardılı veya rota değişiminde durur. Bu görev-içi retrieval, gerçek hesap/site doğrulaması veya W2 kabulü değildir. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**W2 görev-içi sembolik metadata denemesi:** doğrudan backend veya yeni managed `aos-v1 start` için açık review/source opt-in, geçmiş iki koşuyu tekrar denetler; yeni oturumun kendi ayrı onaylı kaynak rota GET/readback'i ve varsa eşlenmiş tüm giden hedeflerin ayrı readback'leri eşleşirse yalnız sembolik sayfa anahtarlarını sonraki Decider kararına ekler. Hedef değişmişse `stale` olayı yazar ve bağlam verilmez; yalnız iki rotalı kaynak→hedef planda hedef sonrası başka karar yoksa ipucu kullanılmaz. URL, onay ve eğitim yetkisi değişmez. Managed yol ağsız/mock ve backend snapshot-ret testiyle doğrulandı, gerçek yeni managed process/site kabulü değildir. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**W2 canlı metadata görünürlüğü:** Tasks paneli yapılandırılmış review hash'inden ayrı olarak, yalnız son rota job'ının kendi onaylı readback'inden sonra içeriksiz eşleşti/eski durumunu ve rota sırasını EN/TR gösterir. Kaynak eşleşmesi açılmamış hedefin tazeliği değildir; bu site sonucu veya öğrenme yetkisi gösterimi değildir. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**W2 denetim maliyeti:** iki tarihsel koşu ve sayfa taslağı önizlemesi çağrı başına tek tutarlı SQLite snapshot'ı kullanır; her koşunun onay/doğrulama denetimi sürer. Bu DB kopyası sayısını azaltır, ancak uçtan uca görev hızı iyileşmesi ayrıca ölçülmelidir. [Kapsam](docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**W1 HTTPS form taşıma prototipi:** exact profil/görev/plan için giriş GET → form fill → tek POST → makbuz GET, ağsız görünür Ubuntu Chromium/Playwright MCP'de pinli uygulama işçisi ve dört ayrı kalıcı scheduler eylem onayıyla sınanır. `.invalid` sentetik hedef backend varsayılanıdır; public hostname yalnız aynı plan hash'iyle ayrı grant verilirse açılır. Yeni managed `plan-remote-form` → `preview-remote-form` → güvenli sonraki `start` yolu public planı ve özel değeri owner-only dosyalardan pinler; çalışan oturumu değiştirmez. Public-görünümlü `example.com` hostu yalnız yerel TLS/DNS fixture ile sınandı, gerçek dış site isteği yapılmadı. Onayda exact URL, alan adı ve POST gövde hash'i görünür; özel değer görünmez. Append-only job/run/plan bağı ve makbuz readback'i yalnız taşıma sonucunu doğrular; hesap/secret veya gerçek uygulama sonucu değildir. [Kullanım](docs/FIRST_RELEASE.md), [sınır](docs/WEB_HTTPS_FORM_TRANSPORT.md).

**W1 sıralı çoklu alan HTTPS formu:** aynı tek-POST sınırında iki–sekiz sıralı gerekli metin, e-posta veya tek seçim alanı ya da exact değeri önceden pinli form-içi gizli alan, DOM sırası ve exact URL-encoded gövde hash'iyle seçilebilir. E-posta alanı tarayıcının yerleşik geçerliliğinden de geçmeden POST'a ulaşmaz; parola alanı kabul edilmez. Ağsız `plan-remote-form-fields` owner-only kaynak alan listesinden canonical `--remote-form-fields-file` üretir; bu dosya managed plan/preview ve güvenli yeni `start` yolunda tek alan ad/değer seçeneklerinin yerini alır. Backend yalnız alan adlarını ilan eder. Owned sentetik TLS/Ubuntu/Chromium/MCP yürütme ve managed backend/UI kabul testleri ayrı ayrı geçti; değerler onay/status/DB'ye yazılmaz. Gerçek site/görev kabulü değildir. [Kullanım](docs/FIRST_RELEASE.md), [sınır](docs/WEB_HTTPS_FORM_TRANSPORT.md).

**Opsiyonel form durum okuması:** aynı exact forma bağlı `plan-remote-form-state` → `preview-remote-form-state` → yeni `start` yolu ayrı public state-plan grant'i ve özel plan kopyasıyla açılır. Girişten sonra ve makbuzdan sonra iki ayrı onaylı host HTTPS GET, toplam altı onay ve ilan edilen önce/sonra HTML SHA-256 geçişi vardır. Owned sentetik TLS/Ubuntu/MCP fixture ve ağsız manager testleri geçti; gerçek site, hesap veya semantik uygulama sonucu doğrulanmadı. Canlı oturum yükseltilmedi. [Kapsam](docs/WEB_HTTPS_FORM_TRANSPORT.md).

**Opsiyonel durum işaretçisi:** aynı durum planı, tekil bir HTML `id` öğesinin önce/sonra normalize metin hash'lerini de pinleyebilir. Altı onay değişmez; eksik/çift işaretçi veya yanlış metin kapalı kalır, onayda yalnız hash'ler görünür. Bu ilan edilen işaretçi geçişidir, bağımsız iş sonucu veya hesap kanıtı değildir.

**Opsiyonel gönderilen değer–işaretçi bağı:** exact özel form alanının normalize değeri beklenen sonra-işaretçi hash'iyle ağdan önce karşılaştırılır; ayrı onaylı readback eşleşirse içeriksiz doğrulama biti oluşur. Bu kalıcı uygulama sonucu, hesap/tenant veya gerçek hedef kabulü değildir. [Sınır](docs/WEB_HTTPS_FORM_TRANSPORT.md).

**W3 sentetik skill vaka/durum bağı:** altı-onaylı sentetik form koşusunda özel vaka alanı, exact POST, sonra-marker readback'i, submit S1 kaynağı ve iki ayrı verification salt okunur denetlenebilir. Bu skill executor, bağımsız site sonucu veya aktivasyon değildir. [Kapsam](docs/SITE_SKILL_FORM_EXECUTION.md).

**Opsiyonel statik Cookie:** public HTTPS formuna `plan-remote-form-cookie` → `preview-remote-form-cookie` → güvenli yeni `start` ile owner-only özel Cookie dosyası ve exact SHA-256 pini eklenebilir. Yalnız aynı-origin onaylı host GET/POST'ları kullanır; ham Cookie argv/DB/UI'ye yazılmaz, hash append-only kaydedilir. Yerel TLS fixture testi vardır; login/yenileme, hesap ve uygulama sonucu kabulü yoktur. [Sınır](docs/WEB_HTTPS_FORM_TRANSPORT.md).

**W1 statik JS/CSS/görsel paketi:** kayıtlı HTTPS profil/görevi için `plan-remote-static-assets` → `preview-remote-static-assets` ağsız owner-only exact plan üretir. Giriş URL'si sorgusuzdur; JS/CSS/görsel URL'lerinde yalnız sabit, sınırlı ve sır içermeyen canonical sorgu olabilir. Güvenli yeni managed `start --remote-static-assets-plan-file` özel planı hash'le pinler; `browser_remote_static_assets` görünür ağsız Ubuntu Chromium/Playwright MCP görevinde giriş ve planlı varlıkları tek kalıcı manuel onaydan sonra yükler, başka istekleri engeller ve içeriksiz readback'i append-only run'a bağlar. Owned sentetik TLS fixture, private manager ve EN/TR arayüz testleri geçti; çalışan eski oturum yükseltilmez, gerçek site veya uygulama sonucu doğrulanmadı. [Kullanım ve sınır](docs/WEB_STATIC_ASSETS.md).

**Statik sorgu için canlı backend kapısı:** Web applications v1/v2 taslakları sorgulu statik URL'yi ancak authenticated backend'in `1.1` yetenek yanıtında exact destek işaretini görünce önizler/kaydeder. Eski/erişilemeyen backend'de sorgusuz taslak kullanılabilir; Development durumu canlı gösterir. Bu görev veya site yetkisi değildir. [Arayüz sınırı](docs/UI_RUNTIME.md).

**W1 exact JSON GET managed opt-in:** ayrı v2 plan, exact aynı-origin JS/CSS yanında en çok dört read-only JSON GET'i pinli ağsız Ubuntu Chromium/Playwright MCP'de DOM'a ulaştırır; POST, off-plan istek ve cookie kapalıdır. JSON ve statik varlık URL'lerinde yalnız önceden pinli, sınırlı ve sır içermeyen canonical sorgu olabilir; giriş URL'si sorgusuz kalır. CLI `plan-remote-readonly-data`/`preview-remote-readonly-data` veya Web applications ağsız exact-hash plan kaydı ve güvenli yeni `start --remote-readonly-data-plan-file`, private planı pinler. Tasks panelinde exact URL'ler ve tek kalıcı manuel onay, append-only job/run bağı ve içeriksiz JSON hash doğrulaması vardır. Sentetik TLS/Chromium ile gerçek pinli Decider'da sınandı; gerçek hedef/site sonucu ve W1–W6 kabulü yoktur. Ayrı W4 metadata izni yalnız açık seçilirse kullanılabilir. [Kullanım ve sınır](docs/WEB_READONLY_DATA_BUNDLE.md).

**W1 planlı HTML rota sorgusu:** iki–sekiz rotalı salt okunur görevde ilk giriş URL'si sorgusuz kalır; sonraki exact aynı-origin HTML rotaları sınırlı, sabit ve sır içermeyen canonical sorgu içerebilir. Her rota ayrı insan onayı, tek host TLS GET ve görünür Ubuntu Chromium/Playwright MCP readback'i ister; yanlış sorgu socket/TLS öncesi reddedilir. Owned sentetik fixture'da gerçek pinli Decider'la sınandı; gerçek hedef-site, hesap, veri hakkı veya W1–W6 kabulü değildir. [Kapsam](docs/WEB_READONLY_ROUTES.md).

**W1 statik planı konsoldan hazırla:** Web applications paneli kayıtlı tek sayfalı uzak görev için exact JS/CSS URL/MIME listesini ağsız önizler, plan SHA-256 onayıyla özel dosyaya kaydeder ve güvenli yeni managed oturumun `preview-remote-static-assets`/`start` komutlarını gösterir. Panel komutları çalıştırmaz; site isteği görevde ayrıca manuel onay ister. [Akış](docs/WEB_STATIC_ASSETS.md).

**W4 statik görev kaynak denetimi:** tamamlanmış exact statik-bundle run'ının profil/plan, tek manuel onay ve tarayıcı readback bağı tek frozen snapshot'ta yeniden denetlenir; gerçekten çağrılmış pinli Decider S1 ve varsa bağlı Bonsai S2 olay ID'leri içeriksiz listelenir. Sentetik owned Chromium testinde gerçek Decider S1 vardır, Bonsai çağrılmadığı için S2 boştur. Bu canlı collector, veri hakkı, gold sonuç veya eğitim değildir. [Sınır](docs/REMOTE_STATIC_LEARNING_SOURCE.md).

**W4 JSON görev kaynak denetimi:** ayrı v2 JSON-bundle run'ı için exact plan, tek manuel onay, authenticated insan kaydı, JS/CSS ve JSON yanıt hash'leri ile bağımsız readback aynı frozen snapshot'ta yeniden denetlenir. Gerçek pinli Decider S1 olay ID'si içeriksiz listelenir; çağrılmamış Bonsai S2 boş kalır. Bu salt okunur rapor kendi başına v2 metadata izni/collector, hak incelemesi veya dataset/eğitim açmaz. [Sınır](docs/REMOTE_READONLY_DATA_SOURCE.md).

**W4 JSON görevinde açık metadata akışı:** ayrı v2 izin kapsamı Tasks veya CLI ile exact run/plan/roller için iki aşamada kaydedilip ayrıca bağlanır; scheduler başarılı onaylı open sonrası gerçek pinli S1/S2 metadata adaylarını izin-başına özel outbox'a dedup yazar. Owned ağsız Chromium'da gerçek Decider **1 S1**, çağrılmamış Bonsai **0 S2**; revocation ve scope retleri sınandı. Harici hak/gold/dataset/eğitim veya gerçek site kabulü değildir. [Kapsam](docs/REMOTE_READONLY_DATA_STREAM.md).

**W2 JSON taşıma değişim önizlemesi:** aynı exact v2 profil/planla iki ayrı onaylı JSON görev koşusunun entry, JS/CSS, JSON yanıt ve sayfa title/H1 hash'leri tek frozen snapshot'ta karşılaştırılır. JSON gövdesi değişip görünen başlık değişmese bile ilgili kaynak indeksi içeriksiz işaretlenir. Bu geçmiş taşıma farkı site anlamı, güncellik, görev-içi retrieval veya öğrenme yetkisi değildir. [Kapsam](docs/REMOTE_READONLY_DATA_CHANGE.md).

**W2 sabit JSON kanıtından sayfa taslağı adayı:** iki ayrı onaylı v2 koşunun tüm taşıma hash'leri aynıysa, exact profil/plan/giriş URL'siyle eşleşen güncel manuel sayfa taslağı salt okunur aday olarak bağlanır. JSON farkı, eski profil/sayfa revizyonu ve gözlenmemiş çıkış-link iddiası reddedilir. Bu semantik review, canlı tazelik veya görev-içi retrieval değildir. [Kapsam](docs/REMOTE_READONLY_DATA_KNOWLEDGE.md).

**W2 JSON metadata inceleme ve taze koşu denetimi:** exact aday hash'i açıkça onaylanırsa özel değişmez metadata review kaydı oluşur. CLI yanında yeni v2-plan oturumunun authenticated Tasks paneli, aynı oturumun iki başarılı koşusu/kayıtlı taslak hash'iyle aday önizleme, iki aşamalı inceleme ve ayrı tamamlanmış koşu recheck'i sunar. Tüm taşıma parmak izleri sabitse yalnız sembolik sayfa/landmark anahtarları salt okunur döner; JSON kayması ve ardıl sayfa/profil reddedilir. Bu görev-içi retrieval veya gerçek-site kabulü değildir. [Kapsam](docs/REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md).

**W2 JSON review canlı readback bağı:** açık dört özel kaynak piniyle yeni v2 managed veya direct backend oturumu başlatılabilir. Onaylı yeni JSON-bundle readback'i tarihsel review parmak iziyle eşleşirse yalnız sembolik sayfa anahtarı içeriksiz olay ve Tasks durumunda görünür; JSON kaymasında `stale` olur. Karar modeline bu anahtar verilmez, site sonucu/hak veya W2 gerçek kabulü doğmaz. [Kullanım ve sınır](docs/REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md).

**W2 JSON taslağını konsoldan hazırla:** yeni v2-plan Tasks paneli iki değişmeyen, onaylı koşudan insanın seçtiği sayfa anahtarına özel incelenmemiş taslak üretir; ayrı exact dosya-hash onayıyla kaydeder ve hash'i metadata adayına aktarır. JSON/DOM içeriğini veya özel kaynak yolunu API'ye açmaz; semantik inceleme, görev yetkisi veya gerçek-site kabulü değildir. [Kapsam](docs/REMOTE_READONLY_DATA_PAGE_DRAFT.md).

**W4 açık statik metadata akışı:** exact statik run ilk onayı beklerken ayrı scope'lu, iki aşamalı yerel izin Tasks veya CLI ile kaydedilip ayrıca göreve bağlanabilir; yeni static-plan backend scheduler hook'u ve bounded host CLI, onaylı `browser.static.open` sonrası gerçek S1 ve varsa S2 kaynaklarını izin-başına özel append-only outbox'a içeriksiz/dedup alır. Sentetik owned MCP ve pinli gerçek Decider sınandı; harici veri hakkı, gold/review/dataset/eğitim ve gerçek site kabulü yoktur. [Kullanım ve sınır](docs/REMOTE_STATIC_LEARNING_STREAM.md).

**W2 yerel profil–run bağlı aday:** iki ayrı opt-in pinli ve doğrulanmış Start→Details run'ı, değişmez sayfa taslağıyla aynı frozen snapshot'ta eşleşirse ayrı salt okunur CLI `candidate_local_profile_bound` üretir. Pinned gerçek Decider/Ubuntu/Chromium/MCP ile iki koşuda sınandı; gerçek site bilgisi, otomatik retrieval veya öğrenme izni değildir. [Kapsam](docs/SITE_PAGE_RETRIEVAL.md).

**Yeni W3/W4/W5 opt-in tanıları:** doğrulanmış ağsız staging run'ından içeriksiz, incelenmemiş S1 skill adayı salt okunur türetilir; `./scripts/aos-v1 start --synthetic-learning` yeni managed oturumda görev başına varsayılan kapalı S1/S2 metadata kaydı seçeneği sunar. Ayrı GPU/CPU testi pinned Decider'dan yalnız iki seçenek-token satırının dağıtılamayan özel adayını üretir. Hiçbiri gerçek site uzmanlığı, eğitim yetkisi veya promotion değildir. [W3](docs/STAGING_SKILL_CANDIDATE.md), [W4](docs/LEARNING_EVENT_STREAM.md), [W5](docs/DECIDER_ROW_CANDIDATE.md).

**S2 görsel aday bağı:** opt-in gerçek Bonsai+Decider sentetik SAVE görevindeki capture→scene→S1 kararı→onaylı click→bağımsız outcome zinciri içeriksiz ve salt okunur S2 adayına projekte edilir. SAVE sonucu S2 gold sayılmaz; review, aktivasyon ve eğitim kapalıdır. [Kapsam](docs/VISION_SKILL_CANDIDATE.md).

**Aynı görevde iki rolün adayı:** `aos.vision_skill_candidate --dual-role`, tek audited sentetik SAVE run'ından ayrı Decider/S1 doğrulanmış eylem adayı ve Bonsai/S2 downstream-bağlı fakat gold olmayan sahne adayını tek snapshot'ta döndürür. İkisi de incelenmemiş, içeriksiz ve eğitim/aktivasyon yetkisizdir. [Kapsam](docs/VISION_SKILL_CANDIDATE.md).

**W5 tanısal optimizer smoke:** iki ayrı incelenmiş sentetik fixture üzerinde pinned Decider'ın frozen logits'inden tek geçici CPU sıcaklık skalerine gradyan/adım uygulanır; model ağırlıkları, checkpoint ve deployment değişmez. Ayrı özel seçenek-satırı adayı yeniden yüklenip tek sentetik held-out girdide read-only sınanabilir; bu deployable adapter, tam fine-tune veya W5 kabulü değildir. [Kalibrasyon](docs/DECIDER_CALIBRATION_SMOKE.md), [satır adayı](docs/DECIDER_ROW_CANDIDATE.md).

**W3 sentetik kaynak isteği bağı:** private kaynak inceleme seçili audited model çağrılarının canonical istek hash'lerini içeriksiz verir; özel skill provası planın kaynak hash'lerini bunlarla eşleştirir. Yanlış veya canonical olmayan istek reddedilir. Parametre değeri, gerçek skill yürütmesi, held-out sonuç, review ve aktivasyon hâlâ yoktur. [Kapsam](docs/SITE_SKILL_REHEARSAL.md).

**W3 iki rolün taslak görünümü:** yeni rota-plan Tasks paneli, sunucu-pinli profildeki S1 ve S2 özel skill taslaklarını ayrı, salt okunur listeler. Rota görevi Bonsai çağırmaz; S2 listesi kaynak/sonuç doğrulaması, aktivasyon veya eğitim değildir. [Sınır](docs/REMOTE_SITE_SKILL_PROVENANCE.md).

**Dördüncü sınırlı görev ve canlı imleç:** yeni managed başlangıçta ağsız Start→Details gezinmesi yalnız pinned Playwright MCP ile ayrı iki manuel onay ve bağımsız doğrulamayla kullanılabilir; mevcut formun CDP taşıması korunur. Bilgisayar paneli owned Ubuntu X11 imlecinin sayısal konumunu ve noVNC üzerinde işaretçisini salt okunur gösterir. Çalışan eski backend kendiliğinden güncellenmez; gerçek hedef web uygulaması veya serbest görev değildir. [Pilot](docs/FIRST_RELEASE.md), [imleç](docs/DESKTOP_POINTER.md).

**Hedef uygulama profili:** site/origin/tenant/rol/görev kapsamı ve ayrı Decider/Bonsai öğrenme talepleri CLI veya authenticated konsolda önizlenip exact hash onayıyla değişmez taslak olarak kaydedilir. UI yeni sürüm-1 ve exact ebeveyn seçilen ardıl taslakları açar; her iki arayüz de ardıl önizlemesinde değişmez ebeveyn zincirini salt okunur denetler. Kayıt ağ erişimi, görev yürütme veya veri toplama yetkisi vermez; gerçek runtime bağı sonraki dilimdir. [Kullanım ve sınırlar](docs/WEB_APPLICATION_PROFILE.md).

**Açık HTTPS giriş ön kontrolü:** kayıtlı staging/production profilinin exact hash'i iki kez verilirse ayrı host CLI tek, yönlendirmesiz HTTPS HTML giriş isteği yapabilir; DNS public IP, TLS hostname ve yanıt boyutu sınırları vardır. Aynı yanıttan yalnız statik HTML form/betik/stil/görsel/parola, kaynak-origin ve meta refresh yönlendirme kapsamı sayıları çıkarılır; HTML saklanmaz veya basılmaz. Opsiyonel taslak görev bağı exact hash onayıyla istek öncesi yeniden doğrulanır. Bu gerçek dış ağ isteği yapabilen bir komuttur, yalnız yetkili hedefte kullanılmalıdır. Tarayıcı hâlâ ağsızdır; dinamik sayfa, görev, hesap, veri toplama veya eğitim izni oluşmaz. [Komut ve sınırlar](docs/WEB_HTTPS_PREFLIGHT.md).

**Konsoldan tek HTTPS giriş kontrolü:** kayıtlı staging/production taslağı için Web Applications ekranında exact URL, profil hash'i ve açık tek-GET yetki beyanıyla aynı host preflight'ı bir kez çalıştırabilirsiniz. Bu gerçek dış isteğe dönüşebilir; yalnız yetkili hedefte kullanın. 1.2 raporunda ilk HTML'nin içeriksiz yapısal sayıları ve POST/form eylemi sinyalleri görünür; eski 1.0 backend yalnız taşıma sonucunu, 1.1 backend önceki sayıları gösterir. Bu tarayıcı işi, hesap/sonuç kanıtı veya öğrenme izni değildir. [Sınırlar](docs/WEB_HTTPS_PREFLIGHT.md).

**Konsoldan özel uzak görev/rota taslağı:** kayıtlı staging/production profil için görev anahtarı, bağımsız doğrulayıcı referansı ve 1–8 sayfa bütçesiyle ağsız görev önizlenir; exact hash onayı `data/web-task-drafts/<sha256>.json` owner-only dosyasını yazar. 2–8 sayfada ayrı exact sıralı URL listesi ve plan hash onayı `data/web-route-drafts/<sha256>.json` üretir. Yeni managed oturumda iki dosya ayrıca pinlenir; kayıt mevcut oturumu değiştirmez veya site/hesap/veri yetkisi vermez. [Kullanım](docs/FIRST_RELEASE.md), [sınır](docs/WEB_APPLICATION_PROFILE.md).

**Opt-in tek HTTPS giriş görevi:** host üzerindeki exact GET/TLS denetimi private Unix socket üzerinden ağsız görünür Ubuntu Chromium'a bağlandı. Pinli Playwright MCP işçisi, scheduler/gateway, ayrı manuel onay, onay sonrası tek kullanımlık istemci sırrı ve append-only profil–job/run bağı yalnız tek giriş URL'sini açar; readback yalnız URL ve başlık/H1 SHA-256 parmak izlerini saklar. Gerçek pinli Decider, ayrı onay isteğiyle sentetik TLS görevini hem API hem mocksuz EN/TR konsol arayüzünden geçmiştir; bu gerçek site iş akışı, hesap, veri toplama veya eğitim değildir. Çalışan managed oturum sessiz yükseltilmez. [Kapsam ve test](docs/WEB_HTTPS_RELAY.md).

**Yönetilen uzak giriş opt-in:** çalışan oturuma dokunmadan `./scripts/aos-v1 preview-remote-entry --remote-entry-profile-sha256 <sha256> --remote-entry-task-file data/<özel-görev>.json` ile kapsam ve hash denetlenir; yeni gerçek-model oturumda aynı seçeneklerle `./scripts/aos-v1 start` kullanılır. Profil kayıtlı, görev dosyası owner-only `0600` olmalıdır; manager canonical kopyayı hash'le pinler. Çalışan oturum değiştirilmez; bu yalnız onaylı tek GET denemesidir. [Deneme sınırı](docs/FIRST_RELEASE.md).

**W1 temelleri:** profil–görev–MCP runtime için yetki vermeyen hash-bağlı taslak ve owned canlı runtime'ın salt okunur kimlik gözlemi, mevcut model çağrılarından içeriksiz S1/S2 kanıt inceleme CLI'si ve ağsız iki-sayfa görünür MCP görev akışı eklendi. Görev, gerçek pinned Decider ile iki ayrı onay ve bağımsız sonuç doğrulamasından geçti; iki S1 çağrısının metadata olayları aynı run'da doğrulanmış sonuçlara bağlandı, eğitim için hazır değildir. Bu hedef web uygulaması veya canlı veri toplama kabulü değildir. [Profil bağı](docs/WEB_APPLICATION_PROFILE.md), [öğrenme olayları](docs/LEARNING_EVENTS.md), [MCP kapsamı](docs/DESKTOP_MCP_RUNTIME.md), [URL kapsam kontrolünün sınırları](docs/WEB_REQUEST_SCOPE.md).

**W1 yerel fixture ek kimlik kapısı:** explicit sentetik profil+pin verilirse iki-sayfa gezinme veya dört adımlı staging MCP görevinde başlatma öncesi, runtime açılışı sonrası ve gateway eylemleri öncesi owned kimlik yeniden denetlenir. Opt-in profilin exact loopback origin portu worker/guard portuna bağlanır; Chromium HTTP isteği aynı porttaki yerel proxy üzerinden exact origin/rota kuralına girer. Varsayılan görev yolu değişmez. Bu yalnız sentetik HTTP fixture bağıdır, gerçek site yetkisi değildir. [Gezinme](docs/LOCAL_NAVIGATION_ADMISSION.md), [POST'lu staging](docs/LOCAL_STAGING_ADMISSION.md).

**W1 tarayıcı istek kapısı provası:** guard'lı yerel MCP sayfalarında Playwright `page.route` exact URL+yöntem dışını sunucudan önce durdurur; gerçek Chromium'da izinli sayfa ve engellenen off-route denendi. Ağsız Docker, Landlock ve fixture sunucusu korunur. Bu genel web ağı veya HTTPS site politikası değildir. [Sınır](docs/DESKTOP_MCP_RUNTIME.md).

**W1 sentetik profil–run kaydı:** opt-in pinli gezinme/staging run'ı açıldığında profil hash'i, canonical pin ve child browser runtime kimliği private trajectory DB'ye append-only bağlanır; önceki run'lar backfill edilmez. Bu kayıt gerçek hesap/site yetkisi veya öğrenme verisi izni vermez. [Kapsam](docs/LOCAL_STAGING_ADMISSION.md).

**Sentetik W1/W4 metadata checkpoint:** mevcut audited sentetik run'daki S1/S2 olayları role ve kaynak snapshot'ına bağlı private, elle tetiklenen değişmez kayda alınabilir. Bu canlı collector, artımlı kuyruk veya eğitim verisi değildir; kaynak değişirse aynı anahtar kapalı kalır. [Sınır](docs/LEARNING_EVENT_OUTBOX.md).

**W2 site bilgisi taslağı:** tek değişmez profil sürümü/tenant/rol için özel, içerik adresli sembolik sayfa kayıtları ve açık stale karşılaştırması eklendi. Bunlar elle girilen, kanıtlanmamış taslaklardır; canlı keşif, yürütme veya eğitim yetkisi vermez. [Kullanım ve sınırlar](docs/SITE_KNOWLEDGE.md).

**W2b sentetik sayfa kanıtı:** mevcut iki-sayfa yerel MCP görevinin bağımsız doğrulanmış sonuçlarından salt okunur içeriksiz parmak izi çıkarılır. Profil/gerçek site bağlanmaz, otomatik kayıt veya eğitim yapılmaz. [Kanıt sınırı](docs/SITE_PAGE_EVIDENCE.md).

**W2 iki-koşu değişim incelemesi:** iki açıkça seçilmiş audited sentetik run/verification için aynı snapshot ve sayfa anahtarındaki parmak izleri salt okunur karşılaştırılır. Sonuç gerçek site değişimi veya yürütme yetkisi kanıtı değildir. [Kullanım ve sınırlar](docs/SITE_PAGE_CHANGE.md).

**W2c karşılaştırma ve W3/W4 temelleri:** açık seçilmiş sayfa taslağı ile sentetik doğrulanmış fingerprint yalnız yetki vermeyen karşılaştırılır; S1/S2 için özel, değişmez sembolik skill taslakları ve rol bazlı içeriksiz eğitim-adayı engel raporu eklendi. Hiçbiri gerçek site bağını, otomatik skill/label üretimini veya fine-tune'u tamamlamaz. [Sayfa karşılaştırması](docs/SITE_PAGE_EVIDENCE.md), [skill taslağı](docs/SITE_SKILL.md), [aday engelleri](docs/LEARNING_CANDIDATE.md).

**W3b kaynak incelemesi:** skill taslakları CLI ile hash onayıyla kaydedilebilir; seçili taslağın event/verification iddiaları tek audit edilmiş run'a karşı yalnız metadata düzeyinde kontrol edilir. Eşleşme profil/gerçek site veya eğitim yetkisi değildir. [CLI](docs/SITE_SKILL.md), [kanıt sınırı](docs/SITE_SKILL_PROVENANCE.md).

**W3 sentetik kaynak provası:** exact skill/plan/audited snapshot seçimi kaynak-ID ve yapısal varyasyon denetimini bir raporda toplar; parametre varyantı gerçek run input'una bağlanamadığı için sonuç daima engellidir. Skill yürütme, başarı, review veya aktivasyon değildir. [Sınır](docs/SITE_SKILL_REHEARSAL.md).

**Web profil envanteri ve W3c yapı provası:** authenticated konsol taslak profil raporlarını listeler; yeni backend'de açık önizleme ve hash onayıyla sürüm-1 taslak kaydı da sunar. Çalışan eski backend yeni POST endpoint'leri için sonraki başlangıcı bekler; bu sırada UI kayıt yapılmadığını bildirir. Ayrı sentetik beceri varyasyon planı yalnız yapıyı denetler. [Profil](docs/WEB_APPLICATION_PROFILE.md), [varyasyon sınırı](docs/SITE_SKILL_VALIDATION.md).

**Geliştirme görünümü:** konsoldaki EN/TR **Development / Geliştirme** paneli tamamlanan dar dilimleri ve yapılacakları sayılabilir kontrol maddeleri olarak yüzdeleriyle gösterir; W1–W6 ürün kabulü ayrı görünür. Yüzdeler eşit ağırlıklı liste dağılımıdır, süre veya hazır-ürün tahmini değildir. Ayrı canlı bölüm sunucunun son oturum görevlerini, son trajectory run'larını, doğrulanmış son run'ın tamamlanmış S1/S2 çağrı toplamlarını ve yalnız exact izinliyse uzak metadata aday sayılarını ayrı gösterir; eski backend'de çağrı görünümü son 10 ile sınırlı ve etiketlidir. Bunlar hedef site veya otomatik eğitim kabulü değildir. Tarihli kod/test özetinin kanonik kanıtı [STATUS](docs/STATUS.md) içindedir; [UI sınırı](docs/UI_RUNTIME.md).

**Salt okunur W2/W3 incelemesi:** tek audited sentetik sayfa doğrulamasını seçen CLI ve exact skill/plan hash'iyle yapısal varyasyon önizleme CLI'si eklendi. Mevcut W4 çift-rol aday-gap CLI'si ayrıca kaynak DB/symlink güvenlik testleriyle denetlendi. Bunlar gerçek site kanıtı, skill başarısı veya eğitim yetkisi üretmez. [W2](docs/SITE_PAGE_EVIDENCE.md), [W3](docs/SITE_SKILL_VALIDATION.md), [W4](docs/LEARNING_CANDIDATE.md).

**Onay beklemeden deneme:** desteklenen görevlerde **Approve all — this task only** arayüzde varsayılan seçilidir; manuel onay için kapatabilirsiniz. Her Start yalnız o sabit göreve yeni izin verir; policy ve bağımsız sonuç doğrulaması korunur. Resume manuel onaylıdır, API'de açık `approve_all` olmadan manuel onay geçerlidir. [Kapsam ve gerçek hız ölçümü](docs/TASK_AUTO_APPROVAL.md).

**Laya System-1 adayı:** ayrı ve devre dışı deneysel kimlikle yerel Decider karşılaştırması vardır; aktif model ve güven eşiği değişmez. [Yöntem ve sınırlar](docs/LAYA_COMPARISON.md).

**İlk System-1 beklemesi:** yeni managed başlangıçta süreli CPU hazırlığı yapılır; hazırken Hello ölçümü 4,38 s, soğuk job 10,52 s. Hazırlık ayrıca yaklaşık 5–6 s almıştır ve yok edilmez; bu ilk açılış veya kesintisiz throughput garantisi değildir. GPU forward, tam yeniden pin kontrolü ve job sonu GPU kapatma korunur. [CPU prewarm](docs/DECIDER_PREWARM.md).

**Web arayüz kontrolü:** [Playwright MCP](docs/PLAYWRIGHT_MCP.md) yerel, ayrı profilli geliştirici incelemesini tamamlar; mevcut Python Playwright testleri korunur. Yeni Codex oturumunda proje MCP yapılandırması yüklenir; AOS model veya görev yetkisine dönüşmez.

**Arayüz dili ve ilerleme:** İngilizce varsayılandır; English/Türkçe düğmeleri seçimi hatırlar. Görev evresi, toplam geçen süre ve tamamlanmış model çağrıları kayıtlı veriden gösterilir. Registry etkinliği ile gerçek pilot çalışma durumu ayrılır. [Görev ilerlemesi ve CPU hazırlığı](docs/TASK_PROGRESS.md).

**Karar gecikmesi:** yeni gerçek managed oturumda Decider aynı görev içinde yeniden kullanılır; her adımda yeni inference ve ayrı onay korunur. Formun ikinci model kararı ölçümde 8,27 saniyeden 0,068 saniyeye indi. Görsel SAVE için CPU-only hazırlık Bonsai gözlemiyle örtüşür; ayrı A/B smoke'ta ilk onaya hazırlık 30,58 → 21,51 saniyedir. GPU çıkarımı hâlâ sıralıdır; genel benchmark sonucu değildir. [Kapsam ve ölçüm](docs/DECIDER_REUSE.md).

**Görünür görevler:** yeni managed pilotta yerel form ve görsel SAVE aynı Docker/noVNC masaüstünde açılır; Görevler'de gerçek pencereyi, ayrı eylem onaylarını ve doğrulanmış sonucu izleyebilirsiniz. [Görünür form](docs/VISIBLE_BROWSER.md), [görünür SAVE sözleşmesi ve kabul sınırları](docs/VISIBLE_VISION.md); güncel kanıtlar STATUS'tadır.

**Kullanıcı denemesi — v0.1 yerel pilot:** Bu bilgisayardaki hazır kurulumu `./scripts/aos-v1 start` ile başlatın; `./scripts/aos-v1 token` giriş anahtarını, `./scripts/aos-v1 open` tarayıcıyı açar. Düz yerel oturumu güvenle yeniden başlatmak için `./scripts/aos-v1 restart`, tam kapatma için `./scripts/aos-v1 stop` kullanın. [İlk deneme ve sorun giderme rehberi](docs/FIRST_RELEASE.md), [salt okunur doctor](docs/LOCAL_PREFLIGHT.md). Bu dört sabit görevlik yerel pilot; genel ajan, eğitim platformu veya başka makine installer'ı değildir.

**İkinci paralel dilim — 6x/6y/7k/8a:** [Bileşik hedeften açık sıra başlatma](docs/COMPOUND_EXECUTION.md), [yeni panellerin native kabulü](docs/NATIVE_ACCEPTANCE.md), [dataset bağlı gerçek forward/loss](docs/DATASET_BOUND_LOSS.md) ve [çalıştırılabilir fixture benchmark](docs/BENCHMARK_RUNTIME.md) eklendi. Genel görev yürütme, tam Wayland/installer, gerçek-model karşılaştırmalı benchmark ve eğitim ayrı açık kapsamlardır; güncel kanıtlar STATUS'tadır.

**Paralel geliştirme — 6u/6v/6w/7j:** [UI session/runtime bağı](docs/RECOVERY_SESSION.md), [bileşik Türkçe katalog planı](docs/GOAL_PLAN.md), [private native kabuk paketi](docs/NATIVE_PACKAGE.md) ve [dataset bağlı S1 tokenizer](docs/DATASET_PREFLIGHT.md) eklendi. Bunlar genel görev yürütme, crash continuation, bağımsız kurulabilir release veya eğitim değildir; gerçek test ve açık kapılar STATUS'ta ayrılır.

**Kalıcı Docker doğum kaydı (6t):** container oluşturulmadan önce private fsync journal; host boot/PID/start-time/namespace bağı ve create yanıtı kaybının salt okunur incelemesi. Journal SQL/model deployment veya cleanup yetkisi değildir. [Kapsam ve komutlar](docs/LIFECYCLE.md).

**Docker runtime incelemesi (6s):** yeni plain CLI hello kayıtlarında deployment/image/container/workspace bağıyla salt okunur orphan-adayı sınıflandırması. Process ölümü kanıtlanmaz; container sahiplenilmez/silinmez, eylem tekrarlanmaz. [Kapsam ve komutlar](docs/RECOVERY_RUNTIME.md).

**Kalıcı doğrulama finalizasyonu (6r):** yarım kalan 6q verification/state/run kayıtlarını salt okunur sınıflandırma; yalnız tam bağımsız kanıtı taze exact-request onayıyla atomik finalleştirme. Yeni model/gateway eylemi veya lease yetkisi yoktur. [Kapsam ve komutlar](docs/RECOVERY_FINALIZE.md).

**Uzlaştırılmış yazmanın son doğrulaması (6q):** aynı hello run'ında yeni lease/model kararı ve iki ayrı onaylı okuma; eski write tekrarlanmaz. Bağımsız doğrulamadan sonra başarı kaydedilir. [Kapsam ve komutlar](docs/RECOVERY_VERIFY.md).

**Açık yazma uzlaştırması (6p):** matching kalıcı receipt ve taze exact-request terminal onayıyla eski hello action sonucu atomik audit ile kaydedilir. Run paused kalır; yeni yürütme veya resume yetkisi verilmez. [Kapsam ve komutlar](docs/RECOVERY_RECONCILE.md).

**Kalıcı yazma kanıtı (6o):** standart descriptor hello yazmasında exclusive-create dosya descriptor'ına bağlı kalıcı receipt ve salt okunur etki incelemesi. Eş içerikli farklı dosya/sürüm reddedilir; belirsiz action çözülmez veya tekrarlanmaz. [Kapsam ve komutlar](docs/RECOVERY_EFFECT.md).

**Sıfır eylemli hello devamı (6n):** yalnız descriptor-workspace plain hello, aynı run/yeni lease/taze model kararı ve exact-action terminal onayı. Herhangi bir eski action, belirsizlik veya desktop job varsa reddedilir. [Kapsam ve komutlar](docs/RECOVERY_RESUME.md).

**Hello checkpoint incelemesi (6m):** yeni descriptor-workspace run'larında dizin/state/deployment-snapshot bağı ve mevcut dosya etkisi salt okunur denetlenir. Gerçek process crash testleri vardır; belirsiz action çözülmez veya tekrarlanmaz, continuation yetkisi verilmez. [Kapsam ve komutlar](docs/RECOVERY_CHECKPOINT.md).

**Sıralı sabit görevler (6l):** iki/üç farklı görevi mevcut onay sınırlarıyla sırala; önceki bağımsız doğrulanmadan sonraki başlamaz. UI/API, kalıcı event izi ve kesinti testleri vardır. Genel scheduler veya crash continuation değildir. [Kapsam ve komutlar](docs/TASK_SEQUENCE.md).

**Sabit görev planı (6i):** üç görev için typed adımlar, kapsam ve bağımsız başarı ölçütü; CLI/API ve explicit Plan paneli. Katalog önizlemesi yürütme veya onay değildir. [Kullanım ve sınırlar](docs/TASK_PLAN.md).

**Kurtarma (6f–6h):** salt okunur CLI/API/Kurtarma paneli ve private DB backup/verify/yeni dosyaya restore. Eski lease/onay canlandırılmaz, belirsiz eylemler tekrarlanmaz. [Kapsam ve kullanım](docs/RECOVERY.md).

**Türkçe görev önizlemesi (6d–6e):** üç sabit görev için CLI, authenticated salt okunur API ve Görevler formu; hedefi gösterir, işlem başlatmaz veya onay sınırını değiştirmez. [Katalog ve sınırlar](docs/TASK_INTENT.md).

**Offline veri hazırlama (7a):** `python -m aos.dataset --include-synthetic --output datasets/fixture-v001` yalnız hash-review bağlı sentetik örnekleri işler; model/eğitim çalıştırmaz ve `training_ready=false` üretir. Kurulum, bütünlük kontrolü ve converter sınırları [DATASET_FIXTURE_RUNTIME](docs/DATASET_FIXTURE_RUNTIME.md) içindedir.

**Salt okunur envanter (7b):** `python -m aos.dataset_audit --database data/aos.sqlite` tutarlı SQLite snapshot'ından flag/review/verification özetini ve engelleri çıkarır; kaynak kayıtları değiştirmez veya eğitim izni vermez. [Kapsam ve kullanım](docs/DATASET_AUDIT.md).

**İnceleme kayıtları (7c):** kaynak/candidate hash'ine ve explicit host yetkisine bağlı accept/revoke kaydı; append-only migration 0008. Gerçek run'lara otomatik izin verilmez. [Sözleşme ve sınırlar](docs/DATASET_REVIEWS.md).

**Hash önizlemesi (7d):** `python -m aos.dataset_preview --database data/aos.sqlite --run-id RUN_ID` mevcut label üzerinden salt okunur S1 türetme/candidate/receipt kontrolü yapar. Ham içerik, yeni label veya eğitim izni üretmez. [Kullanım ve sınırlar](docs/DATASET_PREVIEW.md).

**Yerel reviewer (7e–7f):** Linux socket peer UID + host allowlist + süreli/tek kullanımlık exact-event onayıyla sentetik S1 accept/revoke işler. Explicit private servis, terminal inceleme istemcisi, fsync journal ve crash reconciliation vardır; otomatik eğitim/onay yoktur. [Yetki sınırları](docs/DATASET_REVIEWER.md), [servis kullanımı](docs/DATASET_REVIEW_SERVICE.md).

**Gerçek tokenizer preflight (7g):** pinned yerel tokenizer/prompt/collate ile sentetik S1 token bütçesi, gold/slot ve padding denetimi. Model ağırlığı veya eğitim çalıştırmaz. [Kapsam ve komutlar](docs/DATASET_TOKENIZER.md).

**Forward/loss preflight (7h):** explicit sentetik opt-in ile gerçek Decider forward, sonlu NLL ve VRAM ölçümü; parametre değiştirme, gradient/optimizer veya eğitim yoktur. [Kabul sınırları](docs/DATASET_LOSS.md).

**Hazırlık kapıları (7i):** dataset bütünlüğü/split/converter ve preflight rapor-pinning envanteri; eksik veri/replay/izinleri görünür tutar, eğitim açmaz. [Readiness kullanımı](docs/DATASET_READINESS.md).

1. ZIP'i aç; `aos-aserdargun-com/` klasörünü hedef **CachyOS** makinede Codex projesi olarak aç.
2. Model seçicisinden istersen konuşmada hedeflenen **Astra 6 / High** ayarını seç. Dosya bunu otomatik değiştirmez.
3. [CODEX_KICKOFF.md](CODEX_KICKOFF.md) içeriğini ilk mesaj olarak ver.
4. Codex önce mevcut ortamı inceleyip [STATUS](docs/STATUS.md) dosyasına gerçek bulguları yazmalı. Çalışan kurulumları yeniden kurmamalı.
5. İlk dikey dilim: izole `/workspace/hello.txt` dosyasına `Hello from the local agent.\n` yaz (sonda satır sonu), geri oku, eşitliği doğrula ve trajectory kaydet. System-2 çağrısı gerekmemeli.

Paket sözleşmelerini şimdi kontrol etmek için (Python 3.11+):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python scripts/validate_package.py
```

Bu komut GPU/model indirmez, eğitim veya deployment başlatmaz; geçici SQLite veritabanlarında sözleşmeleri sınar. Sonuç [PACKAGE_VALIDATION](docs/PACKAGE_VALIDATION.md) dosyasında özetlenir. `uv.lock` uygulama bağımlılıklarını sabitler; masaüstü açık hazırlık ve owned Docker lifecycle ile başlatılır, `docker compose up` tanımı sunulmaz.

Seçilebilir, ayrı süreçli test kanıtı profilleri ve sentetik/gerçek-model sınırları için [Yetenek kanıtı test havuzu](docs/CAPABILITY_TEST_POOL.md) kılavuzuna bakın. Development → **Historical capability test pool**, sekiz vakanın gerçek yerel raporlarından tarih/süre ve geçen/atlanan/hatalı sayılarını salt okunur gösterir; yenileme test veya model çalıştırmaz, ürün hazır olma yüzdesi değildir.

İlk dilimi çalıştırmak için:

```bash
.venv/bin/python -m pip install -e '.[dataset,browser,desktop]'
.venv/bin/agentctl hello --engine fixture
.venv/bin/python scripts/check_capabilities.py --profile core
```

`fixture` açıkça test motorudur. Core profili canlı portlara dokunabilen tam local-app lifecycle testlerini dışarıda tutar; opt-in atlamaları gerçek-model başarısı değildir. Gerçek Decider kurulumu ve GPU komutu [DEVELOPMENT](docs/DEVELOPMENT.md) içindedir. `/workspace` bu dilimde açıkça seçilen yerel klasörün gateway adıdır; varsayılanı `data/workspace/` olur. Mevcut farklı içerik üzerine yazılmaz.

Gerçek Bonsai + Decider kurtarma komutu ve kapsamı [BONSAI_RUNTIME](docs/BONSAI_RUNTIME.md) içindedir. Normal `hello` System-2 çağırmaz; `recover-hello` açıkça kontrollü hata senaryosudur.

`browser-form` ağsız Bubblewrap içinde gerçek Chromium/DOM ile sentetik yerel form görevini uygular. Gerçek Decider fill/submit kararları ve ayrı outcome readback kabulü geçti; vision/S2 kullanılmaz. Hazırlık, kapsam ve testler [BROWSER_RUNTIME](docs/BROWSER_RUNTIME.md) içindedir. Genel web gezintisi veya tam masaüstü değildir.

`vision-canvas` semantik DOM hedefi olmayan sentetik canvas'ta gerçek Bonsai screenshot → typed scene → Decider symbolic seçim → capture-bound click/bağımsız outcome doğrulamasını uygular. İlk gerçek kabul geçti; fiziksel masaüstü mouse sürücüsü veya genel vision benchmark değildir. Ayrıntılar [VISION_RUNTIME](docs/VISION_RUNTIME.md) içindedir.

## Kilitlenen mimari

Üç sabit UI görevinde Duraklat aynı run'ı korur; Devam et yeni gözlem, karar ve onay ister. Bu yalnız aynı canlı backend oturumu içindir; kontrol devri, çıkış ve yeniden başlatma eski işi iptal eder.

Native Tauri/WebKitGTK için özel X11 ekranında giriş, canlı noVNC/klavye, onay/ret, kontrol devri, Pause/Resume, Türkçe önizleme/Plan/Kurtarma ve üç sabit görevin kabulü geçti. Fixture regresyonundan ayrı explicit native gerçek Decider/Bonsai kabulü de geçti; genel masaüstü kalitesi veya dağıtım kabulü değildir. Hazırlık ve sınırlar [UI_RUNTIME](docs/UI_RUNTIME.md) içindedir.

React kontrol merkezi `/ui/` altında, Tauri kabuğu `ui/` içindedir. `scripts/serve_desktop.py --engine decider` ile onaylı hello görevi; `--browser-tasks --vision-engine bonsai` eklenince sabit browser formu ve vision canvas görevi de başlatılır. Aynı anda yalnız bir görev çalışır; her eylem ayrı süreli/tek-kullanımlık onay ister. Browser/vision ayrı ağsız headless runtime'dadır, noVNC masaüstüsü değildir. Model çağrısı sırasında kontrol devri görevi iptal eder. Varsayılan motor kapalıdır; fixture açıkça seçilir. Genel görev scheduler'ı ve native release dağıtımı henüz yoktur. Kurulum ve sınırlar [UI_RUNTIME](docs/UI_RUNTIME.md) içindedir.

`hello --runtime desktop` aynı gerçek Decider/gateway döngüsünü ağsız Docker workspace'inde çalıştırır. XFCE/noVNC, office/developer uygulamaları ve Pause/Stop/Take Control/Return Control/Resume/Restart için dar yerel konsol [DESKTOP_RUNTIME](docs/DESKTOP_RUNTIME.md) içinde anlatılır. Bu konsol tamamlanmış Tauri UI veya genel masaüstü ajanı değildir.

| Katman | Karar |
|---|---|
| Hedef donanım | CachyOS, RTX 4070 Ti SUPER 16 GB VRAM, 32 GB RAM |
| System-1 / Operator | Mapika/decider-2b + durum makinesi + deterministik araçlar |
| System-2 / Supervisor | Ternary Bonsai-2 27B + Vision; ilk aday PQ2_0 + eşleşen mmproj |
| Model servisleri | Host-native; Bonsai için uyumlu PrismML llama.cpp CUDA; Decider için PyTorch/CUDA |
| Backend | Host-native Python, uv, FastAPI, Pydantic, asyncio; başlangıç SQLite |
| Agent Computer | Ubuntu + XFCE + X11 + noVNC; ilk backend Docker/Compose |
| Uygulamalar | Chromium, Terminal, VSCodium, Thunar, LibreOffice, PDF viewer, Git, Python, Node, pnpm |
| UI | Tauri + React + TypeScript; pnpm, Rust/cargo |
| Workspace | Açıkça izin verilmiş mount'lar; gerçek host masaüstüne sınırsız erişim yok |
| Dil | Kullanıcı Türkçe; normalize edilmiş karar durumu İngilizce; sonuç Türkçe |
| Eğitim | Offline; Decider için delta/continued training, Bonsai için uyumluluğu kanıtlanmış LoRA |

```mermaid
flowchart TD
  U[Kullanıcı / yerel UI] --> N[Task Normalizer]
  N --> O[Operator: Decider + state machine]
  O --> P[Deterministik policy ve yetki kontrolü]
  P --> E[Executor / Computer Gateway]
  E --> C[İzole Agent Computer]
  C --> V[Observation + outcome verification]
  V --> O
  O -->|belirsizlik, vision, stuck| S[Bonsai Supervisor]
  S -->|yapılandırılmış plan| O
  O --> T[Trajectory Store / SQLite]
  V --> T
  T --> D[Redaction + dataset build]
  D --> F[Offline training + evaluation]
  F --> R[Candidate + açık promotion + rollback]
```

## Değişmez ilkeler

- İşlem önceliği: internal API → filesystem/process/shell → browser DOM/CDP → accessibility → vision ve mouse/keyboard.
- System-1 serbest metin sohbet modeli değildir; seçenekleri izinli ToolRegistry üretir. Model güvenliği belirlemez.
- Modelden gelen `goal_reached` veya tool'un `OK` yanıtı tek başına başarı değildir. Başarı ölçütü bağımsız verification ile kanıtlanır.
- State ve hafıza model bağlamından ayrı tutulur. Model çağrılarına yalnız gerekli, bütçeli bağlam gider.
- ModelRegistry, AdapterRegistry, DeploymentRegistry ve BenchmarkRegistry ilk günden sınırları belli bileşenlerdir.
- V0.1 base modellerle başlar. System-2'de aynı anda en fazla bir uzmanlık LoRA'sı; stacking daha sonra deneyle değerlendirilir.
- Host GPU belleği ve 16K Bonsai context başlangıcı ölçüm gerektirir; birlikte resident çalışma garanti değildir.
- Ham trajectory eğitim datası değildir. Sessiz yeniden eğitim, otomatik production promotion ve zorunlu bulut inference yoktur.
- Pause / Stop / Take Control / Return Control / Approve / Reject / Resume ve AGENT/HUMAN/PAUSED sahipliği tasarımın parçasıdır.

## Paket haritası

| Konum | İçerik |
|---|---|
| [docs/README.md](docs/README.md) | Tüm teknik ve eğitim belgelerinin dizini |
| `database/migrations/` | Trajectory ve registry SQLite migration'ları |
| `schemas/` | System-1, System-2, replay export ve registry JSON Schema |
| `training/recipes/` | Dar kapsamlı ilk iki eğitim deneyi; çalıştırıcı kod değildir |
| `examples/` | Şemaya uygun sentetik JSON/JSONL; eğitim verisi veya benchmark başarısı değildir |
| `config/` | Başlangıç policy ve topoloji önerileri |
| `benchmarks/` | İlk görev kataloğu ve promotion kriterleri |
| `data/`, `datasets/`, `models/`, `adapters/`, `runs/` | Yerel çalışma alanları, gerçek içerikler Git dışında |
| `scripts/validate_package.py` | Gerçek paket/sözleşme doğrulayıcısı |
| `MANIFEST.sha256` | Paket dosyalarının bütünlük listesi |

İlk uygulama planı [ROADMAP](docs/ROADMAP.md), tasarım kararları [DECISIONS](docs/DECISIONS.md), model kaynakları ve doğrulama sınırları [MODELS](docs/MODELS.md) içindedir. `aos.aserdargun.com` gelecekte olası tanıtım adresidir; bu paket alan adı ayırmaz veya site yayınlamaz.
