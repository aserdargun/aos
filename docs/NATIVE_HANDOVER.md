# Native → Scientist handover — salt okunur hazırlık adayı

**3 Ekim 2026:** standalone `prepare_native_handover` preview kaynakta uygulandı;
root incelemesi,12 odaklı CPU testi ve gerçek salt okunur v2 gözlemi geçti.
Bu sonuç yalnız önizleme kabulüdür, gerçek devir kabulü değildir. Bu belge bakım yürütme yetkisi,
çalışan servise müdahale veya tamamlanmış native GPU dışlaması değildir. Native handover
yürütmesi bu kaynak teslimi kapsamında yetkilendirilmemiştir.

**Teslim güncellemesi, 3 Ekim 2026:** `src/aos/native_maintenance.py` ve
`scripts/native_maintenance.py` artık ayrı request/store/receipt şemalarıyla
kaynakta vardır. Exact request hash onayı, eski owner/session kimliği, pidfd,
özgün Docker daemon'ı, kaynak promotion ve kalıcı hata günlüğü CPU/sentetik
sınırlarında sınanır. Canlı bakım, GPU release veya yeni shared runtime
başlatılması yapılmadı. Aday patch varsayılan canlı kaynaklara uygulanmadı.
`src/aos/native_exclusion.py` henüz yalnız typed DTO/schema ve zaman doğrulama
yardımcılarıdır; gerçek `read_native_exclusion` producer ve Scientist consumer
tamamlanmadı. İki fresh absence gözlemi, tam source/config closure ve yeni
actual caller bağının ortak kabulü sonraki aşamadır. Bu modülün bulunması
bakım yetkisi veya ortak GPU kabulü sayılmaz. [Teslim sınırı](DELIVERY_AND_CONTINUATION.md).

## Preview giriş/çıktı sözleşmesi

Uygulama işçisiyle eşlenmiş standalone giriş aşağıdadır; mevcut bir
`aos-v1`/`local_app` lifecycle komutu veya execution flag eklenmez. Komut
yalnız preview içindir;
buradaki session ve output değerleri yer tutucudur:

```bash
.venv/bin/python scripts/prepare_native_handover.py \
  --expected-session EXACT_CURRENT_SESSION \
  --output /absolute/existing-private-dir/preview.json
```

Modül API'si `prepare_native_handover(expected_session: str, output: Path)`
bir `NativeHandoverPreview` döndürür. Kapsam sabit default native manager'dır;
named/shared session seçimi veya adoption yapmaz. Output parent önceden var
olan private `0700` dizin olmalıdır. Preview yeni `0600` dosyaya exclusive
yazılır; mevcut çıktı üzerine yazılmaz, hata başka scope'u seçmez.

Gözlem login-only authenticated POST, dört bounded endpoint GET'i
(`/api/state`, `/api/tasks`, `/api/session/binding`, `/api/scientist/jobs`),
state/binding continuity recheck'i ve sınırlı process ancestry/cgroup
snapshot'ıdır. Drain, stop, start veya control isteği
yapılmaz. Busy, unknown, unresolved, eksik veya truncated gözlem olumlu
handover iznine dönüşemez; blocker kaydedilir veya hazırlık reddedilir.
Consent, sürdürülebilir native dışlama, fiziksel cleanup ve Scientist
reservation her zaman **NOT VERIFIED / required** kalır. Preview dosyası
bu eksik hakları üretmez.

Root draft `data/native-handover-20261003/preview-v1.private.json`,
`recorded_at=2026-10-03T10:16:30.316220+00:00`: `local_idle_observed=true`,
`execution_authorized=false`. Dört açık blocker consent, sürdürülebilir native
dışlama, fiziksel cleanup ve Scientist canonical reservation'dır. İlk denemede
`0755` output parent doğru olarak reddedildi; dosya üretilmedi. Başarılı draft,
idle anlık gözlemdir; bütün handover kapılarının kapanması değildir.
Son kaynakla üretilen `data/native-handover-20261003/preview-v2.private.json`
SHA256 `27f1b7905d726d37c5a538391f1e473d4d4c51746d08f31c4219cbf89b77ec9d`
aynı dört blocker'ı korur. V1 tarihsel taslaktır; v2 discovery parent bilgisini
sahiplik olarak sunmaz ve HTTP okumasından sonra ancestry/cgroup gözlemini
yeniden karşılaştırır. CPU kanıtı `data/native-handover-20261003/cpu-tests.log`.

## Gerekli sıra ve uygulama sınırı

Preview admission'ı kapatmaz, iş iptal etmez, sinyal göndermez veya Scientist'i
başlatmaz. Üretilen yedi `procedure` aşaması yürütülmemiş tasarım kapılarıdır:

