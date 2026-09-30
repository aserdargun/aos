# Yerel reviewer yetki adaptörü — 7e

**Uygulandı:** `aos.dataset_reviewer.serve_review_session(store, peer, grants, audit)` mevcut owned writer ve bağlı Linux Unix-domain stream socket üzerinde bir prepare/confirm oturumu işler. Kimlik istemci JSON'undan değil kernel peer credential'dan gelir; güvenilir host'un UID/run/decision/expiry allowlist'i uygulanır. Yalnız explicit onay sonrası source/candidate/event bağlı accept veya revoke receipt yazılır. **Eğitim, eligibility, gerçek veri hakkı veya deployment izni verilmez.**

Bu bir **gömülebilir adaptördür**, otomatik açılan daemon veya tamamlanmış reviewer UI değildir. Sonraki 7f [private servis/terminal istemcisi](DATASET_REVIEW_SERVICE.md) explicit CLI entegrasyonu sağlar; HTTP/model tool, hesap oluşturma veya desktop session bağlantısı açılmaz. Testler gerçek yerel socket/subprocess kullanır; candidate, label, grant, beyan ve receipt'ler tamamen sentetiktir. İnsan tarafından gerçek içerik incelemesi yapıldığı iddia edilmez.

## Kimlik ve güvenilir host sınırı

