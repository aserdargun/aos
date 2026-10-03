# Tek sürüm kabul kaydı

**Plan incelemesi: 3 Ekim 2026, 05:42:42 UTC.** Aşama sonuçlarının tek yetkili
manuel kaydı [release_acceptance.json](release_acceptance.json) dosyasıdır.
Development ekranı aynı dosyayı okur; bu plan ikinci bir durum kaydı, ilerleme
yüzdesi veya bitiş süresi tahmini üretmez. Bu inceleme JSON kabul bayraklarını
yükseltmez; yeni runtime, kullanıcı teslimi veya ürün tamamlanması değildir.

## Kontrol panelinin güncel okuma biçimi

12:50 kaynak checkpoint'i: teslimdeki yedi regresyon ve sentetik socket broker
takılması düzeltildi; tam core 3122 PASS/304 SKIP/0 FAIL-ERROR. Beş SQLite
finalizer uyarısı ayrıca açık kaydedilir. Maintenance kaynakta uygulanmış ve
CPU'da sınanmıştır, canlı çalıştırılmadı. Native exclusion producer/consumer ve
ortak GPU kabulü açık. v0.1.0 hedefi koşullu sürümleme kaydıdır; Git tag veya
eski HEAD'in yayımlanması değildir. Stage tarih/kabul bayrakları değişmedi;
source checkpoint mevcut canlı UI dosyalarına otomatik taşınmaz.

11:32 kaynak checkpoint'i: shared-only adayında 41 dosya/86 giriş politikası
ve 6 CPU kontrolü tamamlandı. Aday canlıya uygulanmadı; bakım/physical cleanup,
gerçek exclusion üreticisi ve Scientist-only GPU kabulü açık. Stage kabul
bayrakları değişmedi; kaynak JSON güncellemesi mevcut UI asset teslimi değildir.

3 Ekim kontrol merkezi güncellemesi: aynı canonical JSON v1.1 `checkpoint`
alanında tarihli EN/TR geliştirme günlüğü sunar. Altı aşamanın `observed_at`
ve kabul bayrakları korunur. Yeni checkpoint tarihi aşama kabulünü yükseltmez.
Genel durum, sürüm kontrol listesi ve kanıt geçmişi ayrı görünür; runtime
gözlem/yenileme bunlardan ayrı tutulur. [Panel kılavuzu](CONTROL_CENTER.md).

[Shared desktop manager](SHARED_DESKTOP_MANAGER.md) explicit inode provisioning
ve durable launch intent ile ilerledi; Scientist'in shared caller-unit/pre-main
delta'sı MAIN kaynakta bağımsız doğrulandı. Gerçek activation/cleanup composition
ve yeni exact nested scope/config incelemesi henüz açık.19 mutable yol ve iki nested seed deposu CPU kabulünde
izole edildi; gerçek ayrı named arayüz dosyaları hazır, runtime başlatılmadı.
09:59 kaynak diliminde [ortak drain/seal](SHARED_ADMISSION_DRAIN.md), process-bound
kalıcı audit ve40 CPU/ASGI kontrolü tamamlandı. Önceki227 inert source kimliği
tarihsel kaldı; gerçek trusted activation/cleanup ve native dışlama açık kalır.
Önceki43 CPU testi tarihli kanıttır; son sonuç [STATUS](STATUS.md) kaydındadır.
Güncel native8765 oturumu değiştirilmedi. Bu dilim, GPU HOLD veya
Scientist'in tek GPU yürütücüsü olma sınırını kaldırmaz.

## Güncel inceleme ve gecikmenin nedeni

Bu altyapı özeti 3 Ekim 07:07 UTC kalıcı görev runner'ı ve ortak config incelemesiyle
yenilendi; 06:20:11 gerçek UI receipt'i korunur. Orkestrasyon/çoğaltmanın opt-in
çekirdeği ve CPU adaptör kabulü uygulandı; canlı kullanıcı UI'si ve native genel
orkestrasyon kabulü henüz yoktur. Plan incelemesi
tarihi korunur. Canonical JSON ve UI'nin manuel kabul kaydı **05:50:46 tarihli
snapshot** olarak değişmeden kalır; sonraki bağımlılık/deployment değişimini veya
gerçek yerel UI kontrolünü canlı runtime yetkisi gibi temsil etmez.

