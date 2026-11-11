"""Domain services for tasks.

Views and API endpoints call into this module rather than mutating models directly, so a
task created through quick-add, the REST API, or a recurrence sweep all produce the same
activity records, reminders and notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from kernel.events import comment_payload, emit, task_payload
from kernel.models import Activity, Organization
from ops.logging import get_logger
from queues.models import Job
from queues.services import enqueue_job, get_internal_queue

from .models import (
    POSITION_MIN_GAP,
    POSITION_STEP,
    Column,
    Comment,
    Label,
    Reminder,
    Task,
    TaskDependency,
    TaskList,
)
from .notifications import notify_assignment, notify_mentions
from .quickadd import DEFAULT_DUE_TIME, combine_due_datetime, parse_quick_add

logger = get_logger("taskmanager.tasks")

#: How far ahead of the due time a reminder fires.
REMINDER_LEAD = timedelta(minutes=30)

#: Cards rendered per board lane before the rest is loaded on demand.
BOARD_LANE_PAGE_SIZE = 50


class ConcurrentModification(Exception):
    """The caller's ``expected_version`` no longer matches the stored row."""


class DependencyBlocked(Exception):
    """A task cannot be completed while an incomplete dependency remains."""


class WipLimitExceeded(Exception):
    """A move would push a column past its configured WIP limit."""


# --------------------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------------------


def record_activity(*, organization, actor, verb, target_type, target_id, summary, metadata=None):
    return Activity.objects.create(
        organization=organization,
        actor=actor if getattr(actor, "pk", None) else None,
        verb=verb,
        target_type=target_type,
        target_id=str(target_id),
        summary=summary[:255],
        metadata=metadata or {},
    )


# --------------------------------------------------------------------------------------
# Positioning
# --------------------------------------------------------------------------------------


def next_position(*, task_list: TaskList, column: Column | None) -> Decimal:
    """Append position for a new card: one step past the current maximum."""

    queryset = Task.objects.filter(task_list=task_list, archived_at__isnull=True)
    queryset = (
        queryset.filter(column=column) if column is not None else queryset.filter(column__isnull=True)
    )
    current_max = queryset.aggregate(value=Max("position"))["value"]
    return (current_max or Decimal("0")) + POSITION_STEP


def position_between(before: Decimal | None, after: Decimal | None) -> Decimal:
    """Midpoint of two neighbours, for drag-and-drop insertion."""

    if before is None and after is None:
        return POSITION_STEP
    if before is None:
        return after - POSITION_STEP
    if after is None:
        return before + POSITION_STEP
    return (before + after) / 2


def needs_renumber(before: Decimal | None, after: Decimal | None) -> bool:
    """Fractional insertion eventually exhausts the gap between two neighbours."""

    return before is not None and after is not None and abs(after - before) < POSITION_MIN_GAP


def renumber_column(*, task_list: TaskList, column: Column | None) -> None:
    """Rewrite positions onto a clean ``POSITION_STEP`` ladder. The O(n) escape hatch."""

    queryset = Task.objects.filter(task_list=task_list, archived_at__isnull=True)
    queryset = (
        queryset.filter(column=column) if column is not None else queryset.filter(column__isnull=True)
    )
    for index, task in enumerate(queryset.order_by("position", "created_at"), start=1):
        Task.objects.filter(pk=task.pk).update(position=POSITION_STEP * index)


# --------------------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------------------


def task_due_datetime(task: Task):
    """The aware instant a task is due, or ``None`` when it has no date."""

    if not task.due_date:
        return None
    tz = timezone.get_current_timezone()
    return datetime.combine(task.due_date, task.due_time or DEFAULT_DUE_TIME).replace(tzinfo=tz)


