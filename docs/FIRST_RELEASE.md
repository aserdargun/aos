# AOS v0.1 — bu bilgisayarda denenebilir yerel pilot

Çalışan pilotu Mac’ten denemek için [Hızlı pilot başlangıcı](PILOT_QUICKSTART.md) adımlarını izleyin.

Yeni source checkout için ayrı [locked dependency ve console staging kurulumu](LOCAL_SETUP.md)
mevcut ortamı değiştirmeden hazırlık yapar; modeller/runtime ve explicit promotion
ayrı kalır. Hazırlanmış dependency ortamı tamamlanmış genel installer değildir.

## Güncel kapanış kaydı

Kaynak, doğrulama ve kullanıcı teslimi ayrı izlenir. Tek güncel kapanış kaydı
[RELEASE_ACCEPTANCE](RELEASE_ACCEPTANCE.md) / [release_acceptance.json](release_acceptance.json)
dosyasıdır; aşağıdaki eski test/deploy kayıtları yeni kaynakların kullanıcının
oturumuna taşındığını göstermez. Named CPU iki-app akışı gerçek browser/tool
kabulüdür, native/Scientist veya Mac donanım kabulü değildir. Bu kullanım rehberi
yeni izin, otomatik restart veya tamamlanmış installer iddiası oluşturmaz.

## Tarihsel kapanış gözlemi — 30 Eylül 2026

Yeni özellik sayısı veya eski Development checklist yüzdesi release kabulü
değildir. Aşağıdaki tablo o tarihin kanıt sınırlarını saklar; güncel iş sırası
değildir. Tam proje hedefi ve açık kapılar tek kapanış kaydında korunur.

| Kabul | Güncel kanıt / kalan |
|---|---|
| Son kaynak CPU regresyonu | `data/capability-check-kahp3qhu/report.json`: 1941 geçti, 276 atlandı, 0 hata/başarısızlık; runner 173,831 s. `partial`, çünkü atlanan GPU/GUI/gerçek görev testleri geçilmiş sayılmaz. Önceki test toplamlarına eklenmez. |
| Mevcut kullanıcı uygulaması | Salt okunur HTTP kontrolü: `/ui/` 200; `/api/session` `local_auto_login: true`. Yeni kodun deployment veya model/GPU kabulü değildir; instance durdurulmadı/yeniden başlatılmadı. |
| AOS–Scientist kaynak/control-plane | Worker/caller/CLI/Lab onay-intent/readback ve cleanup CPU/mock testli. Ortak capability/principal/reconciliation/drain/release teyidi yok; standalone Scientist startup fail-closed. |
| Ortak gerçek GPU kabulü | Çalıştırılmadı. Scientist tek yürütücü; scheduler reservation, boş/uygun tahsis ve cleanup kanıtı olmadan başlatılmaz. AOS idle veya stop ACK release değildir. |
| Genel web hedefi ve öğrenme | Selected owned skill ve özel bilgi dilimleri var. İki farklı uygulamalı genel free-text→S1→oracle, held-out relevance, portable skill ve S2 eğitim/promotion kabulü açık; yeni dar preview paneli bunları kapatmaz. |
| Genel teslim / şirket fork'u | Source-only paket ve continuation belgeleri var; temiz makine kurulum/başlatma ve gerçek Mac bağlantı kabulü açık. SWAPP intranet/oracle en son, 0/6 gerçek-site kabulü. Lisans seçimi runtime işinden ayrı kalır. |

Karşı oturuma aktarılacak kısa runtime beklentisi:
[Scientist handoff](SCIENTIST_HANDOFF.md). Güncel altı aşamalı kapanış:
[RELEASE_ACCEPTANCE](RELEASE_ACCEPTANCE.md). Mock başarısı veya UI HTTP200 gerçek GPU,
model kalitesi, eğitim sonucu veya kaynakların public olarak yayımlandığı anlamına gelmez.

**Knowledge / Bilgi:** belge metni veya seçtiğiniz UTF-8 dosyası için kapsam → ayrı yayın izni → ayrı inceleme → kaynak/chunk atıflı arama akışı vardır. Bu deterministik lexical retrieval'dır; henüz canlı modele RAG bağlamı eklemez veya eğitim başlatmaz. [Adımlar ve sınırlar](DOCUMENT_KNOWLEDGE.md).

30 Eylül tarihsel kontrollü idle restart sonrasında Knowledge/guidance/reuse etkinleştirildi. Bu geçmiş deployment kaydıdır; sonraki kaynak değişikliklerinin canlıya taşındığını kanıtlamaz. Standart managed oturumda sayfayı yenilemek otomatik yerel girişe yeterlidir; yalnız token isteyen deneysel/raw modda `./scripts/aos-v1 token` kullanılır. Development varsayılan açılıştır; port8765 deployment kabulü Mac tünelinin açıldığını kanıtlamaz.

Knowledge bölüm 4 ayrı exact izinle incelenmiş kaynakları yerel Bonsai'ye sorabilir; yanıt doğrulanmış alıntılardır, serbest chatbot cevabı veya otomatik görev değildir. Bu adapter'ın Python backend aktivasyonu ayrıca kontrollü idle restart ister. [Kullanım ve kanıt sınırı](DOCUMENT_KNOWLEDGE_ANSWERS.md).

30 Eylül tarihsel kontrollü geçişte model-answer capability'si gerçek managed backend'de açıldı; readonly `idle` ve EN/TR mobil panel doğrulandı. Yeni kaynak dilimlerinin deployment kabulü değildir. Standart yerel giriş token istemez; özel token politikalı modlar ayrı kalır. Kullanıcı corpus'una otomatik belge eklenmedi veya model çağrısı yapılmadı.

Arayüz varsayılan olarak İngilizcedir. **English / Türkçe** düğmeleriyle dil değiştirilir ve tercih bu tarayıcıda hatırlanır. Aşağıdaki Türkçe düğme adları için **Türkçe** seçin; İngilizcede Görevler=Tasks, Onayla=Approve, Reddet=Reject. Dil değişimi görev veya onay göndermez; kullanıcı girdileri ve teknik kimlikler çevrilmez.

