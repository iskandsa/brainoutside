"""Shared nav context for the ops UI pages. Each ops view merges
`ops_context(request)` into its render context."""
from __future__ import annotations

from django.urls import reverse


def _mark_active(sections: list[dict], path: str) -> None:
    """Flag the one nav item the current request belongs to.

    Longest match wins, and that is the whole subtlety: the dashboard is
    mounted at the ops root (`/ops/`), which is a prefix of every other
    ops URL, so a naive `startswith` lights up "Dashboard" on every single
    page. An exact match outranks any prefix, and among prefixes the
    longest one wins — so `/ops/brain/take-2026-07-x/` highlights
    "Brain browser", not "Dashboard".

    Prefix matching rather than exact-only is what makes detail pages
    work: entity, feed detail and chat session pages have no nav entry of
    their own and should highlight their parent section.
    """
    best: dict | None = None
    best_score = -1
    for section in sections:
        for item in section["items"]:
            url = item["url"]
            if path == url:
                score = len(url) + 1  # exact beats the same URL as a prefix
            elif path.startswith(url):
                score = len(url)
            else:
                continue
            if score > best_score:
                best, best_score = item, score
    if best is not None:
        best["active"] = True


def ops_context(request) -> dict:
    settings_url = reverse("brainconfig:settings")
    base = settings_url.rsplit("settings/", 1)[0]
    # `icon` names resolve in templates/partials/_nav_icon.html; items
    # without one (the docs endpoint catalog) render label-only.
    # Grouped, not a flat list of twelve. A flat list makes every page
    # look equally important, so the two that ARE the job — approve what is
    # waiting, and ask the brain something — sat sixth and fifth among
    # plumbing. The owner's words: "there is absolutely nothing that takes
    # me to where it matters."
    #
    # "Use it" leads and holds exactly those two. Everything else is
    # inspection or plumbing and is labelled as such.
    sections = [
        {
            "label": "Use it",
            "items": [
                {"label": "Ask your brain", "icon": "chat", "url": reverse("brainconfig:chat")},
                {"label": "Feed queue", "icon": "feeds", "url": reverse("brainconfig:feeds")},
            ],
        },
        {
            "label": "Look inside",
            "items": [
                {"label": "Dashboard", "icon": "dashboard", "url": reverse("brainconfig:dashboard")},
                {"label": "Brain browser", "icon": "browser", "url": reverse("brainconfig:browser")},
                {"label": "Graph", "icon": "graph", "url": reverse("brainconfig:graph")},
                {"label": "Timeline", "icon": "timeline", "url": reverse("brainconfig:timeline")},
            ],
        },
        {
            "label": "Plumbing",
            "items": [
                {"label": "Tasks", "icon": "tasks", "url": reverse("brainconfig:tasks")},
                {"label": "Logs", "icon": "logs", "url": reverse("brainconfig:logs")},
                {"label": "API keys", "icon": "keys", "url": reverse("brainconfig:consumers")},
                {"label": "Connectors", "icon": "link", "url": reverse("brainconfig:connectors")},
                {"label": "Settings", "icon": "settings", "url": settings_url},
                {"label": "Setup & health", "icon": "health", "url": reverse("brainconfig:health")},
            ],
        },
        {
            "label": "Public",
            "items": [
                {"label": "API docs", "icon": "docs", "url": "/docs/"},
                # The raw machine probe. Lived in the topbar; the topbar
                # is mobile-only now, so desktop reaches it from here.
                {"label": "Health probe", "icon": "health", "url": "/readyz"},
            ],
        },
    ]
    # `request` is None in a few unit tests that build the nav in isolation;
    # no request just means nothing is highlighted.
    if request is not None and getattr(request, "path", ""):
        _mark_active(sections, request.path)

    return {
        "ops_nav_sections": sections,
        "ops_top_links": [
            {"label": "Docs", "url": "/docs/"},
            {"label": "Health", "url": "/readyz"},
        ],
        "ops_base": base,
    }
