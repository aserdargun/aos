# Güvenli geliştirme kullanım kaydı

## README model/maliyet özeti — 3 Ekim 2026, 18:34 UTC

AI Scientist README'si biçim örneği olarak salt okunur incelendi; onun sayaçları
ve maliyeti AOS'a aktarılmadı. AOS-owned metadata yeniden toplandı: 95 oturum,
166 UTC saat kovası, 32.681 sayılan usage olayı; 1.884 değişmeyen bildirim ve
34 inherited metadata kaydı dışlandı. Bu gözlemde anomaly yoktur; dosyalar
arasında atomic snapshot veya bütün proje kapsamı iddia edilmez.

Sabit cutoff `2026-10-03T18:34:08.162946Z`; son usage olayı
`2026-10-03T18:33:27.325000Z`. Kaydedilen toplam **4.367.291.356** token:
input4.351.340.188, output15.951.168; cached input4.268.718.848 ve reasoning
output5.406.238 alt kümelerdir. Cache-write gözlemi0. Public
[sanitize snapshot](usage_snapshot_20261003.json) yalnız aggregate taşır;
özel mesaj, session kimliği, ham dosya yolu veya credential içermez.

README'de sağlayıcı/model/effort, input/cache/output/toplam, UTC gün ve ayrı
varsayımsal API maliyeti vardır. [Tarife kaydı](usage_prices_20261003.json)
3 Ekim'de [resmî fiyat sayfası](https://developers.openai.com/api/docs/pricing)
ve [GPT-6 Sol modeli](https://developers.openai.com/api/docs/models/gpt-6-sol)
üzerinden bağımsız doğrulandı. **Standard / kısa bağlam senaryosu 1.954,71 USD**;
bu ücret faturası, abonelik tahsisi veya tarihsel gerçek maliyet değildir.
Geçmiş service tier/bağlam/tarife, vergi, araç, bölge, indirim ve donanım giderleri
bilinmiyor. Fiyatlanamayan modeller sıfır maliyetli sayılmaz.

Hesap: `((input - cached_input) × input_rate + cached_input × cached_rate
+ output × output_rate) / 1_000_000`. Decimal aritmetiği kullanılır; toplam
yuvarlanmamış satırlardan hesaplanır. Reasoning yeniden eklenmez. Nonzero
cache-write muhasebesi kanıtlanmadan o satır fiyatlanmaz. Doğrulanmamış sağlayıcı/
model unpriced kalır. Goal sayacı bu hesaba sokulmaz.

Mevcut default runtime DB salt okunur gözleminde `model_calls=0`; tarihsel
runtime toplamı bilinmiyor. Bu local DB sonucu geliştirme sağlayıcı sayaçlarına
eklenmez ve tüm uygulamanın maliyeti0 diye yorumlanmaz.

Saatlik timer active olarak yeniden gözlendi; mevcut özel snapshot'lar korunur.
README bloğu ve UTC gün tablosu aşağıdaki salt-okunur komutla yeniden üretilebilir:

```sh
python -m scripts.summarize_usage data/accounting/YYYY-MM-DDTHH0000Z.json
```

Komut yalnız stdout üretir; kayıt/README/Git/servis değiştirmez. Yeni saatlik
snapshot'ı inceleyip public aggregate ve işaretli README bloğunu birlikte yenileyin;
tarife tarihi güncel değilse yeni resmî inceleme olmadan “güncel fiyat” demeyin.
19 CPU test; subset hesabı, eksik fiyat, invalid sayı/etiket, duplicate model/gün,
sanitize çıktı ve public snapshot–README eşliğini doğrular. Bunlar gerçek fatura
doğrulaması değildir. Tarihsel gözlemler aşağıda korunur.

Bu kayıt yalnız AOS kapsamındaki yerel Codex metadata/token sayaçlarını içerir.
Provider token faturalaması, abonelik, ücret veya bütün proje toplamı değildir.
AOS runtime Decider/Bonsai/Scientist GPU tokenları geliştirme provider
sayaçlarına eklenmez. Prompt, mesaj, credential, account/session kimliği ve
ham session yolu public rapora alınmaz.

## İlk tarihli gözlem

**3 Ekim 2026, 11:53:23 UTC:** salt okunur ilk extraction, 95 farklı AOS-owned
session dosyasında **4.199.908.219 kaydedilmiş token birimi** gözledi.
Bu tarihsel, dosya-başına gözlemdir; canlı veya global atomic sayaç değildir.
İlk AOS session metadata'sı 19 Eylül 18:06:00.680 UTC;
token olayları 19 Eylül 18:08:27.410 → 3 Ekim 11:52:27.453 UTC aralığındadır.
159 UTC saat diliminde kayıt vardı; kayıt olmayan saat/gün sıfır tüketim kanıtı değildir.

Toplam input **4.184.626.451**, output **15.281.768**.
Cached input **4.105.678.848**, input'un alt kümesidir;
reasoning output **5.201.825**, output'un alt kümesidir. Alt kümeler tekrar
toplanmaz. Cache-write alanında gözlenen değer sıfırdır; billing çıkarımı yapılmaz.

| Provider | Gözlenen model | Effort | Kaydedilmiş toplam token |
|---|---|---|---:|
| openai | gpt-6-astra | high | 639599440 |
| openai | gpt-6-astra | max | 72361937 |
| openai | gpt-6-luna | high | 419754962 |
| openai | gpt-6-luna | medium | 10596959 |
| openai | gpt-6-sol | high | 2240446258 |
| openai | gpt-6.1-sol | high | 148820520 |
| openai | gpt-6.1-sol | medium | 668328143 |

Model/effort ilgili olaydan önceki `turn_context` kaydındandır; thread'in son
modeli geçmiş tüketimin tamamına atanmaz. İlk extraction'da 1775 değişmeyen
kümülatif bildirim dışlandı; reset/gap görülmedi. Root goal'ın ayrı kümülatif
token/saniye sayacı bu toplamla toplanmaz; scope/start farklıdır.
Mevcut default runtime DB'de `model_calls` sayısı sıfır olarak gözlendi;
tarihsel runtime toplamı bilinmiyor. Canonical tablo yalnız yerel input/output,
latency ve VRAM alanları taşır; provider billing/cost alanı yoktur.

## Son collector gözlemi

**3 Ekim 2026, 12:10:15.312922 UTC cutoff:** immutable private snapshot
`data/accounting/2026-10-03T120000Z.json`, 95 AOS-owned session dosyasında
**4.213.032.459 kaydedilmiş token birimi** içerir. İlk session ve token olayı
yukarıdaki tarihsel başlangıçla aynıdır; son token olayı 12:10:07.306 UTC'dir.
160 UTC saat dilimi kapsanır. Input **4.197.684.282**, output **15.348.177**;
cached input **4.118.454.656** ve reasoning output **5.219.723** yine alt
kümelerdir. Gözlenen cache-write sıfırdır; bunlar invoice/abonelik/ücret değildir.

Audit 31.522 usage olayı saydı, 1.784 değişmeyen bildirimi ve 34 inherited
metadata kaydını dışladı. Snapshot `partial` durumundadır: ayrı root goal
satırı sabit cutoff sonrasında güncellendiğinden `unavailable_at_cutoff` olarak
dışlandı. Bu temporal anomaly, session token vektörlerinde reset veya gap
gözlendiği anlamına gelmez. Goal sayacı session toplamına eklenmez.
Read-only current-default runtime gözlemi sıfır model call gösterir;
tarihsel runtime tüketimi ve provider billed/subscription maliyeti bilinmiyor.
Dosya-başına başlangıç byte sınırı alınır; kaynakların ortak atomic snapshot'ı yoktur.

İlk saatlik private snapshot tarihsel uygulama audit'ini korur: değişmeyen
kümülatif sayaçlı context bildirimleri başlangıçta yanlış `partial` işaretlendi.
Collector artık bunları kullanım eklemeden dışlar; aynı tarihsel cutoff'un
read-only yeniden kontrolü aynı 4.199.908.219 toplamla anomaly göstermedi.
Eski immutable snapshot değiştirilmedi.

**3 Ekim 2026, 12:17:44.172824 UTC cutoff:** ownership-scoped dedup düzeltmesi
sonrası salt okunur yeniden hesaplama **4.218.512.079** token birimi gözledi
(input **4.203.136.863**, output **15.375.216**, cached input **4.123.716.992**,
reasoning output **5.225.595**, cache-write sıfır). 95 session/160 UTC saat
kapsamında son usage olayı 12:17:36.186 UTC; 31.580 olay sayıldı ve 1.790
değişmeyen bildirim dışlandı. Session-only durum `observed`, anomaly yoktur;
bu yeniden hesaplama goal/runtime DB okumadı veya yeni snapshot yazmadı.
Önceki 12:10 cutoff'u aynı düzeltilmiş collector ile tam olarak aynı
4.213.032.459 toplamı verdi. Eski immutable snapshot'lar korunur;
billing/whole-project kapsamı hâlâ doğrulanmış değildir.

## Collector ve saatlik kayıt

`scripts/record_usage.py` stdlib ile yalnız `session_meta`, `turn_context` ve
`event_msg/token_count` alanlarını işler. İlk metadata dosya sahibini belirler;
owner start öncesi fork olayları ve değişmeyen kümülatif bildirimler dışlanır.
Duplicate anahtarı yalnız aynı metadata session identity'sindeki timestamp ve
usage vektörlerini eşler; bağımsız session'ların eşit olayları ayrı sayılır.
Identity/lineage yalnız internal kalır; rapora yazılmaz. Geçerli duplicate
dosya-başına baseline'ı ilerletir; seen kaydı yalnız workspace ve vektör
denetimi sonrası eklenir. İlk gözlemde inherited baseline eklenmez.
Monotonic artışın `last_token_usage` eşliği ve cache/reasoning subset sınırları
denetlenir. Reset/regression/gap yeniden yorumlanmaz; o aralık atlanıp
`partial`/anomaly olarak kaydedilir. Unknown model/provider/effort güvenli
`unknown` etiketi olur; yeni etiketler trusted kod allowlist incelemesi ister.
Unreadable/malformed/truncated kaynak eksik tüketimi sıfıra dönüştürmez.

```bash
python scripts/record_usage.py
```

Her invocation sabit UTC cutoff seçer ve her dosyanın başlangıç byte sınırını
alır. `data/accounting/` owned `0700`, yeni saatlik JSON `0600`, exclusive ve
fsync'li oluşturulur. Aynı UTC saatte ikinci invocation mevcut snapshot'ı
değiştirmez. Snapshot provider/model/effort, UTC day/hour ve model/hour
vektörleri, kapsam ve anomaly audit'i taşır; raw mesaj veya kimlik içermez.
Bu klasör Git dışında kalır. Collector daemon değildir; timer kurulumu yapmaz.

**3 Ekim 2026, 13:14 UTC:** ayrı [user timer](USAGE_TIMER.md) bu hostta
etkinleştirildi (`active/waiting`, `enabled`); ilk servis çalışması exit0 verdi.
Bir sonraki saatlik tetik 14:00 UTC olarak gözlendi. Bu timer yalnız özel
aggregate kaydeder; public rapor, commit/push veya runtime/model çağrısı yapmaz.
Kaçırılan saatleri backfill etmez; kullanıcı systemd yöneticisi çalışmalıdır.

`--goal-db PATH` AOS root goal metadata sayaçlarını ayrı okur;
`--goal-thread-id ID` yalnız private seçim filtresidir, raporda yayımlanmaz.
`--runtime-db PATH` yalnız seçilen current-default DB için read-only yerel
model-call aggregate'ı verir; eski DB kopyaları toplanmaz. Historical cutoff'tan
sonraki güncel goal satırı o cutoff'a aitmiş gibi sunulmaz.

Public rapor yalnız explicit seçenekle repository `docs/*.md` dosyasına
sanitize edilmiş aggregate olarak yazılır:

```bash
python scripts/record_usage.py --public-report docs/USAGE_ACCOUNTING.md
```

Bu seçenek ilk tarihsel raporu yeni tarihli aggregate raporuyla yeniler;
aynı saatin snapshot'ı varsa tekrar yazmaz. Collector Git staging/commit/push,
model çağrısı veya servis değişikliği yapmaz. Saatlik safe Git otomasyonu ayrı
incelenmiş ve explicit yetkilendirilmiş bir işlemdir.

Doğrulama: sentetik fixture testleri
`python -m unittest tests.test_record_usage -v`.
