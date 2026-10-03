import {useLanguage} from './i18n';
import './system-topology.css';

const content = {
  en: {
    eyebrow: 'AOS / ARCHITECTURE', title: 'How the system works',
    intro: 'AOS turns a scoped goal into controlled actions, then checks what actually happened.',
    notice: 'Architecture guide — not live telemetry. Connections describe responsibilities, not running processes or granted permissions.',
    flow: 'One task, seven checkpoints', implemented: 'Implemented task path',
    steps: [
      ['Your goal', 'Choose the application, workspace, allowed effects and budget.'],
      ['AOS orchestrator', 'Bind the task to the current session and authority; coordinate the required components.'],
      ['System 1 · Decider', 'Choose from finite typed actions. A model suggestion cannot expand authorization.'],
      ['Policy, approval & intent', 'Check scope and fresh approval; persist the exact action intent before execution.'],
      ['Executor & computer', 'Use authorized tools inside the isolated Ubuntu workspace: Chromium and Playwright MCP for web tasks.'],
      ['Independent verification', 'Read the resulting file, page or report independently. A successful tool response alone is not success.'],
      ['Trace & outcome', 'Keep the run, actions and verification in the trajectory store. Preserve uncertain effects without blind replay.'],
    ],
    support: 'Supporting components', optional: 'Used when needed', partial: 'Partial integration', future: 'Future extension',
    supervisor: 'System 2 · Bonsai',
    supervisorBody: 'Planning, visual interpretation and recovery advice. It supports the task path; not every action needs an S2 call. The same policy and executor remain in control.',
    scientist: 'AI Scientist · first external agent',
    scientistBody: 'AOS sends a typed, approved study and follows its status. Scientist runs experiments and scoring; AOS independently verifies the returned report. Controlled CPU evidence exists; shared GPU and current UI acceptance remain separate.',
    scientistRoute: 'AOS → approved study → Scientist → report → AOS verification',
    extensions: 'More agents, the same boundaries',
    extensionsBody: 'New agent adapters must declare capabilities and obey task scope, budgets, cancellation and result checks. A general plugin system and unrestricted replication are not delivered.',
    gpu: 'One GPU authority · 16 GB VRAM target',
    gpuBody: 'For the shared-runtime target, Scientist’s scheduler/broker is the single GPU allocation authority. AOS does not add a second scheduler. Models and experiments can take turns; simultaneous residency is not required.',
    gpuLimit: 'Shared GPU integration is not accepted yet. Idle or quiesced does not mean VRAM is released: worker drain, fencing and independent cleanup evidence are required.',
    learning: 'Improvement is a reviewed workflow',
    learningRoute: 'Verified experience → reviewed knowledge / skill or dataset → separate evaluation → explicit promotion',
    learningBody: 'Retrieval supplies context, skills reuse reviewed steps, and authorized data can support later LoRA/QLoRA experiments. None of these grants new site access or starts training automatically. General learning quality remains unproven.',
    legend: 'Read the map correctly',
    legendBody: 'Implemented means source paths exist, not that every deployment passed acceptance. Partial and future cards are open work. See Development for dated evidence and separately observed session state.',
    tasks: 'Open Tasks', lab: 'Open Scientist', development: 'View development evidence',
  },
  tr: {
    eyebrow: 'AOS / MİMARİ', title: 'Sistem nasıl çalışır?',
    intro: 'AOS, kapsamı belirlenmiş hedefi kontrollü eylemlere çevirir; ardından gerçekte ne olduğunu doğrular.',
    notice: 'Mimari rehberidir; canlı telemetri değildir. Bağlantılar sorumlulukları anlatır, çalışan süreç veya verilmiş izin göstermez.',
    flow: 'Bir görev, yedi kontrol noktası', implemented: 'Uygulanmış görev yolu',
    steps: [
      ['Hedefiniz', 'Uygulamayı, çalışma alanını, izinli etkileri ve bütçeyi seçin.'],
      ['AOS orkestratörü', 'Görevi güncel oturuma ve yetkiye bağlar; gerekli bileşenleri koordine eder.'],
      ['System 1 · Decider', 'Sonlu typed eylemler arasından seçim yapar. Model önerisi yetkiyi genişletemez.'],
      ['Policy, onay ve intent', 'Kapsamı ve taze onayı denetler; tam eylem niyetini yürütmeden önce kalıcılaştırır.'],
      ['Executor ve bilgisayar', 'İzole Ubuntu çalışma alanındaki yetkili araçları kullanır: web görevlerinde Chromium ve Playwright MCP.'],
      ['Bağımsız doğrulama', 'Oluşan dosyayı, sayfayı veya raporu bağımsız okur. Başarılı araç yanıtı tek başına başarı değildir.'],
      ['İz ve sonuç', 'Koşu, eylem ve doğrulamayı trajectory kaydında tutar. Belirsiz etkileri körlemesine tekrarlamaz.'],
    ],
    support: 'Destekleyici bileşenler', optional: 'Gerektiğinde kullanılır', partial: 'Kısmi entegrasyon', future: 'Gelecek genişletme',
    supervisor: 'System 2 · Bonsai',
    supervisorBody: 'Planlama, görsel yorumlama ve toparlanma önerileri üretir. Görev yolunu destekler; her eylem için S2 çağrısı gerekmez. Aynı policy ve executor sınırları korunur.',
    scientist: 'AI Scientist · ilk harici ajan',
    scientistBody: 'AOS typed ve onaylı deneyi gönderip durumunu izler. Scientist deney ve puanlamayı yürütür; AOS dönen raporu bağımsız doğrular. Kontrollü CPU kanıtı vardır; ortak GPU ve güncel UI kabulü ayrıdır.',
    scientistRoute: 'AOS → onaylı deney → Scientist → rapor → AOS doğrulaması',
    extensions: 'Yeni ajanlar, aynı sınırlar',
    extensionsBody: 'Yeni ajan adaptörleri yeteneklerini bildirmeli; görev kapsamı, bütçe, iptal ve sonuç denetimine uymalıdır. Genel plugin sistemi ve sınırsız çoğaltma teslim edilmedi.',
    gpu: 'Tek GPU otoritesi · 16 GB VRAM hedefi',
    gpuBody: 'Ortak runtime hedefinde GPU tahsisinin tek otoritesi Scientist scheduler/broker mekanizmasıdır. AOS ikinci scheduler eklemez. Modeller ve deneyler sırayla çalışabilir; aynı anda bellekte bulunmaları gerekmez.',
    gpuLimit: 'Ortak GPU entegrasyonu henüz kabul edilmedi. Idle veya quiesce, VRAM bırakıldı demek değildir: worker drain, fencing ve bağımsız cleanup kanıtı gerekir.',
    learning: 'Gelişim, incelenen bir iş akışıdır',
    learningRoute: 'Doğrulanmış deneyim → incelenmiş bilgi / skill veya veri seti → ayrı değerlendirme → açık promotion',
    learningBody: 'Retrieval bağlam sağlar, skill incelenmiş adımları yeniden kullanır; izinli veri ileride LoRA/QLoRA deneylerine kaynak olabilir. Bunlar yeni site yetkisi vermez veya otomatik eğitim başlatmaz. Genel öğrenme kalitesi henüz kanıtlanmadı.',
    legend: 'Şema nasıl okunur?',
    legendBody: 'Uygulanmış, kaynak yolunun bulunduğu anlamına gelir; her deployment kabulü değildir. Kısmi ve gelecek kartları açık işlerdir. Tarihli kanıt ve ayrı gözlenen oturum durumu için Geliştirme ekranını kullanın.',
    tasks: 'Görevleri aç', lab: 'Scientist ekranını aç', development: 'Geliştirme kanıtını gör',
  },
};

