from .desktop_control import DesktopController
from .scientist_cpu_capability import ScientistCpuCapabilityVerifier, ScientistCpuReviewedGrant
from .scientist_lab import ScientistLabClient, _deny_authority, _deny_effect
from .scientist_lab_service import ScientistLabService, prepare_scientist_lab_startup
from .scientist_transport import ScientistAdmissionError
from .storage import TrajectoryStore


def prepare_scientist_cpu_startup(config, grant: ScientistCpuReviewedGrant):
    """Validate an explicit trusted CPU composition before desktop creation; no remote requests."""
    if not isinstance(grant, ScientistCpuReviewedGrant):
        raise ScientistAdmissionError('CPU startup requires a typed reviewed grant')
    grant = ScientistCpuReviewedGrant.model_validate(grant.model_dump(by_alias=True), strict=True)
    config, client = prepare_scientist_lab_startup(config)
    if (config.allowed_suites != frozenset({grant.capability.suite_id})
            or config.program_version != grant.capability.program_version
            or config.authorization_context_sha256 != grant.authorization_context_sha256):
        raise ScientistAdmissionError('CPU startup configuration differs from the exact reviewed grant')
    ScientistCpuCapabilityVerifier(client, grant)
    return config, client, grant


def create_scientist_cpu_service(controller: DesktopController, client: ScientistLabClient,
                                 grant: ScientistCpuReviewedGrant) -> ScientistLabService:
    """Compose a supplied controller and fresh client; no runtime creation, approval or remote call."""
    if (not isinstance(controller, DesktopController) or not isinstance(controller.store, TrajectoryStore)
            or not isinstance(client, ScientistLabClient) or not isinstance(grant, ScientistCpuReviewedGrant)):
        raise ScientistAdmissionError('CPU composition requires an existing typed controller, store, client and reviewed grant')
    if (client._closed or client._lock.locked() or client._async_active is not None
            or client.uncertain_action_id is not None or client._attempted
            or client.verify_authority is not _deny_authority or client.authorize_and_persist is not _deny_effect):
        raise ScientistAdmissionError('CPU composition requires a fresh unbound client without active or uncertain controls')
    if client.allowed_suites != frozenset({grant.capability.suite_id}):
        raise ScientistAdmissionError('CPU composition must expose only the exact reviewed CPU suite')
    current = controller.state()
    if (current['session_id'] != controller.session_id or current['runtime_id'] != controller.runtime.runtime_id
            or current['status'] != 'running'):
        raise ScientistAdmissionError('CPU composition controller is not the exact current running session')
    verifier = ScientistCpuCapabilityVerifier(client, grant)
    return ScientistLabService(controller, client,
        authorization_context_sha256=verifier.grant.authorization_context_sha256,
        program_version=verifier.grant.capability.program_version, verify_capability=verifier)
