import {useSyncExternalStore} from 'react';
import {translationsCore} from './translations_core';
import {translationsTasks} from './translations_tasks';

export type Language = 'en' | 'tr';
const storageKey = 'aos.ui.language';
const translations: Record<string, string> = {...translationsCore, ...translationsTasks};
const listeners = new Set<() => void>();

function storedLanguage(): Language {
  try { return localStorage.getItem(storageKey) === 'tr' ? 'tr' : 'en'; }
  catch { return 'en'; }
}

let language = storedLanguage();

function updateDocument() {
  document.documentElement.lang = language;
  document.title = language === 'en' ? 'AOS · Control center' : 'AOS · Kontrol merkezi';
}

function publish(next: Language) {
  if (language === next) return;
  language = next;
  updateDocument();
  for (const listener of listeners) listener();
}

function storageChanged(event: StorageEvent) {
  if (event.key === storageKey || event.key === null) publish(storedLanguage());
}

function subscribe(listener: () => void) {
  if (listeners.size === 0) window.addEventListener('storage', storageChanged);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener('storage', storageChanged);
  };
}

export function setLanguage(next: Language) {
  if (next !== 'en' && next !== 'tr') return;
  try { localStorage.setItem(storageKey, next); } catch {}
  publish(next);
}

export function useLanguage(): Language {
  return useSyncExternalStore(subscribe, () => language, () => 'en');
}

export function locale(): 'en-US' | 'tr-TR' {
  return language === 'en' ? 'en-US' : 'tr-TR';
}

export function t(value: string): string {
  return language === 'en' && Object.hasOwn(translations, value) ? translations[value] : value;
}

updateDocument();