APIv6/native-v8 revision2 için Sol/high kaynak incelemesi **136 API ve141 native
kaynak pininin**,9 native ve5 API çıktı hash'inin ve canonical successful-resolution
şemasının eşliğini doğruladı. AOS HEAD `ed6e857`, Scientist HEAD `55c5300`;
değişiklik içeren çalışma ağaçlarında HEAD veya tracked diff tek başına bütün
kaynak kimliği değildir. Yeni tek ACK seçili kaynak ve inert config türetimini
kabul eder; canlı runtime/config/generation kabulü değildir.

Yeniden başlatma sonrası11 scope dizininde device54→55 değişti; inode, UID,
mode ve boşluk aynı, iki DB hâlâ yok. Mevcut plan/binder exact kimlik denetimi
bu eski pini haklı olarak reddediyor. Tarihsel scope receipt korunarak yeni
`scope-preparation.boot-d2c97005.private.json` gözlemi eklendi. Scientist'in
etkilenen scope/config türetimi bu gözleme bağlandı; **tek birleşik source/config
ACK** 05:49:18 UTC private FINAL kaydında kabul edildi, SHA
`dd26c6b818c8be29432612cab29fe94eb237aff32e1689d80ac0234e50dcf384`.
Bu tarihli incelemede 136 API/141 native kaynak ve 14 çıktı hash'i eşleşti; eski54 receipt ve reddedilmiş
taslak korunur. Root ACK'i çalışan MCP kanalından Scientist'e iletti.
`configuration_derivation_accepted=true`; runtime/start/GPU/native kabulü false.
05:50:46 canonical JSON yalnız `joint_source_configuration` engelini çıkardı;
source, verification, delivery ve diğer kabul/yetki bayraklarını değiştirmedi.
Sonraki meşru glibc/OpenSSL yükseltmesi için imzalı eski/yeni paket baytları
doğrulandı; açık Astra/max incelemesi ve root yetkisiyle yalnız sekiz Bonsai native
bağımlılık hash'i yeniden pinlendi. 12 diğer kütüphane ve bütün diğer manifest
alanları, 136/141 seçili kaynak baytları korunur. Yeni manifest SHA
`96a50e62fd69a5a5651b7270d874d57c32dbdc0e3174d436d317921821e36dc4`;
Supervisor deployment kimliği `bonsai-8e74c85adfa90771359d185fb9235b3ffd6543a8cd6e73ef44cf292688ef3c1c`.
Dolayısıyla önceki `dd26...f384` ACK **tarihsel** kaldı. İlk yeniden türetimde
broker argv içindeki iki eski profil/policy yolu bulundu; `4f10...` hazırlığı
reddedildi ve kullanılmamış yerel hatalı review açıkça geri çekildi. Düzeltilmiş
`04577691ff68907cf939228f9db62d6d7f56d1eb5c46bc8bbc03542a0ee09d07`
hazırlığı bağımsız etkin argv/environment ve gerçek config/hash grafiğiyle
doğrulandı; yeni FINAL ACK SHA
`d5e1a6e5604f89e869c088561cce2d219177618ef132e6bb249dff7ff735b4ea`.
136 API/141 native kaynak eşliği korunur; runtime/start/GPU/native bayrakları
false. Scientist oturumu bu ACK'i aldı. Eski ACTIVE ve native kabulü yeni kimliğe
aktarılmaz. Canonical
arbiter/root ve actual caller/broker generation yeni boot'ta canlı gözlenmeli;
eski device54 review runtime yerine geçmez. Fresh generation/resource check,
gerçek retained resolution, ikinci çağrı/fairness ve iptal/report/cleanup için
Scientist tek GPU yürütücüsüdür; ikinci bir peer onay katmanı eklenmez.

