# Yerel kontrol merkezi — Aşama 6a / 6b Görev Entegrasyonu

**İlk kullanıcı denemesi:** [v0.1 pilot rehberi](FIRST_RELEASE.md) doctor, managed start/status/token/open/stop ve sabit görevlerin test sırasını verir. Doğrudan backend komutları aşağıda geliştirme içindir. Managed launcher her yeni start'ta ayrı private session/workspace/DB açar; eski session otomatik sürdürülmez.

## Uygulanan ve uygulanmayan

Knowledge bölüm 4 ayrı exact yerel inference/storage izniyle pinli Bonsai'ye sorar; immutable bundle ve atıf alt dizgeleri ayrıca doğrulanır. EN/TR paneli fixture→real display-flag, response ve bundle bozulmalarını reddeder. Model/task lane reservation ve takeover/cancel korunur; yanıt araç yetkisi veya eğitim gold'u değildir. [Model akışı](DOCUMENT_KNOWLEDGE_ANSWERS.md), [gerçek ve fixture kanıtlarını ayrı izleyin](STATUS.md).

Knowledge / Bilgi paneli ayrı kapsamlı belge yayınlama, accept/reject/revoke, katalog ve kaynak atıflı lexical arama sunar. EN/TR ve mobil testleri ayrı fixture kabulüdür; gerçek model/RAG üretimi değildir. Yeni backend capability'si yoksa istek gönderilmez. Varsayılan açılış Development kalır. [Sözleşme](DOCUMENT_KNOWLEDGE.md).

Development açılışında W1–W6 gerçek ürün kabulü ile ayrı tarihli dar-dilim ve açık-iş yüzdeleri uzun kart listesinden önce görünür. Yüzdeler canlı görev ilerlemesi veya ürün hazır olma tahmini değildir; 1280×900 owned Chromium giriş testi üç özeti kaydırma gerektirmeden denetler. Son görev varsa sunucu kaydındaki durum/kalıcı evre/süre ayrı canlı kartta görünür; onay bekleyen iş Tasks'a yönlendirilir, kendiliğinden onaylanmaz. Form planı pinli değilse Development tekrar ölçümü API'sine boşuna istek göndermez; yok durumunu gösterir. Pinli planda eski/eksik 1.1 yanıtı yine başarısız ölçüm olarak kalır.

Development'ın ayrı canlı System-1 hazırlık kartı, authenticated `/api/tasks.decider_preparation` alanından `preparing`, CPU `ready`, süreli GPU `ready_gpu`, `inactive` ve alanı olmayan/kapalı backend durumlarını EN/TR gösterir. Bu yalnız worker'ın boşta hazırlık ACK'idir; VRAM miktarı, görev sırasındaki CUDA kullanımı, sonraki inference veya doğrulanmış görev sonucu olarak yorumlanmaz. Dil değiştirmek ya da Refresh görev/onay başlatmaz. Varsayılan Development ekranı bu sınırlı sinyali Görevler'e geçmeden gösterir; [gerçek CUDA ve ayrı UI kanıtı](STATUS.md) karıştırılmaz.

Tarihli dar listede W1 HTTPS form onayının monotonic son kullanma sınırı, gerekli e-posta alanı ve ayrı sentetik W5 model-gradient smoke ile adapter adayı EN/TR kartlarıdır. Güncel dar liste **42/51 (%82,4)** tamamlanan ve **9/51 (%17,6)** açık madde gösterir; gerçek W1–W6 kabulü **0/6** kalır. Statik varlık kartlarının EN/TR kapsamı planlı JS/CSS/görsel GET'lerini de açıkça anlatır. Bu kartlar yalnız kaynak/test kaydını gösterir; adapter dosyası deployable checkpoint, gerçek eğitim, held-out başarı veya gerçek hedef-site kanıtı değildir.

**HTTPS form kurulum ekranı:** Web applications paneli tek veya 2–8 sıralı alanlı stateless form görevini ve planını iki ayrı ağsız hash onayıyla özel dosyalara kaydeder. Görevden önce açık seçilen altı-onaylı mod, ayrı önce/sonra durum planını ve üçüncü exact hash kaydını zorunlu kılar; public manager komutu state planı kaydedilmeden görünmez. Komutlar yalnız gösterilir. Form değerleri yalnız gizli olmayan test verisi olmalıdır. Oturum/CSRF, bağımsız uygulama sonucu ve gerçek-site kabulü bu ekranda yoktur. [Kapsam](WEB_FORM_ONBOARDING.md).

**Statik plan backend yeteneği:** authenticated Web applications ekranı `GET /api/web-applications/capabilities` yanıtında `1.0` veya `1.1` sürümüyle dört görsel MIME'ını doğrulamadan `image/*` seçeneklerini göstermez. Yeni `1.1` sözleşmesindeki exact `static_asset_canonical_query: true` ayrıca doğrulanmadıkça v1 statik ve v2 JSON paketindeki sorgulu **statik varlık** URL'lerinin önizleme/kayıt düğmeleri kapalıdır; sorgusuz JS/CSS akışı kullanılabilir ve v2 JSON URL sorgusu ayrı kalır. 404/bozuk yanıt kapalı sayılır. Development canlı kartı bu sorgu desteğini W1–W6 kabulünden ayrı gösterir. Bu yalnız taslak arayüz kapısıdır; planın yetkisi ve host/worker sınırları değişmez. Eski canlı Python process'i statik UI build ile kendiliğinden yükselmez.

Varsayılan Development ekranı aynı authenticated yetenek sorgusunu ayrı **canlı backend** kartında gösterir. `checking`, exact kapsam doğrulandı ve `unconfirmed` durumları tarihli tamamlanan/yapılacak yüzdelerine veya W1–W6 kabulüne eklenmez. Başarılı yanıt yalnız taslak MIME desteğidir; bu oturumda görev pini, gerçek site izni veya worker sağlığı değildir. Eski backend 404'ü yeni UI kodunu Python process'ine sıcak uygulamaz; güvenli yeni oturum gerekir. Sayfadaki Refresh düğmesi yeteneği yeniden yoklar.

Development ile Web applications arasında gezinirken profil envanteri yeniden okunur, ancak mevcut liste istek başında `null` yapılıp form bileşeni gereksiz yere sökülmez. Böylece oturum açılışındaki eşzamanlı envanter sorgusu, kullanıcı profil/görev alanı doldururken formu sıfırlamaz. Yeni yanıt gelince liste yine sunucuya göre güncellenir; taslak preview/register sunucu tarafından yeniden doğrulanır.