**Web applications → HTTPS form draft:** kayıtlı profilden tek veya 2–8 sıralı alanlı form görevi ve exact planı arayüzde ağsız önizleyip ayrı hash'lerle özel dosyalara kaydedebilirsiniz. İsteğe bağlı altı-onaylı önce/sonra durum GET modu görev kaydından önce seçilir; form planından sonra ayrı state plan/hash onayı gerekir. Public hedef için manager komutları yalnız gösterilir; kullanıcı güvenli yeni oturumu ayrıca başlatır. Parola/token/müşteri verisi girmeyin. [Dar kullanım ve sınır](WEB_FORM_ONBOARDING.md).

Bu sürümün hedefi yeni özellik sayısı değil, **başlat → giriş → onaylı görev → sonucu gör → güvenle kapat** akışıdır. CachyOS'taki mevcut hazırlanmış checkout, Docker image, modeller ve bağımlılıklar kullanılır. Genel bilgisayar ajanı, üretim servisi veya başka bilgisayara kurulabilir installer değildir. Eğitim, adapter activation ve production promotion kapalıdır.

## Bir dakikalık başlangıç

İleri sentetik öğrenme denemesi için `./scripts/aos-v1 --help` artık `--owned-synthetic-form-invocation` girişini ve yeni oturumda reuse için gereken üç hash seçeneğini açıklar. Normal pilotu denemek için bu mod gerekli değildir. Owned oturumda seçilmiş skill hazır olduğunda **Prepare new-session reuse**, exact sürüm/seçim komutlarını gösterir; kaydedip yalnız o kaynak oturumu güvenle kapattıktan sonra terminalde ayrıca çalıştırın. Arayüz servisi kapatmaz veya yeniden başlatmaz. [Adımlar](OWNED_SKILL_REUSE.md).

Owned-v1 gelişim oturumunda incelenmiş adaylar için Tasks → **Load version catalog** → **Preview version** → ayrı exact onayla **Publish version** kullanılır. Yayın seçim değildir: ayrıca sürümü önizleyip onaylayın, ardından **Bind selected version to a new run** ile yeni değerli koşuya bağlayın. Önceki başarılı sürüme dönüş **Preview rollback to this version** ve ayrı onay ister; altı manuel eylem onayı korunur. Mevcut düz canlı oturumda bu mod yoktur ve otomatik yükseltme yapılmaz. [Tam akış ve sınırlar](OWNED_SKILL_RELEASES.md).

Desteklenen görevlerde **Approve all — this task only** başlangıçta seçilidir. Görev türünü seçip Start'a basarsanız yalnız o görev için otomatik onay verilir; eylemleri elle onaylamak isterseniz önce işareti kaldırın. Kutunun seçili olması tek başına görev başlatmaz. Duraklatma/stop/kontrol devri sınırları ve bağımsız doğrulama korunur; sonraki görev için ayrı Start gerekir. [Kapsam ve ölçüm](TASK_AUTO_APPROVAL.md).

Yeni gerçek managed oturum Decider'ı aynı görev içindeki kararlar arasında yeniden kullanır; ilk yükleme hâlâ bekletebilir, sonraki form kararı belirgin hızlanır. Bonsai gözlemi ve insan onayları atlanmaz. Model görev/pause sonunda kapanır; kapsam ve ölçüm DECIDER_REUSE.md içindedir.

Yeni gerçek managed oturum açılışta Bonsai ağırlık/runtime pinlerini arka planda **CPU/disk üzerinden** doğrular; bu model sürecini, GPU çıkarımını veya görevi başlatmaz. Aynı oturumdaki çağrıda dosya envanteri ve metadata taze denetlenir, değişiklikte tam hash tekrarlanır. Hazırlık henüz bitmemişse ilk görsel görev bekler; ölçülmüş tekrar çağrısı ve güvenlik sınırı [BONSAI_RUNTIME](BONSAI_RUNTIME.md) ile [STATUS](STATUS.md) içindedir.

Repository dizininde:

```bash
./scripts/aos-v1 start
./scripts/aos-v1 open
```

Standart yerel managed UI token istemeden oturum açar; bu yalnız güvenilen tek-kullanıcılı loopback geliştirme içindir. API istemcileri ve opt-in owned/synthetic/remote deneyleri için `./scripts/aos-v1 token` korunur. Mac'te sunucu start, tünel ve tarayıcıyı birlikte açan scriptin kurulumu [pilot rehberinde](PILOT_QUICKSTART.md) yer alır. Otomatik giriş görev başlatmaz veya paused kontrolü resume etmez.

Yeni `start` varsayılan olarak **gerçek yerel Decider + Bonsai**, dört sabit görev ve authenticated loopback UI başlatır. Dördüncü görev yalnız ağsız sentetik Start→Details gezinmesidir; yerel formun CDP taşıması değişmez. Hazırlanmış Playwright MCP paketi yoksa veya hash/sürümü değişmişse eksik önkoşulda indirme/fallback yerine açıkça durur. İlk kontrol model dosyalarının hash'lerini okuduğundan birkaç saniye alır. Başlatma herhangi bir model görevi yürütmez. Görev başlatıldığında gözlem ve model kararı üretilebilir; onay gerektiren eylem ise ancak ayrı onayınızdan sonra uygulanır. Zaten çalışan eski managed oturum yeniden başlatılmaz veya sıcak güncellenmez; dört görev yalnız kontrollü sonraki başlangıçta görünür.

Beşinci ağsız, sentetik App→Draft→Message→makbuz akışını **ayrı yeni managed oturumda** denemek için, mevcut oturumu önce `./scripts/aos-v1 status` ile inceleyip güvenli bir zamanda `./scripts/aos-v1 stop` ile tamamen kapattıktan sonra `./scripts/aos-v1 start --synthetic-staging` çalıştırın. `start --synthetic-staging` çalışan varsayılan oturumu sessizce değiştirmez veya yeniden başlatmaz; açık mod farklıysa reddeder. Bu seçenek hazırlanmış aynı pinned MCP paketini beşinci görev için de ekler, dört manuel onay ister ve gerçek site ağ/hesap/veri yetkisi açmaz. [Kapsam](SYNTHETIC_STAGING_WORKFLOW.md).