Doğrudan oturumlar arası MCP haberleşmesi ve peer mesaj teyidi artık çalışıyor.
Önceki bağlantı arızası güncel engel değildir. Eski-boot oturumu exact fenced
recovery ile güvenle retired oldu. İlk start meşru OS yükseltmesinden kaynaklanan
sekiz Bonsai dependency hash farkını fail-closed reddetti; imzalı provenance ve
yukarıdaki açık sınırlı repin sonrası desteklenen plain real start geçti.
Yeni oturum `app-d0898491ef744a258969589e4e41ffcd`, exact URL
`http://127.0.0.1:8765/ui/`; manager supervisor/backend `same_process`/running.
Birleşik TypeScript/Vite build, 5 Development CPU + 1 tam-kabuk sentetik CPU +
1 dil testi geçti. 06:17:53 gerçek EN/TR desktop ve TR390 tarayıcı kontrolü,
HTTP200/standart otomatik giriş, busy/reserved=false, jobs=[], approval=null,
sıfır task submit ve console/page/network/overflow hatasıyla geçti. Bunlar gerçek
yerel UI erişimi kanıtıdır; Mac, yeni model görevi veya native/GPU kabulü değildir.
Sol/high actual receipt `data/ui-delivery-recovery-20261003/live-default-delivery.private.json`
SHA `c9864ea7b431c3678991458d8eb9a9b1ca5e585a3dcfc3eb5f17f35ce4edd34a`,
doctor 7 PASS ve 06:20:11 CPU-only hazırlık ready kaydını taşır. Bu hazırlık
worker/ağırlık yükleyebilir; worker PID unavailable ve fiziksel GPU ölçümü yoktur.
Inference veya GPU test kabulü sayılmaz. Mac'in Tailscale'de
online olması ve AOS hostunda sshd'nin açık olması gerçek Mac/SSH/UI kabulü
değildir. Default tünel ve URL her iki uçta8765 kullanır;18765/localhost Host
eşdeğerliği varsayılmaz.

**Güncel GPU engeli:** canlı default UI `--engine decider --vision-engine bonsai`
ile native çalışır; görevler ve bilgi yanıtları canonical broker dışında GPU
kullanabilir. CPU prewarm veya idle olması bunu engellemez. Mevcut restart/quiesce
girişleri kapatır fakat retained worker'ı drain etmez; release/restart kapıyı yeniden
açabilir. Pause da tekrar başlatılabilir, dolayısıyla özel GPU rezervasyonu değildir.
Canlı kullanıcı oturumu değiştirilmedi. Ortak koşu için mevcut reviewed
`--engine scientist` host yolunun açık opt-in manager/launch entegrasyonu, native
fallback olmaması ve explicit idle handover/fiziksel cleanup gerekir. Hazırlanmış
izole18865 host, default8765 ile aynı lifecycle/scope değildir. Scientist tek GPU
yürütücüsüdür; mevcut durumda GPU kabulü HOLD.

**Bağımsız somut ilerleme:** [kalıcı delegated-agent runner v1](AGENT_ORCHESTRATION.md)
ayrı SQLite, sürümlü kayıt, bağımlılık, atomik kota, onay bekleme, job-specific iptal
ve bağımsız sonuç/cleanup ile uygulanmıştır. 49 CPU/sentetik regresyon ve dört gerçek
sabit CPU worker'ı geçti; iki paralel işten birini iptal diğerini etkilemedi.
Scientist adaptörü gerçek AOS Lab service/client/journal'ını sentetik HTTP peer ile
kullanır; otomatik onay yoktur ve GPU cleanup provider yokken kapasite tutulur.
Yeni dosyalar seçili74 AOS kaynağı ve root trajectory migration dizini dışındadır.
Bu sonuçlar canlı UI eylemci teslimi, gerçek Scientist/GPU veya ürün kapanışı değildir.

Gecikmenin somut nedenleri kaynak/config revizyonlarının hareket etmesi,
reboot ile süreç/dosya kimliklerinin eskimesi, daha önce kesilen koordinasyon
ve CPU/mikro incelemelerin gerçek uçtan uca kabulün yerini alamamasıdır.
Mac üzerinden kullanıcı teslimi de henüz yapılmadı. **2741PASS/299SKIP** geniş
CPU koşusu ve ayrı dizine açılan kaynak arşivinin5286 paket kontrolü tamamlanmış
kanıttır; değişiklik veya yeni hata olmadan bunları yinelemek yeni ilerleme
sayılmaz. Öncelik mevcut akışın gerçek sonucunu kapatmaktır.

Tarihsel gerçek dosya→sınırlı deney→AOS rapor akışı geçti; araştırma sonucu
DISCARD/no improvement idi. Son v7 girişiminde gerçek S1 yanıtından sonra
retained kapanışı başarısız oldu; görev failed kaldı, replay yapılmadı ve owned
cleanup doğrulandı. Cleanup-only geçmiş closure ve CPU tanı düzeltmeleri bu
hatayı veya ikinci görev/fairness/iptal kabulünü kapatmaz. Kanıtlar [STATUS](STATUS.md)
içinde tarihleriyle korunur.

## Kayıt nasıl okunur?