1. **Consent — NOT IMPLEMENTED execution gate.** Kullanıcı exact
   preview/source/session kimliğini, duracak kullanıcı oturumunu ve sınırlı
   bakım etkisini ayrıca onaylamalıdır. Önceki shared runtime veya preview
   talebi bu shutdown onayının yerine geçmez.
2. **Fence — NOT IMPLEMENTED native lifecycle integration.** Yeni native
   görev/model admission'ı önce kapanmalıdır. Mevcut eski native process
   durable fence instrumentation'ı taşımaz; source integration, review ve
   deployment gerekir. Native yolları korunacak profilde bütün model girişleri
   lifetime store'a bağlanmalıdır; opt-in shared-only profilinde bunun alternatifi
   bütün native model girişlerini koşulsuz kapatmaktır. Bu staged kaynak adayı
   mevcut servise uygulanmış dışlama değildir. Nonblocking exclusive fence yayını, lifetime shared
   lock tutan eski native process'in explicit kapanışını önce gerektirebilir.
   Bu yüzden durable yayın her shutdown'dan önce gelir varsayılmaz;
   sürdürülebilir dışlama Scientist admission'ından önce kesinleşmelidir.
3. **Drain — yalnız source-verified capable runtime için kaynak protokolü.**
   Ayrı onaydan sonra exact controller shared drain/seal uygulanabilir; çalışan
   eski native UI'nin yeni protokolü desteklediği iddia edilmez. Busy, pending,
   unknown veya unresolved işte geçiş reddedilir; otomatik iptal yapılmaz.
4. **UI interruption — NOT IMPLEMENTED handover execution.** Onaydan sonra bütün
   kimlik ve idle kontrolleri taze okunmalıdır. Yalnız exact owned
   supervisor/backend ve açıkça sahip olunan kaynaklar kapanabilir. Busy,
   unknown veya unresolved durumda otomatik iptal/force-stop yapılmaz.
5. **Physical cleanup — NOT IMPLEMENTED handover proof.** Process çıkışı, owned
   worker/resource kapanışı ve kalan native GPU kullanımının yokluğu ayrı
   kanıtlanmalıdır. Tek anlık boş GPU listesi, sürdürülebilir admission fence
   veya tam cleanup yerine geçmez. Eksik, stale, truncated veya tutarsız
   gözlem dışlama kanıtı sayılmaz.
6. **Scientist reservation — NOT IMPLEMENTED handover gate.** Sürdürülebilir
   native dışlama ve canonical Scientist hakkı bağımsız doğrulanmalıdır;
   preview GPU release veya reservation üretmez. Scientist tek broker authority'dir.
7. **Shared launch — NOT IMPLEMENTED handover execution.** Exact yeni
   caller/broker generation, incelenmiş source/config, workspace ve sonlu
   activation hakkı bağımsız doğrulanmalıdır. AOS ikinci scheduler veya native
   fallback açamaz.

Bu sıralama tasarım sözleşmesidir; ileri adımların uygulanmış veya kabul edilmiş
olduğunu göstermez. Gerçek test ve deployment gözlemi [STATUS](STATUS.md)
kaydında ayrıca tutulur.

## Sıradaki uygulama sınırı

**Primitive kaynakta, çalışan native girişlere entegrasyon yok:**
[native-only inhibit](NATIVE_INHIBIT.md), sabit private lock inode'u
üzerinde admission/publication yarışını serialize eder; ikinci GPU scheduler
kurmaz. Native worker yaşamı boyunca shared lock tutulur; publication yalnız
nonblocking exclusive lock ile kabul edilir. Inode silinmez/değiştirilmez.
Native çalıştırmayı koruyan bir profil için parent kapanması yaşayan worker'ın
kilidini düşürmemelidir; lifetime descriptor worker'a aktarılmalıdır.

**Opt-in shared-only alternatif:** Astra incelemesinde onaylanan staged kaynak
adayının paket yolu `scripts/shared-only-runtime-v1/`; dosyaları
`source.patch.txt`, `manifest.json`, `README.md` olarak ayrılır. Aday bütün
native yolları girişte koşulsuz ret ile kapatır; native `ModelSession`
ile yalnız broker S1 için `BrokerModelSession` ayrılır. Scientist broker S1/S2
mevcut TurnGate hakkıyla korunur; broker'ın kullandığı ortak model koduna
native blanket deny uygulanmaz. Bu profilde kapatılmış native yolları yeniden
lifetime store'a bağlamak shared kabulünün zorunlu koşulu değildir.

Çalışan default native oturum sürerken patch uygulanmaz. Profil tasarımı onayı,
üretilen yamanın kaynak incelemesi veya test kabulü değildir; canlı promotion,
maintenance onayı veya GPU kabulü de vermez.
Literal broker import'u değiştiği için eski source/config pair güncel kabul
sayılmaz; yeni pair ve import closure bağımsız yeniden incelenmelidir.
Maintenance executable, `read_native_exclusion` producer'ı ve fiziksel cleanup
kabulü henüz tamamlanmadı; **GPU HOLD** sürer. Yeni test sonucu iddia edilmez.

