# Bileşik katalog hedefinden açık sıra başlatma

Bu dilim iki veya üç farklı sabit görevin Türkçe hedefini mevcut
[TaskSequences](TASK_SEQUENCE.md) yürütücüsüne bağlar. Serbest görev planlama,
yeni araç/path/content üretme veya model çıktısından yetki türetme değildir.
Yalnız mevcut `hello`, `browser_form` ve `vision_canvas` görevleri kullanılabilir.
Tek görev önizlenebilir; bu yeni endpoint'ten başlatılamaz, mevcut tek görev
başlatma yolu kullanılır.

## Önizleme ve başlatma ayrı işlemlerdir

`POST /api/tasks/compound-plan` ve `aos.goal_plan` değişmeden salt okunurdur.
`execution_authorized=false`, `live_state_verified=false`,
`automatic_replay_allowed=false`, `requires_action_approval=true` değerleri
korunur. Metin girmek, panel açmak veya önizleme almak scheduler reservation,
job, action ya da approval oluşturmaz.

Yeni `POST /api/tasks/compound-start` yalnız şu alanları kabul eder:

```json
{
  "goal": "önce hello görevini hazırla; sonra yerel form görevini hazırla",
  "input_sha256": "<önizlemedeki özgün metin hash'i>",
  "composition_sha256": "<önizlemedeki tüm bileşim/katalog hash'i>",
  "lease_id": "<güncel oturum lease kimliği>",
  "generation": 0
}
```

`schemas/compound_sequence_start.schema.json` canonical strict request
sözleşmesidir. Geçerli SHA-256 alanlı, açıkça sentetik örnek
`examples/compound_execution.json` içindedir. Buradaki kısaltılmış hash'ler
çalıştırılabilir istek değildir; UI güncel önizleme ve kontrol snapshot'ını
kullanır. Query parametresi, fazladan alan, tür zorlaması, runtime/araç/kapsam,
seçenek listesi veya onay parametresi kabul edilmez. HTTP gövde sınırı 4096 byte,
hedef sınırı 3000 karakterdir. Cookie, exact Host ve Origin kontrolleri mevcut
konsolla aynıdır.

Sunucu özgün hedefi yeniden ayrıştırır; input hash ve tüm bileşim hash'i yeniden
üretilir. Bu ikinci hash görev sırası yanında her katalog planının kapsam,
adım, onay ve bağımsız doğrulama içeriğini de bağlar. Başta/sonda boşluk değişimi
bile input eşliğini bozar. Negated, unsupported, duplicate, tek görev, değişmiş
metin veya katalog/hash eşleşmezliği admission öncesi `409` olur. Geçersiz istek
şekli `400`, büyük gövde `413` olur. Ham hedef hata/başarı yanıtına eklenmez.
Hash bir imza, insan incelemesi kanıtı veya anonimlik garantisi değildir.

Sonra mevcut `TaskSequences.start` aynı event-loop turunda çağrılır; arada await
yoktur. Aktif control lock, kapalı/yok scheduler, eksik görev motoru, eski
lease/generation, HUMAN/PAUSED sahipliği veya mevcut görev/sıra reservation'ı
başlatmayı reddeder. İki eşzamanlı istekten yalnız biri reservation alabilir.
Başarı yanıtı mevcut `SequenceStatus` sözleşmesidir. Sıra planı ve ilerleme
mevcut `desktop_events.kind=task_sequence` kayıtlarında tutulur; ham Türkçe
metin veya ayrı bir kalıcı kaynak-hash admission kaydı eklenmez. Yeni migration
veya çökme sonrası kaynak/admission kurtarma iddiası yoktur.

## Yürütme sınırları

- Başlatma yalnız sırayı kabul eder; model eylemini onaylamaz. Her gerçek eylem
  mevcut süreli, tek kullanımlık, exact-action hash/state/runtime/lease bağlı
  approval yolundan geçer. İlk onay sonraki adıma veya göreve taşınmaz.
