"""Proposal validator — rules 1–8 as pure functions (PLAN.md §4, M2.3).

Runs on every feed proposal pre-commit: after extraction (annotating the
approval UI), after every human edit (M2.4 re-validate), and as the last
gate before the M2.5 commit. Note rules apply to `knowledge/` only
(grill B8); other paths get their own per-kind checks.

Pure core: `validate(proposal, ctx)` touches no DB and no filesystem —
the caller builds a `ValidationContext` (taxonomy and blocked scripts
from the clone's CLAUDE.md, the entity index, private file texts) via
`context_from_repo()`.

Rule 6 is **owner policy, not engine policy**: it blocks nothing until
the brain's own CLAUDE.md asks for it. See `BLOCKABLE_SCRIPTS`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# Folder ↔ note type mapping (mirror of the indexer's).
KNOWLEDGE_TYPES = {"takes": "take", "stories": "story", "lessons": "lesson", "facts": "fact"}
VISIBILITIES = {"public", "agents-only", "private"}
NOTE_STATUSES = {"current", "superseded"}
# Proposals may only touch content surfaces. INDEX.md and CLAUDE.md are
# edited by trusted code (index_lines / taxonomy_additions); identity/,
# lenses/, eval/ and infrastructure are never feed-written.
ALLOWED_PREFIXES = ("knowledge/", "projects/", "content-catalog/", "raw/")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

#: Rule 6's vocabulary: scripts a brain may ask the validator to reject,
#: by name. **Empty by default** — this used to be a hardcoded Arabic
#: ban, which is a fact about one owner's corpus, not about the engine,
#: and it silently rejected a stranger's notes on a rule they could not
#: see or change. (The template's contract text was generalised for
#: exactly this reason; the server never followed.) A brain opts in via
#: its own CLAUDE.md — see `blocked_scripts_from_claude_md`.
#:
#: Ranges are deliberately coarse. The question is "does this file
#: contain text in that script at all", not "what language is this".
BLOCKABLE_SCRIPTS: dict[str, str] = {
    # Arabic, Arabic Supplement, Arabic Extended-A, presentation forms A+B
    # — byte-for-byte the ranges the hardcoded rule used.
    "arabic": "[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]",
    "hebrew": "[֐-׿יִ-ﭏ]",
    "cyrillic": "[Ѐ-ԯ]",
    "greek": "[Ͱ-Ͽἀ-῿]",
    "devanagari": "[ऀ-ॿ]",
    "thai": "[฀-๿]",
    "han": "[㐀-䶿一-鿿]",
    "hiragana": "[぀-ゟ]",
    "katakana": "[゠-ヿ]",
    "hangul": "[ᄀ-ᇿ가-힯]",
}
_SCRIPT_RES = {name: re.compile(pattern) for name, pattern in BLOCKABLE_SCRIPTS.items()}

_DATE_RE = re.compile(r"^\d{4}-\d{2}$")
_WS_RE = re.compile(r"\s+")
# Minimum normalized-line length considered "quoting" for rule 8 — short
# strings collide by chance, whole sentences don't.
PRIVATE_QUOTE_MIN_CHARS = 30


@dataclass(frozen=True)
class Violation:
    rule: int
    path: str
    message: str
    # Safety violations can NEVER be overridden by the operator: writing
    # outside the repo, leaking private content, emptying a file, or
    # content in a script the brain has explicitly blocked. Everything
    # else is hygiene — either filing the machine should be doing itself,
    # or an editorial call that belongs to the person whose brain this is.
    #
    # The split exists because the gate used to be all-or-nothing, and a
    # single-operator brain with no override means the machine outranks
    # the human. It refused every note its owner wrote for four days.
    safety: bool = False

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"rule {self.rule} [{self.path}]: {self.message}"


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.violations

    @property
    def safety_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.safety]

    @property
    def hygiene_violations(self) -> list[Violation]:
        return [v for v in self.violations if not v.safety]

    @property
    def overridable(self) -> bool:
        """True when nothing but hygiene stands between this and approval.

        Approving on an override is a recorded decision, not a bypass —
        the reason lands in `Feed.decision_note`.
        """
        return not self.safety_violations


@dataclass
class ValidationContext:
    """Everything the pure rules need from the outside world."""

    taxonomy: set[str] = field(default_factory=set)
    # entity_id -> {"path", "visibility", "status"} for the CURRENT repo state.
    entities: dict[str, dict] = field(default_factory=dict)
    # Full text of every `visibility: private` entity (rule 8).
    private_texts: list[str] = field(default_factory=list)
    # The feed's source kind. Kept for reporting; rule 2 no longer branches
    # on it — see `captured_source_url` below for why.
    source_kind: str = ""
    # The URL the CAPTURE actually carried, or "" when it had none.
    #
    # Rule 2 used to waive source_url for the single literal source_kind
    # "thought". Every other kind had to produce a URL. But the write doors
    # (MCP, API) accept any kind string at all, and the ops form does not
    # even offer "thought" for most captures — so notes arrived labelled
    # "note", "doc", "transcript" and were refused for lacking a web
    # address they never had. Four of the owner's first five feeds died
    # here, and the only one that passed was the one with a real URL.
    #
    # Worse, the rule was unsatisfiable in principle: rule 2 demanded a
    # URL while compiler rule 5 forbids inventing anything not in the
    # source. The only legal move was to fabricate, which the contract
    # bans. There was no way through by design.
    #
    # The rule the contract actually states (§5 rule 2) is that provenance
    # is mandatory — `source` — and that a URL is one form of it, valid
    # "ONLY for direct thoughts" to be null. So: `source` stays required
    # unconditionally, and the URL is required exactly when the capture had
    # one to carry. Dropping provenance you were given is still a
    # violation; not having any was never the note's fault.
    captured_source_url: str = ""
    # Rule 6, opted into by the brain's CLAUDE.md. Empty = nothing blocked.
    blocked_scripts: tuple[str, ...] = ()
    # Names the brain asked for that this server does not know. Carried so
    # the approval UI can say so — a typo here silently disarms the rule,
    # and a silently disarmed rule is the failure this whole change is
    # about.
    unknown_scripts: tuple[str, ...] = ()


# ---- context builder (the only impure part, kept at the edge) ------------


def taxonomy_from_claude_md(text: str) -> set[str]:
    """Parse §7's backtick-quoted tag list. Anchored to the heading so a
    stray inline-code span elsewhere never widens the vocabulary."""
    m = re.search(r"^##.*Topic taxonomy.*$", text, re.MULTILINE)
    if not m:
        return set()
    section = text[m.end():]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]
    tags: set[str] = set()
    for span in re.findall(r"`([^`]+)`", section):
        for tok in span.split(","):
            tok = tok.strip()
            if re.fullmatch(r"[a-z0-9][a-z0-9-]*", tok):
                tags.add(tok)
    return tags


#: Only a line that opens with this carries the list. See the docstring.
_SCRIPTS_LINE_RE = re.compile(r"^[ \t]*Scripts:[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)


def blocked_scripts_from_claude_md(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse a `## Blocked scripts` section. Returns (known, unknown).

    Anchored to its own heading like the taxonomy parser above, but the
    list must sit on a line that STARTS with `Scripts:`:

        ## Blocked scripts

        Scripts: `arabic, hebrew`

    Not "any backticks in the section", which is how this was first
    written and which the template immediately broke: that section has
    to name every script the server knows in order to document them, and
    it shows a commented-out example. Harvesting backticks from prose
    would have turned a page of documentation into a blocklist of
    everything, and left `<!-- Scripts: `arabic` -->` switched ON. A
    commented-out line does not start with `Scripts:`, so it stays off.

    Absent section, absent file, or an empty list all mean *block
    nothing* — the right default for a server somebody else is about to
    install. Names this server does not recognise come back separately
    rather than being dropped, so a typo surfaces instead of quietly
    turning the rule off.
    """
    m = re.search(r"^##.*Blocked scripts.*$", text, re.MULTILINE)
    if not m:
        return (), ()
    section = text[m.end():]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]
    known: list[str] = []
    unknown: list[str] = []
    for line in _SCRIPTS_LINE_RE.findall(section):
        for span in re.findall(r"`([^`]+)`", line):
            for tok in span.split(","):
                tok = tok.strip().lower()
                if not tok:
                    continue
                bucket = known if tok in _SCRIPT_RES else unknown
                if tok not in bucket:
                    bucket.append(tok)
    return tuple(known), tuple(unknown)


