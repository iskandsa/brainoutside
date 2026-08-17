"""Deterministic repair of a proposal's bookkeeping, before a human sees it.

Rules 1, 2, 5 and 7 mostly police *filing*: does the filename match the
`id`, does the folder match the `type`, is there an INDEX line, does it end
with the right path, is `last-verified` bumped, is the action `create` or
`update`. None of that is a judgement about the idea being fed. All of it
was, until now, handed to the operator as a wall of violations with a raw
YAML textarea to fix them in — which is why four of this brain's first five
feeds sat unapprovable for four days.

Everything here is mechanical and derived from the proposal itself or the
current repo. Nothing invents content: no note text is written, no topic is
guessed, no provenance is fabricated. Where a fix would require judgement
(unparseable frontmatter, a topic outside the taxonomy, a missing `source`)
this pass leaves it alone and the operator still decides.

Each repair returns a human-readable line. They are shown on the feed so
the machine's tidying is visible rather than silent — a proposal that was
altered and does not say so is worse than one that refused.
"""
from __future__ import annotations

import re
from datetime import date

from . import validator

# knowledge/<folder>/ per note type. The plural folder is the contract's.
_TYPE_FOLDER = {"take": "takes", "story": "stories", "lesson": "lessons", "fact": "facts"}
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def repair(proposal: dict, ctx: validator.ValidationContext) -> tuple[dict, list[str]]:
    """Return (repaired proposal, notes). The input dict is not mutated."""
    p = _deepish_copy(proposal)
    notes: list[str] = []
    _fix_paths(p, notes)
    _fix_actions(p, ctx, notes)
    _fix_source_url(p, ctx, notes)
    _fix_last_verified(p, notes)
    _fix_index_lines(p, notes)
    return p, notes


def _deepish_copy(proposal: dict) -> dict:
    p = dict(proposal)
    p["files"] = [dict(f) for f in proposal.get("files") or []]
    p["index_lines"] = [dict(l) for l in proposal.get("index_lines") or []]
    return p


def _fix_paths(p: dict, notes: list[str]) -> None:
    """Rule 1: the filename must equal `id`, the folder must equal `type`.

    The `id` wins over the path, not the other way round: `id` is what
    INDEX lines, `supersedes` and every cross-reference point at, so
    renaming the file is the one-place fix and rewriting the id would
    break every reference to it.
    """
    for f in p["files"]:
        path = str(f.get("path", ""))
        if not path.startswith("knowledge/"):
            continue
        fm, _ = validator._split_frontmatter(str(f.get("content", "")))
        if not fm:
            continue  # judgement call, not filing — leave it
        note_id = str(fm.get("id") or "").strip()
        ntype = str(fm.get("type") or "").strip()
        folder = _TYPE_FOLDER.get(ntype)
        if not note_id or not folder:
            continue
        want = f"knowledge/{folder}/{note_id}.md"
        if want != path:
            f["path"] = want
            notes.append(f"moved {path} → {want} (filename and folder now match id and type)")


def _fix_actions(p: dict, ctx: validator.ValidationContext, notes: list[str]) -> None:
    """Rule 5: `create` on a file that exists, `update` on one that does not.

    Both are the same mistake — the feeder guessing at repo state it could
    not see — and both have exactly one correct answer.
    """
    existing = {e["path"] for e in ctx.entities.values()}
    for f in p["files"]:
        path = str(f.get("path", ""))
        action = str(f.get("action", ""))
        if action not in {"create", "update"}:
            continue
        # raw/ and content-catalog/ are not entities; their existence is not
        # knowable from ctx.entities, so leave those actions untouched.
        if path.startswith(("raw/", "content-catalog/")):
            continue
        if action == "create" and path in existing:
            f["action"] = "update"
            notes.append(f"{path}: create → update (the file already exists)")
        elif action == "update" and path not in existing:
            f["action"] = "create"
            notes.append(f"{path}: update → create (no such file yet)")


def _fix_source_url(p: dict, ctx: validator.ValidationContext, notes: list[str]) -> None:
    """Rule 2: carry the capture's URL onto notes that dropped it.

    Only ever copies a URL the capture actually had. When the capture had
    none there is nothing to carry and nothing to fix — that is a direct
    thought, and `source` is its provenance.
    """
    url = (ctx.captured_source_url or "").strip()
    if not url:
        return
    for f in p["files"]:
        if not str(f.get("path", "")).startswith("knowledge/"):
            continue
        content = str(f.get("content", ""))
        fm, _ = validator._split_frontmatter(content)
        if not fm or str(fm.get("source_url") or "").strip():
            continue
        if "source_url:" in content.split("\n---", 2)[0]:
            new = re.sub(r"(?m)^source_url:.*$", f"source_url: {url}", content, count=1)
        else:
            new = re.sub(r"(?m)^(source:.*)$", rf"\1\nsource_url: {url}", content, count=1)
        if new != content:
            f["content"] = new
            notes.append(f"{f['path']}: filled source_url from the capture")


def _fix_last_verified(p: dict, notes: list[str]) -> None:
    """Compiler rule 7: a touched project card gets today's `last-verified`.

    Pure bookkeeping, and the one the operator can least afford to do by
    hand — a card whose date is stale reads as current to every consumer
    for 45 days.
    """
    today = date.today().isoformat()
    for f in p["files"]:
        path = str(f.get("path", ""))
        if not path.startswith("projects/"):
            continue
        content = str(f.get("content", ""))
        fm, _ = validator._split_frontmatter(content)
        if not fm:
            continue
        if str(fm.get("last-verified") or "") == today:
            continue
        if "last-verified:" in content.split("\n---", 2)[0]:
            new = re.sub(r"(?m)^last-verified:.*$", f"last-verified: {today}", content, count=1)
        else:
            new = re.sub(r"(?m)^(id:.*)$", rf"\1\nlast-verified: {today}", content, count=1)
        if new != content:
            f["content"] = new
            notes.append(f"{path}: last-verified set to {today}")


def _fix_index_lines(p: dict, notes: list[str]) -> None:
    """Rule 7, both directions: drop lines that index nothing, add missing ones.

    `raw/` is never indexed (contract §4) and archives carry no frontmatter,
    so an INDEX line for one can never be satisfied. Feed 5 died on exactly
    that: four sound lessons refused because of a sixth line describing an
    archive that was never meant to be listed.
    """
    entity_paths = validator.proposed_entity_paths(p)
    by_id = {}
    for f in p["files"]:
        fm, _ = validator._split_frontmatter(str(f.get("content", "")))
        if fm and str(fm.get("id") or "").strip():
            by_id[str(fm["id"]).strip()] = (str(f.get("path", "")), fm, str(f.get("content", "")))

    kept = []
    for entry in p["index_lines"]:
        eid = str(entry.get("entity_id") or "")
        if eid in entity_paths:
            kept.append(entry)
            continue
        path = validator.index_line_path(str(entry.get("line", "")))
        why = "raw/ is never indexed" if path.startswith("raw/") else "no such entity in this proposal"
        notes.append(f"dropped INDEX line for {eid or path!r} — {why}")
    p["index_lines"] = kept

    have = {str(e.get("entity_id") or "") for e in kept}
    for eid, (path, fm, content) in by_id.items():
        if eid in have:
            continue
        kind = str(fm.get("kind") or fm.get("type") or "entity")
        heading = _HEADING_RE.search(content)
        title = heading.group(1) if heading else eid
        p["index_lines"].append({"entity_id": eid, "line": f"- [{kind}] {title} | {path}"})
        notes.append(f"added the missing INDEX line for {eid}")