`--owned-synthetic-form-invocation` yeni, ayrı ve **deneysel** bir gerçek-Decider oturumu için W3 sabit-form denemesini ekler. Bu mod yalnız tek kullanımlık, kaynakları oturum içine hash-pin'lenmiş sentetik `.invalid` TLS fixture'ına bağlanır; public DNS, gerçek site, hesap veya kullanıcı verisine erişim açmaz. Tasks'ta aynı `browser_remote_form` görevi için fill/submit dahil altı ayrı insan onayı gerekir; başarılı görevden sonra yalnızca salt okunur `fixed_template_invocation_execution_verified` denetimi sunulur. Bu ifade sabit altı-aşamalı template'in izini doğrular, sembolik `skill.step_keys` yürütüldüğünü veya hedef site sonucunu değil. İzole real-Decider supervisor/backend/Tasks denemesi özel ephemeral UI listener üzerinde altı onay, denetim, yeniden kullanımın reddi, backend kapanışı ve fixture portunun kapanış doğrulamasıyla geçti. Üretim `start` port-8765 ayırma/yükseltme yolu yalnız birim testlidir ve bu uçtan uca test kapsamında değildir. Mevcut managed oturum sıcak yükseltilmez veya yeniden başlatılmaz. [Sınırlar ve doğrulama](SITE_SKILL_FORM_INVOCATION.md).

Sıralı sembolik adımları çalıştıran ayrı deney için, yalnız güvenli biçimde kapatılmış oturumdan sonra `./scripts/aos-v1 start --owned-synthetic-form-recipe` kullanın. Bu sürüm-2 modunun Tasks → HTTPS form paneli recipe hash'ini, altı adımı ve bekleyen onayın hangi adıma ait olduğunu gösterir. Açık opt-in/Start ve altı manuel onaydan sonra ayrı audit düğmesi `executable_recipe_executed=true` sonucunu denetler; `skill_validated=false` kalır. Kaynak değişirse eski doğrulanmış sonuç gizlenir. Tek kullanımlık sentetik fixture'dır; gerçek site, otomatik öğrenme veya training yetkisi değildir. V1 owned invocation modu ayrı kalır; çalışan oturum sıcak yükseltilmez. [Kullanım ve kabul sınırı](SITE_SKILL_FORM_RECIPE.md).

Owned-v1 başarılı görev ve ayrı invocation audit sonrasında Tasks, deneyimden **incelenmemiş skill/recipe adayı** önizleme → exact hash ile özel kayıt → kaynak yeniden inceleme akışını da sunar. Adları/alan eşlemelerini operatör beyan eder; kayıt yeni görev, model çağrısı, aktivasyon veya eğitim başlatmaz. Kaynak dizini ve DB korunursa supervisor kapalıyken yeni CLI sürecinde yeniden inceleme yapılabilir; bu kapalı oturumun Tasks kartını yeniden açma veya otomatik resume değildir. [Komutlar ve sınırlar](SITE_SKILL_FORM_RECIPE_CANDIDATE.md).

Kaydedilmiş aday için aynı Tasks panelinde ayrı gelişim yürütmesi bulunur: aday hash'i, yeni vaka anahtarı ve gizli olmayan yeni sentetik değer → ağsız önizleme → exact onay → altı manuel eylem onayı → ayrı kaynak-bağlı audit. Özgün gösterim değişmez; aynı kaynak grubundan yeni koşular bağımsız held-out sayılmaz. Oturum başına en fazla dört aday başlangıcı vardır; tüketilmiş önizleme tekrar başlatılamaz. Çalışan eski backend bu özelliği kendiliğinden edinmez. [Kapsam](OWNED_CANDIDATE_EXECUTION.md), ölçülmüş kabul [STATUS](STATUS.md) içindedir.

Denetlenmiş gelişim koşusundan sonra **Review the latest audited execution** ile semantik eşleme/adım/kanıt özetini inceleyin; ayrı kutu ve **Record review** exact kaydı saklar. Yeni koşuda **Bind this run to the selected review; stop if revoked** ayrıca seçilmelidir; kabul otomatik görev başlatmaz. **Revoke review** ayrı onayla bağlı bekleyen görevi durdurur, geçmiş başarıyı silmez. Yenileme sonrasında receipt hash'iyle **Reinspect review receipt** kullanılabilir. Sentetik review skill'i active/validated yapmaz ve eğitim açmaz. [İnceleme sözleşmesi](OWNED_CANDIDATE_REVIEW.md).

Yeni bir oturumda ayrıca `./scripts/aos-v1 start --synthetic-learning` seçilirse Görevler ekranında varsayılan kapalı görev-başına sentetik S1/S2 metadata kutusu açılır. `--synthetic-staging` ve `--synthetic-learning` birlikte kullanılabilir; özel `--owned-synthetic-form-invocation` ve `--owned-synthetic-form-recipe` modları birbirleriyle veya bu seçeneklerle birleştirilemez. Bu seçenek özel oturum outbox'ına yalnız gerçek pinned model çağrısı olduğunda metadata yazar; fixture çağrısı uydurmaz, gerçek site verisi veya eğitim izni sağlamaz. [Sınırlar](LEARNING_EVENT_STREAM.md).

Kayıtlı uzak staging/production profili için yalnız **tek, okuma amaçlı HTTPS giriş denemesi** ayrıca yeni gerçek-model oturumunda sunulabilir. Önce profilin exact SHA-256 kimliğini ve `data/` altındaki owner-only `0600` özel web görev JSON'unu hazırlayın. Çalışan oturuma dokunmadan `./scripts/aos-v1 preview-remote-entry --remote-entry-profile-sha256 <profil-sha256> --remote-entry-task-file data/<özel-görev>.json` ile aynı profil/görev kapsamını ve canonical görev hash'ini doğrulayın; bu komut siteye istek yapmaz, oturum başlatmaz ve yetki vermez. Çalışan oturumu inceleyip güvenle kapattıktan sonra `./scripts/aos-v1 start --remote-entry-profile-sha256 <profil-sha256> --remote-entry-task-file data/<özel-görev>.json` çalıştırın. Manager görev içeriğini yeni özel oturuma canonical kopyalar ve hash'e bağlar; yanlış, eksik veya fixture-mode seçenekleri reddeder. Başlatma siteye istek yapmaz. UI'de ayrı manuel onay verilirse yalnız kayıtlı giriş URL'sine tek GET yapılır; giriş yapma, form, başka rota, alt kaynak, approve-all ve gerçek-site öğrenme kaydı kapalıdır. Bu opt-in görev henüz gerçek hedef web uygulamasında sınanmadı ve mevcut 8765 oturumunu sıcak yükseltmez. [Sınırlar ve test](WEB_HTTPS_RELAY.md).

