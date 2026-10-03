import catalog from '../../config/model_candidates.json';
import {useLanguage} from './i18n';
import './model-candidates.css';

export function ModelCandidates() {
  const language = useLanguage();
  const copy = language === 'tr' ? {
    title: 'S1 / S2 alternatif model adayları', status: 'Aday · runtime kabulü yok',
    limit: 'Araştırma kataloğu; çalışan model veya seçilebilir deployment değildir. Varsayılan Decider/Bonsai değişmedi. Bu ekran indirme, GPU çağrısı veya promotion yapmaz.',
    date: 'Kaynak incelemesi', role: 'Hedef rol', plan: 'Kabul için gereken', source: 'Resmî model kartı',
    unknown: 'AOS hız / VRAM ölçümü: henüz yok', revision: 'İncelenen kaynak revision',
    license: 'Upstream lisans beyanı', artifact: 'Resmî quantized artifact',
    local: '16 GB hedefi · ölçüm bekliyor', reserve: 'Büyük GPU için rezerv aday',
    training: 'Ayrı Unsloth LoRA / QLoRA taslak profili var; eğitim çalıştırıcısı ve AOS uyumluluğu henüz doğrulanmadı.',
    head: 'Clef joint head için ayrı loss, target-module ve adaptör yükleme incelemesi gerekli.',
  } : {
    title: 'S1 / S2 alternative model candidates', status: 'Candidate · no runtime acceptance',
    limit: 'Research catalog, not running models or selectable deployments. Default Decider/Bonsai are unchanged. This panel does not download, invoke GPU inference or promote models.',
    date: 'Source review', role: 'Target role', plan: 'Required for acceptance', source: 'Official model card',
    unknown: 'AOS latency / VRAM measurements: not available', revision: 'Reviewed source revision',
    license: 'Upstream license declaration', artifact: 'Official quantized artifact',
    local: '16 GB target · measurements pending', reserve: 'Larger-GPU reserve candidate',
    training: 'Separate Unsloth LoRA / QLoRA design profile; training runner and AOS compatibility are not yet verified.',
    head: 'Clef joint head requires separate loss, target-module and adapter-loading review.',
  };
  return <section className="panel model-candidates" data-testid="model-candidates">
    <h2>{copy.title}</h2><p className="caption">{copy.limit}</p>
    <p className="caption">{copy.date}: <time dateTime={catalog.observed_at}>{catalog.observed_at}</time></p>
    <div className="model-candidates-grid">{catalog.candidates.map(candidate => <article key={candidate.id} data-testid={'model-candidate-' + candidate.id}>
      <span className="badge">{copy.status}</span><h3>{candidate.name}</h3>
      <p data-testid="model-capacity-tier">{candidate.capacity_tier === 'larger_gpu_reserve' ? copy.reserve : copy.local}</p>
      <p>{copy.role}: <strong>{candidate.role === 'system1' ? 'System 1 · Operator' : 'System 2 · Supervisor'}</strong></p>
      <p>{candidate.rationale[language]}</p>
      <p>{copy.unknown}</p>
      <p data-testid="model-training-status">{copy.training}</p>
      <details><summary>{copy.plan}</summary><p>{candidate.evaluation_plan[language]}</p>
        {candidate.execution_kind === 'typed_choice_probabilities' ? <p>{copy.head}</p> : null}
        <p>{copy.license}: <code>{candidate.license_reported}</code></p>
        <p>{copy.revision}: <code>{candidate.repository}@{candidate.revision}</code></p></details>
      <a href={candidate.source_url} target="_blank" rel="noopener noreferrer">{copy.source}</a>
      {candidate.quantization_reference ? <p><a href={candidate.quantization_reference.source_url} target="_blank" rel="noopener noreferrer">{copy.artifact}</a></p> : null}
    </article>)}</div>
  </section>;
}
