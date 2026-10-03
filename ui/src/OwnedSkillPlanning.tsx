import {useEffect, useRef, useState} from 'react';
import {api, isOwnedFormRecipeSteps, type OwnedFormInvocation, type OwnedFormRecipeStep, type Snapshot, type Tasks} from './api';
import {useLanguage} from './i18n';
import {candidateHash} from './ownedSkillCandidateApi';
import {OwnedEpisodeLearning} from './OwnedEpisodeLearning';
import {OwnedSkillKnowledge} from './OwnedSkillKnowledge';

export type PlanningStatus = {available: boolean; status: 'idle' | 'pending' | 'ready' | 'bound' | 'consumed' | 'needs_human' | 'cancelled' | 'failed' | 'unavailable';
  planning_id?: string; bundle_sha256?: string | null; episode_id?: string; job_id?: string; execution_review_pending?: boolean; execution_authorized: false; real_model?: boolean; model_called?: boolean; knowledge_available?: boolean};
type Preview = {schema_version: '1.4'; preview_sha256: string; planning_bundle_sha256: string;
  reuse_admission_sha256: string; source_run_ref: string; case_key: string; steps: OwnedFormRecipeStep[]};
type Props = {source: OwnedFormInvocation; snapshot: Snapshot; tasks: Tasks; disabled: boolean};
const base = '/api/tasks/owned-skill-plan';

