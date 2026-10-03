# AOS kontrol merkezi

## Ortak açık / koyu tema

[umayos.org](https://umayos.org/) renk sistemi kullanılır: lacivert `#0b1f3a`,
turkuaz `#20b8be`, açık temada okunaklı vurgu `#087b80`; ortak panel, metin ve
çizgi tokenları `ui/src/theme.css` içindedir. Üst çubuktaki **Light / Dark**
(Türkçe: **Açık tema / Koyu tema**) seçimi tarayıcıda saklanır. İlk açılışta
işletim sistemi tercihi izlenir; açık seçim sistem tercihini geçersiz kılar.
Dil ve tema birbirinden bağımsızdır. Depolama engellenirse seçim o sayfada
çalışır, sonraki açılışta sistem tercihi kullanılır.

Development dahil bütün sekmeler aynı header, sidebar, içerik genişliği ve
renk tokenlarını paylaşır; sekmeye göre kabuk/tema değişmez. Scrollbar alanı
sabit ayrılır. Font ailesi Inter/system-ui'dır; arayüz harici font/CDN çağrısı
yapmaz, Inter yoksa sistem fontunu kullanır. AOS ürün adı korunur; pazarlama
sitesinin görselleri veya hero yerleşimi kontrol paneline kopyalanmaz.

## Mevcut teslim

Varsayılan açılış ekranı **Development / Geliştirme** içinde kontrol merkezidir.
Çalışan yerel oturum: `http://127.0.0.1:8765/ui/`. Yeni arayüzü görmek için
sayfayı yenileyin. **3 Ekim 2026, 16:31 UTC:** kullanıcının yalnız UI güncelleme
onayıyla yeni topoloji ve uyumlu Scientist panel assetleri bu hosta aktarıldı.
Backend yeniden başlatılmadı; mevcut Scientist bağlantısı yapılandırılmamış
olarak kalır. Yeni CPU backend kodu/grant deploy edilmedi. Host üzerinde gerçek
backend + Chromium EN/TR read-only kontrolü geçti; gerçek Mac erişimi ayrıdır.
Başka checkout'larda kaynak değişikliği otomatik canlı teslim değildir.

## Sistem ve topoloji

Yeni **System & topology / Sistem ve topoloji** paneli, uygulamanın nasıl
çalıştığını kaynakta açıklayan salt okunur bir mimari görünümüdür. Development
varsayılan açılış ekranı olarak kalır; yeni bölüm yan menüden seçilir.

- Kullanıcı hedefinden typed göreve, policy/onaya, durable intent'e, yürütmeye
  ve bağımsız sonuç doğrulamasına giden akışı gösterir.
- System-1 Decider'ın sonlu eylem seçimini ve gerektiğinde System-2 Bonsai'nin
  planlama/vision/recovery desteğini ayırır. Her adımda S2 çağrısı gerekmez.
- İzole Ubuntu masaüstü, Chromium/Playwright MCP ve yetkili web uygulaması
  sınırını açıklar; model çıktısı host veya site yetkisini genişletmez.
- AOS orkestrasyonu, ilk harici ajan Scientist ve gelecekteki ajanlar arasındaki
  görev/yetki sınırlarını gösterir. Paylaşılan GPU için tek tahsis otoritesi
  Scientist'tir; genel ortak GPU kabulü hâlâ ayrıdır.
- 16 GB VRAM hedefinde kontrollü ardışık kullanım ve gerçek kaynak bırakımını,
  ayrıca ayrı review/veri/eğitim/promotion aşamalarını açıklar.

Şema canlı process haritası veya health telemetrisi değildir. Oklar mevcut
bağlantı/çalışan worker kanıtı, mimari kartlar tamamlanma yüzdesi veya çalışma
izni sayılmaz. Uygulanmış kaynak ile kısmi/gelecek hedefler açıkça ayrılır.
Bu panel görev, API etkisi, model yükleme, eğitim veya servis başlatmaz.
Güncel runtime gözlemi için Development'ın canlı oturum bölümünü, tarihli kabul
için STATUS ve canonical release kaydını kullanın.

## Ekran nasıl okunur?

3 Ekim 18:20 UTC UI-only teslimi: kayıtlı `checkpoint.next_action` artık
**Next development step / Sıradaki geliştirme adımı** kartında görünür. Önceden
JSON'da güncellense de ekranda gösterilmiyordu. Son Scientist geçmiş/seçim kaynak
teslimi günlüğün ilk satırındadır; source/mock kabulü gerçek backend yeteneğiyle
karıştırılmaz. EN desktop/TR mobile gerçek backend okuması geçti; Mac tarayıcı
kabulü ayrı kalır. Backend restart veya GPU işi yapılmadı.

- **Canlı oturum:** son başarılı backend gözleminden görev etkinliği, mevcut iş,
  bekleyen onay ve masaüstü durumu. Oturum kimliği ve runtime kimliği ayrı
  gösterilir. Bu bölüm geliştirme işçilerinin etkinliğini veya GPU kullanımını
  ölçmez. Gözlem 15 saniyeden eskiyse, başarısızsa veya biçimi doğrulanamıyorsa
  eski veri boşta/sağlıklı kabul edilmez.
- **Genel durum:** tarihli geliştirme günlüğü; CPU ile doğrulanan işler,
  geliştirilmekte olan parça, sonraki adım ve kayıtlı engel. Her satırın kanıt
  başvuruları açılabilir. Tarihler ve kapsamlar korunur; bu canlı işçi akışı değil,
  UI derlemesine alınmış manuel kayıttır.
- **Sürüm kontrol listesi:** altı aşamada kaynak, doğrulama, teslim ve açık
  kapılar. İlk açık kapı, o anda çalışan geliştirme ajanı anlamına gelmez.
  Uydurulmuş toplam tamamlanma yüzdesi kullanılmaz.
- **Kanıt ve uygulama geçmişi:** önceki dar kabul kayıtları ve son gözlemlenen
  oturum sonuçları. Veri eskiyse burada da gözlem uyarısı görünür. Tarihsel model
  kanıtı güncel deployment veya genel ürün kabulü değildir.

**Durumu yenile** yalnız durum okur; görev, test, model veya eğitim başlatmaz.
Manuel geliştirme kaydını yeniden yazmaz. **Görevleri aç**, **Scientist Lab aç**
ve **Bilgisayarı aç** ilgili mevcut paneli açar; yürütme veya onay vermez.
Kalıcı kontrol çubuğu ve English / Türkçe seçimi korunur.

## Scientist CPU kapsamı

Ayrı ve açıkça yapılandırılmış CPU Scientist oturumunda panel **CPU only ·
synthetic mode-grid / Yalnız CPU · sentetik mode-grid** etiketiyle incelenmiş
deney/süre sınırlarını ve sıfır model token şartını gösterir. Yalnız `mode`
seçilebilir; bütçeyi yine kullanıcı girer. Yenilenen daha dar sınıra sığmayan
eski öneri devre dışı kalır. Geçersiz/bilinmeyen kapsam metadata'sında yeni
öneri kapalıdır; kapsam otomatik genişletilmez. Bu bilgi canlı yetki veya GPU
kabulü değildir; her işlem sunucuda yeniden denetlenir. Eski metadata'sız
backend'de genel protokol aralığı uyarısı korunur. Bu kaynak değişikliği mevcut
default oturuma CPU grant yüklemez veya kendiliğinden deploy olmaz.

## Kaynağı güncelleme

Tek manuel kaynak `docs/release_acceptance.json` ve canonical şema
`schemas/release_acceptance_snapshot.schema.json` dosyalarıdır. v1.1 `checkpoint`
alanı, tarihli iki dilli geliştirme günlüğünü ekler; v1.0 okunmaya devam eder.
`observed_at` altı aşamanın inceleme tarihidir; `checkpoint.recorded_at` ayrı
geliştirme gözlemidir. Yeni günlük girdisi, aşama bayraklarını veya runtime
yetkisini yükseltmez. Kanıtla desteklenen güncellemeden sonra UI yeniden derlenir.

```bash
cd ui && pnpm build
```

Bu yerel statik UI build'idir; backend, model, ortak GPU servisi veya deployment
aktivasyonu yapmaz. Gerçek mevcut servis gösterimi ayrıca kontrol edilmelidir.
Sınırları ve tarihli doğrulamaları [STATUS](STATUS.md) dosyasında tutun.

## Mac erişimi

### Hazırlanmış geliştirme makinesinde kolay bağlantı

3 Ekim tesliminde ayrı `aos-aserdargun-ui-bridge.service` kullanıcı servisi
hazırlandı. Yetkili Mac'in Tailscale adresi allowlist'tedir; yalnız AOS hostunun
Tailscale arayüzünde 8766 dinler ve mevcut loopback 8765 backend'ine bağlar.
Kullanıcıya özel adres teslim mesajındadır. Mac'te Tailscale açıkken bu adres
tıklanır; ayrı SSH tüneli veya token kopyalama gerekmez. Backend yeniden
başlatılmadı. Hostta backend HTTP200 ve yetkisiz kaynaktan bridge403 doğrulandı;
gerçek Mac tarayıcıdan pozitif readback henüz yapılmadı.

Bu çok kullanıcılı/public reverse proxy değildir: allowlist'teki Mac yerel
oturum güven sınırına alınır. Kaynak IP, exact Host/Origin, HTTP boyut/zaman
sınırları ve yalnız `/websockify` WebSocket yolu korunur. İnternete bind etmeyin,
peer filtresini kaldırmayın veya başka makineye aynı kişisel ayarları taşımayın.
Kaynak `scripts/aos_mac_bridge.py`; systemd unit kişisel host ayarıdır ve
kaynak arşivine dahil değildir. Yeni kurulum için explicit bind/peer seçimi ve
güven incelemesi gerekir.

Salt okunur yerel teşhis:

```sh
systemctl --user status aos-aserdargun-ui-bridge.service --no-pager
./scripts/aos-v1 status
```

### Başka kurulumlar için SSH alternatifi

Mac'in kendi terminalinde, SSH oturumunun içinde değil:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 AOS_USER@AOS_HOST
```

Ardından Mac tarayıcısında `http://127.0.0.1:8765/ui/` adresini açın.
Tünel terminali açık kalır. `localhost` veya farklı portun aynı Host/Origin
olduğu varsayılmaz. AOS hostundan yapılan kontrol, gerçek Mac bağlantısı kanıtı
değildir.
