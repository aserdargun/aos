import {useEffect, useId, useState} from 'react';
import {locale, t, useLanguage} from './i18n';
import type {ImageDraftCapability, StaticQueryCapability, Overview, RemoteFormRepeatReport, RetentionStatus, Snapshot, Tasks, WebApplicationReport} from './api';
import {CapabilityEvidence} from './CapabilityEvidence';
import {DevelopmentJournal, validDevelopmentCheckpoint, type DevelopmentCheckpoint} from './DevelopmentJournal';
import releaseAcceptanceSnapshot from '../../docs/release_acceptance.json';
import {releaseAcceptanceLabels} from './translations_core';
import './development.css';

const acceptanceStageIds = ['candidate', 'scientist', 'native_workflow', 'user_delivery', 'learning', 'swapp'] as const;
type AcceptanceStage = {id: typeof acceptanceStageIds[number]; title_key: string;
  source: 'pending' | 'partial' | 'implemented'; verification: 'pending' | 'cpu_verified' | 'historical_native';
  delivery: 'pending' | 'not_delivered' | 'historical'; blockers: string[]; next_action_key: string; evidence: string[]};
type ReleaseAcceptance = {schema_version: '1.0' | '1.1'; observed_at: string; scope: 'manual_source_acceptance_snapshot';
  runtime_authority: false; product_complete: false; stages: AcceptanceStage[]; checkpoint?: DevelopmentCheckpoint};
const acceptanceObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const acceptanceExact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
export function validReleaseAcceptance(value: unknown): value is ReleaseAcceptance {
  if (!acceptanceObject(value)) return false;
  const keys = ['schema_version', 'observed_at', 'scope', 'runtime_authority', 'product_complete', 'stages'];
  if (value.schema_version === '1.1') keys.push('checkpoint');
  if (!acceptanceExact(value, keys) || !['1.0', '1.1'].includes(String(value.schema_version))
    || (value.schema_version === '1.1' && !validDevelopmentCheckpoint(value.checkpoint))
    || value.scope !== 'manual_source_acceptance_snapshot'
    || value.runtime_authority !== false || value.product_complete !== false
    || typeof value.observed_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value.observed_at)
    || !Number.isFinite(Date.parse(value.observed_at)) || new Date(value.observed_at).toISOString().replace('.000Z', 'Z') !== value.observed_at
    || !Array.isArray(value.stages) || value.stages.length !== acceptanceStageIds.length) return false;
  return value.stages.every((stage, index) => acceptanceObject(stage)
    && acceptanceExact(stage, ['id', 'title_key', 'source', 'verification', 'delivery', 'blockers', 'next_action_key', 'evidence'])
    && stage.id === acceptanceStageIds[index] && stage.title_key === 'release_acceptance.' + stage.id + '.title'
    && stage.next_action_key === 'release_acceptance.' + stage.id + '.next'
    && typeof stage.source === 'string' && ['pending', 'partial', 'implemented'].includes(stage.source)
    && typeof stage.verification === 'string' && ['pending', 'cpu_verified', 'historical_native'].includes(stage.verification)
    && typeof stage.delivery === 'string' && ['pending', 'not_delivered', 'historical'].includes(stage.delivery)
    && Array.isArray(stage.blockers) && stage.blockers.length <= 16 && new Set(stage.blockers).size === stage.blockers.length
    && stage.blockers.every(key => typeof key === 'string' && key.startsWith('release_acceptance.blocker.')
      && Object.hasOwn(releaseAcceptanceLabels, key))
    && Array.isArray(stage.evidence) && stage.evidence.length >= 1 && stage.evidence.length <= 8
    && new Set(stage.evidence).size === stage.evidence.length
    && stage.evidence.every(path => typeof path === 'string' && path.length <= 240
      && /^(?:docs|data)\/[A-Za-z0-9_./-]+$/.test(path) && !path.split('/').some(part => part === '..' || part === '.' || part === '')));
}
const acceptanceCandidate: unknown = releaseAcceptanceSnapshot;
const releaseAcceptance = validReleaseAcceptance(acceptanceCandidate) ? acceptanceCandidate : null;
const nextAcceptanceGate = releaseAcceptance?.stages.find(stage => stage.blockers.length > 0);
const acceptanceStatusLabels = {pending: 'Bekliyor', partial: 'Kısmen uygulandı', implemented: 'Uygulandı',
  cpu_verified: 'CPU / sentetik kabul kanıtı', historical_native: 'Tarihsel native kanıt; güncel aday kabulü değil',
  not_delivered: 'Bu aday teslim edilmedi', historical: 'Yalnız tarihsel teslim'};

const formStageLabels: Record<string, string> = {
  'browser.form.open': 'Giriş sayfası',
  'browser.form.state_before': 'Önce durum GET',
  'browser.form.fill': 'Form doldurma',
  'browser.form.submit': 'Form POST',
  'browser.form.receipt': 'Makbuz GET',
  'browser.form.state_after': 'Sonra durum GET'
};