- Yalnız bağlı `AF_UNIX/SOCK_STREAM` kabul edilir. Linux `SO_PEERCRED` ile bağlantı anındaki process UID okunur; TCP, JSON `uid/reviewer_id`, desktop login token'ı veya model output'u yetki olamaz. Kernel sözleşmesi: [Linux unix(7), SO_PEERCRED](https://man7.org/linux/man-pages/man7/unix.7.html).
- Host `ReviewerGrant(uid, frozenset(run_ids), frozenset(decisions), expires_at)` kurar. Boş/yanlış UID, run veya decision kapsamı, sona ermiş grant ve bir UID için birden fazla geçerli grant reddedilir. Allowlist kullanıcı mesajından otomatik üretilmez; root UID'sine özel bypass yoktur. Grant snapshot'ı oturum boyunca sabittir; canlı policy reload/merkezi hesap iptali bu adaptörde yoktur. Host iptal için bağlantıyı kapatmalıdır.
- Adaptör yalnız **sentetik S1 / offline_training_text / synthetic_authored** kapsamını destekler. Accept için mevcut hello/browser-form run, accepted choice label ve exact pre-decision türetme gerekir. S2, vision/image, gerçek provenance veya `user_authorized` talebi bu adaptör tarafından kabul edilmez. Düşük seviyeli 7c receipt API'sinin geniş şeması otomatik olarak geniş adaptör yetkisi anlamına gelmez.
- `store` güvenilir host tarafından sağlanan, idle ve owned `TrajectoryStore` olmalıdır. Adaptör DB açmaz, migration/reconcile yapmaz; kendisi genel snapshot schema denetleyicisi değildir. Host'un writer kurulumu bu sorumluluğu taşır. Başka bir caller transaction'ına commit/rollback yapmaz. Hazırlık tutarlı read transaction'ı kullanır; append kendi `BEGIN IMMEDIATE` transaction'ında source hash'i tekrar kontrol eder.
- Python helper'ları, callback ve `ReviewAuthority` güvenilir host içidir; aynı process'te keyfi Python çalıştırabilen veya DB dosyasını değiştirebilen saldırgana karşı sınır değildir. Peer credential **insan varlığı, niyeti veya belirli uygulama kimliği** kanıtlamaz. Aynı UID altındaki process'ler ayırt edilmez. FD transferi sonrası kimlik gönderene göre yenilenmez; reviewer socket/FD'leri untrusted process'lere geçirilmemelidir.

7f listener private host-owned dizin ve socket izinleriyle kurulur; agent mount/namespace'ine sokulmamalıdır. Process/account izolasyonu ayrıca kabul edilmelidir. Testler dışında gerçek grant veya sürekli listener oluşturulmadı. Farklı gerçek UID/rootless/container sınırı kabulü henüz yoktur.

## İki mesajlı sözleşme

Mesaj framing: 4 byte network-order unsigned payload uzunluğu + UTF-8 JSON. **Toplam mesaj 1 MiB**, oturum **120 saniye monotonic deadline** ile sınırlıdır; parça parça veri göndermek süreyi uzatmaz. Duplicate key, ek alan, bozuk şema veya yanlış sıra reddedilir. SQL/host callback süresi hard real-time olarak kesilmez; yetki insert öncesi tekrar denetlenir.

`schemas/dataset_reviewer_request.schema.json` üç mesaj tanımlar:

1. **prepare_accept**: `run_id`, `label_id`, önceden incelenmiş `source_sha256`, canonical `candidate`, üç explicit beyan: `content_reviewed`, `usage_rights_reviewed`, `redaction_reviewed`. Bunlar inceleme/hak/redaction **beyanıdır**, otomatik doğrulama değildir. Eksik label veya rationale türetilmez. Candidate'ın hak/redaction metadata'sı dışındaki tüm alanları 7d projection ile aynı olmalıdır; metin redaction dönüşümü henüz desteklenmez. `preview-unreviewed-v1` redaction sürümü kabul edilmez.
2. Alternatif **prepare_revoke**: yalnız mevcut accept `receipt_id`. Host grant'inde aynı run ve revoke izni gerekir. Önceki kayıt bütünlüğü/kapsamı kontrol edilir; kaynak değişmiş veya candidate kaybolmuş olsa da revoke mümkündür. Daha önce iptal edilmiş kayıt tekrar iptal edilmez. Yetkili aynı-run reviewer'ın eski accept'i kendisinin yazmış olması şart değildir.
3. Hazırlık cevabı `confirmation_required`, server tarafından üretilmiş tam receipt, exact `event_sha256`, rastgele challenge ve kalan TTL taşır. İstemci **confirm** ile aynı hash/challenge ve boolean `approved` döndürür. `false` hiçbir receipt yazmadan iptal eder. Metadata veya reviewer kimliği confirm mesajında değiştirilemez.

Server receipt ID/tarihi, `reviewer_id=linux-uid-N` ve `authorization_ref=peercred-…` alanlarını kendisi kurar. Authorization ref yalnız audit korelasyonudur, bearer token değildir. Challenge bellektedir; log/receipt'e yazılmaz. Client, hazırlanan receipt'i incelediği source/candidate/scope ile karşılaştırmadan onaylamamalıdır. 7f terminal istemcisi bu kontrol ve explicit hash onayını sunar; desktop UI entegrasyonu yoktur.

Bir bağlantı yalnız bir karar işler ve her sonuçta kapanır. Eski challenge farklı oturumda kullanılamaz; aynı socket'e ikinci confirm yazılması ikinci karar üretmez. Yeniden başlatmada challenge state'i yoktur. Alt katmanın idempotent retry özelliği yeni socket oturumuna yetki devretmez. Aynı kabul binding'inin ikinci kaydı SQL unique kısıtıyla engellenir.

## Süre, tutarlılık ve audit

7c `ReviewAuthority` artık isteğe bağlı monotonic deadline da taşır. Adaptör bunu daima sağlar; grant wall-clock expiry ve monotonic session deadline girişte ve **insert öncesi** kontrol edilir. Hazırlık ile confirm arasında source değişirse append rollback eder. Clock rollback, monotonic oturum süresini uzatmaz; host grant'inin wall-clock süresi ayrı politikadır.

Zorunlu `audit(event)` callback'i `schemas/dataset_reviewer_audit.schema.json` biçiminde şu aşamaları alır: authenticated → prepared → confirmation → committed; alternatif cancelled/denied/delivery_uncertain. Kayıt yalnız kernel UID, grant hash/expiry, authorization ref, event hash, zaman ve sabit neden içerir. Raw candidate/trajectory, challenge, socket/DB path veya exception mesajı içermez. Grant hash'ini doğrulayabilmek için host kendi yetki yapılandırmasını güvenli tutmalıdır.

**Kalıcılık sınırı:** callback'in dayanıklı yazılması host sorumluluğudur; bu sürüm audit dosyası/DB tablosu, fsync veya kriptografik imza hizmeti sunmaz. Commit öncesi callback hatası receipt yazmayı engeller. Commit sonrası callback/cevap hatası varsa mevcut receipt geri alınmaz; mümkünse `delivery_uncertain` döner. Bağlantı kaybında istemci sonucu varsaymamalı, hazırlanmış receipt ID'sini salt okunur denetlemelidir. Callback ile receipt iki ayrı kaynak olduğundan atomik kalıcı audit garantisi yoktur. Socket kapansa da commit gerçekleşmiş olabilir; otomatik yeni onay/retry verilmez.

`dataset_preview` ve `dataset_audit` hâlâ training-ready=false üretir. `peercred-` prefix'i tek başına kimlik kanıtı değildir; eski receipt, dış callback kanıtı veya başka host'un policy'si otomatik doğrulanmış sayılmaz. Hak/redaction/genel veri yeterliliği kapıları kaldırılmaz. 7d'nin `reviewer_authentication_unverified` raporu bu nedenle değişmedi.

## Kabul ve sonraki dilim

```bash
.venv/bin/python -m unittest discover -s tests -p test_dataset_reviewer.py -v
```

Testler gerçek Linux socketpair ve ayrı Python process'iyle pathname socket/UID doğrulamasını; explicit accept/revoke, exact event/candidate/source, yanlış allowlist/scope, JSON kimlik taklidi, süre/monotonic expiry, iptal/replay, source değişimi, bozuk frame, disconnect, audit hatası ve caller transaction korumasını sınar. Diğer gerçek UID ile adversarial deneme veya insan review kabulü yapılmadı. Ölçümler [STATUS](STATUS.md) içindedir.

**7f tamamlandı:** explicit private listener/terminal istemcisi, host-configured grant yükleme, fsync journal ve audit/receipt reconciliation. Adaptör callback'inin kendi başına atomik persistence garantisi hâlâ yoktur; servis commit boşluğunu yeniden okuyarak ayırt eder. Sentetik kapsam korunur; gerçek label/hak üretimi, genel redaction, loss/eğitim ve promotion ayrı kalır.
