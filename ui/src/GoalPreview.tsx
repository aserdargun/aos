import {t} from './i18n';
import {useEffect, useRef, useState} from 'react';
import {api, type GoalPreview as Preview} from './api';

interface Props {
  busy: boolean;
  action: (operation: () => Promise<void>) => Promise<void>;
}

export function GoalPreview({busy, action}: Props) {
  const [goal, setGoal] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  return <div className="goal-preview">
    <h3>{t("Türkçe görev önizlemesi")}</h3>
    <p id="goal-help" className="caption">{t("Yalnız sabit katalog: “hello görevini hazırla”, “yerel form görevini hazırla” veya “görsel save görevini hazırla”. Secret girmeyin. Önizleme işlem başlatmaz, görev seçimini değiştirmez veya onay vermez.")}</p>
    <form onSubmit={event => {
      event.preventDefault();
      if (busy || !goal.trim()) return;
      request.current?.abort();
      const abort = new AbortController();
      request.current = abort;
      setPreview(null);
      void action(async () => {
        try {
          const result = await api<Preview>('/api/tasks/preview', {goal}, abort.signal);
          if (!abort.signal.aborted) setPreview(result);
        } catch (failure) {
          if (!abort.signal.aborted) throw failure;
        }
      });
    }}>
      <label htmlFor="goal-text">{t("Türkçe görev metni")}</label>
      <input id="goal-text" aria-describedby="goal-help" value={goal} maxLength={1000} autoComplete="off" onChange={event => {
        request.current?.abort();
        setGoal(event.target.value);
        setPreview(null);
      }}/>
      <button type="submit" disabled={busy || !goal.trim()}>{t("Yalnız önizle")}</button>
    </form>
    {preview ? <div data-testid="goal-preview-result" role="status">
      {preview.status === 'recognized' ? <>
        <p>{t("Şablon tanındı:")} <strong>{preview.task_kind}</strong>{t(". Görev başlatılmadı.")}</p>
        <p>{t("Kapsam:")} <code>{preview.scope}</code></p>
        <pre>{preview.normalized_goal}</pre>
        <p className="caption">{t("Görev motorunun kullanılabilirliği ayrıca denetlenir. Başlatma aşağıdaki bağımsız görev seçimiyle yapılır; her eylem yeni onay ister.")}</p>
      </> : <p>{preview.status === 'negated' ? t('Olumsuzluk veya kontrol isteği algılandı. İşlem yapılmadı; kontrol için kalıcı düğmeleri kullanın.') : t('Bu metin sabit katalogda desteklenmiyor. İşlem yapılmadı.')}</p>}
    </div> : null}
  </div>;
}
