"""Run one declared scheduled task, by name, from the command line.

`config/scheduled.py` is the single source of truth for what runs and how
often. That list was designed to be loaded into django-q2's Schedule table
and executed by a cluster daemon — which on this install meant five
processes and ~336 MB resident to run five small cron jobs and commit the
occasional approval.

For a one-operator brain that is the wrong trade. System cron already runs
every other project on this box at zero resident cost, so it can run these
too. This command is the bridge: cron calls it, it dispatches by name to
the same callable the daemon would have used, so the schedule cannot drift
between the two worlds.

    manage.py run_scheduled brain:sync
    manage.py run_scheduled --list

Exit code is non-zero on failure, which is what makes a cron mail or a
watchdog able to notice.
"""
from __future__ import annotations

import importlib
import time

from django.core.management.base import BaseCommand, CommandError

from config.scheduled import SCHEDULED_TASKS


class Command(BaseCommand):
    help = "Run one task declared in config/scheduled.py (see --list)."

    def add_arguments(self, parser):
        parser.add_argument("name", nargs="?", default="", help="Task name, e.g. brain:sync")
        parser.add_argument("--list", action="store_true", help="List declared tasks and exit.")

    def handle(self, *args, **opts):
        tasks = {t.name: t for t in SCHEDULED_TASKS}

        if opts["list"] or not opts["name"]:
            for t in SCHEDULED_TASKS:
                self.stdout.write(f"{t.name:34} {t.cron:16} {t.func}")
            if not opts["name"] and not opts["list"]:
                raise CommandError("Give a task name, or --list.")
            return

        task = tasks.get(opts["name"])
        if task is None:
            raise CommandError(
                f"Unknown task {opts['name']!r}. Known: {', '.join(sorted(tasks))}"
            )

        module_path, _, attr = task.func.rpartition(".")
        fn = getattr(importlib.import_module(module_path), attr)

        started = time.monotonic()
        try:
            result = fn(**(task.kwargs or {}))
        except Exception as exc:  # noqa: BLE001 — boundary; cron needs the exit code
            raise CommandError(f"{task.name} failed: {exc}") from exc
        ms = int((time.monotonic() - started) * 1000)
        self.stdout.write(f"{task.name} ok in {ms}ms" + (f" — {result}" if result else ""))
