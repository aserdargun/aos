"""Optional, separately approved state readback around one exact HTTPS form transport."""

import hashlib
import re
import threading
import ssl
from html.parser import HTMLParser
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, digest
from .web_application import Checksum, WebApplicationProfile, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract
from .web_https_form_transport import (ExactHTTPSFormTransport, WebHTTPSFormPlan,
                                       exact_form_fields, form_body,
                                       verify_web_https_form_plan,
                                       verify_web_https_form_target_grant)
from .web_https_preflight import _fetch


class WebHTTPSFormStatePlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_url: str = Field(min_length=1, max_length=2048)
    expected_before_sha256: Checksum
    expected_after_sha256: Checksum
    marker_id: str | None = Field(default=None, pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$',
                                  exclude_if=lambda value: value is None)
    expected_before_marker_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    expected_after_marker_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    submitted_field_name: str | None = Field(default=None, pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$',
                                            exclude_if=lambda value: value is None)
    approval: Literal['fresh_per_action'] = 'fresh_per_action'
    status: Literal['draft'] = 'draft'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('state_url')
    @classmethod
    def canonical_url(cls, value):
        WebApplicationProfile.entry_is_canonical(value)
        return value

    @field_validator('execution_authorized', 'collection_authorized', mode='before')
    @classmethod
    def cannot_authorize(cls, value):
        if value is not False:
            raise ValueError('form_state_plan_cannot_authorize')
        return value

    @model_validator(mode='after')
    def complete_marker(self):
        marker = (self.marker_id, self.expected_before_marker_sha256,
                  self.expected_after_marker_sha256)
        if any(value is None for value in marker) != all(value is None for value in marker):
            raise ValueError('form_state_marker_requires_complete_expectation')
        if self.marker_id is not None and marker[1] == marker[2]:
            raise ValueError('form_state_marker_requires_transition')
        if self.submitted_field_name is not None and self.marker_id is None:
            raise ValueError('form_state_submitted_field_requires_marker')
        return self


class WebHTTPSFormStateReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['bounded_form_declared_state_readback'] = 'bounded_form_declared_state_readback'
    status: Literal['declared_response_transition_observed'] = 'declared_response_transition_observed'
    profile_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    state_url_sha256: Checksum
    receipt_response_sha256: Checksum
    before_response_sha256: Checksum
    after_response_sha256: Checksum
    before_response_bytes: int = Field(ge=0, le=65536)
    after_response_bytes: int = Field(ge=0, le=65536)
    marker_id_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    before_marker_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    after_marker_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    declared_marker_transition_observed: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    submitted_field_name_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    submitted_value_readback_verified: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)
    tls_hostname_verified: Literal[True] = True
    declared_response_transition_observed: Literal[True] = True
    browser_connected: Literal[False] = False
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def complete_marker(self):
        marker = (self.marker_id_sha256, self.before_marker_sha256,
                  self.after_marker_sha256, self.declared_marker_transition_observed)
        if any(value is None for value in marker) != all(value is None for value in marker):
            raise ValueError('form_state_report_incomplete_marker')
        if marker[0] is not None and marker[3] is not True:
            raise ValueError('form_state_report_marker_not_observed')
        if ((self.submitted_field_name_sha256 is None) != (self.submitted_value_readback_verified is None)
                or self.submitted_value_readback_verified is True and marker[0] is None):
            raise ValueError('form_state_report_submitted_value_unbound')
        return self


class _ExactMarkerParser(HTMLParser):
    VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                           'link', 'meta', 'param', 'source', 'track', 'wbr'})

    def __init__(self, marker_id: str):
        super().__init__(convert_charrefs=True)
        self.marker_id = marker_id
        self.stack = []
        self.matches = 0
        self.closed = False
        self.invalid = False
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        ids = [value for name, value in attrs if name == 'id']
        if len(ids) > 1:
            self.invalid = True
        if self.stack and tag not in self.VOID_TAGS:
            self.stack.append(tag)
        if self.marker_id in ids:
            self.matches += 1
            if not self.stack:
                if tag in self.VOID_TAGS:
                    self.invalid = True
                else:
                    self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        ids = [value for name, value in attrs if name == 'id']
        if len(ids) > 1:
            self.invalid = True
        if self.marker_id in ids:
            self.matches += 1
            self.invalid = True

    def handle_endtag(self, tag):
        if self.stack:
            if self.stack[-1] != tag:
                self.invalid = True
                return
            self.stack.pop()
            if not self.stack:
                self.closed = True

    def handle_data(self, data):
        if self.stack:
            self.fragments.append(data)


