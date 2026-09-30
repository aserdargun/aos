# Approve all — tek sabit görev için onay devri

## İstenen kapsam

İnsan onayı bekleme süresini hız ölçümünden çıkarmak için desteklenen görevlerde **Approve all — this task only** UI'de varsayılan olarak seçilidir. Kullanıcı manuel onay için işareti kaldırabilir. Kutunun seçili olması tek başına görev veya eylem başlatmaz; açık Start isteği yalnız seçilen hello, yerel form veya görsel SAVE görevini kapsar. Doğrudan API isteklerinde `approve_all` açıkça gönderilmedikçe manuel onay geçerlidir. Genel ajan, gelecekteki görevler veya sıralı görev listesine sınırsız izin değildir.

Bu belge yeni onay diliminin sözleşmesidir; çalıştırılmış kabul ve performans kanıtları STATUS'ta ayrıca kaydedilir.

## Yetki sınırları

- Delegasyon job/kind/session/lease/generation ve çalıştırma runtime/run kimliğine bağlıdır. 300 saniyelik wall-clock ve monotonic süre sınırı vardır; eylemler mevcut daha kısa deadline ve approval süre sınırlarını korur.
- En fazla hello için bir yaz/oku, form için sırasıyla bir fill ve bir submit, görsel SAVE için bir click kapsanır. Sabit path/içerik/hedef, güncel DOM/capture, policy ve bağımsız sonuç doğrulaması değişmez. Beklenmeyen araç/argüman/sıra veya eski yetki otomatik onaylanmaz.
- Her gerçek eylem için yeni exact-action approval/digest oluşturulur; normal policy, consume ve gateway doğrulamalarından geçer. Önceden verilen sınırlı kullanıcı delegasyonu yalnız insanın ayrı düğmeye basmasını kaldırır; modelin çıktı verdiği her komutu çalıştırma yetkisi üretmez.
- İş bitince, hata/ret, pause, kontrol devri, stop veya logout ile delegasyon biter. Resume yeni delegasyon vermez; manuel onaya döner. Backend yeniden başlatıldığında eski grant canlandırılmaz.
- Seçim localStorage/cookie içine kalıcı ayar olarak yazılmaz; dil tercihiyle karıştırılmaz. Desteklenen backend'de her yeni görev seçimi veya sekme açılışı kutuyu yeniden seçili gösterir; her Start yalnız o görev için yeni grant oluşturur. İş sürerken duraklatma/stop kontrolleri kullanılabilir.

## Denetim kaydı ve hız ölçümü

Mevcut `desktop_events` tablosunda grant yaşam döngüsü tutulur; migration veya model/deployment değişikliği yoktur. Gerçek eylem onayı `human_interventions` içinde `task_scoped_auto_approval` actor'ı, normal approval/action digest'i ve grant/job bağıyla kaydedilir. Bunlar ayrı manuel insan tıklaması olarak gösterilmez.

Performans testi kullanıcı onayı beklemeden **görev başlatma → bağımsız doğrulanmış başarı** süresini ölçer. UI polling/çizim gecikmesini ayırmak için backend job elapsed değeri ve gerçek S1/S2 çağrı süreleri ayrıca raporlanır. Model çağrı süresi hash/import/yükleme/transfer içerebilir; saf forward veya token/s ölçümü değildir. Tek yerel koşu genel benchmark/p95 garantisi sayılmaz.

Gerçek-model UI kabulü yalnız açık opt-in ile çalışır:

```sh
AOS_AUTO_APPROVAL_REAL_TESTS=1 AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1 \
  PYTHONPATH=tests .venv/bin/python -W error::ResourceWarning \
  -m unittest test_auto_approval_real -v
```

Bu test ayrı private Docker/session/workspace kullanır; üç sabit görev için kutuyu açıkça işaretler ve görev başlatır. Kullanıcının canlı oturumunu veya görevlerini onaylamaz. Gerçek model/runtime dosyaları önceden hazırlanmış olmalıdır; test indirme yapmaz. Sonuç kanıtları private `data/auto-approval-real-*` ve `/tmp/aos-auto-approval-*.png` altındadır, kaynak paketine girmez.

Aynı komuta `AOS_DECIDER_PREWARM_TESTS=1` eklenirse [CPU prewarm](DECIDER_REUSE.md) açılır. Test her görevden önce gerçek CPU-ready durumunu bekler ve bu beklemeyi job süresinden ayrı kaydeder; soğuk uygulama açılışının aynı hızda olduğu iddia edilmez.
