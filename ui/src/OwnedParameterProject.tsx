import {useLanguage} from './i18n';

type Entry = {intent_sha256: string; job_id: string; manifest_sha256: string;
  status: 'unresolved' | 'accepted_verified'; receipt_sha256: string | null};
type Status = {entries: Entry[]; reserved: boolean};
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);

function validated(value: unknown): Status | null {
  if (!object(value) || value.schema_version !== '1.0' || value.kind !== 'owned_parameter_project_manual_bootstrap_status'
    || !Array.isArray(value.entries) || value.entries.length > 64 || typeof value.reserved !== 'boolean'
    || ['released_skill_verified', 'native_model_verified', 'site_outcome_verified', 'activation_authorized',
      'training_ready', 'gpu_release_verified', 'replay_authorized'].some(key => value[key] !== false)) return null;
  const identifiers = new Set<string>();
  for (const entry of value.entries) {
    if (!object(entry) || !hash(entry.intent_sha256) || !hash(entry.manifest_sha256)
      || typeof entry.job_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(entry.job_id)
      || identifiers.has(entry.intent_sha256)
      || (entry.status === 'unresolved' ? entry.receipt_sha256 !== null
        : entry.status !== 'accepted_verified' || !hash(entry.receipt_sha256))) return null;
    identifiers.add(entry.intent_sha256);
  }
  if (value.reserved !== value.entries.some(entry => entry.status === 'unresolved')) return null;
  return value as unknown as Status;
}

export function OwnedParameterProject({status}: {status: unknown}) {
  const language = useLanguage();
  const valid = validated(status);
  const copy = language === 'tr' ? {
    title: 'Çok alanlı sentetik proje', invalid: 'Proje kanıtı kullanılamıyor.',
    ready: 'Kaynak hazır; görev henüz yürütülmedi.', unresolved: 'Belirsiz sonuç: tekrar yürütme kapalı.',
    accepted: 'Sonlu görev ve bütün alanların readback sonucu doğrulandı.',
    scope: 'Manuel bootstrap; released skill, gerçek model, site hesabı, eğitim veya GPU bırakımı kanıtı değildir.',
  } : {
    title: 'Multi-field synthetic project', invalid: 'Project evidence is unavailable.',
    ready: 'Source prepared; no task execution yet.', unresolved: 'Uncertain outcome: replay is blocked.',
    accepted: 'Finite task and whole-record readback verified.',
    scope: 'Manual bootstrap; not proof of a released skill, native model, site account, training or GPU release.',
  };
  return <section className="panel" data-testid="owned-parameter-project">
    <h3>{copy.title}</h3>
    {!valid ? <p role="alert">{copy.invalid}</p> : <>
      {valid.entries.length === 0 && <p>{copy.ready}</p>}
      {valid.entries.map(entry => <div key={entry.intent_sha256}>
        <p>{entry.status === 'accepted_verified' ? copy.accepted : copy.unresolved}</p>
        <code>{entry.manifest_sha256}</code>
        {entry.receipt_sha256 && <p><code>{entry.receipt_sha256}</code></p>}
      </div>)}
    </>}
    <p>{copy.scope}</p>
  </section>;
}

export function ownedParameterProjectCanStart(status: unknown): boolean {
  const valid = validated(status);
  return valid !== null && !valid.reserved && valid.entries.length === 0;
}

export function ownedParameterProjectAcceptedIntent(status: unknown): string | null {
  const valid = validated(status);
  return valid !== null && valid.entries.length === 1
    && valid.entries[0].status === 'accepted_verified' ? valid.entries[0].intent_sha256 : null;
}
