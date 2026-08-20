"""Async proxy from Django `/mcp/` to the FastMCP loopback subprocess.

Why a proxy at all
------------------
The MCP runtime is subprocess-isolated (locked decision): FastMCP
runs in its own process so a tool crashing or hanging cannot take down
the Django web server, and so MCP-specific deps stay out of Django's
import graph. The Django `/mcp/` view is the single entry point clients
hit; it authenticates them (resolving a `Principal`), then forwards the
raw JSON-RPC / SSE traffic to the loopback subprocess with the resolved
identity in trusted `X-MCP-*` headers.

Response handling is split by upstream Content-Type. Single-JSON replies
(the loopback runs with `json_response=True`, so that's every tools/list
+ tools/call response) are BUFFERED and returned with an explicit
Content-Length — a chunked response with no Content-Length gets buffered
and capped by edge proxies (Cloudflare caps such responses at ~64 KiB),
which truncates large tool results mid-JSON and hangs strict MCP clients.
True `text/event-stream` responses (the GET /mcp listen channel) keep
streaming pass-through untouched.

Logging
---------------------
We only write `APICallLog` rows for `tools/call` JSON-RPC requests — the
ones that map to an `@endpoint`. `initialize`, `tools/list`,
`notifications/*`, and DELETE-for-session-close aren't endpoint calls
and would just create noise. Latency is measured from request-received
through end-of-stream so a long-running SSE response gets the right
number.

Bearer auth (API-key OR OAuth token) is enforced before any traffic is
forwarded; the resolved identity rides out on `X-MCP-*` headers and a
fresh request id is added for the trace boundary.

File size — legitimate exception per CLAUDE.md
----------------------------------------------
This module exceeds the ~600-LOC ceiling documented under
`CLAUDE.md` → "File size + modularity". It is preserved as a single
file deliberately, per the exception clause for code "whose body
genuinely doesn't decompose."

The body is one async request lifecycle — bearer-token resolution →
JSON-RPC / SSE pass-through to the loopback subprocess → per-frame
APICallLog write. Splitting along that lifecycle would scatter the
request handling across multiple files without removing complexity:
the pieces are tightly coupled by shared state (the resolved
Principal, the streaming iterator, the request timer) and by ordering
(auth must precede forward; the timer must wrap end-to-end). Any split
that respected those constraints would just be the same code in
several files plus indirection.

If a future change does cleanly separate one concern (e.g. SSE framing
becomes its own primitive used elsewhere), that's the right moment to
revisit.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.core.principal import Principal

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.core import endpoint_gating, error_hook, log_context
from apps.core.bearer import resolve as resolve_bearer
from apps.core.events import EndpointCalled, fire
from apps.core.registry import registry
from apps.core.security.client_ip import client_ip
from apps.core.security.lockout import is_token_locked, is_url_token_locked
from apps.core.throttling import check as throttle_check

log = logging.getLogger(__name__)

# Subscribers may do DB I/O — dispatch on a thread.
_afire = sync_to_async(fire, thread_sensitive=True)

# Hop-by-hop headers must not be forwarded; HTTP/1.1 § 13.5.1.
# Content-Length is dropped because httpx + Django will recompute on the
# rewritten body, and Transfer-Encoding because chunking is per-hop.
#
# Phase 4.3.2 also strips `X-Forwarded-*` + `Forwarded`: ngrok and other
# tunnels add these to record the public-internet peer, but the loopback
# subprocess is logically *behind* Django and must see the connection as
# loopback-only — otherwise its loopback-peer guard trips
# on the original visitor's IP.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-real-ip",
        "forwarded",
    }
)

# A single long-lived async client — connection pooling matters because MCP
# clients tend to open many short-lived JSON-RPC posts in quick succession.
# Timeout shape: connect 5s (loopback should be instant or dead), read None
# (SSE streams stay open indefinitely), write 30s, pool 5s.
_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
_client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)


def _upstream_url(_request: HttpRequest) -> str:
    """Always forward to FastMCP's `/mcp` mount.

    Note the lack of trailing slash — FastMCP's default mount is `/mcp`
    exactly, and Starlette 307-redirects `/mcp/` to `/mcp`. Forwarding to
    the canonical path skips the redirect round-trip.
    """
    host = settings.MCP_LOOPBACK_HOST
    port = int(settings.MCP_LOOPBACK_PORT)
    return f"http://{host}:{port}/mcp"


def _filter_inbound_headers(
    request: HttpRequest,
    request_id: str,
    *,
    principal: Principal | None = None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        # Strip the inbound Authorization header — the FastMCP subprocess
        # only trusts the X-MCP-* identity headers below, set by us after
        # resolving the bearer.
        if key.lower() == "authorization":
            continue
        out[key] = value
    out["X-MCP-Request-Id"] = request_id
    # identity handoff to the loopback subprocess. The
    # subprocess refuses non-loopback peers so these
    # headers cannot be forged externally.
    if principal is not None:
        out["X-MCP-User-Id"] = str(principal.user.pk)
        if principal.credential is not None:
            out["X-MCP-Credential-Id"] = str(principal.credential.pk)
        out["X-MCP-Credential-Kind"] = principal.credential_kind
    # Cross-container trust handshake. The subprocess accepts callers presenting
    # a matching `X-MCP-Loopback-Secret` regardless of peer IP — required when
    # Django and MCP run in separate containers (Docker compose, k8s pods) so
    # the connection arrives from a bridge IP rather than 127.0.0.1.
    secret = getattr(settings, "MCP_LOOPBACK_SECRET", "") or ""
    if secret:
        out["X-MCP-Loopback-Secret"] = secret
    return out


def _filter_response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


def _slug_from_jsonrpc(body: bytes) -> str | None:
    """If `body` is a JSON-RPC `tools/call` request, return the tool name.

    Returns None for `initialize`, `tools/list`, notifications, or anything
    we can't parse. Parsing is best-effort — a malformed body just means
    we don't log, not that we fail the request.
    """
    if not body:
        return None
    try:
        msg = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("method") != "tools/call":
        return None
    name = (msg.get("params") or {}).get("name")
    return name if isinstance(name, str) else None


def _is_tools_list(body: bytes) -> bool:
    """True iff `body` is a JSON-RPC `tools/list` request. Drives the
    tool-hiding filter: disabled tools are dropped from the listing.
    Best-effort parse — a body we can't read just isn't treated as
    tools/list."""
    if not body:
        return False
    try:
        msg = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(msg, dict) and msg.get("method") == "tools/list"


def _is_jsonrpc_batch(body: bytes) -> bool:
    """True iff `body` is a JSON-RPC batch (a top-level array)."""
    if not body:
        return False
    try:
        return isinstance(json.loads(body), list)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def _hidden_tool_slugs() -> set[str]:
    """Slugs to drop from a `tools/list` response.

    Disabled endpoints are hidden from everyone — a disabled tool can't
    be called (the bridge refuses `tools/call`), so listing it would just
    advertise something guaranteed to error. This used to also union in
    the admin-only set for non-staff callers; that flag is gone (see
    `apps.core.models.EndpointFlag`), and with it the last reason for
    this listing to vary by caller. Sync DB read — call via
    sync_to_async."""
    return endpoint_gating.disabled_slugs()


def _tool_names_for_slugs(slugs: set[str]) -> set[str]:
    """Map endpoint slugs → the MCP tool names FastMCP lists them under.

    The tool name carries a `__<version>` suffix for v2+ (see
    `EndpointSpec.mcp_tool_name`), so we resolve through the registry
    rather than assuming name == slug."""
    if not slugs:
        return set()
    return {
        spec.mcp_tool_name
        for spec in registry.all()
        if spec.slug in slugs
    }


def _filter_tools_list_body(
    raw: bytes, content_type: str, hidden_tool_names: set[str]
) -> bytes:
    """Drop hidden tools (disabled / admin-only) from a buffered
    `tools/list` response body.

    FastMCP's streamable-HTTP transport answers either with a single
    `application/json` JSON-RPC object or a `text/event-stream` SSE frame
    whose `data:` line carries that same object. We rewrite both forms,
    leaving every other shape (and any parse failure) untouched —
    fail-open so a format we don't recognise never blanks the listing.
    """
    if not hidden_tool_names or not raw:
        return raw

    def _filter_obj(obj: object) -> object:
        if (
            isinstance(obj, dict)
            and isinstance(obj.get("result"), dict)
            and isinstance(obj["result"].get("tools"), list)
        ):
            obj["result"]["tools"] = [
                t
                for t in obj["result"]["tools"]
                if not (isinstance(t, dict) and t.get("name") in hidden_tool_names)
            ]
        return obj

    ctype = (content_type or "").lower()
    if "text/event-stream" in ctype:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw
        out_lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("data:"):
                payload = line.split("data:", 1)[1].strip()
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    out_lines.append(line)
                    continue
                out_lines.append("data: " + json.dumps(_filter_obj(parsed)))
            else:
                out_lines.append(line)
        return "\n".join(out_lines).encode("utf-8")

    # Default: treat as JSON.
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    return json.dumps(_filter_obj(parsed)).encode("utf-8")


def _not_found_tool_error_response(
    *, request_id: str, slug: str, body: bytes
) -> JsonResponse:
    """MCP-side 'this tool does not exist' response for a non-staff caller
    hitting an admin-only tool. Mirrors `_sunset_tool_error_response`'s
    HTTP-200 + JSON-RPC `isError` envelope so the tool stays invisible —
    a non-staff caller can't tell an admin-only tool apart from one that
    was never registered."""
    rpc_id: object = None
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                rpc_id = parsed.get("id")
        except (UnicodeDecodeError, json.JSONDecodeError):
            rpc_id = None
    envelope = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "error": {
                                "code": "tool_not_found",
                                "message": f"Unknown tool: {slug!r}.",
                            }
                        }
                    ),
                }
            ],
        },
    }
    resp = JsonResponse(envelope, status=200)
    resp.headers["X-Request-ID"] = request_id
    return resp



def _bearer_token_from_request(request: HttpRequest) -> str | None:
    raw = request.headers.get("Authorization") or ""
    if not raw:
        return None
    parts = raw.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve_principal(request: HttpRequest) -> Principal | None:
    token = _bearer_token_from_request(request)
    if token is None:
        return None
    return await resolve_bearer(token)




#: `EndpointSpec.mcp_tool_name`'s versioned form, inverted.
_TOOL_NAME_VERSION_RE = re.compile(r"^(?P<slug>.+?)__(?P<version>v[0-9]+)$")


def _spec_for_tool_name(name: str):  # noqa: ANN202 — return type is EndpointSpec | None
    """Resolve an MCP tool name to the exact spec the bridge registered
    it from: a bare name is v1, `slug__v2` is that version — the inverse
    of `EndpointSpec.mcp_tool_name`. The old helper fed the raw name to
    `registry.by_slug`, which matches on bare slug, so every v2+ tool
    name resolved to nothing and the sunset/deprecation gate silently
    skipped it. Returns None when the name is unknown to the registry
    (a stale client cache); the bridge answers tool_not_found there."""
    m = _TOOL_NAME_VERSION_RE.match(name)
    if m:
        return registry.get(m.group("version"), m.group("slug"))
    return registry.get("v1", name)


def _sunset_tool_error_response(
    *, request_id: str, spec, body: bytes
) -> JsonResponse:
    """Build the MCP-side counterpart of the REST view's 410 sunset
    response. MCP transport doesn't carry HTTP 410 meaning to the
    LLM client, so we return HTTP 200 with a JSON-RPC envelope whose
    `result.isError=true` block surfaces the same `endpoint_sunset`
    error code + sunset_at metadata. The advisory Deprecation/Sunset/
    Link headers ride along on the HTTP response so SDK clients that
    inspect headers see the same signal they'd get from REST."""
    sunset_iso = spec.sunset_at.isoformat() if spec.sunset_at else ""
    message = (
        spec.deprecation_message
        or f"This endpoint was sunset on {sunset_iso} and is no longer available."
    )
    # Recover the JSON-RPC request id from the inbound body so the
    # client can match the error to its outstanding call. Missing /
    # malformed id → null (the JSON-RPC spec allows it).
    rpc_id: object = None
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                rpc_id = parsed.get("id")
        except (UnicodeDecodeError, json.JSONDecodeError):
            rpc_id = None
    envelope = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "error": {
                                "code": "endpoint_sunset",
                                "message": message,
                                "sunset_at": sunset_iso,
                            }
                        }
                    ),
                }
            ],
        },
    }
    resp = JsonResponse(envelope, status=200)
    resp.headers["X-Request-ID"] = request_id
    for k, v in spec.deprecation_response_headers().items():
        resp.headers[k] = v
    return resp


