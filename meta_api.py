"""
Meta Graph API client for exchanging user tokens and fetching Page tokens.

All HTTP traffic is logged safely (secrets masked) to logs/meta_token_generator.log.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from safe_log import (
    get_logger,
    mask_secret,
    sanitize_dict,
    sanitize_json_text,
    setup_logging,
)

GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

DEFAULT_TIMEOUT = 30

setup_logging()
log = get_logger()


class MetaAPIError(Exception):
    """User-facing Meta / network error (no secrets in the message)."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
        operation: str | None = None,
        endpoint: str | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.http_status = http_status
        self.operation = operation
        self.endpoint = endpoint
        self.response_body = response_body

    def format_for_user(self) -> str:
        lines = ["Meta API Error", ""]
        if self.operation:
            lines.extend([f"Operation:\n{self.operation}", ""])
        if self.http_status is not None:
            lines.extend([f"HTTP Status:\n{self.http_status}", ""])
        if self.error_type:
            lines.extend([f"Type:\n{self.error_type}", ""])
        if self.code is not None:
            lines.extend([f"Code:\n{self.code}", ""])
        if self.subcode is not None:
            lines.extend([f"Subcode:\n{self.subcode}", ""])
        lines.extend([f"Message:\n{self.message}", ""])
        lines.append("See logs/meta_token_generator.log for diagnostic details.")
        return "\n".join(lines)

    def safe_details_for_clipboard(self) -> str:
        return self.format_for_user()


@dataclass
class TokenExchangeResult:
    access_token: str
    token_type: str
    expires_in: int | None

    @property
    def approx_days(self) -> float | None:
        if self.expires_in is None:
            return None
        return round(self.expires_in / 86400, 1)


@dataclass
class FacebookPage:
    id: str
    name: str
    access_token: str
    tasks: list[str] = field(default_factory=list)

    def tasks_display(self) -> str:
        return ", ".join(self.tasks) if self.tasks else "—"


@dataclass
class PagePost:
    id: str
    message: str
    created_time: str
    permalink_url: str
    full_picture: str
    attachments_summary: str


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DiagnosticReport:
    input_ok: bool = False
    user_token_valid: bool | None = None
    me_ok: bool | None = None
    me_name: str = ""
    me_id: str = ""
    perm_pages_show_list: str = "missing"  # granted|declined|missing
    perm_pages_read_engagement: str = "missing"
    exchange_ok: bool | None = None
    exchange_http: int | None = None
    exchange_error_type: str = ""
    exchange_error_code: int | None = None
    exchange_error_subcode: int | None = None
    exchange_error_message: str = ""
    long_lived_generated: bool = False
    long_lived_validated: bool | None = None
    long_lived_expires_in: int | None = None
    pages_ok: bool | None = None
    pages_count: int = 0
    failed_step: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    exchanged: TokenExchangeResult | None = None
    pages: list[FacebookPage] = field(default_factory=list)

    def checklist_text(self) -> str:
        lines: list[str] = []
        for check in self.checks:
            mark = "✓" if check.ok else "✗"
            line = f"[{mark}] {check.name}"
            if check.detail:
                line += f"\n    {check.detail}"
            lines.append(line)
        return "\n".join(lines) if lines else "(no checks yet)"

    def full_report_text(self) -> str:
        def yn(v: bool | None) -> str:
            if v is True:
                return "YES"
            if v is False:
                return "NO"
            return "N/A"

        lines = [
            "=== DIAGNOSTIC REPORT (secrets redacted) ===",
            "",
            "USER TOKEN",
            f"Valid: {yn(self.user_token_valid)}",
            f"/me successful: {yn(self.me_ok)}",
        ]
        if self.me_id or self.me_name:
            lines.append(f"User: id={self.me_id} name={self.me_name}")
        lines.extend(
            [
                "",
                "PERMISSIONS",
                f"pages_show_list: {self.perm_pages_show_list}",
                f"pages_read_engagement: {self.perm_pages_read_engagement}",
                "",
                "TOKEN EXCHANGE",
                f"Success: {yn(self.exchange_ok)}",
                f"HTTP Status: {self.exchange_http if self.exchange_http is not None else 'N/A'}",
                f"Meta Error Type: {self.exchange_error_type or 'N/A'}",
                f"Meta Error Code: {self.exchange_error_code if self.exchange_error_code is not None else 'N/A'}",
                f"Meta Error Subcode: {self.exchange_error_subcode if self.exchange_error_subcode is not None else 'N/A'}",
                f"Meta Message: {self.exchange_error_message or 'N/A'}",
                "",
                "LONG-LIVED TOKEN",
                f"Generated: {yn(self.long_lived_generated)}",
                f"Validated: {yn(self.long_lived_validated)}",
                f"expires_in: {self.long_lived_expires_in if self.long_lived_expires_in is not None else 'N/A'}",
                "",
                "PAGES",
                f"/me/accounts successful: {yn(self.pages_ok)}",
                f"Pages found: {self.pages_count}",
                "",
                f"Failed step: {self.failed_step or 'none'}",
                "",
                "CHECKLIST",
                self.checklist_text(),
                "",
                "See logs/meta_token_generator.log for full request/response traces.",
            ]
        )
        return "\n".join(lines)


