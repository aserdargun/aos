import {useEffect, useRef, useState} from 'react';
import {api, type RemoteSkillSource as SkillSource, type SiteSkillDraftReport, type Tasks} from './api';
import {t} from './i18n';

export function RemoteSkillSource({tasks}: {tasks: Tasks}) {
  const runs = tasks.jobs.filter(job => job.kind === 'browser_remote_routes' &&
    job.status === 'succeeded' && job.run_id !== null && job.real_model === 1);
  const runKey = runs.map(job => job.run_id).join(':');
  const [selectedRun, setSelectedRun] = useState('');
  const [selectedRoute, setSelectedRoute] = useState(0);
  const [skillSha256, setSkillSha256] = useState('');
  const [report, setReport] = useState<SkillSource | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [drafts, setDrafts] = useState<SiteSkillDraftReport[] | null>(null);
  const [draftsUnavailable, setDraftsUnavailable] = useState(false);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [supervisorDrafts, setSupervisorDrafts] = useState<SiteSkillDraftReport[] | null>(null);
  const [supervisorDraftsUnavailable, setSupervisorDraftsUnavailable] = useState(false);
  const [supervisorDraftsLoading, setSupervisorDraftsLoading] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const draftsPending = useRef<AbortController | null>(null);
  const supervisorDraftsPending = useRef<AbortController | null>(null);
  const runId = runs.some(job => job.run_id === selectedRun) ? selectedRun : runs[0]?.run_id ?? '';
  const routeCount = tasks.remote_routes?.route_count ?? 0;
  const routeIndex = selectedRoute < routeCount ? selectedRoute : 0;

  useEffect(() => {
    pending.current?.abort();
    setReport(null);
    setUnavailable(false);
    setLoading(false);
  }, [runId, routeIndex, skillSha256, runKey, tasks.remote_routes?.plan_sha256]);
  useEffect(() => {
    draftsPending.current?.abort();
    supervisorDraftsPending.current?.abort();
    setDrafts(null);
    setDraftsUnavailable(false);
    setDraftsLoading(false);
    setSupervisorDrafts(null);
    setSupervisorDraftsUnavailable(false);
    setSupervisorDraftsLoading(false);
  }, [tasks.remote_entry?.profile_sha256, tasks.remote_routes?.plan_sha256]);
  useEffect(() => () => {
    pending.current?.abort();
    draftsPending.current?.abort();
    supervisorDraftsPending.current?.abort();
  }, []);

  async function loadDrafts() {
    draftsPending.current?.abort();
    const controller = new AbortController();
    draftsPending.current = controller;
    setDraftsLoading(true);
    setDraftsUnavailable(false);
    try {
      const result = await api<SiteSkillDraftReport[]>('/api/tasks/skill-drafts', undefined, controller.signal);
      if (!controller.signal.aborted) setDrafts(result);
    } catch {
      if (!controller.signal.aborted) {
        setDrafts(null);
        setDraftsUnavailable(true);
      }
    } finally {
      if (!controller.signal.aborted) setDraftsLoading(false);
    }
  }

  async function loadSupervisorDrafts() {
    supervisorDraftsPending.current?.abort();
    const controller = new AbortController();
    supervisorDraftsPending.current = controller;
    setSupervisorDraftsLoading(true);
    setSupervisorDraftsUnavailable(false);
    try {
      const result = await api<SiteSkillDraftReport[]>(
        '/api/tasks/skill-drafts?model_role=system2', undefined, controller.signal);
      if (!controller.signal.aborted) setSupervisorDrafts(result);
    } catch {
      if (!controller.signal.aborted) {
        setSupervisorDrafts(null);
        setSupervisorDraftsUnavailable(true);
      }
    } finally {
      if (!controller.signal.aborted) setSupervisorDraftsLoading(false);
    }
  }

  async function inspect() {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setReport(null);
    setUnavailable(false);
    setLoading(true);
    try {
      const result = await api<SkillSource>('/api/tasks/skill-source-inspect',
        {run_id: runId, route_index: routeIndex, skill_sha256: skillSha256}, controller.signal);
      if (!controller.signal.aborted) setReport(result);
    } catch {
      if (!controller.signal.aborted) setUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  return <section className="registry" data-testid="remote-skill-source">
    <h3>{t('S1 skill kaynak incelemesi')}</h3>
    <p className="caption">{t('Kayıtlı özel skill taslağı SHA-256 değerini ve kaynak gerçek S1 rota koşusunu seçin. Listelenen taslak koşuyla otomatik eşleşmez; inceleme salt okunurdur.')}</p>
    {runs.length === 0 ? <p>{t('İnceleme için gerçek S1 ile tamamlanmış rota görevi gerekir.')}</p> : <>
      <label>{t('Kaynak koşu')} <select aria-label={t('Kaynak koşu')} value={runId}
        onChange={event => setSelectedRun(event.target.value)}>
        {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
      </select></label>
      <label>{t('Kaynak rota')} <select aria-label={t('Kaynak rota')} value={routeIndex}
        onChange={event => setSelectedRoute(Number(event.target.value))}>
        {Array.from({length: routeCount}, (_, index) => <option key={index} value={index}>{index + 1}</option>)}
      </select></label>
      <label>{t('Skill SHA-256')} <input aria-label={t('Skill SHA-256')}
        value={skillSha256} onChange={event => setSkillSha256(event.target.value)} maxLength={64}/></label>
      <button disabled={draftsLoading} onClick={() => void loadDrafts()}>{t('Özel S1 taslaklarını listele')}</button>
      {drafts ? drafts.length ? <label>{t('Kayıtlı S1 taslağı')} <select aria-label={t('Kayıtlı S1 taslağı')}
        value={drafts.some(draft => draft.skill_sha256 === skillSha256) ? skillSha256 : ''}
        onChange={event => setSkillSha256(event.target.value)}>
        <option value="">{t('Taslak seçin')}</option>
        {drafts.map(draft => <option key={draft.skill_sha256} value={draft.skill_sha256}>
          {draft.skill_key} · v{draft.revision} · {draft.skill_sha256}
        </option>)}
      </select></label> : <p>{t('Bu profile ait kayıtlı S1 taslağı yok.')}</p> : null}
      {draftsUnavailable ? <p role="status">{t('Özel S1 taslakları kullanılamıyor.')}</p> : null}
      <button disabled={loading || !runId || !/^[a-f0-9]{64}$/.test(skillSha256)}
        onClick={() => void inspect()}>{t('Skill kaynağını incele')}</button>
    </>}
    {loading ? <p role="status">{t('Skill kaynağı denetleniyor…')}</p> : null}
    {unavailable ? <p role="status">{t('Skill kaynağı kullanılamıyor; kapsam veya özel kaynak değişmiş olabilir.')}</p> : null}
    {report ? <div role="status" data-testid="remote-skill-source-result">
      <p>{t('S1 kaynak eşleşti:')} <code>{report.skill_sha256}</code> · {t('Rota:')} {report.route_index + 1} · {t('S1 kaynak olayları:')} {report.source_event_ids.length}</p>
      <p className="caption">{t('Yalnız HTTPS taşıma/readback doğrulandı. Uygulama sonucu, hesap, parametreli skill başarısı, S2, aktivasyon ve eğitim doğrulanmadı.')}</p>
    </div> : null}
    <div data-testid="remote-s2-skill-drafts">
      <h4>{t('S2 skill taslakları')}</h4>
      <p className="caption">{t('Yalnız bu profile kayıtlı özel taslak metadata’sı. Rota görevi Bonsai çağırmaz; S2 kaynak veya sonuç doğrulaması yapılmaz.')}</p>
      <button disabled={supervisorDraftsLoading} onClick={() => void loadSupervisorDrafts()}>{t('Özel S2 taslaklarını listele')}</button>
      {supervisorDrafts ? supervisorDrafts.length ? <ul>{supervisorDrafts.map(draft =>
        <li key={draft.skill_sha256}><code>{draft.skill_key}</code> · v{draft.revision} · <code>{draft.skill_sha256}</code> · {t('İncelenmemiş taslak')}</li>)}</ul>
        : <p>{t('Bu profile ait kayıtlı S2 taslağı yok.')}</p> : null}
      {supervisorDraftsUnavailable ? <p role="status">{t('Özel S2 taslakları kullanılamıyor.')}</p> : null}
    </div>
  </section>;
}