const completed = [
  {
    title: 'İncelenmiş belge bağlamıyla gerçek S2 skill önerisi',
    detail: 'İncelenmiş kaynak metni ayrı izinlerle gerçek Bonsai planına bağlandı. İzole managed oturumda EN/TR hedefler ayrı bind/start, gerçek Decider, altışar taze onay ve bağımsız readback ile geçti. Üçüncü koşuda kaynak iptali fill/POST öncesinde yürütmeyi durdurdu; replay reddedildi. Site ve belge sentetiktir; genel hedefler, eğitim veya nedensel kalite kabulü değildir.',
    evidence: 'Gerçek pinli Bonsai/Decider, iki managed EN/TR görev ve revoke-before-fill; ayrı fixture UI; docs/OWNED_SKILL_KNOWLEDGE.md, docs/STATUS.md'
  },
  {
    title: 'İncelenmiş belgeyi gerçek S1 görev kararında kullanma',
    detail: 'Ayrı belge-bağlamı izni ve exact hash ile Hello ve görünür tarayıcı formu gerçek Decider kararlarında kaynak metnini kullandı. Üç ayrı manuel onay ve üç bağımsız doğrulama geçti. Kaynak, snapshot, actual request ve karar bağları denetlendi; fixture hazırlığı model kullanımı sayılmaz. Genel hedefler, harici hesap yetkisi ve nedensel kalite kabulü değildir.',
    evidence: 'Gerçek pinli Decider / Ubuntu Chromium-MCP; iki sentetik görev, EN/TR context UI; docs/TASK_KNOWLEDGE.md, docs/STATUS.md'
  },
  {
    title: 'İncelenmiş kaynaktan gerçek Bonsai alıntı yanıtı',
    detail: 'Ayrı inference ve özel saklama izniyle pinli Bonsai iki EN/TR sentetik olgudan exact kaynak alıntısı çıkardı; belgelenmemiş soruda yanıt vermedi. Request, yanıt ve kaynak hash bağları doğrulandı. Serbest metin doğruluğu, S2 görev planlaması, eğitim veya geniş held-out kalite kabulü değildir.',
    evidence: 'Gerçek Bonsai; üç sentetik soru, authenticated EN/TR Knowledge paneli; docs/DOCUMENT_KNOWLEDGE_ANSWERS.md, docs/STATUS.md'
  },
  {
    title: 'Özel belge yayını, insan incelemesi ve kapsam-bağlı arama',
    detail: 'UTF-8 kaynaklar ayrı yayın ve inceleme izinleriyle değişmez özel kayda alınır. Lexical EN/TR arama yalnız güncel kabul edilmiş kaynakları yerel uygulama/tenant/rol bölümünde bulur; revoked, expired veya başka bölümdeki kaynaklar elenir. Chunk/source hash ve rendered UI retleri doğrulandı. Harici hesap ACL’i, embedding/vector veya eğitim değildir.',
    evidence: 'CPU kaynak/inceleme/ret testleri ve gerçek Chromium fixture UI; docs/DOCUMENT_KNOWLEDGE.md, docs/STATUS.md'
  },
  {
    title: 'Aynı vakada eşli temel ve adapter değerlendirmesi',
    detail: 'Aynı sabit sentetik gelişim girdisinde temel ve adapter kolları ayrı başlatıldı; her biri altı gerçek Decider kararı, altı manuel eylem onayı, bir form POST’u ve readback tamamladı. Yeni model süreçleri, kapanış sonrası tarihsel audit ve klon veritabanında bozulma reddi doğrulandı. Bu tek çift held-out, kalite veya hız üstünlüğü, yükseltme ya da devreye alma iddiası değildir.',
    evidence: 'Gerçek EN/TR Tasks, pinli Decider/adapter ve Ubuntu Chromium-MCP; kapanış sonrası ağsız eşli audit ve klon DB bozulma reddi; docs/OWNED_ADAPTER_EVALUATION.md, docs/STATUS.md'
  },
  {
    title: 'Görev başına ayrı adapter ile gerçek S1 yürütmesi',
    detail: 'Ayrı runtime onayından sonra yeni gelişim girdisiyle altı gerçek S1 kararı adapter-backed görev başına hook ve runtime registry kimliğiyle çalıştı; altı ayrı eylem onayı, tek POST ve readback denetlendi. Ardından açıkça başlatılan temel görev altı temel model çağrısı kullandı. Bekleyen fill sırasında adapter değişikliği görevi POST öncesi durdurdu; kapanıştan sonra tarihsel audit kaldı, güncel kaynak kullanılamaz duruma geldi. Bu kalite artışı, promotion veya genel W1–W6 kabulü değildir.',
    evidence: 'Gerçek adapter engine / CUDA / Decider / Ubuntu Chromium-MCP, EN/TR Tasks, fresh offline audit ve zero-POST tamper denial; docs/OWNED_ADAPTER_RUNTIME.md, docs/STATUS.md'
  },
  {
    title: 'Gerçek episode’dan izinli deneysel S1 adapter',
    detail: 'Altı gerçek S1 gelişim kaydı ayrı veri incelemesi ve exact eğitim onayıyla rank-4 GPU adapter üretti. İkinci CUDA süreci aynı altı kayıtta taban ve adapter’ı karşılaştırdı; doğruluk ikisinde de %100, NLL değişmedi. Bu bağımsız test başarısı, otomatik deployment veya promotion değildir.',
    evidence: 'Gerçek Bonsai/Decider/Chromium kaynak, EN/TR Tasks ve fresh CUDA replay; docs/OWNED_EPISODE_ADAPTATION.md, docs/STATUS.md'
  },
  {
    title: 'Episode dönüşümü ve gerçek S1 tokenizer hazırlığı',
    detail: 'İncelenmiş export, ayrı onayla S1 Example/Q ve S2 plan messages/target kayıtlarına dönüştürüldü. Gerçek CPU tokenizer altı S1 girdisinde 240 varyantı doğruladı; ağırlık veya CUDA açmadı. Kaynak/receipt bağı korunur; S2 trainer uyumluluğu ve eğitim açık kalır.',
    evidence: 'Gerçek kaynaklı EN/TR Tasks, pinli tokenizer/prompt/collate; docs/OWNED_EPISODE_PREPARATION.md, docs/STATUS.md'
  },
  {
    title: 'İzinli canlı S1/S2 içerik adayları ve yerel export',
    detail: 'Planlamadan önce açık izinle gerçek Bonsai planı ve altı Decider kararı özel adaylara bağlandı. Tasks içerik incelemesi, ayrı rol kabulü, değişmez yerel export ve geri çekme sunar; yeni ağsız süreç aynı export’u doğruladı. Yalnız sentetik gelişim verisidir; eğitim veya gerçek-site kabulü değildir.',
    evidence: 'Gerçek Bonsai/Decider, EN/TR Tasks, Ubuntu Chromium-MCP; docs/OWNED_EPISODE_LEARNING.md, docs/STATUS.md'
  },
  {
    title: 'Hedeften plana bağlı Bonsai → Decider yürütmesi',
    detail: 'İki tam EN/TR hedef biçimi, seçilmiş sentetik skill için gerçek Bonsai önerisine bağlandı. Tasks’ta ayrı öneri/önizleme/başlangıç, Decider ile altı manuel onay ve plan-bağlı sonuç denetimi vardır. Genel hedef, gerçek site kabulü veya eğitim değildir.',
    evidence: 'Pinli gerçek Bonsai/Decider, EN/TR Tasks, Ubuntu Chromium-MCP; docs/OWNED_SKILL_PLANNING.md, docs/STATUS.md'
  },
  {
    title: 'Seçilmiş skill’in yeni oturumda yeniden kullanımı',
    detail: 'İki ayrı managed süreçte aynı kaynak ve DB korunarak yeni runtime, token ve lease ile seçilmiş recipe yürütüldü. Eski işler oynatılmadı; altı yeni manuel onay, pending revoke sırasında sıfır POST ve kapanış sonrası ağsız audit doğrulandı. Sentetik gelişim kabulüdür, gerçek site veya eğitim değildir.',
    evidence: 'Gerçek Decider / Ubuntu Chromium-MCP, yeni oturum EN/TR arayüzü; docs/OWNED_SKILL_REUSE.md, docs/STATUS.md'
  },
  {
    title: 'İncelenmiş skill sürümleri ve açık gelişim geri alma',
    detail: 'Tasks’ta A ve B ayrı yayımlanıp seçildi ve çalıştırıldı; açık geri alma sonrası yeni koşu tekrar A pinlerini kullandı. Seçim kalıcıdır, görev başlatmaz; revoke sonrası otomatik fallback yoktur. Sentetik gelişim kabulüdür, üretim aktivasyonu veya eğitim değildir.',
    evidence: 'Gerçek Decider / Ubuntu Chromium-MCP, kapanmış supervisor sonrası ağsız admission denetimi; docs/OWNED_SKILL_RELEASES.md, docs/STATUS.md'
  },
  {
    title: 'Kanıta bağlı aday incelemesi ve geri çekme',
    detail: 'Tasks’ta ayrı exact-hash kabulü, yeni inceleme-bağlı koşu ve altı manuel onay doğrulandı. Pending fill sırasında geri çekme POST olmadan görevi durdurdu; geçmiş başarı korunur. Bu sentetik gelişim kanıtıdır; skill aktivasyonu veya eğitim değildir.',
    evidence: 'Gerçek pinli Decider / Ubuntu Chromium-MCP, ağsız receipt incelemesi ve EN/TR UI; docs/OWNED_CANDIDATE_REVIEW.md, docs/STATUS.md'
  },
  {
    title: 'Kaydedilmiş adayın yeni girdilerle yürütülmesi',
    detail: 'Tasks’ta owned sentetik gösterimden kaydedilmiş recipe, iki yeni değerle altı ayrı onay sonrası çalıştı; kaynak ve yürütme ağsız yeniden denetlendi. Gerçek site, bağımsız held-out, aktivasyon veya eğitim kabulü değildir. Eski backend otomatik güncellenmez.',
    evidence: 'Gerçek pinli Decider / Ubuntu Chromium-MCP, kaynak değişiminde POST öncesi ret; docs/OWNED_CANDIDATE_EXECUTION.md, docs/STATUS.md'
  },
  {
    title: 'Görünür Ubuntu / Chromium / Playwright MCP',
    detail: 'Ağsız sentetik görevde gerçek Chromium ve MCP çalıştı; ayrı onaylar ve bağımsız sonuç okuması doğrulandı.',
    evidence: 'Gerçek MCP + Decider kabulü; docs/STATUS.md'
  },
  {
    title: 'Profil ve runtime kimlik kapısı',
    detail: 'İsteğe bağlı sentetik profilde runtime pinleri yeniden denetleniyor; TCP guard ve fixture sunucusu yalnız iki GET rotasını sınırlandırıyor.',
    evidence: 'Gerçek MCP + Decider: 1 PASS; docs/WEB_REQUEST_SCOPE.md'
  },
  {
    title: 'Açık host HTTPS giriş ön kontrolü',
    detail: 'Kayıtlı uzak profilin exact hash onayıyla tek HTML giriş isteği DNS public IP ve TLS hostname kapılarından geçer; bu tek başına görev yetkisi vermez.',
    evidence: 'Yerel sentetik TLS testleri; gerçek site denenmedi; docs/WEB_HTTPS_PREFLIGHT.md'
  },
  {
    title: 'Onaylı tek HTTPS giriş görevi',
    detail: 'Opt-in görünür Ubuntu/MCP görevi yalnız exact girişi ayrı manuel onay ve tek kullanımlık istemci sırrıyla açar; gerçek site iş akışı veya veri toplama değildir.',
    evidence: 'Owned sentetik TLS/MCP + gerçek pinli Decider ve mocksuz konsol UI kabulü; docs/WEB_HTTPS_RELAY.md'
  },
  {
    title: 'Pinli statik JS/CSS/görsel tarayıcı işçisi',
    detail: 'Exact HTML ve en çok sekiz aynı-origin JS/CSS/görsel, yeni managed oturumda özel plan piniyle tek manuel onaydan sonra görünür Chromium/MCP üzerinde yüklenir; bağımsız içeriksiz readback ve append-only job/run bağı vardır.',
    evidence: 'Owned sentetik TLS/MCP scheduler, private manager ve EN/TR arayüz testleri; gerçek site denenmedi; docs/WEB_STATIC_ASSETS.md'
  },
  {
    title: 'Onaylı exact JSON GET tarayıcı görevi',
    detail: 'Ayrı v2 planda en çok dört exact aynı-origin JSON GET, konsoldan ağsız exact-hash kaydı ve yeni managed oturumda özel plan piniyle tek kalıcı manuel onaydan sonra ağsız Ubuntu Chromium/MCP içinde DOM’u günceller. Gerçek site sonucu veya öğrenme izni değildir.',
    evidence: 'Owned sentetik TLS/Chromium/MCP managed görev, EN/TR konsol plan kaydı, JSON→DOM ve off-plan retleri; docs/WEB_READONLY_DATA_BUNDLE.md'
  },
  {
    title: 'JSON görevinde salt okunur model kaynak denetimi',
    detail: 'Tamamlanmış v2 JSON run’ının exact plan, tek insan onayı, tarayıcı readback ve yanıt hash’leri yeniden denetlenir; yalnız gerçekten çağrılmış S1/S2 olay kimlikleri döner. Veri toplama veya eğitim izni değildir.',
    evidence: 'Owned ağsız Chromium/MCP fixture ve gerçek pinli Decider, değiştirilmiş kaynak retleri; docs/REMOTE_READONLY_DATA_SOURCE.md'
  },
  {
    title: 'HTTPS form görevinde salt okunur kaynak denetimi',
    detail: 'Tamamlanmış form run’ının exact planı, dört insan onayı, tek POST ve bağımsız makbuz readback’i yeniden denetlenir; yalnız pinli S1/S2 olay kimlikleri döner. Site sonucu, veri toplama veya eğitim izni değildir.',
    evidence: 'Owned sentetik TLS/Ubuntu/Chromium/MCP tek ve çok alanlı fixture; docs/REMOTE_FORM_LEARNING_SOURCE.md'
  },
  {
    title: 'Durum planlı HTTPS form kaynak denetimi',
    detail: 'Altı ayrı insan onayı, beyan edilmiş önce/sonra durum geçişi ve ayrı makbuz doğrulaması tek kaynak snapshot’ında yeniden denetlenir. Site sonucu, veri hakkı veya eğitim izni değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP fixture ve gerçek pinli Decider 6 S1/0 S2; docs/REMOTE_FORM_STATE_LEARNING_SOURCE.md'
  },
  {
    title: 'HTTPS form için izinli metadata akışı',
    detail: 'Tasks’ta iki aşamalı exact izin ve ayrı bağlama sonrası scheduler, onaylı form aşamalarının gerçek model olaylarını içeriksiz özel outbox’a yazar. Harici hak ve eğitim yoktur.',
    evidence: 'Owned sentetik TLS/Ubuntu/Chromium/MCP, gerçek pinli Decider 0→4 S1, EN/TR Tasks ve iptal; docs/REMOTE_FORM_LEARNING_STREAM.md'
  },
  {
    title: 'Durum planlı form için ayrı izinli metadata akışı',
    detail: 'Exact form ve durum planına bağlı ayrı izin, altı onaylı aşamanın gerçek model olaylarını artımlı özel outbox’a yazar. HTML değişimi site sonucu veya eğitim etiketi değildir.',
    evidence: 'Owned TLS/Ubuntu/Chromium/MCP, gerçek pinli Decider 0→6 S1/0 S2; docs/REMOTE_FORM_LEARNING_STREAM.md'
  },
  {
    title: 'Temel ve durum planlı form tekrar ölçümü',
    detail: 'Exact dört veya altı-onaylı planın terminal bağlı denemeleri ayrı sayılır; başarılı koşu kendi kaynak denetiminden geçer. Süreler insan beklemesini içerir, uygulama sonucu değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP iki gerçek pinli Decider durum koşusu ve sentetik retler; docs/REMOTE_FORM_REPEAT.md'
  },
  {
    title: 'JSON değişim ve sayfa taslağı adayı',
    detail: 'Aynı exact v2 planda JSON farkı, başlık aynı kalsa bile saptanır; iki sabit onaylı koşu güncel manuel sayfa taslağına yalnız tarihsel aday olarak bağlanır. Eski revizyon ve gözlenmemiş çıkış linkleri reddedilir; görev içi retrieval yoktur.',
    evidence: 'Owned ağsız Ubuntu/Chromium/MCP üç-koşu testi; docs/REMOTE_READONLY_DATA_CHANGE.md, docs/REMOTE_READONLY_DATA_KNOWLEDGE.md'
  },
  {
    title: 'Konsoldan özel JSON sayfa taslağı',
    detail: 'İki sabit, onaylı v2 koşu ve manuel sayfa anahtarından özel incelenmemiş taslak üretilir; exact taslak hash’iyle ayrı kayıt yapılır. Çıkış bağlantısı iddiası reddedilir; review, görev yetkisi veya gerçek site sonucu değildir.',
    evidence: 'Owned ağsız Chromium/MCP fixture ve gerçek pinli Decider, authenticated API ve EN/TR Tasks; docs/REMOTE_READONLY_DATA_KNOWLEDGE.md'
  },
  {
    title: 'JSON metadata inceleme ve taze koşu denetimi',
    detail: 'Exact tarihsel aday hash’i açık onayla özel review kaydına bağlanır; ayrı tamamlanmış yeni koşu tüm JSON/asset/sayfa parmak izlerini ve güncel taslağı yeniden doğrular. Yalnız sembolik anahtarlar döner; görev içi retrieval ve gerçek site kabulü yoktur.',
    evidence: 'Owned ağsız Chromium/MCP fixture ve gerçek pinli Decider, authenticated API ve EN/TR Tasks, JSON kayması ve kaynak retleri; docs/REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md'
  },
  {
    title: 'JSON review için canlı readback bağı',
    detail: 'Özel tarihsel review yeni v2 oturumuna açık kaynak piniyle bağlanır; yalnız ayrı onaylı taze JSON/asset/sayfa readback eşleşirse içeriksiz sayfa anahtarı raporlanır. Eski parmak izi anahtar döndürmez; görev kararına retrieval eklenmez.',
    evidence: 'Owned ağsız Ubuntu/Chromium/MCP fixture, direct backend pin reddi/kabulü ve EN/TR Tasks; docs/REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md'
  },
  {
    title: 'JSON görevinde izinli metadata akışı',
    detail: 'Ayrı exact v2 izin hash’i iki aşamada kaydedilip ayrıca göreve bağlanır; scheduler yalnız onaylı open sonrası pinli S1/S2 kaynaklarını özel outbox’a içeriksiz/dedup yazar. Gerçek hak, gold, dataset veya eğitim değildir.',
    evidence: 'Owned ağsız Chromium/MCP, gerçek Decider 0→1 S1, EN/TR Tasks ve iptal; docs/REMOTE_READONLY_DATA_STREAM.md'
  },
  {
    title: 'Konsoldan statik JS/CSS/görsel planı',
    detail: 'Kayıtlı tek sayfalı görev için exact URL/MIME planı ağsız önizlenir; hash onayıyla özel dosyaya kaydedilir ve güvenli yeni oturum komutu gösterilir. Kayıt görev onayı veya gerçek site sonucu değildir.',
    evidence: 'API kapsam/özel dosya, EN/TR Chromium ve canlı yeni backend kontrolü; docs/WEB_STATIC_ASSETS.md, docs/STATUS.md'
  },
  {
    title: 'Sıralı HTTPS rota onayları',
    detail: 'Owner-only URL listesinden 2–8 exact rota planı ağsız üretilebilir; görevde her GET ayrı onay ve içeriksiz readback ister. Link keşfi veya site sonucu değildir.',
    evidence: 'CLI ve owned sentetik TLS/MCP testleri; docs/WEB_READONLY_ROUTES.md'
  },
  {
    title: 'Managed HTTPS form opt-in',
    detail: 'Plan/preview/start özel dosyalarla tek alanlı public formu pinler; dört manuel onay ve tek POST vardır. Gerçek site veya uygulama sonucu doğrulanmadı.',
    evidence: 'Yerel TLS/DNS fixture, manager ve EN arayüz testleri; docs/WEB_HTTPS_FORM_TRANSPORT.md'
  },
  {
    title: 'HTTPS form onayında son kullanma sınırı',
    detail: 'Tüketilmiş eylem onayı yalnız exact host isteğine, tek kullanıma ve kalan en çok 90 saniyelik monotonic süreye bağlıdır; geç veya yanlış istek ağdan önce reddedilir. Gerçek site kabulü değildir.',
    evidence: '5 izin birimi, 3 taşıma testi ve owned dört/altı onaylı Ubuntu worker; docs/STATUS.md'
  },
  {
    title: 'HTTPS formda gerekli e-posta alanı',
    detail: 'Pinli görünür Ubuntu/MCP işçisi gerekli e-posta alanını exact değerle doldurur; tarayıcı geçerliliği bozuksa POST öncesi durur. Parola, oturum açma ve gerçek site sonucu hâlâ desteklenmez.',
    evidence: 'Owned sentetik TLS/Ubuntu/Chromium/MCP olumlu e-posta, geçersiz e-posta ve parola retleri; docs/WEB_HTTPS_FORM_TRANSPORT.md'
  },
  {
    title: 'Konsoldan özel HTTPS form taslağı',
    detail: 'Kayıtlı profilden tek veya 2–8 sıralı alanlı form görevi ve exact POST/makbuz planı ağsız önizlenir; ayrı hash onaylarıyla özel dosyalara yazılır. Public hedef için yalnız güvenli yeni oturum komutu gösterilir; site isteği veya görev izni verilmez.',
    evidence: 'Authenticated API, private manager eşliği ve EN/TR Chromium; docs/WEB_FORM_ONBOARDING.md'
  },
  {
    title: 'Konsoldan ayrı HTTPS form durum planı',
    detail: 'Görev kayıt öncesi altı onaylı moda alınır; önce/sonra durum URL ve hashleri üçüncü ayrı onayla özel plana kaydedilir. Plan olmadan public manager komutu gösterilmez; değişim site sonucu değildir.',
    evidence: 'Authenticated API/özel dosya ve EN/TR Chromium; docs/WEB_FORM_ONBOARDING.md'
  },
  {
    title: 'Son görevde S1/S2 gecikme yüzdelikleri',
    detail: 'Doğrulanmış son run’ın başarılı, sonlu ve en çok 256 model çağrısı role göre p50/p95 olarak gösterilir. Bu görev süresi, kararlı p95 veya gerçek-site hız kabulü değildir.',
    evidence: 'Run bağı ve negatif birimler, EN/TR Chromium, canlı gerçek Decider Hello; docs/TASK_PROGRESS.md'
  },
  {
    title: 'Site bilgisi ve skill temelleri',
    detail: 'Sentetik staging S1 adayı ve aynı SAVE görevinden ayrı S1 doğrulanmış eylem / downstream-bağlı S2 sahne adayları türetildi; review ve aktivasyon yok.',
    evidence: 'Gerçek Decider/Bonsai, yalnız sentetik adaylar; docs/STAGING_SKILL_CANDIDATE.md, docs/VISION_SKILL_CANDIDATE.md'
  },
  {
    title: 'Pinli profilde iki rolün skill taslakları',
    detail: 'Rota Tasks paneli özel S1 ve S2 taslak metadata’sını ayrı listeler; S2 taslağı S1 kaynak denetimine girmez. Rota görevi Bonsai çağırmaz; review ve aktivasyon yoktur.',
    evidence: 'Owned Ubuntu/Chromium/MCP gerçek Decider API ve EN/TR UI; docs/REMOTE_SITE_SKILL_PROVENANCE.md'
  },
  {
    title: 'Sentetik skill vaka girdisi bağı',
    detail: 'Özel sentetik parametreler yapısal vaka hash’lerine ve seçili S1 vakasında mevcut HTTPS form planının exact gövdesine bağlanır; kaynak, submit ve sonuç doğrulanmaz.',
    evidence: 'Canonical şema/fixture, private CLI, form planı bağı ve negatif birim testleri; docs/SITE_SKILL_CASE_BINDING.md'
  },
  {
    title: 'Sentetik skill vakasında onaylı form taşıma bağı',
    detail: 'Geliştirme vakasının exact form gövdesi, dört ayrı onaylı tamamlanmış form koşusu ve gerçek S1 submit karar kaynağıyla salt okunur eşleşir. Skill çalıştırma, site sonucu ve eğitim hâlâ doğrulanmaz.',
    evidence: 'Owned sentetik TLS/Ubuntu/Chromium/MCP, gerçek pinli Decider, private CLI ve kaynak retleri; docs/SITE_SKILL_FORM_EXECUTION.md'
  },
  {
    title: 'İki rol için öğrenme ayrımı',
    detail: 'Sentetik S1/S2 outbox, Bonsai sahnesini sonraki doğrulanmış Decider sonucuna gold olmayan S2 kaydıyla bağlar. Uzak rota için ayrı read-only kaynak denetimi vardır; izinli metadata akışı ayrı kartta, eğitim verisi yok.',
    evidence: 'Gerçek Bonsai/Decider SAVE ve uzak Decider rota kanıtı; docs/LEARNING_EVENT_STREAM.md, docs/REMOTE_LEARNING_SOURCE.md'
  },
  {
    title: 'Göreve bağlı uzak metadata akışı',
    detail: 'Exact metadata-only izin Tasks ekranında iki aşamalı veya CLI ile kaydedilip ayrıca göreve bağlanınca scheduler, pinli S1 rota kararı/onayı ve varsa S2 escalation olaylarını izin-başına özel outbox’a artımlı yazar. Host CLI exact izni iptal edebilir; yeni managed backend vadesi dolan izinleri özel oturum köklerinde açılışta ve saatte bir mantıksal temizler. Kapalı makinede zaman garantisi, harici hak doğrulaması, dataset veya eğitim değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP gerçek Decider 0→1→2 S1, iptal ve hata izolasyonu; private retention/manager fixture; docs/REMOTE_LEARNING_STREAM.md, docs/REMOTE_LEARNING_CONSENT.md'
  },
  {
    title: 'Statik görev için açık metadata akışı',
    detail: 'Exact statik run için rota izninden ayrı yerel izin Tasks ekranında iki aşamalı kaydedilip ayrıca bağlanır; scheduler onay öncesi ve settle sırasında gerçek pinli S1/S2 metadata adaylarını özel outbox’a dedup yazar. Readback gold etiketi veya eğitim yoktur.',
    evidence: 'Owned sentetik TLS/Ubuntu/Chromium/MCP ve gerçek Decider 0→1 S1, API ve EN/TR Tasks; docs/REMOTE_STATIC_LEARNING_STREAM.md'
  },
  {
    title: 'Web profili sürümleri',
    detail: 'Konsolda exact ebeveyn seçilerek ardıl taslak önizlenir ve ayrı hash onayıyla kaydedilir. Önizleme ebeveyn zincirini yazmadan denetler; görev veya öğrenme yetkisi açmaz.',
    evidence: 'Owned EN/TR Chromium ve API lineage testleri; docs/WEB_APPLICATION_PROFILE.md'
  },
  {
    title: 'Salt okunur inceleme komutları',
    detail: 'İki açık seçilmiş sentetik run aynı snapshot içinde karşılaştırılabiliyor; skill planı exact hash ile yapısal önizleniyor.',
    evidence: 'W2/W3 CLI testleri; docs/SITE_PAGE_CHANGE.md, docs/SITE_SKILL_VALIDATION.md'
  },
  {
    title: 'Uzak rota / sayfa taslağı adayı',
    detail: 'İki değişmeyen tarihsel HTTPS readback, aynı profil ve güncel manuel sayfa taslağıyla yalnız içeriksiz aday olarak eşlenir; review veya görev içi retrieval yok.',
    evidence: 'Owned sentetik TLS/MCP testi; docs/REMOTE_ROUTE_KNOWLEDGE.md'
  },
  {
    title: 'Görev-içi sembolik rota bağlamı · sentetik',
    detail: 'Açıkça pinli metadata review, kaynak ve bağlı hedefin ayrı onaylı readback’leri eşleşmeden Decider kararına sembolik anahtar vermez; hedef değişirse sonraki karar ipucusuz kalır. Gerçek site bilgisi veya yetki değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP ve gerçek pinli Decider üç-rotalı eşleşme/kayma testleri; docs/REMOTE_ROUTE_KNOWLEDGE_REVIEW.md'
  },
  {
    title: 'İzole Decider ağırlık adayı tanısı',
    detail: 'Pinned modelden yalnız iki seçenek-token satırı özel, dağıtılamayan aday olarak bir CPU adımıyla güncellendi; aktif model ve promotion değişmedi.',
    evidence: 'Gerçek pinned Decider smoke; docs/DECIDER_ROW_CANDIDATE.md'
  },
  {
    title: 'Dataset bağlı S1 model-gradient denetimi',
    detail: 'Bir sentetik train satırı gerçek pinli Decider hesap grafiğinde geçici rank-4 adapter ile tek optimizer adımından geçti; taban değişmedi, checkpoint veya held-out sonuç yok.',
    evidence: 'Gerçek CUDA/Decider ve 7 opt-in test; docs/DATASET_GRADIENT_PROBE.md'
  },
  {
    title: 'Sentetik S1 adapter adayı ve bağımsız reload',
    detail: 'Tek sentetik train satırından özel rank-4 adapter dosyası üretildi, ayrı gerçek CUDA sürecinde yeniden yüklendi ve içerik-adresli özel raporla sonradan doğrulanabilir. İzole inference aynı pinli modelde tüm sentetik split kararlarını sayar; mevcut fixture 1/0/0, ayrıca özel işçi probu 1/1/1. Train NLL eşit kaldı; gerçek held-out kalite, canlı loader ve promotion yok.',
    evidence: 'Gerçek pinli Decider/CUDA, 15 opt-in test; docs/DATASET_ADAPTER_CANDIDATE.md, docs/DATASET_ADAPTER_RUNTIME_PROBE.md'
  },
  {
    title: 'Managed restart görev kabul kilidi',
    detail: 'Yeni backend’de doctor ve idle AGENT kontrolünden sonra exact desktop oturumuna bağlı kilit yeni görev, kontrol ve girdiyi reddeder. Eski backend’de otomatik restart kapalı kalır; dış süreç atomikliği iddia edilmez.',
    evidence: 'Ayrı canlı backend süreci ve manager/API regresyonu; docs/STATUS.md'
  },
  {
    title: 'Gönderilen değer–durum marker bağı',
    detail: 'İsteğe bağlı exact form alanı, özel gönderim değeriyle sonra HTML marker hash’ini ağ isteğinden önce bağlar; ayrı onaylı readback uyuşursa içeriksiz eşleşme kanıtı üretir. Kalıcı site sonucu veya hesap doğrulaması değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP, gerçek pinli Decider ve EN/TR konsol; docs/WEB_HTTPS_FORM_TRANSPORT.md'
  },
  {
    title: 'Sentetik skill vakasında durum readback bağı',
    detail: 'Özel vaka değeri, exact form gövdesi ve sonra marker hash’i; altı ayrı onay, gerçek S1 submit kaynağı ve iki doğrulama kaydıyla salt okunur denetlenir. Skill yürütme, kalıcı site sonucu veya aktivasyon değildir.',
    evidence: 'Owned Ubuntu/Chromium/MCP, gerçek pinli Decider, özel CLI ve negatif kaynak testleri; docs/SITE_SKILL_FORM_EXECUTION.md'
  }
] as const;

