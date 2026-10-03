import {useEffect, useRef, useState} from 'react';
import {api} from './api';
import {t} from './i18n';

interface Study {
  schema: string;
  capability: {suite_id: string};
  snapshot: {source_kind: string; source_id: string; source_version: string; rows: number;
    train_rows: number; evaluation_input_rows: number; sensors: string[]};
  grid: {selection: string; request_semantics: string; available_prefix_count: number;
    configurations: {position: number; method: string; configuration_sha256: string; configuration: unknown}[]};
}

interface Readback {
  study: Study;
  metadata_only: boolean;
  execution_authorized: boolean;
  snapshot_content_verified: boolean;
  candidate_code_verified: boolean;
}

export function ScientistCpuStudy({suite, disabled}: {suite: string; disabled: boolean}) {
  const [result, setResult] = useState<Readback | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [observedAt, setObservedAt] = useState('');
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    setResult(null);
    setError('');
    setObservedAt('');
    setBusy(false);
    return () => { active.current?.abort(); active.current = null; };
  }, [suite]);
  const read = async () => {
    if (active.current || disabled) return;
    const abort = new AbortController();
    active.current = abort;
    setBusy(true);
    setError('');
    setResult(null);
    try {
      const value = await api<Readback>('/api/scientist/cpu-study', undefined, abort.signal);
      if (abort.signal.aborted) return;
      if (value.metadata_only !== true || value.execution_authorized !== false
          || value.snapshot_content_verified !== false || value.candidate_code_verified !== false
          || value.study?.schema !== 'scientist.lab-cpu-study.v1' || value.study.capability?.suite_id !== suite
          || value.study.snapshot?.source_kind !== 'synthetic' || !Array.isArray(value.study.snapshot.sensors)
          || typeof value.study.snapshot.source_id !== 'string' || typeof value.study.snapshot.source_version !== 'string'
          || !Number.isSafeInteger(value.study.snapshot.rows) || value.study.snapshot.rows < 3 || value.study.snapshot.rows > 4096
          || !Number.isSafeInteger(value.study.snapshot.train_rows) || value.study.snapshot.train_rows < 2
          || !Number.isSafeInteger(value.study.snapshot.evaluation_input_rows) || value.study.snapshot.evaluation_input_rows < 1
          || value.study.snapshot.rows !== value.study.snapshot.train_rows + value.study.snapshot.evaluation_input_rows
          || value.study.snapshot.sensors.length < 1 || value.study.snapshot.sensors.length > 64
          || !value.study.snapshot.sensors.every(sensor => typeof sensor === 'string' && /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(sensor))
          || value.study.grid?.selection !== 'ordered_registered_prefix'
          || value.study.grid.request_semantics !== 'first_n_of_available_prefix'
          || !Array.isArray(value.study.grid.configurations) || value.study.grid.configurations.length < 1
          || value.study.grid.configurations.length > 35
          || value.study.grid.available_prefix_count !== value.study.grid.configurations.length
          || !value.study.grid.configurations.every((entry, index) => entry?.position === index + 1
            && ['lsh', 'optics', 'som'].includes(entry.method)
            && typeof entry.configuration_sha256 === 'string' && /^[a-f0-9]{64}$/.test(entry.configuration_sha256))) {
        throw new Error(t('Deney tanımı doğrulanamadı.'));
      }
      setResult(value);
      setObservedAt(new Date().toISOString());
    } catch (exception) {
      if (!abort.signal.aborted) setError(String(exception));
    } finally {
      if (active.current === abort) {
        active.current = null;
        setBusy(false);
      }
    }
  };
  return <section data-testid="scientist-cpu-study">
    <h3>{t('Veri ve yöntem tanımı')}</h3>
    <p>{t('Yalnız metadata okunur; deney veya model başlatılmaz. Sonuç, onay ya da ham veri doğrulaması değildir.')}</p>
    <button type="button" disabled={disabled || busy} onClick={() => void read()}>{t('Deney tanımını oku')}</button>
    {error ? <p role="alert">{error}</p> : null}
    {result ? <div data-testid="scientist-cpu-study-result">
      <p>{t('Okuma zamanı')}: {observedAt}</p>
      <p>{result.study.snapshot.source_id} · {result.study.snapshot.source_version}</p>
      <p>{t('Sensörler')}: {result.study.snapshot.sensors.join(', ')}</p>
      <p>{t('Toplam satır')}: {result.study.snapshot.rows} · {t('Eğitim satırı')}: {result.study.snapshot.train_rows}
        {' · '}{t('Değerlendirme giriş satırı')}: {result.study.snapshot.evaluation_input_rows}</p>
      <p>{t('Giriş satırları skorlanan örnek sayısı değildir. Deney bütçesi N, kayıtlı sıranın ilk N ayarını çalıştırır; yöntem sırası başarı sıralaması değildir.')}</p>
      <ol>{result.study.grid.configurations.map(entry => <li key={entry.position}>
        <strong>{entry.method.toUpperCase()}</strong> · <code>{entry.configuration_sha256}</code>
        <details><summary>{t('Ayarlar')}</summary><pre>{JSON.stringify(entry.configuration, null, 2)}</pre></details>
      </li>)}</ol>
      <details><summary>{t('Salt okunur tanım')}</summary><pre>{JSON.stringify(result.study, null, 2)}</pre></details>
    </div> : null}
  </section>;
}
