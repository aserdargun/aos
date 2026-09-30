# Kalan işler — çalışan ilk genel sürüm

Gözlem tarihi: **30 Eylül 2026**. Bu sıra [ROADMAP](ROADMAP.md) ve kullanıcının “çalışan sistem önce, SWAPP en son” kararı içindir. Buradaki işler eşit büyüklükte değildir; tamamlanmış test sayısından proje yüzdesi veya bitiş saati türetilmez. Development ekranındaki **56/65** dar checklist ve **0/6** gerçek-site kabulü bu kuyruğun tamamlanma oranı değildir.

## Bugün çalışan temel

- Yerel managed AOS, Development varsayılan ekranı, standart tokensiz loopback giriş ve Mac SSH/tarayıcı launcher.
- Gerçek yerel Decider/Bonsai, sabit dosya/tarayıcı/vision görevleri, görünür Ubuntu Chromium/MCP ve bağımsız sonuç okuma.
- İncelenmiş özel belgeler, kapsamlı lexical arama, gerçek Bonsai kaynak alıntısı ve ayrı izinli S1 görev bağlamı/denetim raporu.
- Sentetik owned skill planlama, inceleme, sürüm/seçim/reuse ve çift-rol veri adayı/export; deneysel S1 adapter ve ayrı yeniden yükleme/eşli yürütme.

Bunlar genel hedef yürütücüsü, herhangi bir web uygulamasında ustalık veya tamamlanmış S2 eğitim/promotion sistemi değildir. Kesin kanıt [STATUS](STATUS.md) içindedir.

## Öncelikli iş kuyruğu

| Sıra | İş / mevcut boşluk | Tamamlandı demek için gereken kabul |
|---|---|---|
| 1 | **Açılış ve kurtarma:** temiz kapanmış başarısız startup aynı boot'ta manager kilidini bırakabiliyordu. `recover-clean-exit` ayrı explicit yol olarak uygulandı; 12 CPU negatif/pozitif test geçti. Belirsiz orphan yönetimi hâlâ ayrı. | Exact süreç/workspace/journal/token/container yokluğu, boş port ve immutable audit; hiçbir kill/delete/replay veya otomatik start yok. Gerçek tam manager arıza→kurtarma→yeni giriş kabulü ayrıca gerekiyor. |
| 2 | **Mac kullanıcısının ilk gerçek bağlantısı:** launcher ve tokensiz giriş testli; aserdargun bilgisayarından SSH/tünel kabulü yapılmadı. | Gerçek Mac'te tek komut, mevcut oturumun reuse'u, Development açılışı, Ctrl-C'de yalnız kendi tünelinin kapanması; gerçek SSH failure ve dolu port kontrolü. Mac erişimi gerektiğinden diğer işleri bekletmez. |
| 3 | **Genel ama yetki-sınırlı web görevleri:** mevcut görevler ve iki tam hedef grameri dar/sentetik; keyfi kullanıcının web uygulamasına genel görev çözümü yok. | İzinli profil/skill/parametrelerle iki farklı sentetik uygulama ve birden çok yeni girdi; Bonsai planı → sonlu S1 → executor → bağımsız oracle. Kapsam dışı/stale/güvensiz plan, iptal ve belirsiz POST'ta sıfır replay. Genel unrestricted tool runner yapılmaz. |
| 4 | **S1 ve S2 için görev-içi bilgi:** S1 document context, ayrı S2 extractive answer ve yeni selected-skill S2 context tüketicisi var. Gerçek managed EN/TR selected-skill plan→S1→readback ve revoke-before-fill/replay kabulü sentetik site/belge üzerinde geçti. Genel planlama, birleşik veri toplama, embedding/vector ve bağımsız relevance eksik. | [S2 sözleşmesi](OWNED_SKILL_KNOWLEDGE.md) ile farklı uygulama/hedef ve held-out veri üzerinde genişletilmiş kabul. S1/S2 girdileri ayrı denetlenir. Held-out relevance/abstention ve kaynak değişimi retleri; fixture/model ve bağlam hazırlığı/actual use ayrımı korunur. |
| 5 | **Genellenebilir skill yaşam döngüsü:** owned sentetik recipe/sürüm/reuse var; farklı uygulama ve rollere güvenli parametrik taşıma ve kalite kabulü yok. | İki ayrı uygulama/kapsam için parametre schema, reviewed kaynak, bağımsız skill testi, açık seçim/aktivasyon/rollback; yanlış tenant/rol ve eski kaynak retleri. Başarısız görev → öneri → insan incelemesi → yeni izinli test zinciri; otomatik yetki veya gold üretimi yok. |
| 6 | **S1/S2 veri ve eğitim:** çift-rol adaylar ve S1 deneysel eğitim çalışır; S2 tokenizer/loss mask/trainer/adapter uyumluluğu, bağımsız held-out veri ve QLoRA yolu eksik. | Ayrı hak/redaction/review/export/train izinleri, leakage-safe split; gerçek uygun checkpoint/tokenizer ile S2 loss/adapter smoke ve fresh reload. Model lisansı/uyumluluğu doğrulanmadan GGUF eğitimi veya büyük indirme varsayılmaz. Gerçek kalite/hız metriği ve açık promotion/rollback gerekir. |
| 7 | **Model ve ek ajan değiştirme:** typed engine/registry pinleri var; genel entegrasyon SDK'sı ve farklı S1/S2 backend'lerine taşınabilir kabul seti tamamlanmadı. | S1/S2 ayrı capability/identity sözleşmesi; iki backend'in uyum/iptal/bütçe testleri, explicit seçim ve bilinen iyi rollback. Uzman ajan yalnız typed öneri/artefact üretir; tek runtime input sahipliği korunur. AI-Scientist implementation/API seçimi private fork'ta ayrıca yapılır. |
| 8 | **Tekrarlanabilir genel teslim:** source-only paket ve test havuzu var; temiz makine installer/dependency setup, Wayland/native kabulü ve açık kaynak lisansı eksik. | İzole temiz ortam kurulum/başlatma/kurtarma/smoke, EN/TR kullanım, secret/private artifact dışlama ve dated usage snapshot. Lisansı proje sahibi seçmeden “açık kaynak lisanslı” ilan edilmez; model ağırlıkları ve şirket verisi dağıtılmaz. |
| 9 | **En son SWAPP / gerçek-site W1–W6:** yetkili intranet/tünel/hesap/oracle yok; kabul 0/6. | Exact intranet admission ve profil/tenant/rol, izinli gerçek görev ve bağımsız sonuç; incelenmiş site bilgisi/skill, haklı ayrı S1/S2 dataset, ölçülmüş held-out/promotion ve tekrarlı uçtan uca hız/başarı. Sentetik testlerle kapatılamaz. |

## Çalışma kuralı

1'in CPU/paket dilimi kapandı; 4'te native S2 EN/TR öneri, canonical transport ve unchanged gerçek raporun Chromium doğrulaması geçti. 4'ün gerçek managed selected-skill S2→S1→readback zinciri de geçti. Sıradaki geliştirme 3'ün farklı izinli girdiler/uygulama kapsamı; 4'ün genel bağlam ve relevance işleri açık kalır. 1'in gerçek manager recovery kabulü ayrıca korunur. 2, lisans seçimi, destekli S2 trainable checkpoint ve 9 için eksik dış kanıt diğer uygulanabilir işleri durdurmaz. Her teslimde bu kuyruğun durumu ve STATUS gerçek kanıtla revize edilir; bir dar dilim büyük satırın bütünü tamamlandı demek değildir. Yayımlanmış kaynak paketi installer veya şirket içi uygulama entegrasyonu değildir.
