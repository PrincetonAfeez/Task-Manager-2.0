"""Second-round feature services: time tracking, templates, bulk operations, calendar."""

from __future__ import annotations

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from ops.logging import get_logger

from .models import Label, ShareLink, Subtask, Task, TaskTemplate, TimeEntry
from .services import next_position, record_activity

logger = get_logger("taskmanager.tasks.extra")

MAX_BULK_ITEMS = 500


# --------------------------------------------------------------------------------------
# Time tracking
# --------------------------------------------------------------------------------------


class TimerAlreadyRunning(Exception):
    """The user already has a running timer."""


@transaction.atomic
def start_timer(*, task: Task, user, note: str = "") -> TimeEntry:
    """Start a timer, stopping any other one this user has running.

    Auto-stopping rather than refusing is the behaviour people expect: switching tasks is
    the normal case, and forcing an explicit stop first just produces forgotten timers.
    """

    running = TimeEntry.objects.filter(user=user, ended_at__isnull=True).first()
    if running is not None:
        if running.task_id == task.pk:
            return running
        running.stop()

    try:
        return TimeEntry.objects.create(
            organization=task.organization, task=task, user=user, note=note[:255]
        )
    except IntegrityError as exc:
        # Lost a race with another tab; the partial unique constraint caught it.
        raise TimerAlreadyRunning("You already have a timer running.") from exc


def stop_timer(*, user, entry: TimeEntry | None = None) -> TimeEntry | None:
    entry = entry or TimeEntry.objects.filter(user=user, ended_at__isnull=True).first()
    if entry is None:
        return None
    return entry.stop()


def running_timer(user) -> TimeEntry | None:
    return (
        TimeEntry.objects.filter(user=user, ended_at__isnull=True)
        .select_related("task", "organization")
        .first()
    )


@transaction.atomic
def log_time(*, task: Task, user, minutes: int, note: str = "", when=None) -> TimeEntry:
    """Record a completed block of work without using a timer."""

    if minutes <= 0:
        raise ValidationError({"minutes": "Log a positive number of minutes."})
    if minutes > 24 * 60:
        raise ValidationError({"minutes": "A single entry cannot exceed 24 hours."})

    started = when or timezone.now() - timedelta(minutes=minutes)
    return TimeEntry.objects.create(
        organization=task.organization,
        task=task,
        user=user,
        started_at=started,
        ended_at=started + timedelta(minutes=minutes),
        duration_seconds=minutes * 60,
        note=note[:255],
    )


def time_summary(*, organization, since=None, user=None, task=None) -> dict:
    """Aggregate tracked time. One query for the totals, one for each breakdown."""

    entries = TimeEntry.objects.filter(organization=organization, ended_at__isnull=False)
    if since is not None:
        entries = entries.filter(started_at__gte=since)
    if user is not None:
        entries = entries.filter(user=user)
    if task is not None:
        entries = entries.filter(task=task)

    totals = entries.aggregate(
        total_seconds=Sum("duration_seconds"),
        billable_seconds=Sum("duration_seconds", filter=Q(billable=True)),
        entries=Count("id"),
    )
    total = totals["total_seconds"] or 0

    return {
        "total_seconds": total,
        "total_hours": round(total / 3600, 2),
        "billable_hours": round((totals["billable_seconds"] or 0) / 3600, 2),
        "entry_count": totals["entries"] or 0,
        "by_user": list(
            entries.values("user__username")
            .annotate(seconds=Sum("duration_seconds"), entries=Count("id"))
            .order_by("-seconds")[:20]
        ),
        "by_task": list(
            entries.values("task__id", "task__title")
            .annotate(seconds=Sum("duration_seconds"))
            .order_by("-seconds")[:20]
        ),
    }


def estimate_accuracy(*, organization) -> dict:
    """Compare tracked time against estimates — the number that improves planning."""

    tracked = (
        Task.objects.filter(
            organization=organization, estimate_minutes__isnull=False, status=Task.Status.COMPLETED
        )
        .annotate(actual_seconds=Sum("time_entries__duration_seconds"))
        .filter(actual_seconds__isnull=False)
    )

    rows = []
    over = under = 0
    for task in tracked[:200]:
        actual_minutes = (task.actual_seconds or 0) / 60
        ratio = actual_minutes / task.estimate_minutes if task.estimate_minutes else None
        if ratio is not None:
            over += 1 if ratio > 1.1 else 0
            under += 1 if ratio < 0.9 else 0
        rows.append(
            {
                "task": task.title,
                "estimate_minutes": task.estimate_minutes,
                "actual_minutes": round(actual_minutes, 1),
                "ratio": round(ratio, 2) if ratio else None,
            }
        )

    return {"rows": rows, "over_estimate": over, "under_estimate": under, "sampled": len(rows)}


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------


