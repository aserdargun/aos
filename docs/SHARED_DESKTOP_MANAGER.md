# Shared desktop manager — kaynak/CPU adayı

**Durum: kısmi.** Bu çalışma mevcut native AOS kullanıcı oturumunu ortak GPU
runtime'ına dönüştürmez. Gerçek shared servis veya GPU kabul koşusu çalıştırılmadı.
Scientist, tek GPU tahsis otoritesi ve entegre GPU kabul koşusunun tek yürütücüsüdür.

## Uygulanan sınırlar

`aos.shared_desktop_plan` sürümlü template, inert plan ve ayrı activation
modellerini sağlar. `aos.shared_desktop_manager` mevcut `aos.local_app` komutlarına
açık opt-in yol ekler; v1 native davranışı korunur. `aos.shared_desktop_host`
somut, sabit systemd transport'u ve v2 manager durumunu bağlar.

- Tek servis adı `swapp-aos-gpu-shared-desktop-default.service`; acceptance
  servisi ödünç alınmaz. Native fallback ve keyfi komut çalıştırma yoktur.
- `prepare-shared`, exact predecessor ve seçili template hash'iyle yalnız yeni
  private plan dosyası üretir. Workspace, DB, token, socket veya servis oluşturmaz;
  canlı predecessor'ı değiştirmez. Predecessor yokluğu açık `none` seçimidir.
- `provision-shared`, aynı planı mevcut private manager base altında tüketir;
  yeni session ve boş workspace dizinlerini `0700`, sabit
  `shared-provision.json` makbuzunu `0600` oluşturur. Descriptor/inode/UID/path
  kimlikleri ve boot kaydedilir. Canlı predecessor aynı kalıcı kimlikle sürebilir;
  `current.json`, DB, token, socket veya servis oluşturulmaz/değiştirilmez.
  Makbuz yetki veya zaman penceresi üretmez. Eksik/kısmi/eski scope benimsenmez,
  silinmez veya tekrar oluşturulmaz; yeni plan gerekir.
- Shared start ayrı plan/activation dosyaları ve SHA-256 pinleri ister. Ayrıca
  `--shared-provision-sha256` zorunludur; makbuz yolu plandan türetilir ve hem
  activation hem Scientist factory `config_files` içinde aynı hash ile pinlenir.
  Workspace tekrar oluşturulmaz; hazırlanan exact inode ve boşluk doğrulanır.
  Canlı veya
  kapanışı kanıtlanmamış predecessor reddedilir. Plain native start, restart ve
  legacy recovery v2 state'i yeniden kullanamaz.
- Private bounded/nofollow okumalar, exact config/source pinleri ve açıkça
  listelenmiş Python symlink zinciri korunur. Hash tek başına yetki değildir.
- Host19 yazma yolunu sabit session kapsamından türetir: workspace, iki DB
  seçimi, web/knowledge depoları ve yazılabilir noVNC asset hedefi. Altı ek
  site/seed/review kökü entrypoint'ten console'a açık geçirilir. Page-seed
  helper'ı yalnız canonical private managed-session seed köklerini ayrıca
  kabul eder; keyfi nested data erişimi açılmaz. Child depolar runtime sırasında
  oluşturulur; provisioning boşluk kuralı değişmez.
- Template'te `broker_identity_sha256` zorunlu ama nullable alandır. `null`
  broker generation'ının gözlenmediğini belirtir. Non-null değer mevcut
  `ScientistServerGeneration` canonical digest'idir; config veya socket yolu
  hash'i değildir. Activation her durumda fresh authenticated broker/capability
  kanıtı ister; offline broker veya yalnız hash yetki vermez.
- Activation boot'a ve suspend süresini içeren `CLOCK_BOOTTIME` saatine bağlıdır;
  azami pencere 900 saniyedir. Süre bitince UI erişimi ile yeni görev kabulü ayrı
  raporlanır. Otomatik yenileme yapılmaz.
- Host gerçek MainPID/process identity, systemd invocation/cgroup, workspace
  inode, exact token provenance ve mevcut controller/lifecycle readback'lerini
  bağlar. Başlangıç ACK belirsizse kayıt `uncertain` kalır; otomatik tekrar veya
  orphan cleanup yapılmaz. `shared-launch-intent.json` exclusive oluşturulup
  fsync edilmeden başlangıç state'i veya servis başlatılmaz. State kaydı başarısız
  olsa dahi işaret kalır; yeni activation aynı scope'u yeniden çalıştıramaz.
