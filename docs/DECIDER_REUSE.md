# Görev içinde Decider yeniden kullanımı

Decider'ı her eylem kararında yeniden yüklemek yerine aynı canlı görev boyunca bellekte tutan dar hızlandırmadır. **Tahmin önbelleği yoktur:** her güncel gözlem ve seçenek listesiyle gerçek model forward çağrısı yapılır. Model/checkpoint/tokenizer/dependency kimliği, eager CUDA yolu, 1536-token sınırı, güvenlik politikası, iki ayrı form onayı ve bağımsız doğrulama değişmez.

## Etkinleştirme ve yaşam döngüsü

Yeni gerçek managed başlangıçta `./scripts/aos-v1 start` bu yolu kullanır. Açık backend seçimi:

```bash
.venv/bin/python scripts/serve_desktop.py --engine decider --reuse-decider \
  --browser-tasks --desktop-browser --desktop-vision --vision-engine bonsai
```

`--reuse-decider` yalnız gerçek Decider motoruyla kabul edilir. Flagsiz backend ve normal CLI tek çağrılık worker yolunu korur. Çalışan eski backend kendiliğinden değişmez.

Yeni managed gerçek başlangıç ayrıca explicit `--prewarm-decider` ve
`--gpu-idle-seconds 30` geçirir: ilk görev öncesi süreli CPU-only hazırlığı,
başarılı vision-dışı görevden sonra süreli GPU residency ve her yeni görevde tam
yeniden pin kontrolü. Direct backend'in flagsiz GPU worker'ı yalnız job boyunca yaşar. Bu seçimlerin yaşam döngüsü ve
ölçüm ayrımı [DECIDER_PREWARM](DECIDER_PREWARM.md) içindedir; aşağıdaki temel
kurallar yalnız `--reuse-decider` yolunu da korur.

- Worker sabitlenmiş dosyaları ve bağımlılıkları başlangıçta doğrular, ilk geçerli istekte modeli bir kez yükler; TCP endpoint açmaz, yalnız private stdin/stdout kullanır.
- Her istek yeni 32-hex request_id içerir. Worker ID tekrarını, bozuk/yabancı alanları, geçersiz seçenekleri ve 64 KiB sınırını reddeder. İstemci ID, deployment digest, probability ve seçenek üyeliğini yeniden denetler.
- Aynı anda yalnız bir istek kabul edilir. Timeout, iptal, bozuk cevap veya büyük takip isteği worker process grubunu kapatır ve reap eder; hatalı isteğe sessiz retry veya eski tahmin yoktur.
- Varsayılan GPU worker yalnız bir job içinde tutulur. Başarı, hata, ret, pause veya takeover sonunda kapatılır. Başarılı browser penceresi sonucu göstermek için açık kalabilir; bu, GPU modelinin açık kaldığı anlamına gelmez. Ayrı CPU prewarm seçeneği başarıdan sonra yeni CPU-only worker hazırlayabilir. Açık `--gpu-idle-seconds` seçeneği yalnız başarılı vision-dışı görev için süreli istisnadır; tam sınır ve kabul [DECIDER_PREWARM](DECIDER_PREWARM.md) içindedir.
- Resume yeni model instance, gözlem ve karar kullanır. Vision'da CPU hazırlığı Bonsai gözlemiyle örtüşebilir; GPU transfer/forward Bonsai kapandıktan sonradır. Bu değişiklik **Bonsai/Decider ortak GPU residency** açmaz.
- Worker ayrıca prewarm kapalıyken 75 saniye, açıkken parent CPU/GPU idle sürelerinden en az 30 saniye uzun idle/eksik satır deadline'ı ve 256 cevap üst sınırıyla kendini sınırlar. GPU belleği görevdeki onay beklemesi boyunca tutulabilir; tek çağrılık yola göre kaynak takası budur.

## Ölçüm ve sınır

### İlk System-1 çağrısı için CPU prewarm

`--prewarm-decider`, `--reuse-decider` ile birlikte backend açılışında ve yalnız başarılı görev sonrasında tek CPU-only worker hazırlar; yeni gerçek managed başlangıç bu iki seçeneği kullanır. Ağırlık/import hazırlığı görev başlamadan yapılabilir; tahmin, GPU context veya görev eylemi üretilmez. CPU hazır olduktan sonra direct backend'de varsayılan 60 saniye, yeni managed gerçek oturumda 300 saniye kullanılmadan bekler; süre dolunca worker kapanır ve kendiliğinden yeniden açılmaz. Yeni görev normal soğuk yoldan yine başlayabilir.