- Önceki görev bağımsız doğrulanmadan sonraki başlamaz. Ret, süre aşımı,
  doğrulama hatası veya kontrol devri kalan görevleri durdurur. Oturum/runtime
  ve deployment bağları mevcut scheduler tarafından tekrar kontrol edilir.
- Duraklatma kalan sırayı iptal eder; yalnız mevcut görev eski Pause/Resume
  sınırlarıyla korunabilir. Devam et iptal edilmiş sıranın kalanını başlatmaz.
- Model metni, önizleme ve hash eşleşmesi onay ya da yeni araç kapsamı değildir.
  Fixture motoru açıkça sentetiktir; gerçek Decider/Bonsai kabulü yerine geçmez.
- Otomatik retry, replay veya crash continuation eklenmez. Endpoint bir
  idempotency servisi değildir: önceki sıra bittikten sonra aynı geçerli
  kontrol kimliğiyle açıkça gönderilen yeni istek yeni sıra olabilir; tüm
  gerçek eylemler yine taze onay ister. Yanıt kaybolursa yeniden göndermek
  yerine mevcut Görevler/sıra durumunu inceleyin.

## React akışı

Plan sekmesinde **Birleşimi önizle** tanınan görevleri kapsam ve doğrulamayla
gösterir; mevcut salt okunur metin/etiketler korunur. Yalnız tanınan iki/üçlü
sonuçta ayrı **Bu birleşik sırayı başlat** düğmesi görünür. Gerekli motorlar
kapalı, kontrol uygun değil veya görev/sıra meşgulse düğme devre dışıdır.
Başlatmadan önce kalan sıranın Duraklat ile iptal edildiği ve onayların
Görevler sekmesinde ayrı verileceği açıklanır.

İstek girişteki özgün metin, önizlemedeki iki hash ve güncel lease/generation
ile gönderilir. Başlatma denemesi anında önizleme kaldırılır; hata veya belirsiz
yanıt otomatik yeniden deneme üretmez. Başarı mesajı onay verilmediğini söyler.
Görevler sekmesi mevcut onay ve sıra ilerlemesini gösterir. Metin değişimi,
sekme kapanması ve runtime/generation değişimi eski önizlemeyi atar; bekleyen
önizleme cevabı yeni metne veya oturuma taşınmaz. Kalıcı kontrol çubuğu ve eski
tek görev/sıra seçenekleri korunur.

## Doğrulama

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_compound_execution.py -v
cd ui && pnpm build
```

`AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1` explicit opt-in ile yeni rendered test
de çalışır. `tests/test_compound_execution.py` tüm iki/üçlü katalog sıralarını,
strict schema/sentetik fixture, auth/origin/body/kapsam, yeniden hesaplanan
katalog/input hash'i, eski lease ve generation, control lock, eşzamanlı kabul,
onaysız sıfır action, ret ve takeover sonrası onay iptalini sınar.

Rendered akış: `/ui/` giriş → Plan → Türkçe üçlü önizleme → ayrı başlatma →
Görevler → dört taze onay → üç bağımsız doğrulanmış görev. Kontrol değişiminde
eski önizleme silinir, paused durumda başlatma kapalıdır; olumsuz hedef yeni
başlatma düğmesi üretmez. Gerçek Docker konsolu ve ağsız gerçek Chromium
kullanılır; karar/görsel gözlemci motorları fixture'dır, GPU modeli değildir.
Browser plugin mevcut olmadığı için depodaki Python Playwright akışı seçilir.
Desktop `1440×1000` ve mobile `390×844` boyutları; sayfa kimliği, dolu içerik,
framework overlay, console, taşma ve etkileşim denetlenir. Ekran görüntüleri
`/tmp/aos-compound-start-desktop.png` ve `/tmp/aos-compound-start-mobile.png`
konumlarında kalır, paketlenmez. Koşulmuş sonuçlar ve native/Wayland gibi ayrı
kabul sınırları [STATUS](STATUS.md) içinde tutulur.