**Tarihsel rota grafiği görünümü:** pinli sıralı HTTPS rota planı olan yeni backend'de Tasks paneli aynı oturumun iki başarılı rota run'ını açık seçip authenticated, salt okunur karşılaştırır. UI yalnız değişmeyen rota sayısı/indeksleri, gözlenen planlı kenarlar ve eksik/değişen indeksleri EN/TR gösterir; URL, içerik veya hash görüntülemez. API profil/plan ve kaynak denetimini tekrar yapar. Bu canlı site haritası, semantik bilgi incelemesi, yeni GET veya W2 gerçek-site kabulü değildir. [Sınır](REMOTE_NAVIGATION_GRAPH.md).

**Opsiyonel form durum onayı:** backend doğrudan `remote_form_state_plan` veya güvenli yeni managed `aos-v1 start` ile yapılandırılmışsa Görevler paneli altı aşamayı, exact state URL/plan hash'ini ve host HTTPS GET onayının tarayıcıda yeni sayfa açmadığını EN/TR gösterir. Eski dört-aşama modu aynı kalır. HTML yanıt değişimi uygulama sonucu sayılmaz. [Sınır](WEB_HTTPS_FORM_TRANSPORT.md).

**Opt-in statik JS/CSS paketi:** yeni managed oturum exact owner-only planla başlatılmışsa `browser_remote_static_assets` Görevler panelinde sunulur. EN/TR panel giriş URL'sini, plan hash'ini ve her exact JS/CSS URL'sini tek manuel onaydan önce gösterir; `Approve all` kapalıdır. Onaydan önce host GET yoktur; ret/iptal otomatik tekrar yapmaz. Owned sentetik TLS/MCP scheduler ve UI testleri geçmiştir, gerçek site/hesap veya uygulama sonucu doğrulanmamıştır. Eski çalışan oturum yeni backend/task pinlerini sıcak yüklemez. [Sınır](WEB_STATIC_ASSETS.md).

**Geliştirme paneli:** konsol her açılışta ve sayfa yenilendiğinde, oturum açık olsa bile, varsayılan Development / Geliştirme sekmesini gösterir; diğer sekmeye gezinme yalnız o sayfa yüklemesi boyunca geçerlidir. Authenticated panel `ui/src/Development.tsx` içindeki tarihli, iki dilli kod/test özetini gösterir. Uygulanmış dar dilimler ve açık gerçek-web kapıları eşit ağırlıklı tarihli kontrol listesi olarak gösterilir; güncel sayılar arayüzün kendisindedir. W4 kartı exact yerel izinli artımlı metadata CLI ve görev-başı scheduler hook'unu harici hak doğrulaması/eğitimden ayırır; managed HTTPS form ve sıralı rota kartları yerel fixture sınırını korur. Bunlar gerçek hedef-site görevi, incelenmiş bilgi veya retrieval değildir. Bu yüzdeler süre, zorluk veya ürün hazır olma tahmini değildir; W1–W6 gerçek-site milestone kabulü ayrıca **0/6 (%0)** gösterilir. Gerçek hedef URL/hesap/veri hakları olmadan bu altı milestone kabul edilmez. Bunun üstünde ayrı canlı bölüm, mevcut `/api/tasks` yanıtındaki sunulan sabit görev türlerini ve bu oturumun son 20 job'undaki başarı sayısını, `/api/overview` içindeki trajectory DB'nin son 30 run'ında `succeeded`, en az bir passed ve sıfır failed verification koşulunu karşılayan kayıt sayısını gösterir. Bunlar yalnız sınırlı sunucu kayıtlarıdır; hedef-site kabul yüzdesi, yeni test veya W1–W6 başarısı değildir. Görev/Çalışmalar düğmeleri ilgili konsol paneline götürür; Development kendi başına görev başlatmaz ve yenileme test çalıştırmaz. Gerçek kanıtların kanonik kaydı [STATUS](STATUS.md) içindedir. Yeni rota veya statik-plan backend oturumunda Tasks paneli yalnız o aktif run için iki aşamalı yerel metadata izin kaydı, ayrı exact hash bağlama ve S1/S2 durum görünümü sunar; izin beyanı tek başına harici hak veya eğitim yetkisi değildir.

Yeni rota-plan oturumunun Tasks paneli, sunucu-pinli profile ait özel S1 ve S2 skill taslaklarını ayrı açık isteklerle yalnız metadata olarak listeler. S2 taslağı S1 kaynak incelemesinde seçilemez; rota görevi Bonsai çağırmadığından S2 sonucu veya skill doğrulaması gösterilmez. Eski canlı backend yeni query sözleşmesini sıcak yüklemez; yeni backend oturumu gerekir.

Exact JSON GET managed opt-in dilimi de tarihli tamamlanan dar dilimler arasında ayrı görünür. Web applications paneli kayıtlı tek sayfa görevi için 1–8 JS/CSS ve 1–4 exact JSON GET URL'sini ağsız önizler; exact hash kaydıyla `0600` plan ve yeni oturum komutları üretir, komutları çalıştırmaz. Yalnız güvenli yeni oturumda private v2 plan pinlenirse Tasks paneli mevcut internal statik görev türünü JSON bundle olarak etiketler, exact URL'leri gösterir ve tek manuel onay ister. Mevcut çalışan oturum sıcak yükseltilmez; kartın sayılması W1 gerçek hedef uygulama kabulünü ilerletmez. [Sınır](WEB_READONLY_DATA_BUNDLE.md).

JSON-bundle run'ının ayrı salt okunur W4 kaynak denetimi ve açık izinli metadata akışı Development'ta iki tarihli tamamlanan dar dilim olarak görünür. Kaynak CLI'si kendi başına izin vermez; yeni v2 metadata kaydı Tasks'ta iki aşamalı exact izin ve ayrı attach olmadan başlamaz. Akış yalnız içeriksiz gerçekten pinli S1/S2 olaylarını özel outbox'a alır; gerçek site hakkı veya eğitim kapısı açmaz. O dilimde eşit ağırlıklı liste 19/28 tamamlanan ve 9/28 açık gösteriyordu; W1–W6 gerçek ürün kabulü 0/6 kaldı. [Kaynak](REMOTE_READONLY_DATA_SOURCE.md), [akış](REMOTE_READONLY_DATA_STREAM.md).

