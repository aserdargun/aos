# Staged native Decider entry — kaynak adayı

**3 Ekim 2026, 11:01 UTC:** `src/aos/native_decider_entry.py` ve
`scripts/native_decider_entry.py` kaynakta uygulandı; Astra incelemesi ve7 odaklı
CPU kontrolü geçti. Legacy direct/native girişler, Bonsai ve
çalışan default UI henüz bu yolun kapsamına alınmadı. Gerçek entry config,
inhibit store veya route promotion oluşturulmadı; native/GPU dışlama kabulü yoktur.

## Sabit giriş sözleşmesi

Önerilen private config yolu `REPO_ROOT/data/native-decider-entry-v1/entry-config.private.json`
(bu hostta `/home/cachyos/aos/data/native-decider-entry-v1/entry-config.private.json`),
inhibit scope'u `native-default-v1`, store aynı parent altındaki `store` dizinidir.
Bu yollar öneridir; dosyaların/dizinlerin var olduğu iddia edilmez.

CLI `.venv/bin/python scripts/native_decider_entry.py`, external
`--entry-config-sha256 SHA -- MANIFEST [--idle-seconds=N] [--serve]`
sözleşmesini kullanır. `SHA` ve `MANIFEST` exact pinli girdilerdir; seçenekler
worker'ın tek çağrı veya serve moduyla sınırlıdır. Keyfi argv/env/executable,
model seçimi veya fallback yolu açılmaz. Canonical config içerik kimliğidir;
kullanıcı bakım onayı, activation veya GPU authority değildir.

## Pin, lease ve FD exec

AOS interpreter'ı wrapper'ı çalıştırır. Wrapper config/store/ilgili source ve
model pinlerini doğrular, native lifetime SH lease alır ve verified executable
FD'sini retain ederek unchanged pinned model-venv worker'ına exec eder.
Lexical model-venv yolu `argv[0]` olarak korunur: FD üzerinden exec yapılması
venv prefix kimliğini değiştirmemelidir. Exec sonrasında yalnız read-only lease
FD aktarılır; diğer retained descriptor'lar kapanır. Lease sahibi close-only
kuralına uyar; shared inherited description üzerinde `LOCK_UN` yapılmaz.

Mevcut model worker, model venv ve 197 dependency kontrolü değiştirilmez;
model venv'e AOS kurulmaz. Mevcut offline Hugging Face environment korunur.
Source/config/inode veya executable pin uyumsuzluğu, busy/inhibited store ve
belirsiz lease durumunda worker başlatılmaz. Bu staged yolun varlığı,
instrument edilmemiş legacy worker'ı veya Bonsai'yi dışlamaz.

## Ölçülmüş sınırlar ve sonraki kabul

Root, modelsiz geçici CPU venv'lerinde Python 3.14 ve 3.12.13 ile FD exec'in
venv prefix korumasını doğruladı. Aynı7 entry testi normal filesystem, Btrfs ve
ayrı sentetik Python3.12.13 venv ile geçti; gerçek model worker çalıştırılmadı.
Gerçek model Python executable'ı yaklaşık30,9 MB'dır: entry executable okuması
**64 MiB** ile sınırlıdır. Native inhibit'in source-file kontrolü de explicit
**64 MiB** kabul eder; private config/generic read sınırları gevşetilmez.
17 MiB sentetik kaynak kabulü,64 MiB üzeri pre-intent ret ve her dosya öncesi
özgün deadline kontrolü ayrıca doğrulandı. Bu doküman full
dependency closure attestation'ı veya bütün native giriş yollarının kapsandığını
iddia etmez. Kaynak SHA256
`e3cb8f4aca984f08c8acfdba6c9bc037f8e3a258d9f2c323e441f5b0c3ca6263`;
güncel inhibit SHA256
`8b514849feafc902d76418a7b573eca8890ccccb38db32392c1f71016f1cea55`.
Yeni config bu kaynakları pinlemelidir; önceki primitive kimliği tarihseldir.
Kanıtlar `data/native-decider-entry-20261003/` altındadır. Gerçek public CLI,
olmayan config ile exit1 verdi ve hiçbir worker/store oluşturmadı.

Legacy route promotion yalnız ayrı explicit maintenance, source/config review
ve deployment sonrasında yapılabilir. [Native inhibit](NATIVE_INHIBIT.md) ve
[handover sırası](NATIVE_HANDOVER.md) admission/exclusion sınırlarını korur;
gerçek kanıt [STATUS](STATUS.md) kaydında tutulur.

Scientist shared caller'ın `args.source_files.aos` alanı code closure'dır ve
8 MiB dosya sınırı vardır. Buradaki native entry source map'iyle aynı namespace
değildir:30,9 MB interpreter o code map'ine taşınmaz. Birleşik teslimde code,
binary ve private config pinleri ayrı tutulmalı; farklı tüketicilerin sınırları
kendiliğinden eşit veya64 MiB kabul edilmemelidir.
