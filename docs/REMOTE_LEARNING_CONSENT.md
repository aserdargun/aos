# W4 — Göreve bağlı yerel metadata izni

`aos.remote_learning_consent`, zaten oluşturulmuş bir `browser_remote_routes` run'ı için kayıtlı profil, exact sıralı rota planı, task/runtime bağı ve profilin istenen S1/S2 rollerini tek audited SQLite snapshot'ında doğrular. CLI yalnız yerel operatörün açık `--attest-data-rights` beyanıyla bir aday hash'i verir. Aynı komut, `--confirm-sha256` ile exact hash tekrar verilirse sahibi dışında okunamayan `0600` dosyaya immutable kayıt yazar. Kayıt anında kaynak ve bağ yeniden denetlenir. Süre en fazla 24 saattir; kayıtlı profilin retention sınırı taşınır.

Varsayılan `--scope remote_route_model_metadata_only` rota davranışını korur. Ayrı `--scope remote_static_model_metadata_only`, aynı iki aşamalı özel izin store'u ve retention sınırıyla yalnız exact `browser_remote_static_assets` run'ına bağlanır; rota ve statik izinleri birbirinin poller'ında kullanılamaz. Yeni static-plan backend oturumunda authenticated Tasks aynı kayıt/bağlama panelini görev türüne göre seçer; çalışan eski oturum sıcak yükseltilmez. [Statik CLI ve scheduler akışı](REMOTE_STATIC_LEARNING_STREAM.md).

`--scope remote_form_model_metadata_only` yalnız exact temel `browser_remote_form` run'ına bağlanır. Durum planlı altı-onaylı run için ayrı `--scope remote_form_state_model_metadata_only --selected-state-plan-sha256 <exact-hash>` gerekir; state hash'i izin kaydının parçasıdır. Rota/statik/JSON ve temel/durum form izinleri birbirinin poller'ında kullanılamaz. Yeni temel veya durum-planlı backend oturumunda Tasks, ilk giriş onayı beklerken aynı iki aşamalı kaydı ve ayrı exact attach'i sunar; scheduler onay öncesi/settle noktalarında poll eder. CLI alternatifi korunur; Cookie bağlı form kapalıdır. [Form metadata sınırı](REMOTE_FORM_LEARNING_STREAM.md).

```sh
.venv/bin/python -m aos.remote_learning_consent \
  --database data/<private-trajectory.sqlite> --run-id <bound-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-plan-sha256> \
  --roles system1 system2 --expires-at <UTC-ISO8601-plus-00:00> \
  --attest-data-rights --store data/remote-learning-consents
# Hash ve kapsamı kontrol ettikten sonra aynı komuta --confirm-sha256 <preview-hash> ekleyin.
```

Bu **CLI çağrısını yapan yerel operatörün beyanıdır**; CLI insan kimliğini veya sitedeki hukuki veri hakkını bağımsız doğrulamaz. Model, profilin `data_rights_ref` alanı veya web sayfası bu komutu tetikleyip yetki üretemez. Kayıt yalnız içeriksiz model metadata kapsamını ifade eder; URL/DOM/prompt/response, ekran görüntüsü, sırlar, ham içerik, dataset, training ve promotion yasaktır. Şema `external_rights_verified=false`, `raw_content_allowed=false`, `training_authorized=false`, `promotion_authorized=false` değerlerini sabitler. İzin dosyası özel `data/` altında kalır; kaynak paketine eklenmez.

Yeni remote-routes backend oturumunda aynı iki aşamalı kayıt **authenticated Tasks panelinden** de yapılabilir: ilk rota onayı beklerken S1/S2 rollerini ve yerel hak beyanını açıkça seçin, 15 dakika geçerli içeriksiz izin hash'ini önizleyin, exact hash'i tekrar girerek özel kaydı oluşturun. Kaydedilen hash ayrı **Attach metadata recording to this run** eylemiyle göreve bağlanır; kayıt tek başına collector'ı başlatmaz. `POST /api/tasks/remote-learning/consent` yalnız aktif job ID, sıralı roller, canonical UTC bitişi ve açık beyanı alır; `confirm_sha256` eklenirse exact kaydı yeniden kaynak denetimiyle yazar. Profil/plan/DB/store yolları istemciden alınmaz. CLI alternatifi korunur. UI oturumu yerel operatörün hukuki hakkını bağımsız ispatlamaz.

