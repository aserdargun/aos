# Bileşik Türkçe katalog önizlemesi

`aos.goal_plan` bir ila üç mevcut sabit görevi sıralı, salt okunur bir önizlemeye dönüştürür. Genel Türkçe normalizer, serbest planlayıcı, model kararı veya scheduler değildir. Yeni görev, shell, dosya yolu, içerik, lease, onay veya runtime üretmez. Authenticated API ve React Plan paneli aynı önizlemeyi explicit olarak sunar.

```bash
.venv/bin/python -m aos.goal_plan --goal 'hello görevini hazırla'
.venv/bin/python -m aos.goal_plan --goal 'önce hello görevini hazırla; sonra yerel form görevini hazırla'
.venv/bin/python -m aos.goal_plan --goal 'önce görsel save görevini hazırla; sonra hello görevini hazırla; sonra yerel form görevini hazırla'
```

## Tam gramer ve sınırlar

- Tek görev, mevcut [TASK_INTENT](TASK_INTENT.md) kataloğunun tam eşleşmesidir. Çoklu görev tam olarak `önce <görev>; sonra <görev>[; sonra <görev>]` biçimini kullanır. Virgül, örtük `ve`, eksik ayraç, boş parça veya farklı connector tahmin edilmez.
- Connector sözcükleri Türkçe I/İ dönüşümüyle karşılaştırılır; görev parçaları mevcut `preview_plan` fonksiyonuna gönderilir. Tırnak içindeki noktalı virgül parçalanmaz. Path ve tırnaklı içeriğe case dönüşümü, escape açma veya düzeltme uygulanmaz. Yalnız önceden desteklenen `/workspace/hello.txt` yolu ve sabit literal içerik tanınır.
- En çok 3000 karakter toplam, parça başına 1000 karakter ve üç farklı görev vardır. Aynı görevin farklı alias'larla tekrarı da reddedilir. Bu, mevcut [TASK_SEQUENCE](TASK_SEQUENCE.md) unique-kind sınırını korur.
- Destek dışı tek bir parça bütün bileşimi plansız reddeder; tanınan parçalar kısmi çalıştırılabilir sonuç olarak verilmez. Tanınan olumsuzluk/kontrol ifadeleri `negated`; bilinmeyen olumsuz biçimler tam katalog eşleşmediği için `unsupported` olur. C0, DEL ve bidi kontrol işaretleri `opaque_text` olarak reddedilir. Kontrol komutları uygulanmaz.
- Boş, string olmayan veya toplam sınırı aşan giriş hatadır. Geçerli boyuttaki destek dışı giriş, plansız bir rapor döndürür; CLI bu sınıflandırmayı başarıyla raporlar, görev başarısı iddia etmez.

## Kimlik ve yetki

`input_sha256` özgün girişin canonical JSON hash'idir; baştaki/sondaki boşluklar dahil özgün metni bağlar. Her `tasks` girdisi mevcut `PlanPreview` ve katalog doğrulamalı `BoundedPlan` nesnesidir. Parça hash'i connector çıkarıldıktan ve ayraç kenarı boşlukları temizlendikten sonra kalan metne bağlıdır. İki veya üç görevde `sequence`, mevcut `SequencePlan` sözleşmesinin tam sırasını gösterir; tek görevde `null` olur.

`composition_sha256`, kendisi dışında raporun tüm alanlarını bağlar. Typed validator katalog, sıra, duplicate, durum ve hash tutarlılığını denetler. Kaynak metin eldeyse `verify_goal_plan(goal, report)` raporu yeniden hesaplayarak kaynak eşliğini doğrular. Ham kaynak taşınmadığı için typed rapor tek başına input hash'inin gerçekten kullanıcı metninden geldiğini kanıtlayamaz. Hash imza, anonimleştirme garantisi veya yetki değildir; kısa metinler tahmin edilebilir.

`execution_authorized=false`, `live_state_verified=false`, `automatic_replay_allowed=false` ve `requires_action_approval=true` değişmezdir. Sequence önizlemesi admission değildir; mevcut scheduler'ın güncel motor/runtime/lease/generation/policy kontrolleri ve her eylemin ayrı onayı yerine geçmez. Başlatma endpoint'i çağrılmaz; DB, dosya, model veya runtime açılmaz. Ham hedef çıktıya/loga yazılmaz; CLI argümanı yine shell geçmişi ve process listesinde görülebilir, secret girmeyin.

## Authenticated API ve UI

`POST /api/tasks/compound-plan` yalnız `{"goal":"..."}` gövdesini kabul eder;
ek alan, query parametresi, geçersiz JSON/string veya sınırı aşan gövde reddedilir.
Mevcut cookie/Host/Origin sınırları korunur. API yalnız `preview_goal_plan` çağırır;
job seçme, scheduler reservation/admission veya approval işlemi yapmaz. Ham
kullanıcı hedefi response'a eklenmez. Task engine kapalıyken de önizleme alınabilir.

React Plan sekmesinde mevcut **Sabit görev planı** altında **Birleşik görev
önizlemesi** bulunur. Görev metni girilmesi veya sekmenin açılması istek başlatmaz;
**Birleşimi önizle** düğmesi gereklidir. Tanınan görevler sırasıyla scope, bağımsız
verification yöntemi ve ayrı onay sayısıyla gösterilir. Bu önizleme Görevler
panelinin seçimini değiştirmez. Negated/unsupported sonuç hiçbir alt görev planı
üretmez. Giriş değişince eski sonuç temizlenir, bekleyen istek iptal edilir;
sekme kapanınca gelen gecikmiş yanıt kullanılmaz. Kontrol çubuğu kullanılabilir
kalır ve önizlemeden yürütme veya yetki geçişi yapılmaz.

6x ile aynı panelde ayrı **Bu birleşik sırayı başlat** düğmesi vardır; önizleme kendi başına salt okunur kalır. Özgün hedef/katalog hash'i ve güncel kontrol kimliğiyle iki/üçlü sıra kabul edilir; her eylem yine ayrı onay ister. [Açık başlatma sözleşmesi](COMPOUND_EXECUTION.md).

## Doğrulama

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_goal_plan.py -v
```

`schemas/goal_plan.schema.json` canonical typed çıktı; `examples/goal_plan.json` açıkça sentetik örneklerdir. Testler tüm iki/üç görev permütasyonlarını, literal path/content ve tırnak sınırını, olumsuzluk/kontrol, eksik connector, bilinmeyen parça/enjeksiyon, duplicate, input/composition/sıra hash'lerini, yetki genişletmeme ve CLI dosya oluşturmama davranışını denetler. Gerçek Decider/Bonsai veya görev yürütme kabulü değildir. Gerçek koşu sonuçları [STATUS](STATUS.md) içinde ayrı tutulur.

`tests/test_parallel_ui.py` API authentication/source/gövde sınırlarını, no-write
önizlemeyi ve gerçek Docker console + Chromium'da desktop/mobile görüntüsünü,
üçlü tanıma, olumsuzluk, destek dışı giriş ve gecikmiş yanıtın atılmasını sınar.
Browser plugin mevcut olmadığı için depodaki Playwright akışı kullanılır; ekran
görüntüleri `/tmp` alanında kalır. Yeni önizleme panelinin X11 native kabulü [NATIVE_ACCEPTANCE](NATIVE_ACCEPTANCE.md) ile eklendi; yeni sıra başlatma düğmesinin native kabulü ve Wayland etkileşimi ayrı kapsamdır.