İki–sekiz **önceden listelenmiş HTTPS rota** için aynı kayıtlı profil/görev yanında URL'leri sırasıyla, her satırda bir canonical URL ve son satırda newline olacak şekilde owner-only `0600` `data/private-remote-routes/routes.txt` dosyasına yazın; ilk rota profil giriş URL'si, kalanlar exact ve aynı origin olmalıdır. Önce `mkdir -m 700 data/private-remote-routes` çalıştırın. Ağ veya oturum açmadan `./scripts/aos-v1 plan-remote-routes --remote-entry-profile-sha256 <profil-sha256> --remote-entry-task-file data/<özel-görev>.json --remote-routes-source-file data/private-remote-routes/routes.txt --remote-routes-plan-file data/private-remote-routes/plan.json` ile yeni `0600` canonical planı oluşturun; komut mevcut planı ezmez ve URL'leri incelemeniz için gösterir. Canlı oturumu değiştirmeden `./scripts/aos-v1 preview-remote-routes --remote-entry-profile-sha256 <profil-sha256> --remote-entry-task-file data/<özel-görev>.json --remote-routes-plan-file data/private-remote-routes/plan.json` ile planı doğrulayın. Güvenli yeni gerçek-model oturumunda `./scripts/aos-v1 start --remote-entry-profile-sha256 <profil-sha256> --remote-entry-task-file data/<özel-görev>.json --remote-routes-plan-file data/private-remote-routes/plan.json` kullanın; manager planı özel oturuma hash-bağlı kopyalar. Görevler ekranındaki **Sıralı HTTPS okuma** ancak bu yeni oturumda görünür; her GET ayrı manuel onay ister, bir sonraki rota öncekinin içeriksiz readback'i doğrulanmadan açılmaz. Aynı oturumda iki tamamlanmış rota görevi varsa Görevler'deki **Gözlenen gezinme grafiği** bölümünden bu iki run'ı seçip yalnız tarihsel sabit rota/planlı bağlantı indekslerini görebilirsiniz; karşılaştırma GET yapmaz ve gerçek site haritası değildir. Login, form, yeni rota keşfi, öğrenme ve uygulama sonucu doğrulaması yoktur. GET'in yan etkisiz olduğu varsayılmaz; dış site/hesap/veri yetkisi ayrıca gerekir ve mevcut çalışan oturum sıcak yükseltilmez. [Kapsam](WEB_READONLY_ROUTES.md), [grafik sınırı](REMOTE_NAVIGATION_GRAPH.md).

Konsoldaki **Web applications → Private remote-entry task draft** formu, kayıtlı staging/production profili, profildeki görev anahtarı ve gizli olmayan doğrulayıcı referansıyla 1–8 sayfalık görev dosyasını elle JSON hazırlamadan üretebilir. Önizleme dosya/ağ işlemi yapmaz; gösterilen exact görev SHA-256 tekrar girilip kaydedilince owner-only `data/web-task-drafts/<görev-sha256>.json` yolu gösterilir. Tek girişte arayüz, exact profil/görev piniyle `preview-remote-entry` ve sonraki `start` komutlarını gösterir. 2–8 sayfa seçtiyseniz formdaki **Ordered HTTPS route plan** alanına ilk satırı exact profil girişi olan aynı-origin canonical URL'leri sırasıyla girin; ayrı plan hash'ini onaylayınca `data/web-route-drafts/<plan-sha256>.json` üretilir ve arayüz exact profil/görev/plan pinleriyle `preview-remote-routes` ve `start` komutlarını gösterir. Her iki durumda da önce ağsız manager preview'u çalıştırın; mevcut oturumu inceleyip güvenle durdurmadan `start` kullanmayın. Gösterilen komutlar otomatik yürütülmez. Taslaklar çalışan oturumu değiştirmez, gerçek site/hesap/veri hakkı veya görev yetkisi sağlamaz; her GET için ayrı taze onay gerekir.

**Özel sayfa taslağı:** aynı yeni oturumda iki başarılı rota göreviyle oluşan grafikte sabit bir indeks seçin, yalnız sizin iddia ettiğiniz sembolik sayfa anahtarını girin ve **Özel sayfa taslağı kaydet** düğmesini kullanın. Sunucu kanıtı yeniden denetleyip exact URL/parmak izini `data/site-page-seeds/<profil-SHA>/<sayfa-anahtarı>.json` altında `0600` özel dosyaya yazar; URL arayüze çıkmaz. Dosyayı yerel makinede inceleyin. Ayrı **Taslak SHA-256 onayı** alanına gösterilen exact hash'i girip **Onaylanan taslağı kaydet** ile yeniden denetlenen dosyayı değişmez site-bilgisi deposuna yalnız *incelenmemiş taslak* olarak kaydedebilirsiniz. Bu semantik doğrulama, görev-içi kullanma, eğitim veya gerçek hesap/sonuç kanıtı değildir. Yalnız sonraki backend sürümünde kullanılabilir; çalışan oturumu sıcak yükseltmez. [Ayrıntı](REMOTE_PAGE_DRAFT_SEED.md).

**Metadata incelemesi:** kayıtlı taslaktan **Metadata adayını önizle** seçin; sayfa anahtarı/sürümü/rota indeksini inceleyin, ayrı **Aday SHA-256 onayı** alanına exact hash'i girin ve **Yalnız metadata incelemesi olduğunu kabul ediyorum** kutusunu işaretleyin. **Metadata incelemesini kaydet** kaynağı yeniden denetleyip özel değişmez review hash'i verir. Bu inceleme gerçek kimlik/hesap, semantik anlam, uygulama sonucu veya veri hakkı kanıtı değildir; çalışan oturumda bilgi enjeksiyonunu etkinleştirmez. Dar görev-içi metadata kullanımı için sonraki oturumda exact dört review/source pini ayrıca verilmelidir. [Kapsam](REMOTE_ROUTE_KNOWLEDGE_REVIEW.md).