Güncel Development listesi, W2/W3 JSON ve rota dilimlerinin yanı sıra temel ve altı-onaylı durum planlı HTTPS form kaynak denetimi, ayrı izinli metadata akışları, sentetik vaka→denetlenmiş form taşıma/durum readback bağları, ayrı konsol durum planı ve son görevin S1/S2 model gecikmesi, temel/durum form tekrar ölçümü ve managed restart görev kabul kilidi kartlarını da dar kartlar olarak gösterir: **38/47 tamamlanan, 9/47 açık**. Bu hesap gerçek-site ürün kabul yüzdesi değildir; W1–W6 hâlâ **0/6**. İlk vaka bağı özel sentetik değerlerin ilan edilen hash'lerini ve seçili S1 vakasında mevcut HTTPS form planının exact gövdesini karşılar; ayrı yürütme denetimi aynı planın onaylı form run'ını ve gerçek S1 submit olayını bağlar. Opsiyonel durum denetimi ayrıca gönderilen değer–marker eşitliğini ve iki readback doğrulamasını ister. Bunlar skill executor, kaynak parametresi, kalıcı site sonucu veya aktivasyon kanıtı değildir. Yeni v2-plan backend'inde authenticated Tasks paneli iki sabit koşudan özel taslak seed/kayıt ve ayrı review sunar; Development düğmesi bunları kaydetmez veya görev başlatmaz. Tasks kaynak yollarını kullanıcıdan almaz, manuel sayfa anahtarını ve exact hash onaylarını ister; review'ü çalışan göreve otomatik bağlamaz. Açık manager opt-in ile yeni oturum pinlenirse Tasks yalnız son onaylı JSON-bundle readback'i için sembolik matched/stale durumunu gösterir; anahtar JSON görevinin Decider kararına verilmez. Rota planında ise açık metadata review yalnız kaynak ve bağlı hedef ayrı canlı readback ile eşleşirse sonraki karara geçer; hedef kaymasında sonraki karar ipucusuz kalır. [Taslak sınırı](REMOTE_READONLY_DATA_PAGE_DRAFT.md), [JSON review sınırı](REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md), [rota bağlamı](REMOTE_ROUTE_KNOWLEDGE_REVIEW.md), [vaka bağı](SITE_SKILL_CASE_BINDING.md), [form yürütme bağı](SITE_SKILL_FORM_EXECUTION.md).

Development'ın managed restart kartı, yeni backend'de exact `desktop-session-*` kimliğiyle alınan görev-kabul kilidini anlatır. `/api/tasks.restart_quiesced=true` ise panel açık kilidi ve host `release-restart` inceleme yolunu ayrıca gösterir; endpoint'i olmayan eski backend'e kilit varmış gibi davranmaz. Bu, harici process atomikliği veya gerçek hedef-site kabulü değildir.

Yeni temel veya durum-planlı HTTPS form backend oturumunda Tasks, yalnız ilk `browser.form.open` onayı beklerken iki aşamalı yerel metadata izni ve ayrı exact hash attach sunar. Scheduler onay öncesi/settle noktasında içeriksiz S1/S2 outbox'ını yoklar; dört veya altı form eyleminin ayrı manuel onayları değişmez. Sonraki form aşamasında attach reddedilir. Cookie bağlı form planlarında panel kapalıdır; çalışan eski backend hot-upgrade olmaz. Bu veri hakkı, site sonucu veya eğitim kabulü değildir. [Form metadata sınırı](REMOTE_FORM_LEARNING_STREAM.md).

Development / Geliştirme yan menüde ilk sıradadır. Her başarılı girişte varsayılan sekme yeniden seçilir; sayfa açılışı, yenileme ve çıkış-sonrası girişte ilerleme paneli görünür. Standart plain managed oturumda güvenilen loopback ortamı için token alanı yerine otomatik yerel cookie oturumu açılır. Sign out aynı sayfada otomatik tekrar giriş yapmaz; ayrı Open local session düğmesi gerekir. Yeni sayfa oturum açsa bile PAUSED kontrolü resume etmez. Raw/owned/synthetic/remote deneyler token politikasını korur; çok-kullanıcılı/public auth yerine geçmez. [Mac launcher ve sınırlar](PILOT_QUICKSTART.md). W4 kartı yeni managed backend için özel oturumlarda açılış/saatlik retention sweep'ini belirtir; bu gerçek hedef veri hakkı veya kapalı makinede süre garantisi değildir.

Varsayılan Development ekranının ayrı canlı S1/S2 bölümü yeni backend'deki **son görevin doğrulanmış run'ına bağlı tüm tamamlanmış model çağrılarını** role göre başarılı/toplam sayar. State/snapshot/step bağı yoksa sayı uydurulmaz; eski backend tam toplamı sunmuyorsa en yeni en çok 10 çağrılık örnek açıkça sınırlı olarak etiketlenir. Exact uzak metadata izni bağlıysa aynı son görevin özel outbox özetindeki `entries_by_role` S1/S2 adayları ayrı görünür; izin yoksa veya kaynak henüz raporlanmadıysa sayı uydurulmaz. Bu alanlar mevcut `/api/tasks` yanıtını kullanır, yeni model çağrısı veya veri toplama başlatmaz. Fixture çağrısı gerçek Decider/Bonsai sonucu, metadata adayı reviewed dataset/gold, son-10 örneği tüm run toplamı sayılmaz. EN/TR görünümünde kullanıcı bu farkı doğrudan görür; W1–W6 kabulü değişmez.

Aynı son görevin ayrı gecikme kartı yalnız doğrulanmış run'ın başarılı ve sonlu S1/S2 çağrılarından, en çok 256 örnekte en yakın-sıra p50/p95 ve `n` gösterir. Kaynak yok, sıfır çağrı veya sınır aşımı `—` verir; eski backend'de son-10 örneğinden yüzdelik uydurulmaz. Tek çağrının p95'i kararlı hız göstergesi, görev süresi veya W6 gerçek-site kabulü değildir. [Ölçüm kapsamı](TASK_PROGRESS.md).

Development varsayılan ekranda ayrıca authenticated profil envanterindeki **yalnız taslak sayısını** ve bu session'da uzak giriş, rota, form veya statik-varlık görev kapsamı pinlenip pinlenmediğini gösterir. Sıfır taslak veya sıfır pin bir sonraki operatör adımını görünür kılar; taslak sayısı hedef URL/hesap/veri hakkı, pin de gerçek-site yürütme/başarı kanıtı değildir. Web applications düğmesi yalnız ilgili panele gider, görev başlatmaz. Eski backend profil envanteri sunmazsa yokmuş gibi sıfır sayılmaz.

Aynı kart artık sıradaki **kurulum hazırlığını** canlı duruma göre EN/TR gösterir: envanter yükleniyorsa bekler, API yoksa güvenli yeni backend oturumunu ister, sıfır profilde yetkili URL/rol/görev/sonuç ölçütüyle taslağa yönlendirir, profil varken pin yoksa exact görev/plan ve ağsız manager önizlemesini hatırlatır, pin varsa Tasks'ta exact kapsam ve bağımsız sonucu denetlemeye yönlendirir. Pin bulunup envanter boşsa yeni görev önermek yerine oturum incelemesi ister. Tasks bağlantısı yalnız pin varsa görünür; bu kart hiçbir kayıt, görev, site isteği veya izin başlatmaz. Envanter ve pin farklı sunucu kaynaklarından geldiğinden bu yönlendirme yetki veya W1 kabul kanıtı değildir.

