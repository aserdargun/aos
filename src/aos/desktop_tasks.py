import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
import time
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, replace
from typing import Literal

from pydantic import Field

from .computer import ComputerGateway, SafetyPolicy
from .browser import BROWSER_SCOPE, BROWSER_VALUE, BrowserRuntime, DomObservation
from .browser_operator import BrowserOperator
from .contracts import AOSFault, Action, ErrorCode, HELLO_CONTENT, HELLO_PATH, Phase, Settings, State, TypedModel, canonical, digest, identifier, now
from .operator import Operator
from .reusable_decider import ReusableDeciderEngine
from .vision import VISION_SCOPE, VisionRuntime
from .vision_operator import VisionOperator
from .task_sequence import TaskSequences
from .task_progress import task_progress
from .learning_event_stream import poll_learning_stream, ROLES as LEARNING_ROLES


class Approval(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    approval_id: str
    job_id: str
    action: Action
    action_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expires_at: float


@dataclass(frozen=True)
class TaskApprovalGrant:
    grant_id: str
    job_id: str
    kind: str
    session_id: str
    lease_id: str
    generation: int
    parent_runtime_id: str
    expires_at: float
    monotonic_deadline: float
    run_id: str | None = None
    task_id: str | None = None
    runtime_id: str | None = None


class LeasedGateway(ComputerGateway):
    def __init__(self, scheduler, job_id, runtime):
        super().__init__(scheduler.store, runtime)
        self.scheduler = scheduler
        self.job_id = job_id

    def observe(self, state):
        self.scheduler.check_lease(self.job_id)
        self.scheduler.check_failure_followup(self.job_id)
        self.scheduler.check_local_navigation_binding(self.job_id)
        self.scheduler.check_local_staging_binding(self.job_id)
        self.scheduler.check_remote_entry_binding(self.job_id)
        self.scheduler.check_remote_routes_binding(self.job_id)
        self.scheduler.check_remote_static_assets_binding(self.job_id)
        self.scheduler.check_remote_form_binding(self.job_id)
        return super().observe(state)

    def execute(self, action, decision_id):
        self.scheduler.check_lease(self.job_id)
        self.scheduler.check_failure_followup(self.job_id)
        self.scheduler.check_failure_guidance(self.job_id, require_context=True)
        self.scheduler.check_task_knowledge_binding(self.job_id, require_context=True)
        self.scheduler.check_local_navigation_binding(self.job_id)
        self.scheduler.check_local_staging_binding(self.job_id)
        self.scheduler.check_remote_entry_binding(self.job_id)
        self.scheduler.check_remote_routes_binding(self.job_id)
        self.scheduler.check_remote_static_assets_binding(self.job_id)
        self.scheduler.check_remote_form_binding(self.job_id)
        return super().execute(action, decision_id)


class DesktopScheduler:
    def __init__(self, controller, settings: Settings, engine, approval_seconds: float = 60,
                 browser_manifest=None, vision_supervisor=None, desktop_browser: bool = False,
                 desktop_vision: bool = False, desktop_mcp_manifest=None,
                 desktop_navigation_mcp_manifest=None,
                 desktop_staging_mcp_manifest=None,
                 staging_fixture_port: int | None = None,
                 local_navigation_profiles=None, local_navigation_pin=None,
                 local_staging_profiles=None, local_staging_pin=None,
                 remote_entry_mcp_manifest=None, remote_entry_profiles=None,
                 remote_entry_profile_sha256=None, remote_entry_task=None,
                 remote_routes_plan=None, remote_static_assets_plan=None,
                 remote_static_tls_context=None, remote_form_plan=None,
                 remote_route_review_source_database: Path | None = None,
                 remote_route_review_site_store: Path | None = None,
                 remote_route_review_store: Path | None = None,
                 remote_route_review_sha256: str | None = None,
                 remote_json_review_source_database: Path | None = None,
                 remote_json_review_site_store: Path | None = None,
                 remote_json_review_store: Path | None = None,
                 remote_json_review_sha256: str | None = None,
                 remote_form_field_name=None, remote_form_value=None,
                 remote_form_fields=None,
                 remote_form_tls_context=None, remote_form_public_plan_sha256=None,
                 remote_form_state_plan=None, remote_form_public_state_plan_sha256=None,
                 remote_form_cookie=None, remote_form_cookie_sha256=None,
                 remote_form_skill_invocation=None,
                 remote_form_skill_invocation_sha256=None,
                 remote_form_skill_revalidator=None,
                 remote_form_candidate_admission=None,
                 remote_form_owned_target=None,
                 remote_form_owned_fixture=None,
                 remote_form_owned_manifest=None,
                 remote_form_owned_auditor=None,
                 remote_form_owned_candidate_session=None,
                 owned_skill_reuse_workspace_lock=None,
                 synthetic_learning_stream_dir: Path | None = None,
                 remote_learning_consents_dir: Path | None = None,
                 remote_learning_stream_dir: Path | None = None):
        if not 0 < approval_seconds <= 60:
            raise ValueError('Approval duration must be between zero and 60 seconds')
        if type(desktop_browser) is not bool or desktop_browser and browser_manifest is None:
            raise ValueError('Visible desktop browser requires explicit browser task configuration')
        if type(desktop_vision) is not bool or desktop_vision and (not desktop_browser or vision_supervisor is None):
            raise ValueError('Visible desktop vision requires visible browser and vision task configuration')
        if desktop_mcp_manifest is not None and not desktop_browser:
            raise ValueError('MCP browser requires explicit visible desktop browser configuration')
        if desktop_navigation_mcp_manifest is not None:
            if not desktop_browser or desktop_mcp_manifest is not None:
                raise ValueError('Local navigation MCP requires a separate visible browser configuration')
            from .desktop_mcp_bundle import read_bundle

            try:
                read_bundle(desktop_navigation_mcp_manifest)
            except (OSError, ValueError, KeyError) as error:
                raise ValueError('Local navigation MCP package is not pinned') from error
        if desktop_staging_mcp_manifest is not None:
            if not desktop_browser or desktop_mcp_manifest is not None:
                raise ValueError('Synthetic staging MCP requires a separate visible browser configuration')
            from .desktop_mcp_bundle import read_bundle

            try:
                read_bundle(desktop_staging_mcp_manifest)
            except (OSError, ValueError, KeyError) as error:
                raise ValueError('Synthetic staging MCP package is not pinned') from error
        if staging_fixture_port is not None and (
                desktop_staging_mcp_manifest is None or type(staging_fixture_port) is not int
                or not 1024 <= staging_fixture_port <= 65535):
            raise ValueError('Pinned staging fixture port requires explicit staging MCP')
        if (local_navigation_profiles is None) != (local_navigation_pin is None):
            raise ValueError('Synthetic navigation pin requires an explicit profile store')
        if local_navigation_pin is not None and desktop_mcp_manifest is None and desktop_navigation_mcp_manifest is None:
            raise ValueError('Synthetic navigation pin requires the visible MCP runtime')
        if local_navigation_pin is not None:
            from .local_navigation_admission import SyntheticNavigationPin
            from .web_application import WebApplicationProfiles

            if (not isinstance(local_navigation_profiles, WebApplicationProfiles)
                    or not isinstance(local_navigation_pin, SyntheticNavigationPin)):
                raise ValueError('Synthetic navigation requires an immutable profile store')
            local_navigation_pin = SyntheticNavigationPin.model_validate_json(
                local_navigation_pin.model_dump_json())
        if (local_staging_profiles is None) != (local_staging_pin is None):
            raise ValueError('Synthetic staging pin requires an explicit profile store')
        if local_staging_pin is not None:
            from .local_navigation_admission import SyntheticStagingPin
            from .web_application import WebApplicationProfiles

            if (desktop_staging_mcp_manifest is None
                    or not isinstance(local_staging_profiles, WebApplicationProfiles)
                    or not isinstance(local_staging_pin, SyntheticStagingPin)
                    or staging_fixture_port is not None
                    and staging_fixture_port != local_staging_pin.fixture_port):
                raise ValueError('Synthetic staging requires a matching immutable profile pin')
            local_staging_pin = SyntheticStagingPin.model_validate_json(
                local_staging_pin.model_dump_json())
            staging_fixture_port = local_staging_pin.fixture_port
        remote_options = (remote_entry_mcp_manifest, remote_entry_profiles,
                          remote_entry_profile_sha256, remote_entry_task)
        if any(option is not None for option in remote_options):
            from .desktop_mcp_bundle import read_bundle
            from .web_application import WebApplicationProfiles, canonical_origin
            from .web_application_binding import WebTaskContract

            if (not all(option is not None for option in remote_options) or not desktop_browser
                    or not isinstance(remote_entry_profiles, WebApplicationProfiles)
                    or not isinstance(remote_entry_task, WebTaskContract)):
                raise ValueError('Remote entry requires an explicit pinned desktop profile and task')
            try:
                read_bundle(remote_entry_mcp_manifest)
                profile = remote_entry_profiles.get(remote_entry_profile_sha256)
                if (remote_entry_task.profile_sha256 != remote_entry_profile_sha256
                        or remote_entry_task.entry_url != profile.entry_url
                        or remote_entry_task.task_key not in profile.task_keys
                        or not set(remote_entry_task.allowed_origins).issubset(profile.allowed_origins)
                        or profile.environment not in {'staging', 'production'}
                        or canonical_origin(profile.entry_url)[1]
                        or 'browser.navigate' not in remote_entry_task.tools):
                    raise ValueError('Remote entry task differs from its stored profile')
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise ValueError('Remote entry profile or MCP pin is unavailable') from error
        if remote_routes_plan is not None:
            from .web_application_binding import WebReadOnlyRoutePlan, verify_web_readonly_routes

            if (not all(option is not None for option in remote_options)
                    or not isinstance(remote_routes_plan, WebReadOnlyRoutePlan)):
                raise ValueError('Read-only routes require a pinned remote task and plan')
            verify_web_readonly_routes(remote_entry_profiles, remote_entry_task, remote_routes_plan)
        if remote_static_assets_plan is not None:
            from .web_static_assets import WebStaticAssetPlan
            from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_bundle_plan

            if (not all(option is not None for option in remote_options)
                    or not isinstance(remote_static_assets_plan,
                                      (WebStaticAssetPlan, WebReadOnlyDataBundlePlan))):
                raise ValueError('Static assets require a pinned remote task and plan')
            verify_web_bundle_plan(remote_entry_profiles, remote_entry_task,
                                   remote_static_assets_plan)
        if remote_static_tls_context is not None and remote_static_assets_plan is None:
            raise ValueError('Static TLS context requires an exact asset plan')
        review_options = (remote_route_review_source_database,
                          remote_route_review_site_store,
                          remote_route_review_store,
                          remote_route_review_sha256)
        if any(option is not None for option in review_options):
            from .contracts import REPO_ROOT
            from .remote_route_knowledge_review import prepare_live_remote_route_knowledge

            if remote_routes_plan is None or not all(option is not None for option in review_options):
                raise ValueError('Read-only route review requires a complete route task and source')
            for source in review_options[:3]:
                if not Path(source).absolute().is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('Read-only route review sources must remain under private data')
            try:
                prepare_live_remote_route_knowledge(
                    remote_route_review_source_database,
                    profiles=remote_entry_profiles.root,
                    site_store=remote_route_review_site_store,
                    review_store=remote_route_review_store,
                    review_sha256=remote_route_review_sha256,
                    selected_profile_sha256=remote_entry_profile_sha256,
                    selected_plan_sha256=digest(remote_routes_plan.model_dump()))
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                    RecursionError) as error:
                raise ValueError('Read-only route review source is unavailable or stale') from error
        json_review_options = (remote_json_review_source_database,
                               remote_json_review_site_store,
                               remote_json_review_store,
                               remote_json_review_sha256)
        if any(option is not None for option in json_review_options):
            from .contracts import REPO_ROOT
            from .remote_readonly_data_knowledge_review import (
                prepare_live_remote_readonly_data_knowledge)
            from .web_readonly_data import WebReadOnlyDataBundlePlan

            if (not isinstance(remote_static_assets_plan, WebReadOnlyDataBundlePlan)
                    or remote_entry_profiles is None
                    or remote_entry_profile_sha256 is None
                    or not all(option is not None for option in json_review_options)):
                raise ValueError('JSON review requires a complete v2 task and source')
            for source in json_review_options[:3]:
                if not Path(source).absolute().is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('JSON review sources must remain under private data')
            try:
                prepare_live_remote_readonly_data_knowledge(
                    remote_json_review_source_database,
                    profiles=remote_entry_profiles.root,
                    site_store=remote_json_review_site_store,
                    review_store=remote_json_review_store,
                    review_sha256=remote_json_review_sha256,
                    selected_profile_sha256=remote_entry_profile_sha256,
                    selected_plan_sha256=digest(remote_static_assets_plan.model_dump()))
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                    RecursionError) as error:
                raise ValueError('JSON review source is unavailable or stale') from error
        if any(option is not None for option in (remote_form_plan, remote_form_field_name,
                                                  remote_form_value, remote_form_fields,
                                                  remote_form_tls_context,
                                                  remote_form_public_plan_sha256,
                                                  remote_form_owned_target)):
            import hashlib
            import ssl
            from .web_https_form_transport import (WebHTTPSFormPlan,
                                                   exact_form_fields, form_body,
                                                   verify_web_https_form_plan,
                                                   verify_web_https_form_target_grant)

            if (not all(option is not None for option in remote_options)
                    or not isinstance(remote_form_plan, WebHTTPSFormPlan)
                    or (remote_form_owned_target is None
                        and not isinstance(remote_form_tls_context, ssl.SSLContext))
                    or remote_form_owned_target is not None
                    and remote_form_tls_context is not remote_form_owned_target.tls_context
                    or remote_form_tls_context is not None
                    and (remote_form_tls_context.verify_mode != ssl.CERT_REQUIRED
                         or not remote_form_tls_context.check_hostname)):
                raise ValueError('HTTPS form requires a pinned task and verified TLS')
            form_fields = exact_form_fields(remote_form_field_name, remote_form_value,
                                            remote_form_fields)
            verify_web_https_form_plan(remote_entry_profiles, remote_entry_task, remote_form_plan)
            verify_web_https_form_target_grant(
                remote_form_plan, remote_form_public_plan_sha256, remote_form_owned_target)
            if remote_form_owned_target is not None and (
                    remote_form_public_plan_sha256 is not None or remote_form_cookie is not None
                    or remote_form_cookie_sha256 is not None):
                raise ValueError('Owned HTTPS fixture forbids public grants and cookies')
            body = form_body(form_fields)
            if (hashlib.sha256(body).hexdigest() != remote_form_plan.body_sha256
                    or len(body) != remote_form_plan.body_bytes):
                raise ValueError('HTTPS form body or public grant differs')
        if remote_form_state_plan is not None or remote_form_public_state_plan_sha256 is not None:
            from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                                     verify_web_https_form_state_plan)

            if (remote_form_plan is None
                    or not isinstance(remote_form_state_plan, WebHTTPSFormStatePlan)):
                raise ValueError('HTTPS form state requires an exact form and state plan')
            verify_web_https_form_state_plan(
                remote_entry_profiles, remote_entry_task, remote_form_plan,
                remote_form_state_plan, remote_form_tls_context,
                confirm_public_form_plan_sha256=remote_form_public_plan_sha256,
                confirm_public_state_plan_sha256=remote_form_public_state_plan_sha256,
                owned_form_target=remote_form_owned_target)
        if remote_form_cookie is not None or remote_form_cookie_sha256 is not None:
            import hashlib
            from .web_https_preflight import validate_https_cookie_header

            if (remote_form_plan is None or remote_form_public_plan_sha256 is None
                    or remote_form_cookie is None or remote_form_cookie_sha256 is None
                    or hashlib.sha256(validate_https_cookie_header(
                        remote_form_cookie).encode('ascii')).hexdigest()
                    != remote_form_cookie_sha256):
                raise ValueError('HTTPS form cookie requires a public exact form and hash')
        if remote_form_skill_invocation is not None or remote_form_skill_invocation_sha256 is not None:
            from .site_skill_form_invocation import verify_site_skill_form_invocation_input

            if (remote_form_plan is None or remote_form_state_plan is None
                    or remote_form_skill_invocation is None
                    or remote_form_skill_invocation_sha256 is None
                    or not callable(remote_form_skill_revalidator)):
                raise ValueError('Typed skill invocation requires exact form and state plans')
            invocation = verify_site_skill_form_invocation_input(
                remote_form_skill_invocation,
                skill_sha256=remote_form_skill_invocation.get('skill_sha256'),
                task=remote_entry_task, form_plan=remote_form_plan,
                state_plan=remote_form_state_plan)
            if digest(invocation.model_dump(mode='json')) != remote_form_skill_invocation_sha256:
                raise ValueError('Typed skill invocation exact hash differs')
            remote_form_skill_invocation = invocation.model_dump(mode='json')
        if remote_form_candidate_admission is not None:
            from .site_skill_form_recipe_candidate_execution import verify_candidate_admission

            remote_form_candidate_admission = verify_candidate_admission(
                remote_form_candidate_admission, remote_form_skill_invocation)
        self.controller = controller
        self.store = controller.store
        self.settings = settings
        self._failure_improvements = None
        self._failure_followups = None
        self._failure_followup_jobs = {}
        self._failure_guidance = None
        self._failure_guidance_jobs = {}
        self.task_knowledge = None
        self._task_knowledge_jobs = {}
        self._hello_guidance_reuse = None
        self._hello_guidance_reuse_jobs = {}
        self.engine = engine
        self.browser_manifest = browser_manifest
        self.vision_supervisor = vision_supervisor
        self.desktop_browser = desktop_browser
        self.desktop_mcp_manifest = desktop_mcp_manifest
        self.desktop_navigation_mcp_manifest = desktop_navigation_mcp_manifest
        self.desktop_staging_mcp_manifest = desktop_staging_mcp_manifest
        self.staging_fixture_port = staging_fixture_port
        self.local_navigation_profiles = local_navigation_profiles
        self.local_navigation_pin = local_navigation_pin
        self.local_staging_profiles = local_staging_profiles
        self.local_staging_pin = local_staging_pin
        self.remote_entry_mcp_manifest = remote_entry_mcp_manifest
        self.remote_entry_profiles = remote_entry_profiles
        self.remote_entry_profile_sha256 = remote_entry_profile_sha256
        self.remote_entry_task = remote_entry_task
        self.remote_routes_plan = remote_routes_plan
        self.remote_static_assets_plan = remote_static_assets_plan
        self.remote_static_tls_context = remote_static_tls_context
        self.remote_route_review_source = ({
            'database': Path(remote_route_review_source_database).absolute(),
            'site_store': Path(remote_route_review_site_store).absolute(),
            'review_store': Path(remote_route_review_store).absolute(),
            'review_sha256': remote_route_review_sha256}
            if remote_route_review_sha256 is not None else None)
        self.remote_json_review_source = ({
            'database': Path(remote_json_review_source_database).absolute(),
            'site_store': Path(remote_json_review_site_store).absolute(),
            'review_store': Path(remote_json_review_store).absolute(),
            'review_sha256': remote_json_review_sha256}
            if remote_json_review_sha256 is not None else None)
        self.remote_form_plan = remote_form_plan
        self.remote_form_field_name = remote_form_field_name
        self.remote_form_value = remote_form_value
        self.remote_form_fields = form_fields if remote_form_plan is not None else None
        self.remote_form_tls_context = remote_form_tls_context
        self.remote_form_public_plan_sha256 = remote_form_public_plan_sha256
        self.remote_form_state_plan = remote_form_state_plan
        self.remote_form_public_state_plan_sha256 = remote_form_public_state_plan_sha256
        self.remote_form_cookie = remote_form_cookie
        self.remote_form_cookie_sha256 = remote_form_cookie_sha256
        self.remote_form_owned_target = remote_form_owned_target
        self.remote_form_owned_fixture = remote_form_owned_fixture
        self.remote_form_owned_manifest = remote_form_owned_manifest
        self.remote_form_owned_auditor = remote_form_owned_auditor
        self.remote_form_owned_candidate_session = remote_form_owned_candidate_session
        self.owned_skill_reuse_workspace_lock = owned_skill_reuse_workspace_lock
        self._owned_form_lifecycle = 'ready' if remote_form_owned_fixture is not None else None
        self._owned_form_run_id = None
        self._owned_form_source_job_id = None
        self._owned_form_audit = None
        self._owned_skill_reuse = None
        self._owned_skill_reuse_admission_sha256 = None
        self._owned_skill_reuse_admission_file = None
        self.owned_skill_planning = None
        self.knowledge_answer = None
        self._owned_planning_start = None
        self._owned_candidate_execution = None
        self._active_owned_candidate_execution = None
        self._owned_candidate_execution_history = {}
        self._owned_candidate_execution_consumed = set()
        self._owned_selected_candidate_execution_consumed = set()
        self._owned_candidate_execution_session_starts = 0
        self._owned_selected_candidate_execution_session_starts = 0
        self._owned_candidate_execution_restore = None
        if (remote_form_owned_fixture is not None
                and (remote_form_owned_target is None
                     or remote_form_owned_fixture.target is not remote_form_owned_target
                     or not callable(remote_form_owned_auditor)
                     or remote_form_owned_manifest is None)):
            raise ValueError('Owned form session requires its fixture, target, manifest and auditor')
        if (remote_form_owned_candidate_session is not None
                and (remote_form_owned_fixture is None
                     or not isinstance(remote_form_owned_manifest, dict)
                     or remote_form_owned_manifest.get('mode') != 'owned_synthetic_form_invocation')):
            raise ValueError('Owned recipe candidate requires a fixed-template v1 source session')
        self.remote_form_skill_invocation = remote_form_skill_invocation
        self.remote_form_skill_invocation_sha256 = remote_form_skill_invocation_sha256
        self.remote_form_skill_revalidator = remote_form_skill_revalidator
        self.remote_form_candidate_admission = remote_form_candidate_admission
        self.synthetic_learning_stream_dir = Path(synthetic_learning_stream_dir) if synthetic_learning_stream_dir is not None else None
        if (self.synthetic_learning_stream_dir is not None
                and (self.synthetic_learning_stream_dir.absolute() == settings.workspace.absolute()
                     or settings.workspace.absolute() in self.synthetic_learning_stream_dir.absolute().parents)):
            raise ValueError('Synthetic learning outbox must be outside the agent workspace')
        if (remote_learning_consents_dir is None) != (remote_learning_stream_dir is None):
            raise ValueError('Remote learning requires both private stores')
        if (remote_learning_consents_dir is not None
                and remote_routes_plan is None and remote_static_assets_plan is None
                and (remote_form_plan is None or remote_form_cookie is not None)):
            raise ValueError('Remote learning requires an exact supported web plan')
        self.remote_learning_consents_dir = (Path(remote_learning_consents_dir)
                                             if remote_learning_consents_dir is not None else None)
        self.remote_learning_stream_dir = (Path(remote_learning_stream_dir)
                                           if remote_learning_stream_dir is not None else None)
        if any(path.absolute() == settings.workspace.absolute()
               or settings.workspace.absolute() in path.absolute().parents
               for path in (self.remote_learning_consents_dir, self.remote_learning_stream_dir)
               if path is not None):
            raise ValueError('Remote learning stores must be outside the agent workspace')
        self._learning_opted_jobs: set[str] = set()
        self._learning_failed_jobs: set[str] = set()
        self._learning_unpersisted: dict[str, dict] = {}
        self._remote_learning_attached: dict[str, str] = {}
        self._remote_learning_failed: set[str] = set()
        self._remote_learning_unpersisted: dict[str, dict] = {}
        self._local_navigation_runtime_id = None
        self._local_staging_runtime_id = None
        self._remote_entry_runtime_id = None
        self._remote_entry_draft = None
        self._remote_routes_runtime_id = None
        self._remote_routes_draft = None
        self._remote_static_assets_runtime_id = None
        self._remote_static_assets_draft = None
        self._remote_form_runtime_id = None
        self._remote_form_draft = None
        self.desktop_vision = desktop_vision
        self.completed_runtime = None
        self.active_runtime = None
        self.approval_seconds = approval_seconds
        self.task: asyncio.Task | None = None
        self.job_id: str | None = None
        self.answer: asyncio.Future | None = None
        self.closed = False
        self.restart_quiesced = False
        self.operator = None
        self.pause_requested = False
        self._approval_grant: TaskApprovalGrant | None = None
        self._grant_uses = 0
        self.sequences = TaskSequences(self)

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    @property
    def paused(self):
        row = self.store.connection.execute('SELECT status FROM desktop_tasks WHERE job_id=?', (self.job_id,)).fetchone()
        return bool(row and row['status'] == 'paused')

    @property
    def reserved(self):
        return self.busy or self.paused or self.sequences.reserved or self.planning_reserved or self.adaptation_reserved

    @property
    def adaptation_reserved(self):
        learning = self.owned_episode_learning
        return learning is not None and learning.adaptation.reserved

    @property
    def planning_reserved(self):
        knowledge_answer = getattr(self, 'knowledge_answer', None)
        return (self.owned_skill_planning is not None and self.owned_skill_planning.reserved
                or knowledge_answer is not None and knowledge_answer.reserved)

    def configure_knowledge_answer(self, knowledge, answerer, directory):
        from .knowledge_answer import KnowledgeAnswerService
        from .desktop import DesktopRuntime
        from .session_binding import runtime_binding

        if self.knowledge_answer is not None or self.reserved or self.closed or self.restart_quiesced:
            raise ValueError('knowledge_answer_configuration_invalid')
        runtime = self.controller.runtime
        initial = self.controller.state()
        binding = (runtime_binding(runtime, self.controller.session_id, 0).model_dump()
                   if isinstance(runtime, DesktopRuntime) else None)

        def authority(lease_id, generation):
            control = self.controller.state()
            if (self.closed or self.restart_quiesced or self.busy or self.paused or self.sequences.reserved
                    or self.adaptation_reserved
                    or self.owned_skill_planning is not None and self.owned_skill_planning.reserved
                    or control['owner'] != 'AGENT' or control['status'] != 'running'
                    or control['lease_id'] != lease_id or control['generation'] != generation
                    or control['session_id'] != initial['session_id']
                    or control['runtime_id'] != initial['runtime_id'] or control['image_id'] != initial['image_id']
                    or self.controller.runtime is not runtime or not runtime.status()['running']):
                raise ValueError('knowledge_answer_control_changed')
            if binding is not None and runtime_binding(runtime, self.controller.session_id, 0).model_dump() != binding:
                raise ValueError('knowledge_answer_runtime_changed')
            return {'session_id': self.controller.session_id, 'lease_id': lease_id, 'generation': generation}

        async def yield_gpu():
            if isinstance(self.engine, ReusableDeciderEngine):
                await self.engine.close()

        self.knowledge_answer = KnowledgeAnswerService(knowledge, answerer, directory, authority, yield_gpu)

    def configure_task_knowledge(self, knowledge, directory):
        from .task_knowledge import TaskKnowledgeService

        if self.task_knowledge is not None or self.reserved or self.closed or self.restart_quiesced:
            raise ValueError('task_knowledge_configuration_invalid')
        self.task_knowledge = TaskKnowledgeService(self, knowledge, directory)

    def preview_task_knowledge(self, **arguments):
        if self.task_knowledge is None or self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('task_knowledge_requires_idle_session')
        return self.task_knowledge.preview(**arguments)

    def start_task_knowledge(self, preview, confirm_sha256, consent, lease_id, generation):
        if self.task_knowledge is None or self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('task_knowledge_requires_idle_session')
        committed = self.task_knowledge.commit(preview, confirm_sha256, consent, lease_id, generation)
        started = self.start(lease_id, generation, preview['target']['task_kind'],
                             task_knowledge=committed['intent_sha256'])
        return {'schema_version': '1.0', 'kind': 'task_knowledge_start', 'job_id': started['job_id'],
                'intent_sha256': committed['intent_sha256'], 'manual_approval_required': True,
                'untrusted': True, 'execution_authorized': False, 'training_ready': False, 'gold': False,
                'causality_verified': False, 'scope_authorization_verified': False}

    def check_task_knowledge_binding(self, job_id, require_context=False):
        binding = getattr(self, '_task_knowledge_jobs', {}).get(job_id)
        if binding is None:
            return
        try:
            job = self.store.connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            self.task_knowledge.guard(self.store.connection, binding['intent_sha256'], job_id,
                                      self.failure_followup_target(job['kind']), require_context=require_context)
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task knowledge source or authorization changed') from error

    def configure_owned_skill_planning(self, planner):
        from .owned_skill_planner import BonsaiOwnedSkillPlanner
        from .owned_skill_planning import OwnedSkillPlanning
        from .owned_episode_service import OwnedEpisodeLearning

        if (self._owned_skill_reuse is None or self.owned_skill_planning is not None
                or not isinstance(planner, BonsaiOwnedSkillPlanner) or self.reserved):
            raise ValueError('owned_skill_planning_configuration_invalid')
        self._assert_owned_skill_reuse_current()

        async def yield_gpu():
            if isinstance(self.engine, ReusableDeciderEngine):
                await self.engine.close()

        learning = OwnedEpisodeLearning(self, self._owned_skill_reuse_admission_file.parent / 'owned-episodes')
        self.owned_skill_planning = OwnedSkillPlanning(
            planner, self._owned_skill_reuse_admission_file.parent / 'owned-skill-plans',
            self._prepare_owned_skill_plan, yield_gpu, before_begin=learning.begin, on_proposal=learning.proposed)
        self.owned_skill_planning.episode_learning = learning

    def configure_owned_skill_knowledge(self, knowledge):
        from .owned_skill_knowledge_service import OwnedSkillKnowledgeService

        if self.owned_skill_planning is None or self.owned_skill_planning.knowledge is not None:
            raise ValueError('owned_skill_knowledge_configuration_invalid')
        self.owned_skill_planning.knowledge = OwnedSkillKnowledgeService(self, knowledge)

    @property
    def owned_episode_learning(self):
        return getattr(self.owned_skill_planning, 'episode_learning', None)

    @property
    def failure_improvements(self):
        from .failure_improvement import FailureImprovementService

        if self._failure_improvements is None:
            self._failure_improvements = FailureImprovementService(
                self.settings.database, self.settings.database.parent / 'failure-improvements',
                self.controller.session_id)
        return self._failure_improvements

    @property
    def failure_followups(self):
        from .failure_followup import FailureFollowupService

        if self._failure_followups is None:
            self._failure_followups = FailureFollowupService(self.failure_improvements)
        return self._failure_followups

    def failure_followup_target(self, kind):
        if kind not in self.kinds():
            raise ValueError('failure_followup_task_unavailable')
        desktop = self.controller.state()
        session = self.store.connection.execute(
            'SELECT image_id FROM desktop_sessions WHERE session_id=?',
            (self.controller.session_id,)).fetchone()
        if session is None:
            raise ValueError('failure_followup_session_unavailable')
        supervisor = self.vision_supervisor if kind == 'vision_canvas' else None
        configuration = {
            'settings': self.settings.model_dump(mode='json'),
            'engine': self.engine.identity,
            'supervisor': supervisor.identity if supervisor is not None else None,
            'runtime_pins': self.controller.runtime.pins,
            'desktop_browser': self.desktop_browser,
            'desktop_vision': self.desktop_vision,
        }
        for name in ('local_navigation_pin', 'local_staging_pin', 'remote_entry_task',
                     'remote_routes_plan', 'remote_static_assets_plan', 'remote_form_plan',
                     'remote_form_state_plan', 'remote_form_skill_invocation'):
            value = getattr(self, name, None)
            configuration[name] = value.model_dump(mode='json') if value is not None else None
        for name in ('remote_entry_profile_sha256', 'remote_form_field_name', 'remote_form_value',
                     'remote_form_fields', 'remote_form_public_plan_sha256',
                     'remote_form_public_state_plan_sha256', 'remote_form_cookie_sha256',
                     'remote_form_skill_invocation_sha256', 'staging_fixture_port'):
            configuration[name] = getattr(self, name, None)
        manifest_names = (() if kind == 'hello' else
                          ('browser_manifest', 'desktop_mcp_manifest', 'desktop_navigation_mcp_manifest',
                           'desktop_staging_mcp_manifest', 'remote_entry_mcp_manifest'))
        for name in manifest_names:
            manifest = getattr(self, name, None)
            if manifest is None:
                configuration[name] = None
            else:
                descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as source:
                    before = os.fstat(source.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                            or before.st_size > 1024 * 1024):
                        raise ValueError('failure_followup_manifest_unavailable')
                    content = source.read(1024 * 1024 + 1)
                    after = os.fstat(source.fileno())
                linked = os.stat(manifest, follow_symlinks=False)
                fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                if (len(content) != before.st_size
                        or any(getattr(before, key) != getattr(after, key)
                               or getattr(before, key) != getattr(linked, key) for key in fields)):
                    raise ValueError('failure_followup_manifest_unavailable')
                configuration[name] = {'path': str(manifest), 'sha256': hashlib.sha256(content).hexdigest()}
        return {
            'session_id': self.controller.session_id, 'parent_runtime_id': self.controller.runtime.runtime_id,
            'image_id': session['image_id'], 'lease_id': desktop['lease_id'], 'generation': desktop['generation'],
            'task_kind': kind, 'configuration_sha256': digest(configuration),
            'system1_deployment_id': self.engine.identity['deployment_id'],
            'system2_deployment_id': supervisor.identity['deployment_id'] if supervisor is not None else None,
        }

    def preview_failure_followup(self, source_job_id, candidate_sha256, receipt_sha256):
        if self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('failure_followup_requires_idle_session')
        desktop = self.controller.state()
        if desktop['owner'] != 'AGENT' or desktop['status'] != 'running':
            raise ValueError('failure_followup_requires_agent_control')
        source = self.store.connection.execute(
            'SELECT kind FROM desktop_tasks WHERE job_id=? AND session_id=?',
            (source_job_id, self.controller.session_id)).fetchone()
        if source is None:
            raise ValueError('failure_followup_source_unavailable')
        return self.failure_followups.preview(source_job_id, candidate_sha256, receipt_sha256,
                                             self.failure_followup_target(source['kind']))

    def start_failure_followup(self, preview, confirm_sha256, consent, lease_id, generation):
        if self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('failure_followup_requires_idle_session')
        desktop = self.controller.state()
        if (desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or type(lease_id) is not str or desktop['lease_id'] != lease_id
                or type(generation) is not int or desktop['generation'] != generation):
            raise ValueError('failure_followup_requires_current_control')
        target = self.failure_followup_target(preview['target']['task_kind'])
        committed = self.failure_followups.commit(preview, confirm_sha256, consent, target)
        started = self.start(lease_id, generation, target['task_kind'],
                             failure_followup=committed['intent_sha256'])
        return {'schema_version': '1.0', 'job_id': started['job_id'],
                'intent_sha256': committed['intent_sha256'], 'manual_approval_required': True}

    def check_failure_followup(self, job_id):
        binding = self._failure_followup_jobs.get(job_id)
        if binding is None:
            return
        job = self.store.connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?',
                                            (job_id,)).fetchone()
        try:
            if job is None:
                raise ValueError('failure_followup_job_missing')
            self.failure_followups.guard(self.store.connection, binding['intent_sha256'], job_id,
                                         self.failure_followup_target(job['kind']))
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Follow-up evidence or admission changed') from error
        self.check_failure_guidance(job_id)

    @property
    def failure_guidance(self):
        from .failure_guidance import FailureGuidanceService

        if self._failure_guidance is None:
            self._failure_guidance = FailureGuidanceService(self.failure_followups)
        return self._failure_guidance

    def preview_failure_guidance(self, source_job_id, candidate_sha256, receipt_sha256):
        followup = self.preview_failure_followup(source_job_id, candidate_sha256, receipt_sha256)
        return self.failure_guidance.preview(followup)

    def start_failure_guidance(self, preview, confirm_sha256, consent, lease_id, generation):
        if self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('failure_guidance_requires_idle_session')
        desktop = self.controller.state()
        if (desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or type(lease_id) is not str or desktop['lease_id'] != lease_id
                or type(generation) is not int or desktop['generation'] != generation):
            raise ValueError('failure_guidance_requires_current_control')
        target = self.failure_followup_target('hello')
        committed = self.failure_guidance.commit(preview, confirm_sha256, consent, target)
        started = self.start(lease_id, generation, 'hello', failure_followup=committed['intent_sha256'],
                             failure_guidance=committed['guidance_intent_sha256'])
        return {'schema_version': '1.0', 'job_id': started['job_id'], **committed,
                'manual_approval_required': True}

    def check_failure_guidance(self, job_id, require_context=False):
        binding = self._failure_guidance_jobs.get(job_id)
        if binding is None:
            return
        try:
            self.failure_guidance.guard(self.store.connection, binding['guidance_intent_sha256'], job_id,
                                         self.failure_followup_target('hello'), require_context)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reviewed guidance evidence or authority changed') from error
        self.check_hello_guidance_reuse(job_id)

    @property
    def hello_guidance_reuse(self):
        from .hello_guidance_reuse import HelloGuidanceReuseService

        if self._hello_guidance_reuse is None:
            self._hello_guidance_reuse = HelloGuidanceReuseService(self)
        return self._hello_guidance_reuse

    def hello_reuse_control(self, lease_id, generation):
        if self.closed or self.restart_quiesced or self.reserved:
            raise ValueError('hello_reuse_requires_idle_session')
        current = self.controller.state()
        if (current['owner'] != 'AGENT' or current['status'] != 'running'
                or type(lease_id) is not str or current['lease_id'] != lease_id
                or type(generation) is not int or current['generation'] != generation):
            raise ValueError('hello_reuse_requires_current_control')

    def publish_hello_guidance_reuse(self, preview, confirm_sha256, consent, lease_id, generation):
        self.hello_reuse_control(lease_id, generation)
        return self.hello_guidance_reuse.publish(preview, confirm_sha256, consent)

    def start_hello_guidance_reuse(self, preview, confirm_sha256, consent, lease_id, generation):
        self.hello_reuse_control(lease_id, generation)
        committed = self.hello_guidance_reuse.commit(preview, confirm_sha256, consent,
                                                   self.failure_followup_target('hello'))
        started = self.start(lease_id, generation, 'hello', failure_followup=committed['intent_sha256'],
            failure_guidance=committed['guidance_intent_sha256'], hello_guidance_reuse=committed['reuse_intent_sha256'])
        return {'schema_version': '1.0', 'job_id': started['job_id'], **committed, 'manual_approval_required': True}

    def check_hello_guidance_reuse(self, job_id):
        binding = self._hello_guidance_reuse_jobs.get(job_id)
        if binding is None:
            return
        try:
            self.hello_guidance_reuse.guard(self.store.connection, binding['reuse_intent_sha256'], job_id,
                                           self.failure_followup_target('hello'))
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reusable guidance source or workspace scope changed') from error

    def _prepare_owned_skill_plan(self, case_key, development_value, lease_id, generation):
        desktop = self.controller.state()
        if (self.closed or self.restart_quiesced or self.busy or self.paused
                or self.sequences.reserved or desktop['owner'] != 'AGENT'
                or desktop['status'] != 'running' or desktop['lease_id'] != lease_id
                or desktop['generation'] != generation):
            raise ValueError('owned_skill_planning_control_changed')
        admission = self._assert_owned_skill_reuse_current()
        preview = admission['preview']
        authority = {key: admission[key] for key in (
            'manager_session', 'desktop_session_id', 'runtime_id', 'lease_id', 'generation')}
        authority.update({key: preview[key] for key in (
            'source_manifest_sha256', 'source_run_ref', 'source_invocation_sha256',
            'source_fingerprint_sha256', 'family_sha256', 'release_sha256',
            'selection_sha256', 'review_sha256', 'candidate_sha256', 'recipe_sha256')})
        authority['reuse_admission_sha256'] = self._owned_skill_reuse_admission_sha256
        if case_key is None:
            return authority, []
        from .owned_form_candidate_execution import prepare_candidate_form_run

        context = self._owned_skill_reuse['context']
        session = context['candidate_session']
        candidate, source = session.execution_source(
            context['source_run_id'], preview['source_invocation_sha256'],
            preview['candidate_sha256'])
        prepared = prepare_candidate_form_run(
            candidate, preview['candidate_sha256'], source, case_key, development_value)
        prepared.update({'session': session, 'source': source})
        self._assert_owned_skill_reuse_candidate_binding(
            prepared, preview['review_sha256'], preview['release_sha256'],
            preview['selection_sha256'])
        candidate = prepared['candidate']
        binding = candidate['field_bindings'][0]
        evidence = [{'id': 'admitted-skill', 'skill_key': candidate['skill']['skill_key'],
                     'parameter_key': binding['parameter_key'],
                     'form_field_name': binding['form_field_name'],
                     'ordered_steps': prepared['steps'], 'requested_case_key': case_key,
                     'requested_value': development_value}]
        return authority, evidence

    def begin_owned_skill_plan(self, goal, lease_id, generation, *, collect_learning=False):
        if self.owned_skill_planning is None or self.reserved:
            raise ValueError('owned_skill_planning_unavailable')
        return self.owned_skill_planning.begin(goal, lease_id, generation, collect_learning=collect_learning)

    def owned_skill_plan_status(self):
        return (self.owned_skill_planning.status() if self.owned_skill_planning is not None
                else {'available': False, 'status': 'unavailable', 'execution_authorized': False})

    def _planning_selection(self, planning_bundle_sha256, lease_id, generation):
        if self.owned_skill_planning is None or self.reserved:
            raise ValueError('owned_skill_planning_unavailable')
        bundle = self.owned_skill_planning.select(planning_bundle_sha256, lease_id, generation)
        authority, request = bundle['authority'], bundle['request']
        arguments = {key: authority[key] for key in (
            'candidate_sha256', 'source_run_ref', 'review_sha256', 'release_sha256',
            'selection_sha256', 'reuse_admission_sha256')}
        arguments.update({'invocation_sha256': authority['source_invocation_sha256'],
                          'case_key': request['case_key'],
                          'development_value': bundle['model_response']['parameter_value'],
                          'planning_bundle_sha256': planning_bundle_sha256})
        return bundle, arguments

    def bind_owned_skill_plan(self, planning_bundle_sha256, confirm_plan_sha256,
                             lease_id, generation):
        if planning_bundle_sha256 != confirm_plan_sha256:
            raise ValueError('owned_skill_plan_confirmation_required')
        _bundle, arguments = self._planning_selection(planning_bundle_sha256, lease_id, generation)
        preview = self.preview_owned_form_candidate_execution(**arguments)
        self.owned_skill_planning.bind(planning_bundle_sha256, preview['preview_sha256'])
        return preview

    def start_owned_skill_plan(self, planning_bundle_sha256, confirm_plan_sha256,
                              preview_sha256, confirm_sha256, lease_id, generation):
        if planning_bundle_sha256 != confirm_plan_sha256 or preview_sha256 != confirm_sha256:
            raise ValueError('owned_skill_plan_confirmation_required')
        bundle, arguments = self._planning_selection(planning_bundle_sha256, lease_id, generation)
        self.owned_skill_planning.consume(planning_bundle_sha256, preview_sha256)
        self._owned_planning_start = (planning_bundle_sha256, preview_sha256, lease_id, generation)
        try:
            return self.start_owned_form_candidate_execution(
                **arguments, planning_bundle=bundle, preview_sha256=preview_sha256,
                confirm_sha256=confirm_sha256, lease_id=lease_id, generation=generation)
        finally:
            self._owned_planning_start = None

    def _assert_manual_planning_boundary(self, planning_bundle_sha256):
        if (planning_bundle_sha256 is None and self.owned_skill_planning is not None
                and self.owned_skill_planning.status()['status'] == 'bound'):
            raise ValueError('owned_skill_plan_discard_required_for_manual_execution')

    async def cancel_owned_skill_plan(self):
        if self.knowledge_answer is not None:
            await self.knowledge_answer.cancel()
        if self.owned_skill_planning is not None:
            await self.owned_skill_planning.cancel()
            if self.owned_episode_learning is not None:
                await self.owned_episode_learning.preparation.cancel()
                await self.owned_episode_learning.adaptation.cancel()

    def quiesce_for_restart(self, expected_session_id: str):
        desktop = self.controller.state()
        pending = self.store.connection.execute('''SELECT 1 FROM desktop_approvals
            JOIN desktop_tasks USING(job_id) WHERE session_id=?
            AND desktop_approvals.status IN ('pending','approved') LIMIT 1''',
            (self.controller.session_id,)).fetchone()
        unfinished = self.store.connection.execute('''SELECT 1 FROM desktop_tasks
            WHERE session_id=? AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1''',
            (self.controller.session_id,)).fetchone()
        input_pending = self.store.connection.execute('''SELECT 1 FROM desktop_inputs
            WHERE session_id=? AND status IN ('queued','running') LIMIT 1''',
            (self.controller.session_id,)).fetchone()
        if (expected_session_id != self.controller.session_id
                or self.closed or self.restart_quiesced or self.reserved
                or self._approval_grant is not None or pending or unfinished or input_pending
                or desktop['owner'] != 'AGENT' or desktop['status'] != 'running'):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Restart requires an idle agent session')
        self.restart_quiesced = True
        return {'quiesced': True, 'session_id': self.controller.session_id}

    def release_restart_quiesce(self, expected_session_id: str):
        if expected_session_id != self.controller.session_id or not self.restart_quiesced:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'No restart admission quiesce is active')
        self.restart_quiesced = False
        return {'quiesced': False, 'session_id': self.controller.session_id}

    def check_lease(self, job_id):
        self.check_task_knowledge_binding(job_id)
        desktop = self.controller.state()
        job = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if (not job or desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or desktop['lease_id'] != job['lease_id'] or desktop['generation'] != job['generation']
                or desktop['runtime_id'] != self.controller.runtime.runtime_id
                or (job['runtime_id'] is not None and (
                    self.active_runtime is None or job['runtime_id'] != self.active_runtime.runtime_id))):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Scheduled task lost desktop ownership')

    def check_local_navigation_binding(self, job_id):
        if self.local_navigation_pin is None:
            return
        job = self.store.connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_local_navigation':
            if self._local_navigation_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic navigation job kind changed')
            return
        from .local_navigation_admission import check_synthetic_navigation_runtime

        try:
            report = check_synthetic_navigation_runtime(
                self.local_navigation_profiles, self.local_navigation_pin, self.active_runtime)
            if (job_id != self.job_id or self._local_navigation_runtime_id is None
                    or report['browser_runtime_id'] != self._local_navigation_runtime_id):
                raise ValueError('synthetic_navigation_runtime_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic navigation runtime binding changed') from error

    def check_local_staging_binding(self, job_id):
        if self.local_staging_pin is None:
            return
        job = self.store.connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_staging_workflow':
            if self._local_staging_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging job kind changed')
            return
        from .local_navigation_admission import check_synthetic_staging_runtime

        try:
            report = check_synthetic_staging_runtime(
                self.local_staging_profiles, self.local_staging_pin, self.active_runtime)
            if (job_id != self.job_id or self._local_staging_runtime_id is None
                    or report['browser_runtime_id'] != self._local_staging_runtime_id):
                raise ValueError('synthetic_staging_runtime_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging runtime binding changed') from error

    def check_remote_entry_binding(self, job_id):
        if self.remote_entry_task is None:
            return
        job = self.store.connection.execute('SELECT kind,run_id,runtime_id FROM desktop_tasks WHERE job_id=?',
                                            (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_remote_entry':
            if self._remote_entry_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry job kind changed')
            return
        from .web_application_binding import verify_web_task_binding

        try:
            draft = self._remote_entry_draft
            binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_entry_bindings WHERE job_id=?', (job_id,)).fetchone()
            if (draft is None or binding is None or self.active_runtime is None
                    or job_id != self.job_id or job['runtime_id'] != self._remote_entry_runtime_id
                    or self.active_runtime.runtime_id != self._remote_entry_runtime_id
                    or self.active_runtime.runtime_pin() != draft.runtime
                    or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                    or binding['run_id'] != job['run_id']
                    or binding['browser_runtime_id'] != self._remote_entry_runtime_id
                    or binding['profile_sha256'] != draft.profile_sha256
                    or binding['binding_sha256'] != draft.binding_sha256
                    or binding['runtime_sha256'] != draft.runtime_sha256
                    or binding['draft_json'] != canonical(draft.model_dump(mode='json'))):
                raise ValueError('remote_entry_binding_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry runtime or task binding changed') from error

    def check_remote_entry_binding_preinsert(self, job_id, state):
        from .web_application_binding import verify_web_task_binding

        draft = self._remote_entry_draft
        if (job_id != self.job_id or draft is None or self.active_runtime is None
                or self._remote_entry_runtime_id != state.runtime_id
                or self.active_runtime.runtime_id != state.runtime_id
                or self.active_runtime.runtime_pin() != draft.runtime
                or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                or draft.task != self.remote_entry_task
                or draft.profile_sha256 != self.remote_entry_profile_sha256):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry run cannot bind its live runtime')

    def check_remote_routes_binding(self, job_id):
        if self.remote_routes_plan is None:
            return
        job = self.store.connection.execute('SELECT kind,run_id,runtime_id FROM desktop_tasks WHERE job_id=?',
                                            (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_remote_routes':
            if self._remote_routes_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only route job kind changed')
            return
        from .web_application_binding import verify_web_readonly_routes, verify_web_task_binding

        try:
            draft = self._remote_routes_draft
            binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_route_bindings WHERE job_id=?', (job_id,)).fetchone()
            plan = verify_web_readonly_routes(self.remote_entry_profiles, self.remote_entry_task,
                                              self.remote_routes_plan)
            if (draft is None or binding is None or self.active_runtime is None
                    or job_id != self.job_id or job['runtime_id'] != self._remote_routes_runtime_id
                    or self.active_runtime.runtime_id != self._remote_routes_runtime_id
                    or self.active_runtime.runtime_pin() != draft.runtime
                    or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                    or draft.task != self.remote_entry_task
                    or binding['run_id'] != job['run_id']
                    or binding['browser_runtime_id'] != self._remote_routes_runtime_id
                    or binding['profile_sha256'] != draft.profile_sha256
                    or binding['binding_sha256'] != draft.binding_sha256
                    or binding['runtime_sha256'] != draft.runtime_sha256
                    or binding['plan_sha256'] != digest(plan.model_dump())
                    or binding['draft_json'] != canonical(draft.model_dump(mode='json'))
                    or binding['plan_json'] != canonical(plan.model_dump(mode='json'))):
                raise ValueError('remote_routes_binding_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only route runtime or task binding changed') from error

    def check_remote_routes_binding_preinsert(self, job_id, state):
        from .web_application_binding import verify_web_readonly_routes, verify_web_task_binding

        draft = self._remote_routes_draft
        if (job_id != self.job_id or draft is None or self.active_runtime is None
                or self._remote_routes_runtime_id != state.runtime_id
                or self.active_runtime.runtime_id != state.runtime_id
                or self.active_runtime.runtime_pin() != draft.runtime
                or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                or verify_web_readonly_routes(self.remote_entry_profiles, self.remote_entry_task,
                                              self.remote_routes_plan) != self.remote_routes_plan
                or draft.task != self.remote_entry_task
                or draft.profile_sha256 != self.remote_entry_profile_sha256):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only route run cannot bind its live runtime')

    def check_remote_static_assets_binding(self, job_id):
        if self.remote_static_assets_plan is None:
            return
        job = self.store.connection.execute(
            'SELECT kind,run_id,runtime_id FROM desktop_tasks WHERE job_id=?',
            (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_remote_static_assets':
            if self._remote_static_assets_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Static asset job kind changed')
            return
        from .web_application_binding import verify_web_task_binding
        from .web_readonly_data import verify_web_bundle_plan

        try:
            draft = self._remote_static_assets_draft
            binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_static_asset_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            plan = verify_web_bundle_plan(self.remote_entry_profiles,
                                          self.remote_entry_task,
                                          self.remote_static_assets_plan)
            if (draft is None or binding is None or self.active_runtime is None
                    or job_id != self.job_id
                    or job['runtime_id'] != self._remote_static_assets_runtime_id
                    or self.active_runtime.runtime_id != self._remote_static_assets_runtime_id
                    or self.active_runtime.runtime_pin() != draft.runtime
                    or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                    or draft.task != self.remote_entry_task
                    or binding['run_id'] != job['run_id']
                    or binding['browser_runtime_id'] != self._remote_static_assets_runtime_id
                    or binding['profile_sha256'] != draft.profile_sha256
                    or binding['binding_sha256'] != draft.binding_sha256
                    or binding['runtime_sha256'] != draft.runtime_sha256
                    or binding['plan_sha256'] != digest(plan.model_dump())
                    or binding['draft_json'] != canonical(draft.model_dump(mode='json'))
                    or binding['plan_json'] != canonical(plan.model_dump(mode='json'))):
                raise ValueError('remote_static_assets_binding_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Static asset runtime or task binding changed') from error

    def check_remote_static_assets_binding_preinsert(self, job_id, state):
        from .web_application_binding import verify_web_task_binding
        from .web_readonly_data import verify_web_bundle_plan

        draft = self._remote_static_assets_draft
        if (job_id != self.job_id or draft is None or self.active_runtime is None
                or self._remote_static_assets_runtime_id != state.runtime_id
                or self.active_runtime.runtime_id != state.runtime_id
                or self.active_runtime.runtime_pin() != draft.runtime
                or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                or verify_web_bundle_plan(self.remote_entry_profiles,
                                          self.remote_entry_task,
                                          self.remote_static_assets_plan)
                != self.remote_static_assets_plan
                or draft.task != self.remote_entry_task
                or draft.profile_sha256 != self.remote_entry_profile_sha256):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Static asset run cannot bind its live runtime')

    def check_remote_form_binding(self, job_id):
        if self.remote_form_plan is None:
            return
        job = self.store.connection.execute(
            'SELECT kind,run_id,runtime_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if job is None or job['kind'] != 'browser_remote_form':
            if self._remote_form_runtime_id is not None and job_id == self.job_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form job kind changed')
            return
        from .web_application_binding import verify_web_task_binding
        from .web_https_form_transport import (verify_web_https_form_plan,
                                               verify_web_https_form_target_grant)
        import hashlib
        from .web_https_form_transport import form_body

        try:
            draft = self._remote_form_draft
            plan = verify_web_https_form_plan(self.remote_entry_profiles, self.remote_entry_task,
                                              self.remote_form_plan)
            verify_web_https_form_target_grant(
                plan, self.remote_form_public_plan_sha256, self.remote_form_owned_target)
            binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_form_bindings WHERE job_id=?', (job_id,)).fetchone()
            if (draft is None or binding is None or self.active_runtime is None
                    or job_id != self.job_id or job['runtime_id'] != self._remote_form_runtime_id
                    or self.active_runtime.runtime_id != self._remote_form_runtime_id
                    or self.active_runtime.runtime_pin() != draft.runtime
                    or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                    or draft.task != self.remote_entry_task
                    or hashlib.sha256(form_body(self.remote_form_fields)).hexdigest()
                    != plan.body_sha256
                    or binding['run_id'] != job['run_id']
                    or binding['browser_runtime_id'] != self._remote_form_runtime_id
                    or binding['profile_sha256'] != draft.profile_sha256
                    or binding['binding_sha256'] != draft.binding_sha256
                    or binding['runtime_sha256'] != draft.runtime_sha256
                    or binding['plan_sha256'] != digest(plan.model_dump())
                    or binding['draft_json'] != canonical(draft.model_dump(mode='json'))
                    or binding['plan_json'] != canonical(plan.model_dump(mode='json'))):
                raise ValueError('remote_form_binding_changed')
            state_binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_form_state_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            if self.remote_form_state_plan is None:
                if state_binding is not None:
                    raise ValueError('remote_form_state_binding_unexpected')
            else:
                from .web_https_form_state_probe import verify_web_https_form_state_plan

                state_plan = verify_web_https_form_state_plan(
                    self.remote_entry_profiles, self.remote_entry_task, plan,
                    self.remote_form_state_plan, self.remote_form_tls_context,
                    confirm_public_form_plan_sha256=self.remote_form_public_plan_sha256,
                    confirm_public_state_plan_sha256=self.remote_form_public_state_plan_sha256,
                    owned_form_target=self.remote_form_owned_target)
                if (state_binding is None or state_binding['run_id'] != job['run_id']
                        or state_binding['form_plan_sha256'] != digest(plan.model_dump())
                        or state_binding['state_plan_sha256'] != digest(state_plan.model_dump())
                        or state_binding['state_plan_json'] != canonical(state_plan.model_dump(mode='json'))
                        or state_binding['browser_runtime_id'] != self._remote_form_runtime_id):
                    raise ValueError('remote_form_state_binding_changed')
            if self.remote_form_skill_invocation is not None:
                state = self.store.state(job['run_id'])
                if (self.remote_form_skill_revalidator is None
                        or state.skill_invocation_sha256 != self.remote_form_skill_invocation_sha256
                        or digest(self.remote_form_skill_revalidator().model_dump(mode='json'))
                        != self.remote_form_skill_invocation_sha256):
                    raise ValueError('remote_form_skill_invocation_sources_changed')
            cookie_binding = self.store.connection.execute(
                'SELECT * FROM desktop_remote_form_cookie_bindings WHERE job_id=?',
                (job_id,)).fetchone()
            if self.remote_form_cookie is None:
                if cookie_binding is not None or self.remote_form_cookie_sha256 is not None:
                    raise ValueError('remote_form_cookie_binding_unexpected')
            else:
                from .web_https_preflight import validate_https_cookie_header

                if (cookie_binding is None or cookie_binding['run_id'] != job['run_id']
                        or cookie_binding['form_plan_sha256'] != digest(plan.model_dump())
                        or cookie_binding['cookie_sha256'] != self.remote_form_cookie_sha256
                        or cookie_binding['browser_runtime_id'] != self._remote_form_runtime_id
                        or hashlib.sha256(validate_https_cookie_header(
                            self.remote_form_cookie).encode('ascii')).hexdigest()
                        != self.remote_form_cookie_sha256):
                    raise ValueError('remote_form_cookie_binding_changed')
        except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form runtime or task binding changed') from error

    def check_remote_form_binding_preinsert(self, job_id, state):
        from .web_application_binding import verify_web_task_binding
        from .web_https_form_transport import (verify_web_https_form_plan,
                                               verify_web_https_form_target_grant)
        import hashlib
        from .web_https_form_transport import form_body

        draft = self._remote_form_draft
        try:
            verify_web_https_form_target_grant(
                self.remote_form_plan, self.remote_form_public_plan_sha256,
                self.remote_form_owned_target)
        except ValueError as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form public grant changed') from error
        if (job_id != self.job_id or draft is None or self.active_runtime is None
                or self._remote_form_runtime_id != state.runtime_id
                or self.active_runtime.runtime_id != state.runtime_id
                or self.active_runtime.runtime_pin() != draft.runtime
                or verify_web_task_binding(self.remote_entry_profiles, draft) != draft
                or verify_web_https_form_plan(self.remote_entry_profiles, self.remote_entry_task,
                                              self.remote_form_plan) != self.remote_form_plan
                or draft.task != self.remote_entry_task
                or hashlib.sha256(form_body(self.remote_form_fields)).hexdigest()
                != self.remote_form_plan.body_sha256
                or draft.profile_sha256 != self.remote_entry_profile_sha256):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form run cannot bind its live runtime')
        if self.remote_form_skill_invocation is not None:
            try:
                if (self.remote_form_skill_revalidator is None
                        or state.skill_invocation_sha256 != self.remote_form_skill_invocation_sha256
                        or digest(self.remote_form_skill_revalidator().model_dump(mode='json'))
                        != self.remote_form_skill_invocation_sha256):
                    raise ValueError('remote_form_skill_invocation_sources_changed')
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'HTTPS form skill sources changed') from error
        if self.remote_form_cookie is not None:
            from .web_https_preflight import validate_https_cookie_header

            try:
                if (self.remote_form_public_plan_sha256 is None
                        or hashlib.sha256(validate_https_cookie_header(
                            self.remote_form_cookie).encode('ascii')).hexdigest()
                        != self.remote_form_cookie_sha256):
                    raise ValueError('HTTPS form cookie changed')
            except (ValueError, TypeError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'HTTPS form cookie binding changed') from error
        if self.remote_form_state_plan is not None:
            from .web_https_form_state_probe import verify_web_https_form_state_plan

            try:
                verify_web_https_form_state_plan(
                    self.remote_entry_profiles, self.remote_entry_task,
                    self.remote_form_plan, self.remote_form_state_plan,
                    self.remote_form_tls_context,
                    confirm_public_form_plan_sha256=self.remote_form_public_plan_sha256,
                    confirm_public_state_plan_sha256=self.remote_form_public_state_plan_sha256,
                    owned_form_target=self.remote_form_owned_target)
            except (OSError, ValueError, TypeError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'HTTPS form state run plan changed') from error

    def status(self):
        jobs = [dict(row) for row in self.store.connection.execute(
            'SELECT job_id,run_id,kind,status,real_model,created_at,runtime_id FROM desktop_tasks WHERE session_id=? ORDER BY rowid DESC LIMIT 20',
            (self.controller.session_id,))]
        for job in jobs:
            job['progress'] = task_progress(self.store, job['job_id'], self.controller.session_id)
            job['failure_followup'] = self._failure_followup_jobs.get(job['job_id'])
            job['failure_guidance'] = self._failure_guidance_jobs.get(job['job_id'])
            job['hello_guidance_reuse'] = self._hello_guidance_reuse_jobs.get(job['job_id'])
            job['learning_metadata'] = self.learning_status(job['job_id'])
            job['remote_learning_metadata'] = self.remote_learning_status(job['job_id'])
        if (jobs and jobs[0]['kind'] == 'browser_remote_routes'
                and self.remote_route_review_source is not None
                and jobs[0]['run_id'] is not None):
            row = self.store.connection.execute(
                "SELECT payload_json FROM observations WHERE run_id=? "
                "AND kind='browser.remote_route_knowledge' ORDER BY rowid DESC LIMIT 1",
                (jobs[0]['run_id'],)).fetchone()
            if row is not None:
                try:
                    event = json.loads(row[0])
                    if (event['review_sha256'] == self.remote_route_review_source['review_sha256']
                            and event['status'] in {'matched', 'stale'}
                            and type(event['route_index']) is int
                            and 0 <= event['route_index'] < len(self.remote_routes_plan.routes)
                            and event.get('stale_reason') in {
                                None, 'source_fingerprint_changed',
                                'source_link_sample_changed', 'target_fingerprint_changed'}):
                        jobs[0]['route_knowledge'] = {
                            'status': event['status'], 'route_index': event['route_index'],
                            'stale_reason': event.get('stale_reason') if event['status'] == 'stale' else None}
                except (KeyError, TypeError, ValueError):
                    pass
        if (jobs and jobs[0]['kind'] == 'browser_remote_static_assets'
                and jobs[0]['status'] == 'succeeded'
                and self.remote_json_review_source is not None
                and jobs[0]['run_id'] is not None):
            row = self.store.connection.execute(
                "SELECT payload_json FROM observations WHERE run_id=? "
                "AND kind='browser.remote_json_knowledge' ORDER BY rowid DESC LIMIT 1",
                (jobs[0]['run_id'],)).fetchone()
            if row is not None:
                try:
                    from .remote_readonly_data_knowledge_review import (
                        LiveRemoteReadonlyDataKnowledgeEvent)

                    event = LiveRemoteReadonlyDataKnowledgeEvent.model_validate_json(row[0])
                    if event.review_sha256 == self.remote_json_review_source['review_sha256']:
                        jobs[0]['json_knowledge'] = {
                            'status': event.status,
                            'page_key': event.page_key if event.status == 'matched' else None,
                            'draft_revision': (event.draft_revision
                                               if event.status == 'matched' else None)}
                except (TypeError, ValueError):
                    pass
        row = self.store.connection.execute(
            "SELECT envelope_json FROM desktop_approvals JOIN desktop_tasks USING(job_id) WHERE session_id=? AND desktop_approvals.status='pending'",
            (self.controller.session_id,)).fetchone()
        manifest = self.remote_form_owned_manifest
        recipe_mode = (manifest is not None
                       and manifest.get('mode') == 'owned_synthetic_form_recipe')
        owned_form_invocation = ({
            'mode': ('owned_synthetic_form_recipe' if recipe_mode
                     else 'owned_synthetic_form_invocation'),
            'lifecycle': self._owned_form_lifecycle,
            'profile_sha256': manifest['profile_sha256'],
            'skill_sha256': manifest['skill_sha256'],
            'invocation_sha256': manifest['invocation_sha256'],
            **({'recipe_sha256': manifest['recipe_sha256'],
                'steps': [{'step_key': step['step_key'], 'operation': step['operation']}
                          for step in self.remote_form_skill_invocation['steps']]}
               if recipe_mode else {}),
            **({'run_ref': digest({'run_id': self._owned_form_run_id})}
               if self._owned_form_run_id is not None else {}),
            **({'report_sha256': hashlib.sha256(canonical(self._owned_form_audit).encode()).hexdigest()}
               if self._owned_form_audit is not None else {}),
            **({'reuse_admission_sha256': self._owned_skill_reuse_admission_sha256}
               if self._owned_skill_reuse_admission_sha256 is not None else {})}
            if manifest is not None else None)
        return {'available': True, 'busy': self.busy, 'paused': self.paused, 'reserved': self.reserved,
                'failure_improvement_available': True,
                'failure_followup_available': True,
                'failure_guidance_available': True,
                'hello_guidance_reuse_available': True,
                'owned_skill_planning': self.owned_skill_plan_status(),
                'owned_episode_learning': (self.owned_episode_learning.status()
                    if self.owned_episode_learning is not None else {'available': False}),
                'restart_quiesced': self.restart_quiesced, 'real_model': self.engine.identity['real_model'],
                'kinds': self.kinds(), 'real_supervisor': bool(self.vision_supervisor and self.vision_supervisor.identity['real_model']),
                'browser_display': 'desktop' if self.desktop_browser else 'headless',
                'browser_transport': ('playwright_mcp' if self.desktop_mcp_manifest is not None else
                                      'cdp' if self.desktop_browser else 'playwright'),
                'navigation_transport': ('playwright_mcp' if self.desktop_mcp_manifest is not None
                                         or self.desktop_navigation_mcp_manifest is not None else None),
                'staging_transport': ('playwright_mcp' if self.desktop_staging_mcp_manifest is not None else None),
                'remote_entry': ({'profile_sha256': self.remote_entry_profile_sha256,
                                  'entry_url': self.remote_entry_task.entry_url,
                                  'task_key': self.remote_entry_task.task_key,
                                  'mode': 'one_shot_read_only'} if self.remote_entry_task is not None else None),
                'remote_routes': ({'plan_sha256': digest(self.remote_routes_plan.model_dump()),
                                   'route_count': len(self.remote_routes_plan.routes),
                                   'review_sha256': (self.remote_route_review_source['review_sha256']
                                                     if self.remote_route_review_source is not None else None),
                                   'mode': 'ordered_read_only'} if self.remote_routes_plan is not None else None),
                'remote_static_assets': ({
                    'plan_sha256': digest(self.remote_static_assets_plan.model_dump()),
                    'asset_count': len(self.remote_static_assets_plan.assets),
                    'assets': [asset.url for asset in self.remote_static_assets_plan.assets],
                    'data_resources': [resource.url for resource in
                                       getattr(self.remote_static_assets_plan,
                                               'data_resources', [])],
                    'review_sha256': (self.remote_json_review_source['review_sha256']
                                      if self.remote_json_review_source is not None else None),
                    'mode': ('one_shot_readonly_data_bundle'
                             if hasattr(self.remote_static_assets_plan, 'data_resources')
                             else 'one_shot_static_bundle')}
                    if self.remote_static_assets_plan is not None else None),
                'remote_form': ({'plan_sha256': digest(self.remote_form_plan.model_dump()),
                                 'entry_url': self.remote_form_plan.entry_url,
                                 'submit_url': self.remote_form_plan.submit_url,
                                 'receipt_url': self.remote_form_plan.receipt_url,
                                 'field_name': self.remote_form_field_name,
                                 **({'field_names': [name for name, _content in self.remote_form_fields]}
                                    if self.remote_form_field_name is None else {}),
                                 'body_sha256': self.remote_form_plan.body_sha256,
                                 'cookie_sha256': self.remote_form_cookie_sha256,
                                 'state_plan_sha256': (digest(self.remote_form_state_plan.model_dump())
                                                       if self.remote_form_state_plan is not None else None),
                                 'state_url': (self.remote_form_state_plan.state_url
                                               if self.remote_form_state_plan is not None else None),
                                 'mode': ('public_explicit_one_post'
                                          if self.remote_form_public_plan_sha256 is not None
                                          else 'synthetic_one_post')}
                                if self.remote_form_plan is not None else None),
                'vision_display': 'desktop' if self.desktop_vision else 'headless',
                'decider_preparation': (self.engine.prewarm_status() if isinstance(self.engine, ReusableDeciderEngine)
                                        else {'enabled': False, 'state': 'inactive'}),
                'supports_approve_all': True,
                'supports_learning_metadata': self.synthetic_learning_stream_dir is not None,
                'supports_remote_learning_metadata': self.remote_learning_consents_dir is not None,
                'owned_form_invocation': owned_form_invocation,
                'owned_form_candidate_execution': self._owned_candidate_execution_status(),
                'auto_approval': ({'job_id': self._approval_grant.job_id, 'kind': self._approval_grant.kind}
                                  if self.grant_current() else None),
                'sequence': self.sequences.report.model_dump() if self.sequences.report else None,
                'jobs': jobs, 'approval': json.loads(row[0]) if row else None}

    def learning_status(self, job_id: str) -> dict:
        job = self.store.connection.execute('SELECT status,run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if job is None:
            raise ValueError('unknown_learning_job')
        opted = self.store.connection.execute(
            "SELECT 1 FROM desktop_events WHERE kind='learning_stream_opt_in' "
            "AND json_extract(payload_json,'$.job_id')=? LIMIT 1", (job_id,)).fetchone()
        if opted is None:
            return {'enabled': False, 'state': 'disabled'}
        if job_id in self._learning_unpersisted:
            return self._learning_unpersisted[job_id]
        latest = self.store.connection.execute(
            "SELECT kind,payload_json FROM desktop_events WHERE kind IN ('learning_stream_poll','learning_stream_failure') "
            "AND json_extract(payload_json,'$.job_id')=? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
        if latest is None:
            return {'enabled': True, 'state': 'pending' if job['status'] in {'queued', 'running', 'waiting_approval'}
                    else 'recovery_required', 'run_ref': digest({'run_id': job['run_id']}) if job['run_id'] else None}
        payload = json.loads(latest['payload_json'])
        run = self.store.connection.execute('SELECT status FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
        state = ('failed' if latest['kind'] == 'learning_stream_failure' else
                 'recovery_required' if run is None or payload['run_status'] != run['status'] else
                 'synced' if payload['phase'] == 'settled' and job['status'] in {
                     'succeeded', 'failed', 'cancelled', 'waiting_human'} else
                 'paused' if payload['phase'] == 'settled' and job['status'] == 'paused' else
                 'recovery_required' if job['status'] in {'succeeded', 'failed', 'cancelled', 'waiting_human', 'paused'} else
                 'collecting')
        return {'enabled': True, 'state': state, 'run_ref': payload['run_ref'],
                'phase': payload['phase'], 'duration_ms': payload['duration_ms'],
                'new_entries': payload.get('new_entries', 0), 'total_entries': payload.get('total_entries'),
                'entries_by_role': payload.get('entries_by_role'),
                'reason': payload.get('reason')}

    def _learning_poll(self, job_id: str, run_id: str, phase: str) -> None:
        if job_id not in self._learning_opted_jobs or job_id in self._learning_failed_jobs:
            return
        started = time.monotonic()
        report = None
        reason = None
        try:
            if self.synthetic_learning_stream_dir is None or self.store.connection.in_transaction:
                raise ValueError('learning_stream_unsafe_poll_point')
            source = next((row[2] for row in self.store.connection.execute('PRAGMA database_list')
                           if row[1] == 'main'), None)
            job = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            if (source is None or Path(source).resolve(strict=True) != self.settings.database.resolve(strict=True)
                    or job is None or job['run_id'] != run_id):
                raise ValueError('learning_stream_job_or_source_changed')
            report = poll_learning_stream(self.settings.database, run_id, LEARNING_ROLES,
                                          self.synthetic_learning_stream_dir)
        except Exception:
            reason = 'unsafe_or_unavailable'
            self._learning_failed_jobs.add(job_id)
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        payload = {'job_id': job_id, 'run_ref': digest({'run_id': run_id}), 'phase': phase,
                   'duration_ms': duration_ms, 'synthetic': True, 'training_ready': False,
                   'collection_authorized': False}
        if report is not None:
            payload.update(new_entries=report['new_entries'], total_entries=report['total_entries'],
                           entries_by_role=report['entries_by_role'],
                           run_status=report['run_status'],
                           source_snapshot_sha256=report['source_snapshot_sha256'])
        else:
            payload['reason'] = reason
        kind = 'learning_stream_poll' if report is not None else 'learning_stream_failure'
        try:
            with self.store.connection:
                self.store.insert('desktop_events', event_id=identifier('event'),
                                  session_id=self.controller.session_id, kind=kind,
                                  payload_json=canonical(payload), created_at=now())
        except Exception:
            self._learning_failed_jobs.add(job_id)
            self._learning_unpersisted[job_id] = {
                'enabled': True, 'state': 'failed_unpersisted', 'run_ref': payload['run_ref'],
                                  'phase': phase, 'duration_ms': duration_ms, 'reason': 'audit_unavailable'}

    def remote_learning_status(self, job_id: str) -> dict:
        job = self.store.connection.execute(
            'SELECT run_id,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if job is None:
            raise ValueError('unknown_remote_learning_job')
        if job_id in self._remote_learning_unpersisted:
            return self._remote_learning_unpersisted[job_id]
        opted = self.store.connection.execute(
            "SELECT 1 FROM desktop_events WHERE kind='remote_learning_stream_opt_in' "
            "AND json_extract(payload_json,'$.job_id')=? LIMIT 1", (job_id,)).fetchone()
        if opted is None:
            return {'enabled': False, 'state': 'disabled'}
        latest = self.store.connection.execute(
            "SELECT kind,payload_json FROM desktop_events WHERE kind IN "
            "('remote_learning_stream_opt_in','remote_learning_stream_poll',"
            "'remote_learning_stream_failure') AND json_extract(payload_json,'$.job_id')=? "
            "ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
        payload = json.loads(latest['payload_json'])
        if latest['kind'] == 'remote_learning_stream_failure':
            state = 'failed'
        elif job['status'] in {'succeeded', 'failed', 'cancelled', 'waiting_human'}:
            state = 'synced' if payload['phase'] == 'settled' else 'recovery_required'
        elif job_id not in self._remote_learning_attached:
            state = 'recovery_required'
        else:
            state = 'collecting'
        return {'enabled': True, 'state': state, 'run_ref': payload['run_ref'],
                'consent_sha256': payload['consent_sha256'], 'phase': payload['phase'],
                'duration_ms': payload['duration_ms'],
                'new_entries': payload.get('new_entries', 0),
                'total_entries': payload.get('total_entries'),
                'entries_by_role': payload.get('entries_by_role'),
                'reason': payload.get('reason')}

    def attach_remote_learning(self, job_id: str, consent_sha256: str) -> dict:
        from .remote_learning_consent import RemoteLearningConsents

        job = self._pending_remote_learning_job(job_id)
        plan, scope, poller = self._remote_learning_plan(job['kind'])
        consent = RemoteLearningConsents(self.remote_learning_consents_dir).get(consent_sha256)
        if (consent.run_id != job['run_id']
                or consent.profile_sha256 != self.remote_entry_profile_sha256
                or consent.plan_sha256 != digest(plan.model_dump())
                or consent.scope != scope
                or consent.state_plan_sha256 != (digest(self.remote_form_state_plan.model_dump())
                                                 if scope == 'remote_form_state_model_metadata_only'
                                                 else None)):
            raise ValueError('remote_learning_attach_selection_changed')
        report = poller(
            self.settings.database, profiles=self.remote_entry_profiles.root,
            consents=self.remote_learning_consents_dir,
            consent_sha256=consent_sha256,
            outbox_dir=self.remote_learning_stream_dir)
        payload = {'job_id': job_id, 'run_ref': report['run_ref'],
                   'consent_sha256': consent_sha256, 'phase': 'attached',
                   'duration_ms': 0, 'new_entries': report['new_entries'],
                   'total_entries': report['total_entries'],
                   'entries_by_role': report['entries_by_role'],
                   'run_status': report['run_status'], 'metadata_only': True,
                   'training_ready': False}
        with self.store.connection:
            self.store.insert('desktop_events', event_id=identifier('event'),
                              session_id=self.controller.session_id,
                              kind='remote_learning_stream_opt_in',
                              payload_json=canonical(payload), created_at=now())
        self._remote_learning_attached[job_id] = consent_sha256
        return self.remote_learning_status(job_id)

    def _remote_learning_plan(self, kind: str):
        if kind == 'browser_remote_routes' and self.remote_routes_plan is not None:
            from .remote_learning_stream import poll_remote_learning_stream
            return self.remote_routes_plan, 'remote_route_model_metadata_only', poll_remote_learning_stream
        if (kind == 'browser_remote_static_assets'
                and self.remote_static_assets_plan is not None
                and not hasattr(self.remote_static_assets_plan, 'data_resources')):
            from .remote_static_learning_stream import poll_remote_static_learning_stream
            return self.remote_static_assets_plan, 'remote_static_model_metadata_only', poll_remote_static_learning_stream
        if (kind == 'browser_remote_static_assets'
                and self.remote_static_assets_plan is not None
                and hasattr(self.remote_static_assets_plan, 'data_resources')):
            from .remote_static_learning_stream import poll_remote_readonly_data_stream
            return self.remote_static_assets_plan, 'remote_json_model_metadata_only', poll_remote_readonly_data_stream
        if (kind == 'browser_remote_form' and self.remote_form_plan is not None
                and self.remote_form_cookie is None):
            from .remote_form_learning_stream import poll_remote_form_learning_stream
            return (self.remote_form_plan,
                    'remote_form_state_model_metadata_only' if self.remote_form_state_plan is not None
                    else 'remote_form_model_metadata_only', poll_remote_form_learning_stream)
        raise ValueError('remote_learning_task_plan_unavailable')

    def _pending_remote_learning_job(self, job_id: str):
        if (self.remote_learning_consents_dir is None
                or self.remote_learning_stream_dir is None
                or job_id != self.job_id or not self.busy
                or self.store.connection.in_transaction
                or job_id in self._remote_learning_attached):
            raise ValueError('remote_learning_attach_unavailable')
        job = self.store.connection.execute(
            'SELECT run_id,kind,status FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if (job is None or job['kind'] not in {'browser_remote_routes', 'browser_remote_static_assets',
                                              'browser_remote_form'}
                or job['status'] != 'waiting_approval' or job['run_id'] is None):
            raise ValueError('remote_learning_attach_requires_pending_task')
        if job['kind'] == 'browser_remote_form':
            pending = self.store.connection.execute(
                "SELECT envelope_json FROM desktop_approvals WHERE job_id=? AND status='pending'",
                (job_id,)).fetchone()
            if (pending is None or Approval.model_validate_json(
                    pending['envelope_json']).action.tool != 'browser.form.open'):
                raise ValueError('form_learning_attach_requires_entry_approval')
        source = next((row[2] for row in self.store.connection.execute('PRAGMA database_list')
                       if row[1] == 'main'), None)
        if (source is None or Path(source).resolve(strict=True)
                != self.settings.database.resolve(strict=True)):
            raise ValueError('remote_learning_database_changed')
        self.check_lease(job_id)
        self._remote_learning_plan(job['kind'])
        if job['kind'] == 'browser_remote_routes':
            self.check_remote_routes_binding(job_id)
        elif job['kind'] == 'browser_remote_static_assets':
            self.check_remote_static_assets_binding(job_id)
        else:
            self.check_remote_form_binding(job_id)
        return job

    def prepare_remote_learning_consent(self, job_id: str, *, roles: list[str],
                                        expires_at: str, attest_data_rights: bool,
                                        confirm_sha256: str | None = None) -> dict:
        from .remote_learning_consent import (RemoteLearningConsents,
                                              preview_remote_learning_consent)

        job = self._pending_remote_learning_job(job_id)
        plan, scope, _poller = self._remote_learning_plan(job['kind'])
        consent = preview_remote_learning_consent(
            self.settings.database, job['run_id'], profiles=self.remote_entry_profiles.root,
            selected_profile_sha256=self.remote_entry_profile_sha256,
            selected_plan_sha256=digest(plan.model_dump()),
            roles=roles, expires_at=expires_at,
            attest_data_rights=attest_data_rights, scope=scope,
            selected_state_plan_sha256=(digest(self.remote_form_state_plan.model_dump())
                                        if scope == 'remote_form_state_model_metadata_only'
                                        else None))
        checksum = digest(consent.model_dump())
        if confirm_sha256 is not None:
            RemoteLearningConsents(self.remote_learning_consents_dir).register(
                consent, confirm_sha256=confirm_sha256,
                database=self.settings.database, profiles=self.remote_entry_profiles.root)
        return {'consent_sha256': checksum, 'registered': confirm_sha256 is not None,
                'run_ref': digest({'run_id': consent.run_id}), 'roles': consent.roles,
                'expires_at': consent.expires_at, 'metadata_only': True,
                'external_rights_verified': False, 'training_authorized': False}

    def _remote_learning_poll(self, job_id: str, run_id: str, phase: str) -> None:
        consent_sha256 = self._remote_learning_attached.get(job_id)
        if consent_sha256 is None or job_id in self._remote_learning_failed:
            return

        started = time.monotonic()
        report = None
        try:
            if self.store.connection.in_transaction:
                raise ValueError('remote_learning_unsafe_poll_point')
            source = next((row[2] for row in self.store.connection.execute('PRAGMA database_list')
                           if row[1] == 'main'), None)
            job = self.store.connection.execute(
                'SELECT run_id,kind FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            if (source is None or Path(source).resolve(strict=True) != self.settings.database.resolve(strict=True)
                    or job is None or job['run_id'] != run_id):
                raise ValueError('remote_learning_job_or_source_changed')
            _plan, _scope, poller = self._remote_learning_plan(job['kind'])
            report = poller(
                self.settings.database, profiles=self.remote_entry_profiles.root,
                consents=self.remote_learning_consents_dir,
                consent_sha256=consent_sha256,
                outbox_dir=self.remote_learning_stream_dir)
        except Exception:
            self._remote_learning_failed.add(job_id)
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        payload = {'job_id': job_id, 'run_ref': digest({'run_id': run_id}),
                   'consent_sha256': consent_sha256, 'phase': phase,
                   'duration_ms': duration_ms, 'metadata_only': True,
                   'training_ready': False}
        if report is None:
            payload['reason'] = 'unsafe_or_unavailable'
        else:
            payload.update(new_entries=report['new_entries'],
                           total_entries=report['total_entries'],
                           entries_by_role=report['entries_by_role'],
                           run_status=report['run_status'],
                           source_snapshot_sha256=report['source_snapshot_sha256'])
        kind = 'remote_learning_stream_poll' if report is not None else 'remote_learning_stream_failure'
        try:
            with self.store.connection:
                self.store.insert('desktop_events', event_id=identifier('event'),
                                  session_id=self.controller.session_id, kind=kind,
                                  payload_json=canonical(payload), created_at=now())
        except Exception:
            self._remote_learning_failed.add(job_id)
            self._remote_learning_unpersisted[job_id] = {
                'enabled': True, 'state': 'failed_unpersisted',
                'run_ref': payload['run_ref'], 'consent_sha256': consent_sha256,
                'phase': phase, 'duration_ms': duration_ms,
                'reason': 'audit_unavailable'}

    def grant_event(self, grant, status, reason):
        payload = {key: value for key, value in asdict(grant).items() if key != 'monotonic_deadline'}
        payload.update(status=status, reason=reason,
                       max_actions={'hello': 1, 'browser_form': 2, 'vision_canvas': 1}[grant.kind])
        self.store.insert('desktop_events', event_id=identifier('event'), session_id=grant.session_id,
                          kind='task_auto_approval', payload_json=canonical(payload), created_at=now())

    def clear_grant(self, reason):
        grant = self._approval_grant
        self._approval_grant = None
        self._grant_uses = 0
        if grant is not None:
            with self.store.connection:
                self.grant_event(grant, 'revoked', reason)

    def grant_current(self):
        grant = self._approval_grant
        if (grant is None or self.closed or not self.busy or self.job_id != grant.job_id
                or self.controller.session_id != grant.session_id
                or time.time() >= grant.expires_at or time.monotonic() >= grant.monotonic_deadline):
            return False
        desktop = self.controller.state()
        job = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (grant.job_id,)).fetchone()
        return bool(job and job['session_id'] == grant.session_id and job['kind'] == grant.kind
                    and job['lease_id'] == grant.lease_id and job['generation'] == grant.generation
                    and job['status'] in {'queued', 'running', 'waiting_approval'}
                    and desktop['owner'] == 'AGENT' and desktop['status'] == 'running'
                    and desktop['lease_id'] == grant.lease_id and desktop['generation'] == grant.generation
                    and desktop['runtime_id'] == grant.parent_runtime_id == self.controller.runtime.runtime_id
                    and (grant.run_id is None or (job['run_id'] == grant.run_id
                         and job['runtime_id'] == grant.runtime_id and self.active_runtime is not None
                         and self.active_runtime.runtime_id == grant.runtime_id)))

    def validate_delegated_action(self, job_id, action):
        grant = self._approval_grant
        if (not self.grant_current() or grant.job_id != job_id or grant.run_id != action.run_id
                or grant.task_id != action.task_id or grant.runtime_id != action.runtime_id
                or grant.lease_id != action.owner_lease_id):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Action is outside the live task delegation')
        state = self.store.state(action.run_id)
        self.check_lease(job_id)
        SafetyPolicy.check(action, state, self.active_runtime.runtime_id)
        if state.task_kind != grant.kind:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Delegated task kind changed')
        valid = False
        if grant.kind == 'hello':
            arguments = {'path': HELLO_PATH}
            if action.tool == 'filesystem.write':
                arguments['content'] = HELLO_CONTENT
            valid = (self._grant_uses == 0 and state.authorized_path == HELLO_PATH
                     and state.authorized_content == HELLO_CONTENT
                     and (action.tool, action.selected_option) in {
                         ('filesystem.write', 'write_file'), ('filesystem.read', 'read_file')}
                     and action.arguments == arguments and action.verification == 'independent_read_equals')
        elif grant.kind == 'browser_form' and self._grant_uses < 2:
            ordinal = self._grant_uses
            row = self.store.connection.execute(
                "SELECT payload_json FROM observations WHERE run_id=? AND kind='browser.dom' ORDER BY rowid DESC LIMIT 1",
                (grant.run_id,)).fetchone()
            observed = DomObservation.model_validate_json(row['payload_json']) if row else None
            if observed:
                arguments = {'snapshot_id': observed.snapshot_id, 'element_id': observed.elements[ordinal].element_id}
                if ordinal == 0:
                    arguments['value'] = BROWSER_VALUE
                fills = self.store.connection.execute(
                    "SELECT count(*) FROM actions JOIN verifications USING(action_id) WHERE actions.run_id=? AND tool='browser.fill' AND status='ok' AND result='passed'",
                    (grant.run_id,)).fetchone()[0]
                valid = (state.authorized_path == BROWSER_SCOPE and state.authorized_content == BROWSER_VALUE
                         and action.tool == ('browser.fill', 'browser.submit')[ordinal]
                         and action.selected_option == ('fill_message', 'submit_form')[ordinal]
                         and action.verification == 'independent_dom_equals' and action.arguments == arguments
                         and fills == ordinal and observed.value == ('', BROWSER_VALUE)[ordinal]
                         and observed.receipt == '' and observed.submissions == 0)
        elif grant.kind == 'vision_canvas':
            scene = getattr(self.active_runtime, 'scene', None)
            capture = getattr(self.active_runtime, 'capture', None)
            element = scene.targets().get(action.selected_option) if scene is not None else None
            valid = (self._grant_uses == 0 and state.authorized_path == VISION_SCOPE
                     and state.authorized_content == 'SAVE' and action.tool == 'vision.click'
                     and action.verification == 'independent_canvas_equals' and scene is not None
                     and capture is not None and element is not None and element.label == 'SAVE'
                     and not scene.needs_human and scene.capture_id == capture.capture_id == state.capture_id
                     and digest(scene.model_dump()) == state.scene_sha256
                     and action.arguments == {'capture_id': state.capture_id, 'scene_sha256': state.scene_sha256,
                                              'element_id': action.selected_option})
        if not valid:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Action exceeds the fixed task delegation scope or order')

    def kinds(self):
        return ['hello'] + (['browser_form'] if self.browser_manifest else []) + (
            ['vision_canvas'] if self.browser_manifest and self.vision_supervisor else []) + (
            ['browser_local_navigation'] if self.desktop_mcp_manifest is not None
            or self.desktop_navigation_mcp_manifest is not None else []) + (
            ['browser_staging_workflow'] if self.desktop_staging_mcp_manifest is not None else []) + (
            ['browser_remote_entry'] if self.remote_entry_task is not None else []) + (
            ['browser_remote_routes'] if self.remote_routes_plan is not None else []) + (
            ['browser_remote_static_assets']
            if self.remote_static_assets_plan is not None else []) + (
            ['browser_remote_form'] if self.remote_form_plan is not None else [])

    def start(self, lease_id: str, generation: int, kind: str = 'hello', approve_all: bool = False,
              learning_metadata: bool = False, failure_followup: str | None = None,
              failure_guidance: str | None = None, hello_guidance_reuse: str | None = None,
              task_knowledge: str | None = None):
        if self.restart_quiesced:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task admission is quiesced for restart')
        if type(approve_all) is not bool:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task approval delegation must be an explicit boolean')
        if type(learning_metadata) is not bool:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Learning metadata opt-in must be an explicit boolean')
        if failure_followup is not None and (approve_all or learning_metadata
                or type(failure_followup) is not str or re.fullmatch('[a-f0-9]{64}', failure_followup) is None):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Follow-up requires exact intent and separate manual approvals')
        if self.sequences.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'A sequence owns task admission')
        owned_form_was_ready = (self.remote_form_owned_fixture is not None
                                and self._owned_form_lifecycle == 'ready')
        try:
            return self._start(lease_id, generation, kind, approve_all, learning_metadata,
                               failure_followup, failure_guidance, hello_guidance_reuse, task_knowledge)
        except Exception:
            if owned_form_was_ready and self._owned_form_lifecycle == 'running':
                self._consume_owned_form('failed')
            raise

    def _start(self, lease_id: str, generation: int, kind: str, approve_all: bool = False,
               learning_metadata: bool = False, failure_followup: str | None = None,
               failure_guidance: str | None = None, hello_guidance_reuse: str | None = None,
               task_knowledge: str | None = None):
        if task_knowledge is not None and (self.task_knowledge is None or approve_all or learning_metadata
                or failure_followup is not None or failure_guidance is not None or hello_guidance_reuse is not None
                or self.remote_form_owned_fixture is not None or self._owned_skill_reuse is not None
                or type(task_knowledge) is not str or re.fullmatch('[a-f0-9]{64}', task_knowledge) is None):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task knowledge requires a fresh ordinary task and separate manual effects')
        if hello_guidance_reuse is not None and (failure_guidance is None or failure_followup is None
                or kind != 'hello' or type(hello_guidance_reuse) is not str
                or re.fullmatch('[a-f0-9]{64}', hello_guidance_reuse) is None):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reuse requires exact entry intent and separately consented guidance')
        if failure_guidance is not None and (failure_followup is None or kind != 'hello'
                or type(failure_guidance) is not str or re.fullmatch('[a-f0-9]{64}', failure_guidance) is None):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Guidance requires a separately consented fresh hello follow-up')
        if failure_followup is not None and (approve_all or learning_metadata
                or type(failure_followup) is not str or re.fullmatch('[a-f0-9]{64}', failure_followup) is None):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Follow-up requires exact intent and separate manual approvals')
        if self.planning_reserved or self.adaptation_reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Skill planning or adaptation owns task admission')
        if self.restart_quiesced:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task admission is quiesced for restart')
        candidate_start = (self._owned_candidate_execution is not None
                           and self._owned_candidate_execution.get('lifecycle') == 'starting')
        if (self.remote_form_owned_fixture is not None or self._owned_skill_reuse is not None) and not candidate_start and (
                kind != 'browser_remote_form' or self._owned_form_lifecycle != 'ready'):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned synthetic form invocation is single-use')
        if learning_metadata and self.synthetic_learning_stream_dir is None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic learning stream is not configured')
        if learning_metadata and kind not in {'hello', 'browser_form', 'vision_canvas',
                                             'browser_local_navigation', 'browser_staging_workflow'}:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Learning metadata requires a fixed synthetic task')
        if kind in {'browser_local_navigation', 'browser_staging_workflow', 'browser_remote_entry',
                    'browser_remote_routes', 'browser_remote_static_assets',
                    'browser_remote_form'} and approve_all:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Local web workflows require separate explicit action approvals')
        desktop = self.controller.state()
        if (kind not in self.kinds() or self.closed or self.busy or self.paused or desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or desktop['lease_id'] != lease_id or desktop['generation'] != generation):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task admission requires idle, current agent ownership')
        if kind == 'browser_local_navigation' and self.local_navigation_pin is not None:
            from .local_navigation_admission import check_synthetic_navigation_preflight

            try:
                check_synthetic_navigation_preflight(
                    self.local_navigation_profiles, self.local_navigation_pin,
                    self.controller.runtime,
                    self.desktop_navigation_mcp_manifest or self.desktop_mcp_manifest)
            except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic navigation preflight differs') from error
        if kind == 'browser_staging_workflow' and self.local_staging_pin is not None:
            from .local_navigation_admission import check_synthetic_staging_preflight

            try:
                check_synthetic_staging_preflight(
                    self.local_staging_profiles, self.local_staging_pin,
                    self.controller.runtime, self.desktop_staging_mcp_manifest)
            except (AOSFault, OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic staging preflight differs') from error
        if kind in {'browser_remote_entry', 'browser_remote_routes',
                    'browser_remote_static_assets', 'browser_remote_form'}:
            from .web_application_binding import WebTaskContract

            try:
                profile = self.remote_entry_profiles.get(self.remote_entry_profile_sha256)
                if (self.remote_entry_task != WebTaskContract.model_validate(self.remote_entry_task.model_dump())
                        or profile.entry_url != self.remote_entry_task.entry_url
                        or self.remote_entry_task.task_key not in profile.task_keys):
                    raise ValueError('remote_entry_preflight_changed')
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry preflight differs') from error
        if kind == 'browser_remote_routes':
            from .web_application_binding import verify_web_readonly_routes

            try:
                verify_web_readonly_routes(self.remote_entry_profiles, self.remote_entry_task,
                                           self.remote_routes_plan)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only route plan differs') from error
        if kind == 'browser_remote_static_assets':
            from .web_readonly_data import verify_web_bundle_plan

            try:
                verify_web_bundle_plan(self.remote_entry_profiles,
                                       self.remote_entry_task,
                                       self.remote_static_assets_plan)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Static asset plan differs') from error
        if kind == 'browser_remote_form':
            from .web_https_form_transport import verify_web_https_form_plan

            try:
                verify_web_https_form_plan(self.remote_entry_profiles, self.remote_entry_task,
                                           self.remote_form_plan)
                if self.remote_form_cookie is not None:
                    from .web_https_preflight import validate_https_cookie_header
                    import hashlib

                    if (self.remote_form_public_plan_sha256 is None
                            or hashlib.sha256(validate_https_cookie_header(
                                self.remote_form_cookie).encode('ascii')).hexdigest()
                            != self.remote_form_cookie_sha256):
                        raise ValueError('HTTPS form cookie changed')
                if self.remote_form_state_plan is not None:
                    from .web_https_form_state_probe import verify_web_https_form_state_plan

                    verify_web_https_form_state_plan(
                        self.remote_entry_profiles, self.remote_entry_task,
                        self.remote_form_plan, self.remote_form_state_plan,
                        self.remote_form_tls_context,
                        confirm_public_form_plan_sha256=self.remote_form_public_plan_sha256,
                        confirm_public_state_plan_sha256=self.remote_form_public_state_plan_sha256,
                        owned_form_target=self.remote_form_owned_target)
                if self.remote_form_skill_invocation is not None:
                    if (not callable(self.remote_form_skill_revalidator)
                            or digest(self.remote_form_skill_revalidator().model_dump(mode='json'))
                            != self.remote_form_skill_invocation_sha256):
                        raise ValueError('remote_form_skill_invocation_sources_changed')
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form plan differs') from error
        self._local_navigation_runtime_id = None
        self._local_staging_runtime_id = None
        self._remote_entry_runtime_id = None
        self._remote_entry_draft = None
        self._remote_routes_runtime_id = None
        self._remote_routes_draft = None
        self._remote_static_assets_runtime_id = None
        self._remote_static_assets_draft = None
        self._remote_form_runtime_id = None
        self._remote_form_draft = None
        if self.remote_form_owned_fixture is not None and not candidate_start:
            self._owned_form_lifecycle = 'running'
        self.job_id = identifier('job')
        grant = TaskApprovalGrant(identifier('grant'), self.job_id, kind, self.controller.session_id,
                                  lease_id, generation, desktop['runtime_id'], time.time() + 300,
                                  time.monotonic() + 300) if approve_all else None
        followup_binding = None
        guidance_binding = None
        reuse_binding = None
        knowledge_binding = None
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_inputs SET status='cancelled' WHERE session_id=? AND status='queued'", (self.controller.session_id,))
            self.store.insert('desktop_tasks', job_id=self.job_id, session_id=self.controller.session_id,
                              kind=kind, lease_id=lease_id, generation=generation, status='queued',
                              real_model=int(self.engine.identity['real_model'] and (
                                  kind != 'vision_canvas' or self.vision_supervisor.identity['real_model'])), created_at=now(), updated_at=now())
            if failure_followup is not None:
                followup_binding = self.failure_followups.admit(
                    self.store, failure_followup, self.job_id, self.failure_followup_target(kind))
            if failure_guidance is not None:
                guidance_binding = self.failure_guidance.admit(
                    self.store, failure_guidance, failure_followup, self.job_id, self.failure_followup_target(kind))
            if hello_guidance_reuse is not None:
                reuse_binding = self.hello_guidance_reuse.admit(
                    self.store, hello_guidance_reuse, failure_guidance, self.job_id, self.failure_followup_target(kind))
            if task_knowledge is not None:
                knowledge_binding = self.task_knowledge.admit(
                    self.store, task_knowledge, self.job_id, self.failure_followup_target(kind))
            if grant:
                self.grant_event(grant, 'granted', 'explicit_task_start')
            if learning_metadata:
                self.store.insert('desktop_events', event_id=identifier('event'),
                                  session_id=self.controller.session_id, kind='learning_stream_opt_in',
                                  payload_json=canonical({'job_id': self.job_id, 'roles': list(LEARNING_ROLES),
                                                          'synthetic': True, 'collection_authorized': False,
                                                          'training_ready': False}), created_at=now())
        self._approval_grant = grant
        if followup_binding is not None:
            self._failure_followup_jobs[self.job_id] = followup_binding
        if guidance_binding is not None:
            self._failure_guidance_jobs[self.job_id] = guidance_binding
        if reuse_binding is not None:
            self._hello_guidance_reuse_jobs[self.job_id] = reuse_binding
        if knowledge_binding is not None:
            self._task_knowledge_jobs[self.job_id] = knowledge_binding
        self._grant_uses = 0
        if learning_metadata:
            self._learning_opted_jobs.add(self.job_id)
        self.task = asyncio.create_task(self.run(self.job_id, lease_id, kind))
        return {'job_id': self.job_id}

    def update(self, job_id, status):
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_tasks SET status=?,updated_at=? WHERE job_id=?', (status, now(), job_id))

    async def run(self, job_id, lease_id, kind, resume_state=None):
        run_engine = self.engine
        adapter_execution = self._active_owned_candidate_execution
        adapter_run = (adapter_execution is not None and adapter_execution.get('job_id') == job_id
                       and adapter_execution.get('adapter_admission_sha256') is not None)
        evaluation = getattr(self.owned_episode_learning, 'adapter_evaluation', None)
        pair_run = evaluation is not None and job_id in evaluation.jobs
        def created(state: State):
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_tasks SET run_id=?,runtime_id=?,status='running',updated_at=? WHERE job_id=?",
                                              (state.run_id, state.runtime_id, now(), job_id))
                self.check_lease(job_id)
                if kind == 'browser_local_navigation' and self.local_navigation_pin is not None:
                    self.check_local_navigation_binding(job_id)
                    pin = self.local_navigation_pin
                elif kind == 'browser_staging_workflow' and self.local_staging_pin is not None:
                    self.check_local_staging_binding(job_id)
                    pin = self.local_staging_pin
                else:
                    pin = None
                if kind == 'browser_remote_entry':
                    self.check_remote_entry_binding_preinsert(job_id, state)
                    draft = self._remote_entry_draft
                    self.store.insert('desktop_remote_entry_bindings', job_id=job_id,
                                      run_id=state.run_id, profile_sha256=draft.profile_sha256,
                                      binding_sha256=draft.binding_sha256,
                                      runtime_sha256=draft.runtime_sha256,
                                      draft_json=canonical(draft.model_dump(mode='json')),
                                      browser_runtime_id=self._remote_entry_runtime_id, created_at=now())
                    self.check_remote_entry_binding(job_id)
                if kind == 'browser_remote_routes':
                    self.check_remote_routes_binding_preinsert(job_id, state)
                    draft = self._remote_routes_draft
                    plan = self.remote_routes_plan
                    self.store.insert('desktop_remote_route_bindings', job_id=job_id,
                                      run_id=state.run_id, profile_sha256=draft.profile_sha256,
                                      binding_sha256=draft.binding_sha256,
                                      runtime_sha256=draft.runtime_sha256,
                                      plan_sha256=digest(plan.model_dump()),
                                      draft_json=canonical(draft.model_dump(mode='json')),
                                      plan_json=canonical(plan.model_dump(mode='json')),
                                      browser_runtime_id=self._remote_routes_runtime_id,
                                      created_at=now())
                    self.check_remote_routes_binding(job_id)
                if kind == 'browser_remote_static_assets':
                    self.check_remote_static_assets_binding_preinsert(job_id, state)
                    draft = self._remote_static_assets_draft
                    plan = self.remote_static_assets_plan
                    self.store.insert('desktop_remote_static_asset_bindings',
                                      job_id=job_id, run_id=state.run_id,
                                      profile_sha256=draft.profile_sha256,
                                      binding_sha256=draft.binding_sha256,
                                      runtime_sha256=draft.runtime_sha256,
                                      plan_sha256=digest(plan.model_dump()),
                                      draft_json=canonical(draft.model_dump(mode='json')),
                                      plan_json=canonical(plan.model_dump(mode='json')),
                                      browser_runtime_id=self._remote_static_assets_runtime_id,
                                      created_at=now())
                    self.check_remote_static_assets_binding(job_id)
                if kind == 'browser_remote_form':
                    self.check_remote_form_binding_preinsert(job_id, state)
                    draft = self._remote_form_draft
                    plan = self.remote_form_plan
                    self.store.insert('desktop_remote_form_bindings', job_id=job_id,
                                      run_id=state.run_id, profile_sha256=draft.profile_sha256,
                                      binding_sha256=draft.binding_sha256,
                                      runtime_sha256=draft.runtime_sha256,
                                      plan_sha256=digest(plan.model_dump()),
                                      draft_json=canonical(draft.model_dump(mode='json')),
                                      plan_json=canonical(plan.model_dump(mode='json')),
                                      browser_runtime_id=self._remote_form_runtime_id,
                                      created_at=now())
                    if self.remote_form_state_plan is not None:
                        state_plan = self.remote_form_state_plan
                        self.store.insert('desktop_remote_form_state_bindings',
                                          job_id=job_id, run_id=state.run_id,
                                          form_plan_sha256=digest(plan.model_dump()),
                                          state_plan_sha256=digest(state_plan.model_dump()),
                                          state_plan_json=canonical(state_plan.model_dump(mode='json')),
                                          browser_runtime_id=self._remote_form_runtime_id,
                                          created_at=now())
                    if self.remote_form_cookie is not None:
                        self.store.insert('desktop_remote_form_cookie_bindings',
                                          job_id=job_id, run_id=state.run_id,
                                          form_plan_sha256=digest(plan.model_dump()),
                                          cookie_sha256=self.remote_form_cookie_sha256,
                                          browser_runtime_id=self._remote_form_runtime_id,
                                          created_at=now())
                    self.check_remote_form_binding(job_id)
                    if self.remote_form_candidate_admission is not None:
                        from .site_skill_form_recipe_candidate_execution import candidate_admission_payload

                        self.store.insert('observations', observation_id=identifier('observation'),
                                          run_id=state.run_id, step_id=state.step_id,
                                          action_id=None, kind='skill.recipe_candidate_admission',
                                          payload_json=canonical(candidate_admission_payload(
                                              self.remote_form_candidate_admission)),
                                          created_at=now())
                    candidate_execution = self._owned_candidate_execution
                    if (candidate_execution is not None
                            and candidate_execution.get('job_id') == job_id
                            and candidate_execution.get('review_sha256') is not None):
                        review_sha256 = candidate_execution['review_sha256']
                        self._assert_candidate_review_for_state(
                            review_sha256, candidate_execution)
                        review_payload = {
                            'schema_version': '1.0', 'synthetic': True,
                            'review_sha256': review_sha256,
                            'candidate_execution_sha256': candidate_execution[
                                'candidate_execution_sha256'],
                            'candidate_sha256': candidate_execution['candidate_sha256'],
                            'source_run_ref': candidate_execution['source_run_ref'],
                            'review_status': 'accepted',
                            'review_admission_verified': True,
                            'activation_authorized': False,
                            'training_ready': False,
                            'independent_held_out': False,
                        }
                        from .dataset import validator
                        validator('site_skill_recipe_candidate_review_admission').validate(
                            review_payload)
                        self.store.insert(
                            'observations', observation_id=identifier('observation'),
                            run_id=state.run_id, step_id=state.step_id,
                            action_id=None,
                            kind='skill.recipe_candidate_review_admission',
                            payload_json=canonical(review_payload), created_at=now())
                    if (candidate_execution is not None
                            and candidate_execution.get('job_id') == job_id
                            and candidate_execution.get('release_sha256') is not None):
                        release_payload = {
                            'schema_version': '1.0', 'synthetic': True,
                            'candidate_execution_sha256': candidate_execution[
                                'candidate_execution_sha256'],
                            'candidate_sha256': candidate_execution['candidate_sha256'],
                            'invocation_sha256': candidate_execution['invocation_sha256'],
                            'review_sha256': candidate_execution['review_sha256'],
                            'release_sha256': candidate_execution['release_sha256'],
                            'selection_sha256': candidate_execution['selection_sha256'],
                            'family_sha256': candidate_execution['family_sha256'],
                            'selection_admission_verified': True,
                            'activation_authorized': False,
                            'training_ready': False,
                            'independent_held_out': False,
                        }
                        from .dataset import validator
                        validator('site_skill_recipe_candidate_release_admission').validate(
                            release_payload)
                        self.store.insert(
                            'observations', observation_id=identifier('observation'),
                            run_id=state.run_id, step_id=state.step_id, action_id=None,
                            kind='skill.owned_release_selection_admission',
                            payload_json=canonical(release_payload), created_at=now())
                    if (candidate_execution is not None
                            and candidate_execution.get('job_id') == job_id
                            and candidate_execution.get('reuse_admission_sha256') is not None):
                        reuse_payload = self._owned_reuse_admission_observation(
                            candidate_execution, self._owned_skill_reuse,
                            self.controller.session_id)
                        from .dataset import validator
                        validator('site_skill_owned_reuse_admission').validate(
                            reuse_payload)
                        self.store.insert(
                            'observations', observation_id=identifier('observation'),
                            run_id=state.run_id, step_id=state.step_id, action_id=None,
                            kind='skill.owned_reuse_admission',
                            payload_json=canonical(reuse_payload), created_at=now())
                    if (candidate_execution is not None
                            and candidate_execution.get('job_id') == job_id
                            and candidate_execution.get('planning_bundle_sha256') is not None):
                        from .owned_skill_plan_admission import planning_admission

                        payload = planning_admission(candidate_execution,
                                                     candidate_execution['planning_bundle'])
                        self.store.insert(
                            'observations', observation_id=identifier('observation'),
                            run_id=state.run_id, step_id=state.step_id, action_id=None,
                            kind='skill.owned_planning_admission',
                            payload_json=canonical(payload), created_at=now())
                    if adapter_run:
                        from .owned_adapter_admission import runtime_admission_observation

                        payload = runtime_admission_observation(
                            candidate_execution | {'run_id': state.run_id},
                            candidate_execution['adapter_admission'], self._owned_skill_reuse['admission'])
                        self.store.insert(
                            'observations', observation_id=identifier('observation'),
                            run_id=state.run_id, step_id=state.step_id, action_id=None,
                            kind='model.owned_adapter_admission',
                            payload_json=canonical(payload), created_at=now())
                if pair_run:
                    evaluation.created(job_id, state)
                if pin is not None:
                    browser_runtime_id = (self._local_navigation_runtime_id if kind == 'browser_local_navigation'
                                          else self._local_staging_runtime_id)
                    if state.runtime_id != browser_runtime_id:
                        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic profile run runtime differs')
                    pin_record = pin.model_dump(mode='json')
                    self.store.insert('desktop_web_profile_bindings', job_id=job_id, run_id=state.run_id,
                                      profile_sha256=pin.profile_sha256, task_key=pin.task_key,
                                      pin_json=canonical(pin_record), pin_sha256=digest(pin_record),
                                      browser_runtime_id=browser_runtime_id, created_at=now())
                followup_binding = self._failure_followup_jobs.get(job_id)
                if followup_binding is not None:
                    self.failure_followups.created(
                        self.store, followup_binding['intent_sha256'], job_id, state,
                        self.failure_followup_target(kind))
            if self._approval_grant is not None:
                grant = self._approval_grant
                if (grant.job_id != job_id or grant.run_id is not None or not self.grant_current()
                        or state.task_kind != grant.kind or state.owner_lease_id != grant.lease_id):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task delegation cannot bind this run')
                bound = replace(grant, run_id=state.run_id, task_id=state.task_id, runtime_id=state.runtime_id)
                with self.store.connection:
                    self.grant_event(bound, 'bound', 'run_created')
                self._approval_grant = bound

        runtime = self.active_runtime if resume_state else None
        retained = False
        completed = False
        succeeded = False
        try:
            self.release_completed_runtime()
            if runtime is None:
                if kind == 'hello':
                    runtime = self.controller.runtime
                elif kind == 'vision_canvas':
                    if self.desktop_vision:
                        from .desktop_vision import DesktopVisionRuntime

                        runtime = DesktopVisionRuntime(self.controller.runtime)
                    else:
                        runtime = VisionRuntime(self.browser_manifest)
                elif kind == 'browser_staging_workflow':
                    from .desktop_mcp import DesktopMCPBrowserRuntime

                    runtime = DesktopMCPBrowserRuntime(
                        self.controller.runtime, self.desktop_staging_mcp_manifest,
                        fixture_request_guard=True, staging_workflow=True,
                        fixture_port=self.staging_fixture_port)
                elif kind == 'browser_remote_entry':
                    from .desktop_remote_mcp import DesktopRemoteEntryMCPRuntime
                    from .web_application_binding import bind_web_task

                    runtime = DesktopRemoteEntryMCPRuntime(self.controller.runtime,
                                                            self.remote_entry_mcp_manifest)
                    self._remote_entry_draft = bind_web_task(
                        self.remote_entry_profiles, self.remote_entry_task, runtime.runtime_pin())
                    self._remote_entry_runtime_id = runtime.runtime_id
                elif kind == 'browser_remote_routes':
                    from .desktop_readonly_mcp import DesktopReadOnlyRoutesMCPRuntime
                    from .web_application_binding import bind_web_task

                    runtime = DesktopReadOnlyRoutesMCPRuntime(self.controller.runtime,
                                                               self.remote_entry_mcp_manifest)
                    self._remote_routes_draft = bind_web_task(
                        self.remote_entry_profiles, self.remote_entry_task, runtime.runtime_pin())
                    self._remote_routes_runtime_id = runtime.runtime_id
                elif kind == 'browser_remote_static_assets':
                    from .desktop_static_bundle_mcp import DesktopStaticBundleMCPRuntime
                    from .web_application_binding import bind_web_task

                    runtime = DesktopStaticBundleMCPRuntime(
                        self.controller.runtime, self.remote_entry_mcp_manifest)
                    self._remote_static_assets_draft = bind_web_task(
                        self.remote_entry_profiles, self.remote_entry_task,
                        runtime.runtime_pin())
                    self._remote_static_assets_runtime_id = runtime.runtime_id
                elif kind == 'browser_remote_form':
                    from .desktop_form_mcp import DesktopHTTPSFormMCPRuntime
                    from .web_application_binding import bind_web_task

                    runtime = DesktopHTTPSFormMCPRuntime(self.controller.runtime,
                                                         self.remote_entry_mcp_manifest)
                    self._remote_form_draft = bind_web_task(
                        self.remote_entry_profiles, self.remote_entry_task, runtime.runtime_pin())
                    self._remote_form_runtime_id = runtime.runtime_id
                elif ((kind == 'browser_local_navigation' and self.desktop_navigation_mcp_manifest is not None)
                      or self.desktop_mcp_manifest is not None):
                    from .desktop_mcp import DesktopMCPBrowserRuntime

                    runtime = DesktopMCPBrowserRuntime(
                        self.controller.runtime, self.desktop_navigation_mcp_manifest or self.desktop_mcp_manifest,
                        fixture_request_guard=kind == 'browser_local_navigation',
                        fixture_port=self.local_navigation_pin.fixture_port
                        if kind == 'browser_local_navigation' and self.local_navigation_pin is not None else None)
                elif self.desktop_browser:
                    from .desktop_browser import DesktopBrowserRuntime

                    runtime = DesktopBrowserRuntime(self.controller.runtime)
                else:
                    runtime = BrowserRuntime(self.browser_manifest)
            self.active_runtime = runtime
            self.check_lease(job_id)
            if adapter_run:
                if isinstance(self.engine, ReusableDeciderEngine):
                    await self.engine.close()
                run_engine = self.owned_episode_learning.adapter_runtime.engine_for_job(job_id)
            elif pair_run:
                if isinstance(self.engine, ReusableDeciderEngine):
                    await self.engine.close()
                run_engine = evaluation.base_engine(job_id)
            elif isinstance(self.engine, ReusableDeciderEngine):
                if kind == 'vision_canvas' and self.engine.gpu_idle:
                    await self.engine.close()
                    self.engine.begin_job()
                elif self.engine.cpu_prewarm:
                    self.engine.begin_job()
                elif kind == 'vision_canvas':
                    self.engine.prepare_in_background()
            if kind not in {'hello', 'browser_remote_entry', 'browser_remote_routes',
                            'browser_remote_static_assets', 'browser_remote_form'} and resume_state is None:
                startup = asyncio.create_task(asyncio.to_thread(runtime.start))
                try:
                    await asyncio.shield(startup)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await startup
                    raise
            if kind == 'browser_local_navigation' and self.local_navigation_pin is not None:
                from .local_navigation_admission import check_synthetic_navigation_runtime

                report = check_synthetic_navigation_runtime(
                    self.local_navigation_profiles, self.local_navigation_pin, runtime)
                if (resume_state is not None and self._local_navigation_runtime_id != report['browser_runtime_id']):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Paused synthetic navigation runtime changed')
                self._local_navigation_runtime_id = report['browser_runtime_id']
            if kind == 'browser_staging_workflow' and self.local_staging_pin is not None:
                from .local_navigation_admission import check_synthetic_staging_runtime

                report = check_synthetic_staging_runtime(
                    self.local_staging_profiles, self.local_staging_pin, runtime)
                if (resume_state is not None and self._local_staging_runtime_id != report['browser_runtime_id']):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Paused synthetic staging runtime changed')
                self._local_staging_runtime_id = report['browser_runtime_id']
            from .local_navigation_operator import LocalNavigationOperator
            from .staging_workflow_operator import StagingWorkflowOperator
            from .remote_entry_operator import RemoteEntryOperator
            from .remote_routes_operator import RemoteRoutesOperator
            from .remote_static_assets_operator import RemoteStaticAssetsOperator
            from .remote_form_operator import RemoteFormOperator

            operator_class = {'hello': Operator, 'browser_form': BrowserOperator, 'vision_canvas': VisionOperator,
                              'browser_local_navigation': LocalNavigationOperator,
                              'browser_staging_workflow': StagingWorkflowOperator,
                              'browser_remote_entry': RemoteEntryOperator,
                              'browser_remote_routes': RemoteRoutesOperator,
                              'browser_remote_static_assets': RemoteStaticAssetsOperator,
                              'browser_remote_form': RemoteFormOperator}[kind]
            operator = (RemoteStaticAssetsOperator(
                            self.settings, self.store, runtime, run_engine,
                            self.remote_entry_profiles, self._remote_static_assets_draft,
                            self.remote_static_assets_plan,
                            tls_context=self.remote_static_tls_context,
                            review_source=self.remote_json_review_source)
                        if kind == 'browser_remote_static_assets' else
                        operator_class(self.settings, self.store, runtime, run_engine,
                                       self.remote_entry_profiles,
                                       self._remote_entry_draft if kind == 'browser_remote_entry'
                                       else self._remote_routes_draft if kind == 'browser_remote_routes'
                                       else self._remote_form_draft,
                                       *([self.remote_routes_plan] if kind == 'browser_remote_routes'
                                         else [self.remote_form_plan, self.remote_form_field_name,
                                               self.remote_form_value, self.remote_form_tls_context,
                                               self.remote_form_public_plan_sha256,
                                               self.remote_form_state_plan,
                                               self.remote_form_public_state_plan_sha256,
                                               self.remote_form_cookie,
                                               self.remote_form_cookie_sha256,
                                               self.remote_form_skill_invocation,
                                               self.remote_form_skill_invocation_sha256]
                                         if kind == 'browser_remote_form' else []),
                                       **({'fields': [{'name': name, 'value': content}
                                                     for name, content in self.remote_form_fields]}
                                          if kind == 'browser_remote_form'
                                          and self.remote_form_field_name is None else {}),
                                       **({'owned_form_target': self.remote_form_owned_target}
                                          if kind == 'browser_remote_form'
                                          and self.remote_form_owned_target is not None else {}),
                                       **({'review_source': self.remote_route_review_source}
                                          if kind == 'browser_remote_routes' else {}))
                        if kind in {'browser_remote_entry', 'browser_remote_routes',
                                    'browser_remote_form'} else
                        operator_class(self.settings, self.store, runtime, run_engine,
                                       self.vision_supervisor if kind == 'vision_canvas' else None))
            self.operator = operator
            if job_id in self._task_knowledge_jobs:
                from .task_knowledge import TaskKnowledgeContext

                operator.task_decision_context = TaskKnowledgeContext(self, job_id)
            operator.gateway = LeasedGateway(self, job_id, runtime)
            operation = getattr(operator, {'hello': 'hello', 'browser_form': 'form', 'vision_canvas': 'canvas',
                                           'browser_local_navigation': 'navigate',
                                           'browser_staging_workflow': 'workflow',
                                           'browser_remote_entry': 'entry',
                                           'browser_remote_routes': 'routes',
                                           'browser_remote_static_assets': 'assets',
                                           'browser_remote_form': 'form'}[kind])
            context_arguments = {}
            if job_id in self._failure_guidance_jobs:
                from .failure_guidance import FailureGuidanceContext

                context_arguments['decision_context'] = FailureGuidanceContext(self, job_id)
            result = await operation(owner_lease_id=lease_id, on_created=created,
                                     execution_gate=lambda action: self.authorize(job_id, action),
                                     resume_state=resume_state, **context_arguments)
            self.update(job_id, result['status'])
            succeeded = result['status'] == 'succeeded'
            if kind == 'browser_remote_form' and self.remote_form_owned_fixture is not None:
                if succeeded:
                    owned_row = self.store.connection.execute(
                        'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
                    self.remote_form_owned_fixture.verify_complete()
                    if (self._owned_candidate_execution is not None
                            and self._owned_candidate_execution.get('job_id') == job_id):
                        execution = self._owned_candidate_execution
                        execution['run_id'] = owned_row['run_id'] if owned_row else None
                        execution['run_ref'] = (digest({'run_id': execution['run_id']})
                                                if execution['run_id'] else None)
                        from .owned_form_candidate_execution import (
                            persist_candidate_execution_completion)
                        persist_candidate_execution_completion(
                            execution['bundle_directory'],
                            execution['candidate_execution_sha256'],
                            execution['candidate_sha256'],
                            execution['invocation_sha256'], execution['job_id'],
                            execution['run_id'])
                        execution['lifecycle'] = 'completed'
                        self._close_candidate_execution_fixture()
                    else:
                        self._owned_form_run_id = owned_row['run_id'] if owned_row else None
                        self._owned_form_source_job_id = job_id
                        self._consume_owned_form('completed')
                else:
                    if (self._owned_candidate_execution is not None
                            and self._owned_candidate_execution.get('job_id') == job_id):
                        self._owned_candidate_execution['lifecycle'] = 'failed'
                        self._close_candidate_execution_fixture()
                    else:
                        self._consume_owned_form('failed')
            completed = result['status'] == 'succeeded' and (
                self.desktop_browser and kind in {'browser_form', 'browser_local_navigation', 'browser_staging_workflow', 'browser_remote_entry', 'browser_remote_routes', 'browser_remote_static_assets', 'browser_remote_form'}
                or self.desktop_vision and kind == 'vision_canvas')
        except asyncio.CancelledError:
            row = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            retained = bool(kind not in {'browser_remote_entry', 'browser_remote_routes',
                                         'browser_remote_static_assets', 'browser_remote_form'}
                            and self.pause_requested and row['run_id']
                            and self.store.state(row['run_id']).phase == Phase.PAUSED)
            self.update(job_id, 'paused' if retained else 'cancelled')
            if self.remote_form_owned_fixture is not None:
                if (self._owned_candidate_execution is not None
                        and self._owned_candidate_execution.get('job_id') == job_id):
                    self._owned_candidate_execution['lifecycle'] = 'failed'
                    self._close_candidate_execution_fixture()
                else:
                    self._consume_owned_form('failed')
            raise
        except Exception:
            self.update(job_id, 'failed')
            if self.remote_form_owned_fixture is not None:
                if (self._owned_candidate_execution is not None
                        and self._owned_candidate_execution.get('job_id') == job_id):
                    self._owned_candidate_execution['lifecycle'] = 'failed'
                    self._close_candidate_execution_fixture()
                else:
                    self._consume_owned_form('failed')
            row = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
            if row and row['run_id']:
                state = self.store.state(row['run_id'])
                if state.phase not in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                    failed = state.advance(Phase.FAILED, owner='PAUSED', owner_lease_id=identifier('revoked'))
                    self.store.save_state(state, failed)
                    self.store.finish(failed, 'failed', 'unknown')
        finally:
            if job_id in self._learning_opted_jobs:
                try:
                    row = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
                    if row and row['run_id']:
                        self._learning_poll(job_id, row['run_id'], 'settled')
                except Exception:
                    self._learning_failed_jobs.add(job_id)
                    self._learning_unpersisted[job_id] = {
                        'enabled': True, 'state': 'failed_unpersisted', 'reason': 'audit_unavailable'}
            if job_id in self._remote_learning_attached:
                try:
                    row = self.store.connection.execute(
                        'SELECT run_id FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
                    if row and row['run_id']:
                        self._remote_learning_poll(job_id, row['run_id'], 'settled')
                except Exception:
                    self._remote_learning_failed.add(job_id)
                    self._remote_learning_unpersisted[job_id] = {
                        'enabled': True, 'state': 'failed_unpersisted',
                        'reason': 'audit_unavailable'}
            try:
                self.clear_grant('task_finished')
            finally:
                if adapter_run or pair_run:
                    if run_engine is not self.engine:
                        await run_engine.close()
                elif isinstance(self.engine, ReusableDeciderEngine):
                    if (succeeded and kind != 'vision_canvas' and self.engine.gpu_idle_seconds
                            and self.controller.state()['owner'] == 'AGENT'
                            and self.controller.state()['status'] == 'running'):
                        try:
                            await self.engine.park_gpu()
                        except Exception:
                            await self.engine.close()
                    else:
                        await self.engine.close()
                if completed:
                    self.completed_runtime = runtime
                elif runtime is not None and kind != 'hello' and not retained:
                    runtime.stop()
                if not retained:
                    self.active_runtime = None
                    self.operator = None
                    self._local_navigation_runtime_id = None
                    self._local_staging_runtime_id = None
                    self._remote_entry_runtime_id = None
                    self._remote_entry_draft = None
                    self._remote_routes_runtime_id = None
                    self._remote_routes_draft = None
                    self._remote_static_assets_runtime_id = None
                    self._remote_static_assets_draft = None
                    self._remote_form_runtime_id = None
                    self._remote_form_draft = None
                with self.store.connection:
                    self.store.connection.execute("UPDATE desktop_approvals SET status='revoked',updated_at=? WHERE job_id=? AND status IN ('pending','approved')", (now(), job_id))
                self.answer = None
        if succeeded and not adapter_run and not pair_run and isinstance(self.engine, ReusableDeciderEngine) and not self.closed:
            desktop = self.controller.state()
            if desktop['owner'] == 'AGENT' and desktop['status'] == 'running' and self.job_id == job_id:
                self.engine.prewarm_idle()

    def _consume_owned_form(self, lifecycle: str) -> None:
        if self.remote_form_owned_fixture is None:
            return
        self._owned_form_lifecycle = lifecycle
        self.remote_form_owned_target.revoke()
        self.remote_form_owned_fixture.close()

    def restore_owned_skill_reuse(self, context, admission, workspace_lock, *, admission_file):
        """Restore audited source identity only; never restore its task or runtime."""
        from pathlib import Path

        from .owned_skill_reuse_admission import validate_owned_skill_reuse_admission
        from .owned_skill_reuse import stable_source_audit_sha256

        if (self.busy or self.reserved or not isinstance(context, dict)
                or not isinstance(admission, dict) or workspace_lock is None):
            raise ValueError('owned_skill_reuse_restore_unavailable')
        admission = validate_owned_skill_reuse_admission(admission)
        preview = admission['preview']
        state = self.controller.state()
        if (admission['desktop_session_id'] != state['session_id']
                or admission['runtime_id'] != state['runtime_id']
                or admission['lease_id'] != state['lease_id']
                or admission['generation'] != state['generation']
                or admission['manager_session'] != Path(admission_file).absolute().parent.name
                or self.remote_form_owned_candidate_session is not None
                and self.remote_form_owned_candidate_session is not context.get('candidate_session')):
            raise ValueError('owned_skill_reuse_controller_binding_changed')
        workspace_lock.assert_current()
        session = context.get('candidate_session')
        source = context.get('source')
        candidate = context.get('candidate')
        if (session is None or not isinstance(source, dict) or not isinstance(candidate, dict)
                or source.get('manifest_sha256') != preview['source_manifest_sha256']
                or source.get('invocation_sha256') != preview['source_invocation_sha256']
                or context.get('source_run_ref') != preview['source_run_ref']
                or context.get('source_run_id') is None
                or context.get('source_desktop_session_id') != preview['source_desktop_session_id']
                or candidate.get('source_fingerprint_sha256') != preview['source_fingerprint_sha256']):
            raise ValueError('owned_skill_reuse_source_context_changed')
        if (self.remote_form_owned_manifest is None
                or self.remote_form_owned_manifest.get('mode')
                != 'owned_synthetic_form_invocation'
                or self.remote_form_owned_manifest.get('invocation_sha256')
                != preview['source_invocation_sha256']
                or self.remote_form_owned_manifest.get('manifest_sha256')
                not in {None, preview['source_manifest_sha256']}):
            raise ValueError('owned_skill_reuse_source_manifest_changed')
        if not callable(self.remote_form_owned_auditor):
            raise ValueError('owned_skill_reuse_source_auditor_missing')
        report = self.remote_form_owned_auditor(context['source_run_id'])
        if (stable_source_audit_sha256(report) != preview['source_audit_sha256']
                or report.get('run_ref') != preview['source_run_ref']
                or report.get('invocation_sha256') != preview['source_invocation_sha256']):
            raise ValueError('owned_skill_reuse_source_audit_changed')
        job = self.store.connection.execute(
            'SELECT job_id,session_id,kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (preview['source_job_id'],)).fetchone()
        if (job is None or job['session_id'] != preview['source_desktop_session_id']
                or job['kind'] != 'browser_remote_form' or job['status'] != 'succeeded'
                or job['run_id'] != context['source_run_id']):
            raise ValueError('owned_skill_reuse_source_job_changed')
        rechecked_candidate, rechecked_sha256 = session.reinspect(
            preview['source_run_ref'], preview['source_invocation_sha256'],
            preview['candidate_sha256'])
        if (rechecked_sha256 != preview['candidate_sha256']
                or rechecked_candidate.get('source_fingerprint_sha256')
                != preview['source_fingerprint_sha256']):
            raise ValueError('owned_skill_reuse_candidate_changed')
        _rechecked, checked_source = session.execution_source(
            context['source_run_id'], preview['source_invocation_sha256'],
            rechecked_sha256)
        if (checked_source.get('manifest_sha256') != preview['source_manifest_sha256']
                or checked_source.get('invocation_sha256')
                != preview['source_invocation_sha256']):
            raise ValueError('owned_skill_reuse_source_changed')
        from .owned_candidate_review import inspect_owned_candidate_review_evidence
        from .owned_skill_release import current_owned_skill_selection, load_owned_skill_release

        release = load_owned_skill_release(session.directory, preview['release_sha256'])
        head, selection = current_owned_skill_selection(
            session.directory, preview['family_sha256'])
        review = inspect_owned_candidate_review_evidence(
            session.directory, preview['review_sha256'], candidate_session=session,
            database=self.settings.database)
        receipt = review['receipt']
        if (head is None or selection is None
                or head['selection_sha256'] != preview['selection_sha256']
                or selection['release_sha256'] != preview['release_sha256']
                or review.get('status') != 'accepted'
                or release['candidate_sha256'] != preview['candidate_sha256']
                or release['recipe_sha256'] != preview['recipe_sha256']
                or release['review_sha256'] != preview['review_sha256']
                or any(receipt.get(key) != expected for key, expected in (
                    ('candidate_sha256', preview['candidate_sha256']),
                    ('source_run_ref', preview['source_run_ref']),
                    ('source_invocation_sha256', preview['source_invocation_sha256']),
                    ('source_group_sha256', rechecked_candidate['source_group_sha256']),
                    ('source_fingerprint_sha256', preview['source_fingerprint_sha256']),
                    ('profile_sha256', rechecked_candidate['profile_sha256']),
                    ('skill_sha256', rechecked_candidate['recipe']['skill_sha256']),
                    ('recipe_sha256', preview['recipe_sha256']),
                    ('candidate_execution_sha256',
                     preview['review_evidence_execution_sha256'])))):
            raise ValueError('owned_skill_reuse_selected_release_changed')
        from .owned_form_candidate_execution import load_candidate_execution_replay_inventory

        inventory = load_candidate_execution_replay_inventory(
            session.directory, source_run_ref=preview['source_run_ref'],
            source_invocation_sha256=preview['source_invocation_sha256'],
            source_manifest_sha256=preview['source_manifest_sha256'], allow_missing=False)
        if digest({key: sorted(value) for key, value in inventory.items()}) != preview[
                'replay_inventory_sha256']:
            raise ValueError('owned_skill_reuse_replay_inventory_changed')
        admission_file = Path(admission_file).absolute()
        # The caller writes the initial file atomically; read back its exact pin.
        from .owned_skill_reuse_admission import load_owned_skill_reuse_admission
        loaded, admission_sha256 = load_owned_skill_reuse_admission(
            admission_file, digest(admission))
        if loaded != admission:
            raise ValueError('owned_skill_reuse_admission_changed')
        workspace_lock.assert_current()
        if self.remote_form_owned_fixture is not None:
            self.remote_form_owned_target.revoke()
            self.remote_form_owned_fixture.close()
        self.remote_form_owned_fixture = None
        self.remote_form_owned_target = None
        self.remote_form_owned_candidate_session = session
        self._owned_skill_reuse = {'context': context, 'admission': admission,
                                   'admission_sha256': admission_sha256,
                                   'admission_file': admission_file,
                                   'workspace_lock': workspace_lock,
                                   'manager_session': admission['manager_session'],
                                   'source_job_id': preview['source_job_id'],
                                   'source_run_id': context['source_run_id']}
        self._owned_skill_reuse_admission_sha256 = admission_sha256
        self._owned_skill_reuse_admission_file = admission_file
        self._owned_form_run_id = context['source_run_id']
        self._owned_form_source_job_id = preview['source_job_id']
        self._owned_form_audit = report
        self._owned_form_lifecycle = 'audited'
        self._owned_candidate_execution_consumed.update(inventory['direct'])
        self._owned_selected_candidate_execution_consumed.update(inventory['selected'])
        prior_revalidator = self.remote_form_skill_revalidator
        if not callable(prior_revalidator):
            raise ValueError('owned_skill_reuse_invocation_revalidator_missing')

        def revalidate_reuse_source():
            current = prior_revalidator()
            if (digest(current.model_dump(mode='json'))
                    != self.remote_form_owned_manifest.get('invocation_sha256')):
                raise ValueError('owned_skill_reuse_invocation_changed')
            self._assert_owned_skill_reuse_current()
            return current

        self.remote_form_skill_revalidator = revalidate_reuse_source
        return {'reuse_admission_sha256': admission_sha256,
                'source_run_ref': preview['source_run_ref'],
                'source_invocation_sha256': preview['source_invocation_sha256']}

    def _assert_owned_skill_reuse_current(self):
        reuse = self._owned_skill_reuse
        if reuse is None:
            raise ValueError('owned_skill_reuse_admission_missing')
        from .owned_skill_reuse_admission import assert_owned_skill_reuse_current
        admission, _checksum = assert_owned_skill_reuse_current(
            reuse['admission_file'], reuse['admission_sha256'],
            reuse['workspace_lock'], self.controller.state(), reuse['manager_session'])
        preview = admission['preview']
        context = reuse['context']
        session = context['candidate_session']
        report = self.remote_form_owned_auditor(context['source_run_id'])
        from .owned_skill_reuse import stable_source_audit_sha256
        if (stable_source_audit_sha256(report) != preview['source_audit_sha256']
                or report.get('run_ref') != preview['source_run_ref']):
            raise ValueError('owned_skill_reuse_source_audit_changed')
        row = self.store.connection.execute(
            'SELECT session_id,kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (preview['source_job_id'],)).fetchone()
        if (row is None or row['session_id'] != preview['source_desktop_session_id']
                or row['kind'] != 'browser_remote_form' or row['status'] != 'succeeded'
                or row['run_id'] != context['source_run_id']):
            raise ValueError('owned_skill_reuse_source_job_changed')
        candidate, checked_sha256 = session.reinspect(
            preview['source_run_ref'], preview['source_invocation_sha256'],
            preview['candidate_sha256'])
        if (checked_sha256 != preview['candidate_sha256']
                or candidate.get('source_fingerprint_sha256')
                != preview['source_fingerprint_sha256']):
            raise ValueError('owned_skill_reuse_candidate_changed')
        _candidate, checked_source = session.execution_source(
            context['source_run_id'], preview['source_invocation_sha256'], checked_sha256)
        if (checked_source.get('manifest_sha256') != preview['source_manifest_sha256']
                or checked_source.get('invocation_sha256')
                != preview['source_invocation_sha256']):
            raise ValueError('owned_skill_reuse_source_changed')
        from .owned_candidate_review import inspect_owned_candidate_review_evidence
        from .owned_skill_release import current_owned_skill_selection, load_owned_skill_release
        release = load_owned_skill_release(session.directory, preview['release_sha256'])
        head, selection = current_owned_skill_selection(session.directory, preview['family_sha256'])
        review = inspect_owned_candidate_review_evidence(
            session.directory, preview['review_sha256'], candidate_session=session,
            database=self.settings.database)
        receipt = review['receipt']
        if (head is None or selection is None
                or head['selection_sha256'] != preview['selection_sha256']
                or selection['release_sha256'] != preview['release_sha256']
                or review.get('status') != 'accepted'
                or release['candidate_sha256'] != preview['candidate_sha256']
                or release['recipe_sha256'] != preview['recipe_sha256']
                or release['review_sha256'] != preview['review_sha256']
                or any(receipt.get(key) != expected for key, expected in (
                    ('candidate_sha256', preview['candidate_sha256']),
                    ('source_run_ref', preview['source_run_ref']),
                    ('source_invocation_sha256', preview['source_invocation_sha256']),
                    ('source_group_sha256', candidate['source_group_sha256']),
                    ('source_fingerprint_sha256', preview['source_fingerprint_sha256']),
                    ('profile_sha256', candidate['profile_sha256']),
                    ('skill_sha256', candidate['recipe']['skill_sha256']),
                    ('recipe_sha256', preview['recipe_sha256']),
                    ('candidate_execution_sha256',
                     preview['review_evidence_execution_sha256'])))):
            raise ValueError('owned_skill_reuse_selected_release_changed')
        return admission

    def _owned_candidate_execution_status(self):
        execution = self._owned_candidate_execution
        if execution is None:
            return None
        status = {key: execution.get(key) for key in (
            'schema_version', 'mode', 'lifecycle', 'candidate_sha256',
            'candidate_execution_sha256', 'source_run_ref',
            'source_invocation_sha256', 'source_group_sha256', 'profile_sha256',
            'skill_sha256', 'case_key', 'parameter_variant_sha256',
            'invocation_sha256', 'recipe_sha256', 'steps', 'job_id', 'run_id',
            'run_ref', 'report_sha256')}
        if execution.get('lifecycle') == 'previewed':
            status['preview_sha256'] = execution['preview_sha256']
        if execution.get('review_sha256') is not None:
            status.update({'schema_version': '1.1',
                           'review_sha256': execution['review_sha256'],
                           'review_status': execution.get('review_status', 'unavailable')})
        if execution.get('release_sha256') is not None:
            status.update({'schema_version': '1.2',
                           'release_sha256': execution['release_sha256'],
                           'selection_sha256': execution['selection_sha256'],
                           'family_sha256': execution['family_sha256'],
                           'selection_status': self._owned_execution_selection_status(execution)})
        if execution.get('reuse_admission_sha256') is not None:
            status.update({'schema_version': '1.3',
                           'reuse_admission_sha256': execution['reuse_admission_sha256']})
        if execution.get('planning_bundle_sha256') is not None:
            status.update({'schema_version': '1.4',
                           'planning_bundle_sha256': execution['planning_bundle_sha256']})
        if execution.get('adapter_admission_sha256') is not None:
            status.update({'schema_version': '1.5',
                           'adapter_admission_sha256': execution['adapter_admission_sha256']})
        return status

    def _owned_execution_selection_status(self, execution):
        try:
            from .owned_skill_release import current_owned_skill_selection

            session = self.remote_form_owned_candidate_session
            if session is None:
                return 'unavailable'
            head, selection = current_owned_skill_selection(
                session.directory, execution['family_sha256'])
            return ('current' if head is not None
                    and head['selection_sha256'] == execution['selection_sha256']
                    and selection['release_sha256'] == execution['release_sha256']
                    else 'superseded')
        except (OSError, ValueError, TypeError, KeyError):
            return 'unavailable'

    def _close_candidate_execution_fixture(self):
        execution = self._owned_candidate_execution
        fixture = self.remote_form_owned_fixture
        if fixture is not None and execution is not None and fixture is execution.get('fixture'):
            try:
                fixture.target.revoke()
            finally:
                try:
                    fixture.close()
                finally:
                    restore = self._owned_candidate_execution_restore
                    if restore is not None:
                        for name, value in restore.items():
                            setattr(self, name, value)
                        self._owned_candidate_execution_restore = None
                    if execution is not None and execution.get('lifecycle') not in {'audited', 'failed'}:
                        execution['lifecycle'] = 'completed'
                    self._active_owned_candidate_execution = None

    def _owned_candidate_source(self, source_run_ref, invocation_sha256,
                                candidate_sha256):
        from .site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate

        session, run_id = self._owned_form_candidate_run(source_run_ref, invocation_sha256)
        candidate, source = session.execution_source(run_id, invocation_sha256,
                                                     candidate_sha256)
        parsed = parse_site_skill_form_recipe_candidate(candidate)
        return session, run_id, parsed, source

    def prepare_owned_form_candidate_execution(self, candidate_sha256, source_run_ref,
                                               invocation_sha256, case_key,
                                               development_value):
        from .owned_form_candidate_execution import prepare_candidate_form_run

        session, run_id, candidate, source = self._owned_candidate_source(
            source_run_ref, invocation_sha256, candidate_sha256)
        prepared = prepare_candidate_form_run(
            candidate.model_dump(mode='json'), candidate_sha256, source,
            case_key, development_value)
        prepared.update({'session': session, 'source_run_id': run_id,
                         'source': source})
        return prepared

    def _owned_candidate_review_material(self, *, candidate_sha256, source_run_ref,
                                         source_invocation_sha256,
                                         candidate_execution_sha256):
        if (not isinstance(candidate_sha256, str) or len(candidate_sha256) != 64
                or not isinstance(source_run_ref, str) or len(source_run_ref) != 64
                or not isinstance(source_invocation_sha256, str)
                or len(source_invocation_sha256) != 64
                or not isinstance(candidate_execution_sha256, str)
                or len(candidate_execution_sha256) != 64):
            raise ValueError('owned_candidate_review_selection_invalid')
        from .owned_form_candidate_execution import (
            audit_persisted_candidate_execution, load_candidate_execution_bundle)
        from .owned_candidate_review import make_owned_candidate_review

        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_candidate_review_source_unavailable')
        candidate, _source = session.reinspect(
            source_run_ref, source_invocation_sha256, candidate_sha256)
        bundle_root = session.directory / 'candidate-execution-bundles'
        bundle, _manifest_sha256 = load_candidate_execution_bundle(
            bundle_root, candidate_execution_sha256)
        manifest = bundle['manifest']
        completion = bundle.get('completion')
        if (completion is None
                or manifest['candidate_sha256'] != candidate_sha256
                or manifest['source_run_ref'] != source_run_ref
                or manifest['source_invocation_sha256'] != source_invocation_sha256
                or manifest.get('review_sha256') is not None):
            raise ValueError('owned_candidate_review_execution_selection_changed')
        job = self.store.connection.execute(
            'SELECT kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (completion['job_id'],)).fetchone()
        if (job is None or job['kind'] != 'browser_remote_form'
                or job['status'] != 'succeeded' or job['run_id'] != completion['run_id']):
            raise ValueError('owned_candidate_review_execution_not_succeeded')
        report = audit_persisted_candidate_execution(
            bundle_root, candidate_execution_sha256,
            candidate_session=session, database=self.settings.database)
        if (report.get('status') != 'source_bound_development_execution'
                or report.get('source_run_ref') != source_run_ref
                or report.get('execution_run_ref') != completion['run_ref']
                or report.get('admission', {}).get('candidate_sha256') != candidate_sha256
                or report.get('admission', {}).get('invocation_sha256')
                != manifest['invocation_sha256']):
            raise ValueError('owned_candidate_review_execution_audit_changed')
        execution = {
            'candidate_execution_sha256': candidate_execution_sha256,
            'run_ref': completion['run_ref'],
            'invocation_sha256': manifest['invocation_sha256'],
            'recipe_sha256': manifest['recipe_sha256'],
            'skill_sha256': bundle['invocation']['skill_sha256'],
            'profile_sha256': candidate['profile_sha256'],
            'case_key': bundle['invocation']['case_key'],
            'parameter_variant_sha256': manifest['parameter_variant_sha256'],
        }
        review_sha256, receipt = make_owned_candidate_review(
            candidate, candidate_sha256, execution)
        return session, candidate, execution, review_sha256, receipt

    def preview_owned_candidate_review(self, *, candidate_sha256, source_run_ref,
                                       source_invocation_sha256,
                                       candidate_execution_sha256):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Candidate review preview requires an idle scheduler')
        session, _candidate, _execution, review_sha256, receipt = (
            self._owned_candidate_review_material(
                candidate_sha256=candidate_sha256, source_run_ref=source_run_ref,
                source_invocation_sha256=source_invocation_sha256,
                candidate_execution_sha256=candidate_execution_sha256))
        from .owned_candidate_review import inspect_owned_candidate_review

        try:
            stored = inspect_owned_candidate_review(session.directory, review_sha256)
        except FileNotFoundError:
            stored = None
        if stored is not None:
            if stored['receipt'] != receipt:
                raise ValueError('owned_candidate_review_receipt_changed')
            if stored['status'] == 'revoked':
                raise ValueError('owned_candidate_review_cannot_reopen')
        return {'schema_version': '1.0', 'available': True,
                'status': 'preview', 'review_sha256': review_sha256,
                'receipt': receipt, 'summary': receipt['summary'],
                'revocation_sha256': None}

    def accept_owned_candidate_review(self, *, candidate_sha256, source_run_ref,
                                      source_invocation_sha256,
                                      candidate_execution_sha256,
                                      review_sha256, confirm_sha256):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Candidate review acceptance requires an idle scheduler')
        session, _candidate, _execution, expected_sha256, receipt = (
            self._owned_candidate_review_material(
                candidate_sha256=candidate_sha256, source_run_ref=source_run_ref,
                source_invocation_sha256=source_invocation_sha256,
                candidate_execution_sha256=candidate_execution_sha256))
        if review_sha256 != expected_sha256 or confirm_sha256 != expected_sha256:
            raise ValueError('owned_candidate_review_confirmation_invalid')
        from .owned_candidate_review import persist_owned_candidate_review

        return persist_owned_candidate_review(session.directory, receipt,
                                              expected_sha256)

    def inspect_owned_candidate_review(self, review_sha256):
        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_candidate_review_source_unavailable')
        from .owned_candidate_review import inspect_owned_candidate_review_evidence

        return inspect_owned_candidate_review_evidence(
            session.directory, review_sha256, candidate_session=session,
            database=self.settings.database)

    def revoke_owned_candidate_review(self, review_sha256, confirm_sha256):
        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_candidate_review_source_unavailable')
        from .owned_candidate_review import revoke_owned_candidate_review

        result = revoke_owned_candidate_review(session.directory, review_sha256,
                                               confirm_sha256)
        execution = self._active_owned_candidate_execution
        if (execution is not None and execution.get('review_sha256') == review_sha256
                and execution.get('lifecycle') == 'running'):
            execution['review_status'] = 'revoked'
            if self.remote_form_owned_target is not None:
                self.remote_form_owned_target.revoke()
            job_id = execution.get('job_id')
            if job_id is not None:
                row = self.store.connection.execute(
                    "SELECT approval_id,action_sha256 FROM desktop_approvals "
                    "WHERE job_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                    (job_id,)).fetchone()
                if (row is not None and self.answer is not None
                        and not self.answer.done()):
                    self._fail_closed_candidate_form_approval(
                        job_id, row['approval_id'], row['action_sha256'])
        return result

    def preview_owned_form_candidate_execution(self, candidate_sha256,
                                               source_run_ref,
                                               invocation_sha256,
                                               case_key, development_value,
                                               review_sha256=None,
                                               release_sha256=None,
                                               selection_sha256=None,
                                               reuse_admission_sha256=None,
                                               planning_bundle_sha256=None):
        self._assert_manual_planning_boundary(planning_bundle_sha256)
        if (self.busy or self.reserved
                or self._owned_form_lifecycle != 'audited'):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned candidate execution requires an idle audited source')
        prepared = self.prepare_owned_form_candidate_execution(
            candidate_sha256, source_run_ref, invocation_sha256,
            case_key, development_value)
        session = prepared['session']
        if review_sha256 is not None:
            self._assert_candidate_review_for_execution(
                review_sha256, prepared, source_run_ref, invocation_sha256)
        if release_sha256 is not None or selection_sha256 is not None:
            if review_sha256 is None:
                raise ValueError('owned_candidate_release_requires_review')
            self._assert_owned_release_selection(
                release_sha256, selection_sha256, review_sha256, prepared)
        if self._owned_skill_reuse is None:
            if reuse_admission_sha256 is not None:
                raise ValueError('owned_skill_reuse_not_configured')
        else:
            if (release_sha256 is None or reuse_admission_sha256 !=
                    self._owned_skill_reuse_admission_sha256):
                raise ValueError('owned_skill_reuse_pin_required')
            self._assert_owned_skill_reuse_current()
            self._assert_owned_skill_reuse_candidate_binding(
                prepared, review_sha256, release_sha256, selection_sha256)
        preview = self._owned_candidate_execution_preview_payload(
            prepared, candidate_sha256, source_run_ref, invocation_sha256,
            review_sha256=review_sha256, release_sha256=release_sha256,
            selection_sha256=selection_sha256,
            reuse_admission_sha256=reuse_admission_sha256,
            planning_bundle_sha256=planning_bundle_sha256)
        self._owned_candidate_execution = {
            'schema_version': '1.0', 'mode': 'owned_candidate_development',
            'lifecycle': 'previewed', 'candidate_sha256': candidate_sha256,
            'candidate_execution_sha256': None, 'source_run_ref': source_run_ref,
            'source_invocation_sha256': invocation_sha256,
            'source_group_sha256': prepared['source_group_sha256'],
            'profile_sha256': prepared['profile_sha256'],
            'skill_sha256': prepared['skill_sha256'], 'case_key': case_key,
            'parameter_variant_sha256': prepared['parameter_variant_sha256'],
            'invocation_sha256': prepared['invocation_sha256'],
            'recipe_sha256': prepared['recipe_sha256'], 'steps': prepared['steps'],
            'job_id': None, 'run_id': None, 'run_ref': None,
            'report_sha256': None, 'preview_sha256': preview['preview_sha256'],
        }
        if review_sha256 is not None:
            self._owned_candidate_execution.update({
                'schema_version': '1.1', 'review_sha256': review_sha256,
                'review_status': 'accepted'})
        if release_sha256 is not None:
            release = self._load_owned_release(release_sha256)
            self._owned_candidate_execution.update({
                'schema_version': '1.2', 'release_sha256': release_sha256,
                'selection_sha256': selection_sha256,
                'family_sha256': release['family_sha256'],
                'selection_status': 'current'})
        if reuse_admission_sha256 is not None:
            self._owned_candidate_execution.update({
                'schema_version': '1.3',
                'reuse_admission_sha256': reuse_admission_sha256})
        if planning_bundle_sha256 is not None:
            self._owned_candidate_execution.update({
                'schema_version': '1.4', 'planning_bundle_sha256': planning_bundle_sha256})
        return preview

    def _assert_owned_release_selection(self, release_sha256, selection_sha256,
                                        review_sha256, prepared):
        from .owned_skill_release import (
            current_owned_skill_selection, load_owned_skill_release)

        if any(not isinstance(value, str) or len(value) != 64
               for value in (release_sha256, selection_sha256, review_sha256)):
            raise ValueError('owned_skill_selection_pin_invalid')
        session = prepared['session']
        release = load_owned_skill_release(session.directory, release_sha256)
        if (release['candidate_sha256'] != prepared['candidate_sha256']
                or release['recipe_sha256'] != prepared['recipe_sha256']
                or release['skill_sha256'] != prepared['skill_sha256']
                or release['review_sha256'] != review_sha256
                or release['source_run_ref'] != prepared['source_run_ref']
                or release['source_group_sha256'] != prepared['source_group_sha256']):
            raise ValueError('owned_skill_release_candidate_binding_changed')
        head, selection = current_owned_skill_selection(
            session.directory, release['family_sha256'])
        if (head is None or head['selection_sha256'] != selection_sha256
                or selection['release_sha256'] != release_sha256):
            raise ValueError('owned_skill_selection_not_current')
        self._assert_candidate_review_for_state(review_sha256, {
            'candidate_sha256': prepared['candidate_sha256'],
            'source_run_ref': prepared['source_run_ref'],
            'source_invocation_sha256': prepared['source_invocation_sha256'],
            'source_group_sha256': prepared['source_group_sha256'],
            'source_fingerprint_sha256': prepared['candidate']['source_fingerprint_sha256'],
        })
        return release, selection

    def _assert_owned_skill_reuse_candidate_binding(
            self, prepared, review_sha256, release_sha256, selection_sha256):
        reuse = self._owned_skill_reuse
        if reuse is None:
            raise ValueError('owned_skill_reuse_admission_missing')
        preview = reuse['admission']['preview']
        source = prepared['source']
        candidate = prepared['candidate']
        release = self._load_owned_release(release_sha256)
        expected = {
            'source_manifest_sha256': source['manifest_sha256'],
            'source_run_ref': prepared['source_run_ref'],
            'source_invocation_sha256': prepared['source_invocation_sha256'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'family_sha256': release['family_sha256'],
            'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'review_sha256': review_sha256,
            'candidate_sha256': prepared['candidate_sha256'],
            'recipe_sha256': prepared['recipe_sha256'],
        }
        if (any(preview.get(key) != value for key, value in expected.items())
                or release.get('review_sha256') != review_sha256
                or release.get('source_run_ref') != prepared['source_run_ref']
                or release.get('recipe_sha256') != prepared['recipe_sha256']
                or release.get('source_group_sha256') != candidate.get('source_group_sha256')
                or release.get('source_fingerprint_sha256')
                != candidate.get('source_fingerprint_sha256')
                or release.get('family', {}).get('profile_sha256')
                != candidate.get('profile_sha256')
                or release.get('skill_sha256') != prepared.get('skill_sha256')
                or release.get('evidence_execution_sha256')
                != preview.get('review_evidence_execution_sha256')):
            raise ValueError('owned_skill_reuse_candidate_binding_changed')

    def _assert_candidate_review_for_execution(self, review_sha256, prepared,
                                              source_run_ref,
                                              source_invocation_sha256):
        from .owned_candidate_review import inspect_owned_candidate_review_evidence

        candidate = prepared['candidate']
        session = prepared['session']
        result = inspect_owned_candidate_review_evidence(
            session.directory, review_sha256, candidate_session=session,
            database=self.settings.database)
        receipt = result['receipt']
        if (result['status'] != 'accepted'
                or receipt['candidate_sha256'] != prepared['candidate_sha256']
                or receipt['source_run_ref'] != source_run_ref
                or receipt['source_invocation_sha256'] != source_invocation_sha256
                or receipt['source_group_sha256'] != candidate['source_group_sha256']
                or receipt['source_fingerprint_sha256']
                != candidate['source_fingerprint_sha256']):
            raise ValueError('owned_candidate_review_not_currently_accepted')
        return receipt

    def _assert_candidate_review_for_state(self, review_sha256, execution):
        from .owned_candidate_review import inspect_owned_candidate_review_evidence

        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_candidate_review_source_unavailable')
        result = inspect_owned_candidate_review_evidence(
            session.directory, review_sha256, candidate_session=session,
            database=self.settings.database)
        receipt = result['receipt']
        if (result['status'] != 'accepted'
                or receipt['candidate_sha256'] != execution['candidate_sha256']
                or receipt['source_run_ref'] != execution['source_run_ref']
                or receipt['source_invocation_sha256']
                != execution['source_invocation_sha256']
                or receipt['source_group_sha256'] != execution['source_group_sha256']
                or receipt['source_fingerprint_sha256']
                != execution['source_fingerprint_sha256']):
            raise ValueError('owned_candidate_review_not_currently_accepted')
        return receipt

    @staticmethod
    def _owned_candidate_execution_preview_payload(prepared, candidate_sha256,
                                                   source_run_ref, invocation_sha256,
                                                   review_sha256=None,
                                                   release_sha256=None,
                                                   selection_sha256=None,
                                                   reuse_admission_sha256=None,
                                                   planning_bundle_sha256=None,
                                                   adapter_admission_sha256=None):
        if ((release_sha256 is None) != (selection_sha256 is None)
                or release_sha256 is not None and review_sha256 is None):
            raise ValueError('owned_candidate_release_selection_pin_invalid')
        preview = {key: prepared[key] for key in (
            'candidate_sha256', 'source_run_ref', 'source_invocation_sha256',
            'source_group_sha256', 'profile_sha256', 'skill_sha256', 'case_key',
            'parameter_variant_sha256', 'invocation_sha256', 'recipe_sha256',
            'steps', 'purpose', 'independent_held_out')}
        preview.update({
            'schema_version': '1.0', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(prepared['form_plan'].model_dump()),
            'state_plan_sha256': digest(prepared['state_plan'].model_dump()),
            'report': None,
        })
        if review_sha256 is not None:
            preview['schema_version'] = '1.1'
            preview['review_sha256'] = review_sha256
        if release_sha256 is not None:
            preview['schema_version'] = '1.2'
            preview['release_sha256'] = release_sha256
            preview['selection_sha256'] = selection_sha256
        if reuse_admission_sha256 is not None:
            if (release_sha256 is None
                    or re.fullmatch(r'[a-f0-9]{64}', reuse_admission_sha256) is None):
                raise ValueError('owned_skill_reuse_pin_invalid')
            preview['schema_version'] = '1.3'
            preview['reuse_admission_sha256'] = reuse_admission_sha256
        if planning_bundle_sha256 is not None:
            if (reuse_admission_sha256 is None
                    or re.fullmatch(r'[a-f0-9]{64}', planning_bundle_sha256) is None):
                raise ValueError('owned_skill_planning_pin_invalid')
            preview['schema_version'] = '1.4'
            preview['planning_bundle_sha256'] = planning_bundle_sha256
        if adapter_admission_sha256 is not None:
            if (reuse_admission_sha256 is None or planning_bundle_sha256 is not None
                    or re.fullmatch(r'[a-f0-9]{64}', adapter_admission_sha256) is None):
                raise ValueError('owned_adapter_runtime_pin_invalid')
            preview.update({'schema_version': '1.5', 'adapter_admission_sha256': adapter_admission_sha256})
        preview['preview_sha256'] = digest(preview)
        return preview

    def start_owned_form_candidate_execution(self, *, candidate_sha256,
                                             source_run_ref, invocation_sha256,
                                             case_key, development_value,
                                             preview_sha256, confirm_sha256,
                                             lease_id, generation,
                                             review_sha256=None,
                                             release_sha256=None,
                                             selection_sha256=None,
                                             reuse_admission_sha256=None,
                                             planning_bundle_sha256=None,
                                             planning_bundle=None,
                                             adapter_admission_sha256=None,
                                             adapter_admission=None):
        self._assert_manual_planning_boundary(planning_bundle_sha256)
        if adapter_admission_sha256 is not None or adapter_admission is not None:
            learning = self.owned_episode_learning
            if (learning is None or planning_bundle_sha256 is not None
                    or adapter_admission is None or digest(adapter_admission) != adapter_admission_sha256
                    or learning.adapter_runtime.starting != (
                        adapter_admission_sha256, preview_sha256, lease_id, generation)):
                raise ValueError('owned_adapter_runtime_explicit_start_required')
            learning.adapter_runtime.validate(adapter_admission)
        if ((planning_bundle_sha256 is None) != (planning_bundle is None)
                or planning_bundle is not None and digest(planning_bundle) != planning_bundle_sha256):
            raise ValueError('owned_skill_planning_pin_invalid')
        if planning_bundle_sha256 is not None:
            if self._owned_planning_start != (planning_bundle_sha256, preview_sha256, lease_id, generation):
                raise ValueError('owned_skill_plan_explicit_start_required')
            self._owned_planning_start = None
        import hashlib
        import ssl
        from pathlib import Path

        from .owned_form_candidate_execution import (
            bind_exact_loopback_listener, candidate_execution_skill_store_path)
        from .owned_form_fixture import OwnedFormFixture
        from .site_skill import SiteSkillStore
        from .site_skill_form_recipe import compile_site_skill_form_recipe_invocation
        from .site_skill_form_recipe_candidate_execution import (
            audit_candidate_execution, prepare_candidate_execution,
            revalidate_candidate_execution)

        if (self.busy or self.reserved
                or self._owned_form_lifecycle != 'audited'):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned candidate execution requires an idle audited source')
        prepared = self.prepare_owned_form_candidate_execution(
            candidate_sha256, source_run_ref, invocation_sha256,
            case_key, development_value)
        self._restore_owned_candidate_replay_inventory(
            prepared, source_run_ref, invocation_sha256)
        if review_sha256 is not None:
            self._assert_candidate_review_for_execution(
                review_sha256, prepared, source_run_ref, invocation_sha256)
        if release_sha256 is not None or selection_sha256 is not None:
            if review_sha256 is None:
                raise ValueError('owned_candidate_release_requires_review')
            release, selection = self._assert_owned_release_selection(
                release_sha256, selection_sha256, review_sha256, prepared)
        if self._owned_skill_reuse is None:
            if reuse_admission_sha256 is not None:
                raise ValueError('owned_skill_reuse_not_configured')
        else:
            if (release_sha256 is None or reuse_admission_sha256 !=
                    self._owned_skill_reuse_admission_sha256):
                raise ValueError('owned_skill_reuse_pin_required')
            self._assert_owned_skill_reuse_current()
            self._assert_owned_skill_reuse_candidate_binding(
                prepared, review_sha256, release_sha256, selection_sha256)
        checked = self._owned_candidate_execution_preview_payload(
            prepared, candidate_sha256, source_run_ref, invocation_sha256,
            review_sha256=review_sha256, release_sha256=release_sha256,
            selection_sha256=selection_sha256,
            reuse_admission_sha256=reuse_admission_sha256,
            planning_bundle_sha256=planning_bundle_sha256,
            adapter_admission_sha256=adapter_admission_sha256)
        selected_lane = release_sha256 is not None
        if (preview_sha256 != checked['preview_sha256']
                or confirm_sha256 != checked['preview_sha256']):
            raise ValueError('owned_candidate_execution_confirmation_invalid_or_consumed')
        self._assert_owned_candidate_execution_start_available(
            confirm_sha256, selected_lane)
        desktop = self.controller.state()
        if (self.closed or self.busy or self.paused or desktop['owner'] != 'AGENT'
                or desktop['status'] != 'running' or desktop['lease_id'] != lease_id
                or desktop['generation'] != generation):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Candidate start requires current idle agent ownership')
        source = prepared['source']
        session = prepared['session']
        from .site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate
        candidate_model = parse_site_skill_form_recipe_candidate(prepared['candidate'])
        candidate = prepared['candidate']
        execution_store_root = candidate_execution_skill_store_path(
            session.directory, candidate_sha256)
        execution_store = SiteSkillStore(
            execution_store_root, self.remote_entry_profiles, source['pages'])
        registered = execution_store.register(
            candidate_model.skill, confirm_sha256=prepared['skill_sha256'])
        if registered != prepared['skill_sha256']:
            raise ValueError('owned_candidate_execution_skill_pin_changed')
        invocation = compile_site_skill_form_recipe_invocation(
            execution_store, prepared['plan'], prepared['inputs'], case_key,
            self.remote_entry_profiles, source['task'], prepared['form_plan'],
            prepared['state_plan'], list(prepared['field_bindings']),
            prepared['recipe'])
        admission = prepare_candidate_execution(
            candidate, candidate_sha256, invocation, prepared['plan'],
            prepared['inputs'], case_key)
        if (digest(invocation) != prepared['invocation_sha256']
                or admission.parameter_variant_sha256
                != prepared['parameter_variant_sha256']):
            raise ValueError('owned_candidate_execution_preview_changed')
        revalidator = lambda: revalidate_candidate_execution(
            admission, session.candidate_directory,
            database=self.settings.database, profiles=self.remote_entry_profiles,
            pages=source['pages'], source_parameters=source['source_parameters'],
            store=execution_store, plan=prepared['plan'], inputs=prepared['inputs'],
            case_key=case_key, task=source['task'], form_plan=prepared['form_plan'],
            state_plan=prepared['state_plan'],
            field_bindings=list(prepared['field_bindings']),
            recipe=prepared['recipe'], invocation=invocation,
            owned_source_directory=session.directory,
            owned_source_manifest_sha256=session.manifest_sha256)
        if digest(revalidator().model_dump(mode='json')) != admission.invocation_sha256:
            raise ValueError('owned_candidate_execution_source_changed')
        execution_identity = {
            'preview_sha256': checked['preview_sha256'],
            'admission_sha256': digest(admission.model_dump(mode='json')),
            'source_run_ref': source_run_ref,
        }
        if release_sha256 is not None:
            execution_identity.update({'release_sha256': release_sha256,
                                       'selection_sha256': selection_sha256})
        if reuse_admission_sha256 is not None:
            execution_identity['reuse_admission_sha256'] = reuse_admission_sha256
        if planning_bundle_sha256 is not None:
            execution_identity['planning_bundle_sha256'] = planning_bundle_sha256
        if adapter_admission_sha256 is not None:
            execution_identity['adapter_admission_sha256'] = adapter_admission_sha256
        execution_sha256 = digest(execution_identity)
        prepared['preview_sha256'] = checked['preview_sha256']
        from .owned_form_candidate_execution import persist_candidate_execution_bundle
        bundle_root = session.directory / 'candidate-execution-bundles'
        bundle_directory, bundle_manifest_sha256 = persist_candidate_execution_bundle(
            bundle_root, candidate_sha256, execution_sha256, prepared, admission,
            review_sha256=review_sha256, release_sha256=release_sha256,
            selection_sha256=selection_sha256,
            reuse_admission_sha256=reuse_admission_sha256,
            reuse_admission=(self._owned_skill_reuse['admission']
                             if reuse_admission_sha256 is not None else None),
            **({'planning_bundle_sha256': planning_bundle_sha256,
                'planning_bundle': planning_bundle} if planning_bundle is not None else {}),
            **({'adapter_admission_sha256': adapter_admission_sha256,
                'adapter_admission': adapter_admission} if adapter_admission is not None else {}))
        base_revalidator = revalidator

        def revalidate_persisted_candidate_execution():
            from .owned_form_candidate_execution import load_candidate_execution_bundle

            loaded, current_manifest_sha256 = load_candidate_execution_bundle(
                bundle_root, execution_sha256)
            if (current_manifest_sha256 != bundle_manifest_sha256
                    or loaded['manifest']['candidate_sha256'] != candidate_sha256
                    or loaded['manifest']['invocation_sha256'] != admission.invocation_sha256
                    or loaded['manifest']['admission_sha256']
                    != digest(admission.model_dump(mode='json'))
                    or loaded['manifest'].get('review_sha256') != review_sha256
                    or loaded['manifest'].get('release_sha256') != release_sha256
                    or loaded['manifest'].get('selection_sha256') != selection_sha256
                    or loaded['manifest'].get('reuse_admission_sha256')
                    != reuse_admission_sha256
                    or loaded['manifest'].get('planning_bundle_sha256') != planning_bundle_sha256
                    or loaded['manifest'].get('adapter_admission_sha256') != adapter_admission_sha256):
                raise ValueError('owned_candidate_execution_bundle_changed')
            if review_sha256 is not None:
                self._assert_candidate_review_for_execution(
                    review_sha256, prepared, source_run_ref, invocation_sha256)
            if release_sha256 is not None:
                self._assert_owned_release_selection(
                    release_sha256, selection_sha256, review_sha256, prepared)
            if reuse_admission_sha256 is not None:
                self._assert_owned_skill_reuse_current()
            if adapter_admission is not None:
                self.owned_episode_learning.adapter_runtime.validate(adapter_admission)
            if planning_bundle is not None and 'knowledge' in planning_bundle:
                knowledge = getattr(self.owned_skill_planning, 'knowledge', None)
                if knowledge is None:
                    raise ValueError('owned_skill_knowledge_unavailable')
                knowledge.check_binding(loaded['planning-bundle'], check_plan=False)
            evaluation = getattr(self.owned_episode_learning, 'adapter_evaluation', None)
            if self.busy and evaluation is not None and self.job_id in evaluation.jobs:
                evaluation.validate_job(self.job_id)
            return base_revalidator()

        revalidator = revalidate_persisted_candidate_execution
        if digest(revalidator().model_dump(mode='json')) != admission.invocation_sha256:
            raise ValueError('owned_candidate_execution_bundle_changed')
        port = source['manifest']['origin'].rsplit(':', 1)[1]
        try:
            listener_fd = bind_exact_loopback_listener(int(port))
        except (TypeError, ValueError) as error:
            raise ValueError('owned_candidate_execution_fixture_unavailable') from error
        fixture = None
        try:
            fixture = OwnedFormFixture(
                listener_fd, origin=source['manifest']['origin'],
                profile_sha256=prepared['profile_sha256'],
                form_plan_sha256=digest(prepared['form_plan'].model_dump()),
                state_plan_sha256=digest(prepared['state_plan'].model_dump()),
                entry_url=prepared['form_plan'].entry_url,
                submit_url=prepared['form_plan'].submit_url,
                receipt_url=prepared['form_plan'].receipt_url,
                state_url=prepared['state_plan'].state_url,
                expected_body=prepared['form_body'], state_after=prepared['state_after'],
                certificate_file=session.directory / 'owned-form-certificate.pem',
                key_file=session.directory / 'owned-form-key.pem',
                certificate_sha256=source['manifest']['certificate_sha256'])
        finally:
            try:
                import os
                os.close(listener_fd)
            except OSError:
                pass
        try:
            fixture.target.assert_plan(
                prepared['profile_sha256'], digest(prepared['form_plan'].model_dump()))
            fixture.target.assert_state_plan(digest(prepared['state_plan'].model_dump()))
        except Exception:
            fixture.close()
            raise
        old_names = (
            'remote_form_plan', 'remote_form_field_name', 'remote_form_value',
            'remote_form_fields', 'remote_form_tls_context',
            'remote_form_public_plan_sha256', 'remote_form_state_plan',
            'remote_form_public_state_plan_sha256', 'remote_form_cookie',
            'remote_form_cookie_sha256', 'remote_form_skill_invocation',
            'remote_form_skill_invocation_sha256', 'remote_form_skill_revalidator',
            'remote_form_candidate_admission', 'remote_form_owned_target',
            'remote_form_owned_fixture')
        self._owned_candidate_execution_restore = {
            name: getattr(self, name) for name in old_names}
        self.remote_form_plan = prepared['form_plan']
        from .owned_form_candidate_execution import candidate_operator_field_args
        (self.remote_form_field_name,
         self.remote_form_value) = candidate_operator_field_args(prepared['form_fields'])
        self.remote_form_fields = prepared['form_fields']
        self.remote_form_tls_context = fixture.target.tls_context
        self.remote_form_public_plan_sha256 = None
        self.remote_form_state_plan = prepared['state_plan']
        self.remote_form_public_state_plan_sha256 = None
        self.remote_form_cookie = None
        self.remote_form_cookie_sha256 = None
        self.remote_form_skill_invocation = invocation
        self.remote_form_skill_invocation_sha256 = admission.invocation_sha256
        self.remote_form_skill_revalidator = revalidator
        self.remote_form_candidate_admission = admission
        self.remote_form_owned_target = fixture.target
        self.remote_form_owned_fixture = fixture
        state = {
            'schema_version': ('1.3' if reuse_admission_sha256 is not None else
                               '1.2' if release_sha256 is not None else
                               '1.1' if review_sha256 is not None else '1.0'),
            'mode': 'owned_candidate_development',
            'lifecycle': 'starting',
            'candidate_sha256': candidate_sha256,
            'candidate_execution_sha256': execution_sha256,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': invocation_sha256,
            'source_group_sha256': candidate['source_group_sha256'],
            'profile_sha256': prepared['profile_sha256'],
            'skill_sha256': prepared['skill_sha256'], 'case_key': case_key,
            'parameter_variant_sha256': prepared['parameter_variant_sha256'],
            'invocation_sha256': admission.invocation_sha256,
            'recipe_sha256': admission.recipe_sha256,
            'steps': prepared['steps'], 'job_id': None, 'run_id': None,
            'run_ref': None, 'report_sha256': None, 'report': None,
            'preview_sha256': checked['preview_sha256'],
            'bundle_directory': bundle_directory,
            'bundle_manifest_sha256': bundle_manifest_sha256,
            'fixture': fixture, 'admission': admission,
        }
        if review_sha256 is not None:
            state['review_sha256'] = review_sha256
            state['review_status'] = 'accepted'
            state['source_fingerprint_sha256'] = prepared['candidate'][
                'source_fingerprint_sha256']
        if release_sha256 is not None:
            state.update({'release_sha256': release_sha256,
                          'selection_sha256': selection_sha256,
                          'family_sha256': release['family_sha256'],
                          'selection_status': 'current'})
        if reuse_admission_sha256 is not None:
            state.update({'schema_version': '1.3',
                          'reuse_admission_sha256': reuse_admission_sha256})
        if planning_bundle_sha256 is not None:
            state.update({'schema_version': '1.4', 'planning_bundle_sha256': planning_bundle_sha256,
                          'planning_bundle': planning_bundle})
        if adapter_admission_sha256 is not None:
            state.update({'schema_version': '1.5', 'adapter_admission_sha256': adapter_admission_sha256,
                          'adapter_admission': adapter_admission})
        self._owned_candidate_execution = state
        self._active_owned_candidate_execution = state
        self._owned_candidate_execution_history[execution_sha256] = state
        try:
            if planning_bundle is not None and planning_bundle.get('episode_id') is not None:
                if self.owned_episode_learning is None:
                    raise ValueError('owned_episode_learning_unavailable')
                self.owned_episode_learning.bind(planning_bundle, execution_sha256)
            result = self._start_owned_candidate_execution_job(
                checked['preview_sha256'], selected_lane,
                lambda: self._start(lease_id, generation, 'browser_remote_form',
                                   False, False))
            state['job_id'] = result['job_id']
            state['lifecycle'] = 'running'
            if planning_bundle is not None and planning_bundle.get('episode_id') is not None:
                learning = self.owned_episode_learning
                self.task.add_done_callback(lambda completed, job_id=result['job_id']: learning.poll(job_id))
            response = {
                'schema_version': ('1.3' if reuse_admission_sha256 is not None else
                                   '1.2' if release_sha256 is not None else
                                   '1.1' if review_sha256 is not None else '1.0'),
                'accepted': True, 'lifecycle': 'running',
                'candidate_execution_sha256': execution_sha256,
                'candidate_sha256': candidate_sha256, 'case_key': case_key,
                'job_id': result['job_id'], 'run_id': None,
                'invocation_sha256': admission.invocation_sha256,
            }
            if review_sha256 is not None:
                response['review_sha256'] = review_sha256
            if release_sha256 is not None:
                response['release_sha256'] = release_sha256
                response['selection_sha256'] = selection_sha256
            if reuse_admission_sha256 is not None:
                response['reuse_admission_sha256'] = reuse_admission_sha256
            if planning_bundle_sha256 is not None:
                response.update({'schema_version': '1.4',
                                 'planning_bundle_sha256': planning_bundle_sha256})
            if adapter_admission_sha256 is not None:
                response.update({'schema_version': '1.5',
                                 'adapter_admission_sha256': adapter_admission_sha256,
                                 'adapter_deployment_id': adapter_admission['adapter_deployment_id']})
            return response
        except Exception:
            state['lifecycle'] = 'failed'
            self._close_candidate_execution_fixture()
            raise

    def _assert_owned_candidate_execution_start_available(self, preview_sha256,
                                                          selected_lane):
        if (preview_sha256 in self._owned_candidate_execution_consumed
                or preview_sha256 in self._owned_selected_candidate_execution_consumed):
            raise ValueError('owned_candidate_execution_confirmation_invalid_or_consumed')
        starts = (self._owned_selected_candidate_execution_session_starts
                  if selected_lane else self._owned_candidate_execution_session_starts)
        if starts >= 4:
            raise ValueError('owned_candidate_execution_confirmation_invalid_or_consumed')

    def _consume_owned_candidate_execution_start(self, preview_sha256, selected_lane):
        self._assert_owned_candidate_execution_start_available(
            preview_sha256, selected_lane)
        if selected_lane:
            self._owned_selected_candidate_execution_consumed.add(preview_sha256)
            self._owned_selected_candidate_execution_session_starts += 1
        else:
            self._owned_candidate_execution_consumed.add(preview_sha256)
            self._owned_candidate_execution_session_starts += 1

    def _start_owned_candidate_execution_job(self, preview_sha256, selected_lane,
                                             start_job):
        self._consume_owned_candidate_execution_start(preview_sha256, selected_lane)
        return start_job()

    def _load_owned_candidate_replay_inventory(self, prepared, source_run_ref,
                                               invocation_sha256):
        from .owned_form_candidate_execution import (
            load_candidate_execution_replay_inventory)

        session = prepared['session']
        allow_missing = (
            self._owned_candidate_source_is_current_session()
            and not self._owned_candidate_execution_history
            and not self._owned_candidate_execution_consumed
            and not self._owned_selected_candidate_execution_consumed
            and self._owned_candidate_execution_session_starts == 0
            and self._owned_selected_candidate_execution_session_starts == 0)
        return load_candidate_execution_replay_inventory(
            session.directory, source_run_ref=source_run_ref,
            source_invocation_sha256=invocation_sha256,
            source_manifest_sha256=session.manifest_sha256,
            allow_missing=allow_missing)

    def _restore_owned_candidate_replay_inventory(self, prepared, source_run_ref,
                                                  invocation_sha256):
        inventory = self._load_owned_candidate_replay_inventory(
            prepared, source_run_ref, invocation_sha256)
        self._owned_candidate_execution_consumed.update(inventory['direct'])
        self._owned_selected_candidate_execution_consumed.update(
            inventory['selected'])

    def _owned_candidate_source_is_current_session(self):
        if (self._owned_form_run_id is None
                or self._owned_form_source_job_id is None):
            return False
        row = self.store.connection.execute(
            'SELECT job_id,session_id,kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (self._owned_form_source_job_id,)).fetchone()
        return bool(
            row is not None
            and row['job_id'] == self._owned_form_source_job_id
            and row['session_id'] == self.controller.session_id
            and row['kind'] == 'browser_remote_form'
            and row['status'] == 'succeeded'
            and row['run_id'] == self._owned_form_run_id)

    def _candidate_execution_auditor(self, prepared, admission, store, invocation):
        from .site_skill_form_recipe_candidate_execution import audit_candidate_execution

        def audit(run_id):
            return audit_candidate_execution(
                admission, prepared['session'].candidate_directory, run_id=run_id,
                database=self.settings.database, profiles=self.remote_entry_profiles,
                pages=prepared['source']['pages'],
                source_parameters=prepared['source']['source_parameters'],
                store=store, plan=prepared['plan'], inputs=prepared['inputs'],
                case_key=prepared['case_key'], task=prepared['source']['task'],
                form_plan=prepared['form_plan'], state_plan=prepared['state_plan'],
                field_bindings=list(prepared['field_bindings']),
                recipe=prepared['recipe'], invocation=invocation,
                owned_source_directory=prepared['session'].directory,
                owned_source_manifest_sha256=prepared['session'].manifest_sha256)
        return audit

    def audit_owned_form_candidate_execution(self, candidate_execution_sha256, *, _audited_snapshot=None):
        import re

        if not isinstance(candidate_execution_sha256, str) or re.fullmatch(
                r'[a-f0-9]{64}', candidate_execution_sha256) is None:
            raise ValueError('owned_candidate_execution_hash_invalid')
        execution = self._owned_candidate_execution_history.get(candidate_execution_sha256)
        def unavailable(status='unavailable'):
            result = {'schema_version': '1.0', 'available': False,
                      'mode': 'owned_candidate_development', 'status': status,
                      'report': None, 'report_sha256': None}
            if execution is not None and execution.get('review_sha256') is not None:
                result.update({'schema_version': '1.1',
                               'review_sha256': execution['review_sha256'],
                               'review_admission_verified': False,
                               'review_status': execution.get('review_status', 'unavailable')})
            if execution is not None and execution.get('release_sha256') is not None:
                result.update({'schema_version': '1.2',
                               'release_sha256': execution['release_sha256'],
                               'selection_sha256': execution['selection_sha256'],
                               'family_sha256': execution['family_sha256'],
                               'release_admission_verified': False,
                               'selection_status': 'unavailable'})
            if execution is not None and execution.get('reuse_admission_sha256') is not None:
                result.update({'schema_version': '1.3',
                               'reuse_admission_sha256': execution[
                                   'reuse_admission_sha256'],
                               'reuse_admission_verified': False})
            if execution is not None and execution.get('planning_bundle_sha256') is not None:
                result.update({'schema_version': '1.4', 'planning_admission_verified': False,
                               'planning_bundle_sha256': execution['planning_bundle_sha256']})
            if execution is not None and execution.get('adapter_admission_sha256') is not None:
                result.update({'schema_version': '1.5', 'adapter_admission_verified': False,
                               'adapter_admission_sha256': execution['adapter_admission_sha256'],
                               'verification_scope': 'historical_execution', 'current_source_status': 'unchecked',
                               'runtime_reuse_authorized': False})
            return result
        if self.busy or self.reserved:
            learning = self.owned_episode_learning
            evaluation = getattr(learning, 'adapter_evaluation', None)
            if learning is None or not (learning.adaptation.permits_audit(candidate_execution_sha256)
                                       or learning.adapter_runtime.permits_audit(candidate_execution_sha256)
                                       or evaluation is not None and evaluation.permits_audit(candidate_execution_sha256)):
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Owned candidate audit requires an idle scheduler')
        if execution is None:
            session = self.remote_form_owned_candidate_session
            if session is None:
                return unavailable()
            try:
                from .owned_form_candidate_execution import (
                    audit_persisted_candidate_execution,
                    load_candidate_execution_bundle)

                bundle_root = session.directory / 'candidate-execution-bundles'
                bundle, _bundle_sha256 = load_candidate_execution_bundle(
                    bundle_root, candidate_execution_sha256)
                manifest = bundle['manifest']
                if (not isinstance(self.remote_form_owned_manifest, dict)
                        or manifest['source_invocation_sha256']
                        != self.remote_form_owned_manifest['invocation_sha256']):
                    return unavailable()
                completion = bundle.get('completion')
                if completion is None:
                    return unavailable('not_ready')
                candidate = bundle['candidate']
                invocation = bundle['invocation']
                execution = {
                    'schema_version': manifest['schema_version'],
                    'mode': 'owned_candidate_development',
                    'lifecycle': 'completed',
                    'candidate_sha256': manifest['candidate_sha256'],
                    'candidate_execution_sha256': candidate_execution_sha256,
                    'source_run_ref': manifest['source_run_ref'],
                    'source_invocation_sha256': manifest['source_invocation_sha256'],
                    'source_group_sha256': manifest['source_group_sha256'],
                    'source_fingerprint_sha256': candidate[
                        'source_fingerprint_sha256'],
                    'profile_sha256': candidate['profile_sha256'],
                    'skill_sha256': invocation['skill_sha256'],
                    'case_key': invocation['case_key'],
                    'parameter_variant_sha256': manifest['parameter_variant_sha256'],
                    'invocation_sha256': manifest['invocation_sha256'],
                    'recipe_sha256': manifest['recipe_sha256'],
                    'steps': [{'step_key': item['step_key'],
                               'operation': item['operation']}
                              for item in invocation['steps']],
                    'job_id': completion['job_id'], 'run_id': completion['run_id'],
                    'run_ref': completion['run_ref'],
                    'report_sha256': None, 'report': None,
                    'bundle_directory': bundle_root / candidate_execution_sha256,
                }
                if manifest['schema_version'] in {'1.1', '1.2', '1.3', '1.4', '1.5'}:
                    execution['review_sha256'] = manifest['review_sha256']
                    execution['review_status'] = 'accepted'
                if manifest['schema_version'] in {'1.2', '1.3', '1.4', '1.5'}:
                    execution.update({'release_sha256': manifest['release_sha256'],
                                      'selection_sha256': manifest['selection_sha256']})
                    from .owned_skill_release import load_owned_skill_release
                    release = load_owned_skill_release(
                        session.directory, manifest['release_sha256'])
                    execution['family_sha256'] = release['family_sha256']
                if manifest['schema_version'] in {'1.3', '1.4', '1.5'}:
                    execution['reuse_admission_sha256'] = manifest[
                        'reuse_admission_sha256']
                if manifest['schema_version'] == '1.4':
                    execution.update({'planning_bundle_sha256': manifest['planning_bundle_sha256'],
                                      'preview_sha256': manifest['preview_sha256']})
                if manifest['schema_version'] == '1.5':
                    execution.update({'adapter_admission_sha256': manifest['adapter_admission_sha256'],
                                      'preview_sha256': manifest['preview_sha256']})
                self._owned_candidate_execution_history[candidate_execution_sha256] = execution
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                    RecursionError, AttributeError):
                return unavailable()
        if execution['lifecycle'] not in {'completed', 'audited'} or not execution.get('run_id'):
            status = ('not_ready' if execution['lifecycle'] in {
                'starting', 'running', 'previewed'} else 'unavailable')
            return unavailable(status)
        try:
            from .dataset_audit import audit_snapshot
            from .owned_form_candidate_execution import (
                audit_persisted_candidate_execution)
            audit_context = (nullcontext(_audited_snapshot) if _audited_snapshot is not None
                             else audit_snapshot(self.settings.database))
            with audit_context as (snapshot, identity):
                row = snapshot.execute(
                    'SELECT kind,status,run_id FROM desktop_tasks WHERE job_id=?',
                    (execution['job_id'],)).fetchone()
                if (row is None or row['kind'] != 'browser_remote_form'
                        or row['status'] != 'succeeded'
                        or row['run_id'] != execution['run_id']):
                    raise ValueError('owned_candidate_execution_task_binding_changed')
                report = audit_persisted_candidate_execution(
                    execution['bundle_directory'].parent,
                    candidate_execution_sha256,
                    candidate_session=self.remote_form_owned_candidate_session,
                    database=self.settings.database,
                    _audited_snapshot=(snapshot, identity),
                    _allow_reviewed_base_report=(
                        execution.get('review_sha256') is not None))
                if execution.get('review_sha256') is not None:
                    if not self._candidate_review_admission_verified(
                            execution, snapshot=snapshot):
                        raise ValueError('owned_candidate_review_admission_not_recorded')
                if execution.get('release_sha256') is not None:
                    if not self._candidate_release_admission_verified(
                            execution, snapshot=snapshot):
                        raise ValueError('owned_skill_release_admission_not_recorded')
                if execution.get('reuse_admission_sha256') is not None:
                    if not self._candidate_reuse_admission_verified(
                            execution, snapshot=snapshot):
                        raise ValueError('owned_skill_reuse_admission_not_recorded')
                if execution.get('planning_bundle_sha256') is not None:
                    if not self._candidate_planning_admission_verified(execution, snapshot):
                        raise ValueError('owned_skill_planning_admission_not_recorded')
                if execution.get('adapter_admission_sha256') is not None:
                    from .owned_adapter_admission import verify_runtime_admission
                    from .owned_form_candidate_execution import load_candidate_execution_bundle

                    bundle, _checksum = load_candidate_execution_bundle(
                        execution['bundle_directory'].parent, candidate_execution_sha256)
                    if not verify_runtime_admission(snapshot, execution, bundle['adapter-admission'],
                                                    bundle['reuse-admission']):
                        raise ValueError('owned_adapter_runtime_admission_not_recorded')
            if not isinstance(report, dict) or report.get('status') != 'source_bound_development_execution':
                raise ValueError('owned_candidate_execution_report_invalid')
            from .dataset import validator
            validator('site_skill_form_recipe_candidate_execution').validate(report)
            report_sha256 = digest(report)
            if (report.get('admission', {}).get('candidate_sha256')
                    != execution['candidate_sha256']
                    or report.get('execution_run_ref') != execution.get('run_ref')
                    or report.get('source_run_ref') != execution['source_run_ref']):
                raise ValueError('owned_candidate_execution_report_binding_changed')
        except Exception:
            execution['lifecycle'] = 'failed'
            execution['report'] = None
            execution['report_sha256'] = None
            return unavailable()
        execution['report'] = report
        execution['report_sha256'] = report_sha256
        execution['lifecycle'] = 'audited'
        response = {
            'schema_version': '1.0', 'available': True,
            'mode': 'owned_candidate_development', 'status': 'verified',
            **{key: execution[key] for key in (
                'candidate_execution_sha256', 'candidate_sha256',
                'source_run_ref', 'source_invocation_sha256',
                'source_group_sha256', 'profile_sha256', 'skill_sha256',
                'case_key', 'parameter_variant_sha256', 'invocation_sha256',
                'recipe_sha256', 'steps', 'job_id', 'run_id', 'run_ref',
                'report_sha256')},
            'report': report,
        }
        if execution.get('review_sha256') is not None:
            try:
                review = self.inspect_owned_candidate_review(
                    execution['review_sha256'])
                receipt = review['receipt']
                if any(receipt.get(key) != execution.get(execution_key)
                       for key, execution_key in (
                           ('candidate_sha256', 'candidate_sha256'),
                           ('source_run_ref', 'source_run_ref'),
                           ('source_invocation_sha256', 'source_invocation_sha256'),
                           ('source_group_sha256', 'source_group_sha256'),
                           ('source_fingerprint_sha256', 'source_fingerprint_sha256'),
                           ('profile_sha256', 'profile_sha256'),
                           ('skill_sha256', 'skill_sha256'),
                           ('recipe_sha256', 'recipe_sha256'))):
                    raise ValueError('owned_candidate_review_execution_binding_changed')
            except Exception:
                return unavailable()
            execution['review_status'] = review['status']
            response.update({'schema_version': '1.1',
                             'review_sha256': execution['review_sha256'],
                             'review_admission_verified': True,
                             'review_status': review['status']})
        if execution.get('release_sha256') is not None:
            selection_status = self._owned_execution_selection_status(execution)
            if selection_status == 'unavailable':
                return unavailable()
            execution['selection_status'] = selection_status
            response.update({'schema_version': '1.2',
                             'release_sha256': execution['release_sha256'],
                             'selection_sha256': execution['selection_sha256'],
                             'family_sha256': execution['family_sha256'],
                             'release_admission_verified': True,
                             'selection_status': selection_status})
        if execution.get('reuse_admission_sha256') is not None:
            response.update({'schema_version': '1.3',
                             'reuse_admission_sha256': execution[
                                 'reuse_admission_sha256'],
                             'reuse_admission_verified': True})
        if execution.get('planning_bundle_sha256') is not None:
            response.update({'schema_version': '1.4', 'planning_admission_verified': True,
                             'planning_bundle_sha256': execution['planning_bundle_sha256']})
        if execution.get('adapter_admission_sha256') is not None:
            response.update({'schema_version': '1.5', 'adapter_admission_verified': True,
                             'adapter_admission_sha256': execution['adapter_admission_sha256'],
                             'verification_scope': 'historical_execution', 'current_source_status': 'unchecked',
                             'runtime_reuse_authorized': False})
        return response

    def _candidate_planning_admission_verified(self, execution, snapshot):
        from .owned_form_candidate_execution import load_candidate_execution_bundle
        from .owned_skill_plan_admission import verify_planning_admission

        root = self.remote_form_owned_candidate_session.directory / 'candidate-execution-bundles'
        bundle, _checksum = load_candidate_execution_bundle(root, execution['candidate_execution_sha256'])
        if (bundle['manifest']['schema_version'] != '1.4'
                or bundle['manifest']['planning_bundle_sha256'] != execution['planning_bundle_sha256']
                or bundle['manifest']['preview_sha256'] != execution['preview_sha256']):
            return False
        return verify_planning_admission(snapshot, execution, bundle['planning-bundle'])

    def _candidate_reuse_admission_verified(self, execution, snapshot=None):
        if (execution.get('reuse_admission_sha256') is None
                or execution.get('release_sha256') is None
                or not execution.get('run_id') or not execution.get('job_id')):
            return False
        connection = snapshot or self.store.connection
        rows = connection.execute(
            "SELECT * FROM observations WHERE run_id=? "
            "AND kind='skill.owned_reuse_admission'", (execution['run_id'],)).fetchall()
        initial = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0',
            (execution['run_id'],)).fetchone()
        next_state = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=1',
            (execution['run_id'],)).fetchone()
        first_action = connection.execute(
            'SELECT MIN(created_at) AS created_at FROM actions WHERE run_id=?',
            (execution['run_id'],)).fetchone()
        if len(rows) != 1 or initial is None or next_state is None or first_action is None:
            return False
        try:
            from .owned_form_candidate_execution import load_candidate_execution_bundle
            from .owned_skill_reuse_admission import validate_owned_skill_reuse_admission

            bundle, _manifest_sha256 = load_candidate_execution_bundle(
                execution['bundle_directory'].parent,
                execution['candidate_execution_sha256'])
            manifest = bundle['manifest']
            admission = validate_owned_skill_reuse_admission(
                bundle['reuse-admission'])
            source_preview = admission['preview']
            initial_state = json.loads(initial['state_json'])
            following_state = json.loads(next_state['state_json'])
            payload = json.loads(rows[0]['payload_json'])
            from .site_skill_form_invocation_audit import _timestamp

            before = _timestamp(initial['created_at'])
            observed = _timestamp(rows[0]['created_at'])
            after = _timestamp(next_state['created_at'])
            first_action_at = _timestamp(first_action['created_at'])
            expected = {
                'schema_version': '1.0', 'synthetic': True,
                'candidate_execution_sha256': execution['candidate_execution_sha256'],
                'candidate_sha256': execution['candidate_sha256'],
                'invocation_sha256': execution['invocation_sha256'],
                'review_sha256': execution['review_sha256'],
                'release_sha256': execution['release_sha256'],
                'selection_sha256': execution['selection_sha256'],
                'reuse_admission_sha256': execution['reuse_admission_sha256'],
                'manager_session': admission['manager_session'],
                'desktop_session_id': admission['desktop_session_id'],
                'runtime_id': admission['runtime_id'],
                'lease_id': admission['lease_id'],
                'generation': admission['generation'],
                'reuse_admission_verified': True,
                'old_actions_replayed': False, 'execution_authorized': False,
                'activation_authorized': False, 'training_ready': False,
                'independent_held_out': False,
            }
            task = connection.execute(
                'SELECT * FROM desktop_tasks WHERE job_id=?',
                (execution['job_id'],)).fetchone()
            source_job = connection.execute(
                'SELECT session_id,kind,status,run_id FROM desktop_tasks WHERE job_id=?',
                (source_preview['source_job_id'],)).fetchone()
            session = connection.execute(
                'SELECT runtime_id,status FROM desktop_sessions WHERE session_id=?',
                (admission['desktop_session_id'],)).fetchone()
            binding = connection.execute(
                'SELECT browser_runtime_id,draft_json FROM desktop_remote_form_bindings '
                'WHERE run_id=?', (execution['run_id'],)).fetchone()
            from .web_application_binding import WebTaskAdmissionDraft
            draft = (WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
                     if binding is not None else None)
            from .contracts import canonical
            return (
                manifest['schema_version'] in {'1.3', '1.4', '1.5'}
                and digest(admission) == execution['reuse_admission_sha256']
                and manifest['reuse_admission_sha256'] == execution[
                    'reuse_admission_sha256']
                and source_preview['source_run_ref'] == execution['source_run_ref']
                and source_preview['source_invocation_sha256']
                == execution['source_invocation_sha256']
                and source_preview['release_sha256'] == execution['release_sha256']
                and source_preview['selection_sha256'] == execution['selection_sha256']
                and source_preview['review_sha256'] == execution['review_sha256']
                and source_preview['candidate_sha256'] == execution['candidate_sha256']
                and source_preview['recipe_sha256'] == execution['recipe_sha256']
                and source_preview['family_sha256'] == execution['family_sha256']
                and source_preview['source_fingerprint_sha256']
                == execution['source_fingerprint_sha256']
                and rows[0]['action_id'] is None
                and rows[0]['step_id'] == initial['step_id']
                and rows[0]['payload_json'] == canonical(expected)
                and before <= observed <= after <= first_action_at
                and initial_state.get('phase') == 'CREATED'
                and initial_state.get('state_version') == 0
                and initial_state.get('owner') == 'AGENT'
                and initial_state.get('task_kind') == 'browser_remote_form'
                and initial_state.get('run_id') == execution['run_id']
                and initial_state.get('skill_invocation_sha256')
                == execution['invocation_sha256']
                and initial_state.get('owner_lease_id') == admission['lease_id']
                and following_state.get('state_version') == 1
                and all(initial_state.get(key) == following_state.get(key)
                        for key in ('task_id', 'run_id', 'step_id', 'runtime_id',
                                    'deployment_id', 'owner_lease_id',
                                    'skill_invocation_sha256'))
                and self._owned_reuse_identity_joins_match(
                    admission, source_preview, execution['run_id'], initial_state,
                    task, session, binding, source_job))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error,
                RecursionError, AttributeError):
            return False

    @staticmethod
    def _owned_reuse_identity_joins_match(admission, source_preview, run_id,
                                          initial_state, task, session,
                                          binding, source_job):
        if any(value is None for value in (task, session, binding, source_job)):
            return False
        try:
            from .web_application_binding import WebTaskAdmissionDraft

            draft = WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
            source_run_id = source_job['run_id']
            return (
                admission['desktop_session_id'] != source_preview[
                    'source_desktop_session_id']
                and task['session_id'] == admission['desktop_session_id']
                and task['kind'] == 'browser_remote_form'
                and task['status'] == 'succeeded' and task['run_id'] == run_id
                and task['lease_id'] == admission['lease_id']
                and type(task['generation']) is int
                and task['generation'] == admission['generation']
                and task['runtime_id'] == initial_state.get('runtime_id')
                and initial_state.get('owner_lease_id') == admission['lease_id']
                and session['runtime_id'] == admission['runtime_id']
                and binding['browser_runtime_id'] == initial_state.get('runtime_id')
                and canonical(draft.model_dump(mode='json')) == binding['draft_json']
                and draft.runtime.parent_runtime_id == admission['runtime_id']
                and source_job['session_id'] == source_preview[
                    'source_desktop_session_id']
                and source_job['kind'] == 'browser_remote_form'
                and source_job['status'] == 'succeeded'
                and isinstance(source_run_id, str)
                and digest({'run_id': source_run_id}) == source_preview['source_run_ref'])
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    @staticmethod
    def _owned_reuse_admission_observation(candidate_execution, reuse, desktop_session_id):
        admission = reuse['admission']
        return {
            'schema_version': '1.0', 'synthetic': True,
            'candidate_execution_sha256': candidate_execution[
                'candidate_execution_sha256'],
            'candidate_sha256': candidate_execution['candidate_sha256'],
            'invocation_sha256': candidate_execution['invocation_sha256'],
            'review_sha256': candidate_execution['review_sha256'],
            'release_sha256': candidate_execution['release_sha256'],
            'selection_sha256': candidate_execution['selection_sha256'],
            'reuse_admission_sha256': candidate_execution['reuse_admission_sha256'],
            'manager_session': reuse['manager_session'],
            'desktop_session_id': desktop_session_id,
            'runtime_id': admission['runtime_id'],
            'lease_id': admission['lease_id'],
            'generation': admission['generation'],
            'reuse_admission_verified': True, 'old_actions_replayed': False,
            'execution_authorized': False, 'activation_authorized': False,
            'training_ready': False, 'independent_held_out': False,
        }

    def _candidate_release_admission_verified(self, execution, snapshot=None):
        if (execution.get('release_sha256') is None
                or execution.get('selection_sha256') is None
                or not execution.get('run_id')):
            return False
        connection = snapshot or self.store.connection
        rows = connection.execute(
            "SELECT * FROM observations WHERE run_id=? "
            "AND kind='skill.owned_release_selection_admission'",
            (execution['run_id'],)).fetchall()
        initial = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0',
            (execution['run_id'],)).fetchone()
        next_state = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=1',
            (execution['run_id'],)).fetchone()
        first_action = connection.execute(
            'SELECT MIN(created_at) AS created_at FROM actions WHERE run_id=?',
            (execution['run_id'],)).fetchone()
        if len(rows) != 1 or initial is None or next_state is None or first_action is None:
            return False
        expected = {
            'schema_version': '1.0', 'synthetic': True,
            'candidate_execution_sha256': execution['candidate_execution_sha256'],
            'candidate_sha256': execution['candidate_sha256'],
            'invocation_sha256': execution['invocation_sha256'],
            'review_sha256': execution['review_sha256'],
            'release_sha256': execution['release_sha256'],
            'selection_sha256': execution['selection_sha256'],
            'family_sha256': execution['family_sha256'],
            'selection_admission_verified': True,
            'activation_authorized': False, 'training_ready': False,
            'independent_held_out': False,
        }
        try:
            initial_state = json.loads(initial['state_json'])
            after_state = json.loads(next_state['state_json'])
            payload = json.loads(rows[0]['payload_json'])
            release = self._load_owned_release(execution['release_sha256'])
            from .owned_skill_release import owned_skill_selection_chain
            session = self.remote_form_owned_candidate_session
            chain = owned_skill_selection_chain(session.directory, execution['family_sha256'])
        except (OSError, ValueError, TypeError, KeyError):
            return False
        selection = next((event for selection_hash, event in chain
                          if selection_hash == execution['selection_sha256']), None)
        from .site_skill_form_invocation_audit import _timestamp
        return (selection is not None
                and selection['release_sha256'] == execution['release_sha256']
                and release['family_sha256'] == execution['family_sha256']
                and release['review_sha256'] == execution['review_sha256']
                and release['candidate_sha256'] == execution['candidate_sha256']
                and rows[0]['action_id'] is None
                and rows[0]['step_id'] == initial['step_id']
                and initial_state.get('phase') == 'CREATED'
                and initial_state.get('state_version') == 0
                and initial_state.get('owner') == 'AGENT'
                and initial_state.get('task_kind') == 'browser_remote_form'
                and initial_state.get('run_id') == execution['run_id']
                and initial_state.get('skill_invocation_sha256') == execution['invocation_sha256']
                and after_state.get('state_version') == 1
                and all(initial_state.get(key) == after_state.get(key)
                        for key in ('task_id', 'run_id', 'step_id', 'runtime_id',
                                    'deployment_id', 'owner_lease_id',
                                    'skill_invocation_sha256'))
                and canonical(payload) == canonical(expected)
                and canonical(payload).encode() == rows[0]['payload_json'].encode()
                and _timestamp(initial['created_at'])
                <= _timestamp(rows[0]['created_at'])
                <= _timestamp(next_state['created_at'])
                <= _timestamp(first_action['created_at']))

    def _candidate_review_admission_verified(self, execution, snapshot=None):
        if execution.get('review_sha256') is None or not execution.get('run_id'):
            return False
        connection = snapshot or self.store.connection
        rows = connection.execute(
            "SELECT * FROM observations WHERE run_id=? "
            "AND kind='skill.recipe_candidate_review_admission'",
            (execution['run_id'],)).fetchall()
        initial = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0',
            (execution['run_id'],)).fetchone()
        next_state = connection.execute(
            'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=1',
            (execution['run_id'],)).fetchone()
        first_action = connection.execute(
            'SELECT MIN(created_at) AS created_at FROM actions WHERE run_id=?',
            (execution['run_id'],)).fetchone()
        if (len(rows) != 1 or initial is None or next_state is None
                or first_action is None):
            return False
        expected = {
            'schema_version': '1.0', 'synthetic': True,
            'review_sha256': execution['review_sha256'],
            'candidate_execution_sha256': execution['candidate_execution_sha256'],
            'candidate_sha256': execution['candidate_sha256'],
            'source_run_ref': execution['source_run_ref'],
            'review_status': 'accepted', 'review_admission_verified': True,
            'activation_authorized': False, 'training_ready': False,
            'independent_held_out': False,
        }
        try:
            initial_state = json.loads(initial['state_json'])
            next_state_value = json.loads(next_state['state_json'])
            payload = json.loads(rows[0]['payload_json'])
        except (KeyError, TypeError, ValueError):
            return False
        from .site_skill_form_invocation_audit import _timestamp
        return (rows[0]['action_id'] is None
                and rows[0]['step_id'] == initial['step_id']
                and initial_state.get('phase') == 'CREATED'
                and initial_state.get('state_version') == 0
                and initial_state.get('owner') == 'AGENT'
                and initial_state.get('task_kind') == 'browser_remote_form'
                and initial_state.get('run_id') == execution['run_id']
                and initial_state.get('skill_invocation_sha256')
                == execution['invocation_sha256']
                and next_state_value.get('state_version') == 1
                and all(initial_state.get(key) == next_state_value.get(key)
                        for key in ('task_id', 'run_id', 'step_id', 'runtime_id',
                                    'deployment_id', 'owner_lease_id',
                                    'skill_invocation_sha256'))
                and canonical(payload) == canonical(expected)
                and canonical(payload).encode() == rows[0]['payload_json'].encode()
                and _timestamp(initial['created_at'])
                <= _timestamp(rows[0]['created_at'])
                <= _timestamp(next_state['created_at'])
                <= _timestamp(first_action['created_at']))

    def audit_owned_form_invocation(self) -> dict:
        recipe_mode = (self.remote_form_owned_manifest is not None
                       and self.remote_form_owned_manifest.get('mode')
                       == 'owned_synthetic_form_recipe')

        def unavailable(status='unavailable'):
            result = {'available': False, 'status': status,
                      'report': None, 'report_sha256': None}
            if recipe_mode:
                result['mode'] = 'owned_synthetic_form_recipe'
            return result

        if self.remote_form_owned_fixture is None or not callable(self.remote_form_owned_auditor):
            return unavailable()
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Owned invocation audit requires an idle scheduler')
        if (self._owned_form_lifecycle not in {'completed', 'audited'}
                or self._owned_form_run_id is None):
            return unavailable('not_ready' if self._owned_form_lifecycle in {'ready', 'running'}
                               else 'unavailable')
        row = self.store.connection.execute(
            'SELECT kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (self._owned_form_source_job_id or self.job_id,)).fetchone()
        if (row is None or row['kind'] != 'browser_remote_form'
                or row['status'] != 'succeeded' or row['run_id'] != self._owned_form_run_id):
            self._owned_form_audit = None
            self._owned_form_lifecycle = 'failed'
            return unavailable()
        try:
            report = self.remote_form_owned_auditor(self._owned_form_run_id)
        except Exception:
            self._owned_form_audit = None
            self._owned_form_lifecycle = 'failed'
            return unavailable()
        if recipe_mode:
            expected_steps = self.remote_form_skill_invocation['steps']
            report_valid = (
                isinstance(report, dict)
                and report.get('schema_version') == '2.0'
                and report.get('status') == 'executable_recipe_execution_verified'
                and report.get('recipe_sha256') == self.remote_form_owned_manifest['recipe_sha256']
                and report.get('symbolic_step_keys') == [step['step_key'] for step in expected_steps]
                and report.get('executable_recipe_executed') is True
                and report.get('skill_executed') is True
                and report.get('site_outcome_verified') is False
                and report.get('reviewed') is False
                and report.get('activation_authorized') is False
                and report.get('training_ready') is False)
        else:
            report_valid = (isinstance(report, dict)
                            and report.get('status')
                            == 'fixed_template_invocation_execution_verified')
        if (not report_valid
                or report.get('run_ref') != digest({'run_id': self._owned_form_run_id})
                or report.get('invocation_sha256') != self.remote_form_owned_manifest['invocation_sha256']):
            self._owned_form_audit = None
            self._owned_form_lifecycle = 'failed'
            return unavailable()
        self._owned_form_audit = report
        self._owned_form_lifecycle = 'audited'
        result = {'available': True, 'status': 'verified', 'report': report,
                  'report_sha256': hashlib.sha256(canonical(report).encode()).hexdigest()}
        if recipe_mode:
            result['mode'] = 'owned_synthetic_form_recipe'
        return result

    def owned_form_candidate_context(self) -> dict:
        session = self.remote_form_owned_candidate_session
        unavailable = {'schema_version': '1.0', 'available': False,
                       'reason': 'unavailable'}
        if (session is None or not isinstance(self.remote_form_owned_manifest, dict)
                or self.remote_form_owned_manifest.get('mode')
                != 'owned_synthetic_form_invocation'):
            return unavailable
        if self.busy or self.reserved:
            return {'schema_version': '1.0', 'available': False,
                    'reason': 'not_ready'}
        if self._owned_form_lifecycle not in {'audited'} or self._owned_form_run_id is None:
            return {'schema_version': '1.0', 'available': False,
                    'reason': 'not_ready' if self._owned_form_lifecycle in {'ready', 'running', 'completed'}
                    else 'unavailable'}
        report = self._owned_form_audit
        if (not isinstance(report, dict)
                or report.get('status') != 'fixed_template_invocation_execution_verified'
                or report.get('run_ref') != digest({'run_id': self._owned_form_run_id})
                or report.get('invocation_sha256')
                != self.remote_form_owned_manifest.get('invocation_sha256')):
            return unavailable
        try:
            return session.context(self._owned_form_run_id,
                                   self.remote_form_owned_manifest['invocation_sha256'])
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return unavailable

    def _owned_form_candidate_run(self, source_run_ref: str, invocation_sha256: str):
        session = self.remote_form_owned_candidate_session
        if (session is None or not isinstance(self.remote_form_owned_manifest, dict)
                or self.remote_form_owned_manifest.get('mode')
                != 'owned_synthetic_form_invocation'):
            raise ValueError('owned_recipe_candidate_not_configured')
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned candidate requires an idle completed invocation')
        if self._owned_form_lifecycle != 'audited' or self._owned_form_run_id is None:
            raise ValueError('owned_recipe_candidate_source_not_audited')
        expected_run_ref = digest({'run_id': self._owned_form_run_id})
        expected_invocation = self.remote_form_owned_manifest['invocation_sha256']
        if (source_run_ref != expected_run_ref
                or invocation_sha256 != expected_invocation):
            raise ValueError('owned_recipe_candidate_source_pin_changed')
        report = self._owned_form_audit
        if (not isinstance(report, dict)
                or report.get('status') != 'fixed_template_invocation_execution_verified'
                or report.get('run_ref') != expected_run_ref
                or report.get('invocation_sha256') != expected_invocation):
            raise ValueError('owned_recipe_candidate_source_not_audited')
        row = self.store.connection.execute(
            'SELECT kind,status,run_id FROM desktop_tasks WHERE job_id=?',
            (self._owned_form_source_job_id or self.job_id,)).fetchone()
        if (row is None or row['kind'] != 'browser_remote_form'
                or row['status'] != 'succeeded' or row['run_id'] != self._owned_form_run_id):
            raise ValueError('owned_recipe_candidate_source_job_changed')
        return session, self._owned_form_run_id

    def preview_owned_form_candidate(self, source_run_ref: str,
                                     invocation_sha256: str, annotation):
        session, run_id = self._owned_form_candidate_run(source_run_ref, invocation_sha256)
        return session.preview(run_id, invocation_sha256, annotation)

    def publish_owned_form_candidate(self, source_run_ref: str,
                                     invocation_sha256: str, annotation,
                                     confirm_sha256: str):
        session, run_id = self._owned_form_candidate_run(source_run_ref, invocation_sha256)
        return session.publish(run_id, invocation_sha256, annotation, confirm_sha256)

    def inspect_owned_form_candidate(self, source_run_ref: str,
                                     invocation_sha256: str, candidate_sha256: str):
        session = self.remote_form_owned_candidate_session
        if (session is None or not isinstance(self.remote_form_owned_manifest, dict)
                or self.remote_form_owned_manifest.get('mode')
                != 'owned_synthetic_form_invocation'):
            raise ValueError('owned_recipe_candidate_not_configured')
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned candidate reinspection requires an idle scheduler')
        return session.reinspect(source_run_ref, invocation_sha256, candidate_sha256)

    def release_completed_runtime(self):
        if self.completed_runtime is not None:
            self.completed_runtime.stop()
            self.completed_runtime = None

    def intervention(self, reason, actor='local_authenticated_user'):
        job = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (self.job_id,)).fetchone()
        if job and job['run_id']:
            state = self.store.state(job['run_id'])
            with self.store.connection:
                self.store.insert('human_interventions', intervention_id=identifier('intervention'), run_id=state.run_id,
                                  step_id=state.step_id, actor=actor, kind=reason,
                                  payload_json=canonical({'job_id': self.job_id}), created_at=now())
        return job

    async def pause(self):
        try:
            self.clear_grant('pause')
        finally:
            try:
                await self._pause_job()
            finally:
                if isinstance(self.engine, ReusableDeciderEngine) and self.engine.cpu_prewarm:
                    await self.engine.close()

    async def _pause_job(self):
        await self.cancel_owned_skill_plan()
        await self.sequences.cancel()
        if not self.busy:
            return
        job = self.store.connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?',
                                            (self.job_id,)).fetchone()
        if job and job['kind'] in {'browser_remote_entry', 'browser_remote_routes',
                                   'browser_remote_static_assets', 'browser_remote_form'}:
            await self.cancel('pause')
            return
        if self.operator is None:
            await self.cancel('pause')
            return
        self.intervention('pause')
        self.pause_requested = True
        self.operator.interruption_phase = Phase.PAUSED
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        job = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (self.job_id,)).fetchone()
        if job and job['run_id'] and self.store.state(job['run_id']).phase == Phase.PAUSED:
            self.update(self.job_id, 'paused')

    def resume(self, lease_id, generation):
        if self.restart_quiesced:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task admission is quiesced for restart')
        if self.job_id in self._failure_followup_jobs:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'A paused follow-up needs a separately authorized fresh attempt')
        self.clear_grant('resume_requires_manual_approval')
        desktop = self.controller.state()
        if (self.closed or self.busy or not self.paused or desktop['owner'] != 'AGENT'
                or desktop['status'] != 'running' or desktop['lease_id'] != lease_id or desktop['generation'] != generation):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Resume requires current ownership and a paused task')
        job = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (self.job_id,)).fetchone()
        state = self.store.state(job['run_id'])
        uncertain = self.store.connection.execute("SELECT 1 FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (state.run_id,)).fetchone()
        supervisor_id = self.vision_supervisor.identity['deployment_id'] if job['kind'] == 'vision_canvas' and self.vision_supervisor else None
        if (state.phase != Phase.PAUSED or state.owner != 'PAUSED' or uncertain or lease_id == job['lease_id']
                or self.active_runtime is None or not self.active_runtime.status()['running']
                or state.runtime_id != self.active_runtime.runtime_id
                or state.deployment_id != self.engine.identity['deployment_id']
                or state.supervisor_deployment_id != supervisor_id):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Paused task runtime, deployment or state is no longer safe to resume')
        self.intervention('resume')
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET lease_id=?,generation=?,status='queued',updated_at=? WHERE job_id=?",
                                          (lease_id, generation, now(), self.job_id))
        self.pause_requested = False
        self.task = asyncio.create_task(self.run(self.job_id, lease_id, job['kind'], state))
        return {'job_id': self.job_id}

    async def authorize(self, job_id: str, action: Action):
        self.check_lease(job_id)
        self.check_task_knowledge_binding(job_id, require_context=True)
        self.check_failure_followup(job_id)
        self.check_failure_guidance(job_id, require_context=True)
        self.check_remote_entry_binding(job_id)
        self.check_remote_routes_binding(job_id)
        self.check_remote_static_assets_binding(job_id)
        self.check_remote_form_binding(job_id)
        SafetyPolicy.check(action, self.store.state(action.run_id), self.active_runtime.runtime_id)
        self._learning_poll(job_id, action.run_id, 'pre_approval')
        self._remote_learning_poll(job_id, action.run_id, 'pre_approval')
        if self.owned_episode_learning is not None:
            self.owned_episode_learning.poll(job_id)
        approval = Approval(approval_id=identifier('approval'), job_id=job_id, action=action,
                            action_sha256=digest(action.model_dump(mode='json')),
                            expires_at=min(time.time() + self.approval_seconds, action.deadline))
        self.answer = asyncio.get_running_loop().create_future()
        with self.store.connection:
            self.store.insert('desktop_approvals', approval_id=approval.approval_id, job_id=job_id,
                              envelope_json=approval.model_dump_json(), action_sha256=approval.action_sha256,
                              expires_at=approval.expires_at, status='pending', created_at=now(), updated_at=now())
        self.update(job_id, 'waiting_approval')
        delegated = self._approval_grant
        if delegated is not None:
            try:
                self.validate_delegated_action(job_id, action)
                self._respond(approval.approval_id, approval.action_sha256, True, self._approval_grant)
                self._grant_uses += 1
            except Exception:
                self.clear_grant('action_rejected')
                raise
        try:
            accepted = await asyncio.wait_for(self.answer, self.approval_seconds)
        except TimeoutError:
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_approvals SET status='expired',updated_at=? WHERE approval_id=? AND status='pending'", (now(), approval.approval_id))
            raise AOSFault(ErrorCode.TIMEOUT, 'Approval expired; no action executed') from None
        if not accepted:
            raise asyncio.CancelledError
        if delegated is not None and (self._approval_grant is not delegated or not self.grant_current()):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task delegation expired or was revoked before consumption')
        self.check_lease(job_id)
        self.check_failure_followup(job_id)
        self.check_failure_guidance(job_id, require_context=True)
        SafetyPolicy.check(action, self.store.state(action.run_id), self.active_runtime.runtime_id)
        with self.store.connection:
            consumed = self.store.connection.execute(
                "UPDATE desktop_approvals SET status='consumed',updated_at=? WHERE approval_id=? AND status='approved' AND action_sha256=? AND expires_at>?",
                (now(), approval.approval_id, digest(action.model_dump(mode='json')), time.time())).rowcount
            if consumed != 1:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Approval is expired, changed or already consumed')
        self.update(job_id, 'running')

    def respond(self, approval_id: str, action_sha256: str, accept: bool):
        return self._respond(approval_id, action_sha256, accept)

    def _fail_closed_candidate_form_approval(self, job_id, approval_id,
                                            action_sha256):
        execution = self._active_owned_candidate_execution
        if (execution is None or execution.get('job_id') != job_id
                or execution.get('lifecycle') != 'running'):
            return False
        with self.store.connection:
            changed = self.store.connection.execute(
                "UPDATE desktop_approvals SET status='revoked',updated_at=? "
                "WHERE approval_id=? AND job_id=? AND status='pending' "
                "AND action_sha256=?",
                (now(), approval_id, job_id, action_sha256)).rowcount
        if changed != 1:
            return False
        if self.remote_form_owned_target is not None:
            self.remote_form_owned_target.revoke()
        future = self.answer
        if future is not None and not future.done():
            future.set_exception(AOSFault(
                ErrorCode.UNSAFE_ACTION,
                'Candidate form binding changed before approval consumption'))
        return True

    def _respond(self, approval_id: str, action_sha256: str, accept: bool, grant: TaskApprovalGrant | None = None):
        row = self.store.connection.execute('SELECT * FROM desktop_approvals WHERE approval_id=? AND job_id=?', (approval_id, self.job_id)).fetchone()
        if (not self.busy or not row or row['status'] != 'pending' or row['action_sha256'] != action_sha256
                or row['expires_at'] <= time.time() or self.answer is None or self.answer.done()):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Approval is stale or does not match the action')
        approval = Approval.model_validate_json(row['envelope_json'])
        self.check_lease(approval.job_id)
        if (approval.approval_id != approval_id or approval.job_id != self.job_id
                or approval.action_sha256 != row['action_sha256']
                or digest(approval.action.model_dump(mode='json'))
                != row['action_sha256']):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Approval is stale or does not match the action')
        self.check_remote_entry_binding(approval.job_id)
        self.check_remote_routes_binding(approval.job_id)
        self.check_remote_static_assets_binding(approval.job_id)
        try:
            self.check_remote_form_binding(approval.job_id)
        except AOSFault:
            self._fail_closed_candidate_form_approval(approval.job_id,
                                                       approval.approval_id,
                                                       approval.action_sha256)
            raise
        SafetyPolicy.check(approval.action, self.store.state(approval.action.run_id), self.active_runtime.runtime_id)
        if grant is not None:
            if grant is not self._approval_grant:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Task delegation changed')
            self.validate_delegated_action(approval.job_id, approval.action)
        elif not accept:
            self.clear_grant('rejected')
        payload = {'approval_id': approval_id, 'action_sha256': action_sha256}
        if grant is not None:
            payload.update(grant_id=grant.grant_id, job_id=grant.job_id)
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_approvals SET status=?,updated_at=? WHERE approval_id=?',
                                          ('approved' if accept else 'rejected', now(), approval_id))
            self.store.insert('human_interventions', intervention_id=identifier('intervention'),
                              run_id=approval.action.run_id, step_id=approval.action.step_id,
                              actor='task_scoped_auto_approval' if grant is not None else 'local_authenticated_user',
                              kind='approve' if accept else 'reject',
                              payload_json=canonical(payload), created_at=now())
        self.answer.set_result(accept)
        return {'status': 'approved' if accept else 'rejected'}

    def _owned_release_material(self, review_sha256):
        from .owned_skill_release import make_owned_skill_release

        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_skill_release_source_unavailable')
        review = self.inspect_owned_candidate_review(review_sha256)
        receipt = review['receipt']
        if review['status'] != 'accepted':
            raise ValueError('owned_skill_release_review_not_accepted')
        candidate, _source = session.reinspect(
            receipt['source_run_ref'], receipt['source_invocation_sha256'],
            receipt['candidate_sha256'])
        release_sha256, release = make_owned_skill_release(
            candidate, receipt['candidate_sha256'], review_sha256, receipt)
        return session, candidate, receipt, release_sha256, release

    def preview_owned_skill_release(self, review_sha256, parent_release_sha256):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned skill release preview requires an idle scheduler')
        from .owned_skill_release import load_owned_skill_release, make_owned_skill_release

        session, _candidate, _receipt, _release_sha256, release = (
            self._owned_release_material(review_sha256))
        family_hash = release['family_sha256']
        parent = None if parent_release_sha256 is None else load_owned_skill_release(
            session.directory, parent_release_sha256)
        if parent is None and release['revision'] != 1:
            raise ValueError('owned_skill_release_parent_changed')
        if parent is not None:
            _expected_hash, expected = make_owned_skill_release(
                _candidate, _receipt['candidate_sha256'], review_sha256,
                _receipt, parent_release=parent)
            if (parent_release_sha256 != digest(parent)
                    or expected['revision'] != parent['revision'] + 1
                    or expected['parent_release_sha256'] != parent_release_sha256):
                raise ValueError('owned_skill_release_parent_changed')
            release = expected
        release_sha256 = digest(release)
        return {'schema_version': '1.0', 'available': True, 'status': 'preview',
                'persisted': False, 'release_sha256': release_sha256,
                'release': release}

    def publish_owned_skill_release(self, review_sha256, parent_release_sha256,
                                    confirm_sha256):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned skill release publication requires an idle scheduler')
        from .owned_skill_release import (
            load_owned_skill_release, make_owned_skill_release,
            persist_owned_skill_release)

        session, candidate, receipt, first_hash, first_release = (
            self._owned_release_material(review_sha256))
        parent = None if parent_release_sha256 is None else load_owned_skill_release(
            session.directory, parent_release_sha256)
        if parent is None:
            if first_release['revision'] != 1:
                raise ValueError('owned_skill_release_parent_changed')
            release_hash, release = first_hash, first_release
        else:
            release_hash, release = make_owned_skill_release(
                candidate, receipt['candidate_sha256'], review_sha256, receipt,
                parent_release=parent)
            if (parent_release_sha256 != digest(parent)
                    or release['parent_release_sha256'] != parent_release_sha256):
                raise ValueError('owned_skill_release_parent_changed')
        if confirm_sha256 != release_hash:
            raise ValueError('owned_skill_release_confirmation_invalid')
        persisted = persist_owned_skill_release(session.directory, release, release_hash)
        return {'schema_version': '1.0', 'available': True, 'status': 'published',
                'persisted': True, 'release_sha256': persisted['release_sha256'],
                'release': persisted['release']}

    def _owned_release_rollback_evidence(self, session, release_sha256):
        import os

        from .owned_skill_release import owned_skill_selection_chain
        from .owned_form_candidate_execution import load_candidate_execution_bundle

        release = self._load_owned_release(release_sha256)
        chain = owned_skill_selection_chain(session.directory, release['family_sha256'])
        earlier_selection_hashes = {selection_hash for selection_hash, selection in chain
                                    if selection['release_sha256'] == release_sha256}
        if not earlier_selection_hashes:
            raise ValueError('owned_skill_rollback_release_not_previously_selected')
        bundle_root = session.directory / 'candidate-execution-bundles'
        try:
            execution_names = os.listdir(bundle_root)
        except FileNotFoundError:
            execution_names = []
        if len(execution_names) > 256:
            raise ValueError('owned_skill_rollback_execution_inventory_invalid')
        for execution_hash in sorted(execution_names):
            try:
                bundle, _ = load_candidate_execution_bundle(bundle_root, execution_hash)
                manifest, completion = bundle['manifest'], bundle.get('completion')
                if (manifest.get('schema_version') != '1.2'
                        or manifest.get('release_sha256') != release_sha256
                        or manifest.get('selection_sha256') not in earlier_selection_hashes
                        or completion is None):
                    continue
                job = self.store.connection.execute(
                    'SELECT kind,status,run_id FROM desktop_tasks WHERE job_id=?',
                    (completion['job_id'],)).fetchone()
                if (job is None or job['kind'] != 'browser_remote_form'
                        or job['status'] != 'succeeded' or job['run_id'] != completion['run_id']):
                    continue
                report = self.audit_owned_form_candidate_execution(execution_hash)
                if (report.get('available') is True and report.get('status') == 'verified'
                        and report.get('release_sha256') == release_sha256
                        and report.get('selection_sha256') in earlier_selection_hashes):
                    review = self.inspect_owned_candidate_review(manifest['review_sha256'])
                    if review['status'] == 'accepted':
                        return execution_hash
            except (OSError, ValueError, TypeError, KeyError):
                continue
        raise ValueError('owned_skill_rollback_evidence_unavailable')

    def _load_owned_release(self, release_sha256):
        from .owned_skill_release import load_owned_skill_release

        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_skill_release_source_unavailable')
        return load_owned_skill_release(session.directory, release_sha256)

    def preview_owned_skill_selection(self, release_sha256,
                                      expected_selection_sha256, operation):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned skill selection requires an idle scheduler')
        from .owned_skill_release import (
            current_owned_skill_selection, make_owned_skill_release,
            preview_owned_skill_selection)

        session = self.remote_form_owned_candidate_session
        release = self._load_owned_release(release_sha256)
        review = self.inspect_owned_candidate_review(release['review_sha256'])
        receipt = review['receipt']
        if review['status'] != 'accepted':
            raise ValueError('owned_skill_release_review_not_accepted')
        candidate, _source = session.reinspect(
            receipt['source_run_ref'], receipt['source_invocation_sha256'],
            receipt['candidate_sha256'])
        parent = (None if release['parent_release_sha256'] is None else
                  self._load_owned_release(release['parent_release_sha256']))
        expected_release_hash, expected_release = make_owned_skill_release(
            candidate, receipt['candidate_sha256'], release['review_sha256'],
            receipt, parent_release=parent)
        if expected_release_hash != release_sha256 or expected_release != release:
            raise ValueError('owned_skill_release_evidence_changed')
        from .owned_skill_release import list_owned_skill_release_families
        family_inventory = next((family for family in
                                 list_owned_skill_release_families(session.directory)
                                 if family['family_sha256'] == release['family_sha256']), None)
        latest_revision = max((entry['release']['revision'] for entry in
                               (family_inventory or {}).get('releases', [])), default=0)
        if operation == 'select' and release['revision'] != latest_revision:
            raise ValueError('owned_skill_older_release_requires_rollback')
        current_head, _current = current_owned_skill_selection(
            session.directory, release['family_sha256'])
        current_hash = None if current_head is None else current_head['selection_sha256']
        if current_hash != expected_selection_sha256:
            raise ValueError('owned_skill_selection_head_stale')
        evidence = None
        if operation == 'rollback':
            evidence = self._owned_release_rollback_evidence(session, release_sha256)
        elif operation != 'select':
            raise ValueError('owned_skill_selection_operation_invalid')
        preview = preview_owned_skill_selection(
            session.directory, release_sha256, expected_selection_sha256,
            operation, evidence)
        return {'schema_version': '1.0', 'available': True, 'status': 'preview',
                'persisted': False, 'selection_sha256': preview['selection_sha256'],
                'selection': preview['selection'], 'release_sha256': release_sha256,
                'review_sha256': release['review_sha256'],
                'family_sha256': release['family_sha256']}

    def commit_owned_skill_selection(self, release_sha256,
                                     expected_selection_sha256, operation,
                                     confirm_sha256):
        if self.busy or self.reserved:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Owned skill selection requires an idle scheduler')
        preview = self.preview_owned_skill_selection(
            release_sha256, expected_selection_sha256, operation)
        if confirm_sha256 != preview['selection_sha256']:
            raise ValueError('owned_skill_selection_confirmation_invalid')
        from .owned_skill_release import commit_owned_skill_selection

        session = self.remote_form_owned_candidate_session
        head = commit_owned_skill_selection(
            session.directory, preview['selection'], preview['selection_sha256'],
            expected_selection_sha256)
        if head['selection_sha256'] != preview['selection_sha256']:
            raise ValueError('owned_skill_selection_commit_changed')
        return {'schema_version': '1.0', 'available': True, 'status': 'selected',
                'persisted': True, 'selection_sha256': preview['selection_sha256'],
                'selection': preview['selection'], 'release_sha256': release_sha256,
                'review_sha256': preview['review_sha256'],
                'family_sha256': preview['family_sha256']}

    def catalog_owned_skill_releases(self):
        from .owned_skill_release import (list_owned_skill_release_families,
                                          make_owned_skill_release)

        session = self.remote_form_owned_candidate_session
        if session is None:
            raise ValueError('owned_skill_release_source_unavailable')
        result = []
        for item in list_owned_skill_release_families(session.directory):
            releases = []
            for entry in item['releases']:
                release_hash, release = entry['release_sha256'], entry['release']
                try:
                    review = self.inspect_owned_candidate_review(release['review_sha256'])
                    receipt = review['receipt']
                    candidate_session = self.remote_form_owned_candidate_session
                    candidate, _source = candidate_session.reinspect(
                        receipt['source_run_ref'], receipt['source_invocation_sha256'],
                        receipt['candidate_sha256'])
                    expected_hash, expected_release = make_owned_skill_release(
                        candidate, receipt['candidate_sha256'],
                        release['review_sha256'], receipt,
                        parent_release=(self._load_owned_release(
                            release['parent_release_sha256'])
                            if release['parent_release_sha256'] is not None else None))
                    if expected_hash != release_hash or expected_release != release:
                        raise ValueError('owned_skill_release_evidence_changed')
                    status = review['status']
                except (OSError, ValueError, TypeError, KeyError):
                    status = 'unavailable'
                rollback_eligible = False
                if status == 'accepted':
                    try:
                        self._owned_release_rollback_evidence(session, release_hash)
                        rollback_eligible = True
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                releases.append({'release_sha256': release_hash, 'release': release,
                                 'review_status': status,
                                 'rollback_eligible': rollback_eligible})
            if item['releases']:
                result.append({key: item[key] for key in (
                    'family_sha256', 'family', 'selection_sha256',
                    'selected_release_sha256', 'sequence')}
                    | {'releases': releases})
        return {'schema_version': '1.0', 'available': True, 'families': result}

    async def cancel(self, reason: str = 'pause', actor: str = 'local_authenticated_user'):
        await self.cancel_owned_skill_plan()
        try:
            self.clear_grant(reason)
        finally:
            try:
                await self.sequences.cancel()
            finally:
                try:
                    await self._cancel_job(reason, actor)
                    self.release_completed_runtime()
                finally:
                    if isinstance(self.engine, ReusableDeciderEngine) and self.engine.cpu_prewarm:
                        await self.engine.close()

    async def _cancel_job(self, reason: str, actor: str):
        try:
            self.clear_grant(reason)
        finally:
            await self._cancel_task(reason, actor)

    async def _cancel_task(self, reason: str, actor: str):
        self.pause_requested = False
        if self.operator:
            self.operator.interruption_phase = Phase.CANCELLED
        if self.paused:
            job = self.intervention(reason, actor)
            state = self.store.state(job['run_id'])
            cancelled = state.advance(Phase.CANCELLED, owner='PAUSED', owner_lease_id=identifier('revoked'))
            self.store.save_state(state, cancelled)
            self.store.finish(cancelled, 'cancelled', 'unknown')
            self.update(self.job_id, 'cancelled')
            if job['kind'] != 'hello' and self.active_runtime:
                self.active_runtime.stop()
            self.active_runtime = None
            self.operator = None
        if self.busy:
            job = self.store.connection.execute('SELECT run_id FROM desktop_tasks WHERE job_id=?', (self.job_id,)).fetchone()
            if job and job['run_id']:
                state = self.store.state(job['run_id'])
                with self.store.connection:
                    self.store.insert('human_interventions', intervention_id=identifier('intervention'), run_id=state.run_id,
                                      step_id=state.step_id, actor=actor, kind=reason,
                                      payload_json=canonical({'job_id': self.job_id}), created_at=now())
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            if job and job['run_id']:
                state = self.store.state(job['run_id'])
                if state.phase not in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                    cancelled = state.advance(Phase.CANCELLED, owner='PAUSED', owner_lease_id=identifier('revoked'))
                    self.store.save_state(state, cancelled)
                    self.store.finish(cancelled, 'cancelled', 'unknown')
            if self.active_runtime and self.active_runtime is not self.controller.runtime:
                self.active_runtime.stop()
            self.active_runtime = None
            self.operator = None
            self.update(self.job_id, 'cancelled')

    async def close(self):
        self.closed = True
        await self.cancel('stop', actor='runtime_shutdown')
        if self.knowledge_answer is not None:
            await self.knowledge_answer.close()
