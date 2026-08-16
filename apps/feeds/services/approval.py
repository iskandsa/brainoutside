"""Approval handler — the ONLY code that writes the brain repo (M2.5).

Runs on the Q2 worker (the write PAT exists only there — grill C13).
One approval = the locked sequence from PLAN.md §4:

    lock → sync to origin → apply proposal → validate → commit
    `feed: <source-id>` → push → (unlock) → reindex + rebuild snapshots

Push rejection is a ROUTINE race with a local push, not a failure: the
whole locked sequence retries up to MAX_PUSH_ATTEMPTS — the proposal is
declarative (full file contents + index/supersede ops), so replay after
a fresh sync is safe (grill C20). Any terminal failure rolls the tree
back to origin and marks the Feed failed; the working tree is never left
dirty.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from django.conf import settings as dj_settings
from django.utils import timezone

from apps.brain.services import gitrepo
from apps.events.models import emit
from apps.feeds.models import OVERRIDE_NOTE_PREFIX, Feed
from apps.feeds.services import validator

log = logging.getLogger(__name__)

MAX_PUSH_ATTEMPTS = 3
# Commit identity comes from settings (BRAIN_COMMIT_NAME/EMAIL) — engine
# code carries no personal defaults (OPEN-SOURCE.md 5.1).

# INDEX.md section per path prefix, for appending brand-new entity lines.
_INDEX_SECTIONS = (
    ("knowledge/", "## Knowledge"),
    ("projects/", "## Projects"),
    ("content-catalog/", "## Content"),
)


class ApplyFailure(RuntimeError):
    """Terminal apply problem — rolls back and marks the Feed failed."""


class StaleReview(RuntimeError):
    """The brain moved under the diff the operator approved.

    Not a failure of the proposal — the review is simply out of date, so
    this sends the Feed back to `pending` rather than `failed`.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = sorted(paths)
        shown = ", ".join(self.paths[:5])
        if len(self.paths) > 5:
            shown += f", +{len(self.paths) - 5} more"
        super().__init__(
            f"the brain changed {shown} while this approval was in flight. "
            "Applying now would overwrite that change with the version you "
            "reviewed, so the commit would not match the diff you approved. "
            "The diff below is recomputed against the brain as it stands — "
            "check it and approve again."
        )


# ---- write credential (worker-only, grill C13) ---------------------------


def _write_pat() -> str:
    """The push credential: env → mounted file → encrypted setting.

    Kept as a thin wrapper rather than calling `gitcreds` at each use
    site, so this module stays the ONLY place in the codebase that reads
    the write credential (grill C13's container boundary is gone once the
    PAT can live in the DB — this code-level one replaces it).
    """
    from apps.brain.services import gitcreds

    return gitcreds.write_pat()


def _tokenized_origin_url(pat: str) -> str:
    """One-shot tokenized https URL for origin (never written to git config)."""
    origin = gitrepo.run("remote", "get-url", "origin")
    m = re.match(r"^git@([^:]+):(.+?)(\.git)?$", origin) or re.match(
        r"^(?:ssh|https?)://(?:[^@/]+@)?([^/]+)/(.+?)(\.git)?$", origin
    )
    if not m:
        raise ApplyFailure(f"cannot derive an https push URL from origin {origin!r}")
    host, repo_path = m.group(1), m.group(2)
    return f"https://x-access-token:{pat}@{host}/{repo_path}.git"


def _push_target(branch: str) -> list[str]:
    """`git push` argv tail. With a PAT: tokenized https. Without: plain
    origin (dev)."""
    pat = _write_pat()
    if not pat:
        return ["origin", f"HEAD:{branch}"]
    return [_tokenized_origin_url(pat), f"HEAD:{branch}"]


def _fetch_args(branch: str) -> list[str]:
    """`git fetch` argv tail for the replay-safe reset. Plain origin works
    in prod (SSH deploy key via GIT_SSH_COMMAND); a private https origin
    with no key — the local stack — has no read credential in the worker,
    so with a PAT we fetch through the tokenized URL, explicitly updating
    the same origin/<branch> tracking ref that reset/rollback depend on."""
    pat = _write_pat()
    if not pat:
        return ["origin"]
    try:
        url = _tokenized_origin_url(pat)
    except ApplyFailure:
        return ["origin"]
    return [url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]


