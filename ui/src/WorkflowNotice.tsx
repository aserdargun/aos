import type {Snapshot, Tasks} from './api';
import {t} from './i18n';

export function WorkflowNotice({snapshot, tasks, showApproval}: {snapshot: Snapshot | null; tasks: Tasks | null; showApproval: () => void}) {
  if (!snapshot || !tasks) return null;
  const message = snapshot.control.status === 'stopped'
    ? 'Masaüstü durduruldu. Önce üstteki Yeniden başlat, ardından Devam et düğmesine basın.'
    : snapshot.control.owner === 'HUMAN'
      ? 'Kontrol sizde; ajan görev başlatamaz. Görev çalıştırmak için üstteki Ajana geri ver düğmesine basın.'
      : snapshot.control.owner === 'PAUSED' || snapshot.control.status === 'paused'
        ? 'Oturum duraklatıldı. Görev çalıştırmak için üstteki Devam et düğmesine basın. İptal edilmiş görev otomatik başlamaz; yeniden başlatmanız gerekir.'
        : tasks.approval
          ? 'Görev eylem onayınızı bekliyor; henüz bu eylem uygulanmadı. Ayrıntıları okuyup süre dolmadan Onayla düğmesine basın. Yeniden başlat görevi iptal eder.'
          : tasks.busy
            ? ['succeeded', 'failed', 'cancelled'].includes(tasks.jobs[0]?.status ?? '')
              ? 'Görev sona erdi; kaynaklar bırakılıyor.'
              : tasks.jobs[0]?.progress
                ? 'Görev çalışıyor; kaydedilen evre ve süreleri Görevler bölümünden izleyin. Onay gerektiğinde burada gösterilecek.'
                : 'Görev çalışıyor; gözlem, model kararı veya doğrulama bekleniyor. Onay gerektiğinde burada gösterilecek.'
            : tasks.jobs[0]?.status === 'cancelled'
              ? 'Son görev iptal edildi; tamamlanmadı. Görevler bölümünden yeniden başlatıp yeni eylem onayını bekleyin.'
              : null;
  if (!message) return null;
  return <section className="panel workflow-notice" role="status" aria-live="polite" data-testid="workflow-notice">
    <p>{t(message)}</p>
    {tasks.approval && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running'
      ? <button onClick={showApproval}>{t('Onayı göster')}</button> : null}
  </section>;
}
