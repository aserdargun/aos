import {t} from './i18n';
import {useEffect, useRef, useState, type FormEvent} from 'react';
import {api, type PlanPreview, type Snapshot, type Tasks} from './api';

interface CompoundPreview {
  status: 'recognized' | 'unsupported' | 'negated'; reason: string;
  tasks: PlanPreview[]; input_sha256: string; composition_sha256: string;
  execution_authorized: false; automatic_replay_allowed: false;
}

export function CompoundPlan({onError, snapshot, tasks, busy, action}: {
  onError: (failure: unknown) => void; snapshot: Snapshot | null; tasks: Tasks | null; busy: boolean;
  action: (operation: () => Promise<void>) => Promise<void>;
}) {
  const [goal, setGoal] = useState('');
  const [report, setReport] = useState<CompoundPreview | null>(null);
  const [pending, setPending] = useState(false);
  const [started, setStarted] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function preview(event: FormEvent) {
    event.preventDefault();
    if (!goal.trim()) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setReport(null); setPending(true); setStarted(false);
    try {
      const next = await api<CompoundPreview>('/api/tasks/compound-plan', {goal}, abort.signal);
      if (!abort.signal.aborted) setReport(next);
    } catch (failure) {
      if (!abort.signal.aborted) onError(failure);
    } finally {
      if (!abort.signal.aborted) setPending(false);
    }
  }
  return <section className="panel" data-testid="compound-plan-panel">
    <h2>{t("Birleşik görev önizlemesi")}</h2>
    <p>{t("En fazla üç farklı sabit görevi sıralayın. Genel planlayıcı değildir. Önizleme görev seçmez, sıra başlatmaz veya onay vermez; başlatma ayrı düğmedir.")}</p>
    <div className="goal-preview">
      <p id="compound-help" className="caption">{t("Örnek: önce hello görevini hazırla; sonra yerel form görevini hazırla; sonra görsel save görevini hazırla. Secret girmeyin. Her görev ve eylem mevcut bağımsız onay sınırlarını korur.")}</p>
      <form onSubmit={event => void preview(event)}>
        <label htmlFor="compound-goal">{t("Birleşik Türkçe görev")}</label>
        <input id="compound-goal" aria-describedby="compound-help" value={goal} maxLength={3000} autoComplete="off" disabled={busy} onChange={event => {
          request.current?.abort(); setGoal(event.target.value); setReport(null); setPending(false); setStarted(false);
        }}/>
        <button type="submit" disabled={busy || pending || !goal.trim()}>{pending ? t('Birleşim okunuyor…') : t('Birleşimi önizle')}</button>
      </form>
    </div>
    {report ? <div data-testid="compound-plan-result">
      {report.status === 'recognized' ? <>
        <p role="status">{report.tasks.length} {t("sabit görev tanındı; hiçbir görev seçilmedi veya başlatılmadı.")}</p>
        <ol>{report.tasks.map(task => <li className="registry" key={task.plan_sha256} data-testid="compound-plan-task">
          <strong>{task.plan?.task_kind}</strong><p>{t("Kapsam:")} <code>{task.plan?.scope}</code></p>
          <p>{t("Bağımsız doğrulama:")} <code>{task.plan?.verification_method}</code></p>
          <p className="caption">{task.plan?.steps.filter(step => step.approval === 'fresh_action').length} {t("ayrı eylem onayı gerekir. Beklenen sonucu görev yürütücüsü ayrıca doğrular.")}</p>
        </li>)}</ol>
        {report.tasks.length > 1 ? <>
          <p className="caption">{t("Başlatma yalnız bu metin ve katalog hash'lerine bağlı sırayı kabul eder; eylemleri onaylamaz. Görevler sekmesinde her eylemi ayrıca inceleyin. Duraklatma kalan sırayı iptal eder; otomatik tekrar veya çökme sonrası devam yoktur.")}</p>
          {!report.tasks.every(task => task.plan && tasks?.kinds?.includes(task.plan.task_kind)) ? <p className="caption">{t("Bu birleşim için gereken görev motorları açık değil.")}</p> : null}
          <button disabled={busy || pending || !tasks?.available || tasks.reserved
            || !report.tasks.every(task => task.plan && tasks.kinds?.includes(task.plan.task_kind))
            || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onClick={() => {
              if (!snapshot) return;
              const admission = {goal, input_sha256: report.input_sha256, composition_sha256: report.composition_sha256,
                lease_id: snapshot.control.lease_id, generation: snapshot.control.generation};
              setReport(null);
              void action(async () => {
                await api('/api/tasks/compound-start', admission);
                setStarted(true);
              });
            }}>{t("Bu birleşik sırayı başlat")}</button>
        </> : <p className="caption">{t("Tek görev için Görevler sekmesindeki mevcut başlatma düğmesini kullanın.")}</p>}
      </> : <p role="status">{report.status === 'negated' ? t('Olumsuzluk veya kontrol isteği: birleşik plan üretilmedi.') : t('Bu birleşim desteklenmiyor. Bir ila üç farklı katalog görevini örnekteki sırayla belirtin.')}</p>}
      <p className="caption">{t("Birleşim SHA-256:")} <code>{report.composition_sha256}</code> {t("· Yürütme yetkisi değildir.")}</p>
    </div> : <p className="caption">{t("Yalnız düğmeye basıldığında önizlenir; yazmak veya paneli açmak işlem başlatmaz.")}</p>}
    {started ? <p role="status" data-testid="compound-started">{t("Sıra kabul edildi; hiçbir eylem otomatik onaylanmadı. Onayları ve ilerlemeyi Görevler sekmesinde izleyin.")}</p> : null}
  </section>;
}