# ---- proposal application (plain file edits, no agent) -------------------


def _safe_target(repo: Path, relpath: str) -> Path:
    """Resolve a proposal path inside the clone, or refuse to touch it.

    This is the write door's last containment check, and until now it was a
    string prefix test, which is not containment. With the clone at
    `/data/brain-repo`, `../brain-repo-backup/x.md` resolves to
    `/data/brain-repo-backup/x.md` — a different directory that happens to
    start with the same characters — and it passed. Any sibling whose name
    merely EXTENDS the clone's was writable by an approved proposal.

    `.git/` is the second hole: it lives inside the boundary, so the prefix
    test was happy to write it, and a proposal that lands `.git/config` or
    `.git/hooks/pre-commit` turns the very next git call in this module
    into arbitrary code execution on the worker.

    The rules 5 checks in `validator` cover both shapes, but they cannot be
    relied on here: the validator is a gate, and a gate has a lock on the
    other side of it for the day it is bypassed, reordered, or edited.
    """
    root = repo.resolve()
    target = (repo / relpath).resolve()
    if target == root or not target.is_relative_to(root):
        raise ApplyFailure(f"path escapes the repo: {relpath!r}")
    # Case-folded: on Windows and macOS `.GIT/config` IS `.git/config`.
    if any(part.lower() == ".git" for part in target.relative_to(root).parts):
        raise ApplyFailure(f"proposals may not write git internals: {relpath!r}")
    return target


def _file_bytes(f: dict) -> str:
    """Exactly what `_apply_files` will write for this entry.

    Shared with the staleness check so the two cannot drift: "would
    applying this change anything?" has to be asked about the same text
    that would actually land.
    """
    content = str(f.get("content", ""))
    return content if content.endswith("\n") else content + "\n"