Web applications panelinde tek giriş görev dosyası, sıralı rota planı veya tek sayfalı göreve bağlı 1–8 exact JS/CSS planı exact hash onayıyla kaydedilince, arayüz ilgili `aos-v1 preview-*` ve yeni `start` komutlarını kayıt makbuzundaki profil/dosya pinlerinden salt okunur üretir. Rota/statik komutlar kendi plan kaydından önce görünmez; alan ya da profil değişimi ilgili eski makbuzu ve komutu kaldırır. Komutları arayüz çalıştırmaz, mevcut oturumu kapatmaz veya yeni site erişimi açmaz. Operatör önce ağsız preview yapıp mevcut oturumu güvenle durdurmalıdır; statik görev exact bundle için ayrıca tek manuel onay ister.

İki saat boyunca yeni managed tarama denemesi yoksa backend `stale` döndürür; Development eski `ok` veya `incomplete` sonucunu ve eski purge/session/engel sayılarını güncelmiş gibi göstermez. EN/TR kartı zamanlayıcının incelenmesi gerektiğini belirtir. Bu tazelik denetimi saatlik döngünün gerçek duvar saatinde doğrulandığı veya tüm kopyaların silindiği kanıtı değildir.

Yeni backend'de Development ayrıca authenticated `GET /api/retention` yanıtından son managed retention denemesinin durumunu, zamanını ve aggregate session/purge/engel sayılarını gösterir. Ham izin hash'i, özel yol veya outbox içeriği sunulmaz. Direct backend `disabled`, ilk tarama `pending`, eski endpoint 404 ise `unavailable` görünür; bunlar W1–W6 kabulü veya tüm depolama kopyalarının silindiği iddiası değildir. Açık Development sekmesi 30 saniyede bir yeniler; kullanıcı **Yenile** düğmesi de tekrar okur.

Bu varsayılan-sekme değişikliği yalnız yeniden derlenen statik UI varlıklarını etkiler. Canlı managed backend yeniden başlatılmadan yeni `/ui/` HTML/JS dosyalarını sunar; açık tarayıcı sekmesinde yeni davranış için sayfayı yenileyin.

**Opt-in staging görevi arayüzü:** `browser_staging_workflow` yalnız sunucu `/api/tasks.kinds` içinde sunduğunda İlk deneme/Görevler'de görünür; `staging_transport=playwright_mcp` ayrı etiketlenir. App→Draft→fill→submit için dört ayrı onay sırası gösterilir ve `approve_all` kapalı tutulur. UI'de görünmesi site yetkisi veya başarı kanıtı değildir. Mevcut managed dört-görev oturumu yeniden başlatılmadığından bu beşinci tür orada görünmez; ayrı opt-in backend kabulü [STATUS](STATUS.md) içindedir.

**Opt-in sıralı HTTPS okuma:** `browser_remote_routes` yalnız yeni oturumdaki kayıtlı profil/görev ve özel plan piniyle sunulur. Görevler paneli rota sayısı ve plan hash'ini, onay kartı exact URL ve rota indeksini gösterir; toplu onay ve sentetik öğrenme seçimi kapalıdır. Eski çalışan managed oturum bu yeni görevi sıcak yüklemez. Owned backend ile mocksuz UI'den başlatma, iki ayrı onay ve iki doğrulama hem fixture hem de ayrı explicit pinli gerçek Decider testiyle geçti [STATUS](STATUS.md). İkisi de sentetik TLS hedefidir, gerçek site kabulü değildir.

**Dil ve canlı ilerleme:** kontrol merkezi İngilizce açılır; girişte ve oturum başlığındaki English/Türkçe düğmeleri anında dil değiştirir. Yalnız dil tercihi `aos.ui.language` anahtarıyla tarayıcıda tutulur; token, görev veya onay localStorage'a yazılmaz. Dil değişimi görev başlatmaz/onaylamaz, mevcut onayı ve noVNC bağlantısını korur. Teknik kimlikler, kullanıcı girdileri ve action payload'ları çevrilmez; dar hedef önizlemesinin Türkçe katalog girdileri değişmemiştir. Eski Türkçe ekran adlarıyla yazılmış aşağıdaki rehber için Türkçe seçilebilir.

Görevler, [kayıtlı evreyi, örneklenmiş toplam süreyi ve tamamlanmış model çağrılarını](TASK_PROGRESS.md) gösterir; bu yüzde/ETA veya modelin iç düşüncesi değildir. Modeller panelindeki registry etkinliği servis readiness/GPU residency değildir; pilot açıkça pinned EXPERIMENTAL deployment kullanabilir. `enabled=0` kayıtları bu arayüz düzeltmesiyle etkinleştirilmez veya promote edilmez.

**Canlı imleç:** yeni backend, yalnız owned Ubuntu X11 masaüstünden authenticated ve salt okunur koordinat örneği sağlar. Bilgisayar/Görevler noVNC görüntüsü yanında sayı ve konum işaretçisi yaklaşık 500 ms aralıkla güncellenir; panel kapanınca polling durur. Eski çalışan backend destek bayrağını sunmadığı için yeni UI o oturumda “henüz desteklemiyor” gösterir; kaynak build'i mevcut process'e yeni endpoint yüklemez. İmleç göstergesi action, tıklama, başarı veya bağımsız verification kanıtı değildir. [API ve sahiplik sınırı](DESKTOP_POINTER.md).

6l: Görevler panelinde [Sıralı görev akışı](TASK_SEQUENCE.md) iki/üç farklı sabit görevi admission'a alır; her eylem yine ayrı onay ister. İlerleme ancak bağımsız verification sonrası olur. Ret/hata/takeover kalan sırayı durdurur. **Duraklat kalan sırayı iptal eder**, yalnız aktif single-job korunabilir. Genel scheduler veya backend çökmesinden devam değildir; Plan önizlemesi salt okunur, 6x ayrı bileşik başlatma düğmesi explicit admission'dır.

6i: [Plan paneli](TASK_PLAN.md) üç sabit görevin typed adım, kapsam, onay sınırı ve beklenen verification ölçütünü gösterir. Motor kapalıyken de katalog okunabilir; runtime kontrolü, model planı veya canlı ilerleme değildir. Input/panel/logout değişiminde eski istek iptal edilir, kalıcı kontroller kilitlenmez. Genel Plan/scheduler kapsamı hâlâ açık kalır.

