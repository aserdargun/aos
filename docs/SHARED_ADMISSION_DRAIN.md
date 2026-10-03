# Ortak admission drain — kaynak adayı

**3 Ekim 2026, 09:51:49 UTC:** scheduler ve Scientist Lab için ortak drain,
son seal ve kalıcı salt okunur receipt yolu kaynakta uygulandı. Bu belge gerçek
servis teslimi, fiziksel cleanup, native GPU dışlaması veya ortak GPU kabulü
iddia etmez. Güncel test kanıtını [STATUS](STATUS.md) kaydından okuyun.

## Exact istek ve iki kapanış adımı

Authenticated console'da `POST /api/shared/drain` gövdesi tam altı alan taşır:
`request_id`, `session_id`, `runtime_id`, `owner`, `lease_id`, `generation`.
`owner=AGENT`; session, runtime, lease ve generation aynı çalışan controller'ın
güncel kimliğiyle eşleşmelidir. Ek alan, duplicate JSON anahtarı ve query
parametresi kabul edilmez. İlk istek kapanışı kendine bağlar; başka bir exact
istek aynı process içinde onu değiştiremez.

Ortak controller lock altında scheduler ve Lab yeni başlangıç admission'ını
kalıcı olarak process için kapatır. Scheduler reservation, onay, bitmemiş görev
ve pending/uncertain input; Lab aktif kontrol/client, uncertain action ve
pending/approved/intent kayıtları bağımsız blocker olarak okunur. Gözlem hatası
kapalı kalır. Busy desktop işi otomatik iptal edilmez; blocker çözülmeden
`local_controls_drained=true` dönmez. Normal drain, gerekli Lab cleanup/control
işlemlerine izin vermeyi sürdürür; bunlar mevcut yetki kontrollerini aşmaz.

`POST /api/shared/drain/seal` aynı altı alanı ister. Önce admission kapalı ve
yerel blocker listesi boş olmalıdır. Ardından Lab'ın cleanup dahil bütün yeni
kontrolleri geri açılabilir bir API olmadan process içinde mühürlenir;
`cleanup_controls_closed=true` gözlemi kaydedilir. Generic resume/release,
scheduler'ın ortak latch'ini açmaz. Seal, broker'daki uzaktaki işin durduğunu
veya GPU'nun bırakıldığını kanıtlamaz.

## Kalıcı receipt ve salt okunur inceleme

Her iki POST `SavedSharedDrainObservation` döndürür: `receipt_id`,
`receipt_sha256`, `receipt`. Receipt, `SharedDrainReceipt` sürümünü, gerçek
backend `ProcessIdentity` kaydını ve exact isteğe bağlı gözlemi taşır. Canonical
JSON aynı session'ın trajectory DB'sindeki `desktop_events` tablosuna
`kind=shared_admission_drain` ile yazılır. Aynı son canonical payload yeniden
yazılmaz; değişen gözlem yeni event üretir. Receipt hash'i canonical receipt
gövdesinin digest'idir; event kimliği ayrı DB referansıdır.

`GET /api/shared/drain/receipts/{event_id}` authenticated, query parametresiz
salt okunur readback'tir. `aos.shared_drain.read_shared_drain_receipt` bağımsız
bir SQLite connection üzerinde exact session/event/kind, bounded canonical
payload ve isteğe bağlı `expected_sha256` denetler. Bağımsız okuyucu connection'ı
salt okunur açmalıdır; helper migration, replay, receipt yazımı veya yeni
controller oluşturmaz.

Latch ve seal process-local durumdur; DB receipt kalıcı audit kanıtıdır.
Receipt başka process'te latch kurulmuş olduğunu veya eski lease/generation'ın
hâlâ güncel olduğunu göstermez. Restart sonrası admission kapanışı bu eski
kayıttan otomatik geri yüklenmez. `gpu_release_verified`,
`remote_jobs_stopped_verified` ve `native_gpu_excluded` her zaman `false` kalır.

## Host bileşimi ve kalan kabul

Peer/host, receipt'in process kimliğini güncel `SharedServiceBinding` içindeki
exact unit, invocation, process ve cgroup'a ayrıca bağlamalıdır. Yerel drain
receipt'i tek başına `SharedCleanupProof` değildir. Fiziksel owned runtime ve
token temizliği, Scientist'in uzaktaki çözüm/kapanış kanıtı, sürdürülebilir native
GPU dışlaması ve sonlu activation/cleanup yetkisi bağımsız doğrulanmalıdır.
Scientist ortak GPU kabulünün tek yürütücüsüdür.

Varsayılan native kullanıcı servisine müdahale edilmedi; broker dışında yeni
GPU işi kabul edebilen değişmemiş default UI için sürdürülebilir dışlama açık
kalır. Önceki v4/227-source inert template ve gerçek boş workspace makbuzu
tarihli hazırlık kanıtıdır. Yeni drain kaynaklarını kapsamaz; güncel runtime
kaynak kimliği veya yeni workspace benimseme yetkisi olarak kullanılamaz.
Yeni kaynak/config incelemesi gerekir. UI dosyaları yeniden derlenmedi veya
deploy edilmedi; JSON checkpoint değişikliği çalışan UI'nin tazeliğini kanıtlamaz.

Kaynaklar: `src/aos/shared_drain.py`, `src/aos/desktop_tasks.py`,
`src/aos/scientist_lab_service.py`, `src/aos/desktop_console.py`.
[Manager sınırı](SHARED_DESKTOP_MANAGER.md), [tek kabul kaydı](RELEASE_ACCEPTANCE.md).
