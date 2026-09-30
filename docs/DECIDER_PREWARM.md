# Süreli CPU-only Decider hazırlığı

Bu dilim ilk System-1 kararından önceki Python import/model hazırlığı beklemesini
uygun durumda kullanıcı etkileşiminden önceye taşır. **Varsayılan CPU-only yol tahmin
önbelleği veya boşta GPU modeli değildir; süreli GPU seçeneği aşağıda ayrı tanımlanır.** Aynı sonlu seçenekler, taze state/observation, gerçek
forward, policy/lease, eylem onayı veya ayrı görev delegasyonu ve bağımsız sonuç
doğrulaması korunur. İlk soğuk başlangıcın toplam maliyetinin yok edildiği veya
karar inference'ının tek başına 7–9 saniye sürdüğü iddia edilmez.

## Açık seçim

```bash
.venv/bin/python scripts/serve_desktop.py --engine decider --reuse-decider \
  --prewarm-decider --browser-tasks --desktop-browser --desktop-vision \
  --vision-engine bonsai
```

`--prewarm-decider` yalnız `--reuse-decider --engine decider` ile kabul edilir. İsteğe bağlı `--prewarm-idle-seconds` yalnız bu üçlüyle ve **1–600 saniye** aralığında kabul edilir; doğrudan backend varsayılanı 60 saniyedir. Yeni managed gerçek oturum bu seçeneği **300 saniye** olarak pinler; fixture modu geçirmez. Bu, CPU worker'ının boşta RAM tutma süresini uzatır, GPU modelini resident tutmaz veya ilk soğuk hazırlığı kaldırmaz.
Worker'ın kendi satır-okuma boşta sınırı prewarm açıkken parent süresinden en az 30 saniye uzundur (75–630 saniye); prewarm kapalıyken 75 saniyedir. Önceki sabit 75 saniyelik worker sınırı, 300 saniyelik managed prewarm ACK'i hâlâ hazır görünürken worker'ı sonlandırıp ilk görevin `admit_job_cpu` isteğini başarısız kılabiliyordu. Yeni görev sahiplenilirken worker beklenmedik biçimde zaten ölmüşse, inference veya eylem tekrarı yapılmadan yeni worker'da tam CPU hazırlığına dönülür; pin denetimi atlanmaz.
Sadece `--reuse-decider` verilirse önceki job-local yeniden kullanım ve vision
için görev sırasında CPU hazırlığı davranışı korunur. Yeni managed gerçek
`./scripts/aos-v1 start` bu iki flag'i birlikte geçirir; fixture modu geçirmez.
Çalışan eski backend kendiliğinden değişmez. Model/artifact/dependency/image
pinleri ve active deployment değiştirilmez; indirme veya eğitim yapılmaz.

## Yaşam döngüsü ve GPU sınırı

1. Backend lifespan başlangıcı beklenmeden tek CPU hazırlık worker'ı başlatılır.
   Worker mevcut tam artifact-file-set/SHA-256 ve dependency kontrollerini yapar,
   torch/Decider modüllerini import eder ve modeli CPU'ya yükler. Hazırlık öncesi
   ve sonrası `torch.cuda.is_initialized()` false olmalıdır; inference yapılmaz.
2. Başarı ACK'inden sonra direct backend'de varsayılan **60 saniyelik**, yeni managed gerçek oturumda **300 saniyelik** idle süre başlar. Hazırlık isteği de
   mevcut 120 saniyelik parent timeout ile sınırlıdır. Birden fazla idle worker
   veya süresi bitince otomatik yeniden hazırlama döngüsü yoktur. Hazırlık RAM,
   CPU ve disk bant genişliği kullanır; GPU resident modeli tutmaz.
3. Yeni görev mevcut idle worker'ı tek sefer sahiplenir, expiry'yi tüketir ve
   arka planda `admit_job_cpu` ister. Worker **manifest içeriğini ve bütün pinned
   artifact/dependency ortamını yeniden denetler**; model yolu/kimliği ve CUDA
   başlatılmamış olması yeniden doğrulanır. Bu iş runtime başlangıcı ve varsa
   Bonsai gözlemiyle örtüşebilir. Hash denetimi önbelleğe alınmaz/atlanmaz.