const openGates = [
  'W1 · Hedef URL, yetkili test hesabı, izinli görevler ve veri hakları',
  'W1 · Gerçek site için ağ, origin, yönlendirme ve tenant sınırları',
  'W1 · Açık yürütme izni, gerçek görev ve bağımsız sonuç doğrulaması',
  'W1 · İzinli canlı S1/S2 olay toplama, redaction ve retention',
  'W2 · Gözlenen site bilgisi, değişim algılama ve görev içi retrieval',
  'W3 · Parametreli gerçek skill testleri, review ve açık aktivasyon',
  'W4 · Ayrı incelenmiş S1/S2 veri setleri ve held-out değerlendirme',
  'W5 · S1 eğitim, değerlendirme, promotion ve rollback; S2 uyumluluk',
  'W6 · Tek uygulamada tekrarlı uçtan uca başarı ve hız kabulü'
] as const;

interface Props {
  tasks: Tasks | null;
  overview: Overview | null;
  retention: RetentionStatus | null | 'unavailable';
  formRepeats: RemoteFormRepeatReport | null | 'not-configured' | 'unavailable';
  webApplications: WebApplicationReport[] | null | 'unavailable';
  imageDraftCapability: ImageDraftCapability;
  staticQueryCapability: StaticQueryCapability;
  refreshKey?: number;
  snapshot?: Snapshot | null;
  runtimeObservedAt?: number | null;
  runtimeError?: boolean;
  refreshing?: boolean;
  onRefresh?: () => void;
  onNavigate: (tab: 'Görevler' | 'Çalışmalar' | 'Web uygulamaları' | 'Bilgisayar' | 'Scientist') => void;
}