- `source`: `implemented` kaynak dilimi uygulanmış; `partial` aşamanın bir
  bölümü uygulanmış; `pending` aşama için tamamlanmış uygulama kabulü yok.
- `verification`: `cpu_verified` yalnız belgelenmiş CPU/sentetik veya gerçek
  CPU araç akışları; `historical_native` önceki sınırlı native-model kanıtı;
  `pending` bu aşamanın kabulü yok. Hiçbiri aşamanın bütünü kapandı demek değildir.
- `delivery`: `not_delivered` güncel aşama kullanıcıya teslim edilmiş değil;
  `historical` önceki sınırlı teslim var, en yeni kaynak teslimi değil;
  `pending` teslim kabulü henüz yok.
- `runtime_authority=false` ve `product_complete=false` sabittir. Kayıt salt
  okunur gösterim içindir; model, görev, yeniden başlatma, eğitim veya ağ yetkisi
  vermez. Bütün kutuların sayısı veya test toplamı ürün yüzdesi değildir.
- `evidence` mevcut repo belgelerine ve ignored yerel loglara işaret eder.
  Paket alıcısında bulunmayan private log doğrulanmış teslim kanıtı sayılamaz.
  Güncel ölçümün ayrıntılı günlüğü [STATUS](STATUS.md), geçmiş kapsamlar ilgili
  sözleşmelerdir. Eski belgelerdeki “sıradaki/açık/güncel” sözleri bu kaydı geçersiz
  kılamaz; tarihsel kayıt olarak okunur.

## Kullanıcının istediği ürün kabulü

AOS'un ürün rolü merkezi görev orkestratörlüğüdür: kullanıcı hedefini sınırlı işlere ayırır,
bağımlılıkları ve eylemci seçimini yönetir, yetkili işi gönderir, durum/olay ve
sonuçları izler, bağımsız doğrular ve iptal eder. İlk dış eylemci AI-Scientist'tir;
gelecek eylemciler aynı sözleşmeden katılır. İçerideki S1 Operator ve S2
Supervisor rolleri korunur. Orkestrasyon/çoğaltmanın yukarıdaki opt-in çekirdek
dilimi CPU'da doğrulanmıştır; genel canlı host/Tasks entegrasyonu ve gerçek eylemci
kabulü açık kalır. Üç gereksinim aşağıdaki
altı aşamadadır; hello veya birkaç fixture başarısıyla kapsamları daraltılmaz:

| Ürün gereksinimi / aşamalar | Gösterilmesi gereken davranış ve kanıt |
|---|---|
| Yetkili web uygulamalarını kullanma — `native_workflow`, `user_delivery`, `learning`, en son `swapp` | Ubuntu Chromium/Playwright MCP ve DOM üzerinden izinli login/oturum, gezinme, formlar ve çok adımlı görev; taze state, bağımsız uygulama sonucu, değişmiş sayfa/oturum sonu/zaman aşımında kontrollü toparlanma, belirsiz POST'ta sıfır replay. Aynı kullanıcı UI'sinden yeni girdiler ve incelenmiş skill tekrar kullanımı ölçülür. İki owned uygulama tekrar üretilebilir asgari kapıdır; gerçek hedefin yetkili görev kataloğu ayrıca kabul edilir, bütün web siteleri desteklenmiş sayılmaz. |
| AI-Scientist'in tam görev entegrasyonu — `scientist`, `user_delivery` | Aynı AOS Tasks/UI, typed tool/policy, geçerli onay ve kalıcı intent üzerinden başlatma→durum/olay→sonuç/bağımsız readback; ayrıca iptal→drain→fiziksel cleanup. Tek GPU authority/broker, gerçek kuyruk çakışması, fencing ve sonraki ayrı AOS görevi kanıtlanır. Ayrı manuel script gösterimi tek başına tam entegrasyon değildir. |
| Merkezi orkestrasyon, yeni eylemci ve sınırlı çoğaltma — `candidate`, `scientist`, `user_delivery` | Sürümleşmiş typed agent/capability kaydı ve kimliği; iş/bağımlılık/durum/sonuç/iptal takibi, kapsamlı izin/kaynak/bütçe ve start/status/cancel/results/events. AOS gerektiğinde önceden yetkili kota içinde ek izole instance veya sınırlı worker pool açar. İki izole iş/instance, bağımlılığın doğrulanmadan başlamaması, birini iptal etmenin diğerini etkilememesi, kaynak tavanı ve eski generation/geç yanıt/tekrar teslimin yeni etki üretmemesi kanıtlanır. Sentetik ikinci adaptör sözleşme uyumunu sınar; gerçek başka eylemci kendi kabulünü ister. |

