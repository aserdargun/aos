import {version} from '../package.json';
import {useLanguage} from './i18n';

export function SourceRelease() {
  const language = useLanguage();
  return <section className="development-source-release" data-testid="source-release">
    <div><span>{language === 'tr' ? 'Arayüz kaynak / prototip sürümü' : 'UI source / prototype version'}</span><strong>v{version}</strong></div>
    <p>{language === 'tr'
      ? 'Test edilebilir prototip. Tam ürün kabulü açık; arayüz sürümü çalışan backend kimliğini veya Scientist/GPU kabulünü kanıtlamaz.'
      : 'Testable prototype. Full product acceptance remains open; the UI version does not prove backend identity or Scientist/GPU acceptance.'}</p>
  </section>;
}