export function OwnedSkillPlanning({source, snapshot, tasks, disabled}: Props) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const [goal, setGoal] = useState('Save message "gamma"');
  const [planningId, setPlanningId] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [execution, setExecution] = useState<{checksum: string; plan: string; job: string} | null>(null);
  const [verified, setVerified] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [collectLearning, setCollectLearning] = useState(false);
  const serial = useRef(0);
  const scope = `${snapshot.runtime.runtime_id}:${snapshot.control.lease_id}:${snapshot.control.generation}:${source.reuse_admission_sha256}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1; setPlanningId(null); setPreview(null); setConfirmed(false);
    setExecution(null); setVerified(false); setLoading(false); setError(false); setCollectLearning(false);
  }, [scope]);
  useEffect(() => () => { serial.current += 1; }, []);
  const status = tasks.owned_skill_planning;
  if (!status?.available || !source.reuse_admission_sha256) return null;
  const pending = status.status === 'pending';
  const bound = status.status === 'bound';
  const locked = disabled || loading || tasks.busy || Boolean(tasks.reserved)
    || snapshot.control.owner !== 'AGENT' || snapshot.control.status !== 'running';
  const ownsProposal = planningId !== null && status.planning_id === planningId && candidateHash(status.bundle_sha256);
  const job = execution ? tasks.jobs.find(item => item.job_id === execution.job) : null;

  async function fresh() {
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id
        || current.control.lease_id !== snapshot.control.lease_id || current.control.generation !== snapshot.control.generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') throw new Error('control_changed');
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }
  async function operate(operation: 'begin' | 'bind' | 'start' | 'discard' | 'audit') {
    const request = ++serial.current;
    setLoading(true); setError(false);
    try {
      const control = await fresh();
      if (request !== serial.current) return;
      if (operation === 'begin') {
        setPreview(null); setConfirmed(false); setExecution(null); setVerified(false); setPlanningId(null);
        const requestedCollection = collectLearning;
        setCollectLearning(false);
        const result = await api<PlanningStatus>(base, {schema_version: '1.1', goal,
          collect_learning: requestedCollection, ...control});
        if (!result.available || result.execution_authorized !== false
            || !result.planning_id?.match(/^planning-[a-f0-9]{32}$/)
            || (requestedCollection && result.status === 'pending'
              && !result.episode_id?.match(/^episode-[a-f0-9]{32}$/))
            || !['pending', 'needs_human'].includes(result.status)) throw new Error('invalid_plan');
        if (request === serial.current) setPlanningId(result.planning_id);
      } else if (operation === 'audit') {
        if (!execution) return;
        setVerified(false);
        const result = await api<Record<string, unknown>>(base + '/audit', {
          schema_version: '1.4', candidate_execution_sha256: execution.checksum});
        if (result.schema_version !== '1.4' || result.available !== true || result.status !== 'verified'
            || result.planning_admission_verified !== true || result.reuse_admission_verified !== true
            || result.candidate_execution_sha256 !== execution.checksum
            || result.planning_bundle_sha256 !== execution.plan) throw new Error('audit_unavailable');
        if (request === serial.current) setVerified(true);
      } else {
        if (!candidateHash(status?.bundle_sha256)) return;
        const selection = {schema_version: '1.4', planning_bundle_sha256: status.bundle_sha256,
          confirm_plan_sha256: status.bundle_sha256, ...control};
        if (operation === 'bind') {
          const result = await api<Preview>(base + '/bind', selection);
          if (result.schema_version !== '1.4' || result.planning_bundle_sha256 !== selection.planning_bundle_sha256
              || result.reuse_admission_sha256 !== source.reuse_admission_sha256
              || result.source_run_ref !== source.run_ref || !candidateHash(result.preview_sha256)
              || !isOwnedFormRecipeSteps(result.steps)) throw new Error('invalid_preview');
          if (request === serial.current) {setPreview(result); setConfirmed(false);}
        } else if (operation === 'start') {
          if (!preview || !confirmed || preview.planning_bundle_sha256 !== selection.planning_bundle_sha256) return;
          setConfirmed(false);
          const result = await api<Record<string, unknown>>(base + '/start', {...selection,
            preview_sha256: preview.preview_sha256, confirm_sha256: preview.preview_sha256});
          if (result.schema_version !== '1.4' || result.accepted !== true
              || result.planning_bundle_sha256 !== preview.planning_bundle_sha256
              || !candidateHash(result.candidate_execution_sha256) || typeof result.job_id !== 'string') throw new Error('start_unavailable');
          if (request === serial.current) setExecution({checksum: result.candidate_execution_sha256,
            plan: preview.planning_bundle_sha256, job: result.job_id});
        } else {
          await api(base + '/discard', selection);
          if (request === serial.current) {setPreview(null); setPlanningId(null); setConfirmed(false);}
        }
      }
    } catch {
      if (request === serial.current) {setError(true); setConfirmed(false); setVerified(false);}
    } finally {
      if (request === serial.current) setLoading(false);
    }
  }
  return <section className="registry" data-testid="skill-planning">
    <h3>{text('Plan with Bonsai, execute with Decider', 'Bonsai ile planla, Decider ile yürüt')}</h3>
    <p className="caption">{text('One selected synthetic skill. Proposals do not execute; starting still requires six manual action approvals.',
      'Tek seçilmiş sentetik skill. Öneri eylem yapmaz; başlangıçtan sonra altı ayrı manuel eylem onayı gerekir.')}</p>
    <label>{text('Goal (exact supported form)', 'Hedef (desteklenen tam biçim)')}
      <input data-testid="planning-goal" value={goal} maxLength={256} disabled={locked || bound}
        onChange={event => {setGoal(event.target.value); setPlanningId(null); setPreview(null); setConfirmed(false);}}/>
    </label>
    <p className="caption">{'Save message "gamma" · Mesaj alanına "gamma" kaydet'}</p>
    {status.knowledge_available === true ? <OwnedSkillKnowledge goal={goal} source={source} snapshot={snapshot} tasks={tasks}
      disabled={disabled || loading} collectLearning={collectLearning} onStarted={planning => {
        serial.current += 1; setPlanningId(planning); setPreview(null); setConfirmed(false); setExecution(null); setVerified(false); setError(false);
      }}/> : null}
    <label className="caption" style={{display: 'block', margin: '12px 0'}}>
      <input data-testid="episode-opt-in" type="checkbox" checked={collectLearning} disabled={locked || bound}
        onChange={event => setCollectLearning(event.target.checked)}/>{' '}
      {text('Optionally collect this one owned synthetic episode: private local content for explicit review only; no training or network export.',
        'İsteğe bağlı olarak bu tek sahipli sentetik bölümü toplayın: yalnız açık inceleme için yerel özel içerik; eğitim veya ağ dışa aktarımı yoktur.')}
    </label>
    <button data-testid="planning-begin" disabled={locked || bound || !goal.trim()} onClick={() => void operate('begin')}>
      {text('Generate proposal', 'Öneri üret')}</button>
    <p role="status" data-testid="planning-status">{pending ? text('Bonsai is planning… Use Pause or Stop to cancel.', 'Bonsai planlıyor… İptal için Duraklat veya Durdur kullanın.')
      : status.status === 'needs_human' ? text('Human input required. No task started.', 'İnsan girdisi gerekiyor. Görev başlamadı.')
      : status.status === 'failed' ? text('Planning failed. No automatic fallback.', 'Planlama başarısız. Otomatik alternatif yürütme yok.')
      : text('Planning state: ', 'Planlama durumu: ') + status.status}</p>
    {candidateHash(status.bundle_sha256) ? <p className="caption"><code style={{overflowWrap: 'anywhere'}}>{status.bundle_sha256}</code></p> : null}
    <button data-testid="planning-bind" disabled={locked || !ownsProposal || status.status !== 'ready'} onClick={() => void operate('bind')}>
      {text('Confirm proposal and preview execution', 'Öneriyi onayla ve yürütmeyi önizle')}</button>
    <button data-testid="planning-discard" disabled={locked || !['ready', 'bound'].includes(status.status)} onClick={() => void operate('discard')}>
      {text('Discard proposal', 'Öneriyi bırak')}</button>
    {preview && ownsProposal && bound ? <div data-testid="planning-preview">
      <ol>{preview.steps.map(step => <li key={step.step_key}>{step.operation}</li>)}</ol>
      <p className="caption"><code style={{overflowWrap: 'anywhere'}}>{preview.preview_sha256}</code></p>
      <label><input type="checkbox" data-testid="planning-confirm" checked={confirmed} disabled={locked}
        onChange={event => setConfirmed(event.target.checked)}/>{text('Start exactly this plan-bound execution.', 'Tam olarak bu plana bağlı yürütmeyi başlat.')}</label>
      <button data-testid="planning-start" disabled={locked || !confirmed} onClick={() => void operate('start')}>
        {text('Start with manual approvals', 'Manuel onaylarla başlat')}</button>
    </div> : null}
    {execution ? <div><p>{text('Execution: ', 'Yürütme: ')}{job?.status ?? 'pending'}</p>
      <button data-testid="planning-audit" disabled={locked || job?.status !== 'succeeded'} onClick={() => void operate('audit')}>
        {text('Audit plan-bound result', 'Plana bağlı sonucu denetle')}</button></div> : null}
    {verified ? <p data-testid="planning-verified">{text('Plan-bound execution verified. Not training-ready.', 'Plana bağlı yürütme doğrulandı. Eğitime hazır değildir.')}</p> : null}
    {tasks.owned_episode_learning ? <OwnedEpisodeLearning status={tasks.owned_episode_learning}
      snapshot={snapshot} disabled={disabled || Boolean(tasks.busy || tasks.reserved)}/> : null}
    {error ? <p role="alert">{text('Plan, control or source changed. Review again; nothing is automatically retried.', 'Plan, kontrol veya kaynak değişti. Yeniden inceleyin; otomatik tekrar yapılmaz.')}</p> : null}
  </section>;
}
