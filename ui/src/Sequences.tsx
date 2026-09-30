import {t} from './i18n';
import {useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';

const presets = [
  {label: 'Hello → Form', kinds: ['hello', 'browser_form']},
  {label: 'Form → Görsel SAVE', kinds: ['browser_form', 'vision_canvas']},
  {label: 'Hello → Form → Görsel SAVE', kinds: ['hello', 'browser_form', 'vision_canvas']}
];

export function Sequences({tasks, snapshot, busy, action}: {
  tasks: Tasks | null; snapshot: Snapshot | null; busy: boolean;
  action: (operation: () => Promise<void>) => Promise<void>;
}) {
  const [preset, setPreset] = useState(0);
  const selected = presets[preset];
  const available = selected.kinds.every(kind => tasks?.kinds?.includes(kind));
  const sequence = tasks?.sequence;
  return <article className="registry" data-testid="sequences">
    <h3>{t("Sıralı görev akışı")}</h3>
    <p className="caption">{t("Yalnız sabit görevler; önceki görev bağımsız doğrulanmadan sonraki başlamaz. Her gerçek eylem yine ayrı onay ister. Ret, hata veya kontrol devri kalan adımları durdurur.")}</p>
    <p className="caption">{t("Duraklat kalan akışı iptal eder; yalnız o anda çalışan görev mevcut Pause/Resume kurallarıyla korunabilir. Devam et kalan akışı yeniden başlatmaz. Backend çökmesinden sonra otomatik devam yoktur.")}</p>
    <label htmlFor="sequence-preset">{t("Görev akışı")} </label>
    <select id="sequence-preset" value={preset} disabled={busy || tasks?.reserved} onChange={event => setPreset(Number(event.target.value))}>
      {presets.map((option, index) => <option key={option.label} value={index} disabled={!option.kinds.every(kind => tasks?.kinds?.includes(kind))}>{t(option.label)}</option>)}
    </select>
    {!available ? <p className="caption">{t("Bu akış için gereken görev motorları açık değil.")}</p> : null}
    <p><button disabled={busy || !available || !tasks?.available || tasks.reserved || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onClick={() => void action(async () => {
      if (!snapshot) return;
      await api('/api/sequences', {plan: {kinds: selected.kinds}, lease_id: snapshot.control.lease_id, generation: snapshot.control.generation});
    })}>{t("Sıralı akışı başlat")}</button></p>
    {sequence ? <div data-testid="sequence-status">
      <p><strong>{sequence.status}</strong> {t("· Doğrulanan görev:")} {sequence.completed} / {sequence.plan.kinds.length}</p>
      <p>{sequence.plan.kinds.join(' → ')}</p>
      <p className="caption">{sequence.reason} {t("· Otomatik tekrar kapalı. Ayrı eylem onayları ve çalışma izleri aynı paneldedir.")}</p>
    </div> : null}
  </article>;
}