def context_from_repo() -> ValidationContext:
    from apps.brain.models import Entity
    from apps.brain.services import gitrepo

    repo = gitrepo.repo_dir()
    claude_md = repo / "CLAUDE.md"
    claude_text = (
        claude_md.read_text(encoding="utf-8", errors="replace") if claude_md.is_file() else ""
    )
    taxonomy = taxonomy_from_claude_md(claude_text)
    blocked, unknown = blocked_scripts_from_claude_md(claude_text)
    entities: dict[str, dict] = {}
    private_texts: list[str] = []
    for e in Entity.objects.all():
        entities[e.entity_id] = {"path": e.path, "visibility": e.visibility, "status": e.status}
        if e.visibility == "private":
            p = repo / e.path
            if p.is_file():
                private_texts.append(p.read_text(encoding="utf-8", errors="replace"))
    return ValidationContext(
        taxonomy=taxonomy,
        entities=entities,
        private_texts=private_texts,
        blocked_scripts=blocked,
        unknown_scripts=unknown,
    )


# ---- helpers -------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, text
    return (fm if isinstance(fm, dict) else None), text[m.end():]


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", s).strip().lower()


def index_line_path(line: str) -> str:
    """The path an INDEX line points at — its last `|`-separated segment.

    `approval._apply_index_lines` reads the line exactly this way to pick
    which existing INDEX.md line to overwrite, so the validator has to
    read it identically or the two disagree about what a proposal does.
    A line with no `|` at all yields the whole line, which matches no
    real path and is therefore rejected — which is the point.
    """
    return str(line).rstrip().rsplit("|", 1)[-1].strip()


