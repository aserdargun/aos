# Görev evreleri ve görsel görev başlangıcı

## Gerçek durum görünürlüğü

Terminal `failed` job ve `failed` run'da son state/snapshot/step bağı tam doğrulanır ve `last_error.code` kanonik hata kodlarından biriyse `failure_code` ayrıca döner. Hata ayrıntısı, prompt, özel veri ve traceback API'ye taşınmaz; geçersiz veya bağsız kaynakta `null` kalır. Development ilk açılış kartı kodu, Tasks ise kodu ve iki dilli genel açıklamayı gösterir. Bu bir tanı ipucudur, otomatik retry veya site sonucu değildir.

Reusable Decider işçisinin cevap vermeden EOF ile kapanması `MODEL_FAILURE`; gelen fakat sınırı aşan, yarım kalan veya JSON olarak bozuk cevap `INVALID_OUTPUT` olur. Her iki durumda işçi kapatılır, aynı karar/eylem kendiliğinden yeniden denenmez. Önceki backend'in saklanmış hata kodları sonradan yeniden yazılmaz.

GET `/api/tasks` her job için `progress` ekler: son kaydedilmiş `phase`, `state_version`, toplam `elapsed_ms`, en yeni en fazla 10 tamamlanmış model çağrısının role/status/latency bilgisi ve aynı doğrulanmış run'a bağlı tüm tamamlanmış çağrıların S1/S2 başarılı/toplam sayıları. `model_call_totals` yalnız state/snapshot/step bağı geçerse sunulur; eksik, yanlış run veya bozuk durumda `null` kalır. Sayım, yalnız sonlu ve negatif olmayan latency ile tamamlanmış durumlu çağrıları içerir; in-flight, bozuk metrik veya başka run eklenmez. `model_call_latency` aynı güvenilir run'ın yalnız başarılı çağrılarından S1/S2 için örnek sayısı ve en yakın-sıra p50/p95 (milisaniye) üretir. En çok 256 başarılı çağrı işlenir; daha fazlasında tüm yüzdelik raporu `null`, çağrı toplamları ise ayrı kalır. Sıfır çağrıda yüzdelikler `null` olur. Tek/az çağrıda p95 matematiksel olarak hesaplanır ama kararlı performans iddiası değildir. Model çağrısı gecikmesi pin/import/transfer içerebilir; insan onayı, tarayıcı ve ağ dahil uçtan uca görev süresi değildir. Canonical sözleşme `schemas/task_progress.schema.json`, açık sentetik örnek `examples/task_progress.json` içindedir. Migration yoktur; mevcut kalıcı state/snapshot/model-call kayıtları okunur.

- Session/run/task/runtime/step/state-version/snapshot digest bağları denetlenir. Eksik veya tutarsız durumda evre tahmin edilmez. Ham state, prompt, model yanıtı veya private dosya içeriği eklenmez.
- In-flight model çağrıları DB'de geçici `error` başlangıç kaydı taşıyabilir; `latency_ms` tamamlanmadan listeye alınmaz. Gösterilen hata yalnız bitmiş çağrı kaydıdır.
- Toplam süre job başlangıcından son örneğe kadardır, insan onayı ve resume sonrası önceki pause süresini içerir. Terminal/paused durumda son job güncellemesinde donar. Negatif/bozuk/naive timestamp için sayı uydurulmaz.
- Arayüzde waiting_approval/paused/terminal job durumu ham phase'den önce gelir: EXECUTE kaydı varken onay bekleyen iş için “tıklanıyor” denmez. Ayrı son kayıtlı evre yine gösterilir. Yüzde, ETA veya simüle ilerleme yoktur; kısa evreler polling arasında geçebilir.
- Eski backend `progress` sağlamadığında arayüz bunu açıkça belirtir. Dil değiştirmek, panel açmak veya ölçüm okumak eylem onayı değildir.

Varsayılan Development ekranı, son görev varsa onun sunucu durumunu, doğrulanmış son kalıcı evresini ve örneklenmiş toplam süresini üstte ayrı bir kartta gösterir. `waiting_approval` işte onay beklendiği belirtilir ve Görevler ekranına yönlendirilir; kart eylemi otomatik onaylamaz. Süre 1 saniyenin altında milisaniye, üstünde saniye olarak gösterilir. Kayıt yoksa kart oluşturulmaz; evre veya süre yoksa `—` görünür. Bunlar görev tamamlama yüzdesi, ETA veya uygulama sonucu değildir.

## CPU hazırlığını görsel gözlemle paralelleştirme

Önceki görev-içi model reuse formun ikinci kararını hızlandırmıştı; görsel görev hâlâ Bonsai bittikten sonra Decider'ın tüm Python/model yüklemesini bekliyordu. Yeni `--reuse-decider` vision yolu Decider'ın **yalnız CPU/RAM hazırlığını** Bonsai gözlemiyle paralel başlatır. Model/checkpoint/tokenizer/dependency hash kontrolleri atlanmaz; ilk gözlem/karar sonucu cache'lenmez.

Worker'ın private `prepare_cpu` işlemi gerçek inference yapmaz ve hazırlık öncesi/sonrası CUDA context'in başlatılmadığını denetler. Yanıt request_id/deployment digest ve CPU-ready alanlarına bağlıdır. Pinned Decider aynı bf16/eager modeli CPU'ya yükler; Bonsai çağrısı bitip server kapanmadan GPU'ya taşıma veya Decider forward yapılmaz. Sonra güncel capture/scene üzerinden normal sembolik karar ve tek exact-action onayı gerekir.

Pause, hata, ret, takeover ve job sonu hazırlık task'ını iptal/drain eder, worker'ı kapatır. GPU modelleri aynı anda resident değildir; sonraki görev yeniden hazırlanır. Prewarm kapalı worker idle sınırı 75 saniyedir; CPU prewarm açıkken worker sınırı parent idle süresini en az 30 saniye aşar. Uzun veya beklenmedik worker kaybında görev soğuk CPU hazırlığına dönebilir; pin denetimi ve taze karar korunur. CPU hazırlığı ek RAM/CPU kullanır; bu genel kaynak veya p95 garantisi değildir.

Gerçek UI A/B koşusunda görev başlangıcından onay hazır olana kadar **30,58 → 21,51 saniye** ölçüldü; yaklaşık %30 azalma. İki gerçek model, tek test onayı, aynı rendered canvas ve bağımsız SAVE doğrulaması korundu. Bu tek çift yerel ölçümdür; anlık çalışma veya tüm görevler için garanti değildir. Aynı koşuda Decider model-call kaydı 12,12 → 2,43 saniyeye indi; paralel CPU hazırlığı bu son çağrının dışında olduğundan toplam kazanç için job süresine bakılmalıdır. İlk Bonsai hash/yükleme/görüntü üretimi hâlâ zaman alır. Kanıtlar ve kapsam [STATUS](STATUS.md) içindedir.

## Model kayıtlarındaki etkinlik

Registry `enabled=0`, modelin genel kullanıma etkinleştirilmediğini belirtir; process veya GPU sağlığını belirtmez. Pilot, açık yapılandırılmış pinned `EXPERIMENTAL` deployment üzerinden gerçek model çağrısı yapabilir. Bu kayıt genel promotion değildir. Arayüz registry etkinliği, oturum motor yapılandırması ve tamamlanmış gerçek model çağrılarını birbirinden ayırır; hiçbir düğme model etkinliği/promotion değiştirmez.
