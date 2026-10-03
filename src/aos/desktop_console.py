import asyncio
from contextlib import suppress
import http.client
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import subprocess
import threading
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jsonschema.exceptions import SchemaError, ValidationError

from .contracts import AOSFault, ErrorCode, REPO_ROOT, canonical, digest, now
from .capability_evidence import read_capability_evidence
from .dataset import validator
from .desktop import DOCKER, DesktopRuntime
from .goal_plan import CompoundSequenceStart, preview_goal_plan
from .inspection import TrajectoryInspector
from .task_intent import preview_goal
from .task_plan import preview_plan
from .task_sequence import SequenceStart
from .recovery import recovery_inventory
from .recovery_session import inspect_session
from .remote_navigation_graph import preview_remote_navigation_graph
from .remote_page_draft_seed import (private_seed_output, register_remote_page_draft,
                                     seed_remote_page_draft, write_private_draft)
from .remote_route_knowledge import preview_remote_route_knowledge
from .remote_route_knowledge_review import (RemoteRouteKnowledgeCandidatePreview,
                                             register_remote_route_knowledge_review)
from .remote_readonly_data_knowledge import preview_remote_readonly_data_knowledge
from .remote_readonly_data_page_draft import (
    register_remote_readonly_data_page_draft, seed_remote_readonly_data_page_draft)
from .remote_readonly_data_knowledge_review import (
    RemoteReadonlyDataKnowledgeCandidatePreview,
    register_remote_readonly_data_knowledge_review,
    recheck_remote_readonly_data_knowledge)
from .remote_form_repeat import inspect_remote_form_repeats
from .remote_site_skill_provenance import inspect_remote_site_skill_sources
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .scientist_transport import ScientistAdmissionError
from .web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from .web_form_draft import (preview_form_plan, preview_form_task,
                             preview_form_state_plan, register_form_plan,
                             register_form_state_plan, register_form_task)
from .web_readonly_data import WebReadOnlyDataBundlePlan
from .web_https_preflight import probe_https_entry
from .web_static_assets import STATIC_CONTENT_TYPES
from .web_task_draft import (preview_readonly_data, preview_routes, preview_static_assets,
                             preview_task, register_readonly_data, register_routes,
                             register_static_assets, register_task)


