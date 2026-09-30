# Owned adapter: ayrı izinli tek görev

Bu dilim, incelenmiş owned episode'dan üretilen S1 adapter'ını yeni girdili bir seçilmiş-skill görevinde gerçekten kullanmak içindir. Yalnız sentetik owned-reuse oturumunu destekler. Gerçek hedef-site, bağımsız held-out kalite veya üretim promotion değildir. Ölçülmüş kabul ve başarısız denemeler [STATUS](STATUS.md) içindedir.

## Kullanım

1. Yeni owned-reuse oturumunda opted-in Bonsai → Decider görevini tamamlayın; ayrı S1/S2 review, export, conversion, tokenizer ve deneysel adapter eğitimini doğrulayın.
2. Tasks → saved experiment altında **Experimental adapter task** bölümüne yeni bir sentetik vaka ve mesaj girin. Gizli bilgi kullanmayın.
3. **Preview adapter task** exact eğitim izni, rapor, artifact, base deployment, runtime worker ve normal selected-skill preview pinlerini birleştirir. Preview model başlatmaz.
4. Varsayılan kapalı ayrı çalışma iznini işaretleyip **Start adapter task** seçin. Eğitim izni bu iznin yerine geçmez. Altı eylemin her birini Tasks içinde ayrıca onaylayın.
5. Tamamlandıktan sonra **Audit adapter task** geçmiş kullanım kanıtını ve güncel kaynak kullanılabilirliğini ayrı gösterir. Yeni görev daima yeni exact onay ister.

API `/api/tasks/owned-episode/runtime-preview`, `runtime-start` ve `runtime-audit` üzerindedir; authenticated exact body/lease/generation sınırları korunur. Start yalnız host tarafından yeniden oluşturulan preview hash'ini ve `experimental_runtime_authorized: true` değerini kabul eder. Genel manual start API'sine adapter eklemek bu izni atlatmaz.

## Kimlik ve yürütme

- Normal v1.3 preview hash'i, canonical `owned_adapter_runtime_admission` içine bağlanır. v1.5 final preview ve execution hash'i ayrıca admission hash'ini içerir; hash döngüsü yoktur. Planning v1.4 + adapter birleşimi desteklenmez.
- Immutable execution bundle, `adapter-admission.json` dosyasını exact envanterle saklar. V1.5 adapter pinini kaybedip eski schema sonucuna düşemez.
- Scheduler base engine'i değiştirmez. Yalnız bu görevin Operator'ına ayrı `OwnedAdapterDecisionEngine` verilir; kendi base reusable child'ı önce boşaltılır. Diğer oturumlar/süreçler durdurulmaz.
- Native worker, CPU hazırlığından sonra CUDA belleğini denetler ve rank-4 last-MLP hook'unu **ilk gerçek karar öncesinde** bağlar. Gizli base kararı, otomatik fallback, adapter stacking veya download yoktur. Altı request, 75 saniyelik idle ve bounded JSON protokolü kullanılır.
- Her karar öncesi/sonrası ve her eylem/onay sırasında güncel artifact/report/review/source/control denetlenir. Eksik/değişmiş kaynakta görev durur; base ile devam etmez. Pause/cancel kendi child'ını boşaltır; form resume desteklenmez.
- Registry disabled base + gerçek adapter FK + EXPERIMENTAL görev deployment'ını kaydeder. Global active pointer değişmez. Adapter compatibility hâlâ `unknown`; bu tek görev genel uyumluluk kabulü değildir.
- İlk eylemden önce admission observation; her başarılı karar için aynı transaction'da call/run/step/deployment/request/response hash'leri, pozitif hook sayısı ve değişmeyen base beyanı kaydedilir. Audit altı başarılı adapter call/proof ve state/registry kimliğini birleştirir; karışık base çağrısı reddedilir.

## Geçmiş ve güncel durum ayrımı

`verification_scope: historical_execution`, çalışırken kabul edilmiş adapter kimliğiyle gerçekleşmiş altı kararı ve yürütme kanıtını doğrular. Sonradan eğitim kaynağının revoke edilmesi veya artifact'ın silinmesi geçmişteki kullanımı silmez. Bu sonuç bugünkü artifact kullanılabilirliği, eğitim provenance'ının yeniden kabulü veya yeni runtime izni değildir.

Görev audit'i ayrıca kaynak yeniden incelemesiyle `current_source_status: available|unavailable` döndürür; doğrudan scheduler historical audit'i `unchecked` verir. Her durumda `runtime_reuse_authorized: false`. Yeni preview/start ve devam eden eylemler yalnız full güncel source recheck ile ilerler. İçerik collector'ı adapter run'larına otomatik açılmaz.

## Açık kabul sınırları

Gerçek yönetilen kabul, birim fixture'larından ayrı raporlanır. Altı hook'lu yeni görev, altı manuel onay, exact tek POST/readback, pending-fill değişiminde sıfır POST, fresh offline historical audit ve ardından ayrı base görevi birlikte sınanmalıdır. Aynı-grup training sonucu kalite artışı kanıtı değildir; bağımsız değerlendirme, explicit promotion/rollback, S2 trainer uyumluluğu ve W1–W6 ürün kabulü ayrı açık işlerdir.
