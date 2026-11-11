"""Run the schedule scanner and recurrence sweep as a plain process."""

from __future__ import annotations

import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection

from schedules.services import scan_due_schedules
from tasks.recurrence import sweep_due_recurrences


class Command(BaseCommand):
    help = "Fire due schedules and materialise due recurrences."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=30.0)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--max-iterations", type=int, default=None)

    def handle(self, *args, **options):
        if options["once"]:
            self.stdout.write(str(self._tick()))
            return

        stop = threading.Event()

        def request_stop(signum, frame):
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

        iterations = 0
        # The recurrence sweep is date-driven, so running it on every 30-second tick would
        # be wasteful; once an hour is enough to catch every timezone's local midnight.
        recurrence_every = max(1, int(3600 / max(options["interval"], 1)))

        while not stop.is_set():
            if options["max_iterations"] is not None and iterations >= options["max_iterations"]:
                break
            if not connection.in_atomic_block:
                close_old_connections()
            totals = scan_due_schedules()
            if iterations % recurrence_every == 0:
                totals["recurrences"] = sweep_due_recurrences()
            if totals.get("occurrences") or totals.get("errors"):
                self.stdout.write(str(totals))
            iterations += 1
            if options["max_iterations"] is None:
                stop.wait(options["interval"])

        self.stdout.write(self.style.SUCCESS(f"Scheduler stopped after {iterations} ticks."))

    @staticmethod
    def _tick():
        return {"schedules": scan_due_schedules(), "recurrences": sweep_due_recurrences()}
