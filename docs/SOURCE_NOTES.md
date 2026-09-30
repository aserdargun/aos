# Kaynak ve kapsam notları

Ana kaynak: [Yerel Çift Ajan Beyin Fırtınası](chatgpt-conversation://6aae7688-6dd8-83eb-a20d-a3fbeae08913), 19 Eylül 2026. `read_thread` ile iki sayfada 12 konuşma turu incelendi. Son iki uzun kickoff cevabı araçta 20.000 karakter sınırında kesiliyordu; mimari/model/ortam/adaptation kararları ayrı tam mesajlarda da bulunuyordu.

Konuşmadaki attachment/content-reference dosyaları tool çıktısında erişilebilir ek olarak dönmedi. Dolayısıyla bu paket eski ZIP veya SQL'nin birebir kopyası değildir: erişilen kararlara sadık, yeni hazırlanmış belgeler ve doğrulanabilir sözleşmelerdir. Eksik eklerden eğitim parametresi veya model uyumluluğu uydurulmadı.

## Karar izlenebilirliği

| Kaynak tur | Aktarılan karar |
|---|---|
| b6b2d468-eef5-4fb4-8756-b4cf28b49361 | donanım, iki ajan, izole bilgisayar, execution hierarchy |
| b6868a02-c3c8-46ac-87f1-b789f4c0c0f7 | Bonsai vision ve System-1/System-2 ayrımı |
| 93a97e8c-a966-4268-9cf2-a0341b69e420 | yerel Decider, English canonical state, confidence ve loop |
| d5d74f6d-e034-49ad-9866-0adae9751ef4 | registry/deployment, LoRA, delta training, lifecycle |
| 496a6e5f-11f6-4eff-943d-bab0b0458f98 | native host + Ubuntu Docker, gateway, uv/pnpm/Tauri |
| ae33eb3d-e5da-4476-8cbd-363dc223187c | Astra High kickoff, aşamalar ve acceptance |
| d817a74a-1921-4542-8d85-70c6f1e15398 | eğitim belgeleri ve offline learning döngüsü |
| abb3d1a8-9957-49b7-8b2c-368ca71b66a2 | migration/schema/recipes ve verified outcome |
| ae99d307-99fe-46c9-ac3f-df45e114278b | AOS adı ve tek paket |

Yeni mühendislik ayrıntıları (SQL kolonları, schema 1.0, retention/retry/promotion önerileri ve validator) bu paket için oluşturuldu. Bunlar önceden denenmiş ürün davranışı veya önceki konuşmadan birebir alıntı değildir.

## Güncel birincil kaynaklar

- https://huggingface.co/Mapika/decider-2b
- https://github.com/Mapika/decider
- https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf
- https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/tree/main
- https://github.com/PrismML-Eng/Bonsai-demo/blob/main/README.md

Kaynaklar 19 Eylül 2026'da kontrol edildi. Bunlar upstream kimlik ve özellik kaynaklarıdır; yerel benchmark kanıtı değildir. Exact revision ve artifact hash kurulumda tekrar doğrulanır.