def _redact_for_safety(text: str) -> str:
    if len(text) > 400:
        return text[:400] + "…"
    return text


def _endpoint_only(url: str) -> str:
    """Strip query/fragment so secrets in query strings are never logged."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _parse_meta_error_payload(
    payload: dict[str, Any],
    *,
    http_status: int | None,
    operation: str | None,
    endpoint: str | None,
) -> MetaAPIError:
    err = payload.get("error")
    if not isinstance(err, dict):
        return MetaAPIError(
            "Unexpected Meta API response (missing error details).",
            http_status=http_status,
            operation=operation,
            endpoint=endpoint,
            response_body=sanitize_dict(payload),
        )

    message = _redact_for_safety(str(err.get("message") or "Unknown Meta API error."))
    code = err.get("code")
    subcode = err.get("error_subcode")
    error_type = err.get("type")

    code_i = int(code) if isinstance(code, int) or (isinstance(code, str) and str(code).isdigit()) else None
    sub_i = (
        int(subcode)
        if isinstance(subcode, int) or (isinstance(subcode, str) and str(subcode).isdigit())
        else None
    )

    return MetaAPIError(
        message,
        code=code_i,
        subcode=sub_i,
        error_type=str(error_type) if error_type else None,
        http_status=http_status,
        operation=operation,
        endpoint=endpoint,
        response_body=sanitize_dict(payload),
    )


def graph_request(
    method: str,
    path_or_url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    operation: str = "GRAPH_REQUEST",
) -> dict[str, Any]:
    """
    Perform a Graph API request. Secrets only via ``params`` (requests encodes them).
    Never logs full URLs that may contain secrets in the query string.
    """
    params = dict(params or {})

    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"graph.facebook.com", "graph.facebook.net"}:
            raise MetaAPIError(
                "Refusing to follow a non-Meta pagination URL.",
                operation=operation,
            )
        # Absolute paging URLs already include query params — do not re-attach secrets.
        request_params = params if params else None
        endpoint = _endpoint_only(url)
    else:
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        url = f"{GRAPH_API_BASE}{path}"
        request_params = params
        endpoint = url

    safe_params = sanitize_dict(request_params) if request_params else {}
    log.info(
        "%s starting method=%s endpoint=%s params=%s",
        operation,
        method.upper(),
        endpoint,
        sanitize_json_text(safe_params, max_chars=2000),
    )

    started = time.perf_counter()
    try:
        response = requests.request(
            method.upper(),
            url,
            params=request_params,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        duration = time.perf_counter() - started
        log.error("%s TIMEOUT duration=%.3fs endpoint=%s", operation, duration, endpoint)
        log.debug("%s stack:\n%s", operation, traceback.format_exc())
        raise MetaAPIError(
            "HTTP timeout while contacting Meta Graph API. Check your network and try again.",
            operation=operation,
            endpoint=endpoint,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        duration = time.perf_counter() - started
        log.error("%s CONNECTION_ERROR duration=%.3fs endpoint=%s", operation, duration, endpoint)
        log.debug("%s stack:\n%s", operation, traceback.format_exc())
        raise MetaAPIError(
            "No internet connection or Meta Graph API is unreachable.",
            operation=operation,
            endpoint=endpoint,
        ) from exc
    except requests.exceptions.RequestException as exc:
        duration = time.perf_counter() - started
        log.error(
            "%s REQUEST_EXCEPTION duration=%.3fs type=%s endpoint=%s",
            operation,
            duration,
            exc.__class__.__name__,
            endpoint,
        )
        log.debug("%s stack:\n%s", operation, traceback.format_exc())
        raise MetaAPIError(
            f"Network error while contacting Meta Graph API: {exc.__class__.__name__}",
            operation=operation,
            endpoint=endpoint,
        ) from exc

    duration = time.perf_counter() - started
    status = response.status_code
    level = log.info if response.ok else log.error
    level("%s HTTP status=%s duration=%.3fs endpoint=%s", operation, status, duration, endpoint)

    raw_text = response.text or ""
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        clipped = raw_text[:2000]
        log.error("%s NON_JSON_BODY body=%r", operation, clipped)
        raise MetaAPIError(
            f"Meta returned a non-JSON response (HTTP {status}).",
            http_status=status,
            operation=operation,
            endpoint=endpoint,
            response_body=clipped,
        ) from exc

    if isinstance(payload, (dict, list)):
        log.info("%s META_RESPONSE\n%s", operation, sanitize_json_text(payload))
    else:
        log.info("%s META_RESPONSE (non-object)=%r", operation, str(payload)[:500])

    if not isinstance(payload, dict):
        raise MetaAPIError(
            "Unexpected Meta API response shape (expected a JSON object).",
            http_status=status,
            operation=operation,
            endpoint=endpoint,
            response_body=sanitize_dict(payload) if isinstance(payload, dict) else str(payload)[:500],
        )

    if "error" in payload:
        err = _parse_meta_error_payload(
            payload,
            http_status=status,
            operation=operation,
            endpoint=endpoint,
        )
        log.error(
            "%s META_ERROR code=%s subcode=%s type=%s message=%s",
            operation,
            err.code,
            err.subcode,
            err.error_type,
            err.message,
        )
        raise err

    if not response.ok:
        raise MetaAPIError(
            f"HTTP {status} from Meta Graph API.",
            http_status=status,
            operation=operation,
            endpoint=endpoint,
            response_body=sanitize_dict(payload),
        )

    return payload


def get_me(access_token: str, *, operation: str = "GET_ME") -> dict[str, Any]:
    return graph_request(
        "GET",
        "/me",
        params={"fields": "id,name", "access_token": access_token.strip()},
        operation=operation,
    )


def get_permissions(access_token: str) -> dict[str, str]:
    """Return permission_name -> status (granted|declined|…)."""
    payload = graph_request(
        "GET",
        "/me/permissions",
        params={"access_token": access_token.strip()},
        operation="GET_PERMISSIONS",
    )
    result: dict[str, str] = {}
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            perm = str(item.get("permission") or "")
            status = str(item.get("status") or "unknown")
            if perm:
                result[perm] = status
    return result


def inspect_input_token(app_id: str, app_secret: str, input_token: str) -> dict[str, Any]:
    """Call ``/debug_token``. Returns the ``data`` object."""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    input_token = input_token.strip()
    app_token = f"{app_id}|{app_secret}"
    log.info(
        "DEBUG_TOKEN inspect app_id=%s app_secret=%s input_token=%s",
        mask_secret(app_id),
        mask_secret(app_secret),
        mask_secret(input_token),
    )
    payload = graph_request(
        "GET",
        "/debug_token",
        params={
            "input_token": input_token,
            "access_token": app_token,
        },
        operation="DEBUG_TOKEN",
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MetaAPIError(
            "Could not inspect the Access Token (unexpected debug_token response).",
            operation="DEBUG_TOKEN",
        )
    return data


def exchange_user_token(
    app_id: str,
    app_secret: str,
    short_lived_user_token: str,
    *,
    run_preflight: bool = True,
) -> TokenExchangeResult:
    """Exchange a short-lived User Access Token for a long-lived User Access Token."""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    short_lived_user_token = short_lived_user_token.strip()

    log.info("TOKEN_EXCHANGE starting")
    log.info("TOKEN_EXCHANGE endpoint=%s/oauth/access_token", GRAPH_API_BASE)
    log.info("TOKEN_EXCHANGE app_id=%s", mask_secret(app_id))
    log.info("TOKEN_EXCHANGE app_secret=%s", mask_secret(app_secret))
    log.info("TOKEN_EXCHANGE user_token=%s", mask_secret(short_lived_user_token))
    log.info("TOKEN_EXCHANGE grant_type=fb_exchange_token")

    if not app_id:
        raise MetaAPIError("Meta App ID is required.", operation="TOKEN_EXCHANGE")
    if not app_secret:
        raise MetaAPIError("Meta App Secret is required.", operation="TOKEN_EXCHANGE")
    if not short_lived_user_token:
        raise MetaAPIError("User Access Token is required.", operation="TOKEN_EXCHANGE")

    if run_preflight:
        log.info("PREFLIGHT USER_TOKEN checking via GET /me")
        try:
            me = get_me(short_lived_user_token, operation="PREFLIGHT_ME")
            log.info(
                "PREFLIGHT USER_TOKEN VALID id=%s name=%s",
                me.get("id"),
                me.get("name"),
            )
        except MetaAPIError as exc:
            log.error("PREFLIGHT USER_TOKEN INVALID: %s", exc.message)
            raise MetaAPIError(
                f"Preflight failed: User Access Token is not valid for GET /me.\n\n{exc.message}",
                code=exc.code,
                subcode=exc.subcode,
                error_type=exc.error_type,
                http_status=exc.http_status,
                operation="PREFLIGHT_ME",
                endpoint=exc.endpoint,
                response_body=exc.response_body,
            ) from exc

    # Optional type/app check — do not block exchange if debug_token fails.
    try:
        info = inspect_input_token(app_id, app_secret, short_lived_user_token)
        token_type = str(info.get("type") or "").upper()
        app_id_from_token = str(info.get("app_id") or "")
        is_valid = info.get("is_valid")
        log.info(
            "DEBUG_TOKEN summary type=%s app_id=%s is_valid=%s",
            token_type,
            app_id_from_token,
            is_valid,
        )
        if is_valid is False:
            err = info.get("error")
            detail = ""
            if isinstance(err, dict) and err.get("message"):
                detail = f"\n\nMeta says: {_redact_for_safety(str(err.get('message')))}"
            raise MetaAPIError(
                "This Access Token is not valid (expired or revoked)." + detail,
                operation="DEBUG_TOKEN",
            )
        if token_type == "PAGE":
            raise MetaAPIError(
                "You pasted a Page Access Token. This step needs a User Access Token.",
                operation="DEBUG_TOKEN",
            )
        if app_id_from_token and app_id_from_token != app_id:
            raise MetaAPIError(
                "This Access Token belongs to a different Meta App.\n\n"
                f"Token app_id: {app_id_from_token}\n"
                f"You entered App ID: {app_id}",
                operation="DEBUG_TOKEN",
            )
    except MetaAPIError as exc:
        if exc.operation == "DEBUG_TOKEN" and (
            "Page Access Token" in exc.message
            or "different Meta App" in exc.message
            or "not valid" in exc.message
        ):
            raise
        log.warning(
            "DEBUG_TOKEN skipped/failed (will still attempt exchange): %s",
            exc.message,
        )

    payload = graph_request(
        "GET",
        "/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_user_token,
        },
        operation="TOKEN_EXCHANGE",
    )

    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise MetaAPIError(
            "Token exchange succeeded but no access_token was returned.",
            operation="TOKEN_EXCHANGE",
            response_body=sanitize_dict(payload),
        )

    token_type_out = str(payload.get("token_type") or "bearer")
    expires_raw = payload.get("expires_in")
    expires_in: int | None
    if isinstance(expires_raw, int):
        expires_in = expires_raw
    elif isinstance(expires_raw, str) and expires_raw.isdigit():
        expires_in = int(expires_raw)
    else:
        expires_in = None

    log.info(
        "TOKEN_EXCHANGE success token_type=%s expires_in=%s masked_token=%s",
        token_type_out,
        expires_in,
        mask_secret(token),
    )

    # Post-validate long-lived token.
    try:
        me2 = get_me(token, operation="VALIDATE_LONG_LIVED")
        log.info(
            "LONG_LIVED_TOKEN VALID id=%s name=%s",
            me2.get("id"),
            me2.get("name"),
        )
    except MetaAPIError as exc:
        log.error("LONG_LIVED_TOKEN INVALID after exchange: %s", exc.message)
        raise MetaAPIError(
            "Long-lived token was returned by Meta but failed validation via GET /me.\n\n"
            + exc.message,
            code=exc.code,
            subcode=exc.subcode,
            error_type=exc.error_type,
            http_status=exc.http_status,
            operation="VALIDATE_LONG_LIVED",
            endpoint=exc.endpoint,
            response_body=exc.response_body,
        ) from exc

    return TokenExchangeResult(
        access_token=token,
        token_type=token_type_out,
        expires_in=expires_in,
    )


def fetch_all_pages(
    long_lived_user_token: str,
    *,
    include_access_token_field: bool = True,
) -> list[FacebookPage]:
    """Fetch all Pages from ``/me/accounts`` with pagination."""
    token = long_lived_user_token.strip()
    if not token:
        raise MetaAPIError(
            "Long-lived User Access Token is required to list Pages.",
            operation="GET_PAGES",
        )

    fields = "id,name,access_token,tasks" if include_access_token_field else "id,name,tasks"
    pages: list[FacebookPage] = []
    next_url: str | None = None
    first = True

    while first or next_url:
        if first:
            payload = graph_request(
                "GET",
                "/me/accounts",
                params={
                    "fields": fields,
                    "access_token": token,
                    "limit": 100,
                },
                operation="GET_PAGES",
            )
            first = False
        else:
            assert next_url is not None
            payload = graph_request("GET", next_url, operation="GET_PAGES_PAGE")

        data = payload.get("data")
        if data is None:
            data = []
        if not isinstance(data, list):
            raise MetaAPIError(
                "Unexpected /me/accounts response (data is not a list).",
                operation="GET_PAGES",
            )

        for item in data:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip() or "(unnamed)"
            page_token = item.get("access_token")
            if not page_id:
                continue
            if include_access_token_field and (
                not isinstance(page_token, str) or not page_token
            ):
                continue
            if not isinstance(page_token, str):
                page_token = ""
            tasks_raw = item.get("tasks") or []
            tasks: list[str] = []
            if isinstance(tasks_raw, list):
                tasks = [str(t) for t in tasks_raw if t is not None]
            pages.append(
                FacebookPage(
                    id=page_id,
                    name=name,
                    access_token=page_token,
                    tasks=tasks,
                )
            )

        paging = payload.get("paging")
        next_url = None
        if isinstance(paging, dict):
            nxt = paging.get("next")
            if isinstance(nxt, str) and nxt.startswith("https://"):
                next_url = nxt

    log.info("PAGE_ACCESS OK pages_count=%s", len(pages))
    # Never log page access tokens.
    for p in pages:
        log.info("PAGE summary id=%s name=%s tasks=%s", p.id, p.name, p.tasks_display())

    return pages


def run_full_diagnostics(app_id: str, app_secret: str, user_token: str) -> DiagnosticReport:
    """
    Run the full diagnostic pipeline. Never includes full secrets in the report.
    """
    report = DiagnosticReport()
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    user_token = user_token.strip()

    log.info("=== RUN_DIAGNOSTICS start ===")

    # 1. Input validation
    if app_id and app_secret and user_token:
        report.input_ok = True
        report.checks.append(CheckResult("Input validation", True, "App ID, App Secret, User Token present"))
    else:
        report.input_ok = False
        report.failed_step = "input_validation"
        missing = []
        if not app_id:
            missing.append("App ID")
        if not app_secret:
            missing.append("App Secret")
        if not user_token:
            missing.append("User Access Token")
        report.checks.append(
            CheckResult("Input validation", False, "Missing: " + ", ".join(missing))
        )
        log.error("DIAG input validation failed missing=%s", missing)
        return report

    # 2. GET /me
    try:
        me = get_me(user_token, operation="PREFLIGHT_ME")
        report.me_ok = True
        report.user_token_valid = True
        report.me_id = str(me.get("id") or "")
        report.me_name = str(me.get("name") or "")
        report.checks.append(
            CheckResult(
                "User token valid (/me)",
                True,
                f"id={report.me_id} name={report.me_name}",
            )
        )
        report.checks.append(CheckResult("/me successful", True))
    except MetaAPIError as exc:
        report.me_ok = False
        report.user_token_valid = False
        report.failed_step = "preflight_me"
        report.checks.append(CheckResult("User token valid (/me)", False, exc.message))
        report.checks.append(
            CheckResult(
                "/me successful",
                False,
                f"HTTP {exc.http_status} code={exc.code} {exc.message}",
            )
        )
        log.error("DIAG preflight /me failed")
        return report

    # 3. Permissions
    try:
        perms = get_permissions(user_token)
        report.perm_pages_show_list = perms.get("pages_show_list", "missing")
        report.perm_pages_read_engagement = perms.get("pages_read_engagement", "missing")
        show_ok = report.perm_pages_show_list == "granted"
        eng_ok = report.perm_pages_read_engagement == "granted"
        report.checks.append(
            CheckResult(
                "pages_show_list granted",
                show_ok,
                report.perm_pages_show_list,
            )
        )
        report.checks.append(
            CheckResult(
                "pages_read_engagement granted",
                eng_ok,
                report.perm_pages_read_engagement,
            )
        )
    except MetaAPIError as exc:
        report.checks.append(CheckResult("permissions lookup", False, exc.message))
        log.warning("DIAG permissions lookup failed: %s", exc.message)

    # 4. Exchange
    try:
        exchanged = exchange_user_token(
            app_id,
            app_secret,
            user_token,
            run_preflight=False,  # already did /me
        )
        report.exchange_ok = True
        report.long_lived_generated = True
        report.long_lived_validated = True
        report.long_lived_expires_in = exchanged.expires_in
        report.exchanged = exchanged
        report.checks.append(CheckResult("Long-lived token exchange", True))
        report.checks.append(CheckResult("Long-lived token validated (/me)", True))
    except MetaAPIError as exc:
        report.exchange_ok = False
        report.long_lived_generated = False
        report.long_lived_validated = False
        report.failed_step = "token_exchange"
        report.exchange_http = exc.http_status
        report.exchange_error_type = exc.error_type or ""
        report.exchange_error_code = exc.code
        report.exchange_error_subcode = exc.subcode
        report.exchange_error_message = exc.message
        detail = (
            f"HTTP {exc.http_status}\n"
            f"{exc.error_type or ''}\n"
            f"Code: {exc.code}\n"
            f"Subcode: {exc.subcode}\n"
            f"Message: {exc.message}"
        )
        report.checks.append(CheckResult("Long-lived token exchange", False, detail.strip()))
        log.error("DIAG token exchange failed")
        return report

    # 5. Pages (without requiring page tokens for count — use fields with token for full data)
    try:
        pages = fetch_all_pages(exchanged.access_token)
        report.pages_ok = True
        report.pages_count = len(pages)
        report.pages = pages
        report.checks.append(
            CheckResult("/me/accounts", True, f"pages_count={len(pages)}")
        )
    except MetaAPIError as exc:
        report.pages_ok = False
        report.failed_step = "get_pages"
        report.checks.append(CheckResult("/me/accounts", False, exc.message))
        log.error("DIAG /me/accounts failed")
        return report

    log.info("=== RUN_DIAGNOSTICS complete OK pages=%s ===", report.pages_count)
    return report


def test_page_token(page_id: str, page_access_token: str) -> tuple[str, str]:
    """Validate a Page Access Token. Returns (id, name)."""
    page_id = page_id.strip()
    page_access_token = page_access_token.strip()
    if not page_id:
        raise MetaAPIError("Page ID is required.", operation="TEST_PAGE")
    if not page_access_token:
        raise MetaAPIError("Page Access Token is required.", operation="TEST_PAGE")

    payload = graph_request(
        "GET",
        f"/{page_id}",
        params={
            "fields": "id,name",
            "access_token": page_access_token,
        },
        operation="TEST_PAGE",
    )

    pid = str(payload.get("id") or "")
    name = str(payload.get("name") or "")
    if not pid:
        raise MetaAPIError("Page test returned no id.", operation="TEST_PAGE")
    return pid, name


def _summarize_attachments(attachments: Any) -> str:
    if not isinstance(attachments, dict):
        return "—"
    data = attachments.get("data")
    if not isinstance(data, list) or not data:
        return "—"

    parts: list[str] = []
    for att in data[:5]:
        if not isinstance(att, dict):
            continue
        att_type = str(att.get("type") or "unknown")
        url = str(att.get("url") or "")
        media = att.get("media")
        media_src = ""
        if isinstance(media, dict):
            image = media.get("image")
            if isinstance(image, dict):
                media_src = str(image.get("src") or "")
        bit = att_type
        if url:
            bit += f" → {url}"
        elif media_src:
            bit += f" → {media_src}"
        parts.append(bit)

    return "; ".join(parts) if parts else "—"


def fetch_last_posts(page_id: str, page_access_token: str, limit: int = 3) -> list[PagePost]:
    """Load the latest posts for a Page."""
    page_id = page_id.strip()
    page_access_token = page_access_token.strip()
    if not page_id:
        raise MetaAPIError("Page ID is required.", operation="GET_POSTS")
    if not page_access_token:
        raise MetaAPIError("Page Access Token is required.", operation="GET_POSTS")

    payload = graph_request(
        "GET",
        f"/{page_id}/posts",
        params={
            "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,url}",
            "limit": max(1, min(int(limit), 10)),
            "access_token": page_access_token,
        },
        operation="GET_POSTS",
    )

    data = payload.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise MetaAPIError(
            "Unexpected /posts response (data is not a list).",
            operation="GET_POSTS",
        )

    posts: list[PagePost] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        posts.append(
            PagePost(
                id=str(item.get("id") or ""),
                message=str(item.get("message") or "(no message)"),
                created_time=str(item.get("created_time") or "—"),
                permalink_url=str(item.get("permalink_url") or ""),
                full_picture=str(item.get("full_picture") or ""),
                attachments_summary=_summarize_attachments(item.get("attachments")),
            )
        )

    return posts
