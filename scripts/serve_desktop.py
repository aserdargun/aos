import argparse
import asyncio
from contextlib import asynccontextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import stat
from urllib.parse import urlsplit

import uvicorn

from aos.contracts import REPO_ROOT, Settings, canonical, digest
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.reusable_decider import ReusableDeciderEngine
from aos.desktop import DesktopRuntime
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_admission_history import ScientistAdmissionHistory, ScientistOutputContractPin
from aos.scientist_bootstrap import ScientistBootstrapCapture
from aos.scientist_bootstrap_factory import ScientistBootstrapAdmissionFactory
from aos.scientist_protocol import _reject_constant, _unique_object
from aos.scientist_transport import ScientistAdmissionError
from aos.scientist_lab_service import ScientistLabService, prepare_scientist_lab_startup
from aos.storage import TrajectoryStore
from aos.vision import BonsaiVisionSupervisor, FixtureVisionSupervisor
from aos.dataset_preflight import bounded_file
from aos.remote_learning_retention import RetentionTelemetry, run_managed_retention_loop
from aos.web_application import WebApplicationProfiles
from aos.web_application_binding import (WebReadOnlyRoutePlan, WebTaskContract,
                                         verify_web_readonly_routes)
from aos.web_static_assets import WebStaticAssetPlan
from aos.web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_bundle_plan
from aos.web_https_form_transport import (WebHTTPSFormPlan, exact_form_fields,
                                          form_body, parse_form_fields_document,
                                          verify_web_https_form_plan,
                                          verify_web_https_form_target_grant)
from aos.web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                            verify_web_https_form_state_plan)
from aos.web_https_preflight import validate_https_cookie_header


def prepare_console_assets(runtime, assets_root=None):
    root = REPO_ROOT / 'data/desktop-console-assets' if assets_root is None else Path(assets_root)
    if not root.is_absolute() or '..' in root.parts or root.resolve() != root:
        raise ValueError('Console assets require an absolute non-symlink root')
    assets = root / runtime.pins['image_id'].split(':')[1]
    if assets.is_symlink():
        raise ValueError('Console assets cannot follow a symbolic link')
    assets.mkdir(parents=True, exist_ok=True)
    runtime.docker(['cp', runtime.container_id + ':/usr/share/novnc/.', str(assets)])
    return assets