Scientist'in istediği salt okunur `read_native_exclusion` producer'ı ancak
seçili profilde native girişler gerçekten lifetime-fenced veya tamamen disabled
olduktan ve eski çalışan process'ler bağımsız kapandıktan sonra kabul edilebilir. Exact handover/shared
plan hash'leri, boot/process kimlikleri, kaynak/config pinleri, halen etkin
inhibit ve bağımsız owned-worker cleanup kanıtı birlikte gerekir. Caller
deadline özgün sonlu hakkı yalnız kısaltabilir; expiry yeni model/shared
başlangıcını reddeder ama native inhibit'i otomatik kaldırmaz. Bu arayüz
önerisi mevcut endpoint veya çalışan producer olarak sunulmaz.

**3 Ekim oturumlar arası sözleşme ayrımı:** Scientist, consumer/host adapter ve
incelenmiş wire schema'nın henüz bulunmadığını teyit etti. Önerilen okuma
`read_native_exclusion(expected_handover_sha256, expected_shared_plan_sha256,
*, deadline)` biçimindedir; canonical kanıt ve SHA döndürür. Endpoint, ortak
dosya yolu veya çalıştırma yetkisi değildir. İki kimlik ayrı tutulmalıdır:

- **Emekli native oturum:** özgün manager session, supervisor/backend
  `ProcessIdentity` ve bağımsız yokluğu gözlenen owned worker'lar. Shared SSH
  `session-3.scope` owned servis değildir; InvocationID uydurulmaz ve bu scope'a
  toplu sinyal gönderilmez.
- **Yeni shared caller:** gerçekten gözlenen dedicated unit, InvocationID,
  process kimliği ve pinli plan/source/config. Önceki native PID veya tarihsel
  servis kimliği bu role taşınmaz.

Üretici ve consumer aynı sürümlü schema üzerinde ayrıca anlaşmalıdır. Original
issued/expires ve aynı boot bağları korunur; caller deadline hakkı uzatamaz.
Güncel kaynak/config farkı, eksik veya süresi dolmuş kanıt ve gözlem hatası
ret sebebidir. Kaynak profili uygulanmış olsa bile geçmiş makbuz veya idle
gözlemi güncel fiziksel cleanup yerine geçmez. Bu kanıt acquire/release yapmaz;
tek tahsis otoritesi Scientist scheduler'ıdır. Expiry native yolları açmaz.

## Shared drain neden yeterli değil?

[Shared admission drain](SHARED_ADMISSION_DRAIN.md) scheduler/Lab girişini
aynı controller için process-local kapatır; final seal kalan Lab kontrollerini
de kapatır. Canonical DB receipt bu yerel gözlemin kalıcı audit kaydıdır.
Native start/restart yollarına durable fence kurmaz, default native servisi
kapatmaz ve worker/GPU yokluğunu doğrulamaz. Bu nedenle shared drain/seal,
native exclusion veya maintenance shutdown yetkisi olarak kullanılamaz.

## Gerçek salt okunur gözlemin sınırı

Root'un 3 Ekim gözleminde default session
`app-d0898491ef744a258969589e4e41ffcd`, supervisor PID `42512` ve backend PID
`42513` olarak görüldü. Ortak cgroup:
`/user.slice/user-1000.slice/session-3.scope`.

Bu cgroup diğer session process'lerini de kapsayabilir: **asla group shutdown
hedefi değildir**. PID'ler tarihli gözlemdir; gelecekteki signal veya adoption
yetkisi değildir. Exact boot/start-time/process kimliği bağımsız ve taze
doğrulanmalıdır. Gerçek session/PID/cgroup değerleri sentetik fixture'a
kopyalanmaz. Bu çalışma native default servisi değiştirmez.

## Cleanup ve geri dönüş

**NOT IMPLEMENTED:** future cleanup/rollback ayrı explicit sonlu yetki ve
independent readback gerektirir. Scientist'in GPU hakkı sürerken native
servis otomatik yeniden başlatılamaz. Timeout, kayıp ACK veya belirsiz cleanup
otomatik native restart, fallback veya hak devri tetiklemez; durum kapalı ve
inceleme gerektirir. Native'e geri dönüş ancak Scientist hakkının ve owned
worker/GPU kullanımının kapandığı bağımsız kanıtlandıktan sonra ayrıca açık
onay ve taze kimliklerle yapılabilir.

Kaynak/manager sınırları: [SHARED_DESKTOP_MANAGER](SHARED_DESKTOP_MANAGER.md),
tek kabul kaydı: [RELEASE_ACCEPTANCE](RELEASE_ACCEPTANCE.md).
