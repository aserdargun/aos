import {t} from './i18n';
import {useEffect, useRef, useState, type FormEvent} from 'react';
import {api, type PlanPreview} from './api';

const phases = {observe: 'Gözlem', decide: 'Karar', act: 'Eylem', verify: 'Doğrulama'};
const approvals = {fresh_action: 'Ayrı, taze eylem onayı gerekir', covered_readback: 'Onaylı eylemin bağımsız readback kapsamı', not_applicable: 'Yürütme onayı değildir'};

export function Plan({onError}: {onError: (failure: unknown) => void}) {
  const [goal, setGoal] = useState('');
  const [report, setReport] = useState<PlanPreview | null>(null);
  const [pending, setPending] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function preview(event: FormEvent) {
    event.preventDefault();
    if (!goal.trim()) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setReport(null); setPending(true);
    try {
      const next = await api<PlanPreview>('/api/tasks/plan', {goal}, abort.signal);
      if (!abort.signal.aborted) setReport(next);
    } catch (failure) {
      if (!abort.signal.aborted) onError(failure);
    } finally {
      if (!abort.signal.aborted) setPending(false);
    }
  }
  return <section className="panel" data-testid="plan-panel">
    <h2>{t("Sabit görev planı")}</h2>
    <p>{t("Bu katalog önizlemesidir; canlı ilerleme, model planı veya başarı kanıtı değildir. Görev başlatmaz, onay vermez.")}</p>
    <div className="goal-preview">
      <p id="plan-help" className="caption">{t("“hello görevini hazırla”, “yerel form görevini hazırla” veya “görsel save görevini hazırla”. Secret girmeyin. Katalog, görev motoru kapalıyken de görüntülenebilir; kullanılabilirlik ayrıca denetlenir.")}</p>
      <form onSubmit={event => void preview(event)}>
        <label htmlFor="plan-goal">{t("Plan için Türkçe görev")}</label>
        <input id="plan-goal" aria-describedby="plan-help" value={goal} maxLength={1000} autoComplete="off" onChange={event => {
          request.current?.abort(); setGoal(event.target.value); setReport(null); setPending(false);
        }}/>
        <button type="submit" disabled={pending || !goal.trim()}>{pending ? t('Plan okunuyor…') : t('Planı önizle')}</button>
      </form>
    </div>
    {report ? <div data-testid="plan-result">
      {report.plan ? <>
        <p role="status">{report.plan.task_kind} {t("· Yalnız önizleme; hiçbir adım yürütülmedi.")}</p>
        <p>{t("Kapsam:")} <code>{report.plan.scope}</code></p>
        <p>{t("Runtime:")} {report.plan.runtime === 'isolated_workspace' ? t('İzole workspace') : t('Ayrı ağsız headless browser; noVNC masaüstüsü değil')}</p>
        <pre>{report.plan.normalized_goal}</pre>
        <h3>{t("Önkoşullar")}</h3>
        <ul>{report.plan.preconditions.map(condition => <li key={condition}>{t(condition)}</li>)}</ul>
        <h3>{t("Planlanan adımlar")}</h3>
        <ol>{report.plan.steps.map(step => <li className="registry" key={step.order} data-testid="plan-step">
          <strong>{t(phases[step.phase])}</strong><p>{t(step.description)}</p><span className="caption">{t(approvals[step.approval])}</span>
        </li>)}</ol>
        <h3>{t("Bağımsız başarı ölçütü")}</h3>
        <p><code>{report.plan.verification_method}</code></p><pre>{report.plan.expected_json}</pre>
        <p className="caption">{t("Beklenen değerdir, ölçülmüş sonuç değildir. Başlatma yalnız Görevler panelindeki bağımsız seçimle yapılır; plan hash'i onay veya yürütme capability'si değildir.")}</p>
        <p className="caption">{report.plan.catalog_version} {t("· Plan SHA-256:")} <code>{report.plan_sha256}</code></p>
      </> : <p role="status">{report.intent.status === 'negated' ? t('Olumsuzluk veya kontrol isteği: plan üretilmedi. Kalıcı kontrol düğmelerini kullanın.') : t('Bu metin sabit katalogda desteklenmiyor; plan üretilmedi.')}</p>}
    </div> : <p className="caption">{t("Plan otomatik okunmaz. Kalıcı kontrol çubuğu kullanılabilir kalır.")}</p>}
  </section>;
}