`ScientistLabTask/Action/Policy`, durable Lab service, authenticated console
rotaları ve `ScientistLab` paneli zaten vardır. Önceki sabit `anomaly` ve 1 deney/
30 saniye/100 token önerisi kaynakta explicit boş bütçe alanları, protokol-bounded
track ve yerel exact request preview ile değişti; 9 sentetik Chromium testi,
TypeScript ve EN/TR dil kontrolü geçti. Host yalnız suite/program bildirir;
etkin bütçe/track izinleri, task purpose ve Lab profile metadata'sı yoktur.
Bu kaynak dilimi host caps veya gerçek lifecycle kabulü değildir; server admission
ve ayrı exact insan onayı korunur. Etkin host seçeneklerini bildirme ve aynı UI'den
gerçek Scientist kabulü açık kalır; yeni factory eklenmedi. `ToolRegistry`, `DecisionEngine`, `Supervisor` ve
Scientist'in pinli protokolleri yeniden kullanılır. [Mevcut genişletme sınırları](EXTENDING_AOS.md)
ve [composition sözleşmesi](INTEGRATION_COMPOSITIONS.md) genel, sürümleşmiş
üçüncü-eylemci sözleşmesini henüz tamamlanmış göstermiyor. `DesktopScheduler`
tek aktif masaüstü işi yürütüyor; `TaskSequences` yalnız2–3 farklı sabit
hello/browser/vision görevini sıralıyor. Lab journal/service kendi typed
görevlerini izliyor. Bunlar genel bağımlılık orkestrasyonu, eylemci kaydı veya
izole worker çoğaltma hizmetinin kanıtı değildir. Bu açıklar mevcut çift kararlı
olduktan sonra aynı görev/intent/policy sınırlarını kullanan küçük typed modüller,
canonical şema ve gerektiğinde append-only migration ile kapatılır.

**Görev dağıtma/çoğaltma ile GPU tahsisi ayrı sorumluluklardır.** AOS hangi işi
hangi yetkili instance'a vereceğini belirler; GPU tahsisi mevcut tek Scientist
canonical broker/fencing yolundan geçer. Ayrı workspace/runtime ve kotalı CPU
işçileri paralel olabilir;16 GiB GPU'daki model işleri ölçülmüş bütçeyle sıraya
girer, birlikte resident olmak zorunda değildir. Her masaüstünün tek input
sahibi korunur. Yeni bağımsız GPU scheduler, sınırsız self-replication veya
ajan framework'ü kurulmaz; model çıktısı instance sayısını, bütçeyi veya yetkiyi
kendi başına artıramaz. Çoğaltma kota/yetki içinde host tarafından yürütülür.

## Altı kapanış aşaması