6f–6g: [Kurtarma paneli](RECOVERY.md) server-configured control DB'den explicit salt okunur snapshot gösterir; raw içerik veya resume/replay yetkisi üretmez. Okuma worker'ı kontrol event loop'unu bloke etmez; panel kendi loading durumunu kullanır. Kalıcı kontrol düğmeleri kullanılabilir kalır. CLI backup/restore (6h) GUI veya raw DB indirme endpoint'i değildir.

6d–6e: Görevler panelinde [Türkçe görev önizlemesi](TASK_INTENT.md) vardır. Yalnız tam katalog eşleşmesinin İngilizce hedefini gösterir; görev seçimi, lease, scheduler ve eylem onayına etkisi yoktur. Genel Chat/Plan veya keyfi görev yürütümü değildir. Input değişince ve panel kapanınca eski/in-flight sonuç iptal edilir.

`ui/` React/TypeScript kontrol merkezini ve Tauri 2 native kabuğunu içerir. Mevcut authenticated FastAPI backend'i ve Docker/XFCE runtime korunur; UI için ikinci bir orchestration framework veya yeni yazıcı oluşturulmaz.

- **Bilgisayar:** gerçek noVNC görüntüsü, AGENT/HUMAN/PAUSED sahipliği, kalıcı Pause/Stop/Take Control/Return Control/Resume/Restart düğmeleri, mevcut sentetik input kabulü ve session kontrol izi.
- **Çalışmalar:** açıkça seçilen trajectory DB'deki en yeni 30 run; durum, kayıtlı model çağrısı/verification sayıları ve seçilen run'ın karar/action/verification/model-call özeti. Liste yeniden inference çalıştırmaz.
- **Modeller:** mevcut model ve deployment kayıtları; EXPERIMENTAL/ACTIVE durumu DB'den okunur. Servis readiness veya GPU residency olarak sunulmaz, promotion düğmesi yoktur.
- **Kaynaklar:** gerçek Docker inspect'ten RAM/CPU/PID-thread limitleri, network ve rootfs ayarı. Bunlar **kullanım/peak grafiği değildir**. GPU ölçülmüyorsa açıkça belirtilir.

**Görevler:** hello (6b-1), sabit browser formu ve vision canvas (6b-2 görev entegrasyonu), tek aktif scheduler, gerçek modeller veya açık fixture seçimi, eylem başına süreli Onayla/Reddet, aynı run'da güvenli Pause/Resume ve görevden trace'e geçiş uygulanmıştır. Native WebKit kontrol/hello kabulü de özel X11 test ekranında geçti. Bu tam Aşama 6 değildir: genel görev/normalizer, Chat/Plan/Adapters/Benchmarks/Settings açık kalır. CLI workspace kilidi eşzamanlı sahipliği engellemeye devam eder.

**HTTPS form tekrar ölçümü:** pinli yeni temel veya durum-planlı form oturumunda Development, authenticated `GET /api/remote-form/repeats` ile yalnız aynı sunucu profil/form/durum planı/trajectory kaynağındaki terminal, post-admission denemelerin taşıma-readback sayısını ve toplam/onay-penceresi dışı süre p50/p95'ini salt okunur gösterir. Onay penceresi toplamı ve sayısı ayrıca görünür; manuel/toplu onay ve tüketim overhead'i ayrılmaz. İlk `browser.form.open` zamanı kalıcı action intent'idir, görsel etki değildir. Kaynak yoksa, kanıt eksik/değişmişse veya eski 1.0 yanıtı gelirse sayı yerine yok/kullanılamıyor durumu görünür; eski backend 404'ü sıfır başarı sayılmaz. Bu ölçüm gerçek uygulama sonucu, saf model/tarayıcı hızı, site hakkı veya W6 kabulü değildir. [Ölçüm sınırı](REMOTE_FORM_REPEAT.md).

## Kurulum ve çalıştırma

**Geliştirici kontrolü:** [Playwright MCP](PLAYWRIGHT_MCP.md) ayrı profil üzerinden yerel arayüzü etkileşimli incelemek için eklenmiştir. Python Playwright regresyonları korunur; MCP, AOS model/gateway yürütmesinin parçası veya görev/onay yetkisi değildir. Projeye özel bağlantı yeni Codex oturumunda yüklenir.

Önce [DESKTOP_RUNTIME](DESKTOP_RUNTIME.md) hazırlığını tamamlayın. Kurulu inference modelleri yeniden indirilmez.

```bash
cd ui
pnpm install --frozen-lockfile
pnpm build
cd ..
.venv/bin/python scripts/serve_desktop.py --engine decider
```

Tarayıcıda `http://127.0.0.1:8765/ui/` adresini açın. Terminaldeki **yerel token dosyasını** giriş formunda kullanın. UI build'i hazırsa kök `/` adresi `/ui/` adresine yönlenir; eski minimal konsol `/legacy/` adresinde kalır. `dist` yoksa kök eski konsolu açar ve `/ui` mount edilmez. Backend başladıktan sonra ilk build yapılırsa backend'i normal şekilde yeniden başlatın.

Motor verilmezse görev başlatma kapalıdır; read-only paneller `--trajectory-database` dosyasını okumaya devam eder. `--engine fixture` yalnız sentetik test içindir, UI bunu açıkça gösterir. `--engine decider` mevcut pinned manifest'i ve varsayılan `~/.venv/bin/python` ortamını kullanır; `--decider-manifest` ve `--model-python` yalnız backend başlangıcında değiştirilebilir. Fallback/indirme/promotion yoktur. Görev motoru açıkken trace/registry kaynağı aynı tek-writer `--database` dosyasıdır (varsayılan `data/desktop-console.sqlite`); eski `data/aos.sqlite` değiştirilmez.

## Dar görev ve onay sözleşmesi

**Opsiyonel tek-görev delegasyonu:** Desteklenen backend'de **Approve all — this task only** / **Tümünü onayla — yalnız bu görev** varsayılan seçilidir; kullanıcı işareti kaldırıp manuel onayı seçebilir. Start yalnız seçili sabit görevin eylemleri için yeni exact-action approval ve policy/verification kontrolleri korunarak delegasyon verir. Checkbox tek başına başlatmaz; eski grant sonraki görev/sequence/resume'a taşınmaz. Doğrudan API'de varsayılan manuel kalır. [Audit, süre ve kapsam sınırları](TASK_AUTO_APPROVAL.md). Aşağıdaki her eylemde düğmeye basılan akış manuel moddur.

Görev aktifken yalnız `/api/tasks` 500 ms aralıkla güncellenir; daha ağır state/overview/resources incelemesi 3 saniyelik aralığı korur. Bu model hızlandırması değil, tamamlanan işi ekrana yansıtan polling gecikmesini azaltır. Ardışık tek poll döngüsü, abort ve kontrol değişikliği sınırları korunur.