export function SystemTopology({onNavigate}: {onNavigate: (tab: 'Görevler' | 'Scientist' | 'Geliştirme') => void}) {
  const copy = content[useLanguage()];
  return <div className="system-topology" data-testid="system-topology">
    <section className="topology-intro">
      <p className="eyebrow">{copy.eyebrow}</p><h2>{copy.title}</h2><p>{copy.intro}</p>
      <p className="topology-notice" role="note">{copy.notice}</p>
    </section>
    <section aria-labelledby="topology-flow-title">
      <div className="topology-heading"><h2 id="topology-flow-title">{copy.flow}</h2><span className="topology-badge">{copy.implemented}</span></div>
      <ol className="topology-flow" aria-label={copy.flow}>
        {copy.steps.map(([title, description], index) => <li key={index}>
          <span className="topology-step" aria-hidden="true">{index + 1}</span><h3>{title}</h3><p>{description}</p>
        </li>)}
      </ol>
    </section>
    <section aria-labelledby="topology-support-title">
      <h2 id="topology-support-title">{copy.support}</h2>
      <div className="topology-branches">
        <article className="topology-card"><span className="topology-badge">{copy.optional}</span><h3>{copy.supervisor}</h3><p>{copy.supervisorBody}</p></article>
        <article className="topology-card topology-partial"><span className="topology-badge">{copy.partial}</span><h3>{copy.scientist}</h3><p>{copy.scientistBody}</p><p className="topology-route">{copy.scientistRoute}</p></article>
        <article className="topology-card topology-future"><span className="topology-badge">{copy.future}</span><h3>{copy.extensions}</h3><p>{copy.extensionsBody}</p></article>
      </div>
    </section>
    <section className="topology-card topology-gpu" aria-labelledby="topology-gpu-title">
      <span className="topology-badge">{copy.partial}</span><h2 id="topology-gpu-title">{copy.gpu}</h2><p>{copy.gpuBody}</p><p className="topology-notice">{copy.gpuLimit}</p>
    </section>
    <section className="topology-card" aria-labelledby="topology-learning-title">
      <h2 id="topology-learning-title">{copy.learning}</h2><p className="topology-route">{copy.learningRoute}</p><p>{copy.learningBody}</p>
    </section>
    <section className="topology-footer"><h2>{copy.legend}</h2><p>{copy.legendBody}</p>
      <div className="topology-actions"><button onClick={() => onNavigate('Görevler')}>{copy.tasks}</button><button onClick={() => onNavigate('Scientist')}>{copy.lab}</button><button onClick={() => onNavigate('Geliştirme')}>{copy.development}</button></div>
    </section>
  </div>;
}