Bu modül kendi başına **collector değildir**; `remote_learning_source` raporunun `collection_authorized=false` alanı değişmez. Ayrı [CLI poller ve authenticated scheduler hook'u](REMOTE_LEARNING_STREAM.md), yalnız exact run/profil/plan/roller/expiry kaydını kullanarak içeriksiz adayları görev sürerken append-only outbox'a alabilir. Dar managed-session retention süpürücüsü aşağıda açıklanır; genel izin/hak taraması ve gerçek-site hak/hesap kanıtı hâlâ açık W4 kabul kapılarıdır.

**Açık yerel iptal:** host operatörü exact izin hash'ini iki kez vererek append-only `0600` iptal kaydı oluşturabilir ve aynı komutta belirtilen özel outbox tabanındaki yalnız o izne bağlı SQLite dosyasını mantıksal olarak silebilir:

```sh
.venv/bin/python -m aos.remote_learning_lifecycle \
  --consents data/remote-learning-consents \
  --consent-sha256 <exact-consent-sha256> \
  --confirm-sha256 <same-exact-consent-sha256> \
  --outbox-dir data/remote-learning-outbox
```

İptal kaydı oluşturulduğunda yeni poll ve aynı iznin yeniden kaydı reddedilir. Devam eden poll, izin dizininin paylaşımlı kilidini bırakmadan bitmeden iptal kaydı yayınlanmaz; ardından yalnız exact hash-bağlı özel store silinir. Silme başarısızsa iptal yine kalıcıdır ve aynı komutla yeniden denenebilir; çıktı alınmaması temizliğin tamamlandığını kanıtlamaz. Başka outbox kökleri, yedekler, trajectory DB veya dosya sisteminde fiziksel secure erase bu komutun kapsamı değildir. Harici hak doğrulaması ve gerçek-site W4 kabulü hâlâ açık.

**Saklama sınırını uygula:** `aos.remote_learning_retention` yalnız çağrıldığı anda verilen özel kökü denetler. İzin süresi `expires_at` en fazla 24 saat olduğundan metadata için sınır, kaydın izin bitişi **artı** profildeki `retention_days` değeridir. Vade gelmeden hiçbir outbox silinmez. Vade gelince ayrı `automatic_remote_metadata_retention_expiry` marker'ı `local_operator_attested=false` ile yazılır; manuel operatör beyanı taklit edilmez. Önceden manuel/otomatik iptal edilmiş kayıtların yarım kalan exact purge'u da yeniden denenir.

```sh
.venv/bin/python -m aos.remote_learning_retention \
  --consents data/remote-learning-consents \
  --outbox-dir data/local-app-v1/<exact-session>/remote-learning-outbox
```

Komut yalnız **verilen** outbox köküne dokunur; başka managed session kökleri ve yedekler taranmaz. Rapordaki `outbox_absent_count`, verilen kökte bulunmayan izinleri sayar ve başka köklerde kopya olmadığını kanıtlamaz. `failed_consent_sha256` doluysa işlem exit 1 ile biter; marker kalır ve yalnız exact dosya sorunu giderildikten sonra tekrar denenebilir.

Yeni `aos-v1` managed backend'i ayrıca yalnız pinli `data/local-app-v1/app-<id>/store.sqlite` oturumundan başlatıldığında aynı sweep'i özel `app-*` oturum dizinleri için açılışta ve saatte bir çalıştırır. Her dizin owner-only `0700` olarak yeniden denetlenir; bozuk/symlink oturum atlanıp genel hata kaydı üretilir. Silme yine yalnız exact bağlı SQLite ve izin marker'ına dayanır; tek oturumdaki hata diğerlerini durdurmaz. Eski canlı backend hot-upgrade olmaz; yeni kod sonraki güvenli managed başlangıçta yüklenir. Makine kapalıyken saatlik çalışma veya kapalı dönem için duvar-saati SLA'sı yoktur; diğer özel kökler, yedekler ve fiziksel secure erase kapsam dışıdır.

Son `ok` veya `incomplete` managed taramanın ardından iki saat yeni deneme kaydedilmezse backend'in authenticated durumu `stale` olarak döner; önceki session/purge/engel sayıları `null` olur. Hem monotonic process süresi hem UTC duvar süresi kullanılır; böylece askıya alınan süreçte eski tarama sonucu güncelmiş gibi görünmez. `stale` yeni taramayı kendi başına başlatmaz veya gizli store ayrıntısı vermez; zamanlayıcı/process incelenmeli, sonraki tarama yeniden kendi gerçek durumunu kaydetmelidir.

`stale` ve `unavailable` durumlarında session, purge ve başarısızlık toplamları `null` döner; eski sıfır sayısı güncel temiz sonuç olarak yorumlanmaz.

Makine yeniden açıldığında backend başlamadan aynı özel managed session köklerini elle taramak için:

```sh
.venv/bin/python -m aos.remote_learning_retention \
  --consents data/remote-learning-consents \
  --sessions-root data/local-app-v1
```

Bu komut eksik kökte veya herhangi bir exact session/store hatasında nonzero döner; `failed_sessions` ve `failed_consent_sha256` alanları özel kapsam içindeki engelleri gösterir. Otomatik saatlik döngü sweep hatası veya durum bildirim callback'i hata verse de sonraki taramayı dener; callback hatasını özel ayrıntı dökmeden stderr'e bildirir, fakat bozuk callback telemetri güncellemesini garanti etmez. İptal edildiğinde durur. Bu yeni backend kaynak davranışıdır; canlı eski process'in değiştiğini veya 24 saat çalışan bir servis kabulünü kanıtlamaz.

Yeni backend `GET /api/retention` için oturum kimlik doğrulaması ister; URL sorgusuyla store seçimine izin vermez. Yanıt yalnız `disabled`/`pending`/`ok`/`incomplete`/`stale`/`unavailable`, son deneme zamanı ve toplam session/purge/engel sayılarını taşır. İzin SHA'sı, özel dizin, ham metadata veya eğitim hakkı içermez. Development ekranı bu **bu-process** özetini gösterir; eski backend 404 döndürürse durumu ayrı bildirir. `ok`, yalnız son taramanın verilen managed köklerde raporlanan temizliği tamamladığını söyler, tüm kopyaların silindiğini veya gerçek-site haklarını kanıtlamaz.
