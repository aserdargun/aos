import type {OwnedFormInvocation} from './api';
import {candidateHash} from './ownedSkillCandidateApi';

export type ReleaseFamily = {profile_sha256: string; application_key: string; tenant_key: string;
  account_role: string; task_key: string; task_sha256: string; page_draft_sha256: string;
  skill_key: string; model_role: 'system1'; field_binding_sha256: string};
export type SkillRelease = {schema_version: '1.0'; synthetic: true; purpose: 'development_release';
  family: ReleaseFamily; family_sha256: string; revision: number; parent_release_sha256: string | null;
  candidate_sha256: string; skill_sha256: string; recipe_sha256: string; review_sha256: string;
  source_run_ref: string; source_group_sha256: string; source_fingerprint_sha256: string;
  evidence_execution_sha256: string; activation_authorized: false; training_ready: false; independent_held_out: false};
export type ReleaseEntry = {release_sha256: string; release: SkillRelease;
  review_status: 'accepted' | 'revoked' | 'unavailable'; rollback_eligible: boolean};
export type CatalogFamily = {family_sha256: string; family: ReleaseFamily; selection_sha256: string | null;
  selected_release_sha256: string | null; sequence: number; releases: ReleaseEntry[]};
export type ReleaseCatalog = {schema_version: '1.0'; available: true; families: CatalogFamily[]};
export type ReleaseProposal = {schema_version: '1.0'; available: true; status: 'preview' | 'published';
  persisted: boolean; release_sha256: string; release: SkillRelease};
export type SelectionEvent = {schema_version: '1.0'; synthetic: true; purpose: 'development_selected';
  family_sha256: string; sequence: number; previous_selection_sha256: string | null;
  previous_release_sha256: string | null; release_sha256: string; operation: 'select' | 'rollback';
  rollback_evidence_execution_sha256: string | null; actor: 'local_authenticated_user';
  activation_authorized: false; training_ready: false};
export type SelectionProposal = {schema_version: '1.0'; available: true; status: 'preview' | 'selected';
  persisted: boolean; selection_sha256: string; selection: SelectionEvent;
  release_sha256: string; review_sha256: string; family_sha256: string};
export type SelectedRelease = {release_sha256: string; selection_sha256: string; release: SkillRelease};

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function key(value: unknown): boolean {
  return typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
}
function hashOrNull(value: unknown): boolean { return value === null || candidateHash(value); }
function family(value: unknown, source: OwnedFormInvocation): value is ReleaseFamily {
  return record(value) && value.model_role === 'system1' && value.profile_sha256 === source.profile_sha256
    && ['profile_sha256', 'task_sha256', 'page_draft_sha256', 'field_binding_sha256'].every(name => candidateHash(value[name]))
    && ['application_key', 'tenant_key', 'account_role', 'task_key', 'skill_key'].every(name => key(value[name]));
}
export function validSkillRelease(value: unknown, source: OwnedFormInvocation): value is SkillRelease {
  return record(value) && value.schema_version === '1.0' && value.synthetic === true
    && value.purpose === 'development_release' && family(value.family, source)
    && value.source_run_ref === source.run_ref
    && Number.isInteger(value.revision) && Number(value.revision) >= 1 && Number(value.revision) <= 64
    && (value.revision === 1 ? value.parent_release_sha256 === null : candidateHash(value.parent_release_sha256))
    && ['family_sha256', 'candidate_sha256', 'skill_sha256', 'recipe_sha256', 'review_sha256',
      'source_run_ref', 'source_group_sha256', 'source_fingerprint_sha256', 'evidence_execution_sha256']
      .every(name => candidateHash(value[name]))
    && ['activation_authorized', 'training_ready', 'independent_held_out'].every(name => value[name] === false);
}
export function validReleaseProposal(value: unknown, source: OwnedFormInvocation,
                                     reviewHash: string, parent: string | null, persisted: boolean): value is ReleaseProposal {
  return record(value) && value.schema_version === '1.0' && value.available === true
    && value.persisted === persisted && value.status === (persisted ? 'published' : 'preview')
    && candidateHash(value.release_sha256) && validSkillRelease(value.release, source)
    && value.release.review_sha256 === reviewHash && value.release.parent_release_sha256 === parent;
}
export function validReleaseCatalog(value: unknown, source: OwnedFormInvocation): value is ReleaseCatalog {
  if (!record(value) || value.schema_version !== '1.0' || value.available !== true
      || !Array.isArray(value.families) || value.families.length > 64) return false;
  const families = new Set<string>();
  return value.families.every(group => {
    if (!record(group) || !candidateHash(group.family_sha256) || families.has(group.family_sha256)
        || !family(group.family, source) || !Number.isInteger(group.sequence) || Number(group.sequence) < 0
        || Number(group.sequence) > 1024 || !Array.isArray(group.releases)
        || group.releases.length < 1 || group.releases.length > 64) return false;
    families.add(group.family_sha256);
    const releases = new Set<string>();
    for (const item of group.releases) {
      if (!record(item) || !candidateHash(item.release_sha256) || releases.has(item.release_sha256)
          || !validSkillRelease(item.release, source) || item.release.family_sha256 !== group.family_sha256
          || !['accepted', 'revoked', 'unavailable'].includes(String(item.review_status))
          || typeof item.rollback_eligible !== 'boolean' || item.rollback_eligible && item.review_status !== 'accepted') return false;
      const release = item.release;
      if (!Object.entries(group.family).every(([name, entry]) => release.family[name as keyof ReleaseFamily] === entry)) return false;
      releases.add(item.release_sha256);
    }
    return group.sequence === 0 ? group.selection_sha256 === null && group.selected_release_sha256 === null
      : candidateHash(group.selection_sha256) && typeof group.selected_release_sha256 === 'string'
        && releases.has(group.selected_release_sha256);
  });
}
export function validSelectionProposal(value: unknown, group: CatalogFamily, entry: ReleaseEntry,
                                       operation: 'select' | 'rollback', persisted: boolean): value is SelectionProposal {
  if (!record(value) || value.schema_version !== '1.0' || value.available !== true
      || value.persisted !== persisted || value.status !== (persisted ? 'selected' : 'preview')
      || !candidateHash(value.selection_sha256) || value.release_sha256 !== entry.release_sha256
      || value.review_sha256 !== entry.release.review_sha256 || value.family_sha256 !== group.family_sha256
      || !record(value.selection)) return false;
  const event = value.selection;
  return event.schema_version === '1.0' && event.synthetic === true && event.purpose === 'development_selected'
    && event.family_sha256 === group.family_sha256 && event.sequence === group.sequence + 1
    && event.previous_selection_sha256 === group.selection_sha256
    && event.previous_release_sha256 === group.selected_release_sha256
    && event.release_sha256 === entry.release_sha256 && event.operation === operation
    && event.actor === 'local_authenticated_user' && event.activation_authorized === false && event.training_ready === false
    && hashOrNull(event.previous_selection_sha256) && hashOrNull(event.previous_release_sha256)
    && (operation === 'rollback' ? candidateHash(event.rollback_evidence_execution_sha256)
      : event.rollback_evidence_execution_sha256 === null);
}