@transaction.atomic
def apply_template(*, template: TaskTemplate, creator, task_list=None, due_date=None) -> Task:
    """Materialise a template into a real task with its subtasks."""

    from billing.services import check_quota

    organization = template.organization
    check_quota(organization, "tasks")

    target_list = task_list or template.task_list
    if target_list is None:
        raise ValidationError("This template has no task list; choose one.")
    if target_list.organization_id != organization.pk:
        raise ValidationError("That task list belongs to a different workspace.")

    if due_date is None and template.due_in_days is not None:
        due_date = timezone.localdate() + timedelta(days=template.due_in_days)

    column = target_list.columns.order_by("position").first()
    task = Task.objects.create(
        organization=organization,
        task_list=target_list,
        column=column,
        creator=creator,
        title=template.title_template[:240],
        notes=template.notes,
        priority=template.priority,
        estimate_minutes=template.estimate_minutes,
        due_date=due_date,
        position=next_position(task_list=target_list, column=column),
    )

    labels = list(template.labels.all())
    if labels:
        task.labels.set(labels)

    Subtask.objects.bulk_create(
        [
            Subtask(task=task, title=title[:180], position=index)
            for index, title in enumerate(template.subtask_titles or [], start=1)
        ]
    )

    # F() rather than `template.times_used + 1`: the latter reads a value that may already
    # be stale, so two concurrent applications both write the same number and one is lost.
    TaskTemplate.objects.filter(pk=template.pk).update(times_used=F("times_used") + 1)

    record_activity(
        organization=organization,
        actor=creator,
        verb="task.created",
        target_type="task",
        target_id=task.pk,
        summary=f"Created from template: {template.name}",
        metadata={"template_id": template.pk},
    )

    from kernel.events import emit, task_payload

    emit(organization=organization, event="task.created", data=task_payload(task))

    if task.due_date:
        from .services import schedule_reminder

        schedule_reminder(task)

    return task


@transaction.atomic
def template_from_task(*, task: Task, name: str, created_by=None) -> TaskTemplate:
    """Capture an existing task, including its subtasks, as a reusable template."""

    due_in_days = None
    if task.due_date:
        delta = (task.due_date - timezone.localdate()).days
        due_in_days = max(0, delta)

    template = TaskTemplate.objects.create(
        organization=task.organization,
        name=name[:120],
        title_template=task.title,
        notes=task.notes,
        task_list=task.task_list,
        priority=task.priority,
        estimate_minutes=task.estimate_minutes,
        due_in_days=due_in_days,
        subtask_titles=list(task.subtasks.order_by("position").values_list("title", flat=True)),
        created_by=created_by,
    )
    labels = list(task.labels.all())
    if labels:
        template.labels.set(labels)
    return template


# --------------------------------------------------------------------------------------
# Bulk operations
# --------------------------------------------------------------------------------------


