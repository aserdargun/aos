# Durable native inhibit — kaynak adayı

**3 Ekim 2026, 11:01 UTC:** native-only inhibit primitive kaynakta uygulandı;
17 odaklı CPU kontrolü normal filesystem ve Btrfs üzerinde geçti. Mevcut native
entrypoint'lere bağlanmış değildir; çalışan default servis veya Decider/Bonsai
worker'ları için güncel GPU dışlama üreticisi yoktur. Bu sonuç deployment
veya gerçek GPU kabulü değildir. Scientist tek GPU tahsis otoritesidir;
inhibit ikinci GPU allocator, kuyruk, GPU lease veya model scheduler değildir.

## Private store ve immutable publication

Kaynak API'si `provision_native_inhibit(directory, scope_id=...)`,
`acquire_native_lease(store)`, `publish_native_inhibit(store, request)` ve
`read_native_inhibit(store)` fonksiyonlarından oluşur. Store binding'i dışarıdan
beklenen directory/lock/metadata inode'larını ve metadata içerik hash'ini taşır;
okuma bu kimlikleri sessizce yenilemez. `None` readback yalnız receipt
gözlenmediğini söyler; model çalıştırma veya worker/GPU yokluğu kanıtı değildir.

Store yalnız ayrı explicit provisioning ile yeni private scope'ta hazırlanır.
Mevcut dizin/dosya, partial çıktı veya eski store otomatik benimsenmez.
Sonraki okuma/lock/publication exact beklenen device/inode/owner/mode kimliğine
bağlanmalıdır; aynı path veya aynı byte tek başına kimlik yerine geçmez.

Native kullanım ve publication aynı store lock'u üzerinden yarışmalıdır.
Native admission lifetime **shared lock (SH)** tutar. Inhibit yayını
**nonblocking exclusive lock (EX)** ister: bir native lifetime SH varken
publication busy olarak reddedilir; worker durdurulmaz veya lock zorlanmaz.
Bu nedenle eski worker kapanışı EX publication'dan önce gerekebilir.

Publication ayrı explicit ve sonlu publish authority gerektirir. Request'teki
principal/session/generation alanları kendi başına kimlik doğrulama veya
kullanıcı onayı değildir; trusted host bu hakları ayrıca doğrulamalıdır.
Bu primitive için canlı kullanıcı komutu veya yetki sağlayıcısı eklenmez. Başarılı
publication'ın immutable receipt'i kalıcı inhibit'tir; eksik, malformed veya
partial receipt admission'ı fail-closed bırakır. Authority süresinin dolması
inhibit'i açmaz; timeout veya restart otomatik native fallback üretmez.
**Release/reopen uygulanmamıştır.** Receipt silme, overwrite veya yeni store
adoption üzerinden bypass desteklenen geri dönüş değildir. Exact request/receipt
hash'lerine bağlı intent önce sabit lock inode'una yazılıp fsync edilir;
receipt yazımı başarısız olsa veya receipt daha sonra kaybolsa bile native
admission kapalı kalır. Boş lock'un ilk mtime/ctime pinleri, truncate ile yeniden
açılmayı reddeder. Aynı publication'ın tekrarı da reddedilir; tarihsel receipt
okuması ayrı API'dir ve özgün hakkı yenilemez. Publication hata verdiğinde
etki olmadığı varsayılmaz: durable yazımdan sonraki expiry veya readback hatası
inhibit'i kurmuş olabilir. Aynı original store binding ile salt okunur durum
çözülür; kör retry, rollback veya reprovision yapılmaz. Bu iki postcommit hata
senaryosu CPU kabulünde ayrıca doğrulandı.

## Lifetime FD devri

Worker launch lifetime SH descriptor'unu `pass_fds` ile açıkça devralmalıdır.
Parent ve child aynı inherited open-file description'ı paylaşabilir. Her
sahip yalnız kendi FD'sini kapatır; bu description üzerinde **asla `LOCK_UN`
yapılmaz**, çünkü başka yaşayan sahibin kilidini de kaldırabilir. Son sahibi
kapanana kadar SH sürer; worker exit veya eksik descriptor aktarımı ayrıca
denetlenmelidir. Descriptor/inode binding ve receipt denetimi admission'ın
parçasıdır; model çıktısı bu kontrolleri veya publish authority'yi genişletemez.

## Staged giriş ve kalan entegrasyon

[Staged Decider entry wrapper](NATIVE_DECIDER_ENTRY.md) kaynakta uygulandı:
AOS interpreter'ı wrapper'ı
çalıştırır; wrapper unchanged pinned model-venv worker'ını exact argv/env ve
inherited lifetime FD ile başlatır. Model venv, worker veya deployment pini
sessizce değiştirilmez. Bu wrapper'ın uygulanması bütün eski native giriş
yollarını otomatik kapsamış sayılmaz; Bonsai/default UI ve diğer legacy yollar
ayrı kaynak incelemesi gerektirir.

Legacy route promotion ancak daha sonra ayrı explicit maintenance onayı,
incelenmiş source integration/deployment, idle exact-owned kapanış ve
bağımsız worker/GPU yokluğu kanıtıyla yapılabilir. Inhibit primitive'in varlığı
çalışan eski native process'i instrument etmez veya ortak GPU koşusunu açmaz.
Publication kaynak okuması explicit64 MiB ile sınırlıdır; private config okuma
sınırı değişmez. Her dosya öncesinde özgün boot/deadline yeniden kontrol edilir.
Yeni kaynak SHA256 `8b514849feafc902d76418a7b573eca8890ccccb38db32392c1f71016f1cea55`;
önceki `01685b…d20f` onayı ve14-test teslimi tarihseldir.
Güncel CPU kanıtı `data/native-decider-entry-20261003/inhibit-tests.log` ve
`data/native-decider-entry-20261003/inhibit-btrfs-tests.log` dosyalarındadır. Gerçek izole
CPU parent close/crash ve yaşayan child örnekleri descriptor devrini doğrular;
model/GPU çalıştırılmadı ve canlı store provision edilmedi.
[Handover sırası](NATIVE_HANDOVER.md), [shared drain sınırı](SHARED_ADMISSION_DRAIN.md)
ve gerçek kanıt için [STATUS](STATUS.md).
