# AOS kontrol merkezi

Varsayılan açılış ekranı **Development / Geliştirme** içinde kontrol merkezidir.
Çalışan yerel oturum: `http://127.0.0.1:8765/ui/`. Yeni arayüzü görmek için
yalnız daha önce teslim edilmiş assetleri sayfayı yenileyerek yükleyebilirsiniz;
kaynak değişikliği otomatik canlı teslim değildir. Aşağıdaki yeni topoloji bölümü
izole kaynakta hazırlanır; varsayılan oturuma deployment kanıtı STATUS'ta ayrıca
aranmalıdır. Backend veya çalışan kullanıcı görevi bu kaynak çalışması için
yeniden başlatılmaz.

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