def proposed_entity_paths(proposal: dict) -> dict[str, str]:
    """entity_id -> path for every proposal file carrying frontmatter.

    One definition because three callers need the same answer: the
    feeder's index-line normaliser, rule 7 below, and the apply-time
    guard in `approval._apply_index_lines`.
    """
    out: dict[str, str] = {}
    for f in proposal.get("files") or []:
        fm, _ = _split_frontmatter(str(f.get("content", "")))
        if fm and str(fm.get("id") or "").strip():
            out[str(fm["id"]).strip()] = str(f.get("path", ""))
    return out


def _as_list(v: object) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in v.strip("[]").split(",") if s.strip()]
    return []


# ---- the rules -----------------------------------------------------------


def validate(proposal: dict, ctx: ValidationContext) -> ValidationResult:
    res = ValidationResult()
    files = proposal.get("files") or []
    index_lines = proposal.get("index_lines") or []
    supersedes = proposal.get("supersedes") or []
    taxonomy_additions = [str(t) for t in (proposal.get("taxonomy_additions") or [])]
    allowed_topics = ctx.taxonomy | set(taxonomy_additions)
    index_by_entity = {str(l.get("entity_id", "")): str(l.get("line", "")) for l in index_lines}
    proposed_note_ids: set[str] = set()

    if taxonomy_additions:
        res.warnings.append(
            "proposes NEW taxonomy tags (contract §7 — extend deliberately): "
            + ", ".join(taxonomy_additions)
        )
    if ctx.unknown_scripts:
        # Said out loud where the operator already is. A misspelled name
        # under "Blocked scripts" disarms rule 6 for that script, and a
        # silently disarmed rule is worse than no rule.
        res.warnings.append(
            "CLAUDE.md lists scripts this server does not know, so they are "
            f"NOT blocked: {', '.join(ctx.unknown_scripts)}. Known names: "
            + ", ".join(sorted(BLOCKABLE_SCRIPTS))
        )

    for f in files:
        path = str(f.get("path", ""))
        action = str(f.get("action", ""))
        content = str(f.get("content", ""))

        # -- rule 5: no deletions (and only content surfaces are writable) --
        if action not in {"create", "update"}:
            res.violations.append(Violation(5, path, f"unknown action {action!r} — only create/update exist"))
            continue
        if ".." in path or path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
            res.violations.append(Violation(5, path, "path escapes the repo", safety=True))
            continue
        if not path.startswith(ALLOWED_PREFIXES):
            res.violations.append(
                Violation(5, path, f"proposals may only touch {', '.join(ALLOWED_PREFIXES)}", safety=True)
            )
            continue
        if not content.strip():
            res.violations.append(
                Violation(5, path, "empty content — emptying a file is a deletion", safety=True)
            )
            continue
        if action == "update" and not any(e["path"] == path for e in ctx.entities.values()) and not path.startswith(("raw/", "content-catalog/")):
            res.violations.append(Violation(5, path, "update targets a file that does not exist"))
        if action == "create" and any(e["path"] == path for e in ctx.entities.values()):
            res.violations.append(Violation(5, path, "create targets a file that already exists — use update"))

        # -- rule 6: no content in a script this brain blocks --
        for script in ctx.blocked_scripts:
            if _SCRIPT_RES[script].search(content):
                res.violations.append(
                    Violation(
                        6, path,
                        f"{script}-script content — your CLAUDE.md lists "
                        f"{script} under Blocked scripts",
                        safety=True,
                    )
                )

        # -- per-kind frontmatter (rules 1-4) --
        if path.startswith("knowledge/"):
            _validate_note(path, content, allowed_topics, res, proposed_note_ids, ctx)
        elif path.startswith("projects/"):
            _validate_project_card(path, content, res)
        # content-catalog/ and raw/ carry no frontmatter schema.

        # -- rule 7: index line exists + agrees (all entity files) --
        if path.startswith(("knowledge/", "projects/")):
            _validate_index_agreement(path, content, index_by_entity, res)

    # -- rule 7, the other direction --
    #
    # Rule 7 above asks "does every proposed FILE have an index line".
    # Nothing asked the reverse, and the reverse is where the damage is:
    # `diffview.build` renders one diff per proposed FILE and never shows
    # INDEX.md at all, so an index line naming some unrelated entity is
    # invisible to the operator approving the feed — and
    # `_apply_index_lines` overwrites the first existing line whose text
    # ends with that path. The degenerate case is worse still: a line
    # ending in `|` extracts an EMPTY path, and `endswith("")` is true for
    # every line, so it silently clobbers whichever entity happens to be
    # listed first. `feeder.normalize_index_lines` already declines to
    # recompose an entry with no proposed file, deferring to "the
    # validator judges it" — this is that judgement.
    proposed_paths = proposed_entity_paths(proposal)
    for entry in index_lines:
        entity_id = str(entry.get("entity_id", "")).strip()
        line = str(entry.get("line", ""))
        target = proposed_paths.get(entity_id)
        if target is None:
            res.violations.append(
                Violation(
                    7,
                    entity_id or "?",
                    "index line names no file in this proposal — INDEX.md is "
                    "not rendered in the approval diff, so this would be "
                    "committed unreviewed",
                )
            )
            continue
        found = index_line_path(line)
        if found != target:
            res.violations.append(
                Violation(
                    7,
                    target,
                    f"index line must end with `| {target}`; it points at "
                    f"{found!r}, which is the line the commit would overwrite",
                )
            )

    # -- supersedes are declarative; check both ends --
    for s in supersedes:
        old = str(s.get("old_entity_id", ""))
        new = str(s.get("new_entity_id", ""))
        if old not in ctx.entities:
            res.violations.append(Violation(1, old, "supersedes a note that does not exist"))
        if new not in proposed_note_ids:
            res.violations.append(
                Violation(1, new, "superseded_by points at a note this proposal does not create")
            )

    # -- rule 8: no quoting from private entities --
    all_content = _norm("\n".join(str(f.get("content", "")) for f in files))
    for text in ctx.private_texts:
        for line in text.splitlines():
            n = _norm(line)
            if len(n) >= PRIVATE_QUOTE_MIN_CHARS and n in all_content:
                res.violations.append(
                    Violation(
                        8, "", f"proposal quotes private content: “{line.strip()[:80]}…”", safety=True
                    )
                )
                break  # one violation per private file is enough signal

    return res


