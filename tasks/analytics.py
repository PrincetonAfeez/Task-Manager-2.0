"""Workspace analytics.

Read-only aggregates for the analytics page. Everything is computed with database
aggregation rather than Python loops over querysets, so the page cost stays flat as a
workspace grows.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from kernel.models import Membership
from queues.models import DeadLetter, Job

from .models import Task


def workspace_analytics(*, organization, days: int = 30) -> dict:
    today = timezone.localdate()
    since_date = today - timedelta(days=days)
    since = timezone.now() - timedelta(days=days)

    tasks = Task.objects.filter(organization=organization)
    active = tasks.filter(archived_at__isnull=True)

    completed = tasks.filter(completed_at__gte=since)
    created = tasks.filter(created_at__gte=since)

    # Cycle time: creation to completion, averaged over the window.
    cycle = completed.annotate(
        cycle=ExpressionWrapper(F("completed_at") - F("created_at"), output_field=DurationField())
    ).aggregate(avg=Avg("cycle"))["avg"]

    by_day = (
        completed.annotate(day=TruncDate("completed_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    created_by_day = (
        created.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )

    by_assignee = (
        active.filter(status=Task.Status.OPEN)
        .values("assignee__username")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    by_priority = (
        active.filter(status=Task.Status.OPEN).values("priority").annotate(count=Count("id")).order_by("-priority")
    )

    by_list = (
        active.values("task_list__name")
        .annotate(
            open=Count("id", filter=Q(status=Task.Status.OPEN)),
            done=Count("id", filter=Q(status=Task.Status.COMPLETED)),
        )
        .order_by("-open")[:10]
    )

    job_stats = Job.objects.filter(organization=organization, created_at__gte=since).aggregate(
        total=Count("id"),
        succeeded=Count("id", filter=Q(status=Job.Status.SUCCEEDED)),
        dead=Count("id", filter=Q(status=Job.Status.DEAD)),
        waiting=Count("id", filter=Q(status__in=Job.WAITING_STATUSES)),
    )
    total_jobs = job_stats["total"] or 0
    success_rate = round(100 * job_stats["succeeded"] / total_jobs, 1) if total_jobs else None

    counts = active.aggregate(
        open=Count("id", filter=Q(status=Task.Status.OPEN)),
        overdue=Count("id", filter=Q(status=Task.Status.OPEN, due_date__lt=today)),
        completed=Count("id", filter=Q(status=Task.Status.COMPLETED)),
    )
    open_count = counts["open"] or 0

    return {
        "days": days,
        "since": since_date,
        "counts": counts,
        # Throughput vs intake: the ratio people actually act on.
        "created_count": created.count(),
        "completed_count": completed.count(),
        "completion_by_day": list(by_day),
        "created_by_day": list(created_by_day),
        "avg_cycle_hours": round(cycle.total_seconds() / 3600, 1) if cycle else None,
        "overdue_ratio": round(100 * (counts["overdue"] or 0) / open_count, 1) if open_count else 0.0,
        "by_assignee": list(by_assignee),
        "by_priority": list(by_priority),
        "by_list": list(by_list),
        "member_count": Membership.objects.filter(
            organization=organization, status=Membership.Status.ACTIVE
        ).count(),
        "job_stats": job_stats,
        "job_success_rate": success_rate,
        "open_dead_letters": DeadLetter.objects.filter(
            organization=organization, redriven_at__isnull=True
        ).count(),
    }
