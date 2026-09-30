import type {Tasks} from './api';
import {t} from './i18n';

const supported = [['hello', 'Hello dosyası'], ['browser_form', 'Yerel browser formu'], ['browser_local_navigation', 'Yerel iki sayfa gezintisi'], ['vision_canvas', 'Görsel SAVE görevi']] as const;

export function FirstUse({tasks, navigate}: {tasks: Tasks | null; navigate: (tab: 'Görevler' | 'Plan') => void}) {
  const capabilities: readonly (readonly [string, string])[] = [
    ...supported,
    ...(tasks?.kinds?.includes('browser_staging_workflow') ? [['browser_staging_workflow', 'Yerel dört adımlı uygulama akışı'] as const] : []),
    ...(tasks?.kinds?.includes('browser_remote_entry') ? [['browser_remote_entry', 'Uzak HTTPS giriş denemesi'] as const] : [])
  ];
  return <section className="panel first-use" aria-labelledby="first-use-title" data-testid="first-use">
    <h2 id="first-use-title">{t("İlk deneme · v0.1 yerel pilot")}</h2>
    <p data-testid="pilot-engine">{tasks === null ? t("Görev motoru bilgisi bekleniyor…") : !tasks.available ? t("Görev motoru kapalı; görev başlatılamaz.") : tasks.real_model ? t("Gerçek yerel Decider · sabit deployment") : t("SENTETİK TEST MOTORU · gerçek model sonucu değildir")}</p>
    <p className="caption">{t("Deneysel, üretim kullanımı için değil. Genel sohbet veya serbest komut çalıştırma yok; eğitim kapalı.")}</p>
    <ul>{capabilities.map(([kind, label]) => <li key={kind} data-testid={`first-use-capability-${kind}`}>{t(label)}<span>{tasks === null ? t("Durum bekleniyor") : tasks.available && tasks.kinds?.includes(kind) ? kind === 'vision_canvas' ? tasks.real_supervisor ? t("Açık · gerçek Bonsai gözlemci") : t("Açık · sentetik gözlemci") : t("Açık") : t("Bu oturumda kapalı")}</span></li>)}</ul>
    <p className="caption">{t('Görev durumu sunucunun bu oturumda sunduğu türlere dayanır; genel web uygulaması veya gerçek site başarısı değildir.')}</p>
    <p className="caption">{t("Görevler’de bir görev seçin; her eylemi okuyup ayrı onaylayın. Sonucu ve bağımsız doğrulamayı İzi aç ile inceleyin. Plan önizlemesi tek başına görev başlatmaz.")}</p>
    <div className="first-use-actions"><button onClick={() => navigate('Görevler')}>{t("İlk göreve git")}</button><button onClick={() => navigate('Plan')}>{t("Plan oluşturmaya git")}</button></div>
    <details><summary>{t("Kontrol ve güvenlik sınırları")}</summary><p className="caption">{tasks?.vision_display === 'desktop' ? t("Browser formu ve görsel SAVE, Görevler panelindeki aynı canlı noVNC masaüstünde görünür.") : tasks?.browser_display === 'desktop' ? t("Browser formu Görevler panelindeki aynı canlı noVNC masaüstünde görünür. Vision ayrı ağsız headless tarayıcıda çalışır; masaüstünde görünmez.") : t("Browser ve vision görevleri ayrı ağsız headless tarayıcıda çalışır; aşağıdaki noVNC masaüstünde görünmez.")} {t("Duraklat / Devam et yalnız aynı canlı backend oturumunda geçerlidir; çökme sonrası otomatik devam yoktur.")}</p><p className="caption">{t("Durdur görevi ve izole masaüstünü durdurur, backend’i kapatmaz. Kontrolü al ve Çıkış aktif görevi iptal eder. Yerel pilot servisini tamamen kapatmak için terminalde")} <code>.venv/bin/python -m aos.local_app stop</code> {t("kullanın; doğrudan başlatılmış backend kendi terminalinden kapatılır.")}</p></details>
  </section>;
}
