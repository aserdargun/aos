# Sıralı Sabit Görevler — 6l

Mevcut hello/form/canvas görevleri **iki veya üç farklı kind** içeren explicit bir sırayla çalıştırılabilir. Bu genel görev planlayıcısı, serbest Türkçe komut, modelden gelen yürütülebilir plan veya checkpoint devamı değildir. Üç görev kendi Operator/gateway/verification döngülerini korur.

## API ve arayüz

Authenticated `POST /api/sequences`:

```json
{
  "plan": {"kinds": ["hello", "browser_form", "vision_canvas"]},
  "lease_id": "synthetic-current-lease-placeholder",
  "generation": 0
}
```

Örnek lease sentetiktir; gerçek kabul mevcut `/api/state` değerlerini gerektirir. Plan alanına path, content, shell, onay veya genel goal eklenemez. Duplicate kind, bilinmeyen kind, iki adımdan kısa/üç adımdan uzun sıra, query ve ek alanlar reddedilir. Planın bütün motorları baştan yapılandırılmış olmalıdır. Aynı session için başka sequence/job/input kabul edilmez; yeni kuyruk yoktur.

`GET /api/sequences` bu backend instance'ının son akışını ve reservation durumunu verir. `/api/tasks` mevcut job/approval alanlarına ek `sequence` ilerlemesini taşır. Görevler panelindeki **Görev akışı** seçimi üç preset sunar: Hello → Form, Form → Görsel SAVE, Hello → Form → Görsel SAVE. API bu üç kind'ın diğer tekrarsız sıralarını da kabul eder. **Sıralı akışı başlat** görevleri admission'a alır; eylem onayı vermez.

## Güvenlik ve ilerleme

- Sıradaki job yalnız öncekinin persisted run/status/state'i succeeded, outcome passed, bağımsız expected/actual eşitlikleri geçerli ve sabit görevin final ölçütü mevcutsa başlatılır. Intent/running/uncertain action varsa ilerlenmez.
- Her job mevcut lease/generation ve runtime sahipliğini tekrar doğrular; fresh observation ve ayrı eylem onayı ister. Browser/canvas için yeni, ayrı ağsız headless runtime açılır; hello aynı izinli desktop workspace'ini kullanır.
- Decider/Supervisor deployment kimlikleri ve browser manifest byte hash'i sıra başında sabitlenir ve her job öncesi yeniden karşılaştırılır. Hash bir yürütme capability'si değildir; actual runtime ayrıca kendi pin denetimini yapar.
- Üç görevlik normal akış dört ayrı onay ister: filesystem.write (dosya zaten eşitse read), browser.fill, browser.submit, vision.click. Onaylar birbirine taşınmaz. Başarısızlık, ret veya expiry kalan job'ları başlatmaz. Önceki başarılar geri alınmaz.
- Take Control/Stop/Restart/Logout/shutdown kalan akışı iptal eder ve mevcut job için eski iptal/drain kurallarını uygular. Adımlar arasındaki scheduling noktasında da reservation ve iptal korunur.
- **Duraklat tüm sıranın kalanını iptal eder.** Yalnız o anda aktif job eski single-job Pause/Resume sözleşmesiyle tutulabilir; Devam et sadece onu taze onayla sürdürür. Sıranın kalanı tekrar başlamaz. Bu bilinçli ilk-dilim sınırı UI'de açık yazılır.

## Kalıcılık ve crash sınırı

Canonical `sequence_start.schema.json` request, `sequence_status.schema.json` durum sözleşmesidir. Şemalar yapısal/enum/unique kind sınırlarını; Python validator ayrıca plan hash/progress bağlarını doğrular. `examples/task_sequence.json` yalnız sentetik schema fixture'ıdır.

Var olan `desktop_events` tablosuna `kind=task_sequence` ile typed durum snapshot'ları yazılır: admission, job bağlantısı, bağımsız doğrulama ve terminal sonuç. Payload en fazla üç job kimliği ve sabit kind içerir; token, raw model yanıtı veya action argümanlarını kopyalamaz. Yeni migration yoktur; 0001–0008 değişmez. Event log imzalı/tamper-proof veya transaction'lar arası exactly-once protokolü değildir.

Orchestrator bellekteki reservation'ı kullanır; **event log başlangıçta replay edilmez**. Kaynak DB normal startup reconciliation'ıyla eski job'lar cancelled, approval'lar revoked olur; belirsiz eylemler tekrar edilmez. Eski event'te running yazması canlılık veya devam yetkisi değildir. `GET /api/sequences` eski backend akışlarını sahiplenmez; yeni instance'ta sequence=null döner. Gerçek subprocess crash testi bu sınırı doğrular. Çökme sonrası güvenli devam, checkpoint/effect doğrulaması ve orphan runtime incelemesi ayrı kilometre taşıdır.

## Doğrulama

```bash
AOS_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_task_sequence.py -v
AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_desktop_task_ui.py -k sequential_tasks -v
AOS_REAL_BROWSER_TASK_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_desktop_task_ui.py -k three_task_sequence -v
```

İlk iki komutta model fixture'dır; gerçek Chromium/Docker kullanılması gerçek model sonucu değildir. Son komut mevcut pinned Decider/Bonsai ile GPU inference çalıştırır; kalıcı kanıt ignored `data/sequence-real-*` altında kalır. Eğitim, yeni model indirme, review/eligibility veya promotion yoktur. Güncel sonuçlar [STATUS](STATUS.md) içindedir.