**Tek alanlı public HTTPS formu — yalnız açık yetkili hedefte:** kayıtlı staging/production profil ve aynı özel görev dosyasına ek olarak `data/` altında owner-only `0600` UTF-8 tek satırlık değer dosyası hazırlayın. Plan için önce `mkdir -m 700 data/private-remote-form` çalıştırın; `data/` kökü özel dizin değildir ve plan çıktısı doğrudan oraya yazılamaz. Form değeri dosyasına şifre/çerez koymayın; ayrı statik Cookie seçeneği aşağıdadır. Yeni planı ağ açmadan ve değeri ekrana basmadan oluşturun:

```bash
./scripts/aos-v1 plan-remote-form \
  --remote-entry-profile-sha256 <profil-sha256> \
  --remote-entry-task-file data/<özel-görev>.json \
  --remote-form-plan-file data/private-remote-form/<yeni-form-planı>.json \
  --remote-form-field-name message \
  --remote-form-value-file data/<özel-değer>.txt \
  --remote-form-submit-url https://<yetkili-host>/submit \
  --remote-form-receipt-url https://<yetkili-host>/receipt
```

Çıktıdaki `plan_sha256` yalnız **bu exact plan** içindir. Aynı profil/görev/plan/alan/değer dosyalarıyla `./scripts/aos-v1 preview-remote-form` çalıştırın; yukarıdaki iki URL seçeneği yerine `--remote-form-public-plan-sha256 <plan_sha256>` ekleyin. Preview hedefe bağlanmaz, oturumu başlatmaz veya veri hakkı vermez. Yetkili kullanıcı çalışan oturumu güvenle kapattıktan sonra **aynı seçeneklerle** `./scripts/aos-v1 start` çalıştırabilir. Manager planı ve ham değeri yalnız özel `0600` oturum dosyalarına kopyalar; değer argv, status, onay ve DB'ye yazılmaz. Manager backend bittikten sonra kopyalanan değer dosyasını kaldırır; önceki boot'tan güvenli recovery de aynı dosyayı kaldırır. Bu güvenli silme garantisi değildir ve özgün değer dosyası kullanıcı tarafından yönetilir. Yeni backend **HTTPS formu** görevini gösterir; giriş GET, doldurma, tek POST ve makbuz GET ayrı manuel onay ister; toplu onay/otomatik retry yoktur. Public hedef sistem CA'sıyla TLS doğrulanmalıdır. Başlatma ve preview siteye istek yapmaz; onaylı görev yapabilir. Makbuz yalnız taşıma doğrulamasıdır, uygulama kaydının bağımsız sonucu değildir. Mevcut 8765 oturumu sessiz yükseltilmez. [Ayrıntılı sınır](WEB_HTTPS_FORM_TRANSPORT.md).

**Sıralı iki–sekiz form alanı:** özel `0700` `data/` alt dizininde owner-only `0600` bir UTF-8 JSON **dizisi** hazırlayın: her öğe `{name,value}` nesnesi, sırası gerçek formun DOM sırası ve her değer printable tek satırlık metin olsun. Görünür form kontrolleri yalnız gerekli `input[type=text]`, `input[type=email]`, `textarea` veya tek seçimli `select` olabilir; e-posta değeri tarayıcı geçerlilik denetiminden geçmeli, seçim değeri tek, etkin option'ın exact `value` değeri olmalıdır. Çoklu, eksik, pasif veya tekrar değerli seçenek reddedilir. Önceden exact değeri bilinen ve plan alan listesinde ilan edilen tekil gizli `input` yalnız formun içinde, aynı DOM sırasında ve aynı değerle kabul edilir; ilan edilmeyen veya `form=` ile dışarıdan bağlı kontrol reddedilir. Tek etkin gönderici açık/varsayılan `<button>` ya da `<input type=submit>` olabilir; formun içinde ve adsız olmalı, form/submit yönlendirme override'ı taşımamalıdır. Parola alanı ve dinamik CSRF token alma/yenileme yoktur. Yalnız sentetik biçim örneği: `[{"name":"subject","value":"demo"},{"name":"message","value":"hello"}]`. Kaynak alan değerlerini ekrana basmadan canonical plan girdisini ağsız üretin:

```bash
./scripts/aos-v1 plan-remote-form-fields \
  --remote-form-fields-source-file data/private-remote-form/<özel-alan-listesi>.json \
  --remote-form-fields-file data/private-remote-form/<yeni-özel-alanlar>.json
```

Çıktı yalnız alan adları, `fields_sha256`, body SHA-256/byte sayısı ve yetkisiz taslak durumunu taşır; mevcut hedef dosyayı ezmez, kaynak dosyayı silmez. Üretilen owner-only canonical dosya `schemas/web_https_form_fields.schema.json` biçimindeki **yalnız `configuration` nesnesidir**; `examples/web_https_form_fields.json` açıkça sentetiktir. Sonraki `plan-remote-form`, `preview-remote-form`, `plan/preview-remote-form-state`, `plan/preview-remote-form-cookie` ve güvenli **yeni** `start` çağrılarında eski `--remote-form-field-name`/`--remote-form-value-file` çiftinin yerine yalnız `--remote-form-fields-file` kullanın. Plan/preview ağsızdır; start özel dosyanın SHA-256 pinli kopyasını supervisor/backend'de yeniden denetler ve kapanış/izinli recovery sonunda kaldırır. Alan değerleri argv, onay, status, UI veya trajectory DB'ye çıkmaz; alan adları ve hash'ler görünür. Bu sınır login, CSRF, gerçek hesap, veri hakkı veya bağımsız uygulama sonucu doğrulamaz.

