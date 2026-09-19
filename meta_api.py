"""
Meta Graph API client for exchanging user tokens and fetching Page tokens.

Secrets (App Secret, access tokens) are never printed or logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

DEFAULT_TIMEOUT = 30


class MetaAPIError(Exception):
    """User-facing Meta / network error (no secrets in the message)."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.error_type = error_type

    def format_for_user(self) -> str:
        lines = ["Meta API Error", "", f"Message:\n{self.message}"]
        if self.code is not None:
            lines.extend(["", f"Code:\n{self.code}"])
        if self.subcode is not None:
            lines.extend(["", f"Subcode:\n{self.subcode}"])
        if self.error_type:
            lines.extend(["", f"Type:\n{self.error_type}"])
        return "\n".join(lines)


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


def _redact_for_safety(text: str) -> str:
    """Strip common secret-looking substrings from error text (best-effort)."""
    # Do not attempt to log secrets; just avoid echoing long opaque strings back.
    if len(text) > 400:
        return text[:400] + "…"
    return text


def _parse_meta_error_payload(payload: dict[str, Any]) -> MetaAPIError:
    err = payload.get("error")
    if not isinstance(err, dict):
        return MetaAPIError("Unexpected Meta API response (missing error details).")

    message = _redact_for_safety(str(err.get("message") or "Unknown Meta API error."))
    code = err.get("code")
    subcode = err.get("error_subcode")
    error_type = err.get("type")

    code_i = int(code) if isinstance(code, int) or (isinstance(code, str) and code.isdigit()) else None
    sub_i = (
        int(subcode)
        if isinstance(subcode, int) or (isinstance(subcode, str) and str(subcode).isdigit())
        else None
    )

    hint = ""
    msg_l = message.lower()

    if code_i == 190 or "session has expired" in msg_l or "invalid oauth" in msg_l:
        hint = (
            "\n\nHint: The User Access Token is invalid or expired.\n"
            "• Generate a fresh token in Graph API Explorer\n"
            "• Choose “User Token” (not Page Token)\n"
            "• Use the same Meta App as App ID / App Secret below"
        )
    elif "client secret" in msg_l or "app secret" in msg_l:
        hint = (
            "\n\nHint: App Secret does not match this App ID. "
            "Copy App Secret again from Meta App → Settings → Basic."
        )
    elif "application" in msg_l and ("match" in msg_l or "belong" in msg_l or "validating" in msg_l):
        hint = (
            "\n\nHint: This Access Token was issued for a different Meta App. "
            "App ID / App Secret must be from the same app that created the token."
        )
    elif code_i == 200:
        hint = (
            "\n\nHint: Missing permission or Page access. "
            "Ensure pages_show_list / pages_read_engagement (and related) are granted."
        )
    elif code_i == 10:
        hint = "\n\nHint: Permission denied for this operation on the Page or app."
    elif code_i == 100:
        hint = (
            "\n\nHint: Invalid parameter — check App ID, App Secret, and that the "
            "token is a User Access Token (not a Page Access Token)."
        )
    elif code_i == 1:
        hint = (
            "\n\nHint: Meta returned a generic error. Often this means App ID/Secret "
            "mismatch or a Page token used instead of a User token."
        )
    return MetaAPIError(
        message + hint,
        code=code_i,
        subcode=sub_i,
        error_type=str(error_type) if error_type else None,
    )


