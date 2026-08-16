"""Feed proposals — the write door's queue (PLAN.md §3 `apps/feeds`).

A Feed is a captured source waiting for the gated write path: proposed
(M2.1, here) → extracted into `proposal` by the feeder agent (M2.2) →
validated (M2.3) → approved/edited/rejected by the operator in the ops UI
(M2.4) → committed + pushed by the approval handler (M2.5). Creating or
deciding a Feed row NEVER touches the brain repo; only the approval
handler writes there.

PRIMARY data (not rebuildable from the repo) — covered by the DB backup
policy (PLAN.md §10).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


#: Marks a `decision_note` written by an operator override — approval over
#: outstanding HYGIENE violations, with a reason. The ops view writes it and
#: the approval worker reads it, so the worker's own pre-commit gate knows a
#: human already made this call and does not refuse the same rules twice.
#: Safety violations are unaffected: neither gate will pass those, ever.
OVERRIDE_NOTE_PREFIX = "Approved over "


class Feed(models.Model):
    CHANNELS = [
        ("ui", "ui"),
        ("api", "api"),
        ("mcp", "mcp"),
    ]

    # pending covers the whole pre-decision life (including extraction —
    # the M2.2 worker fills `proposal` while status stays pending).
    # `approving` is the transient claim between the approve click and the
    # worker's commit+push — it makes double-approval impossible (the
    # claim is an atomic pending→approving UPDATE).
    STATUSES = [
        ("pending", "pending"),
        ("approving", "approving"),
        ("approved", "approved"),
        ("edited", "edited"),  # approved with human edits
        ("rejected", "rejected"),
        ("failed", "failed"),  # extraction or commit/push failed terminally
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # The mind's source identifier — becomes the `feed: <source-id>` commit
    # subject on approval. Not unique: the same source may be legitimately
    # re-fed (dedupe is conservative, per contract §5.4).
    source_id = models.CharField(max_length=120, db_index=True)
    channel = models.CharField(max_length=8, choices=CHANNELS)
    # Which API key proposed it (NULL for the ops UI form). SET_NULL: the
    # capture outlives key rotation.
    consumer = models.ForeignKey(
        "api_keys.APIKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="feeds"
    )
    # The captured source: source_kind/title/source_url/content/notes +
    # `fetch` metadata when the URL was fetched here in trusted code
    # (grill C12 — the feeder agent gets no network).
    raw_payload = models.JSONField(default=dict)
    # The feeder agent's schema-valid extraction (M2.2). NULL until then.
    proposal = models.JSONField(null=True, blank=True)
    # True once the operator has edited the proposal — approval then lands as
    # status "edited" instead of "approved" (M2.4).
    proposal_edited = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUSES, default="pending", db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    # Human note on the decision: the reject reason, or context on approve.
    decision_note = models.TextField(blank=True, default="")
    # Commit that landed this feed in the brain repo (M2.5).
    commit_hash = models.CharField(max_length=64, blank=True, default="")
    # Locked-sequence push retries consumed (M2.5, grill C20).
    retries = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    # Token-ledger row for the extraction run (M2.2).
    sdk_operation = models.ForeignKey(
        "events.SdkOperation", null=True, blank=True, on_delete=models.SET_NULL, related_name="feeds"
    )
    # Set when an extraction is enqueued, cleared on every terminal
    # outcome (proposal stored / refused / composition failure / final
    # retry). Powers the in-flight button guard and the Tasks page —
    # a second Extract click while set is a double SDK spend.
    extract_queued_at = models.DateTimeField(null=True, blank=True)
    # Set by the atomic approve claim, cleared on every terminal outcome.
    # Q2 on the Redis broker has no ack: a worker killed mid-apply simply
    # loses the task, and the Feed sits in `approving` forever — a status
    # no ops action accepts, so the operator has no move at all. This is
    # the timestamp `reconcile_stuck` needs to tell "a worker is still
    # working on it" from "nobody is coming".
    approve_claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"feed:{self.source_id} [{self.status}] via {self.channel}"

    @property
    def payload_bytes(self) -> int:
        return len((self.raw_payload.get("content") or "").encode("utf-8"))

    @property
    def extraction_in_flight(self) -> bool:
        """Queued or running, with a staleness horizon (all retry attempts
        + backoffs) so a dead worker can't wedge the Extract button forever."""
        if not self.extract_queued_at:
            return False
        horizon = int(settings.Q_CLUSTER["timeout"]) * 3 + 15 * 60
        return (timezone.now() - self.extract_queued_at).total_seconds() < horizon

    @property
    def approval_in_flight(self) -> bool:
        """Claimed, and still inside the window a live worker could need.

        One Q2 task with up to MAX_PUSH_ATTEMPTS inside it, so the bound
        is the cluster timeout plus slack — past that the task is gone,
        not slow. A claim with no timestamp (taken before this field
        existed) reads as NOT in flight: the alternative is a feed that
        can never be recovered, which is the bug.
        """
        if self.status != "approving" or not self.approve_claimed_at:
            return False
        horizon = int(settings.Q_CLUSTER["timeout"]) + 5 * 60
        return (timezone.now() - self.approve_claimed_at).total_seconds() < horizon
