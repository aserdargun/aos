import {t, locale} from './i18n';
import type {Tasks} from './api';

const phases: Record<string, string> = {
  CREATED: 'Görev hazırlanıyor', OBSERVE: 'Gözlem alınıyor', SUPERVISOR: 'Supervisor değerlendirmesi',
  DECIDE: 'Karar seçiliyor', POLICY: 'Güvenlik kontrolü', EXECUTE: 'Eylem uygulanıyor',
  VERIFY: 'Sonuç doğrulanıyor', WAITING_HUMAN: 'İnsan yanıtı bekleniyor', REPLAN: 'Yeniden planlanıyor',
  SUCCEEDED: 'Tamamlandı', FAILED: 'Başarısız', PAUSED: 'Duraklatıldı', CANCELLED: 'İptal edildi'
};
const statuses: Record<string, string> = {
  queued: 'Hazırlanıyor', waiting_approval: 'Eylem onayı bekleniyor', paused: 'Duraklatıldı',
  succeeded: 'Tamamlandı', failed: 'Başarısız', cancelled: 'İptal edildi'
};
const callStatuses = {ok: 'Tamamlandı', error: 'Hata', timeout: 'Zaman aşımı', cancelled: 'İptal'};
const failureLabels: Record<string, string> = {
  TOOL_FAILURE: 'Araç başarısız oldu', MODEL_FAILURE: 'Model worker başarısız oldu',
  INVALID_OUTPUT: 'Model veya worker yanıtı geçersiz', TIMEOUT: 'İşlem zaman aşımına uğradı',
  UI_CHANGED: 'Arayüz değişti', ELEMENT_MISSING: 'Hedef öğe bulunamadı',
  NETWORK_FAILURE: 'Ağ işlemi başarısız oldu', APP_CRASH: 'Uygulama kapandı',
  RUNTIME_CRASH: 'Runtime kapandı', STUCK: 'İlerleme durdu', UNSAFE_ACTION: 'Güvenlik kapsamı reddetti'
};

function duration(milliseconds: number | null | undefined, unit: 'ms' | 'sn' = 'sn'): string {
  if (milliseconds === null || milliseconds === undefined || !Number.isFinite(milliseconds) || milliseconds < 0) return t('Ölçülmedi');
  return `${(unit === 'ms' ? milliseconds : milliseconds / 1000).toLocaleString(locale(), {minimumFractionDigits: 1, maximumFractionDigits: 1})} ${t(unit)}`;
}

export function TaskProgress({tasks}: {tasks: Tasks | null}) {
  const job = tasks?.jobs[0];
  if (!job) return null;
  const progress = job.progress;
  const phase = progress?.phase;
  const current = statuses[job.status] ?? (phase ? phases[phase] ?? 'Bilinmeyen kayıtlı evre' : 'Çalışıyor · evre bilgisi yok');
  return <section className="task-progress" aria-labelledby="task-progress-title" data-testid="task-progress">
    <h3 id="task-progress-title">{t("Son görevin ilerlemesi")}</h3>
    <p data-testid="task-progress-current" role="status" aria-live="polite">{t(current)}</p>
    <dl><dt>{t("Son kayıtlı evre")}</dt><dd data-testid="task-progress-phase">{phase ? <>{t(phases[phase] ?? 'Bilinmeyen evre')} · <code>{phase}</code></> : t('Evre kaydı yok')}{progress?.state_version !== null && progress?.state_version !== undefined ? <> {t("· state")} {progress.state_version}</> : null}</dd>
      <dt>{t("Toplam geçen süre · son örnek")}</dt><dd data-testid="task-progress-elapsed">{duration(progress?.elapsed_ms)}</dd></dl>
    {job.status === 'failed' && progress?.failure_code ? <p data-testid="task-progress-failure" role="status">{t('Doğrulanmış hata kodu:')} <code>{progress.failure_code}</code> · {t(failureLabels[progress.failure_code] ?? 'Bilinmeyen hata')}</p> : null}
    <p className="caption">{progress ? t('Süre onay beklemeyi ve duraklatmayı içerir; sunucunun son ölçümüdür. Bitiş tahmini değildir.') : t('Bu backend görev ilerlemesi ve süre ölçümü sağlamıyor. Sonuç veya süre tahmin edilmedi.')}</p>
    {progress ? <><h4>{t("Son tamamlanan model çağrıları")}</h4><p className="caption">{t("En yeni ilk sırada, en fazla 10 kayıt. Devam eden çağrı için süre gösterilmez.")}</p>
      {progress.model_calls.length ? <ol className="model-timings">{progress.model_calls.map((call, index) => <li key={index} data-testid="task-model-call">
        <span>{t(call.role === 'system1' ? tasks?.real_model ? 'System-1 · yerel Decider' : 'System-1 · sentetik fixture' : tasks?.real_supervisor ? 'System-2 · yerel Bonsai' : 'System-2 · sentetik fixture')}</span>
        <span>{t(callStatuses[call.status])} · {duration(call.latency_ms, 'ms')}</span>
      </li>)}</ol> : <p className="caption" data-testid="task-model-empty">{t("Henüz tamamlanmış model çağrısı kaydı yok.")}</p>}</> : null}
  </section>;
}
