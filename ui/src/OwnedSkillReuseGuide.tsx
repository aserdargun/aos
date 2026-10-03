import {t} from './i18n';
import type {SelectedRelease} from './ownedSkillReleaseApi';

type Props = {release: SelectedRelease; managerScope?: {project: string; port: number}};

export function OwnedSkillReuseGuide({release, managerScope}: Props) {
  if (managerScope && (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(managerScope.project)
      || !Number.isInteger(managerScope.port) || managerScope.port < 1024
      || managerScope.port > 65535 || managerScope.port === 8765)) return null;
  const scope = managerScope ? ` --project ${managerScope.project} --project-port ${managerScope.port}` : '';
  const previewCommand = `./scripts/aos-v1 preview-owned-skill-reuse${scope} --owned-skill-release-sha256 ${release.release_sha256} --owned-skill-selection-sha256 ${release.selection_sha256}`;
  const startCommand = `./scripts/aos-v1 start${scope} --owned-skill-release-sha256 ${release.release_sha256} --owned-skill-selection-sha256 ${release.selection_sha256} --owned-skill-reuse-confirm-sha256 PREVIEW_SHA256`;

  return <div className="registry" data-testid="skill-reuse-guide">
    <h5>{t('Yeni oturumda yeniden kullanım')}</h5>
    <p>{t('Yalnızca ./scripts/aos-v1 ile açılmış bu yönetilen sentetik kaynak oturumu içindir; genel site kullanımı veya eğitim değildir. Eski görevler ve onaylar devam ettirilmez.')}</p>
    <p>{t('Bu panel kapanmadan önce aşağıdaki komutları kaydedin veya kopyalayın. Kaynak temiz kapandıktan sonra CachyOS terminalinde önce önizleme, sonra yeni oturum başlatma komutu çalıştırılır; burada hiçbir komut çalıştırılmaz.')}</p>
    <p>{t('Yalnız bu kaynak oturumu durdurun; ilgisiz bir oturumu durdurmayın.')}</p>
    <p>{t('Önizleme ile başlatma arasında başka bir oturum başlatmayın.')}</p>
    <pre data-testid="reuse-preview-command" style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{previewCommand}</pre>
    <p>{t('Kaynak oturumu temiz kapandıktan sonra preview çıktısını inceleyin; PREVIEW_SHA256 yerine çıktıda verilen hash’i kullanın.')}</p>
    <pre data-testid="reuse-start-command" style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{startCommand}</pre>
    <p>{t('Yeni oturum token’ı için')} <code>{`./scripts/aos-v1 token${scope}`}</code> {t('komutunu kullanın. Yeni görevde altı ayrı manuel onay gerekir.')}</p>
  </div>;
}