### Browser / vision etkinleştirme

```bash
.venv/bin/python scripts/serve_desktop.py --engine decider --browser-tasks --vision-engine bonsai
```

`--browser-tasks` açıkça mevcut pinned Chromium manifest'ini kullanır; yeni browser/model indirmez. Vision için `--bonsai-manifest` varsayılan `models/bonsai-manifest.json` dosyasıdır. Yalnız wiring testi için eşleşen `--engine fixture --browser-tasks --vision-engine fixture` kullanılır; fixture görüntü yorumlamaz ve UI bunu belirtir. Karışık real/fixture S1–S2 CLI konfigürasyonu reddedilir. GET `/api/tasks` yapılandırılmış `kinds` listesini verir; etkin olmayan tür 409 ile reddedilir.

Browser/vision her job için **ayrı, yeni, ağsız Bubblewrap/headless Chromium runtime** açar. Docker/noVNC masaüstüsünde görünen tarayıcı değildir. Aynı desktop lease/generation oturum yetkisi olarak kullanılır; job ayrıca kendi execution runtime_id'sini saklar. Onay ve gateway bu runtime kimliğine bağlanır. Take Control bu işi iptal eder, headless browser'ı insana açmaz. Kaynak panelindeki Docker limitleri bu ayrı browser veya native GPU modellerinin kullanım/limit ölçümü değildir.

**Görünür görev seçenekleri:** `--desktop-browser` browser_form için, ek `--desktop-vision` vision_canvas için aynı owned Docker masaüstündeki headed Chromium'u seçer. İkincisi açık vision motoru ve desktop-browser gerektirir. Yeni managed pilot ikisini kullanır. GET tasks `browser_display` ve `vision_display` ile gerçek modları bildirir. Görevler'de tek kalıcı noVNC görüntüsü onay kontrollerinin yanında kalır; başarılı pencere sonraki görev/kontrol cleanup'ına kadar tutulur. Flagsiz headless yolları değişmez. [Form sözleşmesi](VISIBLE_BROWSER.md), [rendered capture, SAVE ve kabul sınırları](VISIBLE_VISION.md).

Browser formunda `browser.fill` ve `browser.submit` için **iki ayrı** action digest/onay gerekir. İlk onay ikinci eyleme taşınmaz; arada ret/expiry/takeover submit'i engeller. DOM snapshot/fingerprint/node kontrolü execution anında korunur. Vision tek capture/scene/symbolic SAVE action'ına bağlı onay ister; onay beklerken capture/scene değişirse execution reddedilir. Şema mevcut `desktop_approval.schema.json` sözleşmesidir; `examples/desktop_browser_approvals.json` iki sentetik onayı örnekler.

Migration **0006** yalnız scheduled task kind CHECK ve çoklu per-action approval ilişkisini genişletir; eski job/approval satırlarını kopyalayarak korur, foreign key enforcement'ı geri açar. Job başına aynı anda bir pending/approved onay, session başına tek aktif iş korunur. İlk beş migration değiştirilmez. Startup pin doğrulaması thread'de çalışır; cancellation owned startup tamamlanmadan runtime'ı bırakmaz. Bu nedenle yavaş browser başlangıcında kontrol devri readiness timeout'u (25 s + hash kontrolü) kadar bekleyebilir. Bonsai hash doğrulaması read-only thread'e taşınmıştır; iptal sonrası bu thread kısa süre sürebilir fakat model başlatamaz. Bonsai subprocess oluşturma yarışı shield/drain ile korunur; iptal mevcut process group ve geçici anahtarı temizler.

### Ortak sınırlar

- POST `/api/tasks` yalnız yapılandırılmış `kind=hello|browser_form|vision_canvas`, güncel desktop lease ve generation kabul eder. Keyfi goal, path, content, shell veya browser/vision parametresi reddedilir. İkinci görev kuyruğa alınmaz; 409 döner. Eski sentetik input kuyruğu iptal edilir ve görev sırasında input endpoint'leri kapalıdır.
- Model sadece mevcut finite seçeneklerden seçim yapar. Policy izin verirse immutable eylem önizlemesi üretilir. GET `/api/tasks` en son 20 oturum görevini ve bekleyen onayı gösterir. Önizleme yalnız sabit görev parametrelerini/symbolic kimlikleri içerir; genel payload veya screenshot açılımı değildir.
- `schemas/desktop_approval.schema.json` tüm Action envelope'u, SHA-256 digest'i ve en fazla 60 saniyelik expiry'yi tanımlar. Digest; runtime, task/run/step, state_version, lease, tool/arguments, deadline ve idempotency değerlerini kapsar. POST `/api/approvals/{id}` yalnız eşleşen digest ve boolean accept alır. Onay policy kapsamını genişletmez; ilk eylem ve deterministic bağımsız readback dışında yetki vermez.
- Beklerken run'ın son canonical phase'i EXECUTE hazırlığı, job durumu `waiting_approval` olur; henüz gateway action intent/effect yoktur. Yanıt ve yürütme öncesinde lease/state/deadline tekrar denetlenir. Onay atomik olarak bir kez consumed olur; sonra gateway intent-before-effect ve bağımsız verification uygular. Reddetme cancelled, expiry failed sonucudur; hiçbir eylem uygulanmaz.
- Kontrol endpoint'leri seri hale getirilir. Pause önce desktop lease'ini iptal eder; model çağrısını/approval beklemesini iptal edip model worker cleanup'ını bekler. Run/job `paused`, owner PAUSED olur; eski onaylar revoked edilir. Aynı run/task/step ve canlı execution runtime korunur. Stop/Take Control/Return Control/Restart/logout/shutdown ise paused işi de terminal olarak iptal edip ayrı runtime'ı kapatır.
- Resume yalnız aynı canlı backend oturumunda mümkündür: yeni desktop readiness/lease/generation, değişmemiş deployment/runtime ve uncertain action olmaması gerekir. Yeni observation → model kararı → yeni action/onay üretilir; eski eylem oynatılmaz. Browser doğrulanmış fill sonrası yeni DOM gözlemiyle yalnız submit'e devam eder; vision yeniden capture ve scene üretir. Duraklatılmış görev yeni job ve sentetik input kabulünü de engeller (`reserved=true`). Başlangıçta henüz run oluşturulmamışsa Pause terminal iptal olur. Çalışan göreve tekrar Resume 409 döner.
- Append-only migration `0007_paused_tasks.sql` paused job durumunu ve tek reserved job indeksini ekler; ilk altı migration değişmez. Backend restart reconciliation paused job/run'ı iptal eder, otomatik devam veya headless runtime sahiplenme yapmaz. Pause host/browser process snapshot veya çökme sonrası checkpoint kurtarma değildir.
- Bounded senkron file tool başladıysa kontrol isteği tamamlanmasını bekler; gerçekleşmiş etki geri alınmaz. Kesinti anında native process-group kapatılır; process oluşturma ile cancellation arasındaki yarış da drain edilir. Model-call intent önceden yazılır (mevcut enum nedeniyle tamamlanana kadar pessimistic `error`); iptal sonucu `cancelled` ve latency kalıcıdır.
- Append-only migration `0005_desktop_tasks.sql` job/approval kayıtlarını ekler. Onay/ret ve aktif görev kontrol müdahaleleri human_interventions'a bağlanır. Restart queued/running/waiting_approval işleri cancelled, pending/approved onayları revoked yapar; eylem intent/running sonuçları mevcut uncertain reconciliation kuralını korur. Otomatik replay yoktur. Bu kayıtlar otomatik training eligible değildir.