def _unauthorized_response(request_id: str) -> JsonResponse:
    """RFC 9728 / MCP-spec 401: WWW-Authenticate carrying resource_metadata.

    Claude.ai (and any MCP-spec-compliant client) reads the
    ``resource_metadata`` URL from this header, fetches AS metadata
    from there, and starts the OAuth handshake.
    """
    issuer = settings.OAUTH_ISSUER.rstrip("/")
    resource_metadata = f"{issuer}/.well-known/oauth-protected-resource"
    body = {
        "error": {
            "code": "unauthorized",
            "message": (
                "Authentication required. Discover the authorization server via "
                f"{resource_metadata}"
            ),
        }
    }
    resp = JsonResponse(body, status=401)
    # The "Bearer" challenge MUST come first. `resource_metadata` is the
    # MCP-spec extension parameter (matches RFC 9728 § 5.1).
    resp.headers["WWW-Authenticate"] = (
        f'Bearer realm="mcp", resource_metadata="{resource_metadata}"'
    )
    resp.headers["X-Request-ID"] = request_id
    return resp


@csrf_exempt
async def mcp_proxy_view(
    request: HttpRequest, token: str | None = None
) -> HttpResponse:
    # outer RequestIdMiddleware always sets this. Fallback
    # only matters for in-process callers that skip middleware.
    request_id = getattr(request, "request_id", None) or uuid.uuid4().hex

    body = request.body
    log_slug = _slug_from_jsonrpc(body)
    is_tools_list = _is_tools_list(body)
    user_agent = request.headers.get("User-Agent", "")
    ip = client_ip(request)
    start = time.perf_counter()

    # `/mcp/k/<token>/` URL-path auth branch. The route
    # `token` kwarg, when set, contains the plaintext URL token. The
    # path-scrub middleware has already rewritten `request.path` to the
    # `<prefix>***` form for downstream logging — `token` is the raw
    # plaintext stashed by that same middleware before the rewrite, so
    # we can resolve it here without re-reading the dirty path.
    #
    # When the master flag is off we return 404 (not 401) — the surface
    # is deliberately invisible per the PLAN2 spec ("operators flip off
    # to refuse URL-auth entirely").
    if token is not None:
        if not getattr(settings, "MCP_URL_AUTH_ENABLED", True):
            from django.http import HttpResponseNotFound

            return HttpResponseNotFound()
        # Prefix-level lockout gate (mirrors the bearer-header branch
        # below). 10 fails / 60s on a prefix → 5min 429 lockout.
        url_token_plain = getattr(request, "_url_token_plain", token)
        # sync_to_async: this view is async and the lockout gate reads the
        # cache, which on this install is the DATABASE backend. Django
        # refuses ORM access from an async context, so the plain call
        # raised SynchronousOnlyOperation and every connector request
        # 500d. It only ever worked because Redis never touched the ORM.
        # Same treatment `record_use` already gets a few lines below.
        gate = await sync_to_async(is_url_token_locked, thread_sensitive=True)(
            url_token_plain
        )
        if not gate.allowed:
            resp = JsonResponse(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": (
                            "Too many failed authentication attempts on this "
                            f"URL token prefix. Retry after {gate.retry_after_s}s."
                        ),
                        "retry_after_s": gate.retry_after_s,
                    }
                },
                status=429,
            )
            resp.headers["Retry-After"] = str(gate.retry_after_s)
            resp.headers["X-Request-ID"] = request_id
            return resp

        principal = await resolve_bearer(url_token_plain)
        if principal is None:
            # No WWW-Authenticate dance here — a URL-path caller can't
            # respond to a discovery hint. Return a plain 401 with a
            # short hint message; the operator regenerates a fresh URL
            # from the dashboard.
            resp = JsonResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        # No URL in the hint. `/dashboard/url-tokens/` was
                        # a route this app never had; the real page is under
                        # the operator-configurable ops prefix, and naming
                        # that prefix to an unauthenticated caller gives away
                        # the one surface the IP allowlist exists to hide.
                        "message": (
                            "Invalid, revoked or expired URL token. Mint a new "
                            "connector URL from the ops UI."
                        ),
                    }
                },
                status=401,
            )
            resp.headers["X-Request-ID"] = request_id
            return resp
        request._principal = principal  # type: ignore[attr-defined]
        # Stamp last-used metadata + the credential_kind for analytics.
        # `record_use` is sync DB-bound; thread it.
        if principal.credential is not None and hasattr(
            principal.credential, "key_hash"
        ):
            try:
                from apps.url_mcp_tokens.api import record_use

                await sync_to_async(record_use, thread_sensitive=True)(
                    principal.credential, ip=ip
                )
            except Exception:
                log.exception(
                    "mcp proxy: url_token record_use failed",
                    extra={"request_id": request_id},
                )
    else:
        # prefix-level lockout gate fires BEFORE bearer
        # resolution so a known-bad prefix accumulating fails on /api/* +
        # /mcp/ both get rejected at the same threshold. Returns 429 with
        # Retry-After to the client; we also strip + drop the bearer token
        # from forwarding so the FastMCP loopback doesn't see it.
        bearer_for_gate = _bearer_token_from_request(request)
        if bearer_for_gate is not None:
            # Async-context safe, for the same reason as the URL-token gate above.
            gate = await sync_to_async(is_token_locked, thread_sensitive=True)(
                bearer_for_gate
            )
            if not gate.allowed:
                resp = JsonResponse(
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": (
                                "Too many failed authentication attempts on this key "
                                f"prefix. Retry after {gate.retry_after_s}s."
                            ),
                            "retry_after_s": gate.retry_after_s,
                        }
                    },
                    status=429,
                )
                resp.headers["Retry-After"] = str(gate.retry_after_s)
                resp.headers["X-Request-ID"] = request_id
                return resp

        # authenticate before we forward anything. MCP clients
        # (Claude.ai) send no token on the very first probe; we answer 401
        # with a WWW-Authenticate header pointing at the protected-resource
        # metadata, the client follows it to the AS, runs the OAuth flow,
        # and comes back with a token.
        principal = await _resolve_principal(request)
        if principal is None:
            return _unauthorized_response(request_id)
        request._principal = principal  # type: ignore[attr-defined]

    # backfill `user_id` on the log contextvar so any log
    # record emitted further down (upstream forward, streamer
    # finalization) carries the user. The outer RequestIdMiddleware reset
    # restores this when the request completes.
    log_context.update_user_id(principal.user.pk)

    # Refuse JSON-RPC batches. Every gate below — throttle, the sunset
    # check, the APICallLog write — hangs off the single-object
    # `tools/call` slug, so a top-level array sailed past all of them and
    # was forwarded upstream: N tool calls, none metered, gated or
    # recorded. Current MCP (2025-06-18) removed JSON-RPC batching, so no
    # compliant client sends one; refusing whole is honest where
    # partially honouring (some entries throttled, some not) would not be.
    if _is_jsonrpc_batch(body):
        resp = JsonResponse(
            {
                "error": {
                    "code": "batch_not_supported",
                    "message": (
                        "JSON-RPC batch requests are not supported. "
                        "Send one request per HTTP call."
                    ),
                }
            },
            status=400,
        )
        resp.headers["X-Request-ID"] = request_id
        return resp

    # An `admin_only` gate used to sit here, mirroring REST's: a non-staff
    # caller of an admin-only tool got "tool not found". It read
    # `principal.user.is_staff`, which is True for every credential this
    # single-operator product issues, so it never fired. Removed with the
    # flag itself — see `apps.core.models.EndpointFlag`.

    # throttle on the MCP path, but only for tools/call.
    # tools/list / initialize / pings are bookkeeping and shouldn't
    # count against the user's per-endpoint limit.
    if log_slug:
        throttle = await sync_to_async(throttle_check, thread_sensitive=True)(
            user=principal.user,
            endpoint_slug=log_slug,
            ip=ip,
            # Without this the checker sees no credential and took its
            # unlimited branch, so every tools/call went unthrottled and
            # the 429 handling below was unreachable. REST has always
            # passed it (rest.py); the MCP path was simply missed.
            credential=principal.credential,
        )
        if not throttle.allowed:
            resp = JsonResponse(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": (
                            f"Rate limit exceeded ({throttle.limit_per_min}/min). "
                            f"Retry after {throttle.retry_after_s}s."
                        ),
                        "limit_per_min": throttle.limit_per_min,
                        "retry_after_s": throttle.retry_after_s,
                    }
                },
                status=429,
            )
            resp.headers["Retry-After"] = str(throttle.retry_after_s)
            resp.headers["X-RateLimit-Limit"] = str(throttle.limit_per_min)
            resp.headers["X-RateLimit-Remaining"] = "0"
            resp.headers["X-Request-ID"] = request_id
            return resp

    # FinalPolish F3 — RFC 8594 deprecation + sunset gate on the MCP
    # path. Sunset short-circuits with a JSON-RPC tool error envelope
    # before we forward anything to the loopback subprocess — the call
    # literally cannot be served. Deprecation flags the slug so we can
    # stamp advisory HTTP headers on the response once it's built.
    deprecation_headers: dict[str, str] = {}
    if log_slug:
        active_for_gate = _spec_for_tool_name(log_slug)
        if active_for_gate is not None:
            now = timezone.now()
            if active_for_gate.is_sunset_at(now):
                return _sunset_tool_error_response(
                    request_id=request_id,
                    spec=active_for_gate,
                    body=body,
                )
            if active_for_gate.is_deprecation_active_at(now):
                deprecation_headers = active_for_gate.deprecation_response_headers()

    # A credit charge used to sit here: resolve the tool's slug back to
    # its spec, charge `credits_cost` before forwarding, map
    # InsufficientCreditsError to 402, and refund on upstream non-2xx via
    # a hand-managed context manager that had to straddle the streamer
    # coroutine. No endpoint declares a cost and no billing backend was
    # ever registered, so it charged nothing on every call. Removed with
    # the rest of the billing apparatus.

    upstream_req = _client.build_request(
        method=request.method or "POST",
        url=_upstream_url(request),
        headers=_filter_inbound_headers(request, request_id, principal=principal),
        content=body if body else None,
    )

    try:
        upstream = await _client.send(upstream_req, stream=True)
    except httpx.ConnectError as exc:
        log.warning(
            "mcp proxy: cannot reach loopback subprocess at %s: %s",
            _upstream_url(request),
            exc,
            extra={"request_id": request_id},
        )
        if log_slug:
            await _safe_record(
                request_id=request_id,
                slug=log_slug,
                status_code=503,
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                error_class="MCPUnavailable",
                ip=ip,
                user_agent=user_agent,
                principal=principal,
            )
        # Error event so `/ops/logs/` surfaces subprocess outages
        # alongside view-layer crashes.
        await _safe_record_error(
            exc=exc,
            request_id=request_id,
            slug=log_slug or "",
            request_path=getattr(request, "_scrubbed_path", request.path) or "",
            request_method=request.method or "",
            status_code=503,
            principal=principal,
            ip=ip,
            user_agent=user_agent,
        )
        body_payload = {
            "error": {
                "code": "mcp_unavailable",
                "message": ("MCP subprocess is not running. Start it with `make mcp` (Phase 2.3)."),
            }
        }
        resp = JsonResponse(body_payload, status=503)
        resp.headers["X-Request-ID"] = request_id
        return resp
    except httpx.HTTPError as exc:
        log.exception(
            "mcp proxy: upstream transport error",
            extra={"request_id": request_id, "error": str(exc)},
        )
        if log_slug:
            await _safe_record(
                request_id=request_id,
                slug=log_slug,
                status_code=502,
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                error_class=type(exc).__name__,
                ip=ip,
                user_agent=user_agent,
                principal=principal,
            )
        await _safe_record_error(
            exc=exc,
            request_id=request_id,
            slug=log_slug or "",
            request_path=getattr(request, "_scrubbed_path", request.path) or "",
            request_method=request.method or "",
            status_code=502,
            principal=principal,
            ip=ip,
            user_agent=user_agent,
        )
        resp = JsonResponse(
            {"error": {"code": "mcp_unavailable", "message": "Upstream transport error."}},
            status=502,
        )
        resp.headers["X-Request-ID"] = request_id
        return resp

    upstream_status = upstream.status_code

    # Gating: rewrite tools/list so disabled tools never appear in the
    # listing — the bridge refuses to call them anyway, so listing one
    # just advertises a guaranteed error. The listing is small + bounded,
    # so we buffer it, drop the hidden tools, and return it non-streamed.
    # When nothing is disabled, and for every other JSON-RPC method, we
    # fall through to the pass-through streamer below untouched.
    if is_tools_list and 200 <= upstream_status < 400:
        hidden_slugs = await sync_to_async(
            _hidden_tool_slugs, thread_sensitive=True
        )()
        if hidden_slugs:
            try:
                raw = await upstream.aread()
            finally:
                await upstream.aclose()
            content_type = upstream.headers.get("content-type", "application/json")
            filtered = _filter_tools_list_body(
                raw, content_type, _tool_names_for_slugs(hidden_slugs)
            )
            resp = HttpResponse(filtered, status=upstream_status, content_type=content_type)
            for key, value in _filter_response_headers(upstream).items():
                if key.lower() in ("content-type", "content-length"):
                    continue
                resp.headers[key] = value
            resp.headers["X-Request-ID"] = request_id
            return resp

    async def finalize() -> None:
        """Write the call record. Shared by the buffered path and the
        streamer's end-of-stream cleanup."""
        if log_slug:
            latency_ms = max(1, int((time.perf_counter() - start) * 1000))
            await _safe_record(
                request_id=request_id,
                slug=log_slug,
                status_code=upstream_status,
                latency_ms=latency_ms,
                error_class="" if 200 <= upstream_status < 400 else "UpstreamError",
                ip=ip,
                user_agent=user_agent,
                principal=principal,
            )

    def _stamp_common_headers(response: HttpResponse) -> HttpResponse:
        for key, value in _filter_response_headers(upstream).items():
            if key.lower() == "content-type":
                continue
            response.headers[key] = value
        response.headers["X-Request-ID"] = request_id
        # FinalPolish F3 — stamp RFC 8594 advisory headers if the tool maps
        # to a deprecated-but-not-yet-sunset endpoint. Empty dict on non-
        # deprecated tools (or non-endpoint JSON-RPC methods like
        # tools/list), so the loop is a no-op in the common case.
        for k, v in deprecation_headers.items():
            response.headers[k] = v
        return response

    # Buffer everything that is not a true SSE stream. A chunked response
    # with no Content-Length gets buffered and CAPPED by edge proxies
    # (Cloudflare caps such responses at ~64 KiB) — a large tool result
    # truncates mid-JSON and a strict MCP client hangs waiting for the
    # rest of the message. JSON-RPC replies are bounded, so reading them
    # fully and answering with an explicit Content-Length lets every
    # intermediary pass them through intact. The loopback subprocess runs
    # with `json_response=True`, so this is the path virtually every
    # tools/list + tools/call reply takes; only the GET /mcp listen
    # channel (and any genuinely streamed reply) stays SSE.
    upstream_ctype = (upstream.headers.get("content-type") or "").lower()
    if "text/event-stream" not in upstream_ctype:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        await finalize()
        buffered = HttpResponse(
            raw,
            status=upstream_status,
            content_type=upstream.headers.get("content-type", "application/json"),
        )
        buffered.headers["Content-Length"] = str(len(raw))
        return _stamp_common_headers(buffered)

    async def streamer() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await finalize()

    response = StreamingHttpResponse(
        streamer(),
        status=upstream_status,
        content_type=upstream.headers.get("content-type", "application/json"),
    )
    return _stamp_common_headers(response)