def schedule_reminder(task: Task, *, due_at=None) -> Reminder | None:
    """Arm (or re-arm) the reminder for a task's due time.

    The job's idempotency key is derived from the reminder *row* and its instant, never
    from ``now()``. Keying on the wall clock — as the original code did — produced a fresh
    key on every call, which defeated the whole point of an idempotent enqueue.

    Two rules keep a task's reminders bounded however often it is completed and reopened
    (a load test found one task with thousands of rows, each loaded by the dashboard):

    * a task is reminded **once per due time** — reopening an overdue task that has already
      been reminded about does not notify again;
    * a cancelled reminder is superseded, not history. It is removed when the next one is
      armed; the cancelled job in the ledger remains the record.
    """

    due_at = due_at or task_due_datetime(task)
    if due_at is None:
        return None

    now = timezone.now()
    remind_at = due_at - REMINDER_LEAD
    if remind_at < now:
        already_reminded = Reminder.objects.filter(
            task=task, status=Reminder.Status.SENT, remind_at__gte=remind_at.replace(microsecond=0)
        ).exists()
        if already_reminded:
            return None
        # Past-due task that has never been reminded about: do it now rather than never.
        remind_at = now
    # Second precision keeps the idempotency key stable across microsecond jitter.
    remind_at = remind_at.replace(microsecond=0)

    existing = Reminder.objects.filter(task=task, remind_at=remind_at).first()
    if existing is not None and existing.status != Reminder.Status.CANCELED:
        # Armed already, or already sent for this exact instant.
        return existing

    # Any earlier armed reminder for this task is now wrong — cancel it and its job. A
    # cancelled row for this same instant is replaced rather than returned: returning it
    # (as this function used to) left a completed-then-reopened task with no reminder at all.
    cancel_reminders(task)
    Reminder.objects.filter(task=task, status=Reminder.Status.CANCELED).delete()

    reminder = Reminder.objects.create(
        organization=task.organization,
        task=task,
        remind_at=remind_at,
    )
    result = enqueue_job(
        organization=task.organization,
        queue=get_internal_queue(task.organization),
        source=f"task:{task.pk}",
        handler_type=Job.HandlerType.INTERNAL,
        handler="internal:send_reminder",
        payload={
            "task_id": task.pk,
            "reminder_id": reminder.pk,
            "title": task.title,
            "remind_at": remind_at.isoformat(),
        },
        # The row id is part of the key: a reminder re-armed for the same instant is a new
        # row and must get a live job, not be handed back the cancelled one.
        idempotency_key=f"reminder:{task.pk}:{reminder.pk}:{remind_at.isoformat()}"[:180],
        available_at=remind_at,
    )
    Reminder.objects.filter(pk=reminder.pk).update(job=result.job)
    reminder.job = result.job
    return reminder


def cancel_reminders(task: Task) -> int:
    """Disarm every pending reminder for a task (on completion, archive, or reschedule)."""

    from queues.services import cancel_job

    count = 0
    for reminder in Reminder.objects.filter(task=task, status=Reminder.Status.ARMED).select_related("job"):
        reminder.cancel()
        if reminder.job_id:
            cancel_job(reminder.job, reason="Task no longer needs this reminder.")
        count += 1
    return count


# --------------------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------------------


@transaction.atomic
def create_task_from_quick_add(*, organization, task_list, creator, raw: str, now=None) -> Task:
    """Parse a quick-add string and materialise everything it implies."""

    from billing.services import check_quota

    # Checked before the write, not after: a post-hoc check means rolling back, which is
    # slower and risks leaving partial state behind.
    check_quota(organization, "tasks")

    parsed = parse_quick_add(raw, now=now)
    column = task_list.columns.order_by("position").first()

    task = Task.objects.create(
        organization=organization,
        task_list=task_list,
        column=column,
        creator=creator,
        title=parsed.title,
        due_date=parsed.due_date,
        due_time=parsed.due_time,
        priority=parsed.priority,
        position=next_position(task_list=task_list, column=column),
    )

    for label_name in parsed.labels:
        label, _ = Label.objects.get_or_create(
            organization=organization,
            name=label_name,
            defaults={"color": "#0f766e"},
        )
        task.labels.add(label)

    record_activity(
        organization=organization,
        actor=creator,
        verb="task.created",
        target_type="task",
        target_id=task.pk,
        summary=f"Created task: {task.title}",
        metadata={"quick_add": raw[:500]},
    )

    due_at = combine_due_datetime(parsed)
    if due_at:
        schedule_reminder(task, due_at=due_at)

    if parsed.recurrence:
        create_recurrence(task=task, frequency=parsed.recurrence)

    if parsed.mentions:
        # Deferred to commit: the mention notification enqueues a job, and a rolled-back
        # transaction must not leave an email queued for a task that does not exist.
        mentions = list(parsed.mentions)
        transaction.on_commit(
            lambda: notify_mentions(
                organization=organization, task=task, mentions=mentions, actor=creator
            )
        )

    emit(organization=organization, event="task.created", data=task_payload(task))
    return task


def create_recurrence(*, task: Task, frequency: str, interval: int = 1, weekdays=None, until_date=None):
    from .models import RecurrenceRule

    frequency = (frequency or "").lower()
    valid = {choice.value for choice in RecurrenceRule.Frequency}
    if frequency not in valid:
        raise ValidationError({"frequency": f"Unsupported recurrence frequency {frequency!r}."})

    base_date = task.due_date or timezone.localdate()
    rule, _ = RecurrenceRule.objects.get_or_create(
        source_task=task,
        defaults={
            "organization": task.organization,
            "frequency": frequency,
            "interval": max(1, int(interval or 1)),
            "weekdays": list(weekdays or []),
            "next_occurrence_date": base_date,
            "until_date": until_date,
        },
    )
    return rule


