"""Reclaim job leases held by workers that died."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from queues.services import reap_expired_leases, snapshot_queue_metrics


class Command(BaseCommand):
    help = "Return stranded jobs to their queues and sample queue depth."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--no-metrics", action="store_true")

    def handle(self, *args, **options):
        result = reap_expired_leases(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Reaped: {result['reclaimed']} reclaimed, "
                f"{result['dead_lettered']} dead-lettered, {result['scanned']} scanned."
            )
        )
        if not options["no_metrics"]:
            snapshots = snapshot_queue_metrics()
            for snapshot in snapshots:
                self.stdout.write(
                    f"  {snapshot.queue.name}: depth={snapshot.depth} "
                    f"in_flight={snapshot.in_flight} dead_letters={snapshot.dead_letters} "
                    f"oldest={snapshot.oldest_available_age_seconds}s"
                )
