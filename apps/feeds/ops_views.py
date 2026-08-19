"""Ops UI: feed queue + propose form (PLAN.md §8, M2.1 slice).

Staff-only, mounted under the admin-panel prefix via brainconfig.urls.
This page is the UI channel of the write door: the form calls the same
intake service as REST/MCP. The approval actions (diff view, edit,
approve/reject) land in M2.4 — for now the queue lists and inspects.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.brain.services import gitrepo
from apps.brainconfig.nav import ops_context
from apps.events.models import emit

from .models import OVERRIDE_NOTE_PREFIX, Feed
from .services import diffview, feeder, intake, repair, thought, validator

SOURCE_KINDS = ("yt", "blog", "x", "newsletter", "repo", "doc", "thought")


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def queue(request):
    if request.method == "POST":
        # The lane that cannot fail. No agent, no extraction, no editorial
        # choice — the thought is written down verbatim and filed correctly
        # by construction, so "save this" is never refused for filing.
        if request.POST.get("action") == "save_thought":
            try:
                feed = thought.capture(
                    text=request.POST.get("thought", ""),
                    title=request.POST.get("thought_title", ""),
                    topic=request.POST.get("topic", ""),
                    ntype=request.POST.get("ntype", "take"),
                )
            except intake.FeedRejected as exc:
                messages.error(request, str(exc))
                return redirect(request.path)
            # Land ON the feed, not back on a list. The whole complaint was
            # that nothing takes you to where it matters; a success message
            # naming a number you then have to go and find is that same
            # failure in miniature.
            messages.success(request, "Written down verbatim. Approve it and it is in your brain.")
            return redirect("brainconfig:feed-detail", pk=feed.pk)

        try:
            feed = intake.propose(
                channel="ui",
                source_kind=request.POST.get("source_kind", ""),
                title=request.POST.get("title", ""),
                source_url=request.POST.get("url", ""),
                content=request.POST.get("content", ""),
                notes=request.POST.get("notes", ""),
                source_id=request.POST.get("source_id", ""),
            )
        except intake.FeedRejected as exc:
            messages.error(request, str(exc))
        else:
            fetch = feed.raw_payload.get("fetch")
            if fetch and not fetch.get("ok"):
                messages.warning(
                    request,
                    f"Captured {feed.source_id}, but the URL fetch failed "
                    f"({fetch.get('error', 'unknown error')}). Paste the content, or let extraction retry.",
                )
            else:
                messages.success(request, f"Feed {feed.source_id} is pending.")
        return redirect(request.path)

    f_status = request.GET.get("status", "")
    feeds = Feed.objects.all()
    if f_status:
        feeds = feeds.filter(status=f_status)

    # Page AFTER filtering; the filter form carries no `page` input, so
    # changing the status naturally lands back on page 1. get_page clamps
    # junk and out-of-range values instead of 404ing (browser pattern).
    paginator = Paginator(feeds, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    # Filters re-encoded WITHOUT `page`, so pager links compose as
    # "?{filter_qs}&page=N" and never carry a stale page number.
    filter_qs = urlencode({"status": f_status}) if f_status else ""

    return render(
        request,
        "ops/feeds.html",
        {
            "feeds": page_obj.object_list,
            "page_obj": page_obj,
            "filter_qs": filter_qs,
            "total": Feed.objects.count(),
            "pending_count": Feed.objects.filter(status="pending").count(),
            "statuses": [s for s, _ in Feed.STATUSES],
            "f_status": f_status,
            "source_kinds": SOURCE_KINDS,
            "thought_types": thought.THOUGHT_TYPES,
            "taxonomy": thought.taxonomy(),
            "payload_max_kb": intake.payload_max_bytes() // 1024,
            **ops_context(request),
        },
    )


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def feed_detail(request, pk: int):
    feed = Feed.objects.filter(pk=pk).first()
    if feed is None:
        raise Http404
    if request.method == "POST":
        _handle_action(request, feed)
        return redirect(request.path)

    payload = feed.raw_payload or {}
    # Validate only while the decision is still open: an applied proposal
    # re-validated against the post-apply repo self-collides (its `create`
    # files exist now — rule 5) and the stale violations read as failure.
    validation = (
        validator.validate_feed(feed)
        if feed.proposal and feed.status in ("pending", "approving")
        else None
    )
    diffs = diffview.build(feed.proposal, gitrepo.repo_dir()) if feed.proposal else []
    # `issues` carries two unlike things: filing the repair pass already
    # corrected (informational, nothing to decide) and genuine flags from
    # the feeder (read before approving). Split here rather than in the
    # template so neither box renders empty.
    _issues = list((feed.proposal or {}).get("issues") or [])
    tidied = [i for i in _issues if i.startswith(repair.NOTE_PREFIX)]
    feeder_issues = [i for i in _issues if not i.startswith(repair.NOTE_PREFIX)]
    return render(
        request,
        "ops/feed_detail.html",
        {
            "feed": feed,
            "payload": payload,
            "fetch": payload.get("fetch"),
            "content": payload.get("content", ""),
            "proposal_json": (
                json.dumps(feed.proposal, indent=2, ensure_ascii=False) if feed.proposal else ""
            ),
            "validation": validation,
            "diffs": diffs,
            "tidied": tidied,
            "feeder_issues": feeder_issues,
            # Approve is gated on SAFETY only. Hygiene violations — filing
            # the machine should do, or an editorial call — leave the button
            # live and ask for a one-line reason instead. A single-operator
            # brain whose owner cannot overrule it is a brain that refuses
            # its owner's ideas, which is exactly what happened for four
            # days: 13 of 14 queued violations were one hygiene rule.
            "can_approve": bool(feed.status == "pending" and validation and validation.overridable),
            "needs_override": bool(validation and validation.overridable and not validation.valid),
            "edit_files": list(enumerate((feed.proposal or {}).get("files") or [])),
            "edit_lines": list(enumerate((feed.proposal or {}).get("index_lines") or [])),
            **ops_context(request),
        },
    )


def _handle_action(request, feed: Feed) -> None:
    action = request.POST.get("action", "")

    # The one action that exists BECAUSE the feed is not pending. Q2 has no
    # ack on the Redis broker, so a worker restarted mid-apply leaves the
    # feed in `approving` — a status every other action refuses, which
    # made it permanently unreachable. This is the way out.
    if action == "recover":
        if feed.status != "approving":
            messages.error(request, "This feed is not waiting on an approval.")
        elif feed.approval_in_flight:
            messages.info(
                request, "The approval is still within its timeout — give it a moment."
            )
        else:
            from apps.brainconfig import jobs, maintenance

            started, message = jobs.enqueue(maintenance.RECONCILE_APPROVALS)
            (messages.success if started else messages.error)(request, message)
        return

    if feed.status != "pending":
        messages.error(request, f"Feed is {feed.status} — no further actions.")
        return

    if action == "extract":
        if feed.extraction_in_flight:
            messages.info(request, "Extraction is already running — see Tasks for progress.")
        elif feeder.enqueue_extraction(feed):
            messages.success(request, "Extraction queued — the worker will fill the proposal.")
        else:
            messages.error(request, "Could not reach the worker queue — see the error on the feed.")

    elif action == "save_edits" and feed.proposal:
        proposal = dict(feed.proposal)
        files = [dict(f) for f in proposal.get("files") or []]
        for i, f in enumerate(files):
            new = request.POST.get(f"file_content__{i}")
            if new is not None and new.replace("\r\n", "\n") != f.get("content"):
                f["content"] = new.replace("\r\n", "\n")
        lines = [dict(l) for l in proposal.get("index_lines") or []]
        for i, l in enumerate(lines):
            new = request.POST.get(f"index_line__{i}")
            if new is not None and new.strip() != l.get("line"):
                l["line"] = new.strip()
        summary = request.POST.get("summary")
        if summary is not None:
            proposal["summary"] = summary.strip()
        proposal["files"] = files
        proposal["index_lines"] = lines
        # Same trusted-code pass as extraction: mechanical INDEX tokens
        # follow the (possibly just-edited) frontmatter automatically.
        proposal = feeder.normalize_index_lines(proposal)
        if proposal != feed.proposal:
            feed.proposal = proposal
            feed.proposal_edited = True
            feed.save(update_fields=["proposal", "proposal_edited"])
            emit("feed", action="edited", feed_id=feed.pk, source_id=feed.source_id)
        # Re-validate NOW so the flash reflects the post-edit state — the
        # approve button only enables off this fresh result (M2.4 check).
        res = validator.validate_feed(feed)
        if res.valid:
            messages.success(request, "Edits saved — proposal passes rules 1-8; approve is enabled.")
        else:
            messages.error(request, f"Edits saved — {len(res.violations)} validation violation(s) remain.")

    elif action == "reject":
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, "A reject reason is required.")
            return
        feed.status = "rejected"
        feed.decided_at = timezone.now()
        feed.decision_note = reason
        feed.save(update_fields=["status", "decided_at", "decision_note"])
        emit("feed", action="rejected", feed_id=feed.pk, source_id=feed.source_id, reason=reason)
        messages.success(request, f"Feed {feed.source_id} rejected.")

    elif action == "approve":
        res = validator.validate_feed(feed)
        # Safety is absolute and is re-checked here, not just in the
        # template: writing outside the repo, quoting private content,
        # emptying a file, or blocked-script content. No reason clears these.
        if res.safety_violations:
            messages.error(
                request,
                f"{len(res.safety_violations)} safety violation(s) — these cannot be overridden. "
                "Fix the proposal or reject it.",
            )
            return
        override_reason = (request.POST.get("override_reason") or "").strip()
        if not res.valid and not override_reason:
            messages.error(
                request,
                f"{len(res.hygiene_violations)} hygiene violation(s) — approving anyway needs a "
                "one-line reason, so the decision is on the record.",
            )
            return
        # Atomic claim: double-clicks and racing tabs see 0 rows updated.
        # The timestamp is what later tells "a worker is on it" from "the
        # task was lost" — see `approval.reconcile_stuck`.
        claimed = Feed.objects.filter(pk=feed.pk, status="pending").update(
            status="approving", approve_claimed_at=timezone.now()
        )
        if claimed and override_reason:
            # Written in the same breath as the claim, so an override can
            # never land as a silent approval. The rules it overrode are
            # named, not just the reason — "approved anyway" is useless a
            # month later without knowing what was waived.
            waived = ", ".join(sorted({f"rule {v.rule}" for v in res.hygiene_violations}))
            Feed.objects.filter(pk=feed.pk).update(
                decision_note=f"{OVERRIDE_NOTE_PREFIX}{waived}: {override_reason}"
            )
            emit(
                "feed",
                action="override",
                feed_id=feed.pk,
                source_id=feed.source_id,
                waived=waived,
                reason=override_reason,
            )
        if not claimed:
            messages.error(request, "Feed is already being applied.")
            return
        try:
            from django_q.tasks import async_task

            async_task("apps.feeds.services.approval.apply_feed", feed_id=feed.pk)
        except Exception:
            Feed.objects.filter(pk=feed.pk, status="approving").update(
                status="pending", approve_claimed_at=None
            )
            messages.error(request, "Could not reach the worker queue — approval not started.")
            return
        messages.success(
            request,
            f"Approved — the worker is committing feed: {feed.source_id}. Refresh for the result.",
        )

    else:
        messages.error(request, "Unknown action.")
