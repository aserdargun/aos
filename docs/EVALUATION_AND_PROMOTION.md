# Evaluation ve promotion

## Karşılaştırma düzeni

Base ve candidate aynı görev sürümü, seed, izin, runtime image, tool bütçesi ve donanım koşullarında çalışır. Warm/cold latency ayrılır. Held-out test ve benchmark training/replay'e katılmaz; tuning yalnız validation üzerinde yapılır. Az örnekte güven aralığı ve belirsizlik raporlanır; sonuç istatistiksel üstünlük gibi sunulmaz.

Ölçümler: verified task/action success, actions/task, retries, stuck rate, Supervisor/vision calls, unnecessary escalation, human intervention, S1/S2/tool latency p50/p95, duration, tokens, peak VRAM/RAM/CPU. System-1 için accuracy/NLL/Brier/ECE/AURC ve abstention quality; System-2 için diagnosis correctness, executable plan, recovery success ve regression slices.

## Başlangıç kabul kapıları

`benchmarks/promotion-policy.json` sayıların ilk önerisini içerir. Eşikler ilk gerçek baseline'dan önce review edilerek sabitlenir; sonuç görüldükten sonra candidate'i geçirmek için değiştirilmez.

- Tüm package/unit/integration kontrolleri geçer; gerçek hedef ortam kanıtı vardır.
- Credential leakage, yetkisiz dış etki veya HOST policy bypass sayısı sıfır olmalı. Bu test sonucu mutlak güvenlik garantisi değildir.
- En az 30 bağımsız held-out görev ve kritik slice başına en az 5 görev: ilk engineering minimum, istatistiksel yeterlilik iddiası değil. Veri azsa promotion blocked.
- Verified success baseline'a göre en fazla 2 yüzde puan gerileyebilir; latency p95 en fazla %10 artabilir; tepe VRAM measured budget altında olmalı. Hedef iyileşme ve kabul tradeoff'u raporda belirtilir.
- Rollback artifact/config ve activation rehearsal başarılı olmalı; exact deployment hash evaluation ile eşleşmeli.

## Açık promotion

EXPERIMENTAL candidate → benchmark → VALIDATED → yetkili promotion kararı → kontrollü activation. Online self-training yok; evaluator ACTIVE pointer'ını kendiliğinden değiştirmez. Başarısız aday REJECTED ve nedeni kaydedilir. İlk model activation bile compatibility/health ve kullanıcı scope'una bağlıdır.

## Deney hedefi

Decider-first ile her adımda Bonsai çağıran kontrol mimarisi aynı tool/policy/task bütçesiyle karşılaştırılır. Daha az Bonsai çağrısı tek başına başarı değildir; verification, toplam süre ve görev başarısı birlikte değerlendirilir. İnsan yardımını gizleyerek otonom başarı yazılmaz.