async def _safe_record_error(
    *,
    exc: BaseException,
    request_id: str,
    slug: str,
    request_path: str,
    request_method: str,
    status_code: int,
    principal: Principal | None = None,
    ip: str | None = None,
    user_agent: str = "",
) -> None:
    """write an ErrorLog row for MCP-side failures.

    Best-effort; same defensive contract as `_safe_record`. The recorder
    itself is sync DB-bound, hence sync_to_async.
    """
    try:
        await sync_to_async(error_hook.record_error, thread_sensitive=True)(
            exc=exc,
            request_id=request_id,
            source="mcp",
            endpoint_slug=slug,
            request_path=request_path,
            request_method=request_method,
            status_code=status_code,
            user_id=principal.user.pk if principal else None,
            ip=ip,
            user_agent=user_agent,
            handled=False,
        )
    except Exception:
        log.exception(
            "mcp proxy: error_hook.record_error failed",
            extra={"request_id": request_id, "endpoint_slug": slug},
        )


async def _safe_record(
    *,
    request_id: str,
    slug: str,
    status_code: int,
    latency_ms: int,
    error_class: str,
    ip: str | None,
    user_agent: str,
    principal: Principal | None = None,
) -> None:
    try:
        await _afire(
            EndpointCalled(
                request_id=request_id,
                source="mcp",
                endpoint_slug=slug,
                status_code=status_code,
                latency_ms=latency_ms,
                user_id=principal.user.pk if principal else None,
                credential_id=(
                    principal.credential.pk
                    if principal and principal.credential is not None
                    else None
                ),
                # Names the table that pk belongs to. Without it the
                # api_keys subscriber stamps `last_used_*` on whichever
                # APIKey happens to share the number with a URL token.
                credential_kind=(principal.credential_kind if principal else ""),
                ip=ip,
                user_agent=user_agent,
                error_class=error_class,
            )
        )
    except Exception:
        log.exception(
            "mcp proxy: events.fire failed",
            extra={"request_id": request_id, "endpoint_slug": slug},
        )