Opsiyonel HTML durum geçişi gerekiyorsa aynı exact public form seçeneklerine `--remote-form-public-plan-sha256 <form_plan_sha256>` ekleyerek `./scripts/aos-v1 plan-remote-form-state --remote-form-state-plan-file data/<özel-durum-planı>.json --remote-form-state-url https://<yetkili-host>/state --remote-form-state-before-sha256 <önce-html-sha256> --remote-form-state-after-sha256 <sonra-html-sha256>` çalıştırın. Sonra aynı profil/görev/form/alan/değer/form-grant seçenekleri, üretilen `--remote-form-state-plan-file` ve ayrı `--remote-form-public-state-plan-sha256 <state_plan_sha256>` ile `preview-remote-form-state` veya güvenli kapatma ardından yeni `start` kullanın; URL/önce/sonra seçenekleri yalnız planlama içindir. İki ek host GET için ayrı manuel onay gerekir; toplam altı eylem olur. Bu yalnız ilan edilen HTML yanıt değişimini gözler; hesap, veri hakkı ve bağımsız uygulama sonucu kanıtı değildir. Plan/preview ağsızdır, mevcut oturum değişmez. [Ayrıntılı sınır](WEB_HTTPS_FORM_TRANSPORT.md).

Yetkili hedef statik Cookie gerektiriyorsa `data/` altındaki owner-only `0600` dosyaya yalnız `name=value; other=value` biçimindeki **Cookie başlık değerini** son satır sonu olmadan koyun. Aynı exact profil/görev/form-plan/alan/değer seçenekleri ve `--remote-form-public-plan-sha256 <form_plan_sha256>` ile `./scripts/aos-v1 plan-remote-form-cookie --remote-form-cookie-file data/<özel-cookie>.txt` çalıştırın; yalnız hash döner. `preview-remote-form-cookie` ve güvenli yeni `start` için aynı seçeneklere `--remote-form-cookie-sha256 <çıkan-hash>` ekleyin. Plan/preview hedefe bağlanmaz; start özel kopyayı pinler ve yalnız ayrı onaylı exact aynı-origin form/durum istekleri bu statik Cookie'yi kullanır. Çerez değeri argv, UI, onay veya DB'ye yazılmaz; UI yalnız hash'i gösterir. Login, Set-Cookie yenileme, CSRF yönetimi, hesap/tenant doğrulaması ve bağımsız uygulama sonucu bu seçenekle tamamlanmaz. Mevcut canlı oturum yükseltilmez. [Ayrıntılı sınır](WEB_HTTPS_FORM_TRANSPORT.md).

Tarayıcı adresi **http://127.0.0.1:8765/ui/**. Standart managed oturum otomatik yerel giriş yapar ve Development açılır. Yalnız token isteyen deneysel/raw modda `token` çıktısını **Yerel oturum anahtarı** alanına yapıştırın. Token URL'ye eklenmez, kaynak veya localStorage'a yazılmaz; tam durdurmada silinir. Token'ı paylaşmayın veya issue/log dosyasına kopyalamayın. `open` yalnız varsayılan tarayıcıyı açar; kendi başına giriş veya görev onayı göndermez.

Kayıtlı staging/production profili için **Web uygulamaları → Tek HTTPS giriş kontrolü** bölümünde profili seçip saklanan exact giriş URL'sini ve profil SHA-256 değerini yeniden girin. Yetkili olduğunuz bu hedefe tek GET yapılabileceğini ve GET'in yan etkili olabileceğini kabul eden kutuyu işaretlemeden düğme açılmaz. Bu eylem tarayıcıyı sürmez veya görev başlatmaz; başarısız ağ denemesi de aynı sunucu sürecindeki tek deneme hakkını tüketebilir. Yalnız içeriksiz TLS/HTML raporu döner; gerçek uygulama sonucu veya hesap doğrulaması değildir. [Sınırlar](WEB_HTTPS_PREFLIGHT.md).

Arka planda açık kalan yerel yönetici backend'in sahibidir; terminali kapatmak backend'i kapatmaz. Makine açılışında otomatik başlatma/systemd servisi kurulmaz. Aynı moddaki ikinci `start` mevcut çalışan oturumu gösterir; başka mod veya bağımsız port sahibi üzerine geçmez.

## macOS üzerinden SSH erişimi

Hazırlanmış Linux sunucuda uygulama `127.0.0.1:8765` üzerinde çalışır. `AOS_HOST` yerine kendi yetkili SSH sunucu takma adınızı, `/path/to/aos` yerine sunucudaki checkout yolunu yazın. Aşağıdaki komutlar **Mac terminalinde** çalıştırılır. Önce durumu kontrol edin; uygulama zaten çalışıyorsa yeniden başlatmayın:

```bash
ssh AOS_HOST 'cd /path/to/aos && ./scripts/aos-v1 status'
```