4. Idle hazırlanmış worker, admission kontrolü tamamlanmadan inference isteğini
   açıkça reddeder. Decider, görev hazırlığını bekledikten sonra yalnız gerçek
   karar gerektiğinde CPU modelini GPU'ya aktarır. Vision'da bu mevcut Bonsai
   process/server drain sonrasıdır; iki modelin eşzamanlı GPU residency'si açılmaz.
5. Varsayılan yolda görev boyunca taze kararlar mevcut tek worker'da hesaplanabilir;
   görev bitince GPU worker'ı kapatılır ve reap edilir. Yalnız **başarılı**, halen
   AGENT/running oturumdaki görevden sonra yeni CPU-only idle hazırlık başlatılır.
   Hata, ret, pause, takeover, logout, stop veya shutdown yeniden hazırlama açmaz;
   görev bulunmasa bile pending/idle worker kapatılır. Aşağıdaki ayrı opt-in GPU
   bekletme seçeneği bu başarılı görev sonu davranışının istisnasıdır.
6. Expiry cleanup ile aynı anda gelen yeni görev eski owned-child cleanup'ını
   bekler; ikinci worker eski drain tamamlanmadan açılmaz. Eski cleanup yeni
   job claim'ini veya hazırlık metriklerini sıfırlayamaz. Erken iptal hem eski
   idle preparation hem yeni job preparation task'ını kapatır.

Idle worker yoksa/expire olmuşsa görev mevcut soğuk CPU hazırlığıyla başlar;
başlangıçtaki tam pin denetimi zaten o görevin içindedir. Hazır olmayan bir
worker'ın varlığı düşük latency garantisi değildir. Model değişimi, bozuk yanıt,
yanlış request/deployment kimliği, timeout veya iptal taze karar üretmeden owned
process grubunu kapatır. Hazırlık veya admission başarısızlığı otomatik retry
yapmaz. Kullanıcı yeniden açık bir görev başlatabilir.

## Gözlemlenebilir durum

Authenticated `GET /api/tasks` yalnız şu küçük alanı ekler:

```json
{"decider_preparation":{"enabled":true,"state":"ready"}}
```

`state`, `inactive`, `preparing` veya `ready` olur. `ready`, geçerli CPU-only
ACK'i alınmış, canlı ve henüz bir göreve verilmemiş worker demektir; GPU hazır,
eylem onaylı veya görev başarılı demek değildir. Path, PID, ham model metrikleri
ve secret gösterilmez. Alanı olmayan eski backend desteklenebilir. Kabul testleri
sabit sleep yerine bu alanı bounded polling ile bekleyebilir.

## İsteğe bağlı süreli GPU bekletme

`--engine decider --reuse-decider --prewarm-decider --gpu-idle-seconds N` yalnız
`N=1..120` ile kabul edilir. **Direct backend varsayılanı kapalıdır; yeni managed
gerçek oturum 30 saniye geçirir, fixture modu geçirmez.** Başarılı, kontrolü hâlâ AGENT/running olan vision dışı görevin
sonunda worker'ın CUDA modeli en fazla `N` saniye resident tutulur. Sonraki görev
başlangıcında aynı worker, değişmemiş manifest ve tüm artifact/dependency pinlerini
yeniden doğrulayan `admit_job_gpu` ACK'i olmadan çıkarım yapmaz. Bu kontrolün disk
hash maliyeti korunur; tahmin/eylem/onay önbelleği değildir. Worker satır-okuma
deadline'ı CPU ve GPU idle sürelerinin her ikisinden en az 30 saniye uzun tutulur.

