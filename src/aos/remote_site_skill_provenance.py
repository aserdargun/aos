"""Read-only source binding for one manual S1 skill and an audited HTTPS route."""

from contextlib import nullcontext
from pathlib import Path
import sqlite3

from .contracts import REPO_ROOT, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .learning_events import review_learning_events_snapshot
from .remote_route_evidence import _review_remote_route_evidence
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_provenance import PrivateArgumentParser, _inspect_site_skill_sources_snapshot
from .web_application import WebApplicationProfiles
from .web_application_binding import WebReadOnlyRoutePlan, WebTaskAdmissionDraft


def inspect_remote_site_skill_sources(
        database: Path, run_id: str, *, profiles: Path, pages: Path, skills: Path,
        skill_sha256: str, selected_plan_sha256: str, route_index: int) -> dict:
    if type(route_index) is not int or not 0 <= route_index <= 7:
        raise ValueError('remote_skill_invalid_route_index')
    profile_store = WebApplicationProfiles(profiles)
    page_store = SiteKnowledgeStore(pages, profile_store)
    skill = SiteSkillStore(skills, profile_store, page_store).get(skill_sha256)
    page = page_store.get(skill.page_draft_sha256)
    with audit_snapshot(database) as (snapshot, identity):
        source = _inspect_site_skill_sources_snapshot(
            snapshot, identity, run_id, skill_sha256, skill)
        if (source['model_role'] != 'system1' or source['direct_verification_status'] != 'not_claimed'
                or source['has_direct_verified_outcome'] or source['has_downstream_verification']):
            raise ValueError('remote_skill_requires_unclaimed_s1_source')
        if source['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_skill_source_snapshot_changed')
        evidence = _review_remote_route_evidence(
            database, run_id, profiles=profiles,
            selected_profile_sha256=skill.profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            source=nullcontext((snapshot, identity)))
        if (evidence['snapshot_sha256'] != identity['sha256']
                or route_index >= evidence['route_count']):
            raise ValueError('remote_skill_route_source_changed')
        binding = snapshot.execute(
            'SELECT draft_json,plan_json FROM desktop_remote_route_bindings WHERE run_id=?',
            (run_id,)).fetchone()
        if binding is None:
            raise ValueError('remote_skill_binding_missing')
        draft = WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
        plan = WebReadOnlyRoutePlan.model_validate_json(binding['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != binding['draft_json']
                or canonical(plan.model_dump(mode='json')) != binding['plan_json']
                or draft.task.task_key != skill.task_key
                or plan.profile_sha256 != skill.profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256):
            raise ValueError('remote_skill_task_or_plan_changed')
        route = evidence['routes'][route_index]
        if (page.origin + page.route_template != plan.routes[route_index]
                or page.page_fingerprint_sha256 != route['page_fingerprint_sha256']
                or page_store.inspect(skill.page_draft_sha256,
                                      selected_profile_sha256=skill.profile_sha256,
                                      current_fingerprint_sha256=route['page_fingerprint_sha256'])['status']
                != 'draft_match'):
            raise ValueError('remote_skill_page_readback_differs')
        for profile_report in profile_store.list():
            if profile_report['profile_sha256'] != skill.profile_sha256:
                successor = profile_store.get(profile_report['profile_sha256'])
                if successor.previous_sha256 == skill.profile_sha256:
                    raise ValueError('remote_skill_profile_superseded')
        review = review_learning_events_snapshot(snapshot, identity, run_id)
        events = {event['event_id']: event for event in review['events']}
        action = snapshot.execute(
            "SELECT action_id,step_id,decision_id FROM actions WHERE run_id=? "
            "AND tool='browser.remote.route' AND json_extract(arguments_json,'$.route_index')=?",
            (run_id, route_index)).fetchall()
        if len(action) != 1:
            raise ValueError('remote_skill_route_action_ambiguous')
        route_action = action[0]
        if any(events[event_id]['source']['step_id'] != route_action['step_id']
               or events[event_id]['source']['decision_id'] != route_action['decision_id']
               for event_id in skill.source_event_ids):
            raise ValueError('remote_skill_event_not_from_selected_route')
    report = {
        'schema_version': '1.0', 'mode': 'read_only_remote_skill_source_inspection',
        'status': 'unreviewed_route_source_match',
        'skill_sha256': skill_sha256, 'profile_sha256': skill.profile_sha256,
        'page_draft_sha256': skill.page_draft_sha256,
        'plan_sha256': selected_plan_sha256, 'route_index': route_index,
        'run_ref': source['run_ref'], 'snapshot_sha256': source['snapshot_sha256'],
        'model_role': 'system1', 'deployment_sha256': source['deployment_sha256'],
        'source_event_ids': source['source_event_ids'],
        'source_request_sha256': source['source_request_sha256'],
        'profile_bound': True, 'page_readback_bound': True,
        'transport_readback_verified': True, 'site_outcome_verified': False,
        'account_verified': False, 'parameter_values_bound': False,
        'reviewed': False, 'execution_authorized': False,
        'collection_authorized': False, 'activation_authorized': False,
        'training_ready': False,
    }
    validator('remote_site_skill_provenance').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Inspect one manual S1 skill against an audited HTTPS route')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--skills', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--skill-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--route-index', type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_site_skill_sources(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            pages=arguments.pages, skills=arguments.skills,
            skill_sha256=arguments.skill_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            route_index=arguments.route_index)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote skill source inspection unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