def create_console(controller, token: str, origin: str, assets: Path, trajectory_database: Path | None = None, scheduler=None,
                   *, recovery_database: Path | None = None, web_profiles_root: Path | None = None,
                   retention_status: Callable[[], dict] | None = None,
                   site_knowledge_root: Path | None = None,
                   site_skills_root: Path | None = None,
                   page_seed_root: Path | None = None,
                   route_review_root: Path | None = None,
                   json_review_root: Path | None = None,
                   json_page_seed_root: Path | None = None,
                   web_task_root: Path | None = None,
                   web_route_root: Path | None = None,
                   web_static_root: Path | None = None,
                   web_readonly_data_root: Path | None = None,
                   web_form_plan_root: Path | None = None,
                   web_form_value_root: Path | None = None,
                   web_form_state_root: Path | None = None,
                   knowledge_root: Path | None = None,
                   knowledge_answerer=None,
                   scientist_lab=None,
                   owned_form_recipe_mode: bool = False,
                   local_ui_auto_login: bool = False,
                   manager_scope: dict | None = None,
                   manager_session: str | None = None,
                   ui_root: Path | None = None) -> FastAPI:
    if manager_session is not None and (manager_scope is None or not isinstance(manager_session, str)
                                       or re.fullmatch(r'app-[a-f0-9]{32}', manager_session) is None):
        raise ValueError('Manager session requires its exact named project scope')
    if manager_scope is not None:
        if (not isinstance(manager_scope, dict) or set(manager_scope) != {'project', 'port'}
                or not isinstance(manager_scope['project'], str)
                or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', manager_scope['project']) is None
                or type(manager_scope['port']) is not int
                or not 1024 <= manager_scope['port'] <= 65535
                or manager_scope['port'] == 8765
                or origin != f"http://127.0.0.1:{manager_scope['port']}"):
            raise ValueError('Invalid pinned manager project scope')
        manager_scope = dict(manager_scope)
    cookie_name = ('aos_session' if manager_scope is None
                   else f"aos_project_{manager_scope['project']}_{manager_scope['port']}")
    if manager_scope is not None and ui_root is None:
        ui_root = REPO_ROOT / 'data' / ('local-app-project-' + manager_scope['project']) / 'ui'
    if ui_root is not None:
        ui_root = Path(ui_root).absolute()
        if (manager_scope is None
                or ui_root != REPO_ROOT / 'data' / ('local-app-project-' + manager_scope['project']) / 'ui'
                or any(parent.is_symlink() for parent in (ui_root, *ui_root.parents) if parent != REPO_ROOT)
                or not (ui_root / 'index.html').is_file()
                or (ui_root / 'index.html').is_symlink()):
            raise ValueError('Named project UI requires its exact prepared private root')
    else:
        ui_root = REPO_ROOT / 'ui/dist'
    if scientist_lab is not None and scientist_lab.controller is not controller:
        raise ValueError('Scientist service must use this console desktop controller')
    if type(local_ui_auto_login) is not bool:
        raise ValueError('local_ui_auto_login_requires_boolean')
    if local_ui_auto_login:
        if not re.fullmatch(r'http://127\.0\.0\.1(?::[1-9][0-9]{0,4})?', origin):
            raise ValueError('local_ui_auto_login_requires_loopback_http_origin')
        if urlsplit(origin).port is not None and urlsplit(origin).port > 65535:
            raise ValueError('local_ui_auto_login_requires_loopback_http_origin')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    session = secrets.token_urlsafe(32)
    streams = {}
    control_lock = asyncio.Lock()
    from .shared_drain import SharedAdmissionDrain, SharedDrainRequest

    shared_drain = (SharedAdmissionDrain(controller, scheduler, scientist_lab)
                    if scheduler is not None and scientist_lab is not None else None)
    recovery_lock = threading.Lock()
    navigation_graph_lock = threading.Lock()
    remote_form_repeat_lock = threading.Lock()
    session_binding_lock = threading.Lock()
    preflight_lock = threading.Lock()
    preflight_attempted = set()
    host = origin.removeprefix('http://')
    inspector = TrajectoryInspector(trajectory_database) if trajectory_database else None
    web_profiles = WebApplicationProfiles(web_profiles_root or REPO_ROOT / 'data/web-applications')
    page_knowledge = site_knowledge_root or REPO_ROOT / 'data/site-knowledge'
    site_skills = site_skills_root or REPO_ROOT / 'data/site-skills'
    private_seeds = page_seed_root or REPO_ROOT / 'data/site-page-seeds'
    route_reviews = route_review_root or REPO_ROOT / 'data/remote-route-reviews'
    json_reviews = json_review_root or REPO_ROOT / 'data/remote-json-reviews'
    json_page_seeds = json_page_seed_root or REPO_ROOT / 'data/json-page-seeds'
    web_tasks = web_task_root or REPO_ROOT / 'data/web-task-drafts'
    web_routes = web_route_root or REPO_ROOT / 'data/web-route-drafts'
    web_static_assets = web_static_root or REPO_ROOT / 'data/web-static-asset-drafts'
    web_readonly_data = web_readonly_data_root or REPO_ROOT / 'data/web-readonly-data-drafts'
    web_form_plans = web_form_plan_root or REPO_ROOT / 'data/web-form-plan-drafts'
    web_form_values = web_form_value_root or REPO_ROOT / 'data/web-form-value-drafts'
    web_form_states = web_form_state_root or REPO_ROOT / 'data/web-form-state-drafts'
    from .knowledge import KnowledgeStore

    document_knowledge = KnowledgeStore(knowledge_root) if knowledge_root is not None else None
    if document_knowledge is not None and scheduler is not None and hasattr(scheduler, 'configure_task_knowledge'):
        configured = getattr(scheduler, 'task_knowledge', None)
        if configured is None:
            database = recovery_database or scheduler.settings.database
            scheduler.configure_task_knowledge(document_knowledge, database.parent / 'task-knowledge')
        elif configured.store.root != document_knowledge.root:
            raise ValueError('task_knowledge_store_configuration_changed')
        planning = getattr(scheduler, 'owned_skill_planning', None)
        if (planning is not None and planning.knowledge is None
                and planning.planner.pins.get('owned_skill_knowledge_context_protocol')
                == 'aos-owned-skill-knowledge-v1'):
            scheduler.configure_owned_skill_knowledge(document_knowledge)
        web_planning = getattr(scheduler, 'web_goal_planning', None)
        if (web_planning is not None and getattr(web_planning, 'knowledge_service', None) is None
                and web_planning.planner.pins.get('owned_skill_knowledge_context_protocol')
                == 'aos-owned-skill-knowledge-v1'):
            scheduler.configure_web_goal_knowledge(document_knowledge)
    if knowledge_answerer is not None:
        if document_knowledge is None or scheduler is None or recovery_database is None:
            raise ValueError('knowledge_answer_requires_configured_session')
        scheduler.configure_knowledge_answer(document_knowledge, knowledge_answerer,
                                            recovery_database.parent / 'knowledge-answers')

    async def close_streams():
        for websocket in list(streams):
            with suppress(Exception):
                await websocket.close(code=1000)
        await asyncio.gather(*(finished.wait() for finished in list(streams.values())))

    def authorized(cookies):
        return secrets.compare_digest(cookies.get(cookie_name, '').encode(), session.encode())

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.headers.get('host') != host:
            return JSONResponse({'detail': 'Invalid host'}, status_code=403)
        if request.method == 'POST' and request.headers.get('origin') != origin:
            return JSONResponse({'detail': 'Invalid origin'}, status_code=403)
        if (request.url.path.startswith('/api/') and request.url.path not in {'/api/login', '/api/login/local', '/api/session'}) or request.url.path.startswith('/novnc/'):
            if not authorized(request.cookies):
                return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    async def body(request: Request, *, reject_duplicates: bool = False,
                   limit: int = 4096):
        payload = b''
        async for chunk in request.stream():
            payload += chunk
            if len(payload) > limit:
                raise HTTPException(413, 'Request too large')
        try:
            def unique_pairs(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = item
                return result

            value = json.loads(payload, object_pairs_hook=unique_pairs if reject_duplicates else None)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError:
            raise HTTPException(400, 'Invalid JSON object') from None

    @app.exception_handler(AOSFault)
    async def fault_handler(request, exception):
        return JSONResponse(exception.payload(), status_code=409)

    @app.get('/')
    async def index():
        if (ui_root / 'index.html').is_file():
            return RedirectResponse('/ui/', status_code=302)
        return FileResponse(REPO_ROOT / 'computer/console.html')

    @app.get('/legacy/')
    async def legacy_index():
        return FileResponse(REPO_ROOT / 'computer/console.html')

    @app.get('/console.js')
    async def script():
        return FileResponse(REPO_ROOT / 'computer/console.js', media_type='text/javascript')

    @app.get('/console.css')
    async def style():
        return FileResponse(REPO_ROOT / 'computer/console.css', media_type='text/css')

    def login_response():
        response = JSONResponse({'authenticated': True})
        response.set_cookie(cookie_name, session, httponly=True, samesite='strict')
        return response

    @app.post('/api/login')
    async def login(request: Request):
        value = await body(request)
        if set(value) != {'token'} or not isinstance(value['token'], str) or not secrets.compare_digest(value['token'].encode(), token.encode()):
            raise HTTPException(401, 'Invalid credentials')
        return login_response()

    @app.post('/api/login/local')
    async def local_login(request: Request):
        if not local_ui_auto_login:
            raise HTTPException(403, 'Local login is disabled')
        try:
            loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        if (not loopback or request.headers.getlist('host') != [host]
                or request.headers.getlist('origin') != [origin]):
            raise HTTPException(403, 'Local login requires the local origin and peer')
        fetch_site = request.headers.getlist('sec-fetch-site')
        if fetch_site and fetch_site not in (['same-origin'], ['none']):
            raise HTTPException(403, 'Invalid fetch site')
        if request.scope.get('query_string'):
            raise HTTPException(400, 'Local login does not accept queries')
        content_types = request.headers.getlist('content-type')
        if (len(content_types) != 1
                or content_types[0].split(';', 1)[0].strip().lower() != 'application/json'):
            raise HTTPException(415, 'JSON content type required')
        if await body(request, reject_duplicates=True, limit=128) != {}:
            raise HTTPException(400, 'Local login requires an empty JSON object')
        return login_response()

    @app.get('/api/session')
    async def authentication(request: Request):
        result = {'authenticated': authorized(request.cookies)}
        if manager_scope is not None:
            result['manager_scope'] = dict(manager_scope)
            if manager_session is not None:
                result['manager_session'] = manager_session
        if local_ui_auto_login:
            result['local_auto_login'] = True
        return result

    @app.get('/api/retention')
    async def retention(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Retention status is server configured')
        report = (retention_status() if retention_status is not None else
                  {'schema_version': '1.0', 'mode': 'managed_remote_metadata_retention_status',
                   'enabled': False, 'state': 'disabled', 'last_attempt_at': None,
                   'session_count': None, 'purged_count': None,
                   'failed_session_count': 0, 'failed_consent_count': 0,
                   'metadata_only': True, 'training_ready': False})
        validator('remote_learning_retention_status').validate(report)
        return report

    @app.get('/api/capability-checks')
    async def capability_checks(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Capability evidence source is server configured')
        try:
            return await asyncio.to_thread(read_capability_evidence)
        except (OSError, ValueError):
            raise HTTPException(409, 'Capability evidence unavailable') from None

    @app.get('/api/remote-form/repeats')
    async def remote_form_repeats(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Repeat source is server configured')
        if (scheduler is None or scheduler.remote_form_plan is None
                or scheduler.remote_entry_profiles is None
                or scheduler.remote_entry_profile_sha256 is None):
            raise HTTPException(404, 'No pinned HTTPS form repeat source')
        if (scheduler.remote_form_cookie is not None
                or scheduler.store is not controller.store
                or scheduler.remote_form_plan.profile_sha256
                != scheduler.remote_entry_profile_sha256):
            raise HTTPException(409, 'HTTPS form repeat source unavailable')
        state_plan_sha256 = None
        if scheduler.remote_form_state_plan is not None:
            try:
                state_plan_sha256 = digest(scheduler.remote_form_state_plan.model_dump())
            except (AttributeError, TypeError, ValueError):
                raise HTTPException(409, 'HTTPS form repeat source unavailable') from None
        try:
            databases = controller.store.connection.execute('PRAGMA database_list').fetchall()
        except sqlite3.Error:
            raise HTTPException(409, 'HTTPS form repeat source unavailable') from None
        database = next((row[2] for row in databases if row[1] == 'main'), None)
        if not database or control_lock.locked():
            raise HTTPException(409, 'HTTPS form repeat source unavailable')
        profile_sha256 = scheduler.remote_entry_profile_sha256
        plan_sha256 = digest(scheduler.remote_form_plan.model_dump())
        profiles_root = scheduler.remote_entry_profiles.root

        def read_repeats():
            if not remote_form_repeat_lock.acquire(blocking=False):
                raise HTTPException(429, 'HTTPS form repeat inspection already running')
            try:
                return inspect_remote_form_repeats(
                    Path(database), profiles=profiles_root,
                    selected_profile_sha256=profile_sha256,
                    selected_plan_sha256=plan_sha256,
                    selected_state_plan_sha256=state_plan_sha256)
            finally:
                remote_form_repeat_lock.release()

        try:
            return await asyncio.to_thread(read_repeats)
        except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, IndexError,
                RecursionError):
            raise HTTPException(409, 'HTTPS form repeat source unavailable') from None

    @app.post('/api/logout')
    async def logout(request: Request):
        nonlocal session
        session = secrets.token_urlsafe(32)
        async with control_lock:
            if controller.state()['status'] != 'stopped':
                controller.control('pause')
            if scheduler:
                await scheduler.cancel('pause')
            await close_streams()
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(cookie_name)
        return response

    @app.get('/api/state')
    async def state():
        runtime = controller.runtime.status()
        if isinstance(controller.runtime, DesktopRuntime):
            runtime['pointer_tracking'] = True
        return {'control': controller.state(), 'runtime': runtime}

    @app.get('/api/session/binding')
    async def session_binding(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Session binding source is server configured')
        async with control_lock:
            runtime = controller.runtime
            if not isinstance(runtime, DesktopRuntime):
                raise HTTPException(409, 'Session binding unavailable')
            captured = controller.state()
            databases = controller.store.connection.execute('PRAGMA database_list').fetchall()
            database = next((row[2] for row in databases if row[1] == 'main'), None)
            if not database or not runtime.pins.get('image_id') or not runtime.pins.get('source_sha256'):
                raise HTTPException(409, 'Session binding unavailable')
            arguments = {'database': Path(database), 'workspace': runtime.root,
                         'journal': runtime.root.parent / '.aos-lifecycle' / (runtime.runtime_id + '.jsonl'),
                         'session_id': controller.session_id, 'image_id': runtime.pins['image_id'],
                         'source_sha256': runtime.pins['source_sha256']}
            captured_runtime = runtime.runtime_id
        def read_binding():
            if not session_binding_lock.acquire(blocking=False):
                raise HTTPException(429, 'Session binding inspection already running')
            try:
                return inspect_session(**arguments).model_dump()
            finally:
                session_binding_lock.release()
        try:
            result = await asyncio.to_thread(read_binding)
        except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, subprocess.TimeoutExpired):
            raise HTTPException(409, 'Session binding unavailable') from None
        async with control_lock:
            if controller.state() != captured or controller.runtime.runtime_id != captured_runtime:
                raise HTTPException(409, 'Session changed during inspection')
        return result

    @app.get('/api/overview')
    async def overview():
        session_id = controller.session_id
        connection = controller.store.connection
        events = [dict(row) for row in connection.execute(
            'SELECT event_id,kind,created_at FROM desktop_events WHERE session_id=? ORDER BY rowid DESC LIMIT 50', (session_id,))]
        inputs = [dict(row) for row in connection.execute(
            'SELECT input_id,tool,status,generation,created_at FROM desktop_inputs WHERE session_id=? ORDER BY rowid DESC LIMIT 50', (session_id,))]
        trajectory = await asyncio.to_thread(inspector.overview) if inspector else {'available': False, 'reason': 'not_configured'}
        return {'schema_version': 1, 'sampled_at': now(), 'events': events, 'inputs': inputs, 'trajectory': trajectory}

    @app.get('/api/web-applications')
    async def web_applications(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Profile inventory is server configured')
        try:
            return await asyncio.to_thread(web_profiles.list)
        except (ValueError, OSError):
            raise HTTPException(409, 'Profile inventory unavailable') from None

    @app.get('/api/web-applications/capabilities')
    async def web_application_capabilities(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Capabilities are server configured')
        return {'schema_version': '1.1', 'mode': 'web_application_draft_capabilities',
                'static_asset_content_types': list(STATIC_CONTENT_TYPES),
                'static_asset_canonical_query': True}

    def profile_from_request(value, fields):
        if set(value) != fields or not isinstance(value.get('profile'), dict):
            raise HTTPException(400, 'Invalid profile request')
        try:
            profile = WebApplicationProfile.model_validate(value['profile'])
            if profile.model_dump() != value['profile']:
                raise ValueError
            return profile
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid profile request') from None

    @app.post('/api/web-applications/preview')
    async def web_application_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid profile request')
        profile = profile_from_request(await body(request, reject_duplicates=True), {'profile'})
        try:
            return await asyncio.to_thread(web_profiles.preview, profile)
        except (ValueError, OSError):
            raise HTTPException(409, 'Profile lineage unavailable') from None

    @app.post('/api/web-applications/register')
    async def web_application_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid profile request')
        value = await body(request, reject_duplicates=True)
        profile = profile_from_request(value, {'profile', 'confirm_sha256'})
        confirmation = value['confirm_sha256']
        if not isinstance(confirmation, str) or re.fullmatch('[a-f0-9]{64}', confirmation) is None:
            raise HTTPException(400, 'Invalid profile request')
        if confirmation != profile_report(profile).profile_sha256:
            raise HTTPException(409, 'Profile confirmation mismatch')
        try:
            return await asyncio.to_thread(web_profiles.register, profile, confirm_sha256=confirmation)
        except (ValueError, OSError):
            raise HTTPException(409, 'Profile registration unavailable') from None

    @app.post('/api/web-applications/preflight')
    async def web_application_preflight(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS preflight request')
        value = await body(request, reject_duplicates=True)
        if (set(value) != {'profile_sha256', 'confirm_sha256', 'entry_url', 'authorized_get'}
                or any(not isinstance(value[key], str) or re.fullmatch('[a-f0-9]{64}', value[key]) is None
                       for key in ('profile_sha256', 'confirm_sha256'))
                or value['profile_sha256'] != value['confirm_sha256']
                or not isinstance(value['entry_url'], str) or len(value['entry_url']) > 2048
                or value['authorized_get'] is not True):
            raise HTTPException(400, 'Invalid HTTPS preflight request')

        def probe_once():
            try:
                profile = web_profiles.get(value['profile_sha256'])
            except (OSError, ValueError, TypeError, KeyError):
                raise HTTPException(422, 'HTTPS preflight source unavailable; no request made') from None
            if (profile.environment not in {'staging', 'production'}
                    or profile.entry_url != value['entry_url']):
                raise HTTPException(422, 'HTTPS preflight scope mismatch; no request made')
            if not preflight_lock.acquire(blocking=False):
                raise HTTPException(429, 'HTTPS preflight already running')
            try:
                if value['profile_sha256'] in preflight_attempted:
                    raise HTTPException(409, 'HTTPS preflight already attempted')
                preflight_attempted.add(value['profile_sha256'])
                return probe_https_entry(web_profiles, value['profile_sha256'],
                                         value['confirm_sha256']).model_dump()
            finally:
                preflight_lock.release()

        try:
            return await asyncio.to_thread(probe_once)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, TimeoutError,
                ssl.SSLError, http.client.HTTPException):
            raise HTTPException(409, 'HTTPS preflight unavailable') from None

    def task_request(value, *, register=False):
        fields = {'profile_sha256', 'task_key', 'verification_ref'}
        if register:
            fields.add('confirm_sha256')
        if (set(value) not in (fields, fields | {'route_count'})
                or not isinstance(value.get('profile_sha256'), str)
                or re.fullmatch('[a-f0-9]{64}', value['profile_sha256']) is None
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-z][a-z0-9_-]{0,63}', value[field]) is None
                       for field in ('task_key', 'verification_ref'))
                or register and (not isinstance(value.get('confirm_sha256'), str)
                                 or re.fullmatch('[a-f0-9]{64}', value['confirm_sha256']) is None)
                or type(value.get('route_count', 1)) is not int
                or not 1 <= value.get('route_count', 1) <= 8):
            raise HTTPException(400, 'Invalid remote task draft request')
        return value

    @app.post('/api/web-applications/task-preview')
    async def web_task_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote task draft request')
        value = task_request(await body(request, reject_duplicates=True))
        try:
            task, checksum = await asyncio.to_thread(preview_task, web_profiles,
                value['profile_sha256'], value['task_key'], value['verification_ref'],
                value.get('route_count', 1))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Remote task profile or scope unavailable') from None
        return {'task_sha256': checksum, 'profile_sha256': task.profile_sha256,
                'task_key': task.task_key, 'route_count': task.max_pages,
                'status': 'unregistered_draft',
                'execution_authorized': False, 'collection_authorized': False}

    @app.post('/api/web-applications/task-register')
    async def web_task_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote task draft request')
        value = task_request(await body(request, reject_duplicates=True), register=True)
        try:
            checksum, path = await asyncio.to_thread(register_task, web_profiles, web_tasks,
                value['profile_sha256'], value['task_key'], value['verification_ref'],
                value['confirm_sha256'], value.get('route_count', 1))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'Remote task draft registration unavailable') from None
        return {'task_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_key': value['task_key'], 'route_count': value.get('route_count', 1),
                'status': 'private_unactivated_draft',
                'task_file': str(path.relative_to(REPO_ROOT)),
                'execution_authorized': False, 'collection_authorized': False}

    def form_task_request(value, *, register=False):
        fields = {'profile_sha256', 'task_key', 'verification_ref'}
        if register:
            fields.add('confirm_sha256')
        if (not isinstance(value, dict) or set(value) not in (fields, fields | {'state_readback'})
                or not isinstance(value.get('profile_sha256'), str)
                or re.fullmatch('[a-f0-9]{64}', value['profile_sha256']) is None
                or type(value.get('state_readback', False)) is not bool
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-z][a-z0-9_-]{0,63}', value[field]) is None
                       for field in ('task_key', 'verification_ref'))
                or register and (not isinstance(value.get('confirm_sha256'), str)
                                 or re.fullmatch('[a-f0-9]{64}', value['confirm_sha256']) is None)):
            raise HTTPException(400, 'Invalid HTTPS form task draft request')
        return value

    @app.post('/api/web-applications/form-task-preview')
    async def web_form_task_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form task draft request')
        value = form_task_request(await body(request, reject_duplicates=True))
        try:
            task, checksum = await asyncio.to_thread(preview_form_task, web_profiles,
                value['profile_sha256'], value['task_key'], value['verification_ref'],
                value.get('state_readback', False))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'HTTPS form task profile or scope unavailable') from None
        return {'task_sha256': checksum, 'profile_sha256': task.profile_sha256,
                'task_key': task.task_key, 'state_readback': task.max_actions == 6,
                'status': 'unregistered_draft',
                'execution_authorized': False, 'collection_authorized': False}

    @app.post('/api/web-applications/form-task-register')
    async def web_form_task_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form task draft request')
        value = form_task_request(await body(request, reject_duplicates=True), register=True)
        try:
            checksum, path = await asyncio.to_thread(register_form_task, web_profiles,
                web_tasks, value['profile_sha256'], value['task_key'],
                value['verification_ref'], value['confirm_sha256'],
                value.get('state_readback', False))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'HTTPS form task registration unavailable') from None
        return {'task_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_key': value['task_key'],
                'state_readback': value.get('state_readback', False),
                'task_file': str(path.relative_to(REPO_ROOT)),
                'status': 'private_unactivated_draft',
                'execution_authorized': False, 'collection_authorized': False}

    def form_plan_request(value, *, register=False):
        common = {'profile_sha256', 'task_sha256', 'submit_url', 'receipt_url',
                  'attest_non_secret'}
        single = common | {'field_name', 'value'}
        multiple = common | {'fields'}
        if register:
            single.add('confirm_sha256')
            multiple.add('confirm_sha256')
        if (not isinstance(value, dict) or set(value) not in (single, multiple)
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in ('profile_sha256', 'task_sha256'))
                or any(not isinstance(value.get(field), str)
                       or not 1 <= len(value[field]) <= 2048
                       for field in ('submit_url', 'receipt_url'))
                or value.get('attest_non_secret') is not True
                or register and (not isinstance(value.get('confirm_sha256'), str)
                                 or re.fullmatch('[a-f0-9]{64}', value['confirm_sha256']) is None)):
            raise HTTPException(400, 'Invalid HTTPS form plan draft request')
        if set(value) == single:
            if (not isinstance(value.get('value'), str)
                    or not 1 <= len(value['value']) <= 2048
                    or not value['value'].isprintable()
                    or not isinstance(value.get('field_name'), str)
                    or re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', value['field_name']) is None):
                raise HTTPException(400, 'Invalid HTTPS form plan draft request')
        elif (not isinstance(value.get('fields'), list)
              or not 2 <= len(value['fields']) <= 8
              or any(not isinstance(field, dict) or set(field) != {'name', 'value'}
                     or not isinstance(field['name'], str)
                     or re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', field['name']) is None
                     or not isinstance(field['value'], str)
                     or not 1 <= len(field['value']) <= 2048
                     or not field['value'].isprintable()
                     for field in value['fields'])):
            raise HTTPException(400, 'Invalid HTTPS form plan draft request')
        return value

    @app.post('/api/web-applications/form-plan-preview')
    async def web_form_plan_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form plan draft request')
        value = form_plan_request(await body(request, reject_duplicates=True, limit=10000))
        try:
            plan, checksum = await asyncio.to_thread(preview_form_plan, web_profiles,
                web_tasks, value['profile_sha256'], value['task_sha256'],
                value['submit_url'], value['receipt_url'], value.get('field_name'),
                value.get('value'), fields=value.get('fields'))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'HTTPS form task or scope unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'field_name': value.get('field_name'),
                'field_names': ([value['field_name']] if 'field_name' in value else
                                [field['name'] for field in value['fields']]),
                'body_sha256': plan.body_sha256, 'body_bytes': plan.body_bytes,
                'public_grant_required': not (urlsplit(plan.entry_url).hostname or '').endswith('.invalid'),
                'status': 'unregistered_draft', 'execution_authorized': False,
                'collection_authorized': False}

    @app.post('/api/web-applications/form-plan-register')
    async def web_form_plan_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form plan draft request')
        value = form_plan_request(await body(request, reject_duplicates=True, limit=10000),
                                  register=True)
        try:
            checksum, plan_path, value_path, plan = await asyncio.to_thread(
                register_form_plan, web_profiles, web_tasks, web_form_plans,
                web_form_values, value['profile_sha256'], value['task_sha256'],
                value['submit_url'], value['receipt_url'], value.get('field_name'),
                value.get('value'), value['confirm_sha256'], fields=value.get('fields'))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'HTTPS form plan registration unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_sha256': value['task_sha256'], 'field_name': value.get('field_name'),
                'field_names': ([value['field_name']] if 'field_name' in value else
                                [field['name'] for field in value['fields']]),
                'body_sha256': plan.body_sha256, 'body_bytes': plan.body_bytes,
                'public_grant_required': not (urlsplit(plan.entry_url).hostname or '').endswith('.invalid'),
                'plan_file': str(plan_path.relative_to(REPO_ROOT)),
                ('value_file' if 'field_name' in value else 'fields_file'):
                    str(value_path.relative_to(REPO_ROOT)),
                'status': 'private_unactivated_draft',
                'execution_authorized': False, 'collection_authorized': False}

    def form_state_request(value, *, register=False):
        fields = {'profile_sha256', 'task_sha256', 'form_plan_sha256',
                  'state_url', 'before_sha256', 'after_sha256'}
        marker = {'marker_id', 'before_marker_sha256', 'after_marker_sha256'}
        if register:
            fields.add('confirm_sha256')
        submitted = {'submitted_field_name'}
        if (not isinstance(value, dict) or set(value) not in (
                fields, fields | marker, fields | marker | submitted)
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in ('profile_sha256', 'task_sha256', 'form_plan_sha256',
                                     'before_sha256', 'after_sha256'))
                or not isinstance(value.get('state_url'), str)
                or not 1 <= len(value['state_url']) <= 2048
                or register and (not isinstance(value.get('confirm_sha256'), str)
                                 or re.fullmatch('[a-f0-9]{64}', value['confirm_sha256']) is None)):
            raise HTTPException(400, 'Invalid HTTPS form state draft request')
        if marker <= set(value) and (not isinstance(value['marker_id'], str)
                or re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}', value['marker_id']) is None
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in ('before_marker_sha256', 'after_marker_sha256'))):
            raise HTTPException(400, 'Invalid HTTPS form state draft request')
        if 'submitted_field_name' in value and (not isinstance(value['submitted_field_name'], str)
                or re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', value['submitted_field_name']) is None):
            raise HTTPException(400, 'Invalid HTTPS form state draft request')
        return value

    @app.post('/api/web-applications/form-state-preview')
    async def web_form_state_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form state draft request')
        value = form_state_request(await body(request, reject_duplicates=True, limit=10000))
        try:
            plan, checksum = await asyncio.to_thread(
                preview_form_state_plan, web_profiles, web_tasks, web_form_plans,
                value['profile_sha256'], value['task_sha256'], value['form_plan_sha256'],
                value['state_url'], value['before_sha256'], value['after_sha256'],
                value.get('marker_id'), value.get('before_marker_sha256'),
                value.get('after_marker_sha256'), value.get('submitted_field_name'))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'HTTPS form state source or scope unavailable') from None
        return {'state_plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'form_plan_sha256': plan.form_plan_sha256,
                'state_url': plan.state_url, 'marker_id': plan.marker_id,
                'submitted_field_name': plan.submitted_field_name,
                'status': 'unregistered_draft', 'execution_authorized': False,
                'collection_authorized': False}

    @app.post('/api/web-applications/form-state-register')
    async def web_form_state_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid HTTPS form state draft request')
        value = form_state_request(await body(request, reject_duplicates=True, limit=10000),
                                   register=True)
        try:
            checksum, path, plan = await asyncio.to_thread(
                register_form_state_plan, web_profiles, web_tasks,
                web_form_plans, web_form_states,
                value['profile_sha256'], value['task_sha256'], value['form_plan_sha256'],
                value['state_url'], value['before_sha256'], value['after_sha256'],
                value['confirm_sha256'], value.get('marker_id'),
                value.get('before_marker_sha256'), value.get('after_marker_sha256'),
                value.get('submitted_field_name'))
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'HTTPS form state registration unavailable') from None
        return {'state_plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'form_plan_sha256': plan.form_plan_sha256,
                'state_url': plan.state_url, 'marker_id': plan.marker_id,
                'submitted_field_name': plan.submitted_field_name,
                'state_plan_file': str(path.relative_to(REPO_ROOT)),
                'status': 'private_unactivated_draft', 'execution_authorized': False,
                'collection_authorized': False}

    def route_request(value, *, register=False):
        fields = {'profile_sha256', 'task_sha256', 'routes'}
        if register:
            fields.add('confirm_sha256')
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in fields - {'routes'})
                or not isinstance(value.get('routes'), list)
                or not 2 <= len(value['routes']) <= 8
                or any(not isinstance(route, str) or not 1 <= len(route) <= 2048
                       for route in value['routes'])):
            raise HTTPException(400, 'Invalid remote route draft request')
        return value

    @app.post('/api/web-applications/routes-preview')
    async def web_routes_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote route draft request')
        value = route_request(await body(request, reject_duplicates=True, limit=20000))
        try:
            plan, checksum = await asyncio.to_thread(preview_routes, web_profiles, web_tasks,
                value['profile_sha256'], value['task_sha256'], value['routes'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Remote route task or scope unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'route_count': len(plan.routes),
                'status': 'unregistered_draft', 'execution_authorized': False,
                'collection_authorized': False}

    @app.post('/api/web-applications/routes-register')
    async def web_routes_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote route draft request')
        value = route_request(await body(request, reject_duplicates=True, limit=20000), register=True)
        try:
            checksum, path = await asyncio.to_thread(register_routes, web_profiles,
                web_tasks, web_routes, value['profile_sha256'], value['task_sha256'],
                value['routes'], value['confirm_sha256'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'Remote route draft registration unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_sha256': value['task_sha256'], 'route_count': len(value['routes']),
                'status': 'private_unactivated_draft',
                'route_plan_file': str(path.relative_to(REPO_ROOT)),
                'execution_authorized': False, 'collection_authorized': False}

    def static_assets_request(value, *, register=False):
        fields = {'profile_sha256', 'task_sha256', 'assets'}
        if register:
            fields.add('confirm_sha256')
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in fields - {'assets'})
                or not isinstance(value.get('assets'), list)
                or not 1 <= len(value['assets']) <= 8
                or any(not isinstance(asset, dict)
                       or set(asset) != {'url', 'content_type'}
                       or not isinstance(asset['url'], str)
                       or not 1 <= len(asset['url']) <= 2048
                       or not isinstance(asset['content_type'], str)
                       or asset['content_type'] not in STATIC_CONTENT_TYPES
                       for asset in value['assets'])):
            raise HTTPException(400, 'Invalid remote static asset draft request')
        return value

    @app.post('/api/web-applications/static-assets-preview')
    async def web_static_assets_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote static asset draft request')
        value = static_assets_request(await body(request, reject_duplicates=True, limit=20000))
        try:
            plan, checksum = await asyncio.to_thread(preview_static_assets, web_profiles,
                web_tasks, value['profile_sha256'], value['task_sha256'], value['assets'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Remote static asset task or scope unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'asset_count': len(plan.assets),
                'status': 'unregistered_draft', 'execution_authorized': False,
                'collection_authorized': False}

    @app.post('/api/web-applications/static-assets-register')
    async def web_static_assets_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid remote static asset draft request')
        value = static_assets_request(await body(request, reject_duplicates=True, limit=20000),
                                      register=True)
        try:
            checksum, path = await asyncio.to_thread(register_static_assets, web_profiles,
                web_tasks, web_static_assets, value['profile_sha256'], value['task_sha256'],
                value['assets'], value['confirm_sha256'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'Remote static asset draft registration unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_sha256': value['task_sha256'], 'asset_count': len(value['assets']),
                'status': 'private_unactivated_draft',
                'static_plan_file': str(path.relative_to(REPO_ROOT)),
                'execution_authorized': False, 'collection_authorized': False}

    def readonly_data_request(value, *, register=False):
        fields = {'profile_sha256', 'task_sha256', 'assets', 'data_resources'}
        if register:
            fields.add('confirm_sha256')
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value.get(field), str)
                       or re.fullmatch('[a-f0-9]{64}', value[field]) is None
                       for field in fields - {'assets', 'data_resources'})
                or not isinstance(value.get('assets'), list)
                or not 1 <= len(value['assets']) <= 8
                or any(not isinstance(asset, dict)
                       or set(asset) != {'url', 'content_type'}
                       or not isinstance(asset['url'], str)
                       or not 1 <= len(asset['url']) <= 2048
                       or not isinstance(asset['content_type'], str)
                       or asset['content_type'] not in STATIC_CONTENT_TYPES
                       for asset in value['assets'])
                or not isinstance(value.get('data_resources'), list)
                or not 1 <= len(value['data_resources']) <= 4
                or any(not isinstance(resource, dict)
                       or set(resource) != {'url'}
                       or not isinstance(resource['url'], str)
                       or not 1 <= len(resource['url']) <= 2048
                       for resource in value['data_resources'])):
            raise HTTPException(400, 'Invalid read-only data draft request')
        return value

    @app.post('/api/web-applications/readonly-data-preview')
    async def web_readonly_data_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid read-only data draft request')
        value = readonly_data_request(await body(request, reject_duplicates=True, limit=32000))
        try:
            plan, checksum = await asyncio.to_thread(preview_readonly_data, web_profiles,
                web_tasks, value['profile_sha256'], value['task_sha256'], value['assets'],
                value['data_resources'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Read-only data task or scope unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': plan.profile_sha256,
                'task_sha256': plan.task_sha256, 'asset_count': len(plan.assets),
                'data_count': len(plan.data_resources), 'status': 'unregistered_draft',
                'execution_authorized': False, 'collection_authorized': False}

    @app.post('/api/web-applications/readonly-data-register')
    async def web_readonly_data_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Invalid read-only data draft request')
        value = readonly_data_request(await body(request, reject_duplicates=True, limit=32000),
                                      register=True)
        try:
            checksum, path = await asyncio.to_thread(register_readonly_data, web_profiles,
                web_tasks, web_readonly_data, value['profile_sha256'], value['task_sha256'],
                value['assets'], value['data_resources'], value['confirm_sha256'])
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, 'Read-only data draft registration unavailable') from None
        return {'plan_sha256': checksum, 'profile_sha256': value['profile_sha256'],
                'task_sha256': value['task_sha256'], 'asset_count': len(value['assets']),
                'data_count': len(value['data_resources']),
                'status': 'private_unactivated_draft',
                'readonly_data_plan_file': str(path.relative_to(REPO_ROOT)),
                'execution_authorized': False, 'collection_authorized': False}

    @app.get('/api/runs/{run_id}')
    async def trace(run_id: str):
        result = await asyncio.to_thread(inspector.trace, run_id) if inspector else None
        if result is None:
            raise HTTPException(404, 'Run unavailable')
        return result

    @app.get('/api/resources')
    async def resources():
        container_id = controller.runtime.container_id
        if not container_id:
            return {'available': False, 'reason': 'desktop_stopped', 'sampled_at': now()}
        inspected = json.loads(await asyncio.to_thread(controller.runtime.docker, ['inspect', container_id]))[0]
        if inspected['Config']['Labels'].get('com.aos.runtime') != controller.runtime.runtime_id:
            raise HTTPException(409, 'Runtime changed')
        limits = inspected['HostConfig']
        return {'available': True, 'sampled_at': now(), 'source': 'docker_inspect_limits',
                'running': inspected['State']['Running'], 'memory_limit_bytes': limits['Memory'],
                'cpu_limit': limits['NanoCpus'] / 1_000_000_000, 'pids_limit': limits['PidsLimit'],
                'network_mode': limits['NetworkMode'], 'readonly_rootfs': limits['ReadonlyRootfs']}

    @app.get('/api/desktop/pointer')
    async def desktop_pointer(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Pointer source is server configured')
        async with control_lock:
            runtime = controller.runtime
            state = controller.state()
            if state['status'] == 'stopped':
                return {'available': False, 'reason': 'desktop_stopped', 'sampled_at': now()}
            if not isinstance(runtime, DesktopRuntime) or state['runtime_id'] != runtime.runtime_id:
                return {'available': False, 'reason': 'runtime_unavailable', 'sampled_at': now()}
            if runtime.container_id is None:
                return {'available': False, 'reason': 'desktop_stopped', 'sampled_at': now()}
            container_id = runtime.container_id
            runtime_id = runtime.runtime_id
            session_id = state['session_id']
            generation = state['generation']
        try:
            coordinates = await asyncio.to_thread(runtime.pointer_position)
        except AOSFault as fault:
            reason = 'runtime_changed' if fault.code == ErrorCode.UNSAFE_ACTION else 'pointer_unavailable'
            return {'available': False, 'reason': reason, 'sampled_at': now()}
        except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired):
            return {'available': False, 'reason': 'pointer_unavailable', 'sampled_at': now()}
        async with control_lock:
            current = controller.state()
            if (controller.runtime is not runtime or runtime.runtime_id != runtime_id
                    or runtime.container_id != container_id or current['runtime_id'] != runtime_id
                    or current['session_id'] != session_id or current['generation'] != generation
                    or current['status'] == 'stopped'):
                return {'available': False, 'reason': 'runtime_changed', 'sampled_at': now()}
        return {'available': True, **coordinates, 'sampled_at': now()}

    @app.post('/api/control')
    async def control(request: Request):
        value = await body(request)
        commands = {'pause', 'take-control', 'return-control', 'resume', 'stop', 'restart'}
        if set(value) != {'command'} or not isinstance(value['command'], str) or value['command'] not in commands:
            raise HTTPException(400, 'Unknown control')
        async with control_lock:
            if scheduler and scheduler.restart_quiesced:
                raise HTTPException(409, 'Restart admission quiesce is active')
            if value['command'] == 'resume' and scheduler:
                if scheduler.busy or scheduler.sequences.reserved or scheduler.planning_reserved or scheduler.adaptation_reserved:
                    raise HTTPException(409, 'Task is already running')
                await close_streams()
                state = controller.control('resume')
                try:
                    if scheduler.paused:
                        scheduler.resume(state['lease_id'], state['generation'])
                except Exception:
                    controller.control('pause')
                    raise
                return state
            if controller.state()['status'] != 'stopped':
                controller.control('pause')
            if scheduler:
                if value['command'] == 'pause':
                    await scheduler.pause()
                else:
                    await scheduler.cancel('stop' if value['command'] == 'restart' else value['command'].replace('-', '_'))
            await close_streams()
            return controller.control(value['command'])

    @app.get('/api/tasks')
    async def tasks():
        result = scheduler.status() if scheduler else {'available': False, 'busy': False, 'jobs': [], 'approval': None,
                                                     'supports_approve_all': False,
                                                     'supports_learning_metadata': False,
                                                     'auto_approval': None}
        if manager_scope is not None:
            result = result | {'manager_scope': dict(manager_scope)}
            if manager_session is not None:
                result = result | {'manager_session': manager_session}
        return result | {'knowledge_available': document_knowledge is not None,
                         'task_knowledge_available': getattr(scheduler, 'task_knowledge', None) is not None,
                         'knowledge_answer_available': getattr(scheduler, 'knowledge_answer', None) is not None}

    @app.post('/api/tasks/knowledge/{operation}')
    async def task_knowledge_operation(operation: str, request: Request):
        from .task_knowledge import REQUESTS, RESPONSES

        if operation not in REQUESTS or request.query_params:
            raise HTTPException(400, 'Unknown task knowledge operation')
        value = await body(request, reject_duplicates=True, limit=524288)
        if set(value) != set(REQUESTS[operation].model_fields):
            raise HTTPException(400, 'Exact task knowledge request required')
        try:
            parsed = REQUESTS[operation].model_validate(value).model_dump()
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid task knowledge request') from None
        if getattr(scheduler, 'task_knowledge', None) is None:
            raise HTTPException(409, 'Task knowledge unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in parsed.items() if key != 'schema_version'}
                if operation == 'preview':
                    result = scheduler.preview_task_knowledge(**arguments)
                elif operation == 'start':
                    result = scheduler.start_task_knowledge(**arguments)
                else:
                    result = scheduler.task_knowledge.report(**arguments)
                return JSONResponse(RESPONSES[operation].model_validate(result).model_dump())
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Task knowledge source, consent or control unavailable') from None

    @app.post('/api/knowledge-answer/{operation}')
    async def knowledge_answer_operation(operation: str, request: Request):
        from .knowledge_answer import REQUESTS, RESPONSES

        if operation not in REQUESTS or request.query_params:
            raise HTTPException(400, 'Unknown knowledge answer operation')
        value = await body(request, reject_duplicates=True, limit=524288)
        if set(value) != set(REQUESTS[operation].model_fields):
            raise HTTPException(400, 'Exact knowledge answer request required')
        try:
            parsed = REQUESTS[operation].model_validate(value).model_dump()
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid bounded knowledge answer request') from None
        service = getattr(scheduler, 'knowledge_answer', None)
        if service is None:
            raise HTTPException(409, 'Knowledge answer unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in parsed.items() if key != 'schema_version'}
                if operation == 'start':
                    if scheduler.reserved:
                        raise ValueError('knowledge_answer_requires_idle')
                    result = service.begin(**arguments)
                elif operation == 'cancel':
                    if service.status()['answer_id'] != parsed['answer_id']:
                        raise ValueError('knowledge_answer_selection_changed')
                    await service.cancel()
                    result = service.status(parsed['answer_id'])
                else:
                    result = getattr(service, operation)(**arguments)
                return JSONResponse(RESPONSES[operation].model_validate(result).model_dump())
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Knowledge answer evidence, consent or control unavailable') from None

    @app.post('/api/knowledge/{operation}')
    async def knowledge_operation(operation: str, request: Request):
        from pydantic import ValidationError as TypedValidationError
        from .knowledge import REQUESTS, RESPONSES

        if operation not in REQUESTS or request.query_params:
            raise HTTPException(400, 'Unknown knowledge operation')
        value = await body(request, reject_duplicates=True, limit=524288)
        if set(value) != set(REQUESTS[operation].model_fields):
            raise HTTPException(400, 'Exact knowledge request required')
        try:
            parsed = REQUESTS[operation].model_validate(value).model_dump()
        except (TypedValidationError, ValueError, TypeError):
            raise HTTPException(400, 'Invalid bounded knowledge request') from None
        if document_knowledge is None:
            raise HTTPException(409, 'Document knowledge unavailable')
        async with control_lock:
            try:
                if operation in {'publish', 'review'}:
                    control = controller.state()
                    if (control['owner'] != 'AGENT' or control['status'] != 'running'
                            or scheduler is None or scheduler.reserved
                            or getattr(scheduler, 'closed', False) or getattr(scheduler, 'restart_quiesced', False)
                            or parsed['lease_id'] != control['lease_id'] or parsed['generation'] != control['generation']):
                        raise ValueError('current_idle_control_required')
                arguments = {key: item for key, item in parsed.items()
                             if key not in {'schema_version', 'lease_id', 'generation'}}
                result = getattr(document_knowledge, operation.replace('-', '_'))(**arguments)
                result = RESPONSES[operation].model_validate(result).model_dump()
                return JSONResponse(result)
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Knowledge evidence, scope or confirmation unavailable') from None

    @app.post('/api/tasks/owned-skill-knowledge/{operation}')
    async def owned_skill_knowledge_operation(operation: str, request: Request):
        from .owned_skill_knowledge import (
            OwnedSkillKnowledgePreviewRequest, OwnedSkillKnowledgeStartRequest, OwnedSkillKnowledgeReportRequest)
        from .owned_skill_knowledge_transport import (
            OwnedSkillKnowledgeCanonicalStartRequest, preview_transport, report_transport)

        requests = {'preview': OwnedSkillKnowledgePreviewRequest,
                    'start': OwnedSkillKnowledgeStartRequest, 'report': OwnedSkillKnowledgeReportRequest}
        if request.query_params or operation not in requests:
            raise HTTPException(400, 'Unknown planning context operation')
        value = await body(request, reject_duplicates=True, limit=131072)
        try:
            if operation == 'start' and value.get('schema_version') == '1.1':
                value = OwnedSkillKnowledgeCanonicalStartRequest.model_validate(value).normalized_start()
            else:
                value = requests[operation].model_validate(value).model_dump(mode='json')
        except (ValueError, TypeError):
            raise HTTPException(400, 'Exact planning context request required') from None
        planning = getattr(scheduler, 'owned_skill_planning', None)
        service = getattr(planning, 'knowledge', None)
        if service is None:
            raise HTTPException(409, 'Planning context requires a configured selected-skill session')
        async with control_lock:
            try:
                if operation == 'report':
                    return report_transport(service.report(value['planning_bundle_sha256']))
                if operation == 'preview':
                    return preview_transport(service.preview(**value))
                return JSONResponse(service.begin(**value), status_code=202)
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Planning context evidence, authority or confirmation unavailable') from None

    @app.post('/api/tasks/owned-web-goal/{operation}')
    async def owned_web_goal_operation(operation: str, request: Request):
        from .dataset import validator
        from .web_goal_planner import WebGoalPlan

        if operation not in {'catalog', 'preview', 'propose', 'preview-planned', 'start-planned', 'report', 'recover-start', 'preview-knowledge', 'propose-knowledge', 'report-knowledge'} or request.query_params:
            raise HTTPException(400, 'Unknown owned web goal operation')
        value = await body(request, reject_duplicates=True,
                           limit=131072 if operation == 'propose-knowledge' else 16384)
        if operation == 'report-knowledge':
            if scheduler is None or not validator('web_goal_knowledge_report_request').is_valid(value):
                raise HTTPException(400, 'Exact saved web goal knowledge report request required')
            async with control_lock:
                try:
                    return scheduler.web_goal_knowledge_report(value['bundle_sha256'])
                except (ValueError, TypeError, KeyError, OSError, AOSFault):
                    raise HTTPException(409, 'Independent knowledge binding report unavailable; no replay authorized') from None
        if operation in {'preview-knowledge', 'propose-knowledge'}:
            schema = ('web_goal_knowledge_preview_request' if operation == 'preview-knowledge'
                      else 'web_goal_knowledge_start_request')
            if scheduler is None or not validator(schema).is_valid(value):
                raise HTTPException(400, 'Exact reviewed web goal knowledge request required')
            arguments = {key: item for key, item in value.items() if key != 'schema_version'}
            async with control_lock:
                try:
                    if operation == 'preview-knowledge':
                        return scheduler.preview_web_goal_knowledge(**arguments)
                    return JSONResponse(scheduler.begin_web_goal_knowledge(**arguments), status_code=202)
                except (ValueError, TypeError, KeyError, OSError, AOSFault):
                    raise HTTPException(409, 'Reviewed source or authority changed; no automatic retry') from None
        if operation == 'recover-start':
            if scheduler is None or not validator('web_goal_start_recovery_request').is_valid(value):
                raise HTTPException(400, 'Exact start recovery confirmation required')
            async with control_lock:
                try:
                    return scheduler.recover_web_goal_start(
                        value['bundle_sha256'], value['job_id'], value['confirm_job_id'],
                        value['lease_id'], value['generation'])
                except (ValueError, TypeError, KeyError, OSError, AOSFault):
                    raise HTTPException(409, 'Existing task acknowledgement cannot be recovered; do not repeat start') from None
        if operation == 'report':
            if scheduler is None or not validator('web_goal_report_request').is_valid(value):
                raise HTTPException(400, 'Exact web goal report request required')
            async with control_lock:
                try:
                    return scheduler.web_goal_execution_report(value['bundle_sha256'])
                except (ValueError, TypeError, KeyError, OSError, AOSFault):
                    raise HTTPException(409, 'Independent web goal result unavailable; no replay authorized') from None
        schema = 'web_goal_planning_request' if operation in {'propose', 'preview-planned', 'start-planned'} else 'owned_web_goal_request'
        if (not validator(schema).is_valid(value)
                or 'proposal' in value and not validator('web_goal_plan').is_valid(value['proposal'])):
            raise HTTPException(400, 'Invalid canonical owned web goal request')
        expected = {'schema_version', 'lease_id', 'generation'}
        if operation == 'preview':
            expected |= {'proposal', 'confirm_catalog_sha256'}
        elif operation == 'propose':
            expected |= {'goal', 'inference_consent', 'confirm_catalog_sha256'}
        elif operation in {'preview-planned', 'start-planned'}:
            expected |= {'bundle_sha256', 'confirm_bundle_sha256'}
            if operation == 'start-planned':
                expected |= {'preview_sha256', 'confirm_preview_sha256'}
        if (set(value) != expected or value['schema_version'] != '1.0'
                or type(value['lease_id']) is not str or not value['lease_id']
                or type(value['generation']) is not int or value['generation'] < 0):
            raise HTTPException(400, 'Exact owned web goal request required')
        if scheduler is None:
            raise HTTPException(409, 'Owned web goal source unavailable')
        async with control_lock:
            try:
                if operation == 'propose':
                    return JSONResponse(scheduler.begin_web_goal_plan(
                        value['goal'], value['lease_id'], value['generation'],
                        confirm_catalog_sha256=value['confirm_catalog_sha256'],
                        inference_consent=value['inference_consent']), status_code=202)
                if operation == 'preview-planned':
                    return scheduler.preview_planned_web_goal(
                        value['bundle_sha256'], value['confirm_bundle_sha256'],
                        value['lease_id'], value['generation'])
                if operation == 'start-planned':
                    return JSONResponse(scheduler.start_planned_web_goal(
                        value['bundle_sha256'], value['confirm_bundle_sha256'],
                        value['preview_sha256'], value['confirm_preview_sha256'],
                        value['lease_id'], value['generation']), status_code=202)
                if operation == 'catalog':
                    catalog = scheduler.owned_web_goal_catalog(value['lease_id'], value['generation'])
                    result = catalog.model_dump(mode='json')
                    return {'catalog': result, 'catalog_sha256': digest(result)}
                proposal = WebGoalPlan.model_validate(value['proposal'])
                return scheduler.preview_owned_web_goal(
                    proposal, value['confirm_catalog_sha256'], value['lease_id'], value['generation'])
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                if operation == 'start-planned':
                    raise HTTPException(409, 'Task start was not independently acknowledged; inspect the task timeline and do not repeat the start') from None
                raise HTTPException(409, 'Owned web goal source, scope or control changed; no execution started') from None

    @app.post('/api/tasks/parameter-web-goal/{operation}')
    async def parameter_web_goal_operation(operation: str, request: Request):
        from .web_goal_execution_requests import (ParameterWebGoalPreviewRequest,
                                                  ParameterWebGoalStartRequest,
                                                  ParameterWebGoalReportRequest)

        models = {'preview': ParameterWebGoalPreviewRequest, 'start': ParameterWebGoalStartRequest,
                  'report': ParameterWebGoalReportRequest}
        if operation not in models or request.query_params:
            raise HTTPException(400, 'Unknown parameter web goal operation')
        value = await body(request, reject_duplicates=True, limit=4096)
        try:
            parsed = models[operation].model_validate_json(canonical(value))
            if canonical(parsed.model_dump(mode='json')) != canonical(value):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(400, 'Exact parameter web goal request required') from None
        service = getattr(scheduler, 'parameter_web_goal_execution', None)
        if service is None:
            raise HTTPException(409, 'Trusted parameter web goal host composition unavailable')
        arguments = {key: item for key, item in value.items() if key != 'schema_version'}
        async with control_lock:
            try:
                if operation == 'preview':
                    return service.preview(**arguments)
                if operation == 'report':
                    return service.journal.inspect(arguments['intent_sha256'])
                return JSONResponse(scheduler.start_parameter_web_goal_execution(**arguments), status_code=202)
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Parameter task source, control or receipt unavailable; no replay authorized') from None

    @app.post('/api/tasks/parameter-project-skill/{operation}')
    async def parameter_project_skill(request: Request, operation: str):
        from .owned_parameter_skill_requests import (
            OwnedParameterSkillPreviewRequest, OwnedParameterSkillPublishRequest,
            OwnedParameterSkillReadRequest)

        models = {'preview': OwnedParameterSkillPreviewRequest,
                  'publish': OwnedParameterSkillPublishRequest,
                  'read': OwnedParameterSkillReadRequest}
        if operation not in models or request.query_params:
            raise HTTPException(400, 'Exact manual skill request required')
        if scheduler is None or getattr(scheduler, 'owned_parameter_project_execution', None) is None:
            raise HTTPException(409, 'Manual skill host composition unavailable')
        try:
            value = models[operation].model_validate_json(canonical(
                await body(request, limit=4096, reject_duplicates=True))).model_dump(mode='json')
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, 'Exact manual skill request required') from None
        async with control_lock:
            try:
                if operation == 'publish':
                    control = controller.state()
                    if (scheduler.reserved or scheduler.closed or scheduler.restart_quiesced
                            or control['owner'] != 'AGENT' or control['status'] != 'running'
                            or control['lease_id'] != value['lease_id']
                            or control['generation'] != value['generation']):
                        raise ValueError('manual_skill_review_requires_fresh_control')
                service = scheduler.parameter_skill_candidate_session()
                if operation == 'preview':
                    candidate, checksum = await asyncio.to_thread(service.preview, value['intent_sha256'])
                elif operation == 'read':
                    candidate, checksum = await asyncio.to_thread(service.read, value['candidate_sha256'])
                else:
                    candidate, checksum = await asyncio.to_thread(service.publish, value['intent_sha256'],
                        confirm_candidate_sha256=value['confirm_candidate_sha256'],
                        human_confirmation=value['human_confirmation'])
                return {'schema_version': '1.0', 'candidate': candidate,
                        'candidate_canonical': canonical(candidate), 'candidate_sha256': checksum,
                        'status': 'awaiting_manual_review', 'native_model_verified': False,
                        'activation_authorized': False, 'training_ready': False,
                        'gpu_release_verified': False}
            except (ValueError, TypeError, KeyError, OSError, AOSFault, sqlite3.Error):
                raise HTTPException(409, 'Manual skill source or evidence unavailable; no replay authorized') from None

    @app.post('/api/tasks/parameter-project-skill-review/{operation}')
    async def parameter_project_skill_review(request: Request, operation: str):
        from .owned_parameter_skill_review_requests import (
            OwnedParameterSkillReviewAcceptRequest, OwnedParameterSkillReviewPreviewRequest,
            OwnedParameterSkillReviewReadRequest, OwnedParameterSkillReviewRevokeRequest,
            OwnedParameterSkillReviewRecoveryPreviewRequest, OwnedParameterSkillReviewRecoverRequest)

        models = {'preview': OwnedParameterSkillReviewPreviewRequest,
                  'accept': OwnedParameterSkillReviewAcceptRequest,
                  'read': OwnedParameterSkillReviewReadRequest,
                  'revoke-preview': OwnedParameterSkillReviewReadRequest,
                  'revoke': OwnedParameterSkillReviewRevokeRequest,
                  'recovery-preview': OwnedParameterSkillReviewRecoveryPreviewRequest,
                  'recover': OwnedParameterSkillReviewRecoverRequest}
        if operation not in models or request.query_params:
            raise HTTPException(400, 'Exact manual skill review request required')
        if scheduler is None or getattr(scheduler, 'owned_parameter_project_execution', None) is None:
            raise HTTPException(409, 'Manual skill review host unavailable')
        try:
            value = models[operation].model_validate_json(canonical(
                await body(request, limit=4096, reject_duplicates=True))).model_dump(mode='json')
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, 'Exact manual skill review request required') from None
        async with control_lock:
            try:
                if operation in {'accept', 'revoke', 'recover'}:
                    control = controller.state()
                    if (scheduler.reserved or scheduler.closed or scheduler.restart_quiesced
                            or control['owner'] != 'AGENT' or control['status'] != 'running'
                            or control['lease_id'] != value['lease_id']
                            or control['generation'] != value['generation']):
                        raise ValueError('manual_skill_review_requires_fresh_control')
                service = scheduler.parameter_skill_review_session()
                if operation == 'preview':
                    result, checksum = await asyncio.to_thread(service.preview, value['candidate_sha256'])
                    key = 'review'
                elif operation == 'accept':
                    result, checksum = await asyncio.to_thread(service.accept, value['candidate_sha256'],
                        confirm_review_sha256=value['confirm_review_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'review'
                elif operation == 'read':
                    result, checksum = await asyncio.to_thread(service.read, value['review_sha256'])
                    key = 'status'
                elif operation == 'revoke-preview':
                    result, checksum = await asyncio.to_thread(service.revocation_preview, value['review_sha256'])
                    key = 'revocation'
                elif operation == 'recovery-preview':
                    result, checksum = await asyncio.to_thread(service.recovery_preview, value['record_sha256'])
                    key = 'recovery'
                elif operation == 'recover':
                    result, checksum = await asyncio.to_thread(service.recover, value['record_sha256'],
                        confirm_recovery_sha256=value['confirm_recovery_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'recovery'
                else:
                    result, checksum = await asyncio.to_thread(service.revoke, value['review_sha256'],
                        confirm_revocation_sha256=value['confirm_revocation_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'revocation'
                return {'schema_version': '1.0', key: result, key + '_canonical': canonical(result),
                        key + '_sha256': checksum, 'native_model_verified': False,
                        'activation_authorized': False, 'execution_authorized': False,
                        'training_ready': False, 'gpu_release_verified': False}
            except (ValueError, TypeError, KeyError, OSError, AOSFault, sqlite3.Error):
                raise HTTPException(409, 'Manual skill review source or evidence unavailable; no replay authorized') from None

    @app.post('/api/tasks/parameter-project-skill-reuse/{operation}')
    async def parameter_skill_reuse_operation(operation: str, request: Request):
        from .owned_parameter_skill_reuse_requests import REQUESTS

        if operation not in REQUESTS or request.query_params:
            raise HTTPException(400, 'Invalid manual skill reuse operation')
        value = await body(request, reject_duplicates=True, limit=8192)
        try:
            selection = REQUESTS[operation].model_validate(value).model_dump(mode='json')
        except ValueError:
            raise HTTPException(422, 'Invalid manual skill reuse request') from None
        if scheduler is None:
            raise HTTPException(409, 'Manual skill reuse unavailable')
        async with control_lock:
            try:
                if operation in {'status', 'read'}:
                    service = getattr(scheduler, '_parameter_skill_reuse_execution', None)
                    if service is None:
                        raise ValueError('owned_parameter_reuse_not_configured')
                    report = service.status() if operation == 'status' else service.read(selection['intent_sha256'])
                    return {'schema_version': '1.0', **report,
                            'transition_blocked': service.transition_blocked,
                            'transition_in_progress': service.transitioning}
                service = scheduler.parameter_skill_reuse_execution_session()
                arguments = {key: selection[key] for key in (
                    'release_sha256', 'selection_sha256', 'parameters', 'lease_id', 'generation')}
                if operation in {'next-preview', 'next-start'}:
                    arguments |= {key: selection[key] for key in (
                        'previous_intent_sha256', 'previous_receipt_sha256')}
                    preview = service.next_preview(**arguments)
                else:
                    preview = service.preview(**arguments)
                if operation in {'preview', 'next-preview'}:
                    return preview
                if operation == 'next-start':
                    result = await service.next_start(
                        preview['admission'],
                        previous_intent_sha256=selection['previous_intent_sha256'],
                        previous_receipt_sha256=selection['previous_receipt_sha256'],
                        confirm_sha256=selection['confirm_sha256'],
                        human_confirmation=selection['human_confirmation'],
                        lease_id=selection['lease_id'], generation=selection['generation'])
                    return {'schema_version': '1.0', **result}
                result = service.start(
                    preview['admission'], confirm_sha256=selection['confirm_sha256'],
                    human_confirmation=selection['human_confirmation'],
                    lease_id=selection['lease_id'], generation=selection['generation'])
                return {'schema_version': '1.0', **result}
            except (AOSFault, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(409, 'Manual skill source, selection or control changed; inspect status before retry') from None

    @app.post('/api/tasks/parameter-project-skill-release/{operation}')
    async def parameter_project_skill_release(request: Request, operation: str):
        from .owned_parameter_skill_release_requests import (
            OwnedParameterSkillReleasePreviewRequest, OwnedParameterSkillReleaseRequest,
            OwnedParameterSkillReleaseReadRequest, OwnedParameterSkillReleaseInventoryRequest,
            OwnedParameterSkillSelectionPreviewRequest, OwnedParameterSkillSelectRequest,
            OwnedParameterSkillRollbackRequest, OwnedParameterSkillReleaseRecoveryPreviewRequest,
            OwnedParameterSkillReleaseRecoverRequest)

        models = {'preview': OwnedParameterSkillReleasePreviewRequest,
                  'release': OwnedParameterSkillReleaseRequest,
                  'read': OwnedParameterSkillReleaseReadRequest,
                  'inventory': OwnedParameterSkillReleaseInventoryRequest,
                  'select-preview': OwnedParameterSkillSelectionPreviewRequest,
                  'select': OwnedParameterSkillSelectRequest,
                  'rollback-preview': OwnedParameterSkillSelectionPreviewRequest,
                  'rollback': OwnedParameterSkillRollbackRequest,
                  'recovery-preview': OwnedParameterSkillReleaseRecoveryPreviewRequest,
                  'recover': OwnedParameterSkillReleaseRecoverRequest}
        if operation not in models or request.query_params:
            raise HTTPException(400, 'Exact manual skill release request required')
        if scheduler is None or getattr(scheduler, 'owned_parameter_project_execution', None) is None:
            raise HTTPException(409, 'Manual skill release host unavailable')
        try:
            value = models[operation].model_validate_json(canonical(
                await body(request, limit=4096, reject_duplicates=True))).model_dump(mode='json')
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, 'Exact manual skill release request required') from None
        async with control_lock:
            try:
                if operation in {'release', 'select', 'rollback', 'recover'}:
                    control = controller.state()
                    if (scheduler.reserved or scheduler.closed or scheduler.restart_quiesced
                            or control['owner'] != 'AGENT' or control['status'] != 'running'
                            or control['lease_id'] != value['lease_id']
                            or control['generation'] != value['generation']):
                        raise ValueError('manual_skill_release_requires_fresh_control')
                service = scheduler.parameter_skill_release_session()
                flags = {'schema_version': '1.0', 'native_model_verified': False,
                         'activation_authorized': False, 'execution_authorized': False,
                         'training_ready': False, 'gpu_release_verified': False}
                if operation == 'inventory':
                    return flags | {'inventory': await asyncio.to_thread(service.inventory)}
                if operation == 'preview':
                    result, checksum = await asyncio.to_thread(service.preview, value['review_sha256'],
                        expected_parent_release_sha256=value['expected_parent_release_sha256'])
                    key = 'release'
                elif operation == 'release':
                    result, checksum = await asyncio.to_thread(service.release, value['review_sha256'],
                        expected_parent_release_sha256=value['expected_parent_release_sha256'],
                        confirm_release_sha256=value['confirm_release_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'release'
                elif operation == 'read':
                    result, checksum = await asyncio.to_thread(service.read, value['release_sha256'])
                    key = 'release'
                elif operation == 'recovery-preview':
                    result, checksum = await asyncio.to_thread(service.recovery_preview, value['record_sha256'])
                    key = 'recovery'
                elif operation == 'recover':
                    result, checksum = await asyncio.to_thread(service.recover, value['record_sha256'],
                        confirm_recovery_sha256=value['confirm_recovery_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'recovery'
                elif operation in {'select-preview', 'rollback-preview'}:
                    result, checksum = await asyncio.to_thread(service.selection_preview, value['release_sha256'],
                        expected_selection_sha256=value['expected_selection_sha256'],
                        operation='rollback' if operation == 'rollback-preview' else 'select')
                    key = 'selection'
                else:
                    result, checksum = await asyncio.to_thread(service.select, value['release_sha256'],
                        expected_selection_sha256=value['expected_selection_sha256'],
                        operation=operation, confirm_selection_sha256=value['confirm_selection_sha256'],
                        human_confirmation=value['human_confirmation'])
                    key = 'selection'
                return flags | {key: result, key + '_canonical': canonical(result), key + '_sha256': checksum}
            except (ValueError, TypeError, KeyError, OSError, AOSFault, sqlite3.Error):
                raise HTTPException(409, 'Manual skill release source, review or history unavailable; no replay authorized') from None

    @app.get('/api/tasks/owned-web-goal')
    async def web_goal_plan_status(request: Request):
        if request.query_params or scheduler is None:
            raise HTTPException(409, 'Web goal planning session unavailable')
        return scheduler.web_goal_plan_status()

    @app.get('/api/tasks/owned-skill-plan')
    async def owned_skill_plan_status(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Skill planning is session configured')
        if scheduler is None:
            raise HTTPException(409, 'Owned skill planning unavailable')
        return scheduler.owned_skill_plan_status()

    @app.post('/api/tasks/owned-skill-plan', status_code=202)
    async def owned_skill_plan_begin(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Skill planning input belongs in the request body')
        value = await body(request, reject_duplicates=True, limit=2048)
        common = {'schema_version', 'goal', 'lease_id', 'generation'}
        if not ((set(value) == common and value['schema_version'] == '1.0')
                or (set(value) == common | {'collect_learning'} and value['schema_version'] == '1.1'
                    and type(value['collect_learning']) is bool)):
            raise HTTPException(400, 'Invalid skill planning request')
        if scheduler is None:
            raise HTTPException(409, 'Owned skill planning unavailable')
        async with control_lock:
            try:
                return scheduler.begin_owned_skill_plan(
                    value['goal'], value['lease_id'], value['generation'],
                    collect_learning=value.get('collect_learning', False))
            except (ValueError, TypeError, AOSFault):
                raise HTTPException(409, 'Skill planning requires an idle current selected-skill session') from None

    @app.post('/api/tasks/failure-improvement/{operation}')
    async def failure_improvement_operation(operation: str, request: Request):
        from .dataset import validator

        selections = {
            'preview': set(),
            'save': {'confirm_sha256', 'consent'},
            'review': {'candidate_sha256', 'decision', 'correction_code', 'confirm_sha256'},
            'revoke': {'candidate_sha256', 'receipt_sha256'},
        }
        if operation not in selections or request.query_params:
            raise HTTPException(400, 'Unknown failure improvement operation')
        write = operation != 'preview'
        expected = {'schema_version', 'job_id'} | selections[operation]
        if write:
            expected |= {'lease_id', 'generation'}
        value = await body(request, reject_duplicates=True, limit=2048)
        if (set(value) != expected or value['schema_version'] != '1.0'
                or type(value['job_id']) is not str
                or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['job_id']) is None):
            raise HTTPException(400, 'Exact failed task selection required')
        if scheduler is None:
            raise HTTPException(409, 'Failure improvement unavailable')
        async with control_lock:
            try:
                if write:
                    control = controller.state()
                    if (control['owner'] != 'AGENT' or control['status'] != 'running'
                            or scheduler.reserved or getattr(scheduler, 'closed', False)
                            or getattr(scheduler, 'restart_quiesced', False)
                            or type(value['lease_id']) is not str or value['lease_id'] != control['lease_id']
                            or type(value['generation']) is not int or value['generation'] != control['generation']):
                        raise ValueError('current_idle_control_required')
                service = getattr(scheduler, 'failure_improvements', None)
                if service is None:
                    raise ValueError('failure_improvement_unavailable')
                arguments = {key: item for key, item in value.items()
                             if key not in {'schema_version', 'lease_id', 'generation'}}
                result = getattr(service, operation)(**arguments)
                validator('failure_improvement').validate(result)
                if result['job_id'] != value['job_id']:
                    raise ValueError('failure_improvement_response_changed')
                return JSONResponse(result)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, AOSFault, SchemaError, ValidationError):
                raise HTTPException(409, 'Failure evidence or review unavailable; no retry or training authorized') from None

    @app.post('/api/tasks/failure-followup/{operation}')
    async def failure_followup_operation(operation: str, request: Request):
        from .dataset import validator

        fields = {
            'preview': {'source_job_id', 'candidate_sha256', 'receipt_sha256'},
            'start': {'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'},
            'inspect': {'job_id'},
        }
        if operation not in fields or request.query_params:
            raise HTTPException(400, 'Unknown follow-up operation')
        value = await body(request, reject_duplicates=True, limit=16384)
        if set(value) != fields[operation] | {'schema_version'} or value['schema_version'] != '1.0':
            raise HTTPException(400, 'Exact follow-up selection required')
        if scheduler is None:
            raise HTTPException(409, 'Follow-up unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in value.items() if key != 'schema_version'}
                if operation == 'preview':
                    result = scheduler.preview_failure_followup(**arguments)
                    schema = 'failure_followup_preview'
                    if any(result[key] != value[key] for key in fields['preview']):
                        raise ValueError('failure_followup_selection_changed')
                elif operation == 'start':
                    result = scheduler.start_failure_followup(**arguments)
                    schema = 'failure_followup_start'
                else:
                    if (type(value['job_id']) is not str
                            or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['job_id']) is None):
                        raise ValueError('failure_followup_job_invalid')
                    result = scheduler.failure_followups.inspect(value['job_id'])
                    schema = 'failure_followup_report'
                    if result['job_id'] != value['job_id']:
                        raise ValueError('failure_followup_report_changed')
                validator(schema).validate(result)
                return JSONResponse(result)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
                    AOSFault, SchemaError, ValidationError):
                raise HTTPException(409, 'Follow-up evidence or authorization unavailable; no correction or training certified') from None

    @app.post('/api/tasks/failure-guidance/{operation}')
    async def failure_guidance_operation(operation: str, request: Request):
        from .dataset import validator

        fields = {
            'preview': {'source_job_id', 'candidate_sha256', 'receipt_sha256'},
            'start': {'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'},
            'inspect': {'job_id'},
        }
        if operation not in fields or request.query_params:
            raise HTTPException(400, 'Unknown guidance operation')
        value = await body(request, reject_duplicates=True, limit=16384)
        if set(value) != fields[operation] | {'schema_version'} or value['schema_version'] != '1.0':
            raise HTTPException(400, 'Exact guidance selection required')
        if scheduler is None:
            raise HTTPException(409, 'Guidance unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in value.items() if key != 'schema_version'}
                if operation == 'preview':
                    result = scheduler.preview_failure_guidance(**arguments)
                    schema = 'failure_guidance_preview'
                    if any(result['followup'][key] != value[key] for key in fields['preview']):
                        raise ValueError('failure_guidance_selection_changed')
                elif operation == 'start':
                    result = scheduler.start_failure_guidance(**arguments)
                    schema = 'failure_guidance_start'
                else:
                    if (type(value['job_id']) is not str
                            or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['job_id']) is None):
                        raise ValueError('failure_guidance_job_invalid')
                    result = scheduler.failure_guidance.inspect(value['job_id'])
                    schema = 'failure_guidance_report'
                    if result['job_id'] != value['job_id']:
                        raise ValueError('failure_guidance_report_changed')
                validator(schema).validate(result)
                return JSONResponse(result)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
                    AOSFault, SchemaError, ValidationError):
                raise HTTPException(409, 'Reviewed guidance evidence or authorization unavailable; no causal correction or training certified') from None

    @app.post('/api/tasks/hello-guidance-reuse/{operation}')
    async def hello_guidance_reuse_operation(operation: str, request: Request):
        from .dataset import validator

        fields = {
            'publish-preview': {'source_job_id', 'candidate_sha256', 'receipt_sha256'},
            'publish': {'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'},
            'inspect': {'entry_sha256'}, 'reuse-preview': {'entry_sha256'},
            'start': {'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'},
            'report': {'job_id'},
        }
        if operation not in fields or request.query_params:
            raise HTTPException(400, 'Unknown hello reuse operation')
        value = await body(request, reject_duplicates=True, limit=16384)
        if set(value) != fields[operation] | {'schema_version'} or value['schema_version'] != '1.0':
            raise HTTPException(400, 'Exact hello reuse selection required')
        if scheduler is None:
            raise HTTPException(409, 'Hello reuse unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in value.items() if key != 'schema_version'}
                service = scheduler.hello_guidance_reuse
                if operation == 'publish-preview':
                    result = service.publish_preview(**arguments)
                    schema = 'hello_reuse_publication_preview'
                    if any(result['entry']['source'][key] != value[key] for key in fields[operation]):
                        raise ValueError('hello_reuse_source_changed')
                elif operation == 'publish':
                    result = scheduler.publish_hello_guidance_reuse(**arguments)
                    schema = 'hello_reuse_entry_response'
                elif operation in {'inspect', 'reuse-preview'}:
                    result = getattr(service, operation.replace('-', '_'))(**arguments)
                    schema = 'hello_reuse_entry_response' if operation == 'inspect' else 'hello_reuse_preview'
                    if result['entry_sha256'] != value['entry_sha256']:
                        raise ValueError('hello_reuse_entry_changed')
                elif operation == 'start':
                    result = scheduler.start_hello_guidance_reuse(**arguments)
                    schema = 'hello_reuse_start'
                else:
                    if (type(value['job_id']) is not str
                            or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['job_id']) is None):
                        raise ValueError('hello_reuse_job_invalid')
                    result = service.report(**arguments)
                    schema = 'hello_reuse_report'
                    if result['job_id'] != value['job_id']:
                        raise ValueError('hello_reuse_report_changed')
                validator(schema).validate(result)
                return JSONResponse(result)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
                    AOSFault, SchemaError, ValidationError):
                raise HTTPException(409, 'Reviewed hello reuse source or scope unavailable; no replay, causal correction or training certified') from None

    @app.post('/api/tasks/owned-episode/{operation}')
    async def owned_episode_operation(operation: str, request: Request):
        read_only = operation in {'inspect', 'conversion-preview', 'readiness', 'adaptation-inspect', 'runtime-audit', 'pair-report'}
        expected = {'schema_version', 'episode_id'}
        if operation == 'review':
            expected |= {'role', 'decision', 'confirm_sha256'}
        elif operation == 'revoke':
            expected |= {'role', 'confirm_sha256'}
        elif operation == 'export':
            expected.add('review_receipts')
        elif operation in {'conversion-preview', 'convert'}:
            expected.add('export_sha256')
            if operation == 'convert':
                expected.add('confirm_sha256')
        elif operation in {'readiness', 'tokenizer-start'}:
            expected.add('conversion_sha256')
            if operation == 'tokenizer-start':
                expected.add('confirm_sha256')
        elif operation in {'adaptation-preview', 'adaptation-start'}:
            expected.add('conversion_sha256')
            if operation == 'adaptation-start':
                expected |= {'confirm_sha256', 'rights_redaction_reviewed', 'experimental_training_authorized'}
        elif operation == 'adaptation-inspect':
            expected.add('authorization_sha256')
        elif operation in {'runtime-preview', 'runtime-start'}:
            expected |= {'authorization_sha256', 'case_key', 'development_value'}
            if operation == 'runtime-start':
                expected |= {'confirm_sha256', 'experimental_runtime_authorized'}
        elif operation == 'runtime-audit':
            expected |= {'authorization_sha256', 'candidate_execution_sha256'}
        elif operation in {'pair-preview', 'pair-commit'}:
            expected |= {'authorization_sha256', 'case_key', 'development_value'}
            if operation == 'pair-commit':
                expected |= {'confirm_sha256', 'experimental_evaluation_authorized'}
        elif operation == 'pair-start':
            expected |= {'pair_sha256', 'arm', 'confirm_sha256', 'experimental_runtime_authorized'}
        elif operation == 'pair-report':
            expected.add('pair_sha256')
        elif operation != 'inspect':
            raise HTTPException(400, 'Unknown episode operation')
        if not read_only:
            expected |= {'lease_id', 'generation'}
        value = await body(request, reject_duplicates=True, limit=2048)
        if (request.query_params or set(value) != expected or value['schema_version'] != '1.0'
                or type(value['episode_id']) is not str
                or re.fullmatch(r'episode-[a-f0-9]{32}', value['episode_id']) is None):
            raise HTTPException(400, 'Exact episode selection required')
        if scheduler is None or scheduler.owned_episode_learning is None:
            raise HTTPException(409, 'Episode learning unavailable')
        async with control_lock:
            try:
                if not read_only:
                    control = controller.state()
                    if (control['owner'] != 'AGENT' or control['status'] != 'running' or scheduler.reserved
                            or type(value['lease_id']) is not str or value['lease_id'] != control['lease_id']
                            or type(value['generation']) is not int or value['generation'] != control['generation']):
                        raise ValueError('current_idle_control_required')
                method = getattr(scheduler.owned_episode_learning, operation.replace('-', '_'))
                excluded = {'schema_version'} if operation in {'tokenizer-start', 'adaptation-preview', 'adaptation-start',
                                                               'runtime-preview', 'runtime-start', 'pair-preview',
                                                               'pair-commit', 'pair-start'} else {
                    'schema_version', 'lease_id', 'generation'}
                result = method(**{key: item for key, item in value.items() if key not in excluded})
                return JSONResponse(result, status_code=202 if operation in {'tokenizer-start', 'adaptation-start', 'runtime-start', 'pair-start'} else 200)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, AOSFault, SchemaError, ValidationError):
                raise HTTPException(409, 'Episode source or review changed; no training authorized') from None

    @app.post('/api/tasks/owned-skill-plan/{operation}')
    async def owned_skill_plan_operation(operation: str, request: Request):
        if operation not in {'bind', 'start', 'discard', 'audit'} or request.query_params:
            raise HTTPException(400, 'Unknown planned skill operation')
        value = await body(request, reject_duplicates=True, limit=2048)
        expected = ({'schema_version', 'candidate_execution_sha256'} if operation == 'audit' else
                    {'schema_version', 'planning_bundle_sha256', 'confirm_plan_sha256',
                     'lease_id', 'generation'})
        if operation == 'start':
            expected |= {'preview_sha256', 'confirm_sha256'}
        if set(value) != expected or value['schema_version'] != '1.4':
            raise HTTPException(400, 'Exact plan-bound request required')
        if any(type(value[key]) is not str or re.fullmatch(r'[a-f0-9]{64}', value[key]) is None
               for key in expected if key.endswith('_sha256')):
            raise HTTPException(422, 'Invalid planned skill pin')
        if operation != 'audit' and (type(value['lease_id']) is not str
                or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['lease_id']) is None
                or type(value['generation']) is not int or value['generation'] < 0):
            raise HTTPException(422, 'Current control identity required')
        if scheduler is None:
            raise HTTPException(409, 'Owned skill planning unavailable')
        async with control_lock:
            try:
                arguments = {key: item for key, item in value.items() if key != 'schema_version'}
                if operation == 'bind':
                    return scheduler.bind_owned_skill_plan(**arguments)
                if operation == 'start':
                    return scheduler.start_owned_skill_plan(**arguments)
                if operation == 'discard':
                    if value['planning_bundle_sha256'] != value['confirm_plan_sha256']:
                        raise ValueError('confirmation')
                    control = controller.state()
                    planning = scheduler.owned_skill_plan_status()
                    if (control['lease_id'] != value['lease_id']
                            or control['generation'] != value['generation']
                            or planning.get('bundle_sha256') != value['planning_bundle_sha256']
                            or planning['status'] not in {'ready', 'bound'}):
                        raise ValueError('planning_selection_changed')
                    await scheduler.cancel_owned_skill_plan()
                    return scheduler.owned_skill_plan_status()
                result = scheduler.audit_owned_form_candidate_execution(value['candidate_execution_sha256'])
                if (result.get('schema_version') != '1.4'
                        or result.get('available') is True
                        and result.get('planning_admission_verified') is not True):
                    raise ValueError('planned_execution_audit_invalid')
                return result
            except (ValueError, TypeError, KeyError, OSError, AOSFault):
                raise HTTPException(409, 'Plan, control or source changed; no automatic fallback') from None

    @app.get('/api/tasks/owned-form-invocation-audit')
    async def owned_form_invocation_audit(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Owned invocation audit is session configured')
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, 'Owned invocation audit does not accept a request body')
        mode = ('owned_synthetic_form_recipe' if owned_form_recipe_mode else None)

        def unavailable(status='unavailable'):
            result = {'available': False, 'status': status,
                      'report': None, 'report_sha256': None}
            if mode is not None:
                result['mode'] = mode
            return result

        if scheduler is None:
            return unavailable()
        async with control_lock:
            try:
                result = scheduler.audit_owned_form_invocation()
            except AOSFault:
                raise
            except Exception:
                return unavailable()
        expected_keys = ({'mode', 'available', 'status', 'report', 'report_sha256'}
                         if mode is not None else
                         {'available', 'status', 'report', 'report_sha256'})
        if not isinstance(result, dict) or set(result) != expected_keys:
            return unavailable()
        if mode is not None and result.get('mode') != mode:
            return unavailable()
        if (result['available'] is False and result['status'] in {'not_ready', 'unavailable'}
                and result['report'] is None and result['report_sha256'] is None):
            return result
        if (result['available'] is not True or result['status'] != 'verified'
                or not isinstance(result['report'], dict)
                or not isinstance(result['report_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', result['report_sha256']) is None):
            return unavailable()
        try:
            validator('site_skill_form_recipe_audit' if mode is not None
                      else 'site_skill_form_invocation_audit').validate(result['report'])
            report_sha256 = digest(result['report'])
        except Exception:
            return unavailable()
        if report_sha256 != result['report_sha256']:
            return unavailable()
        return result

    def owned_form_candidate_unavailable(reason='unavailable'):
        return {'schema_version': '1.0', 'available': False,
                'reason': reason}

    def owned_form_candidate_summary(candidate, checksum, context):
        from .site_skill_form_recipe_candidate import _candidate_summary

        summary = _candidate_summary(candidate, checksum)
        summary.update({
            'candidate_schema_version': candidate['schema_version'],
            'source_run_ref': candidate['source_run_ref'],
            'profile_sha256': candidate['profile_sha256'],
            'invocation_sha256': candidate['source_context']['invocation_sha256'],
        })
        if (summary['candidate_sha256'] != checksum
                or summary['candidate_schema_version'] != '1.1'
                or summary['source_run_ref'] != context['source_run_ref']
                or summary['profile_sha256'] != context['profile_sha256']
                or summary['invocation_sha256'] != context['invocation_sha256']):
            raise ValueError('owned_recipe_candidate_response_binding_changed')
        return summary

    @app.get('/api/tasks/owned-form-candidate/context')
    async def owned_form_candidate_context(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Owned candidate context is session configured')
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, 'Owned candidate context does not accept a body')
        if scheduler is None:
            return owned_form_candidate_unavailable()
        async with control_lock:
            try:
                result = scheduler.owned_form_candidate_context()
            except AOSFault:
                raise
            except Exception:
                return owned_form_candidate_unavailable()
        if (not isinstance(result, dict) or result.get('schema_version') != '1.0'
                or result.get('available') is not True):
            if (isinstance(result, dict) and set(result) == {
                    'schema_version', 'available', 'reason'}
                    and result.get('schema_version') == '1.0'
                    and result.get('available') is False
                    and result.get('reason') in {'not_ready', 'unavailable'}):
                return result
            return owned_form_candidate_unavailable()
        try:
            from .site_skill_form_recipe_candidate import SiteSkillFormRecipeCandidateAnnotation

            if (set(result) != {'schema_version', 'available', 'mode', 'lifecycle',
                                'source_run_ref', 'invocation_sha256', 'profile_sha256',
                                'task_sha256', 'page_draft_sha256', 'task_key',
                                'form_fields', 'annotation_seed'}
                    or result['mode'] != 'owned_synthetic_form_invocation'
                    or result['lifecycle'] != 'audited'
                    or any(re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                           for key in ('source_run_ref', 'invocation_sha256',
                                       'profile_sha256', 'task_sha256', 'page_draft_sha256'))
                    or not isinstance(result['form_fields'], list)
                    or not result['form_fields']
                    or any(not isinstance(item, str) for item in result['form_fields'])):
                return owned_form_candidate_unavailable()
            seed = SiteSkillFormRecipeCandidateAnnotation.model_validate_json(
                json.dumps(result['annotation_seed']))
            if seed.page_draft_sha256 != result['page_draft_sha256'] or seed.task_key != result['task_key']:
                return owned_form_candidate_unavailable()
            result['annotation_seed'] = seed.model_dump(mode='json')
            return result
        except Exception:
            return owned_form_candidate_unavailable()

    async def owned_candidate_operation(request: Request, operation: str):
        if request.query_params:
            raise HTTPException(400, 'Owned candidate selection belongs in the request body')
        selection = await body(request, reject_duplicates=True, limit=16384)
        common = {'schema_version', 'source_run_ref', 'invocation_sha256'}
        expected = (common | {'annotation'} if operation == 'preview' else
                    common | {'annotation', 'confirm_sha256'} if operation == 'publish' else
                    common | {'candidate_sha256'})
        if set(selection) != expected or selection.get('schema_version') != '1.0':
            raise HTTPException(400, 'Invalid owned candidate request')
        for key in ('source_run_ref', 'invocation_sha256'):
            if (not isinstance(selection[key], str)
                    or re.fullmatch(r'[a-f0-9]{64}', selection[key]) is None):
                raise HTTPException(422, 'Invalid owned candidate source pin')
        if operation == 'publish' and (
                not isinstance(selection['confirm_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['confirm_sha256']) is None):
            raise HTTPException(422, 'Exact candidate confirmation required')
        if operation == 'inspect' and (
                not isinstance(selection['candidate_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['candidate_sha256']) is None):
            raise HTTPException(422, 'Candidate hash required')
        annotation = None
        if operation in {'preview', 'publish'}:
            try:
                from .site_skill_form_recipe_candidate import SiteSkillFormRecipeCandidateAnnotation

                annotation = SiteSkillFormRecipeCandidateAnnotation.model_validate_json(
                    json.dumps(selection['annotation']))
            except (TypeError, ValueError):
                raise HTTPException(422, 'Invalid semantic candidate annotation') from None
        if scheduler is None:
            raise HTTPException(409, 'Owned candidate source unavailable')
        async with control_lock:
            try:
                if operation == 'inspect':
                    session = scheduler.remote_form_owned_candidate_session
                    if (session is None or scheduler.busy or scheduler.reserved
                            or not isinstance(scheduler.remote_form_owned_manifest, dict)
                            or scheduler.remote_form_owned_manifest.get('mode')
                            != 'owned_synthetic_form_invocation'):
                        raise ValueError('owned_recipe_candidate_not_configured_or_idle')
                    candidate, checksum = await asyncio.to_thread(
                        session.reinspect, selection['source_run_ref'],
                        selection['invocation_sha256'], selection['candidate_sha256'])
                    context = {'source_run_ref': candidate['source_run_ref'],
                               'profile_sha256': candidate['profile_sha256'],
                               'invocation_sha256': candidate['source_context']['invocation_sha256']}
                    status, persisted = 'reinspected', True
                else:
                    context = scheduler.owned_form_candidate_context()
                    if (context.get('available') is not True
                            or context.get('source_run_ref') != selection['source_run_ref']
                            or context.get('invocation_sha256') != selection['invocation_sha256']):
                        raise ValueError('owned_recipe_candidate_source_pin_changed')
                    if operation == 'preview':
                        session, run_id = scheduler._owned_form_candidate_run(
                            selection['source_run_ref'], selection['invocation_sha256'])
                        candidate, checksum = await asyncio.to_thread(
                            session.preview, run_id, selection['invocation_sha256'], annotation)
                        status, persisted = 'preview', False
                    else:
                        session, run_id = scheduler._owned_form_candidate_run(
                            selection['source_run_ref'], selection['invocation_sha256'])
                        candidate, checksum = await asyncio.to_thread(
                            session.publish, run_id, selection['invocation_sha256'],
                            annotation, selection['confirm_sha256'])
                        status, persisted = 'published', True
                summary = owned_form_candidate_summary(candidate, checksum, context)
                return {'schema_version': '1.0', 'available': True, 'status': status,
                        'persisted': persisted, 'candidate_sha256': checksum,
                        'summary': summary}
            except AOSFault:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                    RecursionError, AttributeError):
                raise HTTPException(409, 'Owned candidate source changed or unavailable') from None

    @app.post('/api/tasks/owned-form-candidate/preview')
    async def owned_form_candidate_preview(request: Request):
        return await owned_candidate_operation(request, 'preview')

    @app.post('/api/tasks/owned-form-candidate/publish')
    async def owned_form_candidate_publish(request: Request):
        return await owned_candidate_operation(request, 'publish')

    @app.post('/api/tasks/owned-form-candidate/inspect')
    async def owned_form_candidate_inspect(request: Request):
        return await owned_candidate_operation(request, 'inspect')

    async def owned_candidate_execution_operation(request: Request, operation: str):
        if request.query_params:
            raise HTTPException(400, 'Candidate execution selection belongs in the request body')
        selection = await body(request, reject_duplicates=True, limit=4096)
        common = {'schema_version', 'candidate_sha256', 'source_run_ref',
                  'source_invocation_sha256', 'case_key', 'development_value'}
        expected = ({'candidate_execution_sha256'} if operation == 'audit' else
                    common | {'preview_sha256', 'confirm_sha256', 'lease_id', 'generation'}
                    if operation == 'start' else common)
        reviewed = operation != 'audit' and 'review_sha256' in selection
        release_keys = {'release_sha256', 'selection_sha256'}
        released = operation != 'audit' and bool(release_keys & set(selection))
        reused = operation != 'audit' and 'reuse_admission_sha256' in selection
        version = ('1.3' if reused else '1.2' if released else
                   '1.1' if reviewed else '1.0')
        if reviewed:
            expected.add('review_sha256')
        if released:
            expected.update(release_keys | {'review_sha256'})
        if reused:
            expected.add('reuse_admission_sha256')
            if not released or not reviewed:
                raise HTTPException(400, 'Reuse requires reviewed selected release')
        if set(selection) != expected or operation != 'audit' and selection['schema_version'] != version:
            raise HTTPException(400, 'Invalid candidate execution request')
        hashes = ({'candidate_execution_sha256'} if operation == 'audit' else
                  {'candidate_sha256', 'source_run_ref', 'source_invocation_sha256'}
                  | ({'preview_sha256', 'confirm_sha256'} if operation == 'start' else set()))
        if reviewed:
            hashes.add('review_sha256')
        if released:
            hashes.update(release_keys | {'review_sha256'})
        if reused:
            hashes.add('reuse_admission_sha256')
        review_arguments = {'review_sha256': selection['review_sha256']} if reviewed else {}
        if released:
            review_arguments.update({key: selection[key] for key in release_keys})
        if reused:
            review_arguments['reuse_admission_sha256'] = selection[
                'reuse_admission_sha256']
        if any(not isinstance(selection[key], str) or re.fullmatch(r'[a-f0-9]{64}', selection[key]) is None
               for key in hashes):
            raise HTTPException(422, 'Invalid candidate execution pin')
        if operation != 'audit':
            from .owned_form_candidate_execution import validate_candidate_case_input

            try:
                validate_candidate_case_input(selection['case_key'], selection['development_value'])
            except (ValueError, TypeError):
                raise HTTPException(422, 'Invalid synthetic development input') from None
        if operation == 'start' and (not isinstance(selection['lease_id'], str)
                or not 1 <= len(selection['lease_id']) <= 128
                or type(selection['generation']) is not int or selection['generation'] < 0):
            raise HTTPException(422, 'Current control identity required')
        if scheduler is None:
            raise HTTPException(409, 'Owned candidate execution unavailable')
        async with control_lock:
            try:
                if operation == 'preview':
                    result = scheduler.preview_owned_form_candidate_execution(
                        selection['candidate_sha256'], selection['source_run_ref'],
                        selection['source_invocation_sha256'], selection['case_key'],
                        selection['development_value'], **review_arguments)
                    keys = {'schema_version', 'available', 'status', 'preview_sha256', 'candidate_sha256',
                            'source_run_ref', 'source_invocation_sha256', 'source_group_sha256',
                            'profile_sha256', 'skill_sha256', 'case_key', 'parameter_variant_sha256',
                            'form_plan_sha256', 'state_plan_sha256', 'invocation_sha256', 'recipe_sha256',
                            'steps', 'purpose', 'independent_held_out', 'report'}
                    keys.update(review_arguments)
                    if (not isinstance(result, dict) or set(result) != keys
                            or result['schema_version'] != version or result['available'] is not True
                            or any(result[key] != value for key, value in review_arguments.items())
                            or result['status'] != 'preview' or result['purpose'] != 'development_variation'
                            or result['independent_held_out'] is not False or result['report'] is not None
                            or any(result[key] != selection[key] for key in (
                                'candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'case_key'))
                            or any(not isinstance(result[key], str) or re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                                   for key in keys if key.endswith('_sha256') or key == 'source_run_ref')
                            or not isinstance(result['steps'], list) or len(result['steps']) != 6):
                        raise ValueError('candidate_execution_preview_invalid')
                    from .site_skill_form_recipe import SiteSkillFormRecipeStep, SUPPORTED_RECIPE_ORDERS

                    steps = [SiteSkillFormRecipeStep.model_validate(step) for step in result['steps']]
                    if (tuple(step.operation for step in steps) != SUPPORTED_RECIPE_ORDERS[0]
                            or len({step.step_key for step in steps}) != 6
                            or digest({key: value for key, value in result.items() if key != 'preview_sha256'})
                            != result['preview_sha256']):
                        raise ValueError('candidate_execution_preview_binding_changed')
                    return result
                if operation == 'start':
                    result = scheduler.start_owned_form_candidate_execution(
                        candidate_sha256=selection['candidate_sha256'], source_run_ref=selection['source_run_ref'],
                        invocation_sha256=selection['source_invocation_sha256'], case_key=selection['case_key'],
                        development_value=selection['development_value'], preview_sha256=selection['preview_sha256'],
                        confirm_sha256=selection['confirm_sha256'], lease_id=selection['lease_id'],
                        generation=selection['generation'], **review_arguments)
                    keys = {'schema_version', 'accepted', 'lifecycle', 'candidate_execution_sha256',
                            'job_id', 'run_id', 'candidate_sha256', 'case_key', 'invocation_sha256'}
                    keys.update(review_arguments)
                    if (not isinstance(result, dict) or set(result) != keys
                            or result['schema_version'] != version or result['accepted'] is not True
                            or any(result[key] != value for key, value in review_arguments.items())
                            or result['lifecycle'] != 'running' or not isinstance(result['job_id'], str)
                            or not result['job_id'] or result['candidate_sha256'] != selection['candidate_sha256']
                            or result['case_key'] != selection['case_key']
                            or any(not isinstance(result[key], str) or re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                                   for key in ('candidate_execution_sha256', 'invocation_sha256'))):
                        raise ValueError('candidate_execution_start_invalid')
                    return result
                result = scheduler.audit_owned_form_candidate_execution(selection['candidate_execution_sha256'])
                if (not isinstance(result, dict) or result.get('schema_version') not in {'1.0', '1.1', '1.2', '1.3', '1.5'}
                        or result.get('mode') != 'owned_candidate_development'):
                    raise ValueError('candidate_execution_audit_invalid')
                adapter_keys = {'adapter_admission_sha256', 'adapter_admission_verified',
                                'verification_scope', 'current_source_status', 'runtime_reuse_authorized'}
                if result['schema_version'] == '1.5':
                    if (not adapter_keys <= set(result)
                            or result['adapter_admission_verified'] is not result.get('available')
                            or result['verification_scope'] != 'historical_execution'
                            or result['current_source_status'] != 'unchecked'
                            or result['runtime_reuse_authorized'] is not False
                            or type(result['adapter_admission_sha256']) is not str
                            or re.fullmatch(r'[a-f0-9]{64}', result['adapter_admission_sha256']) is None):
                        raise ValueError('candidate_adapter_audit_invalid')
                if result.get('available') is False:
                    if result.get('status') not in {'not_ready', 'unavailable'}:
                        raise ValueError('candidate_execution_audit_invalid')
                    if result.get('schema_version') in {'1.3', '1.5'}:
                        unavailable_keys = {
                            'review_sha256', 'review_admission_verified', 'review_status',
                            'release_sha256', 'selection_sha256', 'family_sha256',
                            'release_admission_verified', 'selection_status',
                            'reuse_admission_sha256', 'reuse_admission_verified'}
                        if result['schema_version'] == '1.5':
                            unavailable_keys |= adapter_keys
                        if (not unavailable_keys <= set(result)
                                or result.get('review_admission_verified') is not False
                                or result.get('release_admission_verified') is not False
                                or result.get('reuse_admission_verified') is not False
                                or result.get('review_status') not in {'accepted', 'revoked', 'unavailable'}
                                or result.get('selection_status') not in {'unavailable', 'current', 'superseded'}
                                or any(not isinstance(result.get(key), str)
                                       or re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                                       for key in ('review_sha256', 'release_sha256',
                                                   'selection_sha256', 'family_sha256',
                                                   'reuse_admission_sha256'))):
                            raise ValueError('candidate_reuse_audit_invalid')
                        return {'schema_version': result['schema_version'], 'available': False,
                                'mode': 'owned_candidate_development',
                                'status': result['status'], 'report': None,
                                'report_sha256': None,
                                **{key: result[key] for key in unavailable_keys}}
                    return {'schema_version': '1.0', 'available': False, 'mode': 'owned_candidate_development',
                            'status': result['status'], 'report': None, 'report_sha256': None}
                report = result.get('report')
                if not validator('site_skill_form_recipe_candidate_execution').is_valid(report):
                    raise ValueError('candidate_execution_audit_report_invalid')
                keys = {'schema_version', 'available', 'mode', 'status', 'candidate_execution_sha256',
                        'candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'source_group_sha256',
                        'profile_sha256', 'skill_sha256', 'case_key', 'parameter_variant_sha256', 'run_id', 'run_ref',
                        'invocation_sha256', 'recipe_sha256', 'report_sha256', 'report'}
                if result['schema_version'] in {'1.1', '1.2'}:
                    keys.update({'review_sha256', 'review_status', 'review_admission_verified'})
                    if (result.get('review_status') not in {'accepted', 'revoked', 'unavailable'}
                            or result.get('review_admission_verified') is not True):
                        raise ValueError('candidate_review_admission_invalid')
                if result['schema_version'] == '1.2':
                    keys.update({'release_sha256', 'selection_sha256', 'family_sha256',
                                 'release_admission_verified', 'selection_status'})
                    if (result.get('release_admission_verified') is not True
                            or result.get('selection_status') not in {'current', 'superseded', 'unavailable'}):
                        raise ValueError('candidate_release_admission_invalid')
                if result['schema_version'] in {'1.3', '1.5'}:
                    keys.update({'review_sha256', 'review_status',
                                 'review_admission_verified',
                                 'release_sha256', 'selection_sha256', 'family_sha256',
                                 'release_admission_verified', 'selection_status',
                                 'reuse_admission_sha256', 'reuse_admission_verified'})
                    if (result.get('release_admission_verified') is not True
                            or result.get('review_admission_verified') is not True
                            or result.get('reuse_admission_verified') is not True
                            or result.get('review_status') not in {'accepted', 'revoked', 'unavailable'}
                            or result.get('selection_status') not in {'current', 'superseded', 'unavailable'}):
                        raise ValueError('candidate_reuse_admission_invalid')
                if result['schema_version'] == '1.5':
                    keys |= adapter_keys
                if (not keys <= set(result) or set(result) - keys - {'steps', 'job_id'}
                        or result['available'] is not True or result['status'] != 'verified'
                        or any(not isinstance(result[key], str) or re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                               for key in keys if key.endswith('_sha256') or key in {'source_run_ref', 'run_ref'})
                        or not isinstance(result['case_key'], str)
                        or re.fullmatch(r'[a-z][a-z0-9-]{0,63}', result['case_key']) is None
                        or not isinstance(result['run_id'], str) or not 1 <= len(result['run_id']) <= 128
                        or result['candidate_execution_sha256'] != selection['candidate_execution_sha256']
                        or result['report_sha256'] != digest(report)
                        or result['run_ref'] != digest({'run_id': result['run_id']})
                        or result['run_ref'] != report['execution_run_ref']
                        or any(result[key] != report['admission'][key] for key in (
                            'candidate_sha256', 'source_run_ref', 'source_group_sha256', 'profile_sha256',
                            'skill_sha256', 'parameter_variant_sha256', 'invocation_sha256', 'recipe_sha256'))):
                    raise ValueError('candidate_execution_audit_binding_changed')
                return {key: result[key] for key in keys}
            except AOSFault:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, AttributeError):
                raise HTTPException(409, 'Candidate execution source or authority changed') from None

    @app.post('/api/tasks/owned-form-candidate/execution-preview')
    async def owned_candidate_execution_preview(request: Request):
        return await owned_candidate_execution_operation(request, 'preview')

    @app.post('/api/tasks/owned-form-candidate/execution-start')
    async def owned_candidate_execution_start(request: Request):
        return await owned_candidate_execution_operation(request, 'start')

    @app.post('/api/tasks/owned-form-candidate/execution-audit')
    async def owned_candidate_execution_audit(request: Request):
        return await owned_candidate_execution_operation(request, 'audit')

    async def owned_candidate_review_operation(request: Request, operation: str):
        if request.query_params:
            raise HTTPException(400, 'Review selection belongs in the request body')
        selection = await body(request, reject_duplicates=True, limit=4096)
        source_keys = {'candidate_sha256', 'source_run_ref', 'source_invocation_sha256',
                       'candidate_execution_sha256'}
        expected = (source_keys if operation == 'preview' else
                    source_keys | {'review_sha256', 'confirm_sha256'} if operation == 'accept' else
                    {'review_sha256', 'confirm_sha256'} if operation == 'revoke' else {'review_sha256'})
        if set(selection) != expected:
            raise HTTPException(400, 'Invalid candidate review request')
        if any(not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{64}', value) is None
               for value in selection.values()):
            raise HTTPException(422, 'Invalid candidate review pin')
        if operation in {'accept', 'revoke'} and selection['review_sha256'] != selection['confirm_sha256']:
            raise HTTPException(409, 'Exact candidate review confirmation required')
        if scheduler is None:
            raise HTTPException(409, 'Candidate review unavailable')
        async with control_lock:
            try:
                callback = getattr(scheduler, {
                    'preview': 'preview_owned_candidate_review',
                    'accept': 'accept_owned_candidate_review',
                    'inspect': 'inspect_owned_candidate_review',
                    'revoke': 'revoke_owned_candidate_review',
                }[operation])
                result = callback(**selection)
                keys = {'schema_version', 'available', 'status', 'review_sha256',
                        'receipt', 'summary', 'revocation_sha256'}
                allowed = ({'preview'} if operation == 'preview' else {'accepted'} if operation == 'accept'
                           else {'revoked'} if operation == 'revoke' else {'accepted', 'revoked'})
                if (not isinstance(result, dict) or set(result) != keys
                        or result['schema_version'] != '1.0' or result['available'] is not True
                        or result['status'] not in allowed
                        or not validator('owned_candidate_review_receipt').is_valid(result['receipt'])
                        or result['review_sha256'] != digest(result['receipt'])
                        or 'review_sha256' in selection and result['review_sha256'] != selection['review_sha256']
                        or any(result['receipt'][key] != selection[key] for key in source_keys & set(selection))):
                    raise ValueError('candidate_review_response_invalid')
                summary = result['summary']
                if (not isinstance(summary, dict) or set(summary) != {
                        'skill_key', 'expected_outcome_key', 'parameter_key', 'form_field_name', 'steps'}
                        or summary != result['receipt'].get('summary')
                        or any(not isinstance(summary[key], str)
                               or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', summary[key]) is None
                               for key in ('skill_key', 'expected_outcome_key', 'parameter_key'))
                        or not isinstance(summary['form_field_name'], str)
                        or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', summary['form_field_name']) is None
                        or not isinstance(summary['steps'], list) or len(summary['steps']) != 6):
                    raise ValueError('candidate_review_summary_invalid')
                from .site_skill_form_recipe import SiteSkillFormRecipeStep, SUPPORTED_RECIPE_ORDERS

                steps = [SiteSkillFormRecipeStep.model_validate(step) for step in summary['steps']]
                if (tuple(step.operation for step in steps) != SUPPORTED_RECIPE_ORDERS[0]
                        or len({step.step_key for step in steps}) != 6):
                    raise ValueError('candidate_review_steps_invalid')
                revoked = result['revocation_sha256']
                if (result['status'] == 'revoked' and (not isinstance(revoked, str)
                        or re.fullmatch(r'[a-f0-9]{64}', revoked) is None)
                        or result['status'] != 'revoked' and revoked is not None):
                    raise ValueError('candidate_review_revocation_invalid')
                return result
            except AOSFault:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, AttributeError):
                raise HTTPException(409, 'Candidate review source or confirmation changed') from None

    @app.post('/api/tasks/owned-form-candidate/review-preview')
    async def owned_candidate_review_preview(request: Request):
        return await owned_candidate_review_operation(request, 'preview')

    @app.post('/api/tasks/owned-form-candidate/review-accept')
    async def owned_candidate_review_accept(request: Request):
        return await owned_candidate_review_operation(request, 'accept')

    @app.post('/api/tasks/owned-form-candidate/review-inspect')
    async def owned_candidate_review_inspect(request: Request):
        return await owned_candidate_review_operation(request, 'inspect')

    @app.post('/api/tasks/owned-form-candidate/review-revoke')
    async def owned_candidate_review_revoke(request: Request):
        return await owned_candidate_review_operation(request, 'revoke')

    def checked_owned_release(value, checksum):
        if (not validator('owned_skill_release').is_valid(value)
                or digest(value) != checksum or digest(value['family']) != value['family_sha256']):
            raise ValueError('owned_release_response_invalid')
        return value

    @app.get('/api/tasks/owned-form-candidate/release-catalog')
    async def owned_release_catalog(request: Request):
        if request.query_params or await request.body():
            raise HTTPException(400, 'Release catalog scope is server configured')
        if scheduler is None:
            raise HTTPException(409, 'Owned release catalog unavailable')
        async with control_lock:
            try:
                result = scheduler.catalog_owned_skill_releases()
                if (not isinstance(result, dict) or set(result) != {'schema_version', 'available', 'families'}
                        or result['schema_version'] != '1.0' or result['available'] is not True
                        or not isinstance(result['families'], list) or len(result['families']) > 64):
                    raise ValueError('owned_release_catalog_invalid')
                families = set()
                for family in result['families']:
                    if (not isinstance(family, dict) or set(family) != {'family_sha256', 'family',
                            'selection_sha256', 'selected_release_sha256', 'sequence', 'releases'}
                            or digest(family['family']) != family['family_sha256']
                            or family['family_sha256'] in families
                            or type(family['sequence']) is not int or not 0 <= family['sequence'] <= 1024
                            or not isinstance(family['releases'], list) or not 1 <= len(family['releases']) <= 64):
                        raise ValueError('owned_release_family_invalid')
                    families.add(family['family_sha256'])
                    releases = set()
                    for item in family['releases']:
                        if (not isinstance(item, dict) or set(item) != {'release_sha256', 'release',
                                'review_status', 'rollback_eligible'}
                                or item['review_status'] not in {'accepted', 'revoked', 'unavailable'}
                                or type(item['rollback_eligible']) is not bool
                                or item['rollback_eligible'] and item['review_status'] != 'accepted'
                                or item['release_sha256'] in releases):
                            raise ValueError('owned_release_catalog_entry_invalid')
                        release = checked_owned_release(item['release'], item['release_sha256'])
                        if release['family'] != family['family'] or release['family_sha256'] != family['family_sha256']:
                            raise ValueError('owned_release_family_changed')
                        releases.add(item['release_sha256'])
                    if family['sequence'] == 0:
                        if family['selection_sha256'] is not None or family['selected_release_sha256'] is not None:
                            raise ValueError('owned_release_genesis_invalid')
                    elif (not isinstance(family['selection_sha256'], str)
                            or re.fullmatch(r'[a-f0-9]{64}', family['selection_sha256']) is None
                            or family['selected_release_sha256'] not in releases):
                        raise ValueError('owned_release_selection_invalid')
                return result
            except AOSFault:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, AttributeError):
                raise HTTPException(409, 'Owned release catalog changed or unavailable') from None

    async def owned_release_operation(request: Request, operation: str):
        if request.query_params:
            raise HTTPException(400, 'Release selection belongs in the request body')
        selection = await body(request, reject_duplicates=True, limit=4096)
        release_operation = operation in {'release-preview', 'release-publish'}
        commit = operation in {'release-publish', 'selection-commit'}
        expected = ({'schema_version', 'review_sha256', 'parent_release_sha256'} if release_operation else
                    {'schema_version', 'release_sha256', 'expected_selection_sha256', 'operation'})
        if commit:
            expected.add('confirm_sha256')
        if set(selection) != expected or selection['schema_version'] != '1.0':
            raise HTTPException(400, 'Invalid owned release request')
        nullable = 'parent_release_sha256' if release_operation else 'expected_selection_sha256'
        if any(not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{64}', value) is None
               for key, value in selection.items() if key.endswith('_sha256')
               and not (key == nullable and value is None)):
            raise HTTPException(422, 'Invalid owned release pin')
        if not release_operation and selection['operation'] not in {'select', 'rollback'}:
            raise HTTPException(422, 'Invalid development selection operation')
        if scheduler is None:
            raise HTTPException(409, 'Owned release selection unavailable')
        async with control_lock:
            try:
                callback = getattr(scheduler, {
                    'release-preview': 'preview_owned_skill_release',
                    'release-publish': 'publish_owned_skill_release',
                    'selection-preview': 'preview_owned_skill_selection',
                    'selection-commit': 'commit_owned_skill_selection',
                }[operation])
                result = callback(**{key: value for key, value in selection.items() if key != 'schema_version'})
                common = {'schema_version', 'available', 'status', 'persisted'}
                keys = (common | {'release_sha256', 'release'} if release_operation else
                        common | {'selection_sha256', 'selection', 'release_sha256', 'review_sha256', 'family_sha256'})
                status = ('published' if release_operation else 'selected') if commit else 'preview'
                if (not isinstance(result, dict) or set(result) != keys or result['schema_version'] != '1.0'
                        or result['available'] is not True or result['persisted'] is not commit or result['status'] != status
                        or any(not isinstance(result[key], str) or re.fullmatch(r'[a-f0-9]{64}', result[key]) is None
                               for key in keys if key.endswith('_sha256'))):
                    raise ValueError('owned_release_response_invalid')
                if release_operation:
                    release = checked_owned_release(result['release'], result['release_sha256'])
                    if (release['review_sha256'] != selection['review_sha256']
                            or release['parent_release_sha256'] != selection['parent_release_sha256']):
                        raise ValueError('owned_release_preview_binding_changed')
                else:
                    event = result['selection']
                    if (not validator('owned_skill_selection').is_valid(event)
                            or digest(event) != result['selection_sha256']
                            or event['previous_selection_sha256'] != selection['expected_selection_sha256']
                            or event['operation'] != selection['operation']
                            or event['release_sha256'] != selection['release_sha256']
                            or event['release_sha256'] != result['release_sha256']
                            or event['family_sha256'] != result['family_sha256']):
                        raise ValueError('owned_selection_preview_binding_changed')
                checksum = result['release_sha256' if release_operation else 'selection_sha256']
                if commit and checksum != selection['confirm_sha256']:
                    raise ValueError('owned_release_confirmation_changed')
                return result
            except AOSFault:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, AttributeError):
                raise HTTPException(409, 'Owned release source, selection or confirmation changed') from None

    @app.post('/api/tasks/owned-form-candidate/release-preview')
    async def owned_release_preview(request: Request):
        return await owned_release_operation(request, 'release-preview')

    @app.post('/api/tasks/owned-form-candidate/release-publish')
    async def owned_release_publish(request: Request):
        return await owned_release_operation(request, 'release-publish')

    @app.post('/api/tasks/owned-form-candidate/selection-preview')
    async def owned_selection_preview(request: Request):
        return await owned_release_operation(request, 'selection-preview')

    @app.post('/api/tasks/owned-form-candidate/selection-commit')
    async def owned_selection_commit(request: Request):
        return await owned_release_operation(request, 'selection-commit')

    @app.post('/api/shared/drain')
    @app.post('/api/shared/drain/seal')
    async def drain_shared_admission(request: Request):
        from .scientist_transport import ScientistAdmissionError

        value = await body(request, reject_duplicates=True)
        try:
            if request.query_params:
                raise ValueError('Shared drain binding belongs in the request body')
            selection = SharedDrainRequest.model_validate(value, strict=True)
        except ValueError:
            raise HTTPException(400, 'Exact shared drain request and generation required') from None
        async with control_lock:
            if shared_drain is None:
                raise HTTPException(409, 'Shared scheduler and Scientist service are unavailable')
            try:
                if request.url.path == '/api/shared/drain/seal':
                    return shared_drain.seal(selection).model_dump(mode='json')
                return shared_drain.persist_observation(selection).model_dump(mode='json')
            except (ValueError, OSError, sqlite3.Error, ScientistAdmissionError):
                raise HTTPException(409, 'Shared drain binding or durable observation is unproven') from None

    @app.get('/api/shared/drain/receipts/{event_id}')
    async def shared_drain_receipt(event_id: str, request: Request):
        if request.query_params:
            raise HTTPException(400, 'Shared drain receipt accepts no query parameters')
        if shared_drain is None:
            raise HTTPException(409, 'Shared scheduler and Scientist service are unavailable')
        try:
            return shared_drain.read_receipt(event_id).model_dump(mode='json')
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(404, 'Shared drain receipt unavailable') from None

    @app.post('/api/restart/quiesce')
    async def restart_quiesce(request: Request):
        value = await body(request, reject_duplicates=True)
        if (request.query_params or set(value) != {'session_id'}
                or not isinstance(value['session_id'], str)
                or re.fullmatch(r'desktop-session-[a-f0-9]{32}', value['session_id']) is None):
            raise HTTPException(400, 'Exact desktop session required')
        async with control_lock:
            if scheduler is None:
                raise HTTPException(409, 'Scheduler unavailable')
            return scheduler.quiesce_for_restart(value['session_id'])

    @app.post('/api/restart/release')
    async def restart_release(request: Request):
        value = await body(request, reject_duplicates=True)
        if (request.query_params or set(value) != {'session_id'}
                or not isinstance(value['session_id'], str)
                or re.fullmatch(r'desktop-session-[a-f0-9]{32}', value['session_id']) is None):
            raise HTTPException(400, 'Exact desktop session required')
        async with control_lock:
            if scheduler is None:
                raise HTTPException(409, 'Scheduler unavailable')
            return scheduler.release_restart_quiesce(value['session_id'])

    @app.post('/api/tasks/navigation-graph')
    async def task_navigation_graph(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Navigation graph selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        if (scheduler is None or scheduler.remote_routes_plan is None
                or set(selection) != {'before_run_id', 'after_run_id'}
                or any(not isinstance(selection[key], str)
                       or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', selection[key]) is None
                       for key in ('before_run_id', 'after_run_id'))
                or selection['before_run_id'] == selection['after_run_id']):
            raise HTTPException(404, 'Navigation graph unavailable')
        selected = (selection['before_run_id'], selection['after_run_id'])
        rows = scheduler.store.connection.execute(
            "SELECT run_id,status FROM desktop_tasks WHERE session_id=? "
            "AND kind='browser_remote_routes' AND run_id IN (?,?)",
            (controller.session_id, *selected)).fetchall()
        if (len(rows) != 2 or {row['run_id'] for row in rows} != set(selected)
                or any(row['status'] != 'succeeded' for row in rows)):
            raise HTTPException(404, 'Navigation graph unavailable')
        def read_graph():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Navigation graph audit already running')
            try:
                return preview_remote_navigation_graph(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()))
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(read_graph)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Navigation graph source changed or unavailable') from None

    @app.get('/api/tasks/skill-drafts')
    async def task_skill_drafts(request: Request):
        parameters = list(request.query_params.multi_items())
        if parameters and parameters not in ([('model_role', 'system1')], [('model_role', 'system2')]):
            raise HTTPException(400, 'Skill draft inventory is server configured')
        model_role = parameters[0][1] if parameters else 'system1'
        if (scheduler is None or scheduler.remote_routes_plan is None
                or scheduler.remote_entry_profile_sha256 is None):
            raise HTTPException(404, 'Skill drafts unavailable')
        def list_drafts():
            store = SiteSkillStore(site_skills, scheduler.remote_entry_profiles,
                                   SiteKnowledgeStore(page_knowledge, scheduler.remote_entry_profiles))
            return store.list(profile_sha256=scheduler.remote_entry_profile_sha256,
                              model_role=model_role)
        try:
            return await asyncio.to_thread(list_drafts)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Skill drafts changed or unavailable') from None

    @app.post('/api/tasks/skill-source-inspect')
    async def task_skill_source_inspect(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Skill source selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        if (scheduler is None or scheduler.remote_routes_plan is None
                or set(selection) != {'run_id', 'route_index', 'skill_sha256'}
                or not isinstance(selection['run_id'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', selection['run_id']) is None
                or type(selection['route_index']) is not int
                or not 0 <= selection['route_index'] < len(scheduler.remote_routes_plan.routes)
                or not isinstance(selection['skill_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['skill_sha256']) is None):
            raise HTTPException(404, 'Skill source unavailable')
        row = scheduler.store.connection.execute(
            "SELECT status FROM desktop_tasks WHERE session_id=? "
            "AND kind='browser_remote_routes' AND run_id=?",
            (controller.session_id, selection['run_id'])).fetchone()
        if row is None or row['status'] != 'succeeded':
            raise HTTPException(404, 'Skill source unavailable')
        def inspect_source():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Route audit already running')
            try:
                return inspect_remote_site_skill_sources(
                    scheduler.settings.database, selection['run_id'],
                    profiles=scheduler.remote_entry_profiles.root,
                    pages=page_knowledge, skills=site_skills,
                    skill_sha256=selection['skill_sha256'],
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()),
                    route_index=selection['route_index'])
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(inspect_source)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise HTTPException(409, 'Skill source changed or unavailable') from None

    def selected_page_runs(selection: dict, keys: set[str]):
        if (scheduler is None or scheduler.remote_routes_plan is None
                or set(selection) != keys
                or any(not isinstance(selection[key], str)
                       or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', selection[key]) is None
                       for key in ('before_run_id', 'after_run_id'))
                or selection['before_run_id'] == selection['after_run_id']
                or type(selection['route_index']) is not int
                or not 0 <= selection['route_index'] < len(scheduler.remote_routes_plan.routes)
                or not isinstance(selection['page_key'], str)
                or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', selection['page_key']) is None):
            raise HTTPException(404, 'Page draft source unavailable')
        selected = (selection['before_run_id'], selection['after_run_id'])
        rows = scheduler.store.connection.execute(
            "SELECT run_id,status FROM desktop_tasks WHERE session_id=? "
            "AND kind='browser_remote_routes' AND run_id IN (?,?)",
            (controller.session_id, *selected)).fetchall()
        if (len(rows) != 2 or {row['run_id'] for row in rows} != set(selected)
                or any(row['status'] != 'succeeded' for row in rows)):
            raise HTTPException(404, 'Page draft source unavailable')
        return selected

    @app.post('/api/tasks/page-draft-seed')
    async def task_page_draft_seed(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Page draft selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_page_runs(selection, {
            'before_run_id', 'after_run_id', 'route_index', 'page_key'})
        def save_seed():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Route audit already running')
            try:
                page, report = seed_remote_page_draft(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root, store=page_knowledge,
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()),
                    route_index=selection['route_index'], page_key=selection['page_key'])
                output = private_seed_output(private_seeds, report['profile_sha256'],
                                             report['page_key'])
                write_private_draft(output, page)
                return report
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(save_seed)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Page draft seed source changed or unavailable') from None

    @app.post('/api/tasks/page-draft-register')
    async def task_page_draft_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Page draft confirmation belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_page_runs(selection, {
            'before_run_id', 'after_run_id', 'route_index', 'page_key', 'confirm_sha256'})
        if (not isinstance(selection['confirm_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['confirm_sha256']) is None):
            raise HTTPException(404, 'Page draft confirmation unavailable')
        def register_seed():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Route audit already running')
            try:
                return register_remote_page_draft(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root, store=page_knowledge,
                    seed_root=private_seeds,
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()),
                    route_index=selection['route_index'], page_key=selection['page_key'],
                    confirm_sha256=selection['confirm_sha256'])
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(register_seed)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Page draft source changed or unavailable') from None

    @app.post('/api/tasks/page-knowledge-preview')
    async def task_page_knowledge_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Knowledge selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_page_runs(selection, {
            'before_run_id', 'after_run_id', 'route_index', 'page_key', 'knowledge_sha256'})
        if (not isinstance(selection['knowledge_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['knowledge_sha256']) is None):
            raise HTTPException(404, 'Knowledge draft unavailable')
        def preview_candidate():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Route audit already running')
            try:
                candidate = preview_remote_route_knowledge(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root, store=page_knowledge,
                    knowledge_sha256=selection['knowledge_sha256'],
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()),
                    route_index=selection['route_index'])
                if candidate['page_key'] != selection['page_key']:
                    raise ValueError('knowledge_page_key_mismatch')
                report = RemoteRouteKnowledgeCandidatePreview(
                    candidate_sha256=digest(candidate), candidate=candidate).model_dump()
                validator('remote_route_knowledge_ui_preview').validate(report)
                return report
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(preview_candidate)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Knowledge candidate source changed or unavailable') from None

    @app.post('/api/tasks/page-knowledge-review')
    async def task_page_knowledge_review(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Knowledge review belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_page_runs(selection, {
            'before_run_id', 'after_run_id', 'route_index', 'page_key',
            'knowledge_sha256', 'confirm_candidate_sha256', 'acknowledge_metadata_only'})
        if (not isinstance(selection['knowledge_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['knowledge_sha256']) is None
                or not isinstance(selection['confirm_candidate_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['confirm_candidate_sha256']) is None
                or selection['acknowledge_metadata_only'] is not True):
            raise HTTPException(404, 'Knowledge review confirmation unavailable')
        def record_review():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Route audit already running')
            try:
                page = SiteKnowledgeStore(page_knowledge, scheduler.remote_entry_profiles).get(
                    selection['knowledge_sha256'])
                if page.page_key != selection['page_key']:
                    raise ValueError('knowledge_page_key_mismatch')
                return register_remote_route_knowledge_review(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    site_store=page_knowledge, review_store=route_reviews,
                    knowledge_sha256=selection['knowledge_sha256'],
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_routes_plan.model_dump()),
                    route_index=selection['route_index'],
                    confirm_candidate_sha256=selection['confirm_candidate_sha256'],
                    acknowledge_metadata_only=True)
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(record_review)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Knowledge review source changed or unavailable') from None

    def selected_json_runs(selection: dict, keys: set[str]):
        if (scheduler is None
                or not isinstance(scheduler.remote_static_assets_plan, WebReadOnlyDataBundlePlan)
                or scheduler.remote_entry_profiles is None
                or scheduler.remote_entry_profile_sha256 is None
                or set(selection) != keys
                or any(not isinstance(selection[key], str)
                       or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', selection[key]) is None
                       for key in ('before_run_id', 'after_run_id'))
                or selection['before_run_id'] == selection['after_run_id']
                or ('knowledge_sha256' in keys and (
                    not isinstance(selection['knowledge_sha256'], str)
                    or re.fullmatch(r'[a-f0-9]{64}', selection['knowledge_sha256']) is None))
                or ('page_key' in keys and (
                    not isinstance(selection['page_key'], str)
                    or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', selection['page_key']) is None))):
            raise HTTPException(404, 'JSON knowledge source unavailable')
        selected = (selection['before_run_id'], selection['after_run_id'])
        rows = scheduler.store.connection.execute(
            "SELECT run_id,status FROM desktop_tasks WHERE session_id=? "
            "AND kind='browser_remote_static_assets' AND run_id IN (?,?)",
            (controller.session_id, *selected)).fetchall()
        if (len(rows) != 2 or {row['run_id'] for row in rows} != set(selected)
                or any(row['status'] != 'succeeded' for row in rows)):
            raise HTTPException(404, 'JSON knowledge source unavailable')
        return selected

    @app.post('/api/tasks/json-page-draft-seed')
    async def task_json_page_draft_seed(request: Request):
        if request.query_params:
            raise HTTPException(400, 'JSON page draft selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_json_runs(selection, {
            'before_run_id', 'after_run_id', 'page_key'})
        def save_seed():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Knowledge audit already running')
            try:
                page, report = seed_remote_readonly_data_page_draft(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    store=page_knowledge,
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_static_assets_plan.model_dump()),
                    page_key=selection['page_key'])
                output = private_seed_output(json_page_seeds, report['profile_sha256'],
                                             report['page_key'])
                write_private_draft(output, page)
                return report
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(save_seed)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'JSON page draft source changed or unavailable') from None

    @app.post('/api/tasks/json-page-draft-register')
    async def task_json_page_draft_register(request: Request):
        if request.query_params:
            raise HTTPException(400, 'JSON page draft confirmation belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_json_runs(selection, {
            'before_run_id', 'after_run_id', 'page_key', 'confirm_sha256'})
        if (not isinstance(selection['confirm_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['confirm_sha256']) is None):
            raise HTTPException(404, 'JSON page draft confirmation unavailable')
        def register_seed():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Knowledge audit already running')
            try:
                return register_remote_readonly_data_page_draft(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    store=page_knowledge, seed_root=json_page_seeds,
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_static_assets_plan.model_dump()),
                    page_key=selection['page_key'],
                    confirm_sha256=selection['confirm_sha256'])
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(register_seed)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'JSON page draft source changed or unavailable') from None

    @app.post('/api/tasks/json-knowledge-preview')
    async def task_json_knowledge_preview(request: Request):
        if request.query_params:
            raise HTTPException(400, 'JSON knowledge selection belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_json_runs(selection, {
            'before_run_id', 'after_run_id', 'knowledge_sha256'})
        def preview_candidate():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Knowledge audit already running')
            try:
                candidate = preview_remote_readonly_data_knowledge(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    store=page_knowledge,
                    knowledge_sha256=selection['knowledge_sha256'],
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_static_assets_plan.model_dump()))
                report = RemoteReadonlyDataKnowledgeCandidatePreview(
                    candidate_sha256=digest(candidate), candidate=candidate).model_dump()
                validator('remote_readonly_data_knowledge_ui_preview').validate(report)
                return report
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(preview_candidate)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'JSON knowledge candidate changed or unavailable') from None

    @app.post('/api/tasks/json-knowledge-review')
    async def task_json_knowledge_review(request: Request):
        if request.query_params:
            raise HTTPException(400, 'JSON knowledge review belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        selected = selected_json_runs(selection, {
            'before_run_id', 'after_run_id', 'knowledge_sha256',
            'confirm_candidate_sha256', 'acknowledge_metadata_only'})
        if (not isinstance(selection['confirm_candidate_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['confirm_candidate_sha256']) is None
                or selection['acknowledge_metadata_only'] is not True):
            raise HTTPException(404, 'JSON knowledge review confirmation unavailable')
        def record_review():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Knowledge audit already running')
            try:
                return register_remote_readonly_data_knowledge_review(
                    scheduler.settings.database, *selected,
                    profiles=scheduler.remote_entry_profiles.root,
                    site_store=page_knowledge, review_store=json_reviews,
                    knowledge_sha256=selection['knowledge_sha256'],
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_static_assets_plan.model_dump()),
                    confirm_candidate_sha256=selection['confirm_candidate_sha256'],
                    acknowledge_metadata_only=True)
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(record_review)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'JSON knowledge review source changed or unavailable') from None

    @app.post('/api/tasks/json-knowledge-recheck')
    async def task_json_knowledge_recheck(request: Request):
        if request.query_params:
            raise HTTPException(400, 'JSON knowledge recheck belongs in the request body')
        selection = await body(request, reject_duplicates=True)
        if (scheduler is None
                or not isinstance(scheduler.remote_static_assets_plan, WebReadOnlyDataBundlePlan)
                or set(selection) != {'current_run_id', 'review_sha256'}
                or not isinstance(selection['current_run_id'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', selection['current_run_id']) is None
                or not isinstance(selection['review_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', selection['review_sha256']) is None):
            raise HTTPException(404, 'JSON knowledge recheck unavailable')
        row = scheduler.store.connection.execute(
            "SELECT status FROM desktop_tasks WHERE session_id=? "
            "AND kind='browser_remote_static_assets' AND run_id=?",
            (controller.session_id, selection['current_run_id'])).fetchone()
        if row is None or row['status'] != 'succeeded':
            raise HTTPException(404, 'JSON knowledge recheck unavailable')
        def recheck():
            if not navigation_graph_lock.acquire(blocking=False):
                raise HTTPException(429, 'Knowledge audit already running')
            try:
                return recheck_remote_readonly_data_knowledge(
                    scheduler.settings.database, selection['current_run_id'],
                    profiles=scheduler.remote_entry_profiles.root,
                    site_store=page_knowledge, review_store=json_reviews,
                    review_sha256=selection['review_sha256'],
                    selected_profile_sha256=scheduler.remote_entry_profile_sha256,
                    selected_plan_sha256=digest(scheduler.remote_static_assets_plan.model_dump()))
            finally:
                navigation_graph_lock.release()
        try:
            return await asyncio.to_thread(recheck)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'JSON knowledge recheck source changed or unavailable') from None

    @app.get('/api/tasks/learning/{job_id}')
    async def task_learning(job_id: str):
        if scheduler is None or re.fullmatch(r'job-[a-f0-9]{32}', job_id) is None:
            raise HTTPException(404, 'Learning status unavailable')
        try:
            return scheduler.learning_status(job_id)
        except ValueError:
            raise HTTPException(404, 'Learning status unavailable') from None

    @app.post('/api/tasks/remote-learning/consent')
    async def prepare_remote_learning_consent(request: Request):
        value = await body(request, reject_duplicates=True)
        required = {'job_id', 'roles', 'expires_at', 'attest_data_rights'}
        confirmation = value.get('confirm_sha256')
        if (request.query_params or set(value) not in (required, required | {'confirm_sha256'})
                or not isinstance(value['job_id'], str)
                or re.fullmatch(r'job-[a-f0-9]{32}', value['job_id']) is None
                or not isinstance(value['roles'], list)
                or not value['roles'] or len(value['roles']) > 2
                or any(not isinstance(role, str) or role not in ('system1', 'system2')
                       for role in value['roles'])
                or value['roles'] != sorted(set(value['roles']))
                or not isinstance(value['expires_at'], str)
                or len(value['expires_at']) > 40
                or value['attest_data_rights'] is not True
                or ('confirm_sha256' in value and (not isinstance(confirmation, str)
                    or re.fullmatch(r'[a-f0-9]{64}', confirmation) is None))):
            raise HTTPException(400, 'Only exact local metadata consent fields are accepted')
        if scheduler is None or control_lock.locked():
            raise HTTPException(409, 'Remote learning unavailable')
        try:
            return scheduler.prepare_remote_learning_consent(
                value['job_id'], roles=value['roles'], expires_at=value['expires_at'],
                attest_data_rights=True, confirm_sha256=confirmation)
        except (AOSFault, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Remote learning consent or source unavailable') from None

    @app.post('/api/tasks/remote-learning')
    async def attach_remote_learning(request: Request):
        value = await body(request, reject_duplicates=True)
        if (request.query_params or set(value) != {'job_id', 'consent_sha256'}
                or not isinstance(value['job_id'], str)
                or re.fullmatch(r'job-[a-f0-9]{32}', value['job_id']) is None
                or not isinstance(value['consent_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', value['consent_sha256']) is None):
            raise HTTPException(400, 'Only an exact active job and consent hash are accepted')
        if scheduler is None or control_lock.locked():
            raise HTTPException(409, 'Remote learning unavailable')
        try:
            return scheduler.attach_remote_learning(value['job_id'], value['consent_sha256'])
        except (AOSFault, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(409, 'Remote learning consent or source unavailable') from None

    @app.get('/api/recovery')
    async def recovery(request: Request):
        if request.query_params:
            raise HTTPException(400, 'Recovery source and limits are server configured')
        if recovery_database is None:
            raise HTTPException(503, 'Recovery inventory unavailable')
        def read_inventory():
            if not recovery_lock.acquire(blocking=False):
                raise HTTPException(429, 'Recovery inventory already running')
            try:
                return recovery_inventory(recovery_database).model_dump()
            finally:
                recovery_lock.release()
        try:
            return await asyncio.to_thread(read_inventory)
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(503, 'Recovery inventory unavailable') from None

    @app.get('/api/sequences')
    async def sequences():
        return scheduler.sequences.status() if scheduler else {'reserved': False, 'sequence': None}

    @app.post('/api/sequences')
    async def start_sequence(request: Request):
        value = await body(request)
        try:
            if request.query_params:
                raise ValueError
            admission = SequenceStart.model_validate(value)
        except ValueError:
            raise HTTPException(400, 'Only an ordered fixed task plan and current lease are accepted') from None
        if scheduler is None or control_lock.locked():
            raise HTTPException(409, 'Scheduler unavailable')
        return scheduler.sequences.start(admission)

    @app.post('/api/tasks')
    async def start_task(request: Request):
        value = await body(request)
        required = {'kind', 'lease_id', 'generation'}
        if (not required <= set(value) <= required | {'approve_all', 'learning_metadata'}
                or value['kind'] not in ('hello', 'browser_form', 'vision_canvas', 'browser_local_navigation',
                                         'browser_staging_workflow', 'browser_remote_entry',
                                         'browser_remote_routes', 'browser_remote_static_assets',
                                         'browser_remote_form')
                or not isinstance(value['lease_id'], str) or type(value['generation']) is not int
                or type(value.get('approve_all', False)) is not bool
                or type(value.get('learning_metadata', False)) is not bool):
            raise HTTPException(400, 'Only a bounded task kind and current lease are accepted')
        if scheduler is None or control_lock.locked():
            raise HTTPException(409, 'Scheduler unavailable during control change or not configured')
        try:
            return scheduler.start(value['lease_id'], value['generation'], value['kind'],
                                   approve_all=value.get('approve_all', False),
                                   learning_metadata=value.get('learning_metadata', False))
        except ScientistAdmissionError:
            return JSONResponse({'detail': 'Scientist admission denied; trusted reconciliation required',
                                 'code': 'reconciliation_required'}, status_code=409)

    @app.post('/api/tasks/preview')
    async def task_preview(request: Request):
        value = await body(request)
        if set(value) != {'goal'}:
            raise HTTPException(400, 'Only bounded goal text is accepted')
        try:
            return preview_goal(value['goal']).model_dump()
        except ValueError:
            raise HTTPException(400, 'Invalid bounded goal text') from None

    @app.post('/api/tasks/plan')
    async def task_plan(request: Request):
        value = await body(request)
        if request.query_params or set(value) != {'goal'}:
            raise HTTPException(400, 'Only bounded goal text is accepted')
        try:
            return preview_plan(value['goal']).model_dump()
        except ValueError:
            raise HTTPException(400, 'Invalid bounded goal text') from None

    @app.post('/api/tasks/compound-plan')
    async def compound_plan(request: Request):
        value = await body(request)
        if request.query_params or set(value) != {'goal'}:
            raise HTTPException(400, 'Only bounded goal text is accepted')
        try:
            return preview_goal_plan(value['goal']).model_dump()
        except ValueError:
            raise HTTPException(400, 'Invalid bounded goal text') from None

    @app.post('/api/tasks/compound-start')
    async def compound_start(request: Request):
        value = await body(request)
        try:
            if request.query_params:
                raise ValueError
            admission = CompoundSequenceStart.model_validate(value)
        except ValueError:
            raise HTTPException(400, 'Only a bounded goal, exact preview hashes and current lease are accepted') from None
        try:
            sequence_request = admission.sequence_start()
        except ValueError:
            raise HTTPException(409, 'Compound goal or catalog no longer matches a supported sequence') from None
        if scheduler is None or control_lock.locked():
            raise HTTPException(409, 'Scheduler unavailable')
        return scheduler.sequences.start(sequence_request)

    @app.post('/api/approvals/{approval_id}')
    async def approval(approval_id: str, request: Request):
        value = await body(request)
        if set(value) != {'action_sha256', 'accept'} or not isinstance(value['action_sha256'], str) or type(value['accept']) is not bool:
            raise HTTPException(400, 'Invalid approval response')
        if scheduler is None or scheduler.restart_quiesced or control_lock.locked():
            raise HTTPException(409, 'Scheduler unavailable')
        return scheduler.respond(approval_id, **value)

    @app.get('/api/scientist/jobs')
    async def scientist_jobs():
        from .scientist_inventory import scientist_inference_inventory

        inference = scientist_inference_inventory(controller, scheduler)
        if scientist_lab is None:
            return {'configured': False, 'joint_runtime_admitted': False, 'jobs': [], 'inference': inference}
        return {**scientist_lab.inventory(), 'inference': inference}

    @app.post('/api/scientist/{operation}')
    async def scientist_operation(operation: str, request: Request):
        from .scientist_lab import ScientistLabBudget, ScientistLabUncertain
        from .scientist_transport import ScientistAdmissionError

        if scientist_lab is None or control_lock.locked():
            raise HTTPException(409, 'Scientist integration is not configured or control is changing')
        if request.query_params:
            raise HTTPException(400, 'No Scientist query parameters are permitted')
        value = await body(request, reject_duplicates=True)
        if control_lock.locked():
            raise HTTPException(409, 'Scientist control is changing')
        try:
            for key in ('action_id', 'envelope_sha256', 'run_id', 'suite', 'track', 'program_version', 'readback_id'):
                if key in value and (type(value[key]) is not str or not 1 <= len(value[key]) <= 128):
                    raise ValueError('Invalid Scientist identifier')
            if 'accept' in value and type(value['accept']) is not bool:
                raise ValueError('Invalid Scientist approval decision')
            if operation == 'propose' and set(value) == {'suite', 'track', 'budget', 'program_version'}:
                value['budget'] = ScientistLabBudget.model_validate(value['budget'], strict=True)
                return scientist_lab.propose(**value)
            if operation == 'approve' and set(value) == {'action_id', 'envelope_sha256', 'accept'}:
                return scientist_lab.respond(value['action_id'], envelope_sha256=value['envelope_sha256'], accept=value['accept'])
            if operation == 'execute' and set(value) == {'action_id'}:
                return await scientist_lab.execute_async(value['action_id'])
            if operation == 'stop' and set(value) == {'run_id'}:
                return scientist_lab.propose_stop(value['run_id'])
            if operation in {'status', 'report'} and set(value) == {'run_id'}:
                return await scientist_lab.read_async(value['run_id'], 'lab.' + operation)
            if operation == 'save_report' and set(value) == {'run_id', 'expected_report_sha256'}:
                if (type(value['expected_report_sha256']) is not str
                        or re.fullmatch('[a-f0-9]{64}', value['expected_report_sha256']) is None):
                    raise ValueError('Invalid exact Scientist report hash')
                return await scientist_lab.save_report_async(value['run_id'],
                    expected_report_sha256=value['expected_report_sha256'])
            if operation == 'read_saved_report' and set(value) == {'run_id', 'readback_id'}:
                return scientist_lab.read_saved_report(value['run_id'], value['readback_id'])
            raise HTTPException(400, 'Unknown Scientist operation or arguments')
        except ScientistLabUncertain:
            raise HTTPException(409, 'Scientist effect is uncertain; inspect the durable intent, do not retry') from None
        except ScientistAdmissionError:
            raise HTTPException(409, 'Scientist authority, approval or durable job binding is not admitted') from None
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid bounded Scientist request') from None

    @app.post('/api/input')
    async def enqueue(request: Request):
        value = await body(request)
        if set(value) != {'lease_id', 'generation'} or not isinstance(value['lease_id'], str) or type(value['generation']) is not int:
            raise HTTPException(400, 'Invalid lease')
        if scheduler and (scheduler.reserved or scheduler.restart_quiesced):
            raise HTTPException(409, 'Desktop input unavailable')
        return {'input_id': controller.enqueue(**value)}

    @app.post('/api/input/{input_id}/execute')
    async def execute(input_id: str, request: Request):
        if await body(request) != {}:
            raise HTTPException(400, 'No arguments permitted')
        if scheduler and (scheduler.reserved or scheduler.restart_quiesced):
            raise HTTPException(409, 'Desktop input unavailable')
        return controller.execute(input_id)

    @app.websocket('/websockify')
    async def vnc(websocket: WebSocket):
        if websocket.headers.get('host') != host or websocket.headers.get('origin') != origin or not authorized(websocket.cookies):
            await websocket.close(code=1008)
            return
        state = controller.state()
        if not controller.runtime.container_id or state['status'] == 'stopped':
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols', []) else None)
        finished = asyncio.Event()
        streams[websocket] = finished
        process = None
        tasks = []
        try:
            process = await asyncio.create_subprocess_exec(*DOCKER, 'exec', '-i', controller.runtime.container_id,
                                                           '/usr/bin/python3', '/opt/aos/bridge.py',
                                                           'control' if state['owner'] == 'HUMAN' else 'view',
                                                           stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.DEVNULL)

            async def output():
                while payload := await process.stdout.read(65536):
                    if controller.state()['lease_id'] != state['lease_id']:
                        break
                    await websocket.send_bytes(payload)

            async def input_frames():
                while True:
                    payload = await websocket.receive_bytes()
                    if len(payload) > 65536 or controller.state()['lease_id'] != state['lease_id']:
                        break
                    process.stdin.write(payload)
                    await process.stdin.drain()

            tasks = [asyncio.create_task(output()), asyncio.create_task(input_frames())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            streams.pop(websocket, None)
            finished.set()
            with suppress(Exception):
                await websocket.close()

    app.mount('/novnc', StaticFiles(directory=assets), name='novnc')
    if (ui_root / 'index.html').is_file():
        app.mount('/ui', StaticFiles(directory=ui_root, html=True), name='ui')
    return app