# --------------------------------------------------------------------------------------
# Mutation
# --------------------------------------------------------------------------------------


def _check_version(task: Task, expected_version: int | None) -> None:
    if expected_version is not None and int(expected_version) != task.version:
        raise ConcurrentModification(
            f"This task was changed by someone else (you had version {expected_version}, "
            f"it is now version {task.version}). Reload and try again."
        )


@transaction.atomic
def toggle_task_completion(*, task: Task, actor, expected_version: int | None = None) -> Task:
    """Complete or reopen a task, honouring dependencies and optimistic concurrency."""

    task = Task.objects.select_related("organization").get(pk=task.pk)
    _check_version(task, expected_version)

    if task.status == Task.Status.COMPLETED:
        task.reopen()
        verb, summary = "task.reopened", f"Reopened task: {task.title}"
        if task.due_date:
            schedule_reminder(task)
    else:
        blockers = list(
            TaskDependency.objects.filter(blocked_task=task)
            .exclude(blocking_task__status=Task.Status.COMPLETED)
            .select_related("blocking_task")[:5]
        )
        if blockers:
            raise DependencyBlocked(
                "Blocked by: " + ", ".join(dep.blocking_task.title for dep in blockers)
            )
        task.complete(actor=actor)
        cancel_reminders(task)
        verb, summary = "task.completed", f"Completed task: {task.title}"
        advance_recurrence_on_completion(task)

    record_activity(
        organization=task.organization,
        actor=actor,
        verb=verb,
        target_type="task",
        target_id=task.pk,
        summary=summary,
    )
    emit(
        organization=task.organization,
        event="task.completed" if task.status == Task.Status.COMPLETED else "task.updated",
        data=task_payload(task),
    )
    return task


@transaction.atomic
def update_task(*, task: Task, actor, expected_version: int | None = None, **fields) -> Task:
    """Apply a field-level edit with optimistic concurrency and side effects."""

    task = Task.objects.select_related("organization").get(pk=task.pk)
    _check_version(task, expected_version)

    editable = {
        "title",
        "notes",
        "due_date",
        "due_time",
        "priority",
        "assignee",
        "column",
        "estimate_minutes",
    }
    previous_assignee_id = task.assignee_id
    previous_due = (task.due_date, task.due_time)

    changed = []
    for key, value in fields.items():
        if key not in editable:
            continue
        if getattr(task, key) != value:
            setattr(task, key, value)
            changed.append(key)

    if not changed:
        return task

    task.version += 1
    task.clean()
    task.save(update_fields=[*changed, "version", "updated_at"])

    if ("due_date" in changed or "due_time" in changed) and (task.due_date, task.due_time) != previous_due:
        cancel_reminders(task)
        if task.due_date and task.status == Task.Status.OPEN:
            schedule_reminder(task)

    if "assignee" in changed and task.assignee_id != previous_assignee_id:
        transaction.on_commit(lambda: notify_assignment(task=task, actor=actor))

    record_activity(
        organization=task.organization,
        actor=actor,
        verb="task.updated",
        target_type="task",
        target_id=task.pk,
        summary=f"Updated task: {task.title}",
        metadata={"fields": changed},
    )
    emit(
        organization=task.organization,
        event="task.assigned" if "assignee" in changed else "task.updated",
        data=task_payload(task),
    )
    return task


