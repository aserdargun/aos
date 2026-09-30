# Mimari

## Bileşen sorumlulukları

| Bileşen | Girdi → çıktı | Sınır |
|---|---|---|
| TaskNormalizer | kullanıcı talebi → canonical goal + success criteria | Türkçe aslı saklanır; dosya adları/alıntılar çevrilmez |
| Supervisor | problem + ilgili kanıt → plan/diagnosis/recovery | Tools doğrudan çalıştırmaz |
| Operator | state + plan → observe/decide/execute/verify | Kontrol döngüsünün sahibi |
| DecisionEngine | kompakt durum + finite options → probability dağılımları | Mapika ayrıntıları adaptör içinde |
| SafetyPolicy | action + yetki + state → allow/deny/approval | Model skorundan bağımsız |
| Executor | doğrulanmış typed action → observation | Yalnız runtime gateway üzerinden |
| ComputerRuntime | lifecycle → runtime handle | Docker ilk sürüm; KVM/QEMU/remote sonraki backend |
| ComputerGateway | authenticated action envelope → tools/desktop | İzole bilgisayarın kontrol yüzeyi |
| StateStore / TrajectoryStore | geçişler ve olaylar → kalıcı kayıt | LLM chat geçmişi state kaynağı değildir |
| Registries / CapabilityRouter | capabilities + kaynak bütçesi → izinli deployment | Model isimleri ajan mantığına yayılmaz |

Supervisor ve Operator iki **mantıksal** ajandır; iki bağımsız masaüstü sahibi değildir. Tek runtime input lease'i, tek writer ve tek aktif action vardır. V0.1'de bir görev yürütülür. Gelecekte her Operator ayrı ComputerRuntime kullanabilir; GPU scheduler servis çağrılarını sınırlar.

## Veri akışı

Normalize edilen görev SQLite'a yazılır. ContextBuilder sadece mevcut adım, son gözlem, güvenlik durumu ve izinli seçenekleri derler. Operator gözlemi sürüm numarasıyla alır. Decider seçenek seçer; policy state'in güncelliğini ve yetkiyi tekrar denetler. Action intent kalıcı olarak kaydedildikten sonra araç çalışır. Yeni gözlem ve verification aynı adım ile ilişkilendirilir. Başarısız veya belirsiz sonuç Supervisor'a, çözülemeyen konu insana gider.

## Interface hedefleri

```python
class DecisionEngine(Protocol):
    async def decide(self, state, questions): ...

class Supervisor(Protocol):
    async def plan(self, problem, evidence): ...

class ComputerRuntime(Protocol):
    async def start(self): ...
    async def stop(self): ...
    async def status(self): ...
    async def screenshot(self): ...

class ModelRuntime(Protocol):
    async def health(self): ...
    async def infer(self, request, deployment_id): ...
```

Bunlar genişletilmiş davranış sözleşmesi hedefleridir. Python implementasyonu `src/aos/` altında typed state, DecisionEngine, Operator, ComputerRuntime/Gateway, policy, persistence ve sınırlı Bonsai Supervisor sağlar. BrowserOperator aynı gateway/state/registry sınırlarını kullanarak ağsız Chromium içinde sabit form görevini yürütür; model yalnız finite symbolic action seçer. DesktopRuntime aynı filesystem gateway'ini pinned Docker/XFCE içinde uygular; ayrı DesktopController ve dar authenticated konsol lifecycle/input ownership sağlar. Bu konsol genel model görev API'si değildir. Türler ve hata tipleri PROTOCOL belgesinden türetilir. Host shell ile agent shell birbirine karıştırılmaz. Modelin önerdiği komut host üzerinde otomatik çalıştırılmaz.

Model katmanı capability ve deployment kimlikleriyle çalışır: reasoning, vision, coding, tool_use, routing, action_selection, risk_estimation. Kapsamlı framework yerine küçük typed bileşenler kullanılır; LangChain/CrewAI/AutoGen çekirdeğiyle mimari değiştirilmez. Skills ve MCP ileride ToolRegistry adaptörleri olabilir, policy'yi aşamaz.

VisionOperator'ın ilk dilimi sabit canvas screenshot'ını native Bonsai'den typed scene'e çevirir; Decider hostun ürettiği symbolic hedefleri seçer. Aynı gateway capture/state/lease kontrolü ve bağımsız application outcome verification uygular. Raw screenshot yalnız yerel artifact'tir; export metadata-only kalır. Semantik DOM hedefi bulunan normal browser görevi vision'a yönlendirilmez. VISION_RUNTIME bu kabulün sentetik canvas/event-dispatch sınırını tanımlar; tam masaüstü veya genel mouse kontrolü değildir.

## Gelecek kapsamı

`DesktopScheduler` tek TrajectoryStore writer'ı ve tek aktif job kullanır. Hello DesktopRuntime'da, browser/vision ise her job için yeni ağsız headless BrowserRuntime/VisionRuntime içinde yürütülür. Bu runtime'lar Docker/noVNC desktop'ının tarayıcısı değildir. Run lease'i desktop kontrol lease'ine, action ise ayrıca kendi execution runtime_id'sine bağlıdır. Gateway gözlem/eylem öncesi iki sınırı da denetler. Model çağrısı async, finite araçlar bounded/senkron kalır. Browser başlangıcı thread'de shield/drain ile yönetilir; cleanup tamamlanmadan kontrol devri bitmez.

Süreli tek-kullanımlık onay gate'i policy ile execution arasındadır. Browser formu fill/submit için ayrı onaylar, vision capture/scene-bound click için tek onay üretir; UI yetki üretmez. Kontrol devri model ve owned browser worker'larını drain eder; yeni görev fresh runtime/observation ile başlar. Pause model çağrısını drain eder, eski onayı iptal eder ve canlı execution runtime ile aynı run'ı saklar. Resume yeni lease, gözlem, model kararı ve onay gerektirir; doğrulanmış browser fill yeniden oynatılmaz, vision yeniden capture edilir. Backend restart veya takeover sonrası eski run devam etmez. UI_RUNTIME ayrıntılı sınırları belirtir.

İlk React/Tauri UI bir backend istemcisidir; policy veya model scheduler'ını frontend'e taşımaz. Mevcut DesktopController lease/lifecycle endpoint'lerini kullanır. Çalışmalar/Trace/Registry için TrajectoryInspector, canonical DB'nin ayrı read-only snapshot'ını alır; reconciliation, migration veya kayıt yeniden oynatma yapmaz. Kaynak paneli Docker limitlerini kullanım metriğinden ayırır; ayrı browser/native model kaynaklarını ölçmez. UI_RUNTIME hello/browser/vision görev entegrasyonu ve açık native E2E sınırlarını tanımlar.

Developer / Office / Research image profilleri, çoklu bilgisayar, remote runtime, kaynak scheduler, adapter factory ve learned router genişleme noktalarıdır. İlk sürümde her biri ayrı servis olarak kurulmaz. Yerel inference ile internet kullanan browser görevleri farklı kavramlardır: offline acceptance yerel fixture'larla yapılır, çevrimiçi görevler açık network policy kullanır.
