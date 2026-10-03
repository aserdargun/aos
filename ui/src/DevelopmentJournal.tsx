import {useId} from 'react';
import {useLanguage} from './i18n';
import type {Language} from './i18n';

type LocalizedCopy = {en: string; tr: string};
type DevelopmentUpdate = {id: string; state: 'verified_cpu' | 'in_progress' | 'next';
  recorded_at: string; title: LocalizedCopy; detail: LocalizedCopy; evidence: string[]};
export type DevelopmentCheckpoint = {recorded_at: string; scope: 'manual_development_checkpoint';
  focus: LocalizedCopy; blocker: LocalizedCopy; next_action: LocalizedCopy; limits: LocalizedCopy;
  updates: DevelopmentUpdate[]};

const objectValue = (value: unknown): value is Record<string, unknown> => value !== null
  && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
const validTime = (value: unknown): value is string => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value) && Number.isFinite(Date.parse(value))
  && new Date(value).toISOString().replace('.000Z', 'Z') === value;
const validCopy = (value: unknown, maximum = 1200): value is LocalizedCopy => objectValue(value)
  && exactKeys(value, ['en', 'tr']) && ['en', 'tr'].every(language => typeof value[language] === 'string'
    && value[language].trim().length > 0 && value[language].length <= maximum);
const evidencePattern = /^(?:docs|data|src|schemas|tests|database)\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*$/;
const validEvidence = (value: unknown): value is string[] => Array.isArray(value) && value.length >= 1
  && value.length <= 8 && new Set(value).size === value.length
  && value.every(path => typeof path === 'string' && path.length <= 240 && path.trim() === path && evidencePattern.test(path));

export function validDevelopmentCheckpoint(value: unknown): value is DevelopmentCheckpoint {
  if (!objectValue(value) || !exactKeys(value, ['recorded_at', 'scope', 'focus', 'blocker', 'next_action', 'limits', 'updates'])
    || value.scope !== 'manual_development_checkpoint' || !validTime(value.recorded_at)
    || !validCopy(value.focus) || !validCopy(value.blocker) || !validCopy(value.next_action) || !validCopy(value.limits)
    || !Array.isArray(value.updates) || value.updates.length < 1 || value.updates.length > 12) return false;
  const identifiers = new Set<string>();
  return value.updates.every(update => {
    if (!objectValue(update) || !exactKeys(update, ['id', 'state', 'recorded_at', 'title', 'detail', 'evidence'])
      || typeof update.id !== 'string' || update.id.length > 96 || update.id.trim() !== update.id || !/^[a-z][a-z0-9_]*$/.test(update.id)
      || identifiers.has(update.id) || typeof update.state !== 'string' || !['verified_cpu', 'in_progress', 'next'].includes(update.state)
      || !validTime(update.recorded_at) || !validCopy(update.title, 120) || !validCopy(update.detail)
      || !validEvidence(update.evidence)) return false;
    identifiers.add(update.id);
    return true;
  });
}

export function DevelopmentJournal({checkpoint, language}: {checkpoint: DevelopmentCheckpoint; language?: Language}) {
  const selected = useLanguage();
  const current = language ?? selected;
  const heading = useId();
  const labels = current === 'tr'
    ? {title: 'Geliştirme güncellemeleri', source: 'Tarihli manuel kayıt · canlı ajan durumu değil',
      verified_cpu: 'CPU doğrulandı', in_progress: 'Geliştirme sürüyor', next: 'Sıradaki',
      evidence: 'Kanıt', unavailable: 'Manuel geliştirme kaydı kullanılamıyor.'}
    : {title: 'Development updates', source: 'Dated manual record · not live agent status',
      verified_cpu: 'CPU verified', in_progress: 'In development', next: 'Next',
      evidence: 'Evidence', unavailable: 'Manual development record is unavailable.'};
  const date = (value: string) => new Intl.DateTimeFormat(current === 'tr' ? 'tr-TR' : 'en-GB',
    {year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
      timeZone: 'UTC', hourCycle: 'h23'}).format(new Date(value)) + ' UTC';
  if (!validDevelopmentCheckpoint(checkpoint)) return <section className="development-journal"
    data-testid="development-journal" aria-labelledby={heading}>
    <h2 id={heading}>{labels.title}</h2><p role="status">{labels.unavailable}</p>
  </section>;
  return <section className="development-journal" data-testid="development-journal" aria-labelledby={heading}>
    <header><h2 id={heading}>{labels.title}</h2><p className="development-journal-source">{labels.source}</p></header>
    <ol className="development-journal-list">
      {[...checkpoint.updates].sort((first, second) => second.recorded_at.localeCompare(first.recorded_at)).map(update => <li key={update.id} className="development-journal-row"
        data-state={update.state} data-testid={'development-update-' + update.id}>
        <span className="development-journal-dot" aria-hidden="true"/>
        <div className="development-journal-copy">
          <div className="development-journal-heading">
            <span className="development-journal-state">{labels[update.state]}</span>
            <time dateTime={update.recorded_at}>{date(update.recorded_at)}</time></div>
          <h3>{update.title[current]}</h3>
          <p>{update.detail[current]}</p>
          <details className="development-journal-evidence"><summary>{labels.evidence}</summary>
            <ul>{update.evidence.map(path => <li key={path}><code>{path}</code></li>)}</ul>
          </details>
        </div>
      </li>)}
    </ol>
    <p className="development-journal-limit">{checkpoint.limits[current]}</p>
  </section>;
}