Native kabuk için, backend açıkken başka terminalde:

```bash
cd ui
pnpm native:run
```

Bu komut mevcut CachyOS/NVIDIA makinesinde doğrulanmış `GDK_BACKEND=x11 WEBKIT_DISABLE_DMABUF_RENDERER=1` uyumluluk profilini kullanır. İlk default Wayland probe'u protocol error ile kapandı; yalnız X11 denemesi GBM buffer hatası verdi. İki ayarla önce sekiz saniyelik native startup smoke geçti; sonraki native kontrol kabulü aşağıdaki özel Xvfb ekranında tamamlandı. Bu ayarlar host sürücüsünü veya sistem ortamını değiştirmez, yalnız çalıştırılan process'e uygulanır. [Upstream NVIDIA tartışması](https://github.com/tauri-apps/tauri/issues/9394) ve [WebKit DMA-BUF kaydı](https://bugs.webkit.org/show_bug.cgi?id=291332) benzer hata/uyumluluk sınırlarını açıklar.

Native URL bu dilimde sabit `127.0.0.1:8765/ui/` adresidir. Backend'in `--port` seçeneği tarayıcı konsolu için kullanılabilir; native kabuk başka porta sessizce geçmez. Native pencereyi kapatmak backend veya masaüstünü durdurmaz; önce UI'de Stop, sonra backend terminalinde Ctrl+C kullanılabilir. SIGTERM cleanup önceki masaüstü sözleşmesini korur.

## Güvenlik ve veri

- Tauri shell/filesystem/process plugin'i ve uygulama IPC command'i yoktur; capability listesi boştur. Native frontend'e host dosya/komut yetkisi verilmez. İncelenen [Tauri capability sınırı](https://v2.tauri.app/security/capabilities/) korunur.
- Navigation yalnız sabit HTTP loopback host/port ve `/ui/` yoluna izin verir. Yeni pencere, download, drag/drop ve devtools kapalıdır; WebView incognito'dur. Native auth hâlâ backend cookie/Origin/Host kurallarıyla yapılır.
- React aynı origin'den relative API/WebSocket URL'leri kullanır; CORS açılmaz. Token localStorage/sessionStorage'a yazılmaz; girişten sonra form temizlenir. Logout eldeki kayıtları siler ve masaüstü lease'ini iptal eden mevcut endpoint'i çağırır.
- Önceki lease'e ait VNC bağlantısı kontrol değişiminden önce kapatılır. noVNC istemci view-only bayrağı yetki sınırı değildir; ayrı server-side view-only listener korunur. `Yenile` görüntü istemcisini yeniden oluşturur.
- `--trajectory-database` host tarafından açıkça seçilir; API'den dosya yolu veya SQL alınmaz. Inspector SQLite `mode=ro` ve `query_only` ile ayrı snapshot okur. Writer lock/reconciliation veya migration çalıştırmaz; eksik DB'yi oluşturmaz.
- GET `/api/overview`, `/api/runs/{run_id}`, `/api/resources` authenticated ve salt okunurdur. Overview 50 desktop event/input, 30 run ve 50 registry satırı; trace her koleksiyonda en fazla 100 satır döndürür. Ham goal/prompt/model response, tool arguments, observation content, token ve screenshot bytes sunulmaz. Bu genel redaction/dataset-export servisi değildir.
- Kaynak dosyaları ve lockfile'lar manifest'e girer; `node_modules`, `dist`, Cargo `target/gen`, yerel DB/token ve ekran görüntüleri girmez. Docker image ve model deployment kimlikleri bu UI diliminde değiştirilmedi.

6b-2 görev kabulü için ayrıca `AOS_REAL_BROWSER_TASK_TESTS=1` gerçek Decider form fill/submit, Bonsai CUDA worker'ında takeover ve yeni Bonsai→Decider vision görevi çalıştırır. Real/fixture kanıtları ayrı raporlanır. React E2E fixture modellerle gerçek izole Chromium üzerinde formun iki onayı ve vision onay/trace akışını sınar; native WebKit yerine geçmez. Test görüntüleri `/tmp/aos-browser-task-approval.png` ve `/tmp/aos-vision-task-mobile.png` altındadır.

## Doğrulama

```bash
cd ui
pnpm build
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo test --manifest-path src-tauri/Cargo.toml --locked
cargo build --manifest-path src-tauri/Cargo.toml --locked
cd ..
AOS_DESKTOP_TESTS=1 AOS_BROWSER_TESTS=1 AOS_UI_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_package.py
```

Gerçek model sırasında kontrol devri ve onaylı hello kabulü için ayrıca `AOS_REAL_TASK_TESTS=1` ekleyin. Bu açık opt-in native Decider/CUDA çalıştırır; gerçek DB/trajectory kanıtını ignored `data/task-real-*` içinde korur. Varsayılan unit testler model yüklemez. React görev E2E'si fixture motoruyla gerçek Docker üzerinde reject → takeover → yeni run → approve → exact readback/trace → logout iptali uygular; 1440×1000 ve 390×844 görselleri yalnız `/tmp/aos-task-approval-*.png` altında tutulur. Gerçek Decider kabulü ayrı API testiyle kaydedilir; Chromium testi native WebKit kabulü değildir.

6a kabulünde Browser plugin olmadığı için mevcut Python Playwright/fresh Chromium kullanıldı. O aşamadaki test akışı: login → gerçek desktop pixel'leri → sentetik input → takeover/return/pause/resume → kaynak limitleri → sentetik fixture run trace → boş registry durumu → mobil overflow → stop/restart → logout/cleanup. Ayrı manuel scripted QA mevcut gerçek Decider kaydını **yalnız okudu**; 6a turunda yeni gerçek inference sonucu iddia edilmedi; 6b-1 gerçek kabulü STATUS'ta ayrıca kaydedilir. Kontrol çubuğu panel değiştirildiğinde görünür kalır. `AOS_UI_TESTS=1` için hazır frontend build'i gerekir; UI opt-in kapalıysa eski minimal konsol kabulü korunur.

Native Rust testi başka host/port, credentials, API yolu ve file URL navigation reddini doğrular. Derleme development profile içindir; AppImage/deb, imzalama/updater, release performansı ve farklı OS'ler kabul edilmedi. Güncel test sayıları ve ortam kanıtı [STATUS](STATUS.md) içindedir.

## Native WebKit kabulü

**6k gerçek model kabulü:** `AOS_NATIVE_UI_TESTS=1 AOS_NATIVE_REAL_TASK_TESTS=1` ayrıca gerçek pinned Decider/Bonsai ile aynı native akışı çalıştırır. Hello reject/takeover/Pause/Resume/approve, browser iki onay + aynı run/yeni digest resume, Bonsai scene → Decider click → bağımsız canvas verification test edilir. Gerçek trajectory `data/native-real-*` içinde korunur; screenshot'lar `/tmp/aos-native-real-*.png` olarak fixture görüntülerinden ayrıdır. Native test ve gerçek-model opt-in birlikte gereklidir; mevcut modeller/pin'ler kullanılır, indirme veya promotion yapılmaz. Kabul **X11 uyumluluk profili ve üç sentetik görevle sınırlıdır**; default Wayland, genel görev kalitesi veya release değildir.

```bash
AOS_NATIVE_UI_TESTS=1 AOS_NATIVE_REAL_TASK_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -p test_native_ui.py -v
```

**6j genişletmesi:** aynı değiştirilmemiş binary'de Türkçe katalog girişi/olumsuzluk, salt okunur Plan ve pending approval'ı değiştirmeyen Kurtarma paneli kabulü eklendi. Browser formunun iki ayrı onayı, verified fill sonrası Pause/Resume ile aynı run/yeni digest, DOM verification trace'i ve vision canvas onayı/bağımsız outcome trace'i de native UI'den sınanır. Üç görevde motor **fixture**; gerçek model kabulü yine ayrı API testidir. Browser/vision execution ayrı ağsız Chromium'dadır, kontrol arayüzü gerçek Tauri/WebKitGTK'dır.

WebKitGTK input AT-SPI'de `entry`/Text olarak görünür fakat EditableText sağlamaz. Türkçe metin için test sürücüsü yalnız kendi cookie-korumalı Xvfb ekranında XTest kullanır; eksik Unicode keysym özel test sunucusunda geçici bir keycode'a bağlanır ve eski mapping finally'de geri konur. Host klavye haritası/ekranı veya native capability ayarı değiştirilmez. İlk EditableText denemesi test-driver timeout verdi; bu başarısızlık ve başarılı yeniden koşu STATUS'ta ayrı kaydedilir.

`tests/test_native_ui.py` ve `tests/native_ui_checks.py`, **değiştirilmemiş Tauri binary'sini** gerçek WebKitGTK ile sürer. Browser plugin bu ortamda yoktur. Chromium/Playwright sonucu native kanıt yerine kullanılmaz: [AT-SPI Action](https://docs.gtk.org/atspi2/method.Action.do_action.html) gerçek pencerenin erişilebilir kontrollerini tetikler; XTest yalnız özel, cookie korumalı Xvfb ekranına tuş/fare gönderir. Native `.incognito(true)`, kapalı devtools, boş capability listesi ve navigation guard değişmez; WebDriver için gizli oturum kapatılmaz. Varsayılan host ekranına klavye göndermek yerine ayrı X sunucusu kullanılır.

Kabul akışı: native URL/başlık/giriş formu → klavyeyle token → canlı noVNC pixel'leri → sentetik giriş/readback → HUMAN takeover → native WebKit/noVNC üzerinden gerçek tuş/fare → container içinde bağımsız `read_note` → Return → Türkçe/Plan/Kurtarma → hello reject/takeover/pause/resume/approve → browser/vision onay/verification → Stop/Restart → Logout. Standart motor **fixture**; gerçek Decider/Bonsai native kabulü ayrıca 6k opt-in'i gerektirir. AT-SPI bazı yalın status span'lerini metin olarak açmadığından durumlar ayrıca salt okunur SQLite ile doğrulanır; bu sorgular UI eylemlerinin yerine geçmez. Native stdout/stderr denetlenir; özel Xvfb'nin beklenen DRI3/GLX uyarıları ayrı tutulur. **JS console/pageerror kanalı yakalanmadı**; Chromium console kontrolünün native için eşdeğeri iddia edilmez.

Gereksinimler: hazır frontend/native debug build, boş `127.0.0.1:8765`, Docker runtime, sistem `/usr/bin/python` + PyGObject/AT-SPI/GDK/X11 ve `xprop`. `AOS_XVFB` ile açık Xvfb yolu verilebilir; sistem Xvfb yoksa harness `data/native-x11/Xvfb` kullanır. Bu makinede yeni paket indirmek veya çalışan sistemi değiştirmek yerine mevcut pinned Docker imajındaki Xvfb ve eksik libselinux kopyalandı:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import subprocess
from aos.desktop import DOCKER
root = Path('data/native-x11')
root.mkdir(exist_ok=True)
image = json.loads(Path('models/desktop-manifest.json').read_text())['image_id']
subprocess.run([*DOCKER, 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
container = subprocess.check_output([*DOCKER, 'create', image], text=True).strip()
try:
    for source, name in [('/usr/bin/Xvfb', 'Xvfb'), ('/usr/lib/x86_64-linux-gnu/libselinux.so.1', 'libselinux.so.1')]:
        subprocess.run([*DOCKER, 'cp', container + ':' + source, str(root / name)], check=True)
finally:
    subprocess.run([*DOCKER, 'rm', container], check=True)
PY
AOS_NATIVE_UI_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_native_ui.py -v
```

Bu hazırlık yerel imajı kullanır, image/model indirmez. Kopyalar kaynak manifest'ine girmez; `LD_LIBRARY_PATH` yalnız Xvfb process'ine uygulanır. Harness fixture workspace/DB, Xauthority, native process group, Xvfb ve kendi container'larını temizler; gerçek-model DB/trajectory kanıtını ignored alanda korur. Screenshot'lar `/tmp/aos-native-*.png` altında, kaynak dışındadır. Native kabul development/software-rendered X11 profiline aittir; default Wayland, donanım hızlandırma, genel erişilebilirlik veya release dağıtımı kabulü değildir.
