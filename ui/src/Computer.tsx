import {forwardRef, useEffect, useImperativeHandle, useRef, useState} from 'react';
import type RFB from '@novnc/novnc';
import {api, ApiError, type PointerTelemetry, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import './computer.css';

export interface ComputerHandle { disconnect(): Promise<void> }

export const Computer = forwardRef<ComputerHandle, {snapshot: Snapshot}>(function Computer({snapshot}, ref) {
  const language = useLanguage();
  const target = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const remote = useRef<RFB | null>(null);
  const [connection, setConnection] = useState('Bağlantı bekleniyor');
  const [pointer, setPointer] = useState<PointerTelemetry | {available: false; reason: 'endpoint_unavailable' | 'request_failed'} | null>(null);
  const [marker, setMarker] = useState<{left: number; top: number} | null>(null);
  async function disconnect() {
    const previous = remote.current;
    remote.current = null;
    if (!previous) return;
    await new Promise<void>(resolve => {
      previous.addEventListener('disconnect', () => resolve(), {once: true});
      previous.disconnect();
      setTimeout(resolve, 2000);
    });
  }
  useImperativeHandle(ref, () => ({disconnect}), []);
  useEffect(() => {
    target.current?.querySelector('canvas')?.setAttribute('aria-label', t('İzole masaüstü tuvali'));
  }, [language]);
  useEffect(() => {
    if (!snapshot.runtime.running) {
      setPointer({available: false, reason: 'desktop_stopped', sampled_at: ''});
      setMarker(null);
      return;
    }
    if (!snapshot.runtime.pointer_tracking) {
      setPointer({available: false, reason: 'endpoint_unavailable'});
      setMarker(null);
      return;
    }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending: AbortController | null = null;
    async function sample() {
      pending = new AbortController();
      try {
        const result = await api<PointerTelemetry>('/api/desktop/pointer', undefined, pending.signal);
        if (stopped) return;
        setPointer(result);
        const canvas = target.current?.querySelector('canvas');
        const bounds = canvas?.getBoundingClientRect();
        const screenBounds = screen.current?.getBoundingClientRect();
        if (result.available && bounds && screenBounds && result.width > 0 && result.height > 0) {
          setMarker({left: bounds.left - screenBounds.left + result.x / result.width * bounds.width,
                     top: bounds.top - screenBounds.top + result.y / result.height * bounds.height});
        } else {
          setMarker(null);
        }
      } catch (failure) {
        if (stopped) return;
        setPointer({available: false, reason: failure instanceof ApiError && failure.status === 404
          ? 'endpoint_unavailable' : 'request_failed'});
        setMarker(null);
        if (failure instanceof ApiError && failure.status === 404) return;
      }
      timer = setTimeout(() => void sample(), 500);
    }
    void sample();
    return () => { stopped = true; if (timer) clearTimeout(timer); pending?.abort(); };
  }, [snapshot.runtime.runtime_id, snapshot.runtime.running, snapshot.runtime.pointer_tracking]);
  useEffect(() => {
    let active = true;
    if (!snapshot.runtime.running) {
      setConnection('Masaüstü durduruldu');
      return;
    }
    setConnection('İzole masaüstüne bağlanıyor');
    import('@novnc/novnc').then(({default: Client}) => {
      if (!active || !target.current) return;
      const client = new Client(target.current, `ws://${location.host}/websockify`);
      target.current.querySelector('canvas')?.setAttribute('aria-label', t('İzole masaüstü tuvali'));
      remote.current = client;
      client.scaleViewport = true;
      client.resizeSession = false;
      client.viewOnly = snapshot.control.owner !== 'HUMAN';
      client.addEventListener('connect', () => { if (active) setConnection('Canlı · 1280 × 800'); });
      client.addEventListener('disconnect', () => {
        if (remote.current === client) remote.current = null;
        if (active) setConnection('Bağlantı kapalı · yeniden bağlanmak için yenileyin');
      });
      client.addEventListener('securityfailure', () => { if (active) setConnection('VNC kimlik doğrulaması reddedildi'); });
    }).catch(() => { if (active) setConnection('Görüntü istemcisi yüklenemedi'); });
    return () => { active = false; void disconnect(); };
  }, [snapshot.control.lease_id, snapshot.control.owner, snapshot.runtime.running]);
  return <section className="computer-panel" aria-label={t('İzole bilgisayar')}>
    <div className="panel-heading"><h2>{t('Bilgisayar')}</h2><span role="status">{t(connection)}</span></div>
    <div className="remote-screen-wrap" ref={screen}><div className="remote-screen" ref={target}/>{marker ? <span className="desktop-pointer-marker" data-testid="desktop-pointer-marker" aria-hidden="true" style={{left: marker.left, top: marker.top}}/> : null}</div>
    <p className="caption" data-testid="desktop-pointer-status">{t('Canlı imleç konumu:')} {pointer?.available
      ? `${pointer.x}, ${pointer.y} / ${pointer.width} × ${pointer.height}`
      : t(pointer?.reason === 'desktop_stopped' ? 'Masaüstü durduruldu'
        : pointer?.reason === 'endpoint_unavailable' ? 'Bu backend imleç izlemesini henüz desteklemiyor'
        : pointer?.reason === 'runtime_changed' ? 'Runtime değişti; imleç örneği alınamadı'
        : pointer?.reason === 'runtime_unavailable' ? 'Runtime kullanılamıyor'
        : pointer?.reason === 'pointer_unavailable' || pointer?.reason === 'request_failed' ? 'İmleç konumu kullanılamıyor'
        : 'Örnek bekleniyor')}</p>
    <p className="caption">{t(snapshot.control.owner === 'HUMAN' ? 'Klavye ve fare yalnız izole masaüstüne gider.' : 'Sunucu seviyesinde salt görüntüleme. Kontrolü alarak giriş yapabilirsiniz.')}</p>
  </section>;
});
