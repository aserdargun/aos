# Hızlı pilot: çalışan AOS’u Mac’ten kullanma

Bu kılavuz, Linux makinesinde hazırlanmış AOS'a bağlanır; sunucuyu kurmaz. Bu ilk pilot dosya ve görünür yerel tarayıcı görevlerini gösterir, genel bilgisayar ajanı ya da gerçek site ustalığı değildir. Yeni makine hazırlığı [kaynak teslimi](SOURCE_HANDOFF.md) içindedir.

## Mac'ten tek komutla bağlantı

**Mac'in yerel Terminal/iTerm penceresinde**, SSH oturumunun içinde değil, bir kez çalıştırın. `cachyos` erişebildiğiniz SSH takma adı olmalıdır; varsayılan sunucu checkout'u `$HOME/aos` konumundadır:

```sh
scp cachyos:aos/scripts/aos-connect-macos.sh ~/AOS.command && chmod +x ~/AOS.command && ~/AOS.command
```

Sonraki bağlantılarda `~/AOS.command` çalıştırın veya Finder'dan çift tıklayın. Script SSH bağlantısı kurar, yalnız `127.0.0.1:8765` portunu aynı uzak porta yönlendirir, uzak `aos-v1 start` ile mevcut oturumu kullanır veya yeni oturum başlatır, hazır olmasını bekler ve tarayıcıyı açar. Standart yerel oturumda token girilmez; Development açılır. SSH parolası hâlâ istenebilir. Terminal açık kalmalıdır; Ctrl-C yalnız scriptin kendi tünelini kapatır, AOS'u durdurmaz. Dolu portta, başarısız start'ta veya tokenless giriş kapalıysa tarayıcı açılmaz; script restart/orphan cleanup yapmaz.

Takma ad yerine `~/AOS.command user@host` kullanılabilir. Farklı güvenli mutlak checkout yolu için `AOS_REMOTE_DIR=/srv/aos ~/AOS.command user@host` kullanın. Önceden açık eski tüneli kendi terminalinde Ctrl-C ile kapatın; alternatif port kullanmayın.

### Ayrı adlandırılmış proje

Önce sunucuda [private UI hazırlama ve açıkça başlatma](ISOLATED_LEARNING_PROJECTS.md)
adımlarını tamamlayın (`--fixture` yalnız CPU demo içindir). Mac'teki güncel
script için aşağıdaki eşlenmiş kapsamı kullanın:

```sh
AOS_PROJECT=learning-demo AOS_PROJECT_PORT=18766 ~/AOS.command
```

Bu mod Mac'te kurulu `python3` gerektirir. Yerel18766 yalnız uzak18766'ya
yönlendirilir;8765 varsayılan oturuma bağlanılmaz. Script yeni UI hazırlamaz,
paket kurmaz, named projeyi başlatmaz veya bozuk/eski oturumu kurtarmaz. Uzak
salt okunur `status` sonucu ile tünelin
`/api/session` proje/port/app-session kimliği eşleşmeden tarayıcı açılmaz.
Bu kimlik bilgisi giriş veya görev yetkisi değildir.

Named proje otomatik giriş açmaz. Ayrı Mac Terminal penceresinde token alın:

```sh
ssh cachyos 'cd "$HOME"/aos && ./scripts/aos-v1 token --project learning-demo --project-port 18766'
```

Yalnız kendi projenizin giriş ekranına yapıştırın. Bağlantı scripti token okumaz,
kaydetmez veya panoya kopyalamaz. Ctrl-C yalnız kendi tünelini kapatır; proje
sunucusunu durdurmaz. Gerçek Mac/SSH kabulü ayrıca yapılmalıdır; Linux üzerindeki
mock script kontrolleri Mac donanımında çalıştırılmış sonuç değildir.

Yerel otomatik giriş, yalnız güvenilen tek-kullanıcılı geliştirme ortamı içindir: aynı makinedeki süreçler oturum açabilir. Exact Host/Origin, gerçek loopback peer ve HttpOnly/SameSite cookie denetimleri korunur; görev/yürütme izni veya Pause sonrası Resume verilmez. Raw `serve_desktop.py`, owned/synthetic-learning/remote deneysel managed modları token politikasını korur. Mevcut eski standart oturum için yalnız güvenli idle durumda sunucuda `./scripts/aos-v1 restart` gerekir. Bu kolaylık çok-kullanıcılı/public deployment kimlik doğrulaması değildir.

## Elle bağlantı (alternatif)

`AOS_HOST` yerine yetkili SSH adresini yazın:

1. Mac Terminal’de tüneli açın ve açık bırakın:

   ```sh
   ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 AOS_HOST
   ```

2. Mac tarayıcısında [http://127.0.0.1:8765/ui/](http://127.0.0.1:8765/ui/) adresini açın.
3. Standart yeni managed oturum otomatik açılır. Yalnız token isteyen deneysel/raw oturumda ayrı bir Mac Terminal penceresinde token alın:

   ```sh
   ssh AOS_HOST 'cd /path/to/aos && ./scripts/aos-v1 token'
   ```

4. Token isteyen modda token’ı giriş ekranına yapıştırıp **Sign in**’ı seçin. **Development** varsayılan ekrandır; **Tasks**’ı açın.
5. Görev listesinden **Hello file**’ı (Türkçe arayüzde **Hello dosyası**) seçin ve **Start Hello task**’a (Türkçe: **Hello görevi başlat**) basın. Gerekirse görünen eylemi onaylayın.
6. Görev başarılı olduğunda sonucu kontrol edin: `/workspace/hello.txt` içeriği `Hello from the local agent.` olmalı ve görev bunu bağımsız geri okumayla doğrulamalıdır. Farklı mevcut içerik üzerine yazılmaz.
7. Bilgisayar üzerinde hareket görmek için **Local browser form** → **Start browser task** seçin. Soldaki **Computer** panelinde Ubuntu Chromium alanı doldurup **Save locally** işlemini yapar. Sonuç satırında `succeeded`, **Open trace** içinde bağımsız doğrulamalar görünür. Bu yerel sentetik form CDP kullanır; Playwright MCP kullanan ayrı Start→Details gezinme göreviyle karıştırılmamalıdır.

**Approve all — this task only** varsayılan seçilidir; yalnız bastığınız Start için geçerlidir. Elle adım adım izlemek için kutuyu kaldırın. Hello bir dosya görevidir, fare hareketi göstermez.

27 Eylül canlı kontrolde gerçek Decider ile Hello **2,308 saniye**, tarayıcı formu **2,566 saniye** sürdü; her ikisinin bağımsız doğrulaması geçti. Bunlar mevcut oturumdaki tekrar ölçümleridir, soğuk başlangıç veya her çalıştırma için hız garantisi değildir. [Kanıt](STATUS.md).

Bu, yerel ve sabit kapsamlı bir pilot görevidir; gerçek web sitelerinde güvenilir genel kullanım veya tam öğrenme/ustalık kabulü anlamına gelmez.