- Stop, exact generation ve kapalı admission/GPU dışlama kanıtı ister. Yalnız
  sahip olunan pidfd işaretlenir; broker veya araştırma servisi durdurulmaz.
  Fiziksel cleanup kanıtlanmazsa release veya başarı ilan edilmez.

## Gerçek activation neden henüz hazır değil?

3 Ekim21:14UTC: [somut Scientist istemci adaptörü](SCIENTIST_SHARED_LAUNCH_ADAPTER.md)
host verify/claim bağlantısına hazırdır; lokal review şeması ve113 CPU testle
doğrulandı. Gerçek Unix frame/credential çapraz testi de geçti; yetki/systemd
denetimleri sentetikti. Üretim listener/authority, in-unit guard ve fiziksel
cleanup bileşimi henüz hazır değil; varsayılan kapalı kalır.

### 3 Ekim 2026, 20:51 UTC — kaynak yerleşimi ve ayrı claim

Güvenilir host bileşimi artık
`SystemdSharedDesktopTransport(scientist_root=reviewed_root)` ile incelenmiş
Scientist checkout'unu açık seçebilir. Bu seçenek CLI/model girdisi değildir;
verilmezse mevcut sibling varsayılanı korunur. Template launcher'ı yalnız bu
kökün `scripts/aos_native_launch.py` dosyası olabilir. Göreli/eksik/symlink
kökler, launcher yönlendirmesi ve ortam/systemd genişlemesine neden olacak
karakterler reddedilir. Aynı kök PYTHONPATH'e girer; **WorkingDirectory AOS
kökü olarak kalır**. Önceki çalışma dizinini Scientist'e taşıma önerisi bu
uygulama tarafından düzeltilmiştir; AOS entrypoint/import kapsamı korunur.

Tam source/config/Python pinleri ve varsayılan kapalı authority kapıları
değişmez. Kaynak yolu seçmek yetki değildir. Host, bilinmeyen/null broker
generation pinini intent/claim öncesinde reddeder; non-null pinin gerçek güncel
broker'a ait olduğunu halen authenticated verifier kanıtlamalıdır.
`activation_claimer` kalıcı intent/state sonrasında yalnız taze claim için
başarabilir; status/cleanup bu yetkiyi tüketmez. Callback'e derin kopyalar
verilir; iç map değişiklikleri doğrulanmış launch girdilerini değiştiremez.

101 CPU/sentetik test geçti. Gerçek producer/socket, current principal
verifier ve consumed-claim in-unit guard bileşimi henüz bağlı değildir.
Varsayılan startup, çalışan pilot ve GPU admission değişmedi.

3 Ekim 19:57 UTC [sonlu başlatma sözleşmesi önerisi](SCIENTIST_SHARED_LAUNCH_PROPOSAL.md)
eksik issuer/transport, tek-kullanım/revoke ve cleanup-only kararlarını mevcut
kaynaklara bağlar. Bu karşılıklı ACK veya uygulanmış authority producer değildir;
varsayılan ret kapısı değişmez. CPU paneli bu GPU anlaşmasından bağımsız ilerler.

1. Scientist MAIN launcher'ın shared-unit ve iki pre-main guard değişikliği
   3 Ekim'de actual disk readback ile doğrulandı: SHA256
   `c18efb89584946975f3740b071d2c2a3eca106ea44ae07b6a83060a267a03495`.
   AOS seçili import closure artık156 kaynaktır. Bu kaynak eşleşmesi yeni exact
   workspace/config incelemesi veya actual caller kabulü yerine geçmez.
2. Güvenilir güncel authority, native GPU dışlama, admission closure ve fiziksel
   cleanup sağlayıcıları varsayılan olarak reddeder. Sentetik test injection'ı
   CLI yetkisi veya üretim sağlayıcısı değildir.
3. Yeni `provision-shared` kaynak yolu, inode hazırlığını activation öncesine alır.
   Gerçek seçili nested workspace için peer'in yeni binder/config incelemesi
   yine gerekir; eski flat workspace incelemesi yeniden kullanılamaz.