def graph_request(
    method: str,
    path_or_url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """
    Perform a Graph API request. Pass secrets only via ``params`` (requests encodes them).

    ``path_or_url`` may be a path like ``/me/accounts`` or a full ``https://graph.facebook.com/...`` URL
    (used for pagination ``paging.next``).
    """
    params = dict(params or {})

    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
        # Absolute paging URLs already include query params; merge carefully.
        # requests will append/override with params= — prefer URL as given for next pages.
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"graph.facebook.com", "graph.facebook.net"}:
            raise MetaAPIError("Refusing to follow a non-Meta pagination URL.")
        request_params = params if params else None
    else:
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        url = f"{GRAPH_API_BASE}{path}"
        request_params = params

    try:
        response = requests.request(
            method.upper(),
            url,
            params=request_params,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise MetaAPIError(
            "HTTP timeout while contacting Meta Graph API. Check your network and try again."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise MetaAPIError(
            "No internet connection or Meta Graph API is unreachable."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise MetaAPIError(f"Network error while contacting Meta Graph API: {exc.__class__.__name__}") from exc

    try:
        payload: Any = response.json()
    except json.JSONDecodeError as exc:
        raise MetaAPIError(
            f"Meta returned a non-JSON response (HTTP {response.status_code})."
        ) from exc
    except ValueError as exc:
        raise MetaAPIError(
            f"Could not parse Meta response as JSON (HTTP {response.status_code})."
        ) from exc

    if not isinstance(payload, dict):
        raise MetaAPIError("Unexpected Meta API response shape (expected a JSON object).")

    if "error" in payload:
        raise _parse_meta_error_payload(payload)

    if not response.ok:
        raise MetaAPIError(f"HTTP {response.status_code} from Meta Graph API.")

    return payload


def inspect_input_token(app_id: str, app_secret: str, input_token: str) -> dict[str, Any]:
    """
    Call ``/debug_token`` (no secrets logged). Returns the ``data`` object.
    Used to catch Page tokens / wrong-app tokens before exchange.
    """
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    input_token = input_token.strip()
    # App access token form: APP_ID|APP_SECRET (passed only via params).
    app_token = f"{app_id}|{app_secret}"
    payload = graph_request(
        "GET",
        "/debug_token",
        params={
            "input_token": input_token,
            "access_token": app_token,
        },
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MetaAPIError("Could not inspect the Access Token (unexpected debug_token response).")
    return data


def exchange_user_token(
    app_id: str,
    app_secret: str,
    short_lived_user_token: str,
) -> TokenExchangeResult:
    """Exchange a short-lived User Access Token for a long-lived User Access Token."""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    short_lived_user_token = short_lived_user_token.strip()

    if not app_id:
        raise MetaAPIError("Meta App ID is required.")
    if not app_secret:
        raise MetaAPIError("Meta App Secret is required.")
    if not short_lived_user_token:
        raise MetaAPIError("User Access Token is required.")

    # Pre-check: reject Page tokens / wrong app early with a clear message.
    try:
        info = inspect_input_token(app_id, app_secret, short_lived_user_token)
    except MetaAPIError:
        # If debug_token itself fails (bad secret etc.), continue to exchange —
        # exchange will return the underlying Meta error.
        info = None

    if info is not None:
        token_type = str(info.get("type") or "").upper()
        app_id_from_token = str(info.get("app_id") or "")
        is_valid = info.get("is_valid")

        if is_valid is False:
            err = info.get("error")
            detail = ""
            if isinstance(err, dict) and err.get("message"):
                detail = f"\n\nMeta says: {_redact_for_safety(str(err.get('message')))}"
            raise MetaAPIError(
                "This Access Token is not valid (expired or revoked)."
                + detail
                + "\n\nGenerate a new User Access Token in Graph API Explorer."
            )

        if token_type == "PAGE":
            raise MetaAPIError(
                "You pasted a Page Access Token.\n\n"
                "This step needs a User Access Token.\n"
                "In Graph API Explorer set the token type to User Token "
                "(not a Page), then copy that token here."
            )

        if app_id_from_token and app_id_from_token != app_id:
            raise MetaAPIError(
                "This Access Token belongs to a different Meta App.\n\n"
                f"Token app_id: {app_id_from_token}\n"
                f"You entered App ID: {app_id}\n\n"
                "Use App ID + App Secret from the same app that issued the token."
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
    )

    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise MetaAPIError("Token exchange succeeded but no access_token was returned.")

    token_type_out = str(payload.get("token_type") or "bearer")
    expires_raw = payload.get("expires_in")
    expires_in: int | None
    if isinstance(expires_raw, int):
        expires_in = expires_raw
    elif isinstance(expires_raw, str) and expires_raw.isdigit():
        expires_in = int(expires_raw)
    else:
        expires_in = None

    return TokenExchangeResult(
        access_token=token,
        token_type=token_type_out,
        expires_in=expires_in,
    )


def fetch_all_pages(long_lived_user_token: str) -> list[FacebookPage]:
    """Fetch all Pages from ``/me/accounts`` with pagination."""
    token = long_lived_user_token.strip()
    if not token:
        raise MetaAPIError("Long-lived User Access Token is required to list Pages.")

    pages: list[FacebookPage] = []
    next_url: str | None = None
    first = True

    while first or next_url:
        if first:
            payload = graph_request(
                "GET",
                "/me/accounts",
                params={
                    "fields": "id,name,access_token,tasks",
                    "access_token": token,
                    "limit": 100,
                },
            )
            first = False
        else:
            assert next_url is not None
            # paging.next is a full URL; do not re-attach the token via params.
            payload = graph_request("GET", next_url)

        data = payload.get("data")
        if data is None:
            data = []
        if not isinstance(data, list):
            raise MetaAPIError("Unexpected /me/accounts response (data is not a list).")

        for item in data:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip() or "(unnamed)"
            page_token = item.get("access_token")
            if not page_id or not isinstance(page_token, str) or not page_token:
                continue
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

    # Empty list is OK — the GUI shows a warning. Long-lived token may still be usable.
    return pages


def test_page_token(page_id: str, page_access_token: str) -> tuple[str, str]:
    """Validate a Page Access Token. Returns (id, name)."""
    page_id = page_id.strip()
    page_access_token = page_access_token.strip()
    if not page_id:
        raise MetaAPIError("Page ID is required.")
    if not page_access_token:
        raise MetaAPIError("Page Access Token is required.")

    payload = graph_request(
        "GET",
        f"/{page_id}",
        params={
            "fields": "id,name",
            "access_token": page_access_token,
        },
    )

    pid = str(payload.get("id") or "")
    name = str(payload.get("name") or "")
    if not pid:
        raise MetaAPIError("Page test returned no id.")
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
        raise MetaAPIError("Page ID is required.")
    if not page_access_token:
        raise MetaAPIError("Page Access Token is required.")

    payload = graph_request(
        "GET",
        f"/{page_id}/posts",
        params={
            "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,url}",
            "limit": max(1, min(int(limit), 10)),
            "access_token": page_access_token,
        },
    )

    data = payload.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise MetaAPIError("Unexpected /posts response (data is not a list).")

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