@transaction.atomic
def move_task(
    *,
    task: Task,
    actor,
    column: Column | None,
    before_id: int | None = None,
    after_id: int | None = None,
    expected_version: int | None = None,
) -> Task:
    """Move a task on the board: reposition, optionally into a different column."""

    task = Task.objects.select_related("organization", "task_list").get(pk=task.pk)
    _check_version(task, expected_version)

    if column is not None:
        if column.task_list_id != task.task_list_id:
            raise ValidationError({"column": "Column belongs to a different task list."})
        if column.wip_limit and column.pk != task.column_id:
            occupied = Task.objects.filter(
                column=column, archived_at__isnull=True, status=Task.Status.OPEN
            ).count()
            if occupied >= column.wip_limit:
                raise WipLimitExceeded(
                    f"Column '{column.name}' is at its WIP limit of {column.wip_limit}."
                )

    def neighbour_queryset():
        queryset = Task.objects.filter(task_list=task.task_list, archived_at__isnull=True).exclude(
            pk=task.pk
        )
        return (
            queryset.filter(column=column) if column is not None else queryset.filter(column__isnull=True)
        )

    def anchor(position_id):
        if not position_id:
            return None
        return neighbour_queryset().filter(pk=position_id).values_list("position", flat=True).first()

    before, after = anchor(before_id), anchor(after_id)

    if before_id is None and after_id is None:
        # No anchors: append to the end of the target column.
        new_position = (
            neighbour_queryset().aggregate(v=Max("position"))["v"] or Decimal("0")
        ) + POSITION_STEP
    else:
        if needs_renumber(before, after):
            renumber_column(task_list=task.task_list, column=column)
            before, after = anchor(before_id), anchor(after_id)
        new_position = position_between(before, after)

    previous_column_id = task.column_id
    task.column = column
    task.position = new_position
    task.version += 1

    # A board where "Done" does not complete the task is a board people stop trusting.
    if column is not None and column.is_done and task.status == Task.Status.OPEN:
        task.status = Task.Status.COMPLETED
        task.completed_at = timezone.now()
        task.completed_by = actor if getattr(actor, "pk", None) else None
        cancel_reminders(task)
    elif column is not None and not column.is_done and task.status == Task.Status.COMPLETED:
        task.status = Task.Status.OPEN
        task.completed_at = None
        task.completed_by = None

    task.save(
        update_fields=[
            "column",
            "position",
            "status",
            "completed_at",
            "completed_by",
            "version",
            "updated_at",
        ]
    )

    if previous_column_id != task.column_id:
        record_activity(
            organization=task.organization,
            actor=actor,
            verb="task.moved",
            target_type="task",
            target_id=task.pk,
            summary=f"Moved task: {task.title}",
            metadata={"from_column": previous_column_id, "to_column": task.column_id},
        )
    return task


@transaction.atomic
def archive_task(*, task: Task, actor) -> Task:
    task.archive()
    cancel_reminders(task)
    record_activity(
        organization=task.organization,
        actor=actor,
        verb="task.archived",
        target_type="task",
        target_id=task.pk,
        summary=f"Archived task: {task.title}",
    )
    emit(organization=task.organization, event="task.archived", data=task_payload(task))
    return task


@transaction.atomic
def create_comment(*, task: Task, author, body: str) -> Comment:
    """Create a comment and fan out the notification and webhook."""

    comment = Comment.objects.create(
        organization=task.organization, task=task, author=author, body=body.strip()
    )
    record_activity(
        organization=task.organization,
        actor=author,
        verb="comment.created",
        target_type="task",
        target_id=task.pk,
        summary=f"Commented on: {task.title}",
    )
    emit(
        organization=task.organization, event="comment.created", data=comment_payload(comment)
    )
    return comment


@transaction.atomic
def add_dependency(*, blocked_task: Task, blocking_task: Task) -> TaskDependency:
    """Record that ``blocked_task`` waits on ``blocking_task``, rejecting cycles."""

    if blocked_task.pk == blocking_task.pk:
        raise ValidationError("A task cannot block itself.")
    if blocked_task.organization_id != blocking_task.organization_id:
        raise ValidationError("Both tasks must belong to the same workspace.")
    if _would_create_cycle(blocked_task_id=blocked_task.pk, blocking_task_id=blocking_task.pk):
        raise ValidationError("That dependency would create a cycle.")

    dependency, _ = TaskDependency.objects.get_or_create(
        organization=blocked_task.organization,
        blocked_task=blocked_task,
        blocking_task=blocking_task,
    )
    return dependency


def _would_create_cycle(*, blocked_task_id: int, blocking_task_id: int) -> bool:
    """Walk the dependency graph from the prospective blocker looking for the blocked task.

    Iterative with a visited set: a recursive walk would blow the stack on a long chain,
    and a malformed graph must not be able to crash the request.
    """

    seen = {blocking_task_id}
    frontier = [blocking_task_id]
    while frontier:
        current = frontier.pop()
        upstream = TaskDependency.objects.filter(blocked_task_id=current).values_list(
            "blocking_task_id", flat=True
        )
        for node in upstream:
            if node == blocked_task_id:
                return True
            if node not in seen:
                seen.add(node)
                frontier.append(node)
    return False


def advance_recurrence_on_completion(task: Task) -> None:
    """Queue the next occurrence when a recurring task is completed."""

    rule = getattr(task, "recurrence", None)
    if rule is None or rule.is_exhausted:
        return
    enqueue_job(
        organization=task.organization,
        queue=get_internal_queue(task.organization),
        source=f"recurrence:{rule.pk}",
        handler_type=Job.HandlerType.INTERNAL,
        handler="internal:materialize_recurrence",
        payload={"recurrence_id": rule.pk, "completed_task_id": task.pk},
        idempotency_key=f"recurrence:{rule.pk}:{rule.next_occurrence_date.isoformat()}"[:180],
    )