| ID / başlık | Kapanış için gereken en küçük kabul | Açık dış kapılar |
|---|---|---|
| `candidate` — Tek sabit teslim adayı | Sahipliği ve bağımlılıkları incelenmiş AOS/Scientist kaynak çifti, untracked dahil dosya hash'leri, config/artifact kimlikleri ve UI build için tek aday kaydı; kabul boyunca aynı baytları sınama ve paketleme. | Seçili kaynak baytları aynı; önceki inert scope/config ACK yeni Bonsai dependency kimliği sonrası tarihsel. Etkilenen config türetimi, bütün teslim adayının kaydı ve incelemesi açık. CPU başarısı veya HEAD tek başına freeze değildir. |
| `scientist` — İlk eylemci ve merkezi görev orkestrasyonu | Mevcut factory/provider/async composition ile aynı AOS yolundan tam Lab yaşam döngüsü; retained resolution, ikinci AOS görevi, kuyrukta adil paylaşım, typed iptal/rapor ve fiziksel cleanup. Kararlı çift üzerinde sürümleşmiş eylemci kaydı, bounded iş/bağımlılık yönlendirme ve iki izole instance'ın kota/iptal/sonuç kabulü. | Yeni Bonsai manifesti için aynı-wire config delta, fresh actual caller/root/broker kimlikleri ve enabled-runtime kabulü açık. Gerçek ortak GPU kabulünün tek yürütücüsü Scientist; UI istek editörü kaynakta var, etkin host metadata'sı/gerçek UI lifecycle, genel orkestrasyon/çoğaltma ve üçüncü-eylemci sözleşmesi açık. |
| `native_workflow` — Web görevlerinin native kabulü | Bonsai planı→sonlu S1→executor→bağımsız oracle zinciri; iki uygulamada yeni girdilerle kullanıcı yolundan çok adımlı görev, skill tekrar kullanımı ve yukarıdaki login/oturum/toparlanma kapsamı. | Named iki-app CPU/fixture başarısı var; güncel iki-app native ve gerçek yetkili görev kataloğu kabulü yok. Desteklenen kapsam görevlerle belirtilir; gerçek SWAPP en son kalır. |
| `user_delivery` — Kontrol merkezi, güncel UI ve Mac teslimi | Exact instance/URL/build ve anlaşılır kontrol panelinden canlı işler, manuel geliştirme durumu, engel/sıradaki iş/kanıt ayrımı; giriş→Tasks→web/Scientist görevi→doğrulanmış sonuç→güvenli kapanış. Gerçek Mac/SSH yolu ve izole temiz kurulumdan tekrar üretim. | Birleşik CPU build ve gerçek yerel yeni-session UI/giriş/read-only kontrolü geçti. Yeni web/Scientist görevi, güvenli kullanıcı kapanışı, gerçek Mac/SSH ve temiz kurulum kabulü açık. Canonical dated kaynak snapshot ve geniş delivery bayrağı değiştirilmedi. |
| `learning` — S1/S2 öğrenme ve model yaşam döngüsü | İncelenmiş düzeltme/skill/bilgi ve ayrı rol verisinden haklı export; uyumlu tokenizer/loss/trainer ve fresh reload; leakage-safe bağımsız değerlendirme sonrası açık promotion ve rollback. Typed alternatif model/uzman ajan sınırları korunur. | Veri hakları/redaction/review, S2 trainable checkpoint/adapter uyumu ve held-out değerlendirme gerekir. Tarihsel S1 deneyi üstünlük, genelleme veya production promotion değildir. |
| `swapp` — En son gerçek SWAPP W1–W6 | Kullanıcı yetkili intranet/origin/hesap/tenant/rolü ve ilk izinli görev/oracle'ı sağladığında ayrı admission ile gerçek W1–W6, haklı çift-rol veri ve tekrarlı ölçüm yap. | Yetkili intranet/tünel ve exact görev/oracle yok; gerçek-site kabulü0/6. Sentetik başarıyla kapatılamaz. |

Sıra kapsamı küçültmez: native görev, Scientist, öğrenme, model değişimi, Mac,
temiz teslim ve SWAPP hedeflerinin hepsi korunur. Dış erişim gerektiren Mac,
trainable checkpoint ve SWAPP kapıları bağımsız kaynak işlerini engellemez;
ancak eksik kanıt “tamamlandı” olarak değiştirilemez. Yeni mod veya panel değil,
aynı adayın gerçek kullanıcı zinciri önceliklidir.

## Uygulama sırası ve teslim ölçütü

Planlama **GPT-6 Astra / max**, uygulama ve kaynak incelemesi **GPT-6.1 Sol /
high** rolleridir; bunlar root model değişimi veya AOS runtime model seçimi
iddiası değildir. Altı aşama korunur. **Kontrol merkezi ve mevcut Scientist
bütçe formu şimdi paralel kaynak işidir**; peer/runtime kabulünü beklemez.
SWAPP kullanıcı kararıyla en sondur; yeni plan onayı bekleme adımı yoktur.

1. **`candidate`: mevcut çifti sabitle.** Yeni append-only scope gözlemini
   Scientist'in inert plan/config türetimine bağla; Sol/high yalnız etkilenen
   hash/kimlik farkını incelesin ve gerçek bulguyla tek birleşik ACK üretsin.
   Güncel kaynak/UI/artifact kaydını bir kabul denemesi boyunca sabit tut.
   Somut hata düzeltilirse ardıl aday ve etkilenmiş kontroller kaydedilir;
   tarihsel receipt değiştirilmez, device veya deadline denetimi gevşetilmez.