`GET /api/tasks.decider_preparation.state=ready_gpu` yalnız canlı ve süreli boşta
GPU worker'ını gösterir. Vision başlamadan önce worker kapatılır; böylece Bonsai
ve Decider eşzamanlı GPU residency açılmaz. Hata, iptal, kontrol devri, logout,
shutdown veya deadline da worker'ı kapatır. Otomatik yeni görev veya onay üretmez.
Bu mod boşta yaklaşık model kadar VRAM harcar. Tek test-owned gerçek Hello çiftinde
iki bağımsız sonuç geçti ve aynı resident worker kullanıldı; gecikme/p95 veya
genel web uygulaması hız garantisi değildir. Ayrıntılı kanıt [STATUS](STATUS.md)'tadır.

## Ölçüm ayrımı

CPU-only tanı ölçümünde (`CUDA_VISIBLE_DEVICES=''`, hiçbir CUDA context yok):
ilk tam pin kontrolü 2.707 s, torch import 1.005 s, Decider import 2.011 s,
CPU model yükleme 1.025 s; ikinci tam pin kontrolü 2.241 s ölçüldü. Bunlar tek
yerel örnektir; GPU inference ölçümü veya dağılım/p95 değildir. Kanıt log'u
`/tmp/aos-s1-cpu-profile.log` paket dışında kalır.

Hazır worker ile iş başlangıcı zamanlaması, startup/idle hazırlığı hariç tutulduğu
açıkça yazılarak raporlanmalıdır. İş latency'si, CPU hazırlığı, gerçek S1 forward
ve kullanıcı onayını bekleme süresi birbirinden ayrılır. Otomatik görev onayı
ayrı explicit özellik olup bu optimizasyon tarafından etkinleştirilmez. Gerçek
warm/cold karşılaştırma, GPU/Bonsai sıralılığı ve uygulama sonuçları [STATUS](STATUS.md)
içinde bağımsız kabul kanıtı olarak tutulur.

23 Eylül 2026'da görev içi erken GPU taşıma denendi ve geri alındı: aynı
görünür form scheduler'ında CPU-ready tabana karşı ilk manuel onaya kadar süre
CDP'de 2804.8→2794.0 ms, MCP'de 2860.8→2863.0 ms oldu. Bu tek çiftler anlamlı
uçtan uca hız kazancı göstermedi; ek VRAM ve yaşam döngüsü karmaşıklığı
korunmadı. Mevcut yol CPU-only idle hazırlık ve karar anında GPU aktarımıdır.

## Doğrulama

`tests/test_decider_prewarm.py`: gerçek küçük subprocess'larla ACK/readiness,
tek worker/tek job claim, taze kararlar, expiry/no-respawn, yavaş cleanup yarışı,
erken cancellation, bozuk admission/kimlik/CUDA cevabı, cold fallback ve default
reuse davranışı; mocked torch ile tam revalidation, manifest/checksum/dependency
değişimi, forward öncesi fail-closed ve CUDA'ya erken geçmeme; scheduler'da
success-only rewarm, manuel onayın korunması ve idle/audit-failure cleanup.

```bash
PYTHONPATH=tests .venv/bin/python -W error::ResourceWarning -m unittest \
  test_decider_prewarm test_decider_worker test_reusable_decider -v
```

Bu portable testler gerçek Decider performans kabulü değildir. Token bütçesi,
eager CUDA yolu ve mevcut worker bounds değişmez; CPU hazırlığının RAM maliyeti
ve kullanıcı çok erken başlattığında kalan soğuk yükleme maliyeti açık sınırlardır.

25 Eylül 2026 canlı eski 60 saniyelik managed backend'de iki ayrı gerçek Hello görevi ölçüldü: idle hazırlığı `inactive` iken ilk görev **6,013 s** (tek S1 **5772,2 ms**), başarılı görevden sonraki `ready` durumunda ikinci görev **2,994 s** (tek S1 **2800,4 ms**). İkisi de yalnız yerel `hello`, `approve_all` görev kapsamı ve bağımsız başarıdır; bu iki örnek p95 veya 300 saniyelik yeni ayarın canlı hız kanıtı değildir. Yeni ayar yalnız hazırlığın kullanılabilir kalabileceği pencereyi uzatır; aynı CPU modelini ikinci kez eğitmez veya GPU'da tutmaz.