def _validate_note(path, content, allowed_topics, res, proposed_note_ids, ctx):
    parts = path.split("/")
    folder = parts[1] if len(parts) == 3 else ""
    expected_type = KNOWLEDGE_TYPES.get(folder)
    if expected_type is None or not path.endswith(".md"):
        res.violations.append(Violation(1, path, "notes live at knowledge/<takes|stories|lessons|facts>/<id>.md"))
        return
    fm, body = _split_frontmatter(content)
    if fm is None:
        res.violations.append(Violation(1, path, "missing or unparseable YAML frontmatter"))
        return

    stem = parts[-1][:-3]
    note_id = str(fm.get("id") or "")
    proposed_note_ids.add(note_id)
    if note_id != stem:
        res.violations.append(Violation(1, path, f"id {note_id!r} ≠ filename {stem!r}"))
    ntype = str(fm.get("type") or "")
    if ntype != expected_type:
        res.violations.append(Violation(1, path, f"type {ntype!r} ≠ folder type {expected_type!r}"))
    status = str(fm.get("status") or "")
    if status not in NOTE_STATUSES:
        res.violations.append(Violation(1, path, f"status must be current|superseded, got {status!r}"))
    if status == "superseded" and not fm.get("superseded_by"):
        res.violations.append(Violation(1, path, "status superseded requires superseded_by"))
    vis = str(fm.get("visibility") or "")
    if vis not in VISIBILITIES:
        res.violations.append(Violation(1, path, f"visibility must be one of {sorted(VISIBILITIES)}, got {vis!r}"))
    date = str(fm.get("date") or "")
    if not _DATE_RE.match(date):
        res.warnings.append(f"{path}: date should be YYYY-MM, got {date!r}")

    # -- rule 2: provenance is mandatory --
    if not str(fm.get("source") or "").strip():
        res.violations.append(Violation(2, path, "source missing — no note without provenance"))
    if not str(fm.get("source_url") or "").strip() and ctx.captured_source_url:
        # A URL is owed only when the capture carried one. Dropping
        # provenance that was handed to you is the real failure; having
        # none to begin with is what a direct thought looks like, and
        # `source` (the feed id / raw archive) is its provenance.
        res.violations.append(
            Violation(2, path, "source_url missing — the capture carried one; put it on the note")
        )

    # -- rule 3: topics ⊆ taxonomy --
    topics = _as_list(fm.get("topics"))
    if not topics:
        res.violations.append(Violation(3, path, "at least one topic tag required"))
    for t in topics:
        if t not in allowed_topics:
            res.violations.append(Violation(3, path, f"topic {t!r} is not in the CLAUDE.md taxonomy"))
    if len(topics) > 4:
        res.warnings.append(f"{path}: {len(topics)} topics (contract says 1-4)")

    # -- rule 4: voice is sacred --
    if ntype in {"take", "story"} and "> VERBATIM:" not in body:
        res.violations.append(Violation(4, path, "takes/stories must carry a `> VERBATIM:` quote"))