# --------------------------------------------------------------------------------------
# Workspace bootstrap and read models
# --------------------------------------------------------------------------------------


def bootstrap_workspace(*, organization: Organization, owner) -> TaskList:
    """Give a new workspace the minimum structure needed to be usable immediately."""

    task_list, created = TaskList.objects.get_or_create(
        organization=organization,
        name="Inbox",
        defaults={"position": 1},
    )
    task_list.ensure_default_columns()
    get_internal_queue(organization)
    if created:
        record_activity(
            organization=organization,
            actor=owner,
            verb="workspace.created",
            target_type="organization",
            target_id=organization.pk,
            summary=f"Created workspace {organization.name}",
        )
    return task_list


@dataclass(frozen=True)
class BoardLane:
    """One board column's visible page of cards plus the true size of the lane."""

    column: Column | None
    cards: list = field(default_factory=list)
    total: int = 0
    offset: int = 0

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.cards)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.next_offset)


def _board_tasks(task_list: TaskList):
    return Task.objects.filter(task_list=task_list, archived_at__isnull=True)


def _lane_totals(task_list: TaskList) -> dict[int | None, int]:
    rows = _board_tasks(task_list).order_by().values("column_id").annotate(total=Count("id"))
    return {row["column_id"]: row["total"] for row in rows}


def _lane_cards(task_list: TaskList, column, *, offset: int, limit: int) -> list[Task]:
    return list(
        _board_tasks(task_list)
        .filter(column=column)
        .select_related("column", "task_list", "assignee")
        .order_by("position", "-created_at", "id")[offset : offset + limit]
    )


def board_snapshot(*, task_list: TaskList, per_lane: int | None = None):
    """Columns with the first page of their ordered cards.

    A lane is bounded on purpose. Rendering every card made the page cost grow with the
    list: measured against a 2,500-task list the board took 4.6 s of server time and
    starved every other request sharing its workers. A "done" column only ever grows, so
    any unbounded render eventually reaches that point.

    Each lane is read separately so that it is one short walk of the
    ``(task_list, column, position)`` index. A single window-function query over the whole
    list was tried first and still had to rank every card (91 ms at 29,000 tasks against
    0.2 ms this way). The query count depends on the number of columns, never on the
    number of tasks.
    """

    from django.db.models import prefetch_related_objects

    per_lane = per_lane or BOARD_LANE_PAGE_SIZE
    columns = list(task_list.columns.order_by("position", "name"))
    totals = _lane_totals(task_list)

    lanes = [
        BoardLane(
            column=column,
            cards=_lane_cards(task_list, column, offset=0, limit=per_lane) if totals.get(column.pk) else [],
            total=totals.get(column.pk, 0),
        )
        for column in columns
    ]
    unassigned = BoardLane(
        column=None,
        cards=_lane_cards(task_list, None, offset=0, limit=per_lane) if totals.get(None) else [],
        total=totals.get(None, 0),
    )
    # One label query for every card on the board, rather than one per lane.
    prefetch_related_objects([card for lane in (*lanes, unassigned) for card in lane.cards], "labels")
    return lanes, unassigned


def board_lane_page(
    *, task_list: TaskList, column: Column | None, offset: int = 0, limit: int | None = None
) -> BoardLane:
    """A later page of one lane, for the board's "show more" control."""

    limit = limit or BOARD_LANE_PAGE_SIZE
    offset = max(0, offset)
    from django.db.models import prefetch_related_objects

    cards = _lane_cards(task_list, column, offset=offset, limit=limit)
    prefetch_related_objects(cards, "labels")
    total = _board_tasks(task_list).filter(column=column).count()
    return BoardLane(column=column, cards=cards, total=total, offset=offset)


def workspace_counts(*, organization, today=None) -> dict:
    """Headline metrics for the dashboard, computed in one aggregate query."""

    today = today or timezone.localdate()
    return Task.objects.filter(organization=organization, archived_at__isnull=True).aggregate(
        open=Count("id", filter=Q(status=Task.Status.OPEN)),
        today=Count("id", filter=Q(status=Task.Status.OPEN, due_date=today)),
        overdue=Count("id", filter=Q(status=Task.Status.OPEN, due_date__lt=today)),
        completed=Count("id", filter=Q(status=Task.Status.COMPLETED)),
        unscheduled=Count("id", filter=Q(status=Task.Status.OPEN, due_date__isnull=True)),
    )
