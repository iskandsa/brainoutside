"""The capture lane that cannot fail: save a thought, decide about it later.

Every other way into this brain goes through the feeder agent — a source
is captured, an SDK run extracts notes from it, and the result is judged
against ~30 rules. That is right for a video, a repo or a playbook. It is
absurd for "here is something I just thought", which is why the owner
stopped using his own brain: a direct thought had to survive an extraction
it did not need and rules written for published content.

This path runs no agent and makes no editorial choice. It writes down what
was typed, verbatim, and files it correctly by construction:

- The thought IS the VERBATIM quote (rule 4), because they are the owner's
  own words. Nothing is paraphrased, so nothing can be paraphrased wrongly.
- `source` is the feed id and `source_url` is absent, which is exactly what
  the contract calls a direct thought (§5 rule 2).
- The topic comes from the live taxonomy as a choice, never invented (§7).
- `visibility` defaults to agents-only, the contract's answer when unsure.
- A `raw/` archive holds the untouched text, and carries NO index line —
  raw is never indexed (§4).

The result is a complete, valid proposal the moment it is saved. One click
approves it. If anything here ever failed to produce a valid proposal, the
capture is still stored — the thought is never the thing that gets lost.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from apps.feeds.models import Feed
from apps.feeds.services import intake, validator

log = logging.getLogger(__name__)

#: Types a thought can be filed as. `take` leads because an unprompted
#: thought is nearly always a position; the others are there so a story or
#: a lesson does not have to be mislabelled to get in.
THOUGHT_TYPES = ("take", "lesson", "fact", "story")
_TYPE_FOLDER = {"take": "takes", "lesson": "lessons", "fact": "facts", "story": "stories"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, words: int = 8) -> str:
    parts = [p for p in _SLUG_RE.sub("-", text.strip().lower()).split("-") if p]
    return "-".join(parts[:words]) or "thought"


def taxonomy() -> list[str]:
    """The live taxonomy, for the form's topic picker.

    Read from the repo rather than hardcoded: a tag added to CLAUDE.md
    shows up here on the next page load, and nothing here can propose a
    tag the contract does not already carry.
    """
    try:
        return sorted(validator.context_from_repo().taxonomy)
    except Exception:  # noqa: BLE001 — the form must render even if the clone is sick
        log.warning("thought: taxonomy unreadable; the picker will be empty", exc_info=True)
        return []


def capture(*, text: str, title: str, topic: str, ntype: str = "take") -> Feed:
    """Store a thought and file it. Raises FeedRejected only on empty text."""
    text = (text or "").strip()
    title = (title or "").strip() or _first_line(text)
    ntype = ntype if ntype in THOUGHT_TYPES else "take"
    month = date.today().strftime("%Y-%m")
    slug = slugify(title)

    feed = intake.propose(
        channel="ui",
        source_kind="thought",
        title=title,
        content=text,
        source_id=f"thought-{month}-{slug}",
        # No agent runs on this path. The proposal below IS the
        # extraction; an SDK run here would spend money replacing a
        # valid verbatim note with a guess at what the writer meant.
        extract=False,
    )

    note_id = f"{ntype}-{month}-{slug}"
    note_path = f"knowledge/{_TYPE_FOLDER[ntype]}/{note_id}.md"
    raw_path = f"raw/{date.today().isoformat()}-thought-{slug}.md"
    topics = [topic] if topic else []

    proposal = {
        "source_id": feed.source_id,
        "summary": title,
        "files": [
            {
                "path": note_path,
                "action": "create",
                "content": _note(
                    note_id=note_id, ntype=ntype, topics=topics, source=feed.source_id,
                    month=month, title=title, text=text, raw_path=raw_path,
                ),
            },
            {"path": raw_path, "action": "create", "content": _archive(title, text)},
        ],
        # No index line for the raw archive: raw is never indexed (§4), and
        # proposing one is what once refused an entire feed.
        "index_lines": [
            {
                "entity_id": note_id,
                "line": f"- [{ntype}] {title} | status: current | visibility: agents-only | {note_path}",
            }
        ],
        "supersedes": [],
        "taxonomy_additions": [],
        "issues": [],
    }

    feed.proposal = proposal
    feed.save(update_fields=["proposal"])
    return feed


def _first_line(text: str) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "Untitled thought")
    return line[:70]


def _quote(text: str) -> str:
    """The whole thought as one blockquote — every line prefixed, blank
    lines included, so a multi-paragraph thought stays one quote instead of
    breaking out of it halfway down."""
    return "\n".join(f"> {l}" if l.strip() else ">" for l in text.splitlines())


def _verbatim(text: str) -> str:
    """The thought as its own VERBATIM block (rule 4).

    A one-line thought IS the quote; repeating it underneath as a second
    blockquote printed the same sentence twice. Longer thoughts keep the
    first line as the headline quote, and the remainder follows it.
    """
    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip()), None)
    if idx is None:
        return '> VERBATIM: ""'
    head = '> VERBATIM: "%s"' % lines[idx].strip()
    rest = "\n".join(lines[idx + 1:])
    if not rest.strip():
        return head
    return head + "\n>\n" + _quote(rest)


def _note(*, note_id, ntype, topics, source, month, title, text, raw_path) -> str:
    return (
        "---\n"
        f"id: {note_id}\n"
        f"type: {ntype}\n"
        f"topics: [{', '.join(topics)}]\n"
        f"source: {source}\n"
        "source_url: null\n"
        f"date: {month}\n"
        "status: current\n"
        "superseded_by: null\n"
        "visibility: agents-only\n"
        "---\n"
        f"# {title}\n\n"
        + _verbatim(text) + "\n\n"
        f"Captured as a direct thought — no source URL exists. Full text: `{raw_path}`.\n"
    )


def _archive(title: str, text: str) -> str:
    return f"# {title}\n\nCaptured directly, {date.today().isoformat()}. Unedited.\n\n{text}\n"