def main(*, scientist_confirm_runtime=None, scientist_lab_config=None, scientist_verify_lab_capability=None,
         scientist_admission_factory=None, scientist_output_contract=None, scientist_output_context_tokens=16384,
         scientist_bootstrap_expected_peer=None, scientist_bootstrap_factory=None,
         scientist_retained_resolver_factory=None):
    parser = argparse.ArgumentParser(description='Authenticated loopback-only AOS desktop console')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--local-ui-auto-login', action='store_true',
                        help='Allow same-origin loopback clients to obtain a session without entering the local token')
    parser.add_argument('--listen-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owned-form-listener-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owned-form-manifest-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--owned-parameter-project-directory', type=Path,
                        help='Explicit private prepared synthetic multi-field project; requires its manifest pin and owned listener')
    parser.add_argument('--owned-parameter-project-manifest-sha256',
                        help='Exact manifest confirmation for the prepared synthetic multi-field project')
    parser.add_argument('--owned-learning-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owned-skill-reuse-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--owned-synthetic-form-recipe', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--workspace', type=Path, default=REPO_ROOT / 'data/desktop-workspace')
    parser.add_argument('--console-assets-root', type=Path,
                        help='Explicit separate host noVNC asset directory for an isolated runtime')
    parser.add_argument('--database', type=Path, default=REPO_ROOT / 'data/desktop-console.sqlite')
    parser.add_argument('--managed-retention', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--trajectory-database', type=Path, default=REPO_ROOT / 'data/aos.sqlite')
    parser.add_argument('--web-profiles-root', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--knowledge-root', type=Path, default=REPO_ROOT / 'data/document-knowledge',
                        help='Private owner-only uploaded document and review store; no path ingestion')
    parser.add_argument('--site-knowledge-root', type=Path, default=None)
    parser.add_argument('--site-skills-root', type=Path, default=None)
    parser.add_argument('--page-seed-root', type=Path, default=None)
    parser.add_argument('--route-review-root', type=Path, default=None)
    parser.add_argument('--json-review-root', type=Path, default=None)
    parser.add_argument('--json-page-seed-root', type=Path, default=None)
    parser.add_argument('--web-task-root', type=Path, default=REPO_ROOT / 'data/web-task-drafts')
    parser.add_argument('--web-form-plan-root', type=Path, default=REPO_ROOT / 'data/web-form-plan-drafts')
    parser.add_argument('--web-form-value-root', type=Path, default=REPO_ROOT / 'data/web-form-value-drafts')
    parser.add_argument('--web-form-state-root', type=Path, default=REPO_ROOT / 'data/web-form-state-drafts')
    parser.add_argument('--web-route-root', type=Path, default=REPO_ROOT / 'data/web-route-drafts')
    parser.add_argument('--web-static-root', type=Path, default=REPO_ROOT / 'data/web-static-asset-drafts')
    parser.add_argument('--web-readonly-data-root', type=Path,
                        default=REPO_ROOT / 'data/web-readonly-data-drafts')
    parser.add_argument('--engine', choices=['disabled', 'decider', 'fixture', 'scientist'], default='disabled')
    parser.add_argument('--scientist-broker-socket', type=Path,
                        help='Explicit broker UDS; scientist mode additionally requires a jointly confirmed host runtime provider')
    parser.add_argument('--reuse-decider', action='store_true', help='Reuse pinned Decider within one task; release afterward unless bounded GPU idle retention is enabled')
    parser.add_argument('--prewarm-decider', action='store_true', help='Prepare one expiring CPU-only Decider while idle; requires --reuse-decider')
    parser.add_argument('--prewarm-idle-seconds', type=int, help='Bounded CPU-only idle preparation lifetime; requires --prewarm-decider')
    parser.add_argument('--gpu-idle-seconds', type=int, help='Keep a successful Decider GPU worker briefly between tasks; requires CPU prewarm')
    parser.add_argument('--decider-manifest', type=Path, default=REPO_ROOT / 'models/decider-manifest.json')
    parser.add_argument('--model-python', type=Path, default=Path.home() / '.venv/bin/python')
    parser.add_argument('--browser-tasks', action='store_true')
    parser.add_argument('--desktop-browser', action='store_true', help='Run the fixed browser form visibly in the owned desktop')
    parser.add_argument('--desktop-mcp-manifest', type=Path, help='Use prepared Playwright MCP for the visible fixed form; requires --desktop-browser')
    parser.add_argument('--desktop-navigation-mcp-manifest', type=Path,
                        help='Use prepared Playwright MCP only for fixed local navigation; preserve the form transport')
    parser.add_argument('--desktop-staging-mcp-manifest', type=Path,
                        help='Opt in to the fixed synthetic local staging workflow; never enables real-site access')
    parser.add_argument('--staging-fixture-port', type=int,
                        help='Pin the local staging HTTP proxy port for an isolated test; requires staging MCP')
    parser.add_argument('--remote-entry-mcp-manifest', type=Path,
                        help='Opt in to one confirmed read-only remote HTTPS entry on the owned MCP desktop')
    parser.add_argument('--remote-entry-profile-sha256',
                        help='Exact registered remote profile hash for the one-entry task')
    parser.add_argument('--remote-entry-task-file', type=Path,
                        help='Private canonical web task contract for the one-entry task')
    parser.add_argument('--remote-entry-task-sha256',
                        help='Optional exact SHA-256 confirmation of the private remote task file')
    parser.add_argument('--remote-routes-plan-file', type=Path,
                        help='Private canonical ordered HTTPS route plan for separate per-route approvals')
    parser.add_argument('--remote-routes-plan-sha256',
                        help='Exact SHA-256 confirmation of the private route plan')
    parser.add_argument('--remote-static-assets-plan-file', type=Path,
                        help='Private canonical exact HTTPS JS/CSS bundle plan')
    parser.add_argument('--remote-static-assets-plan-sha256',
                        help='Exact SHA-256 confirmation of the private static asset plan')
    parser.add_argument('--remote-route-review-source-database', type=Path,
                        help='Owner-only historical route trajectory database under data')
    parser.add_argument('--remote-route-review-site-store', type=Path,
                        help='Owner-only reviewed page draft store under data')
    parser.add_argument('--remote-route-review-store', type=Path,
                        help='Owner-only route review store under data')
    parser.add_argument('--remote-route-review-sha256',
                        help='Exact metadata review record to consult only after fresh route readback')
    parser.add_argument('--remote-route-review-source-snapshot-sha256',
                        help=argparse.SUPPRESS)
    parser.add_argument('--remote-json-review-source-database', type=Path,
                        help='Owner-only historical JSON-bundle trajectory database under data')
    parser.add_argument('--remote-json-review-site-store', type=Path,
                        help='Owner-only JSON page draft store under data')
    parser.add_argument('--remote-json-review-store', type=Path,
                        help='Owner-only JSON metadata review store under data')
    parser.add_argument('--remote-json-review-sha256',
                        help='Exact JSON metadata review record to recheck after browser readback')
    parser.add_argument('--remote-json-review-source-snapshot-sha256',
                        help=argparse.SUPPRESS)
    parser.add_argument('--remote-form-plan-file', type=Path,
                        help='Private canonical public HTTPS form plan')
    parser.add_argument('--remote-form-plan-sha256',
                        help='Exact SHA-256 confirmation of the private form plan')
    parser.add_argument('--remote-form-field-name',
                        help='Exact public HTTPS form field name')
    parser.add_argument('--remote-form-value-file', type=Path,
                        help='Private owner-only UTF-8 form value; never put the value on argv')
    parser.add_argument('--remote-form-fields-file', type=Path,
                        help='Private canonical ordered HTTPS form fields; never put values on argv')
    parser.add_argument('--remote-form-fields-sha256',
                        help='Exact SHA-256 confirmation of the private ordered fields file')
    parser.add_argument('--remote-form-public-plan-sha256',
                        help='Separate exact public target plan grant')
    parser.add_argument('--remote-form-state-plan-file', type=Path,
                        help='Private canonical state readback plan for the exact HTTPS form')
    parser.add_argument('--remote-form-state-plan-sha256',
                        help='Exact SHA-256 confirmation of the private state plan')
    parser.add_argument('--remote-form-public-state-plan-sha256',
                        help='Separate exact public state readback grant')
    parser.add_argument('--remote-form-skill-plan-file', type=Path)
    parser.add_argument('--remote-form-skill-plan-sha256')
    parser.add_argument('--remote-form-skill-case-inputs-file', type=Path)
    parser.add_argument('--remote-form-skill-case-inputs-sha256')
    parser.add_argument('--remote-form-skill-field-bindings-file', type=Path)
    parser.add_argument('--remote-form-skill-field-bindings-sha256')
    parser.add_argument('--remote-form-skill-case-key')
    parser.add_argument('--remote-form-skill-invocation-sha256')
    parser.add_argument('--remote-form-skill-recipe-file', type=Path,
                        help='Private canonical executable synthetic form recipe')
    parser.add_argument('--remote-form-skill-recipe-file-sha256',
                        help='Exact SHA-256 confirmation of the private recipe file')
    parser.add_argument('--remote-form-cookie-file', type=Path,
                        help='Private owner-only static Cookie request value for the exact public form')
    parser.add_argument('--remote-form-cookie-sha256',
                        help='Exact SHA-256 confirmation of the private Cookie value')
    parser.add_argument('--desktop-vision', action='store_true', help='Run the fixed vision canvas visibly in the owned desktop')
    parser.add_argument('--browser-manifest', type=Path, default=REPO_ROOT / 'models/browser-manifest.json')
    parser.add_argument('--vision-engine', choices=['disabled', 'bonsai', 'fixture'], default='disabled')
    parser.add_argument('--synthetic-learning-stream-dir', type=Path,
                        help='Permit per-task opt-in synthetic metadata only in this private outbox')
    parser.add_argument('--bonsai-manifest', type=Path, default=REPO_ROOT / 'models/bonsai-manifest.json')
    arguments = parser.parse_args()
    owned_parameter_startup = None
    project_options = (arguments.owned_parameter_project_directory,
                       arguments.owned_parameter_project_manifest_sha256)
    if any(option is not None for option in project_options):
        conflicting_sources = [value for name, value in vars(arguments).items()
                               if name.startswith('remote_')
                               and name != 'remote_entry_mcp_manifest' and value is not None]
        if (not all(option is not None for option in project_options)
                or arguments.owned_form_listener_fd is None
                or arguments.owned_form_manifest_sha256 is not None
                or arguments.owned_synthetic_form_recipe
                or arguments.owned_skill_reuse_sha256 is not None
                or arguments.engine != 'fixture'
                or arguments.vision_engine not in {'disabled', 'fixture'}
                or arguments.reuse_decider or arguments.prewarm_decider
                or arguments.prewarm_idle_seconds is not None
                or arguments.gpu_idle_seconds is not None
                or not arguments.browser_tasks or not arguments.desktop_browser
                or arguments.desktop_mcp_manifest is not None
                or arguments.desktop_navigation_mcp_manifest is not None
                or arguments.desktop_staging_mcp_manifest is not None
                or arguments.staging_fixture_port is not None
                or conflicting_sources):
            parser.error('Owned parameter project requires complete explicit pins, an owned listener and a separate CPU-fixture manual browser session; native GPU model paths are forbidden')
        try:
            from aos.owned_parameter_project_startup import load_owned_parameter_project_startup

            owned_parameter_startup = load_owned_parameter_project_startup(
                *project_options, arguments.owned_form_listener_fd)
            project = owned_parameter_startup.bundle
            if arguments.web_profiles_root.absolute() not in {
                    REPO_ROOT / 'data/web-applications', project['profiles_root']}:
                raise ValueError('owned_parameter_project_profile_root_mismatch')
            arguments.web_profiles_root = project['profiles_root']
            arguments.remote_entry_mcp_manifest = arguments.remote_entry_mcp_manifest or (
                REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
            arguments.remote_entry_profile_sha256 = project['profile_sha256']
            arguments.remote_entry_task_file = project['directory'] / 'remote-entry-task.json'
            arguments.remote_entry_task_sha256 = project['source_pins']['remote-entry-task.json']
        except (OSError, ValueError, TypeError, KeyError):
            parser.error('Owned parameter project sources or listener are unavailable or invalid')
    if scientist_retained_resolver_factory is not None:
        if (arguments.engine != 'scientist' or not callable(scientist_retained_resolver_factory)
                or not callable(getattr(scientist_retained_resolver_factory, 'verify_configuration', None))
                or (scientist_bootstrap_factory is None and (
                    scientist_admission_factory is None or scientist_output_contract is None))):
            parser.error('Scientist retained resolution requires an explicit verified trusted factory; no runtime started')
        try:
            if scientist_retained_resolver_factory.verify_configuration() is not None:
                raise ScientistAdmissionError('Retained factory configuration must complete or raise')
        except Exception:
            parser.error('Scientist retained resolution configuration is unavailable or incompatible; no runtime started')
    if scientist_bootstrap_factory is not None:
        if (type(scientist_bootstrap_factory) is not ScientistBootstrapAdmissionFactory
                or arguments.engine != 'scientist'
                or any(value is not None for value in (scientist_confirm_runtime, scientist_admission_factory,
                    scientist_bootstrap_expected_peer, scientist_output_contract))):
            parser.error('Scientist bootstrap requires one concrete trusted factory without mixed admission/runtime/peer/output hooks')
        try:
            scientist_output_contract = scientist_bootstrap_factory.output_contract
        except (TypeError, ValueError, ScientistAdmissionError):
            parser.error('Scientist bootstrap factory output contract is unavailable or incompatible; no runtime started')
        scientist_confirm_runtime = scientist_bootstrap_factory.confirm_runtime
        scientist_admission_factory = scientist_bootstrap_factory
        scientist_bootstrap_expected_peer = scientist_bootstrap_factory.expected_peer
    if scientist_bootstrap_expected_peer is not None and (
            not callable(scientist_bootstrap_expected_peer) or scientist_admission_factory is None):
        parser.error('Scientist bootstrap requires an explicit peer reader and original admission factory')
    if scientist_admission_factory is not None or scientist_output_contract is not None:
        if (arguments.engine != 'scientist' or not callable(scientist_admission_factory)
                or scientist_output_contract is None):
            parser.error('Scientist original admission requires a trusted factory and pinned output contract together')
        try:
            contract = scientist_output_contract
            if isinstance(contract, ScientistOutputContractPin):
                contract = contract.model_dump(mode='json')
            scientist_output_contract = ScientistOutputContractPin.model_validate(contract, strict=True).model_dump(mode='json')
            if type(scientist_output_context_tokens) is not int or not 256 <= scientist_output_context_tokens <= 16384:
                raise ValueError('Scientist output context limit is invalid')
        except (TypeError, ValueError):
            parser.error('Scientist output contract or trusted context limit is invalid; no runtime started')
    elif scientist_output_context_tokens != 16384 or type(scientist_output_context_tokens) is not int:
        parser.error('Scientist output context configuration requires the original admission factory and contract')
    lab_startup = None
    if scientist_lab_config is not None:
        if arguments.engine != 'scientist' or not callable(scientist_verify_lab_capability):
            parser.error('Lab startup requires Scientist engine and an explicit trusted joint Lab capability verifier')
        try:
            lab_startup = prepare_scientist_lab_startup(scientist_lab_config)
        except (OSError, ValueError, ScientistAdmissionError):
            parser.error('Lab startup authority/principal/suites/private credential are unavailable or incompatible')
    elif scientist_verify_lab_capability is not None:
        parser.error('Lab capability verifier requires explicit host Lab startup configuration')
    scientist_pins = None
    if arguments.engine == 'scientist':
        if not callable(scientist_confirm_runtime):
            parser.error('Scientist runtime is not jointly admitted: trusted host capability/version provider required; no native fallback')
        if (arguments.scientist_broker_socket is None or not arguments.scientist_broker_socket.is_absolute()
                or '..' in arguments.scientist_broker_socket.parts):
            parser.error('Scientist mode requires an explicit absolute --scientist-broker-socket')
        if (arguments.reuse_decider or arguments.prewarm_decider or arguments.gpu_idle_seconds
                or arguments.owned_synthetic_form_recipe
                or arguments.owned_skill_reuse_sha256 is not None or arguments.owned_form_listener_fd is not None
                or arguments.vision_engine == 'fixture'):
            parser.error('Scientist mode forbids native reuse/prewarm/idle and unbrokered owned model paths')
        try:
            scientist_pins = json.loads(bounded_file(arguments.decider_manifest.absolute()),
                                       object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(scientist_pins, dict):
                raise ValueError('Pinned Scientist Decider manifest must be an object')
            profiles = {'aos.decider.turn.v1': digest(scientist_pins)}
            if arguments.vision_engine == 'bonsai':
                from aos.scientist_supervisor import ScientistBonsaiVisionSupervisor

                profiles['aos.bonsai.vision.v1'] = ScientistBonsaiVisionSupervisor(
                    arguments.bonsai_manifest, None)._deployment_digest
            if scientist_confirm_runtime(profiles.copy()) is not None:
                raise ScientistAdmissionError('Joint runtime verifier must complete or raise')
        except (OSError, ValueError, ScientistAdmissionError):
            parser.error('Scientist pinned source/capability is unavailable or incompatible; no runtime started')
    elif arguments.scientist_broker_socket is not None:
        parser.error('--scientist-broker-socket requires --engine scientist')
    recipe_options = (arguments.remote_form_skill_recipe_file,
                      arguments.remote_form_skill_recipe_file_sha256)
    if any(option is not None for option in recipe_options):
        if (not all(option is not None for option in recipe_options)
                or arguments.owned_form_listener_fd is not None):
            parser.error('Executable recipe requires both private pins and a direct session')
    owned_form_directory = None
    owned_form_manifest = None
    owned_skill_reuse_material = None
    manager_base = arguments.workspace.absolute().parent.parent
    manager_project = (re.fullmatch(r'local-app-project-([a-z0-9][a-z0-9-]{0,47})', manager_base.name)
                       if manager_base.parent == REPO_ROOT / 'data' else None)
    manager_scope = ({'project': manager_project.group(1), 'port': arguments.port}
                     if manager_project is not None else None)

    def scoped_manager():
        if manager_scope is None:
            return nullcontext()
        from aos.local_app import LocalAppInstance, instance_scope
        return instance_scope(LocalAppInstance.for_project(manager_scope['project'], manager_scope['port']))

    if (arguments.owned_learning_lock_fd is not None
            and (arguments.owned_learning_lock_fd < 0 or arguments.owned_form_listener_fd is None)):
        parser.error('Owned learning lock requires its private owned form source')
    if arguments.owned_skill_reuse_sha256 is not None:
        if (arguments.owned_learning_lock_fd is None or arguments.owned_form_listener_fd is None
                or arguments.owned_synthetic_form_recipe or arguments.managed_retention):
            parser.error('Owned skill reuse requires its locked v1 source and separate manager workspace')
        try:
            from aos.local_app import load_owned_skill_reuse_startup

            manager_directory = arguments.workspace.absolute().parent
            with scoped_manager():
                owned_skill_reuse_material, _previous, retained_directory = load_owned_skill_reuse_startup(
                    manager_directory, arguments.owned_skill_reuse_sha256)
            if (arguments.workspace.absolute() != manager_directory / 'workspace'
                    or arguments.database.absolute() != retained_directory / 'store.sqlite'
                    or manager_directory == retained_directory
                    or arguments.owned_form_manifest_sha256
                    != owned_skill_reuse_material['preview']['source_manifest_sha256']):
                raise ValueError('Owned skill reuse paths differ from retained source')
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            parser.error('Owned skill reuse source changed or is unavailable')
    if (owned_parameter_startup is None and (arguments.owned_form_listener_fd is None)
            != (arguments.owned_form_manifest_sha256 is None)):
        parser.error('Owned synthetic form requires its listener and manifest pin')
    if arguments.owned_synthetic_form_recipe and arguments.owned_form_listener_fd is None:
        parser.error('Owned recipe mode requires its inherited private fixture listener')
    if owned_parameter_startup is not None:
        owned_form_directory = project['directory']
        owned_form_manifest = project['manifest']
    elif arguments.owned_form_listener_fd is not None:
        try:
            from aos.owned_form_invocation_session import verify_owned_form_invocation_manifest

            session_dir = arguments.database.absolute().parent
            if (session_dir.parent.parent != REPO_ROOT / 'data'
                    or re.fullmatch(r'local-app-(?:v1|test-[a-f0-9]{32}|project-[a-z0-9][a-z0-9-]{0,47})',
                                    session_dir.parent.name) is None
                    or re.fullmatch(r'app-[a-f0-9]{32}', session_dir.name) is None
                    or not 1024 <= arguments.port <= 65535):
                raise ValueError('owned_form_session_scope_invalid')
            owned_form_directory = session_dir / 'owned-form'
            owned_form_manifest = verify_owned_form_invocation_manifest(
                owned_form_directory, arguments.owned_form_manifest_sha256)
            manifest_is_recipe = owned_form_manifest.get('mode') == 'owned_synthetic_form_recipe'
            if (arguments.owned_synthetic_form_recipe is not manifest_is_recipe
                    or manifest_is_recipe != ('recipe_sha256' in owned_form_manifest)):
                raise ValueError('owned_form_recipe_mode_mismatch')
            arguments.web_profiles_root = owned_form_directory / 'profiles'
            arguments.remote_entry_mcp_manifest = arguments.remote_entry_mcp_manifest or (
                REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
            arguments.remote_entry_profile_sha256 = owned_form_manifest['profile_sha256']
            arguments.remote_entry_task_file = owned_form_directory / 'remote-entry-task.json'
            arguments.remote_entry_task_sha256 = owned_form_manifest['sources']['remote-entry-task.json']
            arguments.remote_form_plan_file = owned_form_directory / 'remote-form-plan.json'
            arguments.remote_form_plan_sha256 = owned_form_manifest['form_plan_sha256']
            arguments.remote_form_field_name = 'message'
            arguments.remote_form_value_file = owned_form_directory / 'remote-form-value.txt'
            arguments.remote_form_state_plan_file = owned_form_directory / 'remote-form-state-plan.json'
            arguments.remote_form_state_plan_sha256 = owned_form_manifest['state_plan_sha256']
            arguments.remote_form_skill_plan_file = owned_form_directory / 'remote-form-skill-plan.json'
            arguments.remote_form_skill_plan_sha256 = owned_form_manifest['sources']['remote-form-skill-plan.json']
            arguments.remote_form_skill_case_inputs_file = owned_form_directory / 'remote-form-skill-case-inputs.json'
            arguments.remote_form_skill_case_inputs_sha256 = owned_form_manifest['sources']['remote-form-skill-case-inputs.json']
            arguments.remote_form_skill_field_bindings_file = owned_form_directory / 'remote-form-skill-field-bindings.json'
            arguments.remote_form_skill_field_bindings_sha256 = owned_form_manifest['sources']['remote-form-skill-field-bindings.json']
            arguments.remote_form_skill_case_key = 'dev-query'
            arguments.remote_form_skill_invocation_sha256 = owned_form_manifest['invocation_sha256']
            if manifest_is_recipe:
                arguments.remote_form_skill_recipe_file = (
                    owned_form_directory / 'remote-form-skill-recipe.json')
                arguments.remote_form_skill_recipe_file_sha256 = (
                    owned_form_manifest['sources']['remote-form-skill-recipe.json'])
        except (OSError, ValueError, TypeError, KeyError):
            parser.error('Owned synthetic form sources are unavailable or invalid')
    if arguments.managed_retention:
        managed_session = arguments.database.absolute().parent
        if (managed_session.parent != REPO_ROOT / 'data/local-app-v1'
                or re.fullmatch(r'app-[a-f0-9]{32}', managed_session.name) is None
                or arguments.database.name != 'store.sqlite'
                or arguments.workspace.absolute() != managed_session / 'workspace'):
            parser.error('Managed retention requires the pinned private local-app session')
    if not 1024 <= arguments.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    if arguments.listen_fd is not None:
        with socket.socket(fileno=os.dup(arguments.listen_fd)) as listener:
            if (listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM
                    or listener.getsockname() != ('127.0.0.1', arguments.port)):
                parser.error('Inherited listener must match the pinned loopback address')
    if arguments.browser_tasks and arguments.engine == 'disabled':
        parser.error('Browser tasks require an explicit decision engine')
    if arguments.reuse_decider and arguments.engine != 'decider':
        parser.error('Decider reuse requires --engine decider')
    if arguments.prewarm_decider and not arguments.reuse_decider:
        parser.error('Decider CPU prewarm requires --reuse-decider')
    if arguments.prewarm_idle_seconds is not None and (
            not arguments.prewarm_decider or not 1 <= arguments.prewarm_idle_seconds <= 600):
        parser.error('Decider CPU idle lifetime requires --prewarm-decider and 1–600 seconds')
    if arguments.gpu_idle_seconds is not None and (
            not arguments.prewarm_decider or not 1 <= arguments.gpu_idle_seconds <= 120):
        parser.error('Decider GPU idle lifetime requires --prewarm-decider and 1–120 seconds')
    if arguments.desktop_browser and not arguments.browser_tasks:
        parser.error('Visible desktop browser requires --browser-tasks')
    if arguments.desktop_mcp_manifest is not None and not arguments.desktop_browser:
        parser.error('Playwright MCP requires --desktop-browser')
    if arguments.desktop_navigation_mcp_manifest is not None and not arguments.desktop_browser:
        parser.error('Local navigation MCP requires --desktop-browser')
    if arguments.desktop_navigation_mcp_manifest is not None and arguments.desktop_mcp_manifest is not None:
        parser.error('Choose one Playwright MCP browser configuration')
    if arguments.desktop_staging_mcp_manifest is not None and not arguments.desktop_browser:
        parser.error('Synthetic staging MCP requires --desktop-browser')
    if arguments.desktop_staging_mcp_manifest is not None and arguments.desktop_mcp_manifest is not None:
        parser.error('Synthetic staging requires a separate MCP browser configuration')
    if arguments.staging_fixture_port is not None and (
            arguments.desktop_staging_mcp_manifest is None
            or not 1024 <= arguments.staging_fixture_port <= 65535):
        parser.error('Pinned staging port requires staging MCP and a nonprivileged port')
    remote_options = (arguments.remote_entry_mcp_manifest,
                      arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file)
    if arguments.remote_entry_task_sha256 is not None and (
            len(arguments.remote_entry_task_sha256) != 64
            or any(character not in '0123456789abcdef' for character in arguments.remote_entry_task_sha256)
            or not all(option is not None for option in remote_options)):
        parser.error('Remote entry task confirmation requires complete exact remote entry options')
    if any(option is not None for option in remote_options) and (
            not all(option is not None for option in remote_options)
            or not arguments.desktop_browser or arguments.engine == 'disabled'):
        parser.error('Remote entry requires explicit MCP, profile, task, visible browser and decision engine')
    remote_task = None
    if all(option is not None for option in remote_options):
        try:
            content = bounded_file(arguments.remote_entry_task_file, 65536)
            if (arguments.remote_entry_task_sha256 is not None
                    and hashlib.sha256(content).hexdigest() != arguments.remote_entry_task_sha256):
                raise ValueError('Remote entry task confirmation differs')
            remote_task = WebTaskContract.model_validate_json(content)
        except (OSError, ValueError, TypeError):
            parser.error('Remote entry task file is unavailable or invalid')
    remote_routes_plan = None
    if arguments.remote_routes_plan_file is not None or arguments.remote_routes_plan_sha256 is not None:
        if (arguments.remote_routes_plan_file is None or arguments.remote_routes_plan_sha256 is None
                or remote_task is None or len(arguments.remote_routes_plan_sha256) != 64
                or any(character not in '0123456789abcdef'
                       for character in arguments.remote_routes_plan_sha256)):
            parser.error('Read-only routes require a complete pinned remote task and plan')
        try:
            from aos.local_app import private_read

            plan_file = arguments.remote_routes_plan_file.absolute()
            if not plan_file.is_relative_to(REPO_ROOT / 'data'):
                raise ValueError('Read-only route plan must be private under data')
            content = private_read(plan_file, 20000)
            if hashlib.sha256(content).hexdigest() != arguments.remote_routes_plan_sha256:
                raise ValueError('Read-only route plan confirmation differs')
            remote_routes_plan = WebReadOnlyRoutePlan.model_validate_json(content)
            if content != canonical(remote_routes_plan.model_dump(mode='json')).encode():
                raise ValueError('Read-only route plan is not canonical')
            verify_web_readonly_routes(WebApplicationProfiles(arguments.web_profiles_root),
                                       remote_task, remote_routes_plan)
        except (OSError, ValueError, TypeError):
            parser.error('Read-only route plan file is unavailable or invalid')
    remote_static_assets_plan = None
    if (arguments.remote_static_assets_plan_file is not None
            or arguments.remote_static_assets_plan_sha256 is not None):
        if (arguments.remote_static_assets_plan_file is None
                or arguments.remote_static_assets_plan_sha256 is None
                or remote_task is None
                or re.fullmatch('[a-f0-9]{64}',
                                arguments.remote_static_assets_plan_sha256) is None):
            parser.error('Static assets require a complete pinned remote task and plan')
        try:
            from aos.local_app import private_read

            plan_file = arguments.remote_static_assets_plan_file.absolute()
            if not plan_file.is_relative_to(REPO_ROOT / 'data'):
                raise ValueError('Static asset plan must be private under data')
            content = private_read(plan_file, 32000)
            if hashlib.sha256(content).hexdigest() != arguments.remote_static_assets_plan_sha256:
                raise ValueError('Static asset plan confirmation differs')
            def unique_pairs(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('Web bundle plan has a duplicate JSON field')
                    value[key] = item
                return value
            document = json.loads(content, object_pairs_hook=unique_pairs)
            if not isinstance(document, dict):
                raise ValueError('Web bundle plan must be an object')
            plan_type = {'1.0': WebStaticAssetPlan,
                         '2.0': WebReadOnlyDataBundlePlan}.get(document.get('schema_version'))
            if plan_type is None:
                raise ValueError('Unsupported web bundle plan version')
            remote_static_assets_plan = plan_type.model_validate(document)
            if content != canonical(remote_static_assets_plan.model_dump(mode='json')).encode():
                raise ValueError('Static asset plan is not canonical')
            verify_web_bundle_plan(WebApplicationProfiles(arguments.web_profiles_root),
                                   remote_task, remote_static_assets_plan)
        except (OSError, ValueError, TypeError):
            parser.error('Static asset plan file is unavailable or invalid')
    route_review_options = (arguments.remote_route_review_source_database,
                            arguments.remote_route_review_site_store,
                            arguments.remote_route_review_store,
                            arguments.remote_route_review_sha256)
    if (any(option is not None for option in route_review_options)
            or arguments.remote_route_review_source_snapshot_sha256 is not None):
        if remote_routes_plan is None or not all(option is not None for option in route_review_options):
            parser.error('Read-only route review requires complete source, stores and exact hash')
        try:
            from aos.remote_route_knowledge_review import prepare_live_remote_route_knowledge

            for source in route_review_options[:3]:
                if not source.absolute().is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('Remote route review sources must stay under data')
            metadata = arguments.remote_route_review_source_database.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise ValueError('Remote route review source must be private')
            pin = prepare_live_remote_route_knowledge(
                arguments.remote_route_review_source_database,
                profiles=arguments.web_profiles_root,
                site_store=arguments.remote_route_review_site_store,
                review_store=arguments.remote_route_review_store,
                review_sha256=arguments.remote_route_review_sha256,
                selected_profile_sha256=arguments.remote_entry_profile_sha256,
                selected_plan_sha256=arguments.remote_routes_plan_sha256)
            if (arguments.remote_route_review_source_snapshot_sha256 is not None
                    and pin.source_snapshot_sha256
                    != arguments.remote_route_review_source_snapshot_sha256):
                raise ValueError('Managed route review source snapshot changed')
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            parser.error('Read-only route review source is unavailable or stale')
    json_review_options = (arguments.remote_json_review_source_database,
                           arguments.remote_json_review_site_store,
                           arguments.remote_json_review_store,
                           arguments.remote_json_review_sha256)
    if (any(option is not None for option in json_review_options)
            or arguments.remote_json_review_source_snapshot_sha256 is not None):
        if (not isinstance(remote_static_assets_plan, WebReadOnlyDataBundlePlan)
                or not all(option is not None for option in json_review_options)):
            parser.error('JSON review requires a complete v2 source, stores and exact hash')
        try:
            from aos.remote_readonly_data_knowledge_review import (
                prepare_live_remote_readonly_data_knowledge)

            for source in json_review_options[:3]:
                if not source.absolute().is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('JSON review sources must stay under data')
            metadata = arguments.remote_json_review_source_database.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise ValueError('JSON review source must be private')
            pin = prepare_live_remote_readonly_data_knowledge(
                arguments.remote_json_review_source_database,
                profiles=arguments.web_profiles_root,
                site_store=arguments.remote_json_review_site_store,
                review_store=arguments.remote_json_review_store,
                review_sha256=arguments.remote_json_review_sha256,
                selected_profile_sha256=arguments.remote_entry_profile_sha256,
                selected_plan_sha256=arguments.remote_static_assets_plan_sha256)
            if (arguments.remote_json_review_source_snapshot_sha256 is not None
                    and pin.source_snapshot_sha256
                    != arguments.remote_json_review_source_snapshot_sha256):
                raise ValueError('Managed JSON review source snapshot changed')
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            parser.error('JSON review source is unavailable or stale')
    remote_form_plan = None
    remote_form_value = None
    remote_form_fields = None
    remote_form_state_plan = None
    remote_form_cookie = None
    remote_form_skill_invocation = None
    remote_form_skill_revalidator = None
    skill_source_options = (arguments.remote_form_skill_plan_file,
                            arguments.remote_form_skill_plan_sha256,
                            arguments.remote_form_skill_case_inputs_file,
                            arguments.remote_form_skill_case_inputs_sha256,
                            arguments.remote_form_skill_field_bindings_file,
                            arguments.remote_form_skill_field_bindings_sha256,
                            arguments.remote_form_skill_case_key,
                            arguments.remote_form_skill_invocation_sha256)
    synthetic_skill_invocation = any(option is not None for option in skill_source_options)
    if arguments.remote_form_skill_recipe_file is not None and not synthetic_skill_invocation:
        parser.error('Executable recipe requires exact skill invocation sources')
    if synthetic_skill_invocation and not all(option is not None for option in skill_source_options):
        parser.error('Synthetic skill invocation requires all exact private source pins')
    remote_form_options = (arguments.remote_form_plan_file,
                           arguments.remote_form_plan_sha256,
                           arguments.remote_form_field_name,
                           arguments.remote_form_value_file,
                           arguments.remote_form_fields_file,
                           arguments.remote_form_fields_sha256,
                           arguments.remote_form_public_plan_sha256)
    if any(option is not None for option in remote_form_options):
        multi_field = arguments.remote_form_fields_file is not None
        if (arguments.remote_form_plan_file is None
                or arguments.remote_form_plan_sha256 is None
                or (arguments.remote_form_public_plan_sha256 is None
                    and not synthetic_skill_invocation)
                or remote_task is None
                or re.fullmatch('[a-f0-9]{64}', arguments.remote_form_plan_sha256) is None
                or multi_field and (
                    arguments.remote_form_field_name is not None
                    or arguments.remote_form_value_file is not None
                    or re.fullmatch('[a-f0-9]{64}',
                                    arguments.remote_form_fields_sha256 or '') is None)
                or not multi_field and (
                    arguments.remote_form_fields_sha256 is not None
                    or arguments.remote_form_value_file is None
                    or re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',
                                    arguments.remote_form_field_name or '') is None)):
            parser.error('Public HTTPS form requires complete exact profile, plan and one private input mode')
        try:
            from aos.local_app import private_read

            plan_file = arguments.remote_form_plan_file.absolute()
            value_file = (arguments.remote_form_fields_file if multi_field
                          else arguments.remote_form_value_file).absolute()
            if (not plan_file.is_relative_to(REPO_ROOT / 'data')
                    or not value_file.is_relative_to(REPO_ROOT / 'data')):
                raise ValueError('Public form input must remain under ignored data')
            content = private_read(plan_file, 20000)
            if hashlib.sha256(content).hexdigest() != arguments.remote_form_plan_sha256:
                raise ValueError('Public form plan confirmation differs')
            remote_form_plan = WebHTTPSFormPlan.model_validate_json(content)
            if content != canonical(remote_form_plan.model_dump(mode='json')).encode():
                raise ValueError('Public form plan is not canonical')
            profiles = WebApplicationProfiles(arguments.web_profiles_root)
            verify_web_https_form_plan(profiles, remote_task, remote_form_plan)
            public_target = verify_web_https_form_target_grant(
                remote_form_plan, arguments.remote_form_public_plan_sha256)
            if (public_target is not True and not (
                    synthetic_skill_invocation and public_target is False
                    and (urlsplit(remote_form_plan.entry_url).hostname or '').endswith('.invalid'))):
                raise ValueError('Public form target grant is absent')
            raw_input = private_read(value_file, 8192 if multi_field else 4096)
            if multi_field:
                if hashlib.sha256(raw_input).hexdigest() != arguments.remote_form_fields_sha256:
                    raise ValueError('Public form fields confirmation differs')
                fields = parse_form_fields_document(raw_input)
                remote_form_fields = [{'name': name, 'value': value} for name, value in fields]
            else:
                remote_form_value = raw_input.decode('utf-8')
                if not remote_form_value.isprintable():
                    raise ValueError('Public form value is not printable')
                fields = exact_form_fields(arguments.remote_form_field_name, remote_form_value)
            body = form_body(fields)
            if (len(body) != remote_form_plan.body_bytes
                    or hashlib.sha256(body).hexdigest() != remote_form_plan.body_sha256):
                raise ValueError('Public form value differs from plan')
        except (OSError, ValueError, TypeError):
            parser.error('Public HTTPS form plan or private value is unavailable or invalid')
    state_options = (arguments.remote_form_state_plan_file,
                     arguments.remote_form_state_plan_sha256,
                     arguments.remote_form_public_state_plan_sha256)
    if any(option is not None for option in state_options):
        if (arguments.remote_form_state_plan_file is None
                or arguments.remote_form_state_plan_sha256 is None
                or remote_form_plan is None
                or (arguments.remote_form_public_state_plan_sha256 is None
                    and not synthetic_skill_invocation)
                or re.fullmatch('[a-f0-9]{64}', arguments.remote_form_state_plan_sha256) is None):
            parser.error('HTTPS form state requires complete exact plan and separate grant')
        try:
            state_file = arguments.remote_form_state_plan_file.absolute()
            if not state_file.is_relative_to(REPO_ROOT / 'data'):
                raise ValueError('HTTPS form state input must remain under ignored data')
            content = private_read(state_file, 20000)
            if hashlib.sha256(content).hexdigest() != arguments.remote_form_state_plan_sha256:
                raise ValueError('HTTPS form state plan confirmation differs')
            remote_form_state_plan = WebHTTPSFormStatePlan.model_validate_json(content)
            if content != canonical(remote_form_state_plan.model_dump(mode='json')).encode():
                raise ValueError('HTTPS form state plan is not canonical')
            verify_web_https_form_state_plan(
                WebApplicationProfiles(arguments.web_profiles_root), remote_task,
                remote_form_plan, remote_form_state_plan, ssl.create_default_context(),
                confirm_public_form_plan_sha256=arguments.remote_form_public_plan_sha256,
                confirm_public_state_plan_sha256=arguments.remote_form_public_state_plan_sha256)
        except (OSError, ValueError, TypeError):
            parser.error('HTTPS form state plan is unavailable or invalid')
    if synthetic_skill_invocation:
        try:
            from aos.site_knowledge import SiteKnowledgeStore
            from aos.site_skill import SiteSkillStore
            from aos.site_skill_case_binding import (SiteSkillCaseInputs,
                SiteSkillFormFieldBinding)
            from aos.site_skill_form_invocation import (
                compile_site_skill_form_invocation,
                revalidate_site_skill_form_invocation)
            from aos.site_skill_validation import SiteSkillValidationPlan

            if (remote_form_plan is None or remote_form_state_plan is None
                    or arguments.remote_form_public_plan_sha256 is not None
                    or arguments.remote_form_public_state_plan_sha256 is not None
                    or arguments.remote_form_cookie_file is not None
                    or arguments.remote_form_cookie_sha256 is not None
                    or not (urlsplit(remote_form_plan.entry_url).hostname or '').endswith('.invalid')):
                raise ValueError('Synthetic skill invocation requires a cookie-free .invalid stateful form')
            sources = (arguments.remote_form_skill_plan_file,
                       arguments.remote_form_skill_case_inputs_file,
                       arguments.remote_form_skill_field_bindings_file)
            hashes = (arguments.remote_form_skill_plan_sha256,
                      arguments.remote_form_skill_case_inputs_sha256,
                      arguments.remote_form_skill_field_bindings_sha256)
            if any(not path.absolute().is_relative_to(REPO_ROOT / 'data') for path in sources):
                raise ValueError('Synthetic skill sources must remain under private data')
            profiles = WebApplicationProfiles(arguments.web_profiles_root)
            skill_store = SiteSkillStore(
                (owned_form_directory / 'site-skills' if owned_form_directory is not None
                 else REPO_ROOT / 'data/site-skills'), profiles,
                SiteKnowledgeStore(
                    (owned_form_directory / 'site-knowledge' if owned_form_directory is not None
                     else REPO_ROOT / 'data/site-knowledge'), profiles))

            def load_skill_sources():
                from aos.local_app import private_read

                plan_content = private_read(sources[0].absolute(), 20000)
                inputs_content = private_read(sources[1].absolute(), 16384)
                fields_content = private_read(sources[2].absolute(), 8192)
                if tuple(hashlib.sha256(content).hexdigest() for content in
                         (plan_content, inputs_content, fields_content)) != hashes:
                    raise ValueError('Synthetic skill source pin changed')
                plan_value = SiteSkillValidationPlan.model_validate_json(plan_content)
                inputs_value = SiteSkillCaseInputs.model_validate_json(inputs_content)
                fields_value = json.loads(fields_content)
                bindings_value = [SiteSkillFormFieldBinding.model_validate(value)
                                  for value in fields_value]
                if (canonical(plan_value.model_dump()).encode() != plan_content
                        or canonical(inputs_value.model_dump()).encode() != inputs_content
                        or canonical([item.model_dump(mode='json') for item in bindings_value]).encode()
                        != fields_content):
                    raise ValueError('Synthetic skill sources are not canonical')
                return plan_value, inputs_value, bindings_value

            def load_skill_recipe():
                if arguments.remote_form_skill_recipe_file is None:
                    return None
                from aos.local_app import private_read
                from aos.site_skill_form_recipe import SiteSkillFormRecipe

                recipe_path = arguments.remote_form_skill_recipe_file.absolute()
                if not recipe_path.is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('Synthetic recipe must remain under private data')
                recipe_content = private_read(recipe_path, 16384)
                if (hashlib.sha256(recipe_content).hexdigest()
                        != arguments.remote_form_skill_recipe_file_sha256):
                    raise ValueError('Synthetic recipe source pin changed')
                recipe = SiteSkillFormRecipe.model_validate_json(recipe_content)
                if canonical(recipe.model_dump(mode='json')).encode() != recipe_content:
                    raise ValueError('Synthetic recipe source is not canonical')
                return recipe

            selected_plan, selected_inputs, selected_bindings = load_skill_sources()
            selected_recipe = load_skill_recipe()
            compile_invocation = compile_site_skill_form_invocation
            if selected_recipe is not None:
                from aos.site_skill_form_recipe import compile_site_skill_form_recipe_invocation

                compile_invocation = compile_site_skill_form_recipe_invocation
            remote_form_skill_invocation = compile_invocation(
                skill_store, selected_plan, selected_inputs,
                arguments.remote_form_skill_case_key, profiles, remote_task,
                remote_form_plan, remote_form_state_plan, selected_bindings,
                *(() if selected_recipe is None else (selected_recipe,)))
            remote_form_skill_invocation_sha256 = hashlib.sha256(
                canonical(remote_form_skill_invocation).encode()).hexdigest()
            if remote_form_skill_invocation_sha256 != arguments.remote_form_skill_invocation_sha256:
                raise ValueError('Synthetic skill invocation confirmation differs')

            def revalidate_skill_invocation():
                current_plan, current_inputs, current_bindings = load_skill_sources()
                current_recipe = load_skill_recipe()
                revalidate_invocation = revalidate_site_skill_form_invocation
                if current_recipe is not None:
                    from aos.site_skill_form_recipe import revalidate_site_skill_form_recipe_invocation

                    revalidate_invocation = revalidate_site_skill_form_recipe_invocation
                return revalidate_invocation(
                    remote_form_skill_invocation, skill_store, current_plan, current_inputs,
                    arguments.remote_form_skill_case_key, profiles, remote_task,
                    remote_form_plan, remote_form_state_plan, current_bindings,
                    *(() if current_recipe is None else (current_recipe,)))
        except (OSError, ValueError, TypeError, KeyError):
            parser.error('Synthetic skill invocation sources are unavailable or invalid')
        remote_form_skill_revalidator = revalidate_skill_invocation
    if owned_parameter_startup is not None:
        project = owned_parameter_startup.source()
        if remote_task != project['task']:
            parser.error('Owned parameter project task changed during startup')
        remote_form_plan = project['form_plan']
        remote_form_state_plan = project['state_plan']
        remote_form_fields = [{'name': name, 'value': value} for name, value in project['fields']]
        remote_form_skill_invocation = project['invocation']
        remote_form_skill_invocation_sha256 = project['invocation_sha256']
        remote_form_skill_revalidator = owned_parameter_startup.revalidate
    owned_form_fixture = None
    def create_owned_form_fixture():
        if owned_form_directory is None:
            return None
        fixture = None
        try:
            from aos.owned_form_fixture import OwnedFormFixture

            if owned_parameter_startup is not None:
                fixture = owned_parameter_startup.create_fixture(arguments.owned_form_listener_fd)
                os.close(arguments.owned_form_listener_fd)
                arguments.owned_form_listener_fd = None
                return fixture
            if (remote_form_plan is None or remote_form_state_plan is None
                    or remote_form_skill_invocation is None
                    or arguments.remote_form_public_plan_sha256 is not None
                    or arguments.remote_form_public_state_plan_sha256 is not None
                    or remote_form_cookie is not None
                    or arguments.remote_form_cookie_file is not None
                    or arguments.remote_form_cookie_sha256 is not None
                    or remote_form_skill_invocation_sha256
                    != owned_form_manifest['invocation_sha256']
                    or hashlib.sha256(canonical(remote_form_skill_invocation).encode()).hexdigest()
                    != owned_form_manifest['invocation_sha256']):
                raise ValueError('owned_form_invocation_sources_changed')
            body = form_body(exact_form_fields(arguments.remote_form_field_name,
                                                remote_form_value))
            fixture = OwnedFormFixture(
                arguments.owned_form_listener_fd,
                origin=owned_form_manifest['origin'],
                profile_sha256=owned_form_manifest['profile_sha256'],
                form_plan_sha256=owned_form_manifest['form_plan_sha256'],
                state_plan_sha256=owned_form_manifest['state_plan_sha256'],
                entry_url=remote_form_plan.entry_url,
                submit_url=remote_form_plan.submit_url,
                receipt_url=remote_form_plan.receipt_url,
                state_url=remote_form_state_plan.state_url,
                expected_body=body,
                certificate_file=owned_form_directory / 'owned-form-certificate.pem',
                key_file=owned_form_directory / 'owned-form-key.pem',
                certificate_sha256=owned_form_manifest['certificate_sha256'])
            fixture.target.assert_plan(
                remote_form_plan.profile_sha256, digest(remote_form_plan.model_dump()))
            fixture.target.assert_state_plan(digest(remote_form_state_plan.model_dump()))
            os.close(arguments.owned_form_listener_fd)
            arguments.owned_form_listener_fd = None
            return fixture
        except (OSError, ValueError, TypeError, KeyError):
            if fixture is not None:
                fixture.close()
            try:
                os.close(arguments.owned_form_listener_fd)
            except (OSError, TypeError):
                pass
            raise ValueError('Owned synthetic HTTPS fixture is unavailable or invalid') from None
    cookie_options = (arguments.remote_form_cookie_file,
                      arguments.remote_form_cookie_sha256)
    if any(option is not None for option in cookie_options):
        if (not all(option is not None for option in cookie_options)
                or remote_form_plan is None
                or arguments.remote_form_public_plan_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', arguments.remote_form_cookie_sha256) is None):
            parser.error('HTTPS form cookie requires exact public form and private hash')
        try:
            cookie_file = arguments.remote_form_cookie_file.absolute()
            if not cookie_file.is_relative_to(REPO_ROOT / 'data'):
                raise ValueError('HTTPS form cookie must remain under ignored data')
            content = private_read(cookie_file, 2048)
            remote_form_cookie = validate_https_cookie_header(content.decode('ascii'))
            if hashlib.sha256(content).hexdigest() != arguments.remote_form_cookie_sha256:
                raise ValueError('HTTPS form cookie confirmation differs')
        except (OSError, ValueError, TypeError, UnicodeError):
            parser.error('HTTPS form cookie is unavailable or invalid')
    if arguments.desktop_vision and (not arguments.desktop_browser or arguments.vision_engine == 'disabled'):
        parser.error('Visible desktop vision requires --desktop-browser and an explicit --vision-engine')
    if arguments.vision_engine != 'disabled' and (not arguments.browser_tasks or (
            arguments.vision_engine == 'fixture') != (arguments.engine == 'fixture')):
        parser.error('Vision requires --browser-tasks and matching real or fixture model engines')
    token = secrets.token_urlsafe(32)
    key = REPO_ROOT / 'runs' / ('desktop-console-' + secrets.token_hex(8) + '.token')
    key.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(token)
    runtime = DesktopRuntime(arguments.workspace, REPO_ROOT / 'models/desktop-manifest.json')
    store = None
    controller = None
    scheduler = None
    owned_workspace_lock = None
    retention_telemetry = RetentionTelemetry(enabled=arguments.managed_retention)

    def cleanup():
        nonlocal store, controller, owned_workspace_lock
        try:
            if controller and controller.state()['status'] != 'stopped':
                controller.control('stop')
        finally:
            controller = None
            try:
                runtime.stop()
            finally:
                if store:
                    store.close()
                    store = None
                key.unlink(missing_ok=True)
                if owned_form_fixture is not None and owned_parameter_startup is None:
                    owned_form_fixture.close()
                if owned_parameter_startup is not None:
                    owned_parameter_startup.close()
                if owned_workspace_lock is not None:
                    owned_workspace_lock.close()
                    owned_workspace_lock = None
                if arguments.owned_learning_lock_fd is not None:
                    os.close(arguments.owned_learning_lock_fd)
                    arguments.owned_learning_lock_fd = None

    def retention_notice(report, failure):
        retention_telemetry.record(report, failure)
        if failure is not None:
            print('Remote learning retention sweep unavailable; inspect private stores.', flush=True)
        elif report['failed_sessions'] or report['failed_consent_sha256']:
            print('Remote learning retention sweep incomplete; inspect private stores.', flush=True)

    @asynccontextmanager
    async def lifespan(app):
        retention_task = None
        try:
            if scheduler and isinstance(scheduler.engine, ReusableDeciderEngine):
                scheduler.engine.prewarm_idle()
            if scheduler and isinstance(scheduler.vision_supervisor, BonsaiVisionSupervisor):
                scheduler.vision_supervisor.prewarm_pins_idle()
            if arguments.managed_retention:
                retention_task = asyncio.create_task(run_managed_retention_loop(
                    consents=REPO_ROOT / 'data/remote-learning-consents',
                    sessions_root=REPO_ROOT / 'data/local-app-v1',
                    on_result=retention_notice))
            yield
        finally:
            if retention_task is not None:
                retention_task.cancel()
                await asyncio.gather(retention_task, return_exceptions=True)
            if scheduler:
                await scheduler.close()
                if isinstance(scheduler.vision_supervisor, BonsaiVisionSupervisor):
                    await scheduler.vision_supervisor.close_pin_prewarm()
            if scientist_lab is not None:
                await scientist_lab.close_async(timeout_seconds=scientist_lab.client.timeout_seconds)
            cleanup()

    try:
        if owned_form_directory is not None:
            from aos.owned_learning_workspace import OwnedLearningWorkspace

            owned_workspace_lock = (
                OwnedLearningWorkspace.adopt(owned_form_directory, arguments.owned_learning_lock_fd)
                if arguments.owned_learning_lock_fd is not None else
                OwnedLearningWorkspace.acquire(owned_form_directory, create=True))
            if arguments.owned_learning_lock_fd is not None:
                os.close(arguments.owned_learning_lock_fd)
                arguments.owned_learning_lock_fd = None
            if owned_parameter_startup is not None:
                owned_parameter_startup.retain_workspace(owned_workspace_lock)
            if owned_skill_reuse_material is not None:
                with scoped_manager():
                    owned_skill_reuse_material, _previous, _retained = load_owned_skill_reuse_startup(
                        arguments.workspace.absolute().parent, arguments.owned_skill_reuse_sha256)
        owned_form_fixture = create_owned_form_fixture()
        store = TrajectoryStore(arguments.database)
        runtime.start()
        controller = DesktopController(store, runtime)
        scientist_admission_history = None
        scientist_bootstrap_capture = None
        if scientist_admission_factory is not None:
            try:
                scientist_admission_history = scientist_admission_factory(controller)
                if (not isinstance(scientist_admission_history, ScientistAdmissionHistory)
                        or scientist_admission_history.record_version != '2.0'
                        or scientist_admission_history.store is not controller.store):
                    raise ScientistAdmissionError('Scientist admission history must use this controller store and version2.0')
                if scientist_bootstrap_expected_peer is not None:
                    scientist_bootstrap_capture = scientist_admission_history.capture
                    if (not isinstance(scientist_bootstrap_capture, ScientistBootstrapCapture)
                            or scientist_bootstrap_capture.store is not controller.store):
                        raise ScientistAdmissionError('Scientist bootstrap requires the exact original typed capture')
            except (OSError, ValueError, TypeError, ScientistAdmissionError):
                parser.error('Scientist trusted original admission factory is unavailable or returned an incompatible history')
        remote_form_owned_candidate_session = None
        if arguments.engine != 'disabled':
            remote_learning_enabled = (remote_routes_plan is not None
                                       or remote_static_assets_plan is not None
                                       or (remote_form_plan is not None
                                           and remote_form_cookie is None))
            engine_class = ReusableDeciderEngine if arguments.reuse_decider else DeciderEngine
            engine = None if arguments.engine == 'scientist' else FixtureDecisionEngine() if arguments.engine == 'fixture' else engine_class(
                arguments.decider_manifest, arguments.model_python,
                **({'cpu_prewarm': arguments.prewarm_decider,
                    'idle_seconds': arguments.prewarm_idle_seconds or 60,
                    'gpu_idle_seconds': arguments.gpu_idle_seconds or 0}
                   if arguments.reuse_decider else {}))
            supervisor = None if arguments.vision_engine == 'disabled' or arguments.engine == 'scientist' else (
                FixtureVisionSupervisor() if arguments.vision_engine == 'fixture' else BonsaiVisionSupervisor(arguments.bonsai_manifest))
            remote_form_owned_auditor = None
            if owned_parameter_startup is not None:
                def audit_owned_parameter_run(run_id):
                    return owned_parameter_startup.audit(arguments.database, run_id)

                remote_form_owned_auditor = audit_owned_parameter_run
            elif owned_form_fixture is not None:
                from aos.owned_form_invocation_session import (
                    verify_owned_form_invocation_manifest)

                def audit_owned_form_run(run_id):
                    current_manifest = verify_owned_form_invocation_manifest(
                        owned_form_directory, arguments.owned_form_manifest_sha256)
                    if current_manifest != owned_form_manifest:
                        raise ValueError('owned_form_invocation_manifest_changed')
                    if current_manifest['mode'] == 'owned_synthetic_form_recipe':
                        from aos.site_skill_form_recipe_audit import (
                            audit_site_skill_form_recipe_execution)

                        current_invocation = remote_form_skill_revalidator()
                        return audit_site_skill_form_recipe_execution(
                            skill_store, selected_plan, selected_inputs,
                            arguments.remote_form_skill_case_key, profiles, remote_task,
                            remote_form_plan, remote_form_state_plan, selected_bindings,
                            selected_recipe, current_invocation, arguments.database, run_id)
                    from aos.site_skill_form_invocation import SiteSkillFormInvocation
                    from aos.site_skill_form_invocation_audit import (
                        audit_site_skill_form_invocation_execution)

                    current_invocation = SiteSkillFormInvocation.model_validate_json(
                        canonical(remote_form_skill_invocation))
                    return audit_site_skill_form_invocation_execution(
                        skill_store, selected_plan, selected_inputs,
                        arguments.remote_form_skill_case_key, profiles, remote_task,
                        remote_form_plan, remote_form_state_plan, selected_bindings,
                        current_invocation, arguments.database, run_id)

                remote_form_owned_auditor = audit_owned_form_run
                if owned_form_manifest['mode'] == 'owned_synthetic_form_invocation':
                    from aos.site_skill_form_recipe_candidate import (
                        OwnedSiteSkillFormRecipeCandidateSession)

                    candidate_pages = SiteKnowledgeStore(
                        owned_form_directory / 'site-knowledge', profiles)
                    remote_form_owned_candidate_session = (
                        owned_skill_reuse_material['context']['candidate_session']
                        if owned_skill_reuse_material is not None else
                        OwnedSiteSkillFormRecipeCandidateSession(
                            directory=owned_form_directory,
                            manifest_sha256=arguments.owned_form_manifest_sha256,
                            candidate_directory=(owned_form_directory
                                / 'site-skill-recipe-candidates'),
                            database=arguments.database, profiles=profiles,
                            pages=candidate_pages))
            scheduler_factory = DesktopScheduler
            if arguments.engine == 'scientist':
                def scheduler_factory(controller, settings, engine, *, vision_supervisor, **options):
                    return create_scientist_desktop_scheduler(controller, settings, scientist_pins,
                        arguments.scientist_broker_socket, confirm_runtime=scientist_confirm_runtime,
                        admission_history=scientist_admission_history, output_contract=scientist_output_contract,
                        output_context_tokens=scientist_output_context_tokens,
                        bootstrap_capture=scientist_bootstrap_capture,
                        expected_bootstrap_peer=scientist_bootstrap_expected_peer,
                        resolver_factory=scientist_retained_resolver_factory,
                        bonsai_manifest=arguments.bonsai_manifest if arguments.vision_engine == 'bonsai' else None,
                        **options)

            scheduler = scheduler_factory(controller, Settings(workspace=arguments.workspace, database=arguments.database), engine,
                                         browser_manifest=arguments.browser_manifest if arguments.browser_tasks else None,
                                         vision_supervisor=supervisor, desktop_browser=arguments.desktop_browser,
                                         desktop_vision=arguments.desktop_vision,
                                         desktop_mcp_manifest=arguments.desktop_mcp_manifest,
                                         desktop_navigation_mcp_manifest=arguments.desktop_navigation_mcp_manifest,
                                         desktop_staging_mcp_manifest=arguments.desktop_staging_mcp_manifest,
                                         staging_fixture_port=arguments.staging_fixture_port,
                                         remote_entry_mcp_manifest=arguments.remote_entry_mcp_manifest,
                                         remote_entry_profiles=WebApplicationProfiles(arguments.web_profiles_root) if remote_task else None,
                                         remote_entry_profile_sha256=arguments.remote_entry_profile_sha256,
                                         remote_entry_task=remote_task,
                                         remote_routes_plan=remote_routes_plan,
                                         remote_static_assets_plan=remote_static_assets_plan,
                                         remote_route_review_source_database=(
                                             arguments.remote_route_review_source_database),
                                         remote_route_review_site_store=(
                                             arguments.remote_route_review_site_store),
                                         remote_route_review_store=(
                                             arguments.remote_route_review_store),
                                         remote_route_review_sha256=arguments.remote_route_review_sha256,
                                         remote_json_review_source_database=(
                                             arguments.remote_json_review_source_database),
                                         remote_json_review_site_store=(
                                             arguments.remote_json_review_site_store),
                                         remote_json_review_store=(
                                             arguments.remote_json_review_store),
                                         remote_json_review_sha256=arguments.remote_json_review_sha256,
                                         remote_form_plan=remote_form_plan,
                                         remote_form_field_name=arguments.remote_form_field_name,
                                         remote_form_value=remote_form_value,
                                         remote_form_fields=remote_form_fields,
                                         remote_form_tls_context=(owned_form_fixture.target.tls_context
                                                                  if owned_form_fixture is not None else
                                                                  ssl.create_default_context()
                                                                  if remote_form_plan is not None else None),
                                         remote_form_public_plan_sha256=(
                                             arguments.remote_form_public_plan_sha256
                                             if remote_form_plan is not None else None),
                                         remote_form_state_plan=remote_form_state_plan,
                                         remote_form_public_state_plan_sha256=(
                                             arguments.remote_form_public_state_plan_sha256
                                             if remote_form_state_plan is not None else None),
                                         remote_form_cookie=remote_form_cookie,
                                         remote_form_cookie_sha256=(
                                             arguments.remote_form_cookie_sha256
                                             if remote_form_cookie is not None else None),
                                         remote_form_skill_invocation=remote_form_skill_invocation,
                                         remote_form_skill_invocation_sha256=(
                                             remote_form_skill_invocation_sha256
                                             if remote_form_skill_invocation is not None else None),
                                         remote_form_skill_revalidator=remote_form_skill_revalidator,
                                         remote_form_owned_target=(owned_form_fixture.target
                                                                   if owned_form_fixture is not None else None),
                                         remote_form_owned_fixture=owned_form_fixture,
                                         remote_form_owned_manifest=owned_form_manifest,
                                         remote_form_owned_auditor=remote_form_owned_auditor,
                                         remote_form_owned_candidate_session=remote_form_owned_candidate_session,
                                         synthetic_learning_stream_dir=arguments.synthetic_learning_stream_dir,
                                         remote_learning_consents_dir=(REPO_ROOT / 'data/remote-learning-consents'
                                                                       if remote_learning_enabled else None),
                                         remote_learning_stream_dir=(arguments.database.parent / 'remote-learning-outbox'
                                                                     if remote_learning_enabled else None))
            if owned_parameter_startup is not None:
                scheduler.configure_owned_parameter_project_execution(
                    owned_parameter_startup.bundle,
                    arguments.database.absolute().parent / 'owned-parameter-project-execution',
                    current_source=owned_parameter_startup.source,
                    source_auditor=remote_form_owned_auditor)
            if owned_skill_reuse_material is not None:
                from aos.local_app import write_new_private_plan
                from aos.owned_skill_reuse_admission import make_owned_skill_reuse_admission

                admission = make_owned_skill_reuse_admission(
                    owned_skill_reuse_material['preview'],
                    manager_session=arguments.workspace.absolute().parent.name,
                    controller_state=controller.state())
                admission_file = arguments.workspace.absolute().parent / 'owned-skill-reuse-admission.json'
                write_new_private_plan(admission_file, canonical(admission).encode())
                scheduler.restore_owned_skill_reuse(
                    owned_skill_reuse_material['context'], admission,
                    owned_workspace_lock, admission_file=admission_file)
                from aos.owned_skill_planner import BonsaiOwnedSkillPlanner

                scheduler.configure_owned_skill_planning(
                    BonsaiOwnedSkillPlanner(arguments.bonsai_manifest, knowledge_context=True))
                from aos.web_goal_planner import BonsaiWebGoalPlanner

                scheduler.configure_web_goal_planning(BonsaiWebGoalPlanner(
                    arguments.bonsai_manifest, knowledge_context=arguments.knowledge_root is not None))
        assets = prepare_console_assets(runtime, arguments.console_assets_root)
        origin = f'http://127.0.0.1:{arguments.port}'
        print(f'Console: {origin}\nLocal token file (0600): {key}', flush=True)
        knowledge_answerer = None
        if scheduler is not None and arguments.engine == 'decider' and arguments.knowledge_root is not None:
            from aos.knowledge_answer_model import BonsaiKnowledgeAnswerer

            knowledge_answerer = BonsaiKnowledgeAnswerer(arguments.bonsai_manifest)
        scientist_lab = None
        if lab_startup is not None:
            lab_config, lab_client = lab_startup
            scientist_lab = ScientistLabService(controller, lab_client,
                authorization_context_sha256=lab_config.authorization_context_sha256,
                program_version=lab_config.program_version, verify_capability=scientist_verify_lab_capability)
        app = create_console(controller, token, origin, assets,
                             arguments.database if scheduler else arguments.trajectory_database, scheduler,
                             local_ui_auto_login=arguments.local_ui_auto_login,
                             manager_scope=manager_scope,
                             manager_session=arguments.workspace.absolute().parent.name if manager_scope is not None else None,
                             ui_root=manager_base / 'ui' if manager_scope is not None else None,
                             retention_status=retention_telemetry.snapshot,
                             recovery_database=arguments.database,
                             web_profiles_root=arguments.web_profiles_root,
                             knowledge_root=arguments.knowledge_root,
                             site_knowledge_root=arguments.site_knowledge_root,
                             site_skills_root=arguments.site_skills_root,
                             page_seed_root=arguments.page_seed_root,
                             route_review_root=arguments.route_review_root,
                             json_review_root=arguments.json_review_root,
                             json_page_seed_root=arguments.json_page_seed_root,
                             knowledge_answerer=knowledge_answerer,
                             scientist_lab=scientist_lab,
                             web_task_root=arguments.web_task_root,
                             web_form_plan_root=arguments.web_form_plan_root,
                             web_form_value_root=arguments.web_form_value_root,
                             web_form_state_root=arguments.web_form_state_root,
                             web_route_root=arguments.web_route_root,
                             web_static_root=arguments.web_static_root,
                             web_readonly_data_root=arguments.web_readonly_data_root,
                             owned_form_recipe_mode=(owned_form_manifest is not None
                                and owned_form_manifest.get('mode')
                                in {'owned_synthetic_form_recipe',
                                    'owned_synthetic_parameter_project'}))
        app.router.lifespan_context = lifespan
        uvicorn.run(app, host='127.0.0.1', port=arguments.port, fd=arguments.listen_fd,
                    access_log=False, ws_max_size=65536, timeout_graceful_shutdown=5)
    finally:
        cleanup()


if __name__ == '__main__':
    main()