4. Yeni opt-in dosyalar önceki API136/native141 kaynak ACK'ine otomatik dahil
   değildir. Fresh ortak kaynak/config ve actual caller generation kabulü gerekir.
5.19 yolun izolasyonu ve iki nested seed kökünün gerçek private yazımı CPU
   kabulünde kapatıldı. Değişen `serve_desktop.py` ve seed helper'ı yeni ortak
   kaynak incelemesine alınmalıdır; eski141 ACK korunmuş kabul edilemez.
   Scientist scheduler dalında ana DB `--database` kullanır; önceki global
   trajectory default'u gerçekleşmiş bir yetkisiz yazma kanıtı değildir.

Mevcut native kullanıcı oturumunun idle/quiesced olması GPU release değildir.
Onu kapatmak veya yeni shared servis açmak bu CPU adımının yan etkisi değildir.
Named `expected-session=none`, ayrı default native oturumun GPU'dan dışlandığını
kanıtlamaz. Pre-spawn callback'in doğruladığı sonlu **başlatma izni**, henüz
oluşmamış future caller PID/InvocationID istemez. Spawn sonrası peer launcher
actual caller/broker generation, workspace ve current runtime haklarını
`module.main` öncesi doğrular. Başlatma izni model/GPU admission değildir.

### Sonraki üretim bileşimi

Yeni [ortak drain/seal](SHARED_ADMISSION_DRAIN.md) bu sınırı kaynakta uygular;
40 odaklı CPU/ASGI kontrolü geçti, canlı servise dağıtılmadı. Eski restart quiesce yalnız desktop scheduler girişini kapatır; Scientist Lab
service yeni iş yolu aynı bayrağı kullanmaz. Bu nedenle tek başına
`SharedCleanupProof.admission_closed` kanıtı değildir. Yeni kaynak değişikliği,
aynı control lock altında exact generation'a bağlı ve belirsizlikte kapalı kalan
ortak drain'dir: scheduler ve Lab yeni başlangıç/onay girişleri kapanır; devam eden
async kontrol ve kalıcı çözülmemiş işler bağımsız okunur. Aynı isteğin tekrarı
yeniden gözlem yapar, generic release/resume bu kapanışı açamaz. Ayrı final seal
yerel kontroller boşken kalan Lab control admission'ını da kapatır.
Fiziksel cleanup exact service/container/token ve Scientist çözüm kanıtına
bağlanmalı; tek bir inference release makbuzu bütün session kapanışı sayılmamalıdır.
Launch izni bittikten sonraki cleanup ayrıca geçerli cleanup-only hak ister.

Mevcut native-capable default UI değişmeden çalışırken sürdürülebilir GPU
dışlaması kanıtlanamaz: ileride broker dışında Decider/Bonsai işi kabul edebilir.
Anlık boş GPU/PID listesi yeterli değildir. Bu açık üretim sınırı, inert workspace
hazırlığını engellemez fakat gerçek ortak GPU koşusunu engeller.

## Açık hazırlık sırası

Önceden pinlenmiş template ve mevcut `0700` manager base gerekir. Aşağıdaki
değerler yer tutucudur; yetki/config dosyası üretmez ve komutlar çalıştırılmadı:

```bash
./scripts/aos-v1 prepare-shared --shared-template TEMPLATE --shared-template-sha256 TEMPLATE_SHA \
  --shared-output PLAN --expected-session CURRENT_SESSION_OR_none
./scripts/aos-v1 provision-shared --shared-plan PLAN --shared-plan-sha256 PLAN_SHA \
  --expected-session CURRENT_SESSION_OR_none
```

Ardından Scientist gerçek workspace kimliğini ve yeni kaynak/config çiftini
inceler. Ancak güvenilir fresh activation ve predecessor/GPU kapanış kanıtları
hazırsa, reviewed host composition içinde start plan/activation pinlerine ek
olarak provision makbuz hash'ini alır. Public CLI, keyfi trusted provider veya
factory seçimi sunmaz; mevcut default-deny kapıları komutla atlanamaz.

## Kanıt kapsamı