2. **`scientist`: mevcut gerçek zinciri bitir.** Scientist fresh APIv6 ve
   original süreli yetkiyle güncel caller/root/broker kimliklerini bağlasın.
   İlk AOS işi ve retained resolution → Lab A aktifken olayla kanıtlı kuyruk
   → doğal release sonrası ayrı başarılı AOS işi → terminal rapor/AOS readback
   → ayrı Lab B'de inference sırasında typed stop → rapor ve fiziksel cleanup
   zincirini çalıştırsın. Tamamlanmış Lab işi iptal için yeniden açılmaz.
   Paralel hazırlanan host-bağlı UI önerisiyle aynı AOS yolundan gerçek lifecycle
   kabulünü tamamla. Kararlı çiftin ardından sürümleşmiş eylemci adaptörü, bounded
   iş/bağımlılık yönlendirme ve yetkili kota içindeki izole instance çoğaltmayı
   mevcut görev çekirdeğine ekle. İki iş/instance, tekil iptal, kaynak tavanı,
   generation ve bağımsız sonuç kontrolleri olmadan genel orkestrasyon kapalıdır.
3. **`native_workflow`: desteklenen web işlerini uçtan uca kabul et.** Aynı
   adayda iki owned uygulama ve yeni girdilerle gerçek S2/S1, bağımsız sonuç,
   izinli oturum/form/çok adım, değişmiş state/toparlanma ve skill tekrar
   kullanımını ölç. Görev kataloğundaki gerçek açığı mevcut typed araçlarda
   kapat; yalnız etkilenen kontrolleri çalıştır. Gerçek hedef/hesap/oracle
   sağlandığında aynı kabulü o kapsamda yap; SWAPP koşusu altıncı aşamadadır.
4. **`user_delivery`: kontrol merkezini kullanıcıya gerçekten ulaştır.**
   Aşağıdaki panel düzenlemesini ve bütçe formunu bağımsız CPU/UI kabulünden
   geçir; hazır build ile
   exact eski-session için mevcut `restart --expected-session` fenced recovery
   yolunu kullan; Docker'ın ve ilgili süreçlerin o andaki sahipliğini kontrol
   et, başka projelerin container veya GPU işlerine dokunma. Docker inactive
   fakat socket active ise salt okunur görünen inspect/doctor bile daemon'ı
   uyandırabileceğinden bu yan etkiyi önce çöz. Mevcut başlatma yetkisini yeniden
   sorma. Mac'ten8765→8765 tünel, gerçek tarayıcı girişi, Tasks sonucu ve kapanışı
   kanıtla; ardından kilitli bağımlılıklardan temiz kurulum kabulünü yap.
   Plain UI'nin CPU hazırlığı GPU işi başlatmaz; sonraki doğrudan Decider
   görevlerinin ortak broker'a katıldığı varsayılamaz. Paylaşımlı görev kabulü
   desteklenen `--engine scientist` ve factory yolunda tek authority ile ayrıca
   kanıtlanır; kullanıcının default modu sessizce değiştirilmez.
5. **`learning`: veri ve model yaşam döngüsünü kapat.** İzin/hak/redaction,
   incelenmiş düzeltme/skill/bilgi ve ayrı S1/S2 provenance ile export; model ve
   veri kullanım hakları; gerçek tokenizer/loss/trainer/adapter uyumu ve fresh
   reload; leakage-safe held-out karşılaştırma; ayrı açık promotion ve rollback
   kanıtı üret. Alternatif model/eylemci verisi kendi typed rolünü korur.
   Aynı fixture grubunun eğitimi veya DISCARD sonucu kalite artışı sayılmaz.
6. **`swapp`: gerçek W1–W6 en son.** Yetkili intranet/origin/hesap/tenant/rol,
   görev kataloğu, bağımsız oracle ve veri akışı haklarını exact scope'a bağla;
   gerçek kullanım, bilgi/skill, çift-rol veri, değerlendirme ve tekrarlı ölçümü
   tamamla. Erişim eksikse bu kapı açık kalır; diğer ürün işlerinin yerini almaz.

## Hemen paralel iş: AOS kontrol merkezi

Mevcut React/Development/Tasks/Scientist yüzeyleri yeniden düzenlenir; yeni
runtime veya hayali API eklenmez. İngilizce birincil, Türkçe ikincil dil kalır.
Görsel hiyerarşi, tutarlı boşluk/tipografi, responsive düzen ve klavye erişimi
kullanıcının “neredeyiz, şu an ne oluyor, sırada ne var?” sorularını ilk ekranda
yanıtlamalıdır:

- **Oturum başlığı:** mevcut instance/URL ve eldeki kaynak/build kimliği, bağlantı
  durumu, son başarılı gözlem zamanı; online/offline/stale/unknown açık ayrılır.