const stageOwners = {
  candidate: 'AOS geliştirme', scientist: 'AOS + Scientist', native_workflow: 'AOS geliştirme',
  user_delivery: 'AOS + kullanıcı', learning: 'Veri ve model incelemesi', swapp: 'Yetkili intranet kullanıcısı'
};

export function Development({tasks, overview, retention, formRepeats, webApplications, imageDraftCapability, staticQueryCapability,
  refreshKey = 0, snapshot = null, runtimeObservedAt = null, runtimeError = false, refreshing = false, onRefresh, onNavigate}: Props) {
  const language = useLanguage();
  const panelId = useId();
  const [view, setView] = useState<'overview' | 'release' | 'history'>('overview');
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  const runtimeStale = runtimeObservedAt !== null && clock - runtimeObservedAt > 15000;
  const runtimeUnavailable = runtimeError || runtimeStale || runtimeObservedAt === null || tasks === null;
  const runtimeFresh = !runtimeUnavailable && !runtimeStale && runtimeObservedAt !== null;
  const runtimeStatus = runtimeError ? 'Durum okunamadı' : runtimeStale ? 'Eski gözlem'
    : tasks === null ? 'Oturum gözlemi bekleniyor' : runtimeObservedAt === null ? 'Gözlem zamanı bilinmiyor' : 'Güncel gözlem';
  const recentJobs = tasks?.jobs ?? [];
  const recentRuns = overview?.trajectory.available ? overview.trajectory.runs : undefined;
  const verifiedRuns = recentRuns?.filter(run => run.status === 'succeeded' && run.passed > 0 && run.failed === 0).length;
  const latestJob = recentJobs[0];
  const activeJob = tasks?.busy ? recentJobs.find(job => !['succeeded', 'failed', 'cancelled'].includes(job.status)) : null;
  const checkpoint = releaseAcceptance?.checkpoint;
  const elapsedMilliseconds = latestJob?.progress?.elapsed_ms;
  const elapsedText = typeof elapsedMilliseconds === 'number' && Number.isFinite(elapsedMilliseconds) && elapsedMilliseconds >= 0
    ? elapsedMilliseconds < 1000
      ? `${new Intl.NumberFormat(locale(), {maximumFractionDigits: 0}).format(elapsedMilliseconds)} ms`
      : `${new Intl.NumberFormat(locale(), {maximumFractionDigits: 1}).format(elapsedMilliseconds / 1000)} s`
    : '—';
  const latestCalls = latestJob?.progress?.phase ? latestJob.progress.model_calls : null;
  const modelCallTotals = latestJob?.progress?.phase ? latestJob.progress.model_call_totals : null;
  const modelCallLatency = latestJob?.progress?.phase ? latestJob.progress.model_call_latency : null;
  const system1Calls = modelCallTotals?.system1 ?? (latestCalls ? {
    ok: latestCalls.filter(call => call.role === 'system1' && call.status === 'ok').length,
    total: latestCalls.filter(call => call.role === 'system1').length} : null);
  const system2Calls = modelCallTotals?.system2 ?? (latestCalls ? {
    ok: latestCalls.filter(call => call.role === 'system2' && call.status === 'ok').length,
    total: latestCalls.filter(call => call.role === 'system2').length} : null);
  const remoteEntries = latestJob?.remote_learning_metadata?.enabled
    ? latestJob.remote_learning_metadata.entries_by_role : null;
  const deciderPreparation = tasks?.decider_preparation;
  const deciderPreparationLabel = tasks === null ? 'Canlı görev durumu bekleniyor…'
    : !deciderPreparation?.enabled ? 'Boşta Decider hazırlığı sunulmuyor'
    : deciderPreparation.state === 'ready_gpu' ? 'GPU worker süreli beklemede'
    : deciderPreparation.state === 'ready' ? 'CPU modeli hazır'
    : deciderPreparation.state === 'preparing' ? 'CPU modeli hazırlanıyor'
    : 'Boşta model hazır değil';
  const remoteTaskPinned = Boolean(tasks?.remote_entry || tasks?.remote_routes || tasks?.remote_form || tasks?.remote_static_assets);
  const nextSetupStep = webApplications === null || tasks === null
    ? 'Canlı kurulum durumu bekleniyor.'
    : webApplications === 'unavailable'
      ? 'Profil envanteri yok; kayıt öncesi backend sürümünü güvenli yeni oturumda güncelleyin.'
      : remoteTaskPinned && webApplications.length === 0
        ? 'Uzak görev pini var ama profil envanteri boş; yeni görev başlatmadan oturum kapsamını inceleyin.'
      : webApplications.length === 0
        ? 'Önce yetkili hedef URL, test rolü, izinli görev ve sonuç ölçütüyle bir profil taslağı kaydedin; sır girmeyin.'
        : !remoteTaskPinned
          ? 'Kayıtlı profilden exact görev ve planı hazırlayın; manager komutunu ağsız önizleyip güvenli yeni oturumda pinleyin.'
          : 'Pinli kapsamı Görevler ekranında inceleyin; yalnız yetkili görevi başlatıp bağımsız sonucu doğrulayın.';
  const repeatReport = formRepeats !== null && typeof formRepeats === 'object' ? formRepeats : null;
  const formatDuration = (milliseconds: number) => new Intl.NumberFormat(locale(), {maximumFractionDigits: 1}).format(milliseconds);
  const latencyText = (role: 'system1' | 'system2') => {
    const summary = modelCallLatency?.[role];
    if (!summary || !Number.isInteger(summary.samples) || summary.samples < 1 || summary.samples > 256
        || typeof summary.p50_ms !== 'number' || !Number.isFinite(summary.p50_ms) || summary.p50_ms < 0
        || typeof summary.p95_ms !== 'number' || !Number.isFinite(summary.p95_ms)
        || summary.p95_ms < summary.p50_ms) return '—';
    const format = new Intl.NumberFormat(locale(), {maximumFractionDigits: 1});
    return `${format.format(summary.p50_ms)} / ${format.format(summary.p95_ms)} ms · n=${summary.samples}`;
  };
  return <section className="panel development" data-testid="development-status">
    <header className="development-heading development-intro"><div><h1>{t('Kontrol merkezi')}</h1>
      <p>{t('Ne çalışıyor, ne değişti, sırada ne var?')}</p></div>
      {onRefresh ? <button type="button" disabled={refreshing} onClick={onRefresh}>{t('Durumu yenile')}</button> : null}</header>
      <section className="development-runtime" data-testid="development-runtime" aria-busy={refreshing}>
        <div className="development-heading"><div><h3>{t('Canlı oturum')}</h3>
          <p role="status" data-testid="development-runtime-status" className={runtimeFresh ? 'runtime-fresh' : 'runtime-unknown'}>{t(runtimeStatus)}</p></div>
          <button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button></div>
        {runtimeError || runtimeStale ? <p className="development-runtime-warning">{t('Son gözlem güncel durum değildir. Yenileyin; veri yokluğunu boşta kabul etmeyin.')}</p> : null}
        <dl className="development-runtime-strip">
          <div><dt>{t('Görev etkinliği')}</dt><dd>{runtimeUnavailable ? t('Kullanılamıyor') : tasks.restart_quiesced ? t('Toparlanma kilidi')
            : !tasks.available ? t('Görev motoru kapalı') : tasks.paused ? t('Duraklatıldı') : tasks.busy ? t('İş sürüyor') : tasks.reserved ? t('Kaynak rezervasyonu') : t('Kayıtlı aktif iş yok')}</dd></div>
          <div><dt>{t('Güncel görev')}</dt><dd>{runtimeUnavailable ? t('Kullanılamıyor') : activeJob ? <><code>{activeJob.kind}</code><small>{activeJob.progress?.phase ?? activeJob.status}</small></> : tasks.busy ? t('İş ayrıntısı sunulmadı') : t('Aktif görev yok')}</dd></div>
          <div><dt>{t('Eylem onayı')}</dt><dd>{runtimeUnavailable ? t('Kullanılamıyor') : tasks.approval ? t('Onay bekliyor') : t('Bekleyen onay yok')}</dd></div>
          <div><dt>{t('Sistem durumu')}</dt><dd>{runtimeUnavailable || !snapshot ? t('Kullanılamıyor')
            : <>{snapshot.control.owner}<small>{snapshot.control.status} · {snapshot.runtime.running ? t('Masaüstü çalışıyor') : t('Masaüstü kapalı')}</small></>}</dd></div>
        </dl>
        <div className="development-runtime-meta"><span>{t('Son başarılı gözlem:')} {runtimeObservedAt === null ? t('Bilinmiyor') : new Date(runtimeObservedAt).toLocaleString(locale())}</span>
          <span>{t('Oturum verisi; geliştirme ajanlarının etkinliği değildir.')}</span></div>
        <details className="development-session-details"><summary>{t('Oturum kimliği ve kapsamı')}</summary><div className="development-runtime-meta">
          <span>{t('Oturum:')} <code>{tasks?.manager_scope?.project ?? tasks?.manager_session ?? t('Kimlik sunulmadı')}</code></span>
          <span>Runtime: <code>{snapshot?.runtime.runtime_id ?? t('Kimlik sunulmadı')}</code></span>
          <span>{t('Bu arayüz:')} <code>{window.location.origin + window.location.pathname}</code></span>
          <span>{t('Build kimliği: sunulmadı')}</span></div></details>
        {latestJob && !activeJob ? <p className="development-last-result">{t('Son kayıt · aktif görev değildir:')} <code>{latestJob.kind}</code> · {latestJob.status}</p> : null}
      </section>
    <nav className="development-views" aria-label={t('Kontrol merkezi görünümleri')}>
      {([['overview', 'Genel durum'], ['release', 'Sürüm kontrol listesi'], ['history', 'Kanıt ve uygulama geçmişi']] as const).map(([key, label]) =>
        <button type="button" key={key} aria-pressed={view === key} aria-controls={panelId + '-' + key}
          onClick={() => setView(key)}>{t(label)}</button>)}
    </nav>
    <div className="development-layout" hidden={view === 'history'}><div className="development-primary">
    <section id={panelId + '-overview'} hidden={view !== 'overview'} aria-label={t('Genel durum')}>
      {checkpoint ? <DevelopmentJournal checkpoint={checkpoint}/>
        : <p role="status">{t('Geliştirme özeti sunulmadı; sürüm kontrol listesini inceleyin.')}</p>}
    </section>
    <section id={panelId + '-release'} hidden={view !== 'release'} data-testid="development-acceptance">
      <article className="development-focus" data-testid="development-current-focus">
        <div><h3>{t('Şu an neredeyiz?')}</h3>
          <p>{t('Uygulama temelleri mevcut. Güncel native entegrasyon ve kullanıcı teslimi kabul bekliyor.')}</p>
          <h3>{t('Sıradaki açık sürüm kapısı')}</h3>
          <p data-testid="development-checkpoint-next">{nextAcceptanceGate
            ? t(releaseAcceptanceLabels[nextAcceptanceGate.next_action_key]) : t('Kabul kaydını inceleyin; tamamlanma varsayılmaz.')}</p>
        </div><div className="development-snapshot"><strong>{t('Geliştirme kaydı')}</strong>
          <p data-testid="development-acceptance-date">{releaseAcceptance
            ? <time dateTime={releaseAcceptance.observed_at}>{releaseAcceptance.observed_at.replace('T', ' ').replace('Z', ' UTC')}</time> : '—'}</p>
          <p className="caption">{t('Tarihli manuel kayıt; canlı ajan sağlığı değildir.')}</p>
          <span>{t('Ürün kabulü açık')}</span></div>
      </article>
      <h3>{t('Sürüm kabulü · altı ayrı aşama')}</h3>
      <p className="caption">{t('Bu, elle incelenmiş tarihli kaynak kaydıdır; canlı runtime durumu, yürütme izni veya toplam tamamlanma yüzdesi değildir.')}</p>
      {!releaseAcceptance ? <p role="alert">{t('Kabul kaydı doğrulanamadı; tamamlanma veya yetki varsayılmaz.')}</p> : <>
        <div className="development-ledger">{releaseAcceptance.stages.map((stage, index) => <details className="development-stage"
          key={stage.id} data-testid={'development-acceptance-' + stage.id}>
          <summary><span className="development-stage-number">{String(index + 1).padStart(2, '0')}</span>
            <span className="development-stage-title"><strong>{t(releaseAcceptanceLabels[stage.title_key])}</strong>
              <small>{t(stageOwners[stage.id])} · {t('Kabul kapısı açık')}</small></span>
            <span className={'development-stage-state ' + stage.source}>{t(acceptanceStatusLabels[stage.source])}</span>
            <svg aria-hidden="true" viewBox="0 0 20 20" width="20" height="20"><path d="m5 7 5 5 5-5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg></summary>
          <div className="development-stage-body">
          <p data-testid="acceptance-source"><strong>{t('Kaynak')}:</strong> {t(acceptanceStatusLabels[stage.source])}</p>
          <p data-testid="acceptance-verification"><strong>{t('Kabul doğrulaması')}:</strong> {t(acceptanceStatusLabels[stage.verification])}</p>
          <p data-testid="acceptance-delivery"><strong>{t('Teslim')}:</strong> {t(acceptanceStatusLabels[stage.delivery])}</p>
          <p><strong>{t('Engeller')}</strong></p>
          {stage.blockers.length ? <ul data-testid="acceptance-blockers">{stage.blockers.map(key => <li key={key}>{t(releaseAcceptanceLabels[key])}</li>)}</ul>
            : <p>{t('Kayıtlı engel yok; tek başına tamamlanma değildir.')}</p>}
          <p data-testid="acceptance-next"><strong>{t('Sonraki kabul adımı')}:</strong> {t(releaseAcceptanceLabels[stage.next_action_key])}</p>
          <p><strong>{t('Kanıt başvuruları')}</strong></p>
          <ul data-testid="acceptance-evidence">{stage.evidence.map(path => <li key={path}><code style={{overflowWrap: 'anywhere'}}>{path}</code></li>)}</ul>
          </div></details>)}</div>
      </>}
    </section>
    </div><aside className="development-rail">
      <section><h3>{t('Sürüm hazırlığı')}</h3><strong>{t('Ürün kabulü açık')}</strong>
        <p>{t('Altı aşama; kaynak, doğrulama ve teslim ayrı izlenir.')}</p>
        <button type="button" onClick={() => setView('release')}>{t('Sürüm kontrol listesini aç')}</button>
        <p className="caption">{t('Durum yenileme yalnız oturumu okur. Geliştirme kaydı bu UI derlemesine aittir.')}</p></section>
      <section data-testid="development-blocker"><h3>{t('Kayıtlı engel')}</h3>
        <p>{checkpoint?.blocker[language] ?? t('Kabul kaydını inceleyin; tamamlanma varsayılmaz.')}</p>
        {checkpoint ? <small><time dateTime={checkpoint.recorded_at}>{new Date(checkpoint.recorded_at).toLocaleString(locale())}</time></small> : null}</section>
      <section><h3>{t('Çalışma alanı ve ajanlar')}</h3>
        <button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button>
        <button type="button" onClick={() => onNavigate('Scientist')}>{t('Scientist Lab aç')}</button>
        <button type="button" onClick={() => onNavigate('Bilgisayar')}>{t('Bilgisayarı aç')}</button></section>
    </aside></div>
    <section id={panelId + '-history'} hidden={view !== 'history'} className="development-history" data-testid="development-history"><h3>{t('Kanıt ve uygulama geçmişi')}</h3>
    <p className="caption">{t('Geçmiş kayıtlar dar kapsamını korur. Yenileme test, model veya görev başlatmaz.')}</p>
    <p className={runtimeFresh ? 'caption' : 'development-runtime-warning'} data-testid="development-history-observation">
      <strong>{t(runtimeStatus)}</strong> · {t('Son başarılı gözlem:')} {runtimeObservedAt === null ? t('Bilinmiyor') : new Date(runtimeObservedAt).toLocaleString(locale())}
      <br/>{t('Oturum kayıtları son gözlemdir; canlı iş veya geliştirme ajanı etkinliği sayılmaz.')}
    </p>
    <CapabilityEvidence refreshKey={refreshKey}/>
    <div className="development-card" data-testid="development-source-delivery">
      <h3>{t('Son kaynak geliştirmeleri · 1 Ekim 2026')}</h3>
      <p className="caption">{t('Kaynakta uygulandı ve CPU/mock ile doğrulandı. Bu tarihsel kayıt çalışan oturumun build kimliğini veya sürüm kabulünü kanıtlamaz.')}</p>
      <ul>
        <li>{t('Serbest hedef zinciri: scoped catalog → öneri → ayrı onay → sonlu görev → bağımsız sonuç; belirsiz başlangıç yalnız mevcut işten kurtarılır, tekrar yürütülmez.')}</li>
        <li>{t('Bilgi kullanım raporu: tarihsel bağ, güncel kaynak hakları ve native acknowledgement ayrı denetlenir. Eski proposal veya model etiketi gerçek kullanım kanıtı değildir.')}</li>
        <li>{t('Kurulum: ayrı fresh CPU environment ve UI staging; salt okunur prerequisite kontrolü runtime veya GPU hazır demek değildir.')}</li>
        <li>{t('Çok alanlı manuel bootstrap: original kabul edilmiş run ve whole-record receipt → yeniden denetlenen özel skill adayı. CLI ve Tasks exact hash/onay ile bağlı; model olayı uydurulmaz.')}</li>
        <li>{t('Skill yaşam döngüsü: DB-anchored insan incelemesi ve iptal geçmişi; fresh süreç eksik kayıtları reddeder. Exact recovery yalnız önceden yetkili kaydı geri koyar; görev tekrarı, release, aktivasyon veya eğitim değildir.')}</li>
        <li>{t('İncelenmiş skill sürümleri: exact parent/seçim hash’i, ayrı insan onayı ve geri alma CLI/Tasks’a bağlı. Yeni parametrelerle görev yürütme ayrı kapıdır; seçim aktivasyon değildir.')}</li>
        <li>{t('Skill ile yeni görev: seçilmiş manuel sürüm, yeni parametreler ve ayrı onay; özgün bootstrap korunur. Ayrı yürütücüde altı action onayı, tek POST ve bağımsız readback CPU/TLS ile geçti; native GPU kabulü değildir.')}</li>
        <li>{t('Ardışık skill görevleri: aynı oturumda iki yeni görev, exact önceki sonuç bağı ve ayrı onaylarla CPU/TLS ve Chromium arayüzünde geçti. Takılan temizlik yeni görevi engeller; eski sonuçlar okunabilir. Canlı deployment ve native GPU kabulü değildir.')}</li>
        <li>{t('Scientist sonuç bağı: pinli Bonsai yanıt adaptörü ve journal öncesi Decider/recovery/vision kontrolü; original request, evidence/capture ve usage korunur. 133 CPU testi geçti; ortak GPU kabulü ve deployment değildir.')}</li>
        <li>{t('Scientist original kimlik geçmişi: request ile aynı transaction’da stable admission hash’i ve ayrı capability gözlemi; Tasks salt okunur hash gösterir. Legacy kayıt ve belirsizlik kilidi korunur; terminal çözümleme ve gerçek GPU kabulü açık.')}</li>
        <li>{t('Scientist sürümlü kabul: yeni kayıt 2.0 ve exact çıktı bundle pini; eski kayıtlar değişmez. Yanlış pin GPU isteği göndermez. Ortak terminal sözleşmesi ve gerçek GPU kabulü henüz açık; canlı deployment yapılmadı.')}</li>
        <li>{t('Scientist terminal kanıtı: özgün kayıt ve sonuç hash’i bağımsız doğrulanır. GPU bırakımı için ayrıca güvenilir süreç/fencing kanıtı gerekir; idle veya stop ACK yeterli değildir. Belirsizlik kilidi otomatik kaldırılmaz.')}</li>
        <li>{t('Scientist kanıt socket istemcisi: exact hedef yetkisi ve gönderim öncesi kalıcı kontrol kaydı; kimlik değişimi, iptal veya kayıp yanıtta otomatik tekrar yok. Sağlayıcılar varsayılan kapalı; canlı GPU kabulü değildir.')}</li>
        <li>{t('Scientist kalıcı kontrol günlüğü: original DB ve kabul 2.0 bağı, gönderim öncesi istek ve ayrı değişmez yanıt; yeniden açılış bekleyen kontrolü atlayamaz. AGENT/HUMAN cleanup ayrı yetki ister. Yanıt veya sıfır sayaç GPU bırakımı değildir.')}</li>
        <li>{t('Scientist canlı durum görünümü: açık panelde envanter 5 saniyede bir salt okunur yenilenir; son yenileme ve eski bilgi uyarısı görünür. Otomatik deney, tekrar veya GPU devri başlatılmaz.')}</li>
        <li>{t('Scientist tek başlangıç bağlantısı: aynı controller ve DB üzerindeki kabul, peer ve çıktı pinleri tek trusted factory ile bağlı. Bootstrap ağ okuması iptal edilebilir ve UI thread dışında çalışır; gönderim öncesi kalıcı audit GPU yetkisi değildir.')}</li>
        <li>{t('Scientist kaynak doğrulaması: gerçek yapılandırma ve bağımlılık hakları bağımsız incelenmeden canlı bağlantı açılmaz. CPU testleri model veya GPU devrini kanıtlamaz; çalışan oturum otomatik güncellenmez.')}</li>
        <li data-testid="development-scientist-report-history">{t('Scientist rapor geçmişi: bağımsız doğrulanmış rapor ayrı exact-hash isteğiyle özel DB’ye kaydedilir; EN/TR Tasks geçmişi yalnız ayrıca açılır. 128 CPU/sentetik kontrol ve 7 izole arayüz testi geçti; geçmiş güncel durum, eğitim veya GPU bırakımı değildir. Canlı oturum güncellenmedi.')}</li>
        <li data-testid="development-isolated-learning-project">{t('İzole öğrenme projesi: ad ve port ayrı private oturuma bağlı; kendi UI derlemesi, giriş cookie’si ve scoped komutları vardır. İki gerçek CPU fixture oturumu açılıp temiz kapandı; native model, öğrenme veya GPU kabulü değildir. Varsayılan canlı oturum değişmedi.')}</li>
        <li data-testid="development-public-cpu-applications">{t('Public proje akışı: iki ayrı sentetik CRM ve inventory uygulaması, belgelenmiş komutlar ve gerçek CPU tarayıcısıyla çalıştı. Her görev altı ayrı onay, tek POST ve bütün alanların bağımsız makbuz doğrulamasını tamamladı. Model kararları fixture; native S1/S2, öğrenilmiş skill veya genel iki-uygulama kabulü değildir.')}</li>
        <li data-testid="development-skill-record-recovery">{t('Skill sürüm kaydı kurtarma: eksik release veya selection dosyası, original DB kaydı ve ayrı exact insan onayıyla CLI veya Tasks/API üzerinden geri konur. Kaynak yeniden denetlenir; seçim, görev, model ve DB değişmez. Belirsiz yanıt otomatik tekrar edilmez. Canlı deployment değildir.')}</li>
      </ul>
      <p>{t('Kalan entegrasyon: Scientist ortak capability/principal/cancel/drain sözleşmesi ve yalnız Scientist oturumunun yürüteceği gerçek GPU kabulü; iki uygulamalı genel görev kabulü de açık.')}</p>
      <button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button>
    </div>
    <div className="development-card" data-testid="development-backend-capability">
      <h3>{t('Canlı backend · görsel plan taslağı')}</h3>
      <p>{imageDraftCapability === 'checking' ? t('Backend yeteneği kontrol ediliyor…')
        : imageDraftCapability === 'supported' ? t('Bu backend PNG/JPEG/WebP/GIF taslaklarını sunuyor; mevcut oturumda görev pini veya site izni olduğu anlamına gelmez.')
        : t('Bu backend görsel plan yeteneğini doğrulamıyor. Tarihli kod/test kartları çalışan Python oturumuna sıcak uygulanmaz; güncelleme sonrası güvenli yeni oturum gerekir.')}</p>
      <p data-testid="development-static-query-capability">{staticQueryCapability === 'checking' ? t('Statik sorgu desteği kontrol ediliyor…')
        : staticQueryCapability === 'supported' ? t('Bu backend exact statik varlık sorgusu taslağını destekliyor; görev veya site izni vermez.')
        : t('Bu backend exact statik varlık sorgusu taslağını doğrulamıyor; sorgusuz plan kullanın veya güvenli yeni oturum açın.')}</p>
    </div>
    <div className="development-card" data-testid="development-system1-preparation">
      <h3>{t('Canlı System-1 hazırlığı')}</h3>
      <p data-testid="development-system1-preparation-state">{t(deciderPreparationLabel)}</p>
      <p className="caption">{t(deciderPreparation?.enabled && deciderPreparation.state === 'ready_gpu'
        ? 'Canlı worker CUDA hazırlığı bildiriyor; bu kart VRAM miktarını, sonraki çıkarımı veya görev başarısını ölçmez. Vision öncesi model bırakılır.'
        : 'Yalnız boşta hazırlık durumudur; görev sırasındaki GPU kullanımı veya model başarısı bu alandan çıkarılamaz.')}</p>
    </div>
    {latestJob ? <div className="development-card development-task-progress" data-testid="development-task-progress">
      <div className="development-heading"><h3>{t('Son gözlemlenen görev')}</h3><button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button></div>
      <p data-testid="development-current-job"><code>{latestJob.kind}</code> · {latestJob.status}</p>
      <p>{t('Kalıcı evre:')} <strong data-testid="development-task-phase">{latestJob.progress?.phase ?? '—'}</strong> · {t('Geçen süre:')} <strong data-testid="development-task-elapsed">{elapsedText}</strong></p>
      {latestJob.status === 'failed' && latestJob.progress?.failure_code ? <p data-testid="development-task-failure">{t('Doğrulanmış hata kodu:')} <code>{latestJob.progress.failure_code}</code></p> : null}
      {latestJob.status === 'waiting_approval' ? <p role="status">{t('Görev eylem onayı bekliyor; ayrıntılar Görevler ekranında.')}</p> : null}
      <p className="caption">{t('Evre ve süre sunucu kaydından gelir; tamamlanma yüzdesi veya uygulama sonucu değildir.')}</p>
    </div> : null}
    {tasks?.restart_quiesced === true ? <p className="development-release" data-testid="development-restart-quiesced">{tasks.manager_scope
      ? <>{t('Yalnız bu adlandırılmış proje ve exact oturum için restart kilidini kaldırın; varsayılan oturumun komutunu kullanmayın.')}
        {/^[a-z0-9][a-z0-9-]{0,47}$/.test(tasks.manager_scope.project)
          && Number.isInteger(tasks.manager_scope.port) && tasks.manager_scope.port >= 1024
          && tasks.manager_scope.port <= 65535 && tasks.manager_scope.port !== 8765
          && typeof tasks.manager_session === 'string' && /^app-[a-f0-9]{32}$/.test(tasks.manager_session)
          ? <code>{`./scripts/aos-v1 release-restart --project ${tasks.manager_scope.project} --project-port ${tasks.manager_scope.port} --expected-session ${tasks.manager_session}`}</code>
          : <span>{t('Manager oturum kimliği doğrulanmadı; kapsam dışı komut gösterilmez.')}</span>}</>
      : t('Restart görev kabul kilidi açık: yeni görevler ve kontroller reddedilir. Manager durumunu inceleyin; güvenliyse hostta ./scripts/aos-v1 release-restart çalıştırın.')}</p> : null}
    <div className="development-card development-onboarding" data-testid="development-onboarding">
      <h3>{t('İlk web uygulaması kurulumu')}</h3>
      <p>{webApplications === null ? t('Profil taslakları yükleniyor…') : webApplications === 'unavailable' ? t('Bu backend profil envanterini sunmuyor.') : webApplications.length === 0 ? t('Kayıtlı hedef profil taslağı yok.') : <>{t('Kayıtlı profil taslağı:')} {webApplications.length}. {t('Taslaklar yürütme veya veri hakkı değildir.')}</>}</p>
      <p>{tasks === null ? t('Oturum kapsamı yükleniyor…') : remoteTaskPinned ? t('Bu oturumda uzak görev kapsamı pinli; gerçek site izni veya başarısı kanıtlanmadı.') : t('Bu oturumda uzak görev kapsamı pinli değil.')}</p>
      <p data-testid="development-next-setup-step"><strong>{t('Sıradaki kurulum adımı:')}</strong> {t(nextSetupStep)}</p>
      <button type="button" onClick={() => onNavigate('Web uygulamaları')}>{t('Web uygulamalarını aç')}</button>
      {remoteTaskPinned ? <button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button> : null}
      <p className="caption">{t('Profil kaydı veya görev pini W1 kabulü değildir; hedef URL, yetkili hesap, izinli görev ve bağımsız sonuç kanıtı gerekir.')}</p>
    </div>
    <div className="development-heading"><h3>{t('Son gözlemlenen oturum kanıtı')}</h3>{!latestJob ? <button type="button" onClick={() => onNavigate('Görevler')}>{t('Görevleri aç')}</button> : null}</div>
    <p className="caption">{t('Bu sayılar mevcut sunucu yanıtından okunur; görev kapsamı veya ürün kabul yüzdesi değildir.')}</p>
    <div className="development-live" data-testid="development-live-evidence">
      <article><span>{t('Sunulan sabit görev türleri')}</span><strong>{tasks ? tasks.available ? tasks.kinds?.length ?? 0 : 0 : '—'}</strong><small>{t('Sunulması başarı kanıtı değildir.')}</small></article>
      <article><span>{t('Son 20 oturum görevinde başarılı')}</span><strong>{tasks ? `${recentJobs.filter(job => job.status === 'succeeded').length}/${recentJobs.length}` : '—'}</strong><small>{t('Yalnız bu oturumun son görev kayıtları.')}</small></article>
      <article><span>{t('Son 30 run içinde doğrulanmış başarı')}</span><strong>{recentRuns ? `${verifiedRuns}/${recentRuns.length}` : '—'}</strong><small>{t('Trajectory DB genelindeki son kayıtlar; hedef site sonucu değildir.')}</small></article>
    </div>
    <div className="development-heading"><h3>{t('Son görevde S1/S2 kanıtı')}</h3></div>
    <p className="caption">{modelCallTotals
      ? t('Çağrı sayıları son görevin tüm tamamlanmış model çağrılarındandır; görev başarısı veya eğitim örneği değildir. İzinli metadata yalnız exact uzak görev izninin özel outbox özetidir.')
      : t('Bu backend toplam çağrı sayılarını sunmuyor; yalnız son görevin en yeni en çok 10 tamamlanmış çağrısı gösterilir. İzinli metadata yalnız exact uzak görev izninin özel outbox özetidir.')}</p>
    <div className="development-live" data-testid="development-model-evidence">
      <article data-testid="development-s1-calls"><span>{t('S1 çağrıları · başarılı/toplam')}</span><strong>{system1Calls ? `${system1Calls.ok}/${system1Calls.total}` : '—'}</strong><small>{modelCallTotals ? t('Run’a bağlı tamamlanmış çağrıların tamamı.') : t('Son görevin en yeni 10 çağrısıyla sınırlı.')}</small></article>
      <article data-testid="development-s2-calls"><span>{t('S2 çağrıları · başarılı/toplam')}</span><strong>{system2Calls ? `${system2Calls.ok}/${system2Calls.total}` : '—'}</strong><small>{t('Çağrı yoksa Bonsai sonucu varsayılmaz.')}</small></article>
      <article data-testid="development-remote-metadata"><span>{t('İzinli uzak metadata adayları · S1/S2')}</span><strong>{remoteEntries ? `${remoteEntries.system1}/${remoteEntries.system2}` : '—'}</strong><small>{latestJob?.remote_learning_metadata?.enabled ? t('Yalnız içeriksiz outbox adayları; dataset veya gold değil.') : t('Son görevde uzak metadata izni bağlı değil.')}</small></article>
    </div>
    <div className="development-heading"><h3>{t('Son görevde model gecikmesi')}</h3></div>
    <p className="caption">{t('Yalnız son görevin başarılı ve sonlu model çağrıları; en çok 256 örnek ve en yakın sıra yüzdeliği. Küçük örneklem, görev süresi veya W6 hız kabulü değildir.')}</p>
    <div className="development-live" data-testid="development-model-latency">
      <article data-testid="development-s1-latency"><span>{t('S1 gecikmesi · p50/p95')}</span><strong>{latencyText('system1')}</strong></article>
      <article data-testid="development-s2-latency"><span>{t('S2 gecikmesi · p50/p95')}</span><strong>{latencyText('system2')}</strong></article>
    </div>
    <div className="development-retention" data-testid="development-form-repeats">
      <h3>{t('Aynı HTTPS form planında tekrar ölçümü')}</h3>
      <p>{formRepeats === null ? t('Tekrar ölçümü yükleniyor…')
        : formRepeats === 'not-configured' ? t('Bu backend’de HTTPS form planı pinli değil veya ölçüm API’si yok.')
        : formRepeats === 'unavailable' ? t('En az iki tamamlanmış bağlı deneme ve değişmemiş kaynak gerekiyor; ölçüm kullanılamıyor.')
        : repeatReport?.mode === 'read_only_post_admission_form_state_repeats'
          ? t('Yalnız bu trajectory DB’deki exact form ve durum planının kayıtlı denemeleri.')
          : t('Yalnız bu trajectory DB’deki exact temel planın kayıtlı denemeleri.')}</p>
      {repeatReport ? <div className="development-live">
        <article data-testid="development-form-repeat-outcomes"><span>{t('Taşıma readback’i doğrulandı / bağlı deneme')}</span><strong>{repeatReport.transport_verified}/{repeatReport.bound_attempts}</strong><small>{t('Başarısız ve iptal edilenler paydada kalır; uygulama sonucu değildir.')}</small></article>
        <article data-testid="development-form-repeat-elapsed"><span>{t('Toplam süre · p50/p95')}</span><strong>{formatDuration(repeatReport.elapsed_p50_ms)} / {formatDuration(repeatReport.elapsed_p95_ms)} ms</strong><small>{t('Onay pencereleri dahildir.')}</small></article>
        <article data-testid="development-form-repeat-approval"><span>{t('Onay penceresi · toplam / sayı')}</span><strong>{formatDuration(repeatReport.approval_window_sum_ms)} ms · n={repeatReport.approval_window_count}</strong><small>{t('Onay isteğinden kapanışa kadar; manuel ve toplu onay dahildir.')}</small></article>
        <article data-testid="development-form-repeat-excluding-approval"><span>{t('Onay penceresi dışı süre · p50/p95')}</span><strong>{formatDuration(repeatReport.elapsed_excluding_approval_p50_ms)} / {formatDuration(repeatReport.elapsed_excluding_approval_p95_ms)} ms</strong><small>{t('Model, tarayıcı, ağ ve scheduler süreleri hâlâ birliktedir.')}</small></article>
        <article data-testid="development-form-repeat-first"><span>{t('İlk open intent’i · p50/p95')}</span><strong>{repeatReport.first_action_samples && repeatReport.first_action_p50_ms !== null && repeatReport.first_action_p95_ms !== null ? `${formatDuration(repeatReport.first_action_p50_ms)} / ${formatDuration(repeatReport.first_action_p95_ms)} ms · n=${repeatReport.first_action_samples}` : '—'}</strong><small>{t('Görünür tarayıcı etkisinin zamanı değildir.')}</small></article>
      </div> : null}
      {repeatReport ? <p className="caption">{t('Model çağrısı / ölçülmüş gecikme:')} {repeatReport.model_call_count} / {repeatReport.model_latency_samples} · {t('S1/S2 kayıtları; site sonucu ve W6 kabulü doğrulanmadı.')}</p> : null}
      {repeatReport?.schema_version === '1.2' && repeatReport.transport_verified > 0 && repeatReport.post_approval_stage_ms ? <>
        <p className="caption">{t('Onay tüketiminden eylem tamamlanmasına p50/p95; yalnız taşıması doğrulanmış koşular. Scheduler, tarayıcı ve ağ birlikte; saf tarayıcı süresi değildir.')}</p>
        <div className="development-live" data-testid="development-form-stage-timing">
          {Object.entries(formStageLabels).filter(([tool]) => repeatReport.post_approval_stage_ms?.[tool]).map(([tool, label]) => {
            const stage = repeatReport.post_approval_stage_ms?.[tool];
            return stage ? <article key={tool} data-testid={`development-form-stage-${tool.replace(/[._]/g, '-')}`}>
              <span>{t(label)} · p50/p95</span><strong>{formatDuration(stage.p50_ms)} / {formatDuration(stage.p95_ms)} ms · n={stage.samples}</strong>
            </article> : null;
          })}
        </div>
      </> : null}
      <p className="caption">{repeatReport?.mode === 'read_only_post_admission_form_state_repeats'
        ? t('Altı-onaylı durum planı ve dört-onaylı temel plan ayrı ölçülür; Cookie kapsam dışı. Ölçüm yeni görev veya eğitim başlatmaz.')
        : repeatReport ? t('Yalnız dört-onaylı temel form; statik Cookie/durum planı kapsam dışı. Ölçüm yeni görev veya eğitim başlatmaz.')
          : t('Temel ve durum planları ayrı ölçülür; statik Cookie kapsam dışı. Ölçüm yeni görev veya eğitim başlatmaz.')}</p>
    </div>
    <div className="development-retention" data-testid="development-retention">
      <h3>{t('Metadata saklama süresi taraması')}</h3>
      <p>{retention === 'unavailable' ? t('Bu backend saklama süresi durumunu sunmuyor.')
        : retention === null ? t('Saklama süresi durumu yükleniyor…')
        : !retention.enabled ? t('Bu oturumda managed tarama kapalı.')
        : retention.state === 'pending' ? t('İlk tarama bekleniyor.')
        : retention.state === 'ok' ? t('Son tarama tamamlandı.')
        : retention.state === 'incomplete' ? t('Son tarama eksik; özel store incelemesi gerekli.')
        : retention.state === 'stale' ? t('Son tarama güncel değil; managed backend zamanlayıcısını inceleyin.')
        : t('Son tarama kullanılamadı; özel store incelemesi gerekli.')}</p>
      {retention && retention !== 'unavailable' && retention.enabled && retention.last_attempt_at ?
        <p>{t('Son deneme:')} {new Date(retention.last_attempt_at).toLocaleString(locale())} · {t('Oturum:')} {retention.session_count ?? '—'} · {t('Silinen outbox:')} {retention.purged_count ?? '—'} · {t('Engel:')} {retention.failed_session_count === null || retention.failed_consent_count === null ? '—' : retention.failed_session_count + retention.failed_consent_count}</p> : null}
      <p className="caption">{t('Yalnız bu backend sürecinin son tarama özeti; gerçek hedef veri hakkı veya fiziksel silme kanıtı değildir.')}</p>
    </div>
    {!latestJob ? <p className="caption" data-testid="development-current-job">{t('En son görev:')} {t('Bu oturumda görev kaydı yok veya henüz yüklenmedi.')}</p> : null}
    <button type="button" onClick={() => onNavigate('Çalışmalar')}>{t('Çalışma kayıtlarını aç')}</button>
    <details data-testid="development-historical-checklist"><summary>{t('Tarihsel dar dilimler · 30 Eylül 2026')}</summary>
    <p className="caption" data-testid="development-checklist-date">{t('Kontrol listesi gözlemi: 30 Eylül 2026. Dar kabul kanıtları STATUS içinde tarihli tutulur.')}</p>
    <div className="development-grid">{completed.map(item => <article className="development-card" key={item.title}><span className="development-state">{t('Uygulandı / dar kapsamda doğrulandı')}</span><h4>{t(item.title)}</h4><p>{t(item.detail)}</p><small>{t(item.evidence)}</small></article>)}</div>
    </details>
    <h3 data-testid="development-swapp-deferred">{t('SWAPP W1–W6 · ayrı ve ertelenmiş kabul; tamamlanmadı')}</h3>
    <ul className="development-gates">{openGates.map(item => <li key={item}>{t(item)}</li>)}</ul>
    <p className="caption">{t('Ayrıntılı test kanıtı: docs/STATUS.md. Tarihli kontrol listesi statiktir; canlı oturum sayıları sunucudan gelir, yenileme test çalıştırmaz.')}</p>
    </section>
    <p className="development-release" data-testid="development-work-order">{t('Önce yerel AOS geliştirmeleri. SWAPP intranet/tünel bağlantısı ve gerçek-site kabulü son aşamaya ertelendi; diğer işleri engellemez ve tamamlanmış sayılmaz.')}</p>
  </section>;
}
