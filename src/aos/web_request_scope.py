"""Strict origin matching for future web request interception.

This pure check does not grant execution or enforce container network egress.
"""

from urllib.parse import urlsplit

from .web_application import WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding


MAX_REQUEST_URL_BYTES = 8192


def require_request_origin(profiles: WebApplicationProfiles, draft: WebTaskAdmissionDraft,
                           url: str) -> str:
    """Return the exact allowed origin, or reject an ambiguous/out-of-scope URL."""
    checked = verify_web_task_binding(profiles, draft)
    if (not isinstance(url, str) or not url or len(url) > MAX_REQUEST_URL_BYTES
            or any(ord(character) <= 32 or ord(character) >= 127 for character in url)
            or '\\' in url or '#' in url):
        raise ValueError('invalid_web_request_url')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
                or not parsed.path.startswith('/') or parsed.fragment):
            raise ValueError('invalid_web_request_url')
        origin, _local = canonical_origin(parsed.scheme + '://' + parsed.netloc)
    except ValueError as error:
        raise ValueError('invalid_web_request_url') from error
    if origin not in checked.task.allowed_origins:
        raise ValueError('web_request_origin_outside_task')
    return origin