Görev idle worker'ı sahiplenirse süre sayacı tüketilir ve manifest/artifact/dependency doğrulaması **tam olarak yeniden yapılır**. Bu doğrulama runtime/görsel gözlemle örtüşebilir; tamamlanmadan GPU'ya taşıma veya inference yoktur. Eski tahmin/önceki job state'i kullanılmaz. Pause, takeover, hata ve shutdown idle hazırlığı da kapatır; kullanıcı kontrolünden sonra otomatik yeniden hazırlama yapılmaz. GPU bekletme kapalı başarı yolunda eski GPU worker tamamen kapatıldıktan sonra yeni CPU hazırlığı açılır. CPU RAM maliyeti vardır; explicit süreli GPU bekletme dışında sürekli GPU residency veya sınırsız process pool değildir.

UI'deki **System-1 CPU preparation** yalnız `preparing/ready/inactive` kayıtlı durumunu gösterir. Ready, GPU'da yüklü veya karar verilmiş demek değildir. Çok erken Start veya idle süresi geçtikten sonraki görev hâlâ ilk yükleme bekleyebilir. Worker'ın mevcut 75 s idle/256 response ve istek timeout sınırları da korunur.

CPU-only profilde yaklaşık 2.71 s tam pin kontrolü + 1.01 s torch import + 2.01 s Decider import + 1.02 s CPU yükleme ölçüldü. Taze pin kontrolü ayrıca 2.24 s sürdü. Bunlar GPU çıkarımı değildir. Gerçek karşılaştırma [tek-görev onay delegasyonu](TASK_AUTO_APPROVAL.md) ile insan beklemesi kaldırılarak yapılır; startup/idle hazırlık süresi job süresinin dışındaysa ayrıca raporlanır. Kabul sayıları ve gerçek hız farkı [STATUS](STATUS.md) içindedir.

```bash
AOS_DECIDER_PREWARM_TESTS=1 AOS_AUTO_APPROVAL_REAL_TESTS=1 \
  AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1 PYTHONPATH=tests \
  .venv/bin/python -W error::ResourceWarning -m unittest test_auto_approval_real -v
```

### Önceki görev-içi ölçüm

Sonraki dilim vision için CPU-only hazırlığı Bonsai gözlemiyle paralelleştirir; GPU yürütmesi yine sıralıdır. İlk yükleme maliyeti atlanmaz, beklemenin bir bölümü örtüştürülür. TASK_PROGRESS.md yeni uçtan uca ölçümü ve cleanup sınırını açıklar; aşağıdaki form ölçümü önceki dilimin kanıtıdır.

Görünür formun iki gerçek-model kararında aynı host üzerinde bir A/B smoke yapıldı. Tek çağrılık yolda ikinci karar 8270.008 ms, yeniden kullanımda 68.422 ms ölçüldü. İki koşuda da iki ayrı onay, iki `independent_dom_equals passed`, succeeded/passed ve training_eligible=0 doğrulandı. Bu **ikinci model çağrısının** ölçümüdür; tüm işin veya tüm görevlerin 120 kat hızlandığı iddiası değildir. İlk yükleme ve Bonsai gözlemi bu dilimde hızlandırılmadı. İlk çağrıların 10487.803/7953.780 ms farkı ayrı bir optimizasyona atfedilmez; tek çift ölçüm ve koşu değişkenliği vardır.

Worker metrikleri `load_ms`, `inference_ms`, `reused` alanlarını ekler; ilk iki değer hash/import/process maliyetini kapsamaz. Kalıcı `model_calls.latency_ms` uçtan uca model çağrı süresidir; UI onay beklemesi dahil değildir. Ölçülen PyTorch peak allocated 3811927040 bayttır; bu toplam GPU/process tüketimi değildir. Gerçek test ve açık sınırlar [STATUS](STATUS.md) içindedir.

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_reusable_decider.py' -v
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_decider_worker.py' -v
AOS_DESKTOP_TESTS=1 AOS_REAL_BROWSER_TASK_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_visible_scheduler.py' -v
```

İlk iki test sentetik worker/model fixture'larıdır; GPU kabulü değildir. Üçüncü komut gerçek GPU/masaüstü opt-in'idir. Training, model promotion, model küçültme/quantization, genel inference servisi ve oturumlar arası model cache bu dilimde yoktur.