def _validate_project_card(path, content, res):
    fm, _body = _split_frontmatter(content)
    if fm is None:
        res.violations.append(Violation(1, path, "project card missing or unparseable YAML frontmatter"))
        return
    if not str(fm.get("id") or "").strip():
        res.violations.append(Violation(1, path, "project card requires an id"))
    lv = str(fm.get("last-verified") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", lv):
        res.violations.append(
            Violation(1, path, "touched project cards must update last-verified (YYYY-MM-DD, compiler rule 7)")
        )


def _validate_index_agreement(path, content, index_by_entity, res):
    """Rule 7 — the line must exist and agree on status + visibility.

    Non-public entities MUST carry an explicit `visibility:` token in
    their line: the silent-omission form is exactly the voice.md mismatch
    the old rule missed (grill B5/B7)."""
    fm, _ = _split_frontmatter(content)
    if fm is None:
        return  # rule 1 already fired
    entity_id = str(fm.get("id") or "")
    line = index_by_entity.get(entity_id, "")
    if not line:
        res.violations.append(Violation(7, path, f"no index line proposed for {entity_id!r}"))
        return
    if not line.rstrip().endswith(path):
        res.violations.append(Violation(7, path, "index line does not end with the entity's path"))

    if path.startswith("knowledge/"):
        status = str(fm.get("status") or "current")
        if f"status: {status}" not in line:
            res.violations.append(
                Violation(7, path, f"index line disagrees with frontmatter status {status!r}")
            )

    vis = str(fm.get("visibility") or "agents-only")
    m = re.search(r"visibility:\s*([a-z-]+)", line)
    line_vis = m.group(1) if m else ""
    if vis != "public" and line_vis != vis:
        res.violations.append(
            Violation(7, path, f"non-public entity must carry `visibility: {vis}` in its index line")
        )
    elif vis == "public" and line_vis not in ("", "public"):
        res.violations.append(
            Violation(7, path, f"index line claims visibility {line_vis!r} but frontmatter says public")
        )


# ---- orchestrator --------------------------------------------------------


def validate_feed(feed) -> ValidationResult:
    """Validate a Feed's stored proposal against the current repo state."""
    if not feed.proposal:
        res = ValidationResult()
        res.warnings.append("no proposal to validate yet")
        return res
    ctx = context_from_repo()
    ctx.source_kind = str((feed.raw_payload or {}).get("source_kind") or "")
    ctx.captured_source_url = str((feed.raw_payload or {}).get("source_url") or "").strip()
    return validate(feed.proposal, ctx)
