"""Celery entry points.

Celery is an execution harness, not the ledger. Every task here is a thin wrapper that
tells a worker to look at the database; losing a Celery message costs a poll interval, not
a job.
"""

from __future__ import annotations

from celery import shared_task

from ops.logging import get_logger

logger = get_logger("taskmanager.queues.celery")


@shared_task(name="queues.dispatch_available_jobs", ignore_result=True)
def dispatch_available_jobs(queue_id: int | None = None, limit: int | None = None):
    """Claim and execute durable jobs until the queue is empty or the tick's budget is spent.

    One claim per tick is a throughput ceiling, not a design: a claim is capped by the
    queue's concurrency limit (8 by default), so a single batch every 10 seconds drains
    0.8 jobs per second no matter how many workers exist. Measured under load, that turned
    a 2,000-job backlog into a 40-minute wait. Looping inside a time budget shorter than
    the beat interval keeps ticks from piling up while letting each one do real work.
    """

    import time

    from django.conf import settings

    from .models import Queue
    from .runtime import run_once

    queue = Queue.objects.filter(pk=queue_id).first() if queue_id else None
    budget = getattr(settings, "QUEUE_DISPATCH_TIME_BUDGET_SECONDS", 8)
    deadline = time.monotonic() + budget

    totals = {"claimed": 0, "succeeded": 0, "retried": 0, "dead": 0, "batches": 0}
    while True:
        batch = run_once(queue=queue, limit=limit)
        totals["batches"] += 1
        for key in ("claimed", "succeeded", "retried", "dead"):
            totals[key] += batch[key]
        if batch["claimed"] == 0 or time.monotonic() >= deadline:
            break
    return totals


@shared_task(name="queues.reap_expired_leases", ignore_result=True)
def reap_expired_leases_task(limit: int = 200):
    """Return jobs stranded by dead workers."""

    from .services import reap_expired_leases

    return reap_expired_leases(limit=limit)


@shared_task(name="queues.snapshot_metrics", ignore_result=True)
def snapshot_metrics_task():
    """Sample queue depth and emit backlog alerts."""

    from .services import snapshot_queue_metrics

    return {"snapshots": len(snapshot_queue_metrics())}


@shared_task(name="schedules.scan_due_schedules", ignore_result=True)
def scan_due_schedules_task(limit: int = 200):
    """Materialise schedule occurrences that are due."""

    from schedules.services import scan_due_schedules

    return scan_due_schedules(limit=limit)


@shared_task(name="tasks.sweep_recurrences", ignore_result=True)
def sweep_recurrences_task(limit: int = 200):
    """Create follow-up tasks for due recurrence rules."""

    from tasks.recurrence import sweep_due_recurrences

    return sweep_due_recurrences(limit=limit)


@shared_task(name="tasks.enqueue_due_digests", ignore_result=True)
def enqueue_due_digests_task():
    """Queue digest emails for members whose local digest hour has arrived."""

    from tasks.digests import enqueue_due_digests

    return enqueue_due_digests()


@shared_task(name="kernel.purge_expired_organizations", ignore_result=True)
def purge_expired_organizations_task():
    """Hard-delete workspaces whose retention window has elapsed."""

    from kernel.purge import enqueue_due_purges

    return enqueue_due_purges()
