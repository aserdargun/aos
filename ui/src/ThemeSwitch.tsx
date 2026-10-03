import {useSyncExternalStore} from 'react';
import {t} from './i18n';

type Theme = 'light' | 'dark';
const storageKey = 'aos.ui.theme';
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
const listeners = new Set<() => void>();

function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(storageKey);
    return value === 'light' || value === 'dark' ? value : null;
  } catch { return null; }
}

let preference = storedTheme();
let theme: Theme = preference ?? (systemTheme.matches ? 'dark' : 'light');
document.documentElement.dataset.theme = theme;

function publish() {
  theme = preference ?? (systemTheme.matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = theme;
  for (const listener of listeners) listener();
}

function storageChanged(event: StorageEvent) {
  if (event.key === storageKey || event.key === null) {
    preference = storedTheme();
    publish();
  }
}

function subscribe(listener: () => void) {
  if (listeners.size === 0) {
    window.addEventListener('storage', storageChanged);
    systemTheme.addEventListener('change', publish);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      window.removeEventListener('storage', storageChanged);
      systemTheme.removeEventListener('change', publish);
    }
  };
}

function setTheme(next: Theme) {
  preference = next;
  try { localStorage.setItem(storageKey, next); } catch {}
  publish();
}

export function ThemeSwitch() {
  const selected = useSyncExternalStore(subscribe, () => theme);
  return <div className="theme-switch" role="group" aria-label={t('Görünüm teması')}>
    <button type="button" aria-pressed={selected === 'light'} onClick={() => setTheme('light')}>{t('Açık tema')}</button>
    <button type="button" aria-pressed={selected === 'dark'} onClick={() => setTheme('dark')}>{t('Koyu tema')}</button>
  </div>;
}