Yalnız çalışmıyorsa aynı SSH komutunda `status` yerine `start` kullanın. Ayrı terminalde tüneli açın:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 AOS_HOST
```

CachyOS kullanıcısının parolasını girdikten sonra terminalin sessiz beklemesi normaldir; tünel terminalini açık bırakın. Mac tarayıcısında tam olarak **http://127.0.0.1:8765/ui/** adresini açın. **Mac 8765 → CachyOS 8765** eşleşmesi gereklidir: farklı yerel port veya `localhost`, backend'in exact Host/Origin kontrolünde reddedilir. `Invalid host` hatasını gidermek için bu güvenlik kontrolünü kapatmayın. `Address already in use` varsa önce mevcut tüneli ve aynı adresi kontrol edin; bilinmeyen port sahibini sonlandırmayın.

Standart managed oturum token istemez. Yalnız token isteyen deneysel/raw modda, tüneli kapatmadan ikinci bir Mac terminalinde alın:

```bash
ssh AOS_HOST 'cd /path/to/aos && ./scripts/aos-v1 token'
```

Token isteyen modda anahtarı yalnız konsolun giriş alanına yapıştırın; sohbet, kaynak dosyası veya ekran görüntüsünde paylaşmayın. Giriş sonrası varsayılan Development ekranı açılır. Tüneli kapatmak için yalnız tünel terminalinde `Ctrl+C` kullanın; bu işlem CachyOS'taki uygulamayı kapatmaz. Tek komutlu önerilen bağlantı için [pilot rehberindeki Mac launcher](PILOT_QUICKSTART.md) kullanılır.

## İlk deneme sırası

1. **Bilgisayar** ekranında “v0.1 yerel pilot” ve “Gerçek yerel Decider” görünmeli. Aynı noVNC ekranında canlı imleç koordinatı ve konum işaretçisi görünür; bu yalnız owned X11 okumasıdır, tıklama veya görev sonucu kanıtı değildir. SENTETİK TEST MOTORU yazıyorsa gerçek-model denemesi yapmıyorsunuz.
2. **İlk göreve git** → Hello dosyası → görevi başlat. Gelen eylemi okuyup **Onayla**; işlem ve bağımsız readback bitene kadar bekleyin. **İzi aç** üzerinden succeeded/passed kaydını inceleyin. Modelin cevabı tek başına başarı değildir.
3. Yerel browser formunu deneyin: yeni managed oturumda **Görevler** ekranındaki aynı noVNC masaüstünde Chromium açılır. Fill ve submit için **iki ayrı onay** gerekir; alanın dolmasını ve receipt sonucunu canlı izleyin. Başarılı pencere sonraki görev/kontrol devrine kadar açık kalır. Eski veya `--desktop-browser` verilmemiş backend headless kalır; ekran açıklaması gerçek modu belirtir. [Görünür form kapsamı](VISIBLE_BROWSER.md).
4. **Yerel iki sayfa gezintisi:** aynı görünür Chromium'da yalnız sentetik `/start` ve `/details` sayfalarını açar. Her geçiş için ayrı manuel onay gerekir; bu görevde Approve all kapalıdır. Details sonucu iki bağımsız doğrulamayla kaydedilir. Gerçek web sitesi veya genel gezinme değildir. [MCP ve ağ sınırı](DESKTOP_MCP_RUNTIME.md).
5. **Görsel SAVE → Vision görevi başlat:** yeni managed session'da aynı masaüstünde SAVE/CANCEL canvas'ı açılır. Bonsai gerçek rendered kırpımı yorumlar, Decider seçimi ardından tek capture-bound onay gelir. **Onayla** sonrası `Selected: SAVE` ve bağımsız outcome doğrulanır; pencere açık kalır. İlk GPU yüklemesi bekleme yaratabilir. Eski backend headless olabilir; UI modu açıkça belirtir. [Görünür SAVE sınırları](VISIBLE_VISION.md).
6. İsterseniz **Plan** bölümünde şu metni önizleyin: `önce hello görevini hazırla; sonra yerel form görevini hazırla; sonra görsel save görevini hazırla`. Ayrı **Bu birleşik sırayı başlat** düğmesi sırayı kabul eder; toplam dört eylem onayını Görevler bölümünde ayrı verin. Gezinti görevi bu üçlü sıra kataloğuna dahil değildir. Önizleme kendi başına yürütme değildir.
7. Bir görevde **Reddet** veya **Kontrolü al** deneyin: görev iptal olmalı, eski onay yeniden kullanılamamalı. Kontrolü aldıysanız yeni görevden önce **Ajana geri ver** düğmesine basın. Yeni görev için taze gözlem/onay gerekir.

Onaylar sürelidir; uzun bekletilirse görev başarısız olabilir. Bu güvenlik sınırını kapatmak yerine yeni görev başlatın. Aynı anda tek görev/sıra çalışır. Görev hata verirse üst üste başlatmak yerine İzi aç/Çalışmalar ekranındaki son durumu inceleyin.

## Kontrollerin anlamı

- **Duraklat / Devam et:** yalnız aynı canlı backend'deki mevcut işi korur; taze model kararı/onayı gerekir. Sıranın kalan görevleri Duraklat ile iptal olur.
- **Kontrolü al / Çıkış:** aktif görevi iptal eder; eski lease ve onaylar canlandırılmaz.
- UI **Durdur:** görev ve izole masaüstünü kapatır, backend'i kapatmaz. UI **Yeniden başlat** sonrasında durum PAUSED'dır; çalışmak için Devam et gerekir.
- **Tam kapatma:** aşağıdaki `stop` backend'e yalnız exact process identity/pidfd ile SIGTERM gönderir; owned runtime normal cleanup yapar. Başka uygulama, WinBoat veya model servisi durdurulmaz.

```bash
./scripts/aos-v1 status
./scripts/aos-v1 stop
```

Temiz stop ancak backend çıkışı, kaldırılmış lifecycle kayıtları ve token silinmesi doğrulanınca raporlanır. `stop` veritabanı veya workspace içeriğini silmez. Sonraki `start` **yeni session/workspace/DB** açar; eski oturumdan crash continuation yapılmaz.

**Yeniden başlatma:** düz yerel pilot oturumu için `./scripts/aos-v1 restart` kullanın. Çalışan oturum kapatılmadan önce yeni kodun aynı moddaki salt okunur doctor ön kontrolü ve mevcut backend'in authenticated idle AGENT durumu denetlenir; ardından yeni backend'de authenticated admission quiesce alınır. Kilit, yeni görev/sıra, kontrol değişimi ve masaüstü girdisini normal stop'a kadar reddeder. Eksik UI build/model pini, etkin görev, rezervasyon, onay, kontrol devri veya eski backend'de quiesce endpoint'i yoksa **eski oturum durdurulmaz**. Bu kilit yalnız backend API/scheduler kabulünü kapatır; harici process ve dosya sistemi kilidi değildir. Manager kesintisinden sonra oturum hâlâ çalışıyor ve kilit açık kalmışsa önce `status` ile oturumu inceleyin; yalnız güvenli olduğuna karar verince `./scripts/aos-v1 release-restart` kilidi explicit kaldırır. Belirsiz kapanışta otomatik kaldırma yapılmaz. Normal `stop` sonrası yeni `start` ön kontrolü tekrar yapar; dış kaynak değişirse yeni başlangıç yine fail-closed kalabilir. Çalışan oturumda exact manager kimliğiyle normal `stop` tamamlanmadan yeni oturum açılmaz. Önceki sistem açılışından kalmış oturumda komut, `recover-reboot` ile aynı journal/container/workspace/port denetimlerini yapar; yalnız bu denetimler geçerse yeni oturum açar. Eski oturum ve DB korunur, yeni token gerekir. `--fixture` veya opt-in sentetik/uzak görev ayarları `restart` ile verilmez; böyle bir oturumda aynı kapsamın sessizce kaybolmasını önlemek için komut reddeder. O durumda mevcut oturumu inceleyip güvenle kapatın ve exact seçeneklerle yeni `start` çalıştırın. Aynı açılıştaki belirsiz/başarısız kapanışı otomatik onarmaz. Tünel kullanıyorsanız tünel açık kalabilir; giriş anahtarını yeniden alın.

Manager `app-*` kimliği ile backend `desktop-session-*` kimliği farklıdır. Quiesce/release isteği önce authenticated state'ten okunan exact desktop kimliğini taşır; backend farklı veya eski kimliği **yan etki olmadan** reddeder. Development ekranı yeni backend'in kilit durumunu canlı gösterir; eski backend'de bu alan yoktur.

## Veri ve sınırlar

Her başlatma `data/local-app-v1/app-<kimlik>/` altında owner-only session dizini oluşturur: `workspace/`, `store.sqlite`, lifecycle journal, `backend.log`, `manager.log`. O anki session metadata'sı `data/local-app-v1/current.json`; token ayrı `runs/desktop-console-*.token` dosyasında 0600'dür. Bunlar ignored private dosyalardır, source pakete girmez. `status` güncel dizin ve token **yolunu** gösterir, token değerini göstermez. UI yalnız bu session'ın izlerini gösterir; önceki session'lar diskte korunur.

Host home/masaüstü veya Docker socket agent container'a açılmaz. Dört sabit görevin dışındaki path, içerik, web gezintisi ve shell komutları desteklenmez. Sınırsız Türkçe sohbet, Office iş planlama, sistem yönetimi, eğitim ve otomatik recovery bu ilk pilotun kapsamı değildir.

## Sorun giderme

**Görev düğmesi çalışmıyor / işlem ilerlemiyor:** üstteki durum açıklamasını izleyin. PAUSED için **Devam et**, HUMAN için **Ajana geri ver** gerekir. Durdurulmuş masaüstünde önce **Yeniden başlat**, sonra **Devam et** kullanın; yeniden başlatma tek başına görev başlatmaz. “Eylem onayınızı bekliyor” görünüyorsa **Onayı göster** ile Görevler bölümünün üstündeki ayrıntıları açın ve süre dolmadan **Onayla** düğmesine basın. Kontrol çubuğundaki **Yeniden başlat** görev düğmesi değildir; aktif görevi iptal eder. İptal edilen görev için **Hello görevi başlat** gibi ilgili görev düğmesine yeniden basmanız gerekir. Duraklatma veya onay engeli kendiliğinden kaldırılmaz.

```bash
./scripts/aos-v1 doctor
./scripts/aos-v1 status
```

- Doctor `ready=false`: ilgili kontrolün `action` açıklamasını izleyin; [önkoşul sözleşmesi](LOCAL_PREFLIGHT.md). UI build eskiyse `cd ui && pnpm build` çalıştırın. Doctor GPU/model görevi veya indirme çalıştırmaz.
- Port 8765 dolu: sahibi araştırılmadan başka süreci öldürmeyin. Yerel manager sahibi ise `status`/`stop`; doğrudan açılmış eski backend ise kendi terminalinden kapatın.
- Aynı boot'ta `failed`, ama backend gerçekten temiz kapanmışsa: `./scripts/aos-v1 recover-clean-exit`. Bu explicit komut yalnız düz yerel oturumda kayıtlı manager/backend süreçleri yok, token zaten kaldırılmış, exact workspace/lifecycle `removed`, Docker'da exact container yok ve port boş ise private `.clean-exit-<session>.json` audit'iyle state'i `stopped` yapar. Kill, token/container/DB silme, görev tekrarı veya otomatik start yapmaz; ardından ayrıca `./scripts/aos-v1 start` kullanın. Bilinmeyen Docker hatası, PID reuse, eski boot, belirsiz journal ve dolu portta reddeder. Mac scripti bu kurtarmayı otomatik çalıştırmaz.
- `failed`/`needs_inspection`: önce status'taki session dizininin loglarını ve lifecycle kayıtlarını inceleyin. Eski sistem açılışından kalmış manager/backend ve exact kayıtlı Docker container'ların durmuş olması doğrulanırsa `./scripts/aos-v1 recover-reboot` çalışır; eski oturum kaydını private `.recovery-<session>.json` içinde korur, eski token'ı kaldırır ve yeni `start` için kilidi açar. Bu komut eski görevi sürdürmez, container silmez veya belirsiz eylemi yeniden oynatmaz. Çalışan/eski kimliği eşleşmeyen container, aynı boot'taki process, bozuk journal, değişmiş workspace path/inode/owner veya dolu UI portunda reddeder. Yeniden açılışta workspace aygıt numarası değişebilir; path/inode/owner bağları yine aranır. `current.json` dosyasını silerek kilidi atlatmayın. Farklı kesinti durumları operatör incelemesi gerektirir.
- UI boş/eski: frontend build ve doctor kontrol edin, tarayıcıyı yenileyin. Backend'in çalışıp çalışmadığını `status` ile doğrulayın.
- GPU yetersiz: başka işin kaynaklarını inceleyin; launcher ilgisiz model servislerini kendiliğinden kapatmaz.

Fixture testi gerekirse önce gerçek oturumu tamamen kapatın, ardından `./scripts/aos-v1 start --fixture`. Bu açıkça sahte karar/gözlemci motorudur, gerçek Decider/Bonsai kabulü sayılmaz.

## Native pencere ve kabul

İlk kullanıcı denemesi için varsayılan tarayıcı yolu seçilmiştir. İsteğe bağlı mevcut native kabuk backend açıkken `cd ui && pnpm native:run` ile X11 uyumluluk profilinde açılır. Default Wayland hâlâ doğrulanmış değildir; bunu düzeltmek için host compositor ayarı değiştirilmez. Yeni rehberle birlikte mevcut native klavye/üç-görev akışı fixture ve gerçek modellerle yeniden geçti; tarayıcıdaki responsive rehber kabulü native'e genellenmez.

Kabul kayıtları [STATUS](STATUS.md) içindedir. Testler mevcut işlevleri doğrular; pilot dışındaki genel ürün hedeflerinin tamamlandığı anlamına gelmez. Kullanıcı denemesinde hata raporuna yalnız görev adı, beklenen/görülen durum ve private olmayan hata özeti ekleyin; token, ham trajectory veya ekran görüntüsünü otomatik paylaşmayın.
