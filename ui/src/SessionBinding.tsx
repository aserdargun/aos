import {useEffect, useRef, useState} from 'react';
import {api} from './api';
import {locale, t} from './i18n';

interface BindingReport {
  session_status: string; session_owner: string; generation: number; job_count: number;
  unresolved_inputs: number; snapshot_sha256: string; binding_sha256: string; inspected_at: string;
  lifecycle: {recorded_stage: string; owner_observation: string; workspace_busy: boolean; container_observation: string};
  execution_authorized: false; resume_authorized: false; cleanup_authorized: false;
  lease_restored: false; approval_restored: false;
}

const ownerLabels: Record<string, string> = {same_process: 'Aynı host süreci gözlendi', not_observed: 'Süreç gözlenmedi',
  different_process: 'PID farklı bir sürece ait', different_boot: 'Farklı host açılışı', incomparable: 'Karşılaştırılamadı', unavailable: 'Gözlem alınamadı'};
const containerLabels: Record<string, string> = {not_queried: 'Sorgulanmadı', missing: 'Bulunamadı', running: 'Çalışıyor', created: 'Oluşturulmuş', exited: 'Çıkmış'};

export function SessionBinding({onError}: {onError: (failure: unknown) => void}) {
  const [report, setReport] = useState<BindingReport | null>(null);
  const [pending, setPending] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function inspect() {
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setReport(null); setPending(true);
    try {
      const next = await api<BindingReport>('/api/session/binding', undefined, abort.signal);
      if (!abort.signal.aborted) setReport(next);
    } catch (failure) {
      if (!abort.signal.aborted) onError(failure);
    } finally {
      if (!abort.signal.aborted) setPending(false);
    }
  }
  return <section className="panel" data-testid="session-binding-panel">
    <h2>{t("Oturum ve runtime bağı")}</h2>
    <p>{t("Yalnız bu backend oturumunun kalıcı SQL ve Docker doğum kaydı eşliğini okur. Eski lease veya onayı canlandırmaz; sahiplenme, silme veya devam ettirmez.")}</p>
    <p className="caption">{t("Kaynaklar host tarafından sabittir. Eski veya fixture oturumunda bağ bulunmayabilir. Görev/model eşliği bu panelde incelenmez; sayaç başarı kanıtı değildir.")}</p>
    <button disabled={pending} onClick={() => void inspect()}>{pending ? t("Oturum bağı okunuyor…") : t("Oturum bağını oku")}</button>
    {report ? <div data-testid="session-binding-report">
      <p role="status">{t("Kalıcı oturum bağı eşleşti; yürütme veya kurtarma yetkisi verilmedi.")}</p>
      <div className="metrics">
        <article><span>{t("SQL oturum durumu")}</span><strong>{report.session_status}</strong></article>
        <article><span>{t("SQL sahiplik")}</span><strong>{report.session_owner}</strong></article>
        <article><span>{t("Oturumdaki görev")}</span><strong>{report.job_count}</strong></article>
        <article><span>{t("Sonuçlanmamış girdi")}</span><strong>{report.unresolved_inputs}</strong></article>
      </div>
      <p>{t("Host süreç gözlemi:")} {t(ownerLabels[report.lifecycle.owner_observation] ?? 'Bilinmiyor')}</p>
      <p>Container: {t(containerLabels[report.lifecycle.container_observation] ?? 'Bilinmiyor')} · {t("Workspace kilidi:")} {report.lifecycle.workspace_busy ? t("Meşgul") : t("Alınabildi")}</p>
      <p className="caption">{t("Kalıcı aşama:")} {report.lifecycle.recorded_stage} · Generation: {report.generation} · {new Date(report.inspected_at).toLocaleString(locale())}. {t("Sürekli canlılık veya kesin orphan kanıtı değildir.")}</p>
      <p className="caption">{t("Bağ SHA-256:")} <code>{report.binding_sha256}</code></p>
      <p className="caption">Snapshot SHA-256: <code>{report.snapshot_sha256}</code></p>
    </div> : <p className="caption">{t("Otomatik okunmaz. Kontrol generation veya runtime değiştiğinde önceki sonuç temizlenir.")}</p>}
  </section>;
}
