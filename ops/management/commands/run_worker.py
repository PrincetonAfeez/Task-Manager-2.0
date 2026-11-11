"""Run a durable-queue worker without needing a broker.

This is the same runtime the Celery task calls, exposed as a process. It exists so the
queue can be operated (and tested, and demoed) without Redis, and so a deployment can
choose plain processes over a Celery fleet.
"""

from __future__ import annotations

import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from queues.models import Queue
from queues.runtime import run_forever, run_once


class Command(BaseCommand):
    help = "Claim and execute durable jobs."

    def add_arguments(self, parser):
        parser.add_argument("--queue", help="Only process this queue (by name).")
        parser.add_argument("--once", action="store_true", help="Drain one batch and exit.")
        parser.add_argument("--batch-size", type=int, default=None)
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument(
            "--max-iterations", type=int, default=None, help="Stop after N polls (for tests/CI)."
        )

    def handle(self, *args, **options):
        queue = None
        if options["queue"]:
            queue = Queue.objects.filter(name=options["queue"]).first()
            if queue is None:
                raise CommandError(f"No queue named {options['queue']!r}.")

        if options["once"]:
            summary = run_once(queue=queue, limit=options["batch_size"])
            self.stdout.write(self.style.SUCCESS(f"Drained one batch: {summary}"))
            return

        # SIGTERM is how a container orchestrator asks for a graceful stop. Setting the
        # event lets the current job finish instead of being killed mid-effect.
        stop = threading.Event()

        def request_stop(signum, frame):
            self.stdout.write(self.style.WARNING("\nShutdown requested; finishing current batch..."))
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

        totals = run_forever(
            queue=queue,
            poll_interval=options["poll_interval"],
            batch_size=options["batch_size"],
            max_iterations=options["max_iterations"],
            stop_event=stop,
        )
        self.stdout.write(self.style.SUCCESS(f"Worker stopped: {totals}"))