3 Ekim 2026, 08:38 UTC kapsamlı CPU koşusu: **191 test /185 PASS,6 SKIP**;
log `data/shared-scoped-regression-20261003.log`. İlk odaklı96 kontrol aynı
koşuyla örtüşür; sayılar toplanmaz. Yeni entrypoint testinde gerçek authenticated
HTTP route yalnız nested private seed dosyasını yazar; audit/model/runtime
sınırları sentetiktir.19flag/default/named kapsamı, legacy roots ve private
mode/owner/symlink retleri doğrulandı; native/GPU kabulü değildir.

Gerçek offline `prepare-ui`, `scientist-shared-v1` adlı ayrı manager'ın
`data/local-app-project-scientist-shared-v1/ui` dizinini oluşturdu. Seçilen18866
portunda listener yok; named durum `not_started`. Arayüz hazırlığını izleyen
gerçek public `prepare-shared` → `provision-shared` komutları da tamamlandı.
Yeni workspace:
`data/local-app-project-scientist-shared-v1/app-0a5d1cfcebd44c6b9ede47b93755ea1c/workspace`.
Root bağımsız plan/source/provision readback'i boş workspace, UID1000,
device55/inode9608217 ve yalnız receipt+workspace içeren session'ı doğruladı.
Template `shared-template-20261003-v2.private.json`, plan
`shared-plan-20261003-v2.private.json` aynı manager base altındadır; receipt
session altındaki `shared-provision.json` dosyasıdır. Kaynak closure227 dosya
(156 AOS,67 Scientist,4 Python/model pini), config map3 dosyadır.
Servis unit adı `swapp-aos-gpu-shared-desktop-default.service` sabittir; servis
başlatılmadı, unit dosyası yüklenmedi. Bu gerçek filesystem hazırlığıdır;
runtime/GPU kabulü, activation veya çalışır18866 bağlantısı değildir.

İlk public komut named scope'u kaybetti: `python -m aos.local_app` içindeki
`__main__._INSTANCE` ile manager'ın import ettiği canonical modülün `_INSTANCE`
değeri farklıydı. Entry dispatch artık canonical `main` çağırır. Test için
public komut atlanmadı; başarısız ilk template ve kanıt korunarak yeni kaynak
pinli v2 template ile gerçek komut tekrarlandı. Yeni subprocess regresyonu
named/default hazırlık, provisioning ve yanlış scope reddini kapsar.
Son focused Btrfs100 kontrolü geçti; legacy dahil normal tmp/umask022 koşusunda
187 testin182'si geçti;4 opt-in test ve dolu pilot portunu isteyen1 test atlandı. Loglar
`data/shared-public-cli-btrfs-focused-20261003.log` ve
`data/shared-public-cli-regression-final-20261003.log`.

3 Ekim 2026, 08:21 UTC birleşik explicit-provisioning koşusu: **72 shared CPU
test geçti** —20 plan,18 provision,20 host,14 manager. Legacy manager87 kontrolü
ile toplam159 testte154 PASS/5 SKIP; `data/shared-provision-cpu-20261003.log`.
Gerçek tmpdir kimlikleri/lock/exclusive write ile tekrar ve inode değişimi
reddedilir; service/GPU sınırları sentetiktir. Yeni yan-depo izolasyon engeli
bu test başarısıyla kapanmış sayılmaz.

3 Ekim 2026, 07:57 UTC tarihsel root birleşik koşusu: **43 CPU test** — 20 plan,
14 host, 9 manager; explicit provisioning öncesi kaynak sürümü.
Manager testi actual `SharedDesktopHost` sınıfıyla public CLI
prepare → start → status → token → stop akışını private dosyalarda çalıştırır;
systemd, network, process ve GPU sınırları açıkça sentetiktir. Gerçek servis/GPU
başarısı değildir. Yanlış predecessor/owner, eski generation, finite expiry,
lost ACK, yeniden kullanılmış unit, belirsiz cleanup ve native fallback reddi
kapsanır. Gerçek native ve koordineli iptal/toparlanma kabulü açık kalır.

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  test_shared_desktop_plan test_shared_desktop_provision test_shared_desktop_host test_shared_desktop_manager -v
```

Commit çiftini ve güncel test/gözlem sınırlarını [STATUS](STATUS.md), tam kapanış
hedefini [RELEASE_ACCEPTANCE](RELEASE_ACCEPTANCE.md) dosyasında izleyin.
