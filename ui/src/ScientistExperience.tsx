import {useEffect, useState} from 'react';
import {t} from './i18n';

export interface ScientistHistorySelection {
  source_run_id: string;
  source_report_sha256: string;
  records: {experiment_id: string; experiment_sha256: string; trajectory_sha256: string}[];
}

interface ExperienceRecord {
  record_id: string;
  experiment_id: string | null;
  experiment_sha256: string | null;
  trajectory_sha256: string | null;
  method: string | null;
  decision: string | null;
  score: number | null;
  history_eligibility: {eligible: boolean; reasons: string[]};
  training_eligibility: {eligible: boolean};
}

interface ExperienceReadback {
  schema_version: string;
  run_id: string;
  report_sha256: string;
  independent_report_verified: boolean;
  ledger_verification: string;
  execution_authorized: boolean;
  experience: {schema: string; run_id: string; report_sha256: string; training_started: boolean; holdout_included: boolean;
    records: ExperienceRecord[];
    field_context_usage?: {status: string; context_bound_proposal_count: number} | null;
    prior_findings_usage?: {status: string; context_bound_proposal_count: number} | null};
}

function parsedReadback(value: unknown): ExperienceReadback | null {
  if (!value || typeof value !== 'object') return null;
  const result = value as ExperienceReadback;
  const experience = result.experience;
  const hash = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  if (result.schema_version !== 'aos.scientist-experience-readback.v1' || result.independent_report_verified !== true
      || result.ledger_verification !== 'scientist-reported-not-independently-replayed' || result.execution_authorized !== false
      || typeof result.run_id !== 'string' || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(result.run_id)
      || !hash(result.report_sha256) || experience?.schema !== 'run-experience.v1'
      || experience.run_id !== result.run_id || experience.report_sha256 !== result.report_sha256
      || experience.training_started !== false || experience.holdout_included !== false
      || !Array.isArray(experience.records) || experience.records.length > 200
      || !experience.records.every(record => record && typeof record.record_id === 'string'
        && (record.method === null || typeof record.method === 'string')
        && (record.decision === null || typeof record.decision === 'string')
        && (record.score === null || typeof record.score === 'number' && Number.isFinite(record.score))
        && record.training_eligibility?.eligible === false && typeof record.history_eligibility?.eligible === 'boolean'
        && Array.isArray(record.history_eligibility.reasons) && record.history_eligibility.reasons.every(reason => typeof reason === 'string')
        && (!record.history_eligibility.eligible || typeof record.experiment_id === 'string'
          && /^exp_[a-f0-9]{32}$/.test(record.experiment_id) && hash(record.experiment_sha256) && hash(record.trajectory_sha256)))) return null;
  if (new Set(experience.records.map(record => record.record_id)).size !== experience.records.length) return null;
  for (const usage of [experience.field_context_usage, experience.prior_findings_usage]) {
    if (usage != null && (!Number.isSafeInteger(usage.context_bound_proposal_count) || usage.context_bound_proposal_count < 0
        || !['admitted-only', 'context-bound'].includes(usage.status)
        || (usage.status === 'context-bound') !== (usage.context_bound_proposal_count > 0))) return null;
  }
  return result;
}

export function ScientistExperience({value, disabled, onSelect}: {
  value: unknown; disabled: boolean; onSelect: (selection: ScientistHistorySelection) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => { setSelected([]); }, [value]);
  const readback = parsedReadback(value);
  if (!readback) return <p role="alert">{t('Deney geçmişi doğrulanamadı; kayıt seçilemez.')}</p>;
  const records = readback.experience.records.filter(record => selected.includes(record.record_id) && record.history_eligibility.eligible);
  const select = () => {
    if (!records.length || records.length > 8 || disabled) return;
    onSelect({source_run_id: readback.run_id, source_report_sha256: readback.report_sha256,
      records: records.map(record => ({experiment_id: record.experiment_id!,
        experiment_sha256: record.experiment_sha256!, trajectory_sha256: record.trajectory_sha256!}))});
  };
  return <section data-testid="scientist-experience">
    <h3>{t('Deney geçmişi ve bağlam kullanımı')}</h3>
    <p>{t('Rapor AOS tarafından bağımsız doğrulandı. Deney günlüğü ve bağlam kullanımı Scientist tarafından bildirildi; AOS trajectory kayıtlarını yeniden yürütmedi. Eğitim izni değildir.')}</p>
    <p>{t('Kaynak rapor')}: <code>{readback.report_sha256}</code></p>
    {(['field_context_usage', 'prior_findings_usage'] as const).map(kind => {
      const usage = readback.experience[kind];
      return <p key={kind}>{t(kind === 'field_context_usage' ? 'Saha amacı kullanımı' : 'Seçili geçmiş kullanımı')}: {' '}
        {usage ? t(usage.status === 'context-bound' ? 'Öneri bağlamına bağlandı' : 'Yalnız kabul edildi; öneri bağlamı yok') : t('Bu istekte seçilmedi')}
        {usage ? ` · ${usage.context_bound_proposal_count}` : ''}</p>;
    })}
    <p>{t('Geçmişi taslağa taşımak için en fazla sekiz kayıt seçin. DISCARD da ölçülmüş geçmiş olabilir; iyileşme veya eğitim başarısı değildir.')}</p>
    {!readback.experience.records.length ? <p>{t('Görüntülenebilir deney kaydı yok.')}</p> : null}
    {readback.experience.records.map(record => <div key={record.record_id}>
      <label><input type="checkbox" checked={selected.includes(record.record_id)}
        disabled={disabled || !record.history_eligibility.eligible || selected.length >= 8 && !selected.includes(record.record_id)}
        onChange={event => setSelected(previous => event.target.checked ? [...previous, record.record_id] : previous.filter(item => item !== record.record_id))}/>
        {record.experiment_id || record.record_id} · {record.method || '—'} · {record.decision || '—'} · {record.score ?? '—'}</label>
      {!record.history_eligibility.eligible ? <p>{record.history_eligibility.reasons.join(', ')}</p> : null}
    </div>)}
    <button disabled={disabled || !records.length || records.length > 8} onClick={select}>{t('Seçili referansları yeni öneri taslağına taşı')}</button>
    <p>{t('Bu seçim yalnız taslağı değiştirir; yeni deney için güncel yetki, yeni öneri ve ayrı onay gerekir.')}</p>
  </section>;
}