def _apply_files(repo: Path, proposal: dict) -> None:
    for f in proposal.get("files") or []:
        target = _safe_target(repo, str(f.get("path", "")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_file_bytes(f), encoding="utf-8", newline="\n")


def _apply_index_lines(repo: Path, proposal: dict) -> None:
    index = repo / "INDEX.md"
    lines = index.read_text(encoding="utf-8").splitlines()
    # An index line may only describe a file this proposal carries. The
    # validator enforces the same rule, but this loop OVERWRITES the first
    # existing line ending with the extracted path, and INDEX.md is not in
    # the approval diff — so an unchecked entry rewrites an unrelated
    # entity's line with nobody seeing it. The empty-path case (a line
    # ending in `|`) is the sharp one: `endswith("")` matches every line,
    # so it clobbers whichever entity is listed first.
    targets = set(validator.proposed_entity_paths(proposal).values())
    for entry in proposal.get("index_lines") or []:
        new_line = str(entry.get("line", "")).rstrip()
        path = validator.index_line_path(new_line)
        if path not in targets:
            raise ApplyFailure(
                f"index line points at {path!r}, which is not a file this "
                f"proposal carries: {new_line!r}"
            )
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith("- ") and line.rstrip().endswith(path):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            section = next((s for pfx, s in _INDEX_SECTIONS if path.startswith(pfx)), None)
            at = len(lines)
            if section is not None:
                try:
                    start = next(i for i, l in enumerate(lines) if l.startswith(section))
                    at = next(
                        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
                        len(lines),
                    )
                    while at > start + 1 and not lines[at - 1].strip():
                        at -= 1
                except StopIteration:
                    at = len(lines)
            lines.insert(at, new_line)
    index.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _apply_supersedes(repo: Path, proposal: dict, entities: dict[str, dict]) -> None:
    """Trusted-code frontmatter edit: old note → superseded (+ INDEX token)."""
    index = repo / "INDEX.md"
    index_text = index.read_text(encoding="utf-8")
    for s in proposal.get("supersedes") or []:
        old_id = str(s.get("old_entity_id", ""))
        new_id = str(s.get("new_entity_id", ""))
        info = entities.get(old_id)
        if info is None:
            raise ApplyFailure(f"supersede target {old_id!r} not in the entity index")
        target = _safe_target(repo, info["path"])
        text = target.read_text(encoding="utf-8")
        m = validator._FRONTMATTER_RE.match(text)
        if not m:
            raise ApplyFailure(f"{info['path']}: no frontmatter to mark superseded")
        fm = m.group(0)
        fm = re.sub(r"^status:.*$", "status: superseded", fm, count=1, flags=re.MULTILINE)
        if re.search(r"^superseded_by:", fm, flags=re.MULTILINE):
            fm = re.sub(r"^superseded_by:.*$", f"superseded_by: {new_id}", fm, count=1, flags=re.MULTILINE)
        else:
            fm = fm.replace("\n---", f"\nsuperseded_by: {new_id}\n---", 1)
        target.write_text(fm + text[m.end():], encoding="utf-8", newline="\n")
        # Keep the old note's INDEX line truthful too.
        index_text = "\n".join(
            line.replace("status: current", "status: superseded")
            if line.startswith("- ") and line.rstrip().endswith(info["path"])
            else line
            for line in index_text.splitlines()
        ) + "\n"
    index.write_text(index_text, encoding="utf-8", newline="\n")


def _apply_taxonomy(repo: Path, proposal: dict) -> None:
    """Contract §7: extending the taxonomy edits CLAUDE.md in the same commit."""
    additions = [str(t) for t in (proposal.get("taxonomy_additions") or [])]
    if not additions:
        return
    claude = repo / "CLAUDE.md"
    text = claude.read_text(encoding="utf-8")
    m = re.search(r"^##.*Topic taxonomy.*$", text, re.MULTILINE)
    if not m:
        raise ApplyFailure("CLAUDE.md has no Topic taxonomy section to extend")
    section_end = re.search(r"^## ", text[m.end():], re.MULTILINE)
    end = m.end() + (section_end.start() if section_end else len(text) - m.end())
    spans = list(re.finditer(r"`([^`]+)`", text[m.end():end]))
    if not spans:
        raise ApplyFailure("CLAUDE.md taxonomy list (backtick span) not found")
    last = spans[-1]
    existing = {t.strip() for t in last.group(1).split(",")}
    new_tags = [t for t in additions if t not in existing]
    if not new_tags:
        return
    abs_end = m.end() + last.end(1)
    text = text[:abs_end] + ", " + ", ".join(new_tags) + text[abs_end:]
    claude.write_text(text, encoding="utf-8", newline="\n")


# ---- the locked sequence -------------------------------------------------


def _rollback(branch: str) -> str:
    """Return the clone to pristine origin state. Never raises.

    Returns "" when the tree came back clean, else `git status --porcelain`
    naming what survived. A rollback that quietly failed is how content
    nobody approved reaches the public API: `indexer.rebuild()` walks the
    WORKING TREE, not the commit, so a leftover file is indexed, copied
    into a tier snapshot, and served.
    """
    for args in (("rebase", "--abort"), ("reset", "--hard", f"origin/{branch}"), ("clean", "-fd")):
        try:
            gitrepo.run(*args)
        except gitrepo.BrainRepoError:
            pass
    try:
        return gitrepo.run("status", "--porcelain")
    except gitrepo.BrainRepoError as exc:  # pragma: no cover - defensive
        return f"could not verify the rollback: {exc}"


def _dirty_note(leftover: str) -> str:
    """Failure-message suffix when the clone did not come back clean."""
    if not leftover:
        return ""
    lines = leftover.splitlines()
    shown = "; ".join(lines[:5])
    if len(lines) > 5:
        shown += f"; +{len(lines) - 5} more"
    return (
        f" [ROLLBACK INCOMPLETE — the clone still holds: {shown}. "
        "Nothing here was committed, but the next sync indexes the working "
        "tree and will serve it. Clean the clone before re-approving.]"
    )


def _push(branch: str) -> None:
    gitrepo.run("push", *_push_target(branch), timeout=120)


def landed_commit(feed_pk: int, branch: str) -> str:
    """SHA of the commit on origin carrying this Feed's trailer, or "".

    Every approval commits `Feed-Id: <pk>` as a trailer, and until now
    nothing ever read it back. That trailer is the only durable link
    between a Feed row and the brain's history, and it is what makes
    "did my push actually land?" answerable at all.

    It has to be answerable, because a push can succeed on the server and
    still report failure to the client — a dropped connection after the
    ref moved. The retry then replays onto a tree that already contains
    the proposal, `git commit` says "nothing to commit", that reads as a
    push failure, and after MAX_PUSH_ATTEMPTS the Feed is marked `failed`
    with an empty `commit_hash` while its content is live in the brain.

    `--grep` is an unanchored coarse filter (so `Feed-Id: 12` also
    matches feed 123); the exact per-line comparison below is what
    decides. Being generous in the filter and strict in the check is the
    right way round — a missed match here would resurrect the bug.
    """
    try:
        out = gitrepo.run(
            "log",
            f"origin/{branch}",
            "--fixed-strings",
            f"--grep=Feed-Id: {feed_pk}",
            "--format=%H%x00%B%x1e",
        )
    except gitrepo.BrainRepoError:  # no such ref yet, or an unreadable log
        return ""
    for record in out.split("\x1e"):
        sha, _, body = record.strip("\n").partition("\x00")
        if not sha.strip():
            continue
        if any(line.strip() == f"Feed-Id: {feed_pk}" for line in body.splitlines()):
            return sha.strip()
    return ""


def _changed_between(old: str, new: str) -> set[str]:
    """Repo-relative paths whose content differs between two commits."""
    out = gitrepo.run("diff", "--name-only", old, new)
    return {line.strip() for line in out.splitlines() if line.strip()}


def _assert_review_still_holds(repo: Path, reviewed_at: str, touched: dict[str, str]) -> None:
    """Refuse to apply a reviewed diff onto a brain that has moved under it.

    Every attempt starts with `reset --hard origin/<branch>` and then
    re-applies FULL file contents. That is what makes push-race replay
    safe for the proposal — and what silently reverted anything upstream
    had done to the same file in the meantime. The operator approved a
    diff `diffview.build` computed against the clone at review time; the
    commit that landed was a different one, and nothing said so.

    The same window exists without any push race: the clone advances on
    every sync, so a brain edit pushed between the review and the
    worker's run is reverted by the very first attempt.

    Only `files` paths are checked. INDEX.md and CLAUDE.md are edited by
    `_apply_index_lines` / `_apply_taxonomy`, which re-read whatever is
    there now and merge into it, so an upstream change to those survives.

    A path that moved to EXACTLY the bytes this proposal would write is
    not a conflict — applying it changes nothing, and there is nothing
    for the operator to re-review. That case is real: it is what a
    replay after a lost push looks like, and what happens when the same
    content is committed by hand.

    Reading `repo / path` needs no containment guard: `path` is in the
    intersection of the proposal's paths with git's own changed-file
    list, so it is a real repo-relative path by construction.
    """
    if not touched:
        return
    conflicts = []
    for path in sorted(touched.keys() & _changed_between(reviewed_at, "HEAD")):
        target = repo / path
        try:
            current = target.read_text(encoding="utf-8") if target.is_file() else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current != touched[path]:
            conflicts.append(path)
    if conflicts:
        raise StaleReview(conflicts)


def apply_feed(feed_id: int) -> str:
    """Q2 task entry: perform the approval for a claimed (`approving`) Feed."""
    feed = Feed.objects.filter(pk=feed_id).first()
    if feed is None:
        return f"feed {feed_id} gone"
    if feed.status != "approving":
        return f"feed {feed_id} is {feed.status} — not applying"
    if not feed.proposal:
        _finalize_failed(feed, "no proposal to apply")
        return "no proposal"

    proposal = feed.proposal
    # path -> the exact text this proposal would write there.
    touched = {
        str(f.get("path", "")): _file_bytes(f)
        for f in (proposal.get("files") or [])
        if f.get("path")
    }
    attempts = 0
    outcome = "committed"
    try:
        with gitrepo.repo_lock():
            branch = gitrepo.run("rev-parse", "--abbrev-ref", "HEAD")
            repo = gitrepo.repo_dir()
            # The clone as the operator's diff was rendered against it.
            reviewed_at = gitrepo.run("rev-parse", "HEAD")
            while True:
                attempts += 1
                # Start every attempt from exact origin state — replay-safe.
                gitrepo.run("fetch", *_fetch_args(branch), timeout=120)
                gitrepo.run("reset", "--hard", f"origin/{branch}")
                gitrepo.run("clean", "-fd")
                # Is this feed already in the brain? A push can succeed on
                # the server and still report failure to the client, and
                # the replay that follows finds nothing to commit — which
                # used to burn all three attempts and then mark the Feed
                # `failed` with an empty commit_hash while its content was
                # live. The trailer says otherwise, so ask it first.
                landed = landed_commit(feed.pk, branch)
                if landed:
                    commit, outcome = landed, "already-landed"
                    log.warning(
                        "feed %s: already committed as %s — recording it "
                        "instead of replaying",
                        feed.pk, landed[:12],
                    )
                    break
                _assert_review_still_holds(repo, reviewed_at, touched)
                try:
                    ctx = validator.context_from_repo()
                    ctx.source_kind = str((feed.raw_payload or {}).get("source_kind") or "")
                    # Must match validate_feed(), or the gate the operator
                    # saw and the gate that commits disagree — which is
                    # exactly how feed 5 came to be approved and then fail.
                    ctx.captured_source_url = str(
                        (feed.raw_payload or {}).get("source_url") or ""
                    ).strip()
                    # Validate BEFORE writing anything. `validate` is pure
                    # over (proposal, ctx) and `ctx` is captured from the
                    # pre-apply repo either way, so this is the same answer
                    # the old post-apply call produced — but now the gate
                    # runs on the gate's side of the door. Previously every
                    # invalid proposal was written to the shared clone
                    # first and only `_rollback` took it back out, which
                    # made a correctness check depend on cleanup working.
                    res = validator.validate(proposal, ctx)
                    # Safety is absolute on both sides of the door and is
                    # re-checked here on purpose: the repo can move between
                    # the operator's click and this commit.
                    if res.safety_violations:
                        raise ApplyFailure(
                            "pre-commit safety violation: "
                            + "; ".join(str(v) for v in res.safety_violations[:5])
                        )
                    # Hygiene is the operator's call, and they already made
                    # it — the reason is in decision_note. Refusing again
                    # here would mean the button said yes and the worker
                    # said no, which is how feed 5 landed as `failed` after
                    # a deliberate, reasoned approval. Without a recorded
                    # override, hygiene still blocks.
                    overridden = (feed.decision_note or "").startswith(OVERRIDE_NOTE_PREFIX)
                    if res.hygiene_violations and not overridden:
                        raise ApplyFailure(
                            "pre-commit validation failed: "
                            + "; ".join(str(v) for v in res.hygiene_violations[:5])
                        )
                    if res.hygiene_violations:
                        log.warning(
                            "feed %s: committing over %d hygiene violation(s) on a "
                            "recorded operator override — %s",
                            feed.pk, len(res.hygiene_violations), feed.decision_note[:120],
                        )
                    _apply_files(repo, proposal)
                    _apply_index_lines(repo, proposal)
                    _apply_supersedes(repo, proposal, ctx.entities)
                    _apply_taxonomy(repo, proposal)
                    gitrepo.run("add", "-A")
                    if not gitrepo.run("status", "--porcelain"):
                        # The proposal is already present verbatim, but
                        # under no commit of ours (the trailer check above
                        # came back empty) — somebody committed the same
                        # bytes by hand. `git commit` would exit non-zero
                        # with "nothing to commit", which reads as a push
                        # failure and ends as a bogus `failed`. What the
                        # approval asked for is true, so say so, and point
                        # at the commit that made it true.
                        commit, outcome = gitrepo.run("rev-parse", "HEAD"), "already-present"
                        log.warning(
                            "feed %s: proposal already present at %s — nothing to commit",
                            feed.pk, commit[:12],
                        )
                        break
                    gitrepo.run(
                        "-c", f"user.name={dj_settings.BRAIN_COMMIT_NAME}",
                        "-c", f"user.email={dj_settings.BRAIN_COMMIT_EMAIL}",
                        "commit",
                        "-m", f"feed: {feed.source_id}",
                        "-m", f"Feed-Id: {feed.pk}\nChannel: {feed.channel}",
                    )
                    _push(branch)
                    commit = gitrepo.run("rev-parse", "HEAD")
                    break
                except ApplyFailure as exc:
                    raise ApplyFailure(f"{exc}{_dirty_note(_rollback(branch))}") from exc
                except gitrepo.BrainRepoError as exc:
                    # Push/transport trouble — routine race territory.
                    leftover = _rollback(branch)
                    # A retry re-applies onto whatever is in the tree, so a
                    # rollback that did not fully take ends the attempt loop.
                    if leftover or attempts >= MAX_PUSH_ATTEMPTS:
                        raise ApplyFailure(
                            f"push failed after {attempts} attempt(s): {exc}"
                            f"{_dirty_note(leftover)}"
                        ) from exc
                    log.warning("feed %s: attempt %s failed (%s), retrying", feed.pk, attempts, exc)
                except Exception as exc:
                    # ANYTHING else: an OSError from mkdir over an existing
                    # file, a UnicodeDecodeError from one of the strict
                    # read_text calls, a DB blip inside the validator. These
                    # used to escape past the rollback with the proposal's
                    # files already written — and `pull --rebase --autostash`
                    # then preserved them, so the next sync indexed and
                    # served content no one ever approved into a commit.
                    log.exception("feed %s: unexpected failure during apply", feed.pk)
                    raise ApplyFailure(
                        f"unexpected {type(exc).__name__} while applying: {exc}"
                        f"{_dirty_note(_rollback(branch))}"
                    ) from exc
                except BaseException:
                    # Worker shutdown or cancellation mid-apply. Not ours to
                    # convert into a Feed failure — but the tree still must
                    # not be left holding half a proposal.
                    _rollback(branch)
                    raise
    except StaleReview as exc:
        _finalize_stale(feed, exc)
        return f"stale: {exc}"
    except Exception as exc:
        # Deliberately broad: the alternative to marking the Feed failed is
        # leaving it wedged in `approving`, where no UI action can reach it.
        _finalize_failed(feed, str(exc), attempts)
        return f"failed: {exc}"

    feed.status = "edited" if feed.proposal_edited else "approved"
    feed.commit_hash = commit
    feed.decided_at = timezone.now()
    feed.retries = attempts - 1
    feed.error = ""
    feed.approve_claimed_at = None
    feed.save(
        update_fields=[
            "status", "commit_hash", "decided_at", "retries", "error", "approve_claimed_at",
        ]
    )
    emit(
        "feed",
        action="approved",
        feed_id=feed.pk,
        source_id=feed.source_id,
        commit=commit,
        retries=attempts - 1,
        edited=feed.proposal_edited,
        # "committed" | "already-landed" | "already-present" — the last two
        # mean this run recorded a commit it did not create.
        outcome=outcome,
    )

    # Post-push (lock released): reindex + rebuild snapshots so the new
    # notes serve immediately; the webhook echo will no-op on the SHA.
    try:
        from apps.brain.services import indexer, sync

        sync.publish_snapshots(indexer.rebuild(trigger="feed-approval"))
    except Exception:
        log.exception("feed %s: post-approval reindex failed (sync beat will repair)", feed.pk)

    return f"approved as {commit[:12]} after {attempts} attempt(s) [{outcome}]"


# ---- recovery for approvals that never reported back ---------------------


def stuck_feeds(*, force: bool = False) -> list[Feed]:
    """Feeds sitting in `approving` that no live worker can still be on."""
    return [
        f
        for f in Feed.objects.filter(status="approving").order_by("pk")
        if force or not f.approval_in_flight
    ]


def reconcile_stuck(*, force: bool = False) -> dict[str, int]:
    """Resolve every Feed wedged in `approving`. Safe to run repeatedly.

    Q2 on the Redis broker has no ack. A worker killed mid-approval loses
    the task outright, and because every ops action requires `pending`,
    the Feed becomes unreachable: it cannot be approved, rejected, edited
    or retried, forever. The worst shape is a crash between a successful
    `git push` and `feed.save` — the commit is in the brain and being
    served, while the DB says `approving` with an empty `commit_hash`.

    The `Feed-Id:` trailer settles it. If a commit on origin carries this
    feed's trailer the approval DID land and the row is completed against
    that commit; if none does, nothing was committed and the feed goes
    back to `pending` for the operator to approve again.

    Every write is a conditional `status="approving"` UPDATE, so a worker
    that turns out to be alive after all wins the race rather than having
    its result overwritten.

    Content convergence is not this function's job: it moves DB rows to
    match git, and `brain:sync` moves the clone to match origin.
    """
    feeds = stuck_feeds(force=force)
    result = {"scanned": len(feeds), "recovered": 0, "returned": 0, "skipped": 0}
    if not feeds:
        return result

    with gitrepo.repo_lock():
        branch = gitrepo.run("rev-parse", "--abbrev-ref", "HEAD")
        # The lost task may have pushed before it died, so judge against
        # the origin as it stands now, not the clone as we last left it.
        try:
            gitrepo.run("fetch", *_fetch_args(branch), timeout=120)
        except gitrepo.BrainRepoError:
            log.warning(
                "reconcile: fetch failed — judging against the last known origin",
                exc_info=True,
            )
        for feed in feeds:
            commit = landed_commit(feed.pk, branch)
            if commit:
                done = _finalize_recovered(feed, commit)
                result["recovered" if done else "skipped"] += 1
            else:
                done = _finalize_lost(feed)
                result["returned" if done else "skipped"] += 1

    log.info(
        "reconcile: scanned %s, recovered %s, returned to pending %s, skipped %s",
        result["scanned"], result["recovered"], result["returned"], result["skipped"],
    )
    return result


def _finalize_recovered(feed: Feed, commit: str) -> bool:
    """The lost task had already pushed — record the commit it made."""
    status = "edited" if feed.proposal_edited else "approved"
    if not Feed.objects.filter(pk=feed.pk, status="approving").update(
        status=status,
        commit_hash=commit,
        decided_at=timezone.now(),
        error="",
        approve_claimed_at=None,
    ):
        return False
    emit(
        "feed",
        action="approved",
        feed_id=feed.pk,
        source_id=feed.source_id,
        commit=commit,
        retries=feed.retries,
        edited=feed.proposal_edited,
        outcome="reconciled",
    )
    return True


def _finalize_lost(feed: Feed) -> bool:
    """No commit carries this feed's trailer — the approval never landed."""
    if not Feed.objects.filter(pk=feed.pk, status="approving").update(
        status="pending",
        decided_at=None,
        approve_claimed_at=None,
        error=(
            "This approval was claimed but never reported back — the worker "
            "was most likely restarted while it was running. Nothing was "
            "committed: no commit in the brain carries this feed's "
            "`Feed-Id:` trailer. Re-check the diff and approve again."
        ),
    ):
        return False
    emit("feed", action="approval_lost", feed_id=feed.pk, source_id=feed.source_id)
    return True


def _finalize_stale(feed: Feed, exc: StaleReview) -> None:
    """Back to `pending`, not `failed`.

    Nothing is wrong with the proposal; the review is out of date.
    `pending` is the only status the ops UI can act on, and re-opening
    the feed re-renders the diff against the clone as it now stands and
    re-validates against the current entity index — which is exactly what
    the operator has to see before approving again. `failed` would strand
    a perfectly good proposal with no way back.
    """
    feed.status = "pending"
    feed.error = str(exc)[:2000]
    feed.decided_at = None
    feed.approve_claimed_at = None
    feed.save(update_fields=["status", "error", "decided_at", "approve_claimed_at"])
    emit(
        "feed",
        action="stale_review",
        feed_id=feed.pk,
        source_id=feed.source_id,
        paths=exc.paths[:20],
    )


def _finalize_failed(feed: Feed, error: str, attempts: int = 0) -> None:
    feed.status = "failed"
    feed.error = error[:2000]
    feed.decided_at = timezone.now()
    feed.retries = max(0, attempts - 1)
    feed.approve_claimed_at = None
    feed.save(
        update_fields=["status", "error", "decided_at", "retries", "approve_claimed_at"]
    )
    emit("feed", action="apply_failed", feed_id=feed.pk, source_id=feed.source_id, error=error[:500])