def form_state_marker_sha256(body: bytes, marker_id: str) -> str:
    if (type(body) is not bytes or not isinstance(marker_id, str)
            or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', marker_id) is None):
        raise ValueError('form_state_invalid_marker')
    try:
        parser = _ExactMarkerParser(marker_id)
        parser.feed(body.decode('utf-8', 'strict'))
        parser.close()
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError('form_state_marker_not_parseable') from error
    text = ' '.join(''.join(parser.fragments).split())
    if (parser.invalid or parser.stack or parser.matches != 1 or not parser.closed
            or not text or len(text) > 512):
        raise ValueError('form_state_marker_not_unique_complete_text')
    return digest({'text': text})


def verify_submitted_field_binding(plan: WebHTTPSFormStatePlan, form_plan: WebHTTPSFormPlan,
                                   fields: tuple[tuple[str, str], ...]) -> None:
    if plan.submitted_field_name is None:
        return
    if not isinstance(fields, tuple) or not 1 <= len(fields) <= 8:
        raise ValueError('form_state_submitted_field_binding_invalid')
    normalized = (exact_form_fields(*fields[0]) if len(fields) == 1 else
                  exact_form_fields(None, None, fields))
    body = form_body(normalized)
    values = [value for name, value in normalized if name == plan.submitted_field_name]
    text = ' '.join(values[0].split()) if len(values) == 1 else ''
    if (hashlib.sha256(body).hexdigest() != form_plan.body_sha256
            or len(body) != form_plan.body_bytes or not text or len(text) > 512
            or digest({'text': text}) != plan.expected_after_marker_sha256):
        raise ValueError('form_state_submitted_field_value_differs_from_after_marker')


def plan_web_https_form_state(transport: ExactHTTPSFormTransport, *, state_url: str,
                              expected_before_sha256: str,
                              expected_after_sha256: str,
                              marker_id: str | None = None,
                              expected_before_marker_sha256: str | None = None,
                              expected_after_marker_sha256: str | None = None,
                              submitted_field_name: str | None = None) -> WebHTTPSFormStatePlan:
    if not isinstance(transport, ExactHTTPSFormTransport):
        raise ValueError('form_state_requires_exact_transport')
    transport.verify_plan_identity()
    verify_web_https_form_plan(transport.profiles, transport.task, transport.plan)
    verify_web_https_form_target_grant(
        transport.plan, transport.public_plan_sha256, transport.owned_form_target)
    plan = WebHTTPSFormStatePlan(
        profile_sha256=transport.plan.profile_sha256,
        task_sha256=transport.plan.task_sha256,
        form_plan_sha256=transport.plan_sha256,
        state_url=state_url,
        expected_before_sha256=expected_before_sha256,
        expected_after_sha256=expected_after_sha256,
        marker_id=marker_id,
        expected_before_marker_sha256=expected_before_marker_sha256,
        expected_after_marker_sha256=expected_after_marker_sha256,
        submitted_field_name=submitted_field_name)
    origin = canonical_origin(transport.plan.entry_url)[0]
    if (canonical_origin(plan.state_url)[0] != origin
            or plan.state_url in (transport.plan.entry_url, transport.plan.submit_url,
                                  transport.plan.receipt_url)
            or plan.expected_before_sha256 == plan.expected_after_sha256
            or transport.task.max_actions < 6):
        raise ValueError('form_state_outside_confirmed_task')
    if transport.owned_form_target is not None:
        if (plan.state_url != transport.owned_form_target.state_url
                or digest(plan.model_dump()) != transport.owned_form_target.state_plan_sha256):
            raise ValueError('owned_form_fixture_state_plan_mismatch')
    return plan


def form_state_request_sha256(plan: WebHTTPSFormStatePlan,
                              phase: Literal['before', 'after']) -> str:
    if phase not in ('before', 'after'):
        raise ValueError('form_state_invalid_phase')
    return digest({'method': 'GET', 'url': plan.state_url, 'phase': phase,
                   'state_plan_sha256': digest(plan.model_dump())})


def form_state_action_arguments(plan: WebHTTPSFormStatePlan,
                                binding_sha256: str,
                                phase: Literal['before', 'after'],
                                cookie_sha256: str | None = None) -> dict:
    if phase not in ('before', 'after'):
        raise ValueError('form_state_invalid_phase')
    arguments = {'profile_sha256': plan.profile_sha256,
            'binding_sha256': binding_sha256,
            'form_plan_sha256': plan.form_plan_sha256,
            'state_plan_sha256': digest(plan.model_dump()),
            'phase': phase,
            'url': plan.state_url,
            'request_sha256': form_state_request_sha256(plan, phase)}
    if plan.marker_id is not None:
        arguments['marker_id_sha256'] = digest({'marker_id': plan.marker_id})
        arguments['expected_marker_sha256'] = (plan.expected_before_marker_sha256
                                                if phase == 'before'
                                                else plan.expected_after_marker_sha256)
    if plan.submitted_field_name is not None:
        arguments['submitted_field_name_sha256'] = digest({'field_name': plan.submitted_field_name})
    if cookie_sha256 is not None:
        if (not isinstance(cookie_sha256, str) or len(cookie_sha256) != 64
                or any(character not in '0123456789abcdef' for character in cookie_sha256)):
            raise ValueError('form_state_invalid_cookie_hash')
        arguments['cookie_sha256'] = cookie_sha256
    return arguments


def verify_web_https_form_state_plan(
        profiles: WebApplicationProfiles, task: WebTaskContract,
        form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan,
        tls_context: ssl.SSLContext | None = None, *,
        confirm_public_form_plan_sha256: str | None = None,
        confirm_public_state_plan_sha256: str | None = None,
        owned_form_target=None) -> WebHTTPSFormStatePlan:
    transport = ExactHTTPSFormTransport(
        profiles, task, form_plan, digest(form_plan.model_dump()),
        consume_approval=lambda _request_sha256: False,
        tls_context=tls_context,
        confirm_public_plan_sha256=confirm_public_form_plan_sha256,
        owned_form_target=owned_form_target)
    probe = ExactHTTPSFormStateProbe(
        transport, state_plan, digest(state_plan.model_dump()),
        consume_approval=lambda _request_sha256: False,
        confirm_public_state_plan_sha256=confirm_public_state_plan_sha256)
    return probe.plan


class ExactHTTPSFormStateProbe:
    def __init__(self, transport: ExactHTTPSFormTransport, plan: WebHTTPSFormStatePlan,
                 confirm_plan_sha256: str, *, consume_approval: Callable[[str], bool],
                 confirm_public_state_plan_sha256: str | None = None):
        if not isinstance(plan, WebHTTPSFormStatePlan):
            raise ValueError('form_state_requires_exact_plan')
        checked = plan_web_https_form_state(
            transport, state_url=plan.state_url,
            expected_before_sha256=plan.expected_before_sha256,
            expected_after_sha256=plan.expected_after_sha256,
            marker_id=plan.marker_id,
            expected_before_marker_sha256=plan.expected_before_marker_sha256,
            expected_after_marker_sha256=plan.expected_after_marker_sha256,
            submitted_field_name=plan.submitted_field_name)
        self.plan_sha256 = digest(checked.model_dump())
        if (checked != plan or confirm_plan_sha256 != self.plan_sha256
                or not callable(consume_approval)):
            raise ValueError('form_state_requires_exact_plan_and_approval_gate')
        self.plan = checked
        self.public_state_plan_sha256 = confirm_public_state_plan_sha256
        self._verify_public_grant()
        self.transport = transport
        self.consume_approval = consume_approval
        self._lock = threading.Lock()
        self._before_attempted = False
        self._before_response = None
        self._before_marker_sha256 = None
        self._after_attempted = False
        self._submitted_field_bound = False

    def bind_submitted_fields(self, fields: tuple[tuple[str, str], ...]) -> None:
        if self.plan.submitted_field_name is None or self._submitted_field_bound:
            raise ValueError('form_state_submitted_field_binding_unavailable')
        verify_submitted_field_binding(self.plan, self.transport.plan, fields)
        self._submitted_field_bound = True

    def observe_before(self, *, confirm_request_sha256: str) -> None:
        request_sha256 = form_state_request_sha256(self.plan, 'before')
        if confirm_request_sha256 != request_sha256:
            raise ValueError('form_state_requires_exact_before_confirmation')
        with self._lock, self.transport._lock:
            if (self._before_attempted or self.transport._entry_response_sha256 is None
                    or self.plan.submitted_field_name is not None and not self._submitted_field_bound
                    or self.transport._submit_attempted):
                raise ValueError('form_state_requires_entry_before_submit')
            self._verify_scope()
            if self.consume_approval(request_sha256) is not True:
                raise ValueError('form_state_before_approval_denied')
            self._before_attempted = True
            report, body = _fetch(self.transport.profiles, self.plan.profile_sha256,
                                   self.plan.profile_sha256, tls_context=self.transport.tls_context,
                                   target_url=self.plan.state_url,
                                   cookie_header=self.transport._cookie_header,
                                   owned_form_target=self.transport.owned_form_target,
                                   **({'owned_form_plan_sha256': self.transport.plan_sha256,
                                       'owned_form_state_plan_sha256': self.plan_sha256}
                                      if self.transport.owned_form_target is not None else {}))
            if report.response_sha256 != self.plan.expected_before_sha256:
                raise ValueError('form_state_unexpected_before_response')
            if self.plan.marker_id is not None:
                marker_sha256 = form_state_marker_sha256(body, self.plan.marker_id)
                if marker_sha256 != self.plan.expected_before_marker_sha256:
                    raise ValueError('form_state_unexpected_before_marker')
                self._before_marker_sha256 = marker_sha256
            self._before_response = report

    def observe_after(self, *, confirm_request_sha256: str) -> WebHTTPSFormStateReport:
        request_sha256 = form_state_request_sha256(self.plan, 'after')
        if confirm_request_sha256 != request_sha256:
            raise ValueError('form_state_requires_exact_after_confirmation')
        with self._lock, self.transport._lock:
            if (self._before_response is None or self._after_attempted
                    or self.transport._receipt_response_sha256 is None):
                raise ValueError('form_state_requires_receipt_after_submit')
            self._verify_scope()
            if self.consume_approval(request_sha256) is not True:
                raise ValueError('form_state_after_approval_denied')
            self._after_attempted = True
            before = self._before_response
            receipt_response_sha256 = self.transport._receipt_response_sha256
        report, body = _fetch(self.transport.profiles, self.plan.profile_sha256,
                               self.plan.profile_sha256, tls_context=self.transport.tls_context,
                               target_url=self.plan.state_url,
                               cookie_header=self.transport._cookie_header,
                               owned_form_target=self.transport.owned_form_target,
                               **({'owned_form_plan_sha256': self.transport.plan_sha256,
                                   'owned_form_state_plan_sha256': self.plan_sha256}
                                  if self.transport.owned_form_target is not None else {}))
        if report.response_sha256 != self.plan.expected_after_sha256:
            raise ValueError('form_state_unexpected_after_response')
        after_marker_sha256 = None
        if self.plan.marker_id is not None:
            after_marker_sha256 = form_state_marker_sha256(body, self.plan.marker_id)
            if after_marker_sha256 != self.plan.expected_after_marker_sha256:
                raise ValueError('form_state_unexpected_after_marker')
        return WebHTTPSFormStateReport(
            profile_sha256=self.plan.profile_sha256, task_sha256=self.plan.task_sha256,
            form_plan_sha256=self.plan.form_plan_sha256,
            state_plan_sha256=self.plan_sha256,
            state_url_sha256=digest({'url': self.plan.state_url}),
            receipt_response_sha256=receipt_response_sha256,
            before_response_sha256=before.response_sha256,
            after_response_sha256=report.response_sha256,
            before_response_bytes=before.response_bytes,
            after_response_bytes=report.response_bytes,
            marker_id_sha256=(digest({'marker_id': self.plan.marker_id})
                              if self.plan.marker_id is not None else None),
            before_marker_sha256=self._before_marker_sha256,
            after_marker_sha256=after_marker_sha256,
            declared_marker_transition_observed=(True if self.plan.marker_id is not None else None),
            submitted_field_name_sha256=(digest({'field_name': self.plan.submitted_field_name})
                                         if self.plan.submitted_field_name is not None else None),
            submitted_value_readback_verified=(True if self.plan.submitted_field_name is not None else None))

    def _verify_scope(self) -> None:
        self._verify_public_grant()
        self.transport.verify_cookie_identity()
        verify_web_https_form_plan(self.transport.profiles, self.transport.task,
                                   self.transport.plan)
        verify_web_https_form_target_grant(self.transport.plan,
                                           self.transport.public_plan_sha256,
                                           self.transport.owned_form_target)
        if plan_web_https_form_state(
                self.transport, state_url=self.plan.state_url,
                expected_before_sha256=self.plan.expected_before_sha256,
                expected_after_sha256=self.plan.expected_after_sha256,
                marker_id=self.plan.marker_id,
                expected_before_marker_sha256=self.plan.expected_before_marker_sha256,
                expected_after_marker_sha256=self.plan.expected_after_marker_sha256,
                submitted_field_name=self.plan.submitted_field_name) != self.plan:
            raise ValueError('form_state_plan_binding_changed')

    def _verify_public_grant(self) -> None:
        synthetic = urlsplit(self.plan.state_url).hostname.endswith('.invalid')
        if ((self.public_state_plan_sha256 is None) != synthetic
                or self.public_state_plan_sha256 is not None
                and self.public_state_plan_sha256 != self.plan_sha256):
            raise ValueError('form_state_public_target_requires_exact_plan_grant')