@transaction.atomic
def bulk_update_tasks(*, organization, actor, task_ids, action: str, value=None) -> dict:
    """Apply one action to many tasks in a single, tenant-scoped operation.

    Every id is re-scoped to the caller's organization before anything is written, so a
    crafted id list cannot reach another tenant's rows.
    """

    task_ids = [int(item) for item in list(task_ids)[:MAX_BULK_ITEMS] if str(item).isdigit()]
    if not task_ids:
        return {"updated": 0, "skipped": 0}

    tasks = list(Task.objects.filter(organization=organization, pk__in=task_ids))
    # A shorter result than requested means some ids were foreign or deleted; both are
    # reported as skipped rather than raising, so a partially stale UI still works.
    skipped = len(task_ids) - len(tasks)
    updated = 0

    if action == "complete":
        from .services import toggle_task_completion

        for task in tasks:
            if task.status != Task.Status.COMPLETED:
                try:
                    toggle_task_completion(task=task, actor=actor)
                    updated += 1
                except Exception:
                    skipped += 1
    elif action == "reopen":
        for task in tasks:
            if task.status == Task.Status.COMPLETED:
                task.reopen()
                updated += 1
    elif action == "archive":
        from .services import archive_task

        for task in tasks:
            archive_task(task=task, actor=actor)
            updated += 1
    elif action == "priority":
        priority = int(value)
        if priority not in dict(Task.Priority.choices):
            raise ValidationError({"value": "Unknown priority."})
        updated = Task.objects.filter(pk__in=[t.pk for t in tasks]).update(priority=priority)
    elif action == "assign":
        from django.contrib.auth import get_user_model

        from kernel.models import Membership

        User = get_user_model()
        assignee = None
        if value:
            assignee = User.objects.filter(
                pk=value,
                memberships__organization=organization,
                memberships__status=Membership.Status.ACTIVE,
            ).first()
            if assignee is None:
                raise ValidationError({"value": "That person is not a member of this workspace."})
        updated = Task.objects.filter(pk__in=[t.pk for t in tasks]).update(assignee=assignee)
    elif action == "add_label":
        label = Label.objects.filter(pk=value, organization=organization).first()
        if label is None:
            raise ValidationError({"value": "Unknown label."})
        for task in tasks:
            task.labels.add(label)
            updated += 1
    elif action == "move_list":
        from .models import TaskList

        target = TaskList.objects.filter(pk=value, organization=organization).first()
        if target is None:
            raise ValidationError({"value": "Unknown task list."})
        # The column belongs to the old list; clearing it prevents an orphaned card.
        updated = Task.objects.filter(pk__in=[t.pk for t in tasks]).update(
            task_list=target, column=None
        )
    else:
        raise ValidationError({"action": f"Unsupported bulk action {action!r}."})

    record_activity(
        organization=organization,
        actor=actor,
        verb="task.bulk_updated",
        target_type="task",
        target_id="bulk",
        summary=f"Bulk {action} on {updated} task(s)",
        metadata={"action": action, "count": updated, "skipped": skipped},
    )
    return {"updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------------------


def calendar_month(*, organization, year: int, month: int, assignee=None) -> dict:
    """Tasks grouped by due date for a month, plus the leading/trailing week padding."""

    import calendar as calendar_module

    if not 1 <= month <= 12:
        raise ValidationError({"month": "Month must be between 1 and 12."})
    if not 1970 <= year <= 2200:
        raise ValidationError({"year": "Year is out of range."})

    first = date(year, month, 1)
    last = date(year, month, calendar_module.monthrange(year, month)[1])

    # Pad to whole weeks so the grid always renders 5 or 6 complete rows.
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=6 - last.weekday())

    tasks = (
        Task.objects.filter(
            organization=organization,
            archived_at__isnull=True,
            due_date__gte=grid_start,
            due_date__lte=grid_end,
        )
        .select_related("assignee", "task_list")
        .prefetch_related("labels")
        .order_by("due_date", "-priority")
    )
    if assignee is not None:
        tasks = tasks.filter(assignee=assignee)

    by_day: dict[date, list[Task]] = {}
    for task in tasks:
        by_day.setdefault(task.due_date, []).append(task)

    weeks = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            week.append(
                {
                    "date": cursor,
                    "in_month": cursor.month == month,
                    "is_today": cursor == timezone.localdate(),
                    "tasks": by_day.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)

    return {
        "year": year,
        "month": month,
        "month_name": calendar_module.month_name[month],
        "weeks": weeks,
        "task_count": len(tasks),
        "previous": (first - timedelta(days=1)).replace(day=1),
        "next": (last + timedelta(days=1)),
    }


# --------------------------------------------------------------------------------------
# Share links
# --------------------------------------------------------------------------------------


def resolve_share_link(raw_token: str) -> ShareLink | None:
    """Look up a live share link by its raw token, or return ``None``."""

    if not raw_token:
        return None
    link = (
        ShareLink.objects.select_related("task_list", "organization")
        .filter(token_hash=ShareLink.hash_token(raw_token))
        .first()
    )
    if link is None or not link.is_live:
        return None
    return link


def shared_tasks(link: ShareLink):
    """The read-only task set a share link exposes.

    Assignee identities are omitted by the template: a public link should not enumerate a
    company's staff.
    """

    queryset = (
        Task.objects.filter(task_list=link.task_list, archived_at__isnull=True)
        .select_related("column")
        .prefetch_related("labels")
        .order_by("-priority", "due_date", "position")
    )
    if not link.include_completed:
        queryset = queryset.filter(status=Task.Status.OPEN)
    return queryset
