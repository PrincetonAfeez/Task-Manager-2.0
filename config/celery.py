"""Celery application.

Celery is an execution *harness*, not the ledger. Every periodic task below is a nudge that
tells a worker to look at the database; the durable ``Job`` rows are the source of truth, so
a lost broker message costs one interval, not the work.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("task_manager")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Route each responsibility to its own queue so one slow lane cannot starve the others,
# and so the worker services in docker-compose consume queues that actually receive work.
app.conf.task_routes = {
    "queues.dispatch_available_jobs": {"queue": "dispatcher"},
    "queues.reap_expired_leases": {"queue": "reaper"},
    "queues.snapshot_metrics": {"queue": "reaper"},
    "schedules.scan_due_schedules": {"queue": "scheduler"},
    "tasks.sweep_recurrences": {"queue": "scheduler"},
    "tasks.enqueue_due_digests": {"queue": "scheduler"},
    "kernel.purge_expired_organizations": {"queue": "reaper"},
}

# Without this, nothing ever triggers the dispatcher, the reaper or the scheduler scan --
# the entire automation layer would sit idle no matter how correct it is.
#
# Every entry carries `expires`: if beat or a worker stalls, the backlog of identical ticks
# must not all fire at once on recovery.
app.conf.beat_schedule = {
    "dispatch-available-jobs": {
        "task": "queues.dispatch_available_jobs",
        "schedule": 10.0,
        "options": {"queue": "dispatcher", "expires": 30},
    },
    "reap-expired-leases": {
        "task": "queues.reap_expired_leases",
        "schedule": 60.0,
        "options": {"queue": "reaper", "expires": 55},
    },
    "scan-due-schedules": {
        "task": "schedules.scan_due_schedules",
        "schedule": 30.0,
        "options": {"queue": "scheduler", "expires": 25},
    },
    "snapshot-queue-metrics": {
        "task": "queues.snapshot_metrics",
        "schedule": 300.0,
        "options": {"queue": "reaper", "expires": 280},
    },
    "sweep-recurrences": {
        "task": "tasks.sweep_recurrences",
        # Hourly rather than daily: a workspace in any timezone should see its recurring
        # task appear on the right local day.
        "schedule": crontab(minute=5),
        "options": {"queue": "scheduler", "expires": 3000},
    },
    "enqueue-due-digests": {
        "task": "tasks.enqueue_due_digests",
        # Runs hourly; the task itself decides which members are at their local digest
        # hour, so one schedule serves every timezone.
        "schedule": crontab(minute=0),
        "options": {"queue": "scheduler", "expires": 3000},
    },
    "purge-expired-organizations": {
        "task": "kernel.purge_expired_organizations",
        "schedule": crontab(hour=3, minute=30),
        "options": {"queue": "reaper", "expires": 3600},
    },
}
