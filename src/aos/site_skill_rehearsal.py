"""Read-only, synthetic-only rehearsal of a symbolic skill and variation plan."""

import argparse
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import Field

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_audit import SYNTHETIC_POLICIES, audit_snapshot
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_provenance import inspect_site_skill_sources
from .site_skill_validation import SiteSkillValidationPlan, preview_skill_validation
from .site_skill_validation_cli import read_plan_source
from .web_application import Checksum, WebApplicationProfiles


RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


class SiteSkillRehearsalReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['source_request_bound_parameter_unbound']
    skill_sha256: Checksum
    plan_sha256: Checksum
    run_ref: Checksum
    snapshot_sha256: Checksum
    model_role: Literal['system1', 'system2']
    source_event_count: int = Field(ge=1, le=32)
    source_variant_count: int = Field(ge=1, le=16)
    development_count: int = Field(ge=1, le=14)
    held_out_count: int = Field(ge=2, le=15)
    source_inspection_status: Literal['unreviewed_source_match']
    structural_status: Literal['structure_only']
    source_request_bound: Literal[True]
    source_variant_bound: Literal[False]
    held_out_separated_structurally: Literal[True]
    execution_performed: Literal[False]
    outcomes_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]


def rehearse_site_skill(database: Path, run_id: str, *, profiles: Path, pages: Path,
                        skills: Path, plan: SiteSkillValidationPlan,
                        skill_sha256: str, plan_sha256: str,
                        snapshot_sha256: str) -> dict:
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError('invalid_rehearsal_run')
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    if (skill_sha256 != plan.skill_sha256 or plan_sha256 != digest(plan.model_dump())
            or not isinstance(snapshot_sha256, str)
            or re.fullmatch(r'[a-f0-9]{64}', snapshot_sha256) is None):
        raise ValueError('rehearsal_selection_mismatch')
    profile_store = WebApplicationProfiles(profiles)
    page_store = SiteKnowledgeStore(pages, profile_store)
    skill_store = SiteSkillStore(skills, profile_store, page_store)
    structure = preview_skill_validation(skill_store, plan)
    source = inspect_site_skill_sources(database, run_id, profiles=profiles, pages=pages,
                                        skills=skills, skill_sha256=skill_sha256)
    with audit_snapshot(database) as (snapshot, identity):
        run = snapshot.execute('SELECT policy_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None or run['policy_version'] not in SYNTHETIC_POLICIES:
            raise ValueError('rehearsal_requires_synthetic_source')
        if identity['sha256'] != source['snapshot_sha256']:
            raise ValueError('rehearsal_source_stale_or_wrong_scope')
        if source['source_request_sha256'] != plan.source_variant_sha256:
            raise ValueError('rehearsal_source_request_unbound')
    if (source['snapshot_sha256'] != snapshot_sha256
            or source['model_role'] != plan.model_role
            or source['profile_sha256'] != plan.profile_sha256):
        raise ValueError('rehearsal_source_stale_or_wrong_scope')
    return SiteSkillRehearsalReport.model_validate({
        'schema_version': '1.0', 'synthetic': True,
        'status': 'source_request_bound_parameter_unbound',
        'skill_sha256': skill_sha256, 'plan_sha256': plan_sha256,
        'run_ref': source['run_ref'], 'snapshot_sha256': snapshot_sha256,
        'model_role': plan.model_role,
        'source_event_count': source['source_event_count'],
        'source_variant_count': structure['source_variant_count'],
        'development_count': structure['development_count'],
        'held_out_count': structure['held_out_count'],
        'source_inspection_status': source['status'],
        'structural_status': structure['status'],
        'source_request_bound': True,
        'source_variant_bound': False,
        'held_out_separated_structurally': True,
        'execution_performed': False, 'outcomes_verified': False,
        'skill_validated': False, 'reviewed': False,
        'activation_authorized': False, 'training_ready': False,
    }).model_dump()


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, _message):
        self.exit(2, 'Site skill rehearsal unavailable: invalid arguments.\n')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Read-only synthetic skill source rehearsal')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--skills', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--skill-sha256', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--snapshot-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = rehearse_site_skill(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            pages=arguments.pages, skills=arguments.skills,
            plan=read_plan_source(arguments.plan), skill_sha256=arguments.skill_sha256,
            plan_sha256=arguments.plan_sha256, snapshot_sha256=arguments.snapshot_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site skill rehearsal unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
