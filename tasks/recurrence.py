"""Recurrence engine.

The scaffold parsed "weekly" out of quick-add and threw it away; ``RecurrenceRule`` existed
but nothing ever advanced it. This module closes that loop.

Materialisation is idempotent by construction: the ``(source_task, next_occurrence_date)``
unique constraint means a duplicate sweep or a redelivered job cannot create two copies of
the same occurrence.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from ops.logging import get_logger

from .models import RecurrenceRule, Task
from .services import next_position, record_activity, schedule_reminder

logger = get_logger("taskmanager.recurrence")

MAX_SWEEP_ITERATIONS = 500


def add_months(start: date, months: int) -> date:
    """Add calendar months, clamping to the last valid day.

    31 January + 1 month is 28/29 February, not an error and not 3 March. Clamping is what
    every calendar application does and what users expect.
    """

    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_occurrence_date(rule: RecurrenceRule, *, after: date | None = None) -> date | None:
    """The next date this rule should produce a task for, or ``None`` when exhausted."""

    current = after or rule.next_occurrence_date
    interval = max(1, rule.interval)

    if rule.frequency == RecurrenceRule.Frequency.DAILY:
        candidate = current + timedelta(days=interval)
    elif rule.frequency == RecurrenceRule.Frequency.WEEKLY:
        candidate = _next_weekly(current, rule)
    elif rule.frequency == RecurrenceRule.Frequency.MONTHLY:
        candidate = add_months(current, interval)
    elif rule.frequency == RecurrenceRule.Frequency.YEARLY:
        candidate = add_months(current, 12 * interval)
    else:  # pragma: no cover - guarded by model choices
        return None

    if rule.until_date and candidate > rule.until_date:
        return None
    return candidate


def _next_weekly(current: date, rule: RecurrenceRule) -> date:
    """Advance a weekly rule, honouring a multi-day weekday set when one is configured."""

    weekdays = sorted({int(day) for day in (rule.weekdays or []) if 1 <= int(day) <= 7})
    if not weekdays:
        return current + timedelta(weeks=max(1, rule.interval))

    current_iso = current.isoweekday()
    later_this_week = [day for day in weekdays if day > current_iso]
    if later_this_week:
        return current + timedelta(days=later_this_week[0] - current_iso)
    # Wrap to the first configured weekday of the next interval week.
    days_to_week_end = 7 - current_iso
    weeks_forward = max(1, rule.interval) - 1
    return current + timedelta(days=days_to_week_end + weeks_forward * 7 + weekdays[0])


@transaction.atomic
def materialize_next_occurrence(rule: RecurrenceRule) -> Task | None:
    """Create the task for ``rule.next_occurrence_date`` and advance the rule.

    Returns the new task, or ``None`` when the rule is exhausted or the occurrence already
    exists (a redelivery).
    """

    rule = RecurrenceRule.objects.select_related("source_task", "organization").get(pk=rule.pk)
    if rule.is_exhausted:
        return None

    source = rule.source_task
    occurrence_date = rule.next_occurrence_date
    column = source.task_list.columns.order_by("position").first()

    # Occurrence identity is enforced by Task's uniq_recurrence_occurrence_per_date
    # constraint. The pre-check keeps the common case cheap; the try/except is what makes
    # it correct when two sweeps run at once.
    if Task.objects.filter(recurrence_source=source, due_date=occurrence_date).exists():
        _advance(rule, occurrence_date)
        return None

    try:
        with transaction.atomic():
            task = Task.objects.create(
                organization=source.organization,
                task_list=source.task_list,
                column=column,
                creator=source.creator,
                assignee=source.assignee,
                title=source.title,
                notes=source.notes,
                due_date=occurrence_date,
                due_time=source.due_time,
                priority=source.priority,
                estimate_minutes=source.estimate_minutes,
                recurrence_source=source,
                position=next_position(task_list=source.task_list, column=column),
            )
    except IntegrityError:
        # Lost the race. The winner's task is the occurrence; just advance the rule.
        _advance(rule, occurrence_date)
        return None
    labels = list(source.labels.all())
    if labels:
        task.labels.set(labels)

    if task.due_date:
        schedule_reminder(task)

    record_activity(
        organization=rule.organization,
        actor=None,
        verb="task.recurred",
        target_type="task",
        target_id=task.pk,
        summary=f"Created recurring task: {task.title}",
        metadata={"recurrence_id": rule.pk, "source_task_id": source.pk},
    )

    _advance(rule, occurrence_date)
    logger.info(
        "recurrence materialised",
        extra={"recurrence_id": rule.pk, "task_id": task.pk, "due_date": str(occurrence_date)},
    )
    return task


def _advance(rule: RecurrenceRule, from_date: date) -> None:
    """Move the rule to its next date, deactivating it when the series ends."""

    upcoming = next_occurrence_date(rule, after=from_date)
    occurrences = rule.occurrences_created + 1
    exhausted = upcoming is None or (
        rule.max_occurrences is not None and occurrences >= rule.max_occurrences
    )
    RecurrenceRule.objects.filter(pk=rule.pk).update(
        next_occurrence_date=upcoming or from_date,
        occurrences_created=occurrences,
        active=not exhausted,
    )


def sweep_due_recurrences(*, today=None, limit: int = 200) -> dict:
    """Materialise every rule whose next occurrence has arrived.

    The safety net for date-driven recurrences: completion-driven ones advance immediately
    via ``advance_recurrence_on_completion``, but a rule whose source task is never
    completed still needs to produce its occurrences.
    """

    today = today or timezone.localdate()
    rules = (
        RecurrenceRule.objects.filter(
            active=True,
            next_occurrence_date__lte=today,
            organization__deleted_at__isnull=True,
        )
        .select_related("source_task", "organization")
        .order_by("next_occurrence_date")[:limit]
    )

    created = skipped = 0
    for rule in rules:
        # A rule far behind (e.g. daily, dormant for a year) would otherwise create one
        # task per missed day. Bounded catch-up: fast-forward and create only the current one.
        guard = 0
        while rule.next_occurrence_date < today and not rule.is_exhausted and guard < MAX_SWEEP_ITERATIONS:
            upcoming = next_occurrence_date(rule)
            if upcoming is None:
                break
            RecurrenceRule.objects.filter(pk=rule.pk).update(next_occurrence_date=upcoming)
            rule.next_occurrence_date = upcoming
            guard += 1

        if rule.is_exhausted:
            skipped += 1
            continue
        if materialize_next_occurrence(rule) is not None:
            created += 1
        else:
            skipped += 1

    return {"scanned": len(rules), "created": created, "skipped": skipped}
