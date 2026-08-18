"""Feed intake — the trusted first step of the write door (M2.1).

All three channels (ops UI form, REST, MCP) create Feeds through
`propose()`. URL fetching happens HERE, in trusted Django code, never in
the feeder agent (grill C12: fed URLs are attacker-influenceable input,
so the agent runs with no network and gets pre-fetched text). Nothing in
this module touches the brain repo.

Failure policy (grill C21 — capture must never lose data):
- invalid INPUT (no source, oversize pasted content, bad URL/scheme,
  private-address host) → `FeedRejected`, no row created;
- a fetch that legitimately FAILS (network error, HTTP error, non-text
  body) still creates the pending Feed with the error recorded in
  `raw_payload["fetch"]` — the operator can paste content or the M2.2
  extraction can re-fetch.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings as dj_settings
from django.utils import timezone

from apps.events.models import emit
from apps.feeds.models import Feed

FETCH_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 3
# Body content types we'll extract text from. Anything else (pdf, images,
# archives) is recorded as a fetch failure, not silently mangled.
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,100}$")
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


class FeedRejected(ValueError):
    """Invalid proposal input — no Feed row is created. Messages are
    user-safe (the REST layer returns ValueError text verbatim)."""


def payload_max_bytes() -> int:
    return int(dj_settings.FEED_PAYLOAD_MAX_KB) * 1024


# ---- source id -----------------------------------------------------------


def derive_source_id(source_kind: str, title: str, source_url: str) -> str:
    """`<kind>-<YYYY-MM>-<slug>` from the best available label, matching
    the contract's source-id shape (`yt-2026-06-rag-video`)."""
    label = title or urlsplit(source_url).path.rsplit("/", 1)[-1] or urlsplit(source_url).hostname or "untitled"
    slug = _NON_SLUG_CHARS.sub("-", label.lower()).strip("-")[:60] or "untitled"
    return f"{source_kind}-{timezone.localdate():%Y-%m}-{slug}"


# ---- URL fetch (trusted code, SSRF-guarded) ------------------------------


def _assert_public_http_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts that resolve to any
    non-global address (loopback, RFC1918, link-local, metadata range…).
    Best-effort TOCTOU caveat: resolution here and inside httpx are two
    lookups; the egress firewall (PLAN.md §9) is the backstop."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FeedRejected(f"unsupported URL scheme: {parts.scheme or '(none)'} — http(s) only.")
    host = parts.hostname
    if not host:
        raise FeedRejected("URL has no host.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        raise FeedRejected(f"URL host does not resolve: {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FeedRejected("URL host resolves to a private or reserved address.")


class _TextExtractor(HTMLParser):
    """Crude HTML → text: drops script/style/head, keeps visible text.
    Good enough for the feeder agent; depth stays in the linked source."""

    _SKIP = {"script", "style", "noscript", "template", "head", "svg"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html  # malformed markup: hand the raw bytes to the feeder
    return parser.text() or html


def fetch_url(url: str) -> dict:
    """Fetch `url` with SSRF guards on every redirect hop; returns the
    `raw_payload["fetch"]` metadata dict (+ extracted text under "text").
    Only raises FeedRejected (bad input); transport errors come back as
    ok=False so the caller can still capture the Feed."""
    meta: dict = {"attempted": True, "ok": False, "url": url, "fetched_at": timezone.now().isoformat()}
    cap = payload_max_bytes()
    try:
        current = url
        with httpx.Client(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": "my-brain-web-app/feed-intake"},
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                _assert_public_http_url(current)
                with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        current = urljoin(current, resp.headers["location"])
                        continue
                    meta["status"] = resp.status_code
                    meta["final_url"] = current
                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    meta["content_type"] = ctype
                    if resp.status_code >= 400:
                        meta["error"] = f"HTTP {resp.status_code}"
                        return meta
                    if ctype and not ctype.startswith(_TEXT_TYPES):
                        meta["error"] = f"unsupported content type: {ctype}"
                        return meta
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > cap:
                            meta["truncated"] = True
                            body = body[:cap]
                            break
                    raw = body.decode(resp.encoding or "utf-8", errors="replace")
                    text = html_to_text(raw) if "html" in ctype else raw
                    meta["ok"] = True
                    meta["bytes"] = len(text.encode("utf-8"))
                    meta["text"] = text
                    return meta
            meta["error"] = f"too many redirects (>{MAX_REDIRECTS})"
            return meta
    except FeedRejected:
        raise
    except httpx.HTTPError as exc:
        meta["error"] = f"fetch failed: {exc.__class__.__name__}"
        return meta


# ---- the one entry point -------------------------------------------------


def propose(
    *,
    channel: str,
    source_kind: str,
    title: str = "",
    source_url: str = "",
    content: str = "",
    notes: str = "",
    source_id: str = "",
    consumer=None,
    extract: bool = True,
) -> Feed:
    """Validate + capture one proposed source as a pending Feed.

    The repo is never touched. Raises FeedRejected on invalid input;
    fetch failures are captured on the row instead (see module docstring).
    """
    source_kind = _NON_SLUG_CHARS.sub("-", (source_kind or "").strip().lower()).strip("-") or "doc"
    title = (title or "").strip()
    source_url = (source_url or "").strip()
    content = (content or "").strip()
    notes = (notes or "").strip()

    if not source_url and not content:
        raise FeedRejected("Provide a source URL, pasted content, or both.")
    cap = payload_max_bytes()
    if len(content.encode("utf-8")) > cap:
        raise FeedRejected(
            f"Pasted content exceeds the payload cap ({dj_settings.FEED_PAYLOAD_MAX_KB} KB). "
            "Trim it, or raise FEED_PAYLOAD_MAX_KB."
        )

    if source_id:
        source_id = source_id.strip().lower()
        if not _SLUG_RE.match(source_id):
            raise FeedRejected("source_id must be a kebab-case slug (a-z, 0-9, hyphens, 3-100 chars).")
    else:
        source_id = derive_source_id(source_kind, title, source_url)

    payload: dict = {
        "source_kind": source_kind,
        "title": title,
        "source_url": source_url,
        "content": content,
        "notes": notes,
    }
    # Fetch only when the caller didn't paste the content themselves —
    # pasted text is authoritative (e.g. a cleaned transcript).
    if source_url and not content:
        fetch = fetch_url(source_url)
        payload["content"] = fetch.pop("text", "")
        payload["fetch"] = fetch

    feed = Feed.objects.create(
        source_id=source_id,
        channel=channel,
        consumer=consumer,
        raw_payload=payload,
        status="pending",
    )
    emit(
        "feed",
        consumer=consumer,
        action="proposed",
        feed_id=feed.pk,
        source_id=source_id,
        channel=channel,
        payload_bytes=feed.payload_bytes,
        fetch_ok=payload.get("fetch", {}).get("ok"),
    )
    # Fire-and-forget: the M2.2 feeder agent fills feed.proposal on the
    # worker. Enqueue failure is recorded on the row, never raised — the
    # capture is already safe.
    #
    # `extract=False` is for callers that ALREADY have a complete proposal
    # and must not have it overwritten — the direct-thought lane composes
    # its note deterministically, so an extraction here would spend an SDK
    # run to replace a valid proposal with a guess at what the writer meant.
    if extract:
        from apps.feeds.services import feeder

        feeder.enqueue_extraction(feed)
    return feed
