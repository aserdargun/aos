# Bonsai structured recovery — ilk kabul

Bu milestone kontrollü `read-before-create` hatasını kurtarır. Eksik hello dosyasına gerçek gateway read yapılır; `ELEMENT_MISSING` sonucu kalıcı kayda alınır. Başlangıç kararı açıkça sentetik test harness'ine aittir, Decider tahmini olarak raporlanmaz. Sonraki tanı gerçek Bonsai, uygulama kararı gerçek Decider tarafından üretilir.

## Yerel hazırlık

```bash
.venv/bin/python scripts/prepare_bonsai.py --download
```

Script yalnız proje `models/` alanına yaklaşık 7.8 GB PQ2_0 ağırlık/Q8_0 projektör ve 146 MB PrismML CUDA arşivi indirir. Model dosyaları resmi HF LFS SHA-256 değerleriyle, arşiv GitHub release SHA-256 değeriyle doğrulanır. Manifest binary/paylaşılan kütüphane dosyalarını ve kullanılan native CUDA/driver bağımlılıklarını hash ile sabitler. Mevcut bağımlılıklar yeniden kurulmaz.

- HF checkpoint, gömülü tokenizer ve projector: `6ed5e12bf84b7a63069882c91dd9e9218647d17b`.
- PrismML release: `prism-b10709-9a9394a`; kod revision: `9a9394a895b96003ca842a6041cb28ac49a108f7`.
- Binary: Linux x64 CUDA 13.3, GNU 11.4.0 ile üretilmiş resmi release; CachyOS CUDA 13.4/driver 615.71.09 üzerinde çalıştırıldı.
- Output şeması ve recovery protocol sürümü deployment digest'ine dahildir. Registry EXPERIMENTAL kalır; active pointer değiştirilmez.

[Resmi demo](https://github.com/PrismML-Eng/Bonsai-demo) Bonsai-2 için PrismML fork'unu gerektirir. Hazırlık demo setup'ını çalıştırmaz; WebUI veya eğitim kurmaz. Kaynaklar: [sabit release](https://github.com/PrismML-Eng/llama.cpp/releases/tag/prism-b10709-9a9394a) ve [sabit HF revision](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/tree/6ed5e12bf84b7a63069882c91dd9e9218647d17b).

## Gerçek kurtarma komutu

```bash
.venv/bin/agentctl recover-hello \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --bonsai-manifest models/bonsai-manifest.json \
  --workspace data/my-new-recovery-workspace
```

Workspace içinde hello.txt bulunmamalıdır. Mevcut dosya silinmez/değiştirilmez; precondition sağlanmıyorsa probe reddedilir. Başarılı çalışmayı tekrarlamak için yeni workspace seçilir. `hello` eski sıfır-System-2 davranışını korur.

Akış: missing read → SUPERVISOR → validated plan → REPLAN → fresh OBSERVE → Decider → POLICY/Gateway → create → bağımsız read → VERIFY. Sadece verification geçtikten sonra escalation `recovered` olur. Timeout, yanlış kanıt referansı, eksik/sırasız plan veya sahiplik değişiminde dosyaya yazılmaz.

Runtime plan kontratı `schemas/supervisor_plan.schema.json`, sentetik örneği `examples/supervisor_plan.json` içindedir. Yalnız `observe_workspace`, `create_authorized_file`, `verify_exact_content` adları bu sırada kabul edilir. Sıra hem JSON grammar hem host policy ile denetlenir. `needs_human=true` ise plan boş olmalı ve run insanda beklemelidir. Geniş System-2 eğitim şeması değiştirilmez; runtime planı eğitim kaydı sayılmaz.

## Servis ve güvenlik

Her S2 çağrısı kendi pinned `llama-server` process'ini açar. Yalnız `127.0.0.1`, boş port ve rastgele API anahtarı kullanılır. Anahtar 0600 geçici dosyadadır. Model alias'ı doğrulanır; yanlış listener yetki vermez. Port yarışı fail-closed olur; mevcut listener durdurulmaz.

Başarılı çağrının bellekteki `last_metrics` alanı toplam `latency_ms` yanında `pin_verify_ms`, `server_start_ms`, `request_build_ms`, `inference_http_ms` ve `response_parse_ms` ayrımını verir. Çağrı başında eski metrik silinir; başarısız çağrı eski başarı ölçümünü yeni ölçüm gibi taşımaz. Bu süreler model çağrısının içindedir; süreç kapatma, görev onayı, tarayıcı hazırlığı ve toplam görev süresi değildir.

Yeni gerçek managed backend, Bonsai pinlerini açılışta **yalnız CPU/disk üzerinde** arka planda tam SHA-256 ile doğrular; model süreci, GPU belleği veya görev açmaz. Aynı `BonsaiSupervisor` instance'ındaki her çağrı pinned model/runtime dosya envanterini, çözümlenen yolları, native kütüphaneleri ve inode/ctime/mtime/size bilgilerini yeniden denetler. Metadata veya dosya kümesi değişmişse tam hash ve native dependency denetimi yeniden yapılır; doğrulama sırasında değişim de reddedilir. Önbellek süreçler arası/kalıcı değildir; başka session veya deployment'a aktarılmaz. Yerel root'un metadata/blok aygıtına müdahalesine karşı kriptografik kanıt değildir. Her S2 çağrısı yine yeni, token'lı tek kullanımlık `llama-server` açıp kapatır. Gerçek aşama ve karşılaştırma ölçümleri [STATUS](STATUS.md) içindedir.

WebUI/MCP açılmaz; istek tools içermez. Thinking `reasoning-budget=0` ve `enable_thinking=false` ile kapalıdır; yalnız kısa tanı/plan saklanır. Çağrı bitince sadece kendi process grubu kapatılır ve anahtar kaldırılır. Decider sonra yüklenir; eşzamanlı residency iddia edilmez.

Context 16384, concurrency 1, GPU layers 99, flash attention açık, output sınırı 512 token, temperature 0.0. 16K buffer kurulumu kısa prompt ile doğrulandı; dolu context kalitesi/limit testi yapılmadı. Bu recovery kabulünde Q8 projektör yüklendi fakat görüntü gönderilmedi. Sonraki ayrı [VISION_RUNTIME](VISION_RUNTIME.md) dilimi aynı pinned artifact'lerle tek sentetik screenshot → scene/action kabulünü gerçekleştirdi; genel vision doğruluğu iddia edilmez.

## İnceleme ve sınırlar

`agentctl export --run-id RUN_ID` ilk başarısız okuma, model kimlikleri, recovery planı ve verification bağlantılarını canonical export'a taşır. Yerel DB/trajectory/model dosyaları kaynak manifest'ine girmez.

`scripts/probe_bonsai.py` yalnız sentetik protokol isteğini inceler; araç kabulü değildir. İlk API schema wrapper hatası şekil doğrulamasında reddedildi ve düzeltildi. Tekrarda plan sırası sapması da host policy tarafından reddedildi; sonrasında sıra grammar'a eklendi. Kontroller gevşetilmedi.

Keyfi shell kurtarması, belirsiz write replay, genel replan döngüsü, vision ve UI takeover kuyruğu kapsam dışıdır. In-flight write uncertain kalır ve körlemesine tekrarlanmaz. Ölçümler [STATUS](STATUS.md) içindedir.
