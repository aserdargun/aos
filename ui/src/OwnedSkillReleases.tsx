import {useEffect, useRef, useState} from 'react';
import {api, type OwnedFormInvocation, type Snapshot, type Tasks} from './api';
import {t} from './i18n';
import {candidateHash} from './ownedSkillCandidateApi';
import {OwnedSkillReuseGuide} from './OwnedSkillReuseGuide';
import {validReleaseCatalog, validReleaseProposal, validSelectionProposal,
  type CatalogFamily, type ReleaseCatalog, type ReleaseEntry, type ReleaseProposal,
  type SelectedRelease, type SelectionProposal} from './ownedSkillReleaseApi';

type Props = {source: OwnedFormInvocation; snapshot: Snapshot; tasks: Tasks;
  reviewHash: string; onSelected: (release: SelectedRelease | null) => void};
type PendingSelection = {proposal: SelectionProposal; group: CatalogFamily;
  entry: ReleaseEntry; operation: 'select' | 'rollback'};
const base = '/api/tasks/owned-form-candidate/';

export function OwnedSkillReleases({source, snapshot, tasks, reviewHash, onSelected}: Props) {
  const [catalog, setCatalog] = useState<ReleaseCatalog | null>(null);
  const [parent, setParent] = useState('');
  const [proposal, setProposal] = useState<ReleaseProposal | null>(null);
  const [selection, setSelection] = useState<PendingSelection | null>(null);
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [selectionConfirmed, setSelectionConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [published, setPublished] = useState<string | null>(null);
  const [reuseGuide, setReuseGuide] = useState<{familySha256: string; release: SelectedRelease} | null>(null);
  const [reuseUnavailable, setReuseUnavailable] = useState(false);
  const serial = useRef(0);
  const scope = `${snapshot.runtime.runtime_id}:${source.run_ref}:${source.invocation_sha256}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => () => { serial.current += 1; }, []);
  useEffect(() => {
    serial.current += 1; setCatalog(null); setParent(''); setProposal(null); setSelection(null);
    setLoading(false); setPublished(null); setReleaseConfirmed(false); setSelectionConfirmed(false);
    setReuseGuide(null); setReuseUnavailable(false);
  }, [scope]);
  useEffect(() => {
    serial.current += 1; setProposal(null); setSelection(null); setReleaseConfirmed(false);
    setSelectionConfirmed(false); setLoading(false); setReuseGuide(null); setReuseUnavailable(false);
  }, [reviewHash]);
  useEffect(() => {
    if (tasks.busy || tasks.reserved || tasks.approval || snapshot.control.owner !== 'AGENT'
        || snapshot.control.status !== 'running') {
      serial.current += 1; setReuseGuide(null); setReuseUnavailable(false); setLoading(false);
    }
  }, [tasks.busy, tasks.reserved, tasks.approval, snapshot.control.owner,
    snapshot.control.status, snapshot.control.lease_id, snapshot.control.generation]);
  useEffect(() => {
    serial.current += 1; setReuseGuide(null); setReuseUnavailable(false); setLoading(false);
  }, [source.mode, source.lifecycle, source.profile_sha256, source.skill_sha256,
    source.invocation_sha256, source.run_ref, source.report_sha256, source.reuse_admission_sha256,
    snapshot.control.lease_id, snapshot.control.generation]);
  const locked = loading || tasks.busy || Boolean(tasks.reserved);

  async function current(request: number) {
    const [state, fresh] = await Promise.all([api<Snapshot>('/api/state'), api<Tasks>('/api/tasks')]);
    if (request !== serial.current || scopeRef.current !== scope
        || state.runtime.runtime_id !== snapshot.runtime.runtime_id
        || fresh.owned_form_invocation?.run_ref !== source.run_ref
        || fresh.owned_form_invocation?.invocation_sha256 !== source.invocation_sha256
        || fresh.owned_form_invocation?.profile_sha256 !== source.profile_sha256) throw new Error('release_scope_changed');
    return {state, tasks: fresh};
  }
  async function readCatalog() {
    const response = await api<unknown>(base + 'release-catalog');
    if (!validReleaseCatalog(response, source)) throw new Error('release_catalog_invalid');
    return response;
  }
  async function run(work: (request: number) => Promise<void>) {
    const request = ++serial.current;
    setReuseGuide(null); setReuseUnavailable(false);
    setLoading(true); setUnavailable(false);
    try { await work(request); }
    catch {
      if (request === serial.current) {
        setUnavailable(true); setProposal(null); setSelection(null); setCatalog(null); onSelected(null);
      }
    } finally { if (request === serial.current) setLoading(false); }
  }
  function load() {
    void run(async request => {
      const result = await readCatalog(); await current(request);
      setCatalog(result); setProposal(null); setSelection(null); onSelected(null);
    });
  }
  function previewRelease() {
    if (!candidateHash(reviewHash)) return;
    setReleaseConfirmed(false); setProposal(null); setPublished(null);
    void run(async request => {
      const response = await api<unknown>(base + 'release-preview', {
        schema_version: '1.0', review_sha256: reviewHash, parent_release_sha256: parent || null});
      if (!validReleaseProposal(response, source, reviewHash, parent || null, false)) throw new Error('release_preview_invalid');
      await current(request); setProposal(response);
    });
  }
  function publishRelease() {
    if (!proposal || !releaseConfirmed) return;
    setReleaseConfirmed(false);
    void run(async request => {
      const response = await api<unknown>(base + 'release-publish', {
        schema_version: '1.0', review_sha256: proposal.release.review_sha256,
        parent_release_sha256: proposal.release.parent_release_sha256, confirm_sha256: proposal.release_sha256});
      if (!validReleaseProposal(response, source, proposal.release.review_sha256,
          proposal.release.parent_release_sha256, true) || response.release_sha256 !== proposal.release_sha256)
        throw new Error('release_publish_changed');
      const next = await readCatalog(); await current(request);
      setCatalog(next); setPublished(response.release_sha256); setProposal(null); setSelection(null);
    });
  }
  function previewSelection(group: CatalogFamily, entry: ReleaseEntry, operation: 'select' | 'rollback') {
    setSelection(null); setSelectionConfirmed(false);
    void run(async request => {
      const response = await api<unknown>(base + 'selection-preview', {schema_version: '1.0',
        release_sha256: entry.release_sha256, expected_selection_sha256: group.selection_sha256, operation});
      if (!validSelectionProposal(response, group, entry, operation, false)) throw new Error('selection_preview_invalid');
      await current(request); setSelection({proposal: response, group, entry, operation});
    });
  }
  function commitSelection() {
    if (!selection || !selectionConfirmed) return;
    setSelectionConfirmed(false);
    void run(async request => {
      const response = await api<unknown>(base + 'selection-commit', {schema_version: '1.0',
        release_sha256: selection.entry.release_sha256, expected_selection_sha256: selection.group.selection_sha256,
        operation: selection.operation, confirm_sha256: selection.proposal.selection_sha256});
      if (!validSelectionProposal(response, selection.group, selection.entry, selection.operation, true)
          || response.selection_sha256 !== selection.proposal.selection_sha256) throw new Error('selection_commit_changed');
      const next = await readCatalog(); await current(request);
      if (!next.families.some(group => group.family_sha256 === response.family_sha256
          && group.selection_sha256 === response.selection_sha256
          && group.selected_release_sha256 === response.release_sha256)) throw new Error('selection_head_changed');
      setCatalog(next); setSelection(null); onSelected(null);
    });
  }
  function useSelected(group: CatalogFamily) {
    void run(async request => {
      const next = await readCatalog(); await current(request);
      const fresh = next.families.find(item => item.family_sha256 === group.family_sha256);
      const entry = fresh?.releases.find(item => item.release_sha256 === fresh.selected_release_sha256);
      if (!fresh || !entry || !fresh.selection_sha256 || entry.review_status !== 'accepted'
          || fresh.selection_sha256 !== group.selection_sha256) throw new Error('selected_release_unavailable');
      setCatalog(next); onSelected({release_sha256: entry.release_sha256,
        selection_sha256: fresh.selection_sha256, release: entry.release});
    });
  }
  function prepareReuse(group: CatalogFamily) {
    const expectedSelection = group.selection_sha256;
    const expectedRelease = group.selected_release_sha256;
    if (!expectedSelection || !candidateHash(expectedSelection) || !expectedRelease || !candidateHash(expectedRelease)) return;
    const request = ++serial.current;
    setReuseGuide(null); setReuseUnavailable(false); setLoading(true);
    void (async () => {
      try {
        const [freshCatalog, freshScope] = await Promise.all([readCatalog(), current(request)]);
        const freshSource = freshScope.tasks.owned_form_invocation;
        if (!freshScope.state.runtime.running || freshScope.state.control.owner !== 'AGENT'
            || freshScope.state.control.status !== 'running'
            || freshScope.state.control.lease_id !== snapshot.control.lease_id
            || freshScope.state.control.generation !== snapshot.control.generation
            || freshScope.tasks.busy || freshScope.tasks.reserved || freshScope.tasks.approval !== null
            || freshSource?.mode !== 'owned_synthetic_form_invocation' || freshSource.mode !== source.mode
            || freshSource.lifecycle !== 'audited'
            || freshSource.skill_sha256 !== source.skill_sha256
            || freshSource.profile_sha256 !== source.profile_sha256
            || freshSource.invocation_sha256 !== source.invocation_sha256
            || freshSource.run_ref !== source.run_ref
            || freshSource.report_sha256 !== source.report_sha256
            || freshSource.reuse_admission_sha256 !== source.reuse_admission_sha256) throw new Error('reuse_source_not_idle');
        const freshGroup = freshCatalog.families.find(item => item.family_sha256 === group.family_sha256);
        const freshEntry = freshGroup?.releases.find(item => item.release_sha256 === expectedRelease);
        if (request !== serial.current || !freshGroup || !freshEntry
            || freshGroup.selection_sha256 !== expectedSelection
            || freshGroup.selected_release_sha256 !== expectedRelease
            || freshEntry.review_status !== 'accepted') throw new Error('reuse_selection_changed');
        setCatalog(freshCatalog);
        setReuseGuide({familySha256: group.family_sha256, release: {
          release_sha256: freshEntry.release_sha256, selection_sha256: freshGroup.selection_sha256,
          release: freshEntry.release}});
      } catch {
        if (request === serial.current) { setReuseGuide(null); setReuseUnavailable(true); }
      } finally {
        if (request === serial.current) setLoading(false);
      }
    })();
  }
  return <section data-testid="skill-releases" className="owned-skill-candidate">
    <h4>{t('Skill sürümleri ve gelişim seçimi')}</h4>
    <p className="caption">{t('Yalnız incelenmiş sentetik sürümler. Seçim görev başlatmaz; aktivasyon, bağımsız doğrulama veya eğitim değildir. Geri dönüş açık onay ister.')}</p>
    <button data-testid="release-catalog-load" disabled={locked} onClick={load}>{t('Sürüm kataloğunu yükle')}</button>
    <fieldset disabled={locked}>
      <legend>{t('İncelemeden değişmez sürüm kaydet')}</legend>
      <p>{t('Yeni koşu için inceleme SHA-256')}: <code>{reviewHash || '—'}</code></p>
      <label>{t('Önceki sürüm')}<select data-testid="release-parent" value={parent}
        onChange={event => { setParent(event.target.value); setProposal(null); setReleaseConfirmed(false); }}>
        <option value="">{t('İlk sürüm')}</option>
        {catalog?.families.flatMap(group => group.releases.map(entry => <option key={entry.release_sha256} value={entry.release_sha256}>
          {entry.release.family.skill_key} · v{entry.release.revision} · {entry.release_sha256.slice(0, 12)}
        </option>))}
      </select></label>
      <button data-testid="release-preview" disabled={!candidateHash(reviewHash)} onClick={previewRelease}>{t('Sürümü önizle')}</button>
    </fieldset>
    {proposal ? <div data-testid="release-preview-result">
      <p>{proposal.release.family.skill_key} · v{proposal.release.revision}</p>
      <p><code>{proposal.release_sha256}</code></p>
      <p>{t('Recipe SHA-256:')} <code>{proposal.release.recipe_sha256}</code></p>
      <label><input data-testid="release-confirm" type="checkbox" checked={releaseConfirmed} disabled={locked}
        onChange={event => setReleaseConfirmed(event.target.checked)}/>{t('Bu exact sürümün kaydını onaylıyorum')}</label>
      <button data-testid="release-publish" disabled={locked || !releaseConfirmed} onClick={publishRelease}>{t('Sürümü kaydet')}</button>
    </div> : null}
    {published ? <p data-testid="release-published">{t('Sürüm kaydedildi; seçim ayrı bir işlemdir.')} <code>{published}</code></p> : null}
    {catalog ? <div data-testid="release-catalog">
      {!catalog.families.length ? <p>{t('Henüz kayıtlı sürüm yok.')}</p> : null}
      {catalog.families.map(group => <div key={group.family_sha256} data-testid="release-family">
        <h5>{group.family.skill_key} · {group.family.account_role}</h5>
        <p>{t('Gelişim seçimi:')} <code>{group.selected_release_sha256 || '—'}</code></p>
        <p>{t('Seçim kaydı:')} <code>{group.selection_sha256 || '—'}</code></p>
        <button data-testid="release-use-selected" disabled={locked || !group.selection_sha256}
          onClick={() => useSelected(group)}>{t('Seçili sürümü yeni koşuya bağla')}</button>
        {group.selected_release_sha256 && group.selection_sha256 ? <button data-testid="release-prepare-reuse"
          disabled={locked} onClick={() => prepareReuse(group)}>{t('Yeni oturumda yeniden kullanmayı hazırla')}</button> : null}
        {!locked && !tasks.approval && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running'
          && reuseGuide?.familySha256 === group.family_sha256
          && group.selection_sha256 === reuseGuide.release.selection_sha256
          && group.selected_release_sha256 === reuseGuide.release.release_sha256
          ? <OwnedSkillReuseGuide release={reuseGuide.release} managerScope={tasks.manager_scope}/> : null}
        {group.releases.map(entry => <div key={entry.release_sha256} data-testid="release-entry" data-release={entry.release_sha256}>
          <p>v{entry.release.revision} · {entry.review_status} · <code>{entry.release_sha256}</code></p>
          <p>{t('Recipe SHA-256:')} <code>{entry.release.recipe_sha256}</code></p>
          <button data-testid="release-select" disabled={locked || entry.review_status !== 'accepted'
            || entry.release_sha256 === group.selected_release_sha256}
            onClick={() => previewSelection(group, entry, 'select')}>{t('Bu sürümü seçmeyi önizle')}</button>
          <button data-testid="release-rollback" disabled={locked || !entry.rollback_eligible
            || entry.release_sha256 === group.selected_release_sha256}
            onClick={() => previewSelection(group, entry, 'rollback')}>{t('Bu sürüme dönüşü önizle')}</button>
        </div>)}
      </div>)}
    </div> : null}
    {selection ? <div data-testid="selection-preview-result">
      <p>{selection.operation} · v{selection.entry.release.revision}</p>
      <p><code>{selection.proposal.selection_sha256}</code></p>
      <label><input data-testid="selection-confirm" type="checkbox" checked={selectionConfirmed} disabled={locked}
        onChange={event => setSelectionConfirmed(event.target.checked)}/>{t('Bu exact sürüm seçimini veya geri dönüşü onaylıyorum')}</label>
      <button data-testid="selection-commit" disabled={locked || !selectionConfirmed} onClick={commitSelection}>{t('Gelişim seçimini kaydet')}</button>
    </div> : null}
    {loading ? <p role="status">{t('Sürüm kanıtları denetleniyor…')}</p> : null}
    {unavailable ? <p role="alert" data-testid="release-unavailable">{t('Sürüm veya seçim değişti. Kataloğu yenileyin; başka sürüme otomatik geçilmedi.')}</p> : null}
    {reuseUnavailable ? <p role="alert" data-testid="reuse-guide-unavailable">{t('Seçim artık aynı değil, incelenmiş değil veya kaynak oturum değişti. Kataloğu yenileyin; hiçbir görev başlatılmadı.')}</p> : null}
  </section>;
}
