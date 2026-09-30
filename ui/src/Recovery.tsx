import {t, locale} from './i18n';
import {useEffect, useRef, useState} from 'react';
import {api, type RecoveryInventory} from './api';

const reasons: Record<string, string> = {
  job_unsettled: 'Görev terminal durumda değil', run_unsettled: 'Çalışma terminal durumda değil',
  action_effect_unresolved: 'Eylem etkisi bağımsız inceleme gerektiriyor', approval_not_terminal: 'Eski onay yeniden kullanılamaz',
  job_run_status_mismatch: 'Görev ve çalışma durumları farklı', job_run_missing: 'Bağlı çalışma kaydı yok'
};

export function Recovery({onError}: {onError: (failure: unknown) => void}) {
  const [report, setReport] = useState<RecoveryInventory | null>(null);
  const [pending, setPending] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function inspect() {
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setReport(null); setPending(true);
    try {
      const next = await api<RecoveryInventory>('/api/recovery', undefined, abort.signal);
      if (!abort.signal.aborted) setReport(next);
    } catch (failure) {
      if (!abort.signal.aborted) onError(failure);
    } finally {
      if (!abort.signal.aborted) setPending(false);
    }
  }
  return <section className="panel" data-testid="recovery-panel">
    <h2>{t("Salt okunur kurtarma envanteri")}</h2>
    <p>{t("Kayıtları inceleyin; bu panel görev başlatmaz, eski onayı canlandırmaz veya belirsiz eylemi tekrarlamaz.")}</p>
    <p className="caption">{t("Anlık DB kopyasıdır, canlı süreç veya çökme tespiti değildir. Çalışan bir görev de terminal olmayan kayıtlar gösterebilir. Yeniden başlatmadan önce izole workspace içindeki gerçek etkiyi bağımsız doğrulayın; taze runtime, lease ve eylem onayı gerekir.")}</p>
    <button disabled={pending} onClick={() => void inspect()}>{pending ? t('Envanter okunuyor…') : t('Envanteri oku')}</button>
    {report ? <div data-testid="recovery-report">
      <p role="status">{report.requires_inspection ? t('İncelenecek kayıtlar var; otomatik devam kapalı.') : t('Bu kopyada terminal olmayan iş veya belirsiz etki bulunmadı; yürütme izni verilmedi.')}</p>
      <p className="caption">{t("Kopya zamanı:")} {new Date(report.captured_at).toLocaleString(locale())} {t("· Canlı durum doğrulanmadı.")}</p>
      <div className="metrics">
        <article><span>{t("İncelenecek görev")}</span><strong>{report.totals.jobs_requiring_inspection}</strong></article>
        <article><span>{t("Belirsiz eylem")}</span><strong>{report.totals.action_uncertain}</strong></article>
        <article><span>{t("Intent / çalışan eylem")}</span><strong>{report.totals.action_intent + report.totals.action_running}</strong></article>
        <article data-testid="recovery-approvals"><span>{t("Terminal olmayan onay")}</span><strong>{report.totals.approval_pending + report.totals.approval_approved}</strong></article>
      </div>
      <p className="caption">{t("Tüm DB:")} {report.totals.jobs} {t("görev ·")} {report.totals.unsettled_runs} {t("terminal olmayan çalışma (")}{report.totals.unsettled_runs_without_jobs} {t("scheduler görevi olmadan) ·")} {report.totals.input_queued + report.totals.input_running + report.totals.input_uncertain} {t("sonuçlanmamış masaüstü girdisi ·")} {report.totals.sessions_not_stopped} {t("stopped olmayan oturum kaydı. Bunlar canlılık veya başarı kanıtı değildir.")}</p>
      <h3>{t("Görev kayıtları")}</h3>
      <p className="caption">{t("İnceleme gerektirenler önce, sonra en yeni kayıtlar. Gösterilen")} {report.jobs.length} / {report.totals.jobs}. {report.jobs_truncated ? t('Liste sınırlı; sayaçlar gösterilmeyen kayıtları da kapsar.') : ''}</p>
      {report.jobs.length === 0 ? <p>{t("Scheduler görev kaydı yok.")}</p> : report.jobs.map(job => <article className="registry" key={job.job_ref} data-testid="recovery-job">
        <h4>{job.kind} · {job.status} {t("· çalışma:")} {job.run_status ?? t('yok')}</h4>
        <p className="caption">{t("Görev hash:")} <code>{job.job_ref}</code></p>
        {job.reasons.length ? <ul>{job.reasons.map(reason => <li key={reason}>{t(reasons[reason] ?? reason)}</li>)}</ul> : <p>{t("Bu görev için terminal olmayan kayıt bulunmadı.")}</p>}
      </article>)}
      <p className="caption">{t("Snapshot SHA-256:")} <code>{report.snapshot_sha256}</code></p>
    </div> : <p className="caption">{t("Envanter otomatik okunmaz veya yenilenmez. Kontrol çubuğu kullanılabilir kalır.")}</p>}
  </section>;
}