- **Canlı çalışma alanı:** mevcut AOS/Scientist işleri, gerçek evre/eylemci,
  onay veya kuyruk bekleme nedeni, sonuç ve mevcut Tasks/Trace kontrolleri.
  Güncel iş ile geçmiş son iş ayrıdır; veri yokluğu boşta/başarılı sayılmaz.
- **Geliştirme ve teslim:** yalnız `release_acceptance.json` üzerinden altı
  aşamanın source/verification/delivery, tamamlanan kanıt, engel ve sıradaki
  somut adımı; tarihli manuel plan canlı runtime telemetrisi gibi gösterilmez.
- **Kanıt ve kaynaklar:** son doğrulanmış sonuçlara bağlantı, hata/iptal ve
  gerçekten gözlenebilen kaynak/GPU durumu. Desteklenmeyen orkestrasyon veya
  worker çoğaltma yeteneği planlanmış gösterilir; çalışıyor gibi sunulmaz.

`/api/state`, `/api/tasks`, `/api/overview`, `/api/resources` ve mevcut
Scientist envanter/iş rotalarının sağladığı veriler kullanılır. İkinci bir manuel
tamamlanma kaydı, uydurma canlı olay, yüzde veya test/adet sayısından ürün
tamamlanması üretilmez. Loading/empty/error/stale ve dar ekran durumları,
EN/TR, salt okunur yenilemenin iş başlatmaması, doğru Tasks/Trace yönlendirmesi
ve mevcut onayların korunması CPU/browser kabulüyle ölçülür. Mevcut Scientist
panelinde hostun kabul ettiği bütçe seçimi aynı iş paketindedir. Bu kaynak/UI
kabulü ile8765 ve gerçek Mac teslimi ayrı kanıtlardır.

## Kurumsal forka uygun kalite sınırı

Hedef, şirketin geliştirebileceği denetlenebilir çekirdektir. Kabul adayı şu
kanıtları birlikte taşır: küçük sürümleşmiş typed eylemci sınırı ve merkezi
bounded görev orkestrasyonu; kotalı instance izolasyonu, asgari yetkili workspace
ve tek GPU authority; kalıcı intent/idempotency/audit ve bağımsız
sonuç; sır/veri izolasyonu; append-only migration ve doğrulanmış backup/recovery;
kilitli, tekrar üretilebilir kurulum ve özel artifact içermeyen kaynak teslimi;
anlamlı sözleşme retleri ile gerçek uçtan uca kabul ve belgeli işletim sınırları.
Mevcut modüller kullanılır; her açık somut kabul örneğiyle kapatılır.

Token istemeyen loopback kullanıcı pilotu tek-kullanıcılıdır; kurumsal RBAC,
çok-kiracılı izolasyon veya SSO uygulanmış kabul edilmez. Kurumsal fork için
kimlik/yetki, intranet ve veri yönetişimi ayrıca tasarlanıp kabul edilmelidir;
bu plan pilotun varsayılanını sessizce değiştirmez. Lisans seçimi ve genel CI
ayrı işlerdir; kaynak paylaşımı kullanım hakkı veya kurumsal hazır ürün iddiası
değildir. Bu inceleme lisans seçmez, commit/push/deploy yapmaz.

## Güncelleme kuralı

Kabul sonrası yalnız ilgili aşamanın source/verification/delivery durumunu,
engellerini ve evidence referanslarını gerçek ölçümle güncelle; `observed_at`
yeni UTC gözlemidir. Bir source fix, fixture sonucu veya HTTP200 için native
ya da kullanıcı teslimi bayrağı yükseltme. Aynı kabul kaydına yeni kanıt ekle;
ikinci tamamlanma listesi üretme. AOS yalnız kendi workspace'ini değiştirir;
Scientist kaynakları salt okunur incelenir ve ortak GPU işini yalnız Scientist
yürütür. Production enablement ve model promotion ayrı açık yetki gerektirir.
Private token/veri/DB ekleme; README kullanım sayacı ve MANIFEST son geliştirme
devrinde ana oturum tarafından güncellenir.

Tarihsel kronoloji [STATUS](STATUS.md) içinde korunur. Kullanım komutları
[FIRST_RELEASE](FIRST_RELEASE.md), ayrıntılı mimari/kabul sözleşmeleri
[ROADMAP](ROADMAP.md), kısa yönlendirme [REMAINING_WORK](REMAINING_WORK.md)
üzerinden erişilir; aktif kuyruk ve durum yalnız bu kayıttadır.
