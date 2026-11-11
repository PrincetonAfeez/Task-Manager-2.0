"""Schedule evaluation and firing.

Correctness properties this module is responsible for:

* **Timezone fidelity** — cron expressions are evaluated in the schedule's own timezone, so
  "09:00 daily" survives a DST transition instead of drifting by an hour.
* **Exactly-once firing** — a firing writes a ``ScheduleOccurrence`` first. Two scheduler
  instances racing on the same tick means one insert wins and one hits the unique
  constraint; the loser does nothing.
* **Bounded catch-up** — a scheduler that was down for a week must not enqueue ten thousand
  jobs on restart. Each policy has an explicit, capped behaviour.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ops.logging import get_logger
from queues.models import Job
from queues.services import enqueue_job

from .models import Schedule, ScheduleOccurrence

logger = get_logger("taskmanager.schedules")

INTERVAL_RE = re.compile(
    r"^\s*(?P<amount>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes"
    r"|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.I,
)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 365 * 86400


@dataclass(frozen=True)
class FireResult:
    schedule_id: int
    occurrences_created: int
    jobs_enqueued: int
    skipped: int
    error: str = ""


def get_zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except Exception as exc:
        raise ValueError(f"Unknown timezone {name!r}.") from exc


def parse_interval(expression: str) -> timedelta:
    """Parse ``"15m"`` / ``"2 hours"`` / ``"90 seconds"`` into a timedelta.

    Rejects sub-30-second intervals: the scanner ticks on the order of a minute, so a
    5-second schedule would silently under-fire and mislead the customer.
    """

    match = INTERVAL_RE.match(expression or "")
    if not match:
        raise ValueError("Interval must look like '15m', '2 hours' or '90 seconds'.")
    seconds = int(match.group("amount")) * _UNIT_SECONDS[match.group("unit").lower()]
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"Interval must be at least {MIN_INTERVAL_SECONDS} seconds.")
    if seconds > MAX_INTERVAL_SECONDS:
        raise ValueError("Interval must be at most 365 days.")
    return timedelta(seconds=seconds)


def validate_expression(expression_type, expression: str, tz_name: str = "UTC", *, next_run_at=None) -> None:
    """Raise ``ValueError`` if the schedule definition can never produce a run time."""

    get_zone(tz_name)

    if expression_type == Schedule.ExpressionType.CRON:
        if not (expression or "").strip():
            raise ValueError("A cron schedule needs an expression.")
        try:
            croniter(expression, datetime.now(tz=get_zone(tz_name)))
        except (CroniterBadCronError, CroniterBadDateError, ValueError, KeyError) as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
    elif expression_type == Schedule.ExpressionType.INTERVAL:
        parse_interval(expression)
    elif expression_type == Schedule.ExpressionType.ONE_OFF:
        if next_run_at is None:
            raise ValueError("A one-off schedule needs a scheduled time.")
    else:
        raise ValueError(f"Unsupported expression type {expression_type!r}.")


def next_run_after(schedule: Schedule, after) -> datetime | None:
    """The first firing instant strictly after ``after``, or ``None`` if there is none."""

    if schedule.expression_type == Schedule.ExpressionType.CRON:
        zone = get_zone(schedule.timezone)
        try:
            iterator = croniter(schedule.expression, after.astimezone(zone))
            return iterator.get_next(datetime).astimezone(UTC)
        except (CroniterBadCronError, CroniterBadDateError, KeyError) as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc

    if schedule.expression_type == Schedule.ExpressionType.INTERVAL:
        delta = parse_interval(schedule.expression)
        anchor = schedule.last_run_at or schedule.created_at or after
        if anchor > after:
            return anchor
        # Advance in whole intervals from the anchor so ticks stay on a fixed cadence
        # rather than drifting by the scan latency on every fire.
        steps = int((after - anchor).total_seconds() // delta.total_seconds()) + 1
        return anchor + delta * steps

    if schedule.expression_type == Schedule.ExpressionType.ONE_OFF:
        if schedule.next_run_at and schedule.next_run_at > after:
            return schedule.next_run_at
        return None

    return None


def preview_next_runs(schedule: Schedule, count: int = 5) -> list[datetime]:
    """Upcoming firing instants, for the UI. Never raises — a bad expression shows empty."""

    count = max(0, min(int(count or 0), 50))
    if count == 0:
        return []
    try:
        runs: list[datetime] = []
        cursor = timezone.now()
        for _ in range(count):
            nxt = next_run_after(schedule, cursor)
            if nxt is None:
                break
            runs.append(nxt)
            cursor = nxt
        return runs
    except (ValueError, CroniterBadCronError, CroniterBadDateError):
        logger.warning("schedule preview failed", extra={"schedule_id": schedule.pk})
        return []


def _due_instants(schedule: Schedule, *, now) -> list[datetime]:
    """Every firing instant at or before ``now`` that has not been claimed yet.

    Applies the catch-up policy across a scheduler outage window.
    """

    max_occurrences = getattr(settings, "SCHEDULE_CATCH_UP_MAX_OCCURRENCES", 100)
    anchor = schedule.next_run_at

    if anchor is None:
        anchor = next_run_after(schedule, schedule.last_run_at or now - timedelta(seconds=1))
    if anchor is None or anchor > now:
        return []

    if schedule.expression_type == Schedule.ExpressionType.ONE_OFF:
        return [anchor]

    instants = [anchor]
    cursor = anchor
    while len(instants) < max_occurrences:
        nxt = next_run_after(schedule, cursor)
        if nxt is None or nxt > now:
            break
        instants.append(nxt)
        cursor = nxt

    policy = schedule.catch_up_policy
    if policy == Schedule.CatchUpPolicy.SKIP:
        # Only the most recent tick matters, and only if it is genuinely current: an old
        # missed tick is dropped entirely.
        horizon = getattr(settings, "SCHEDULE_SCAN_HORIZON_SECONDS", 60)
        latest = instants[-1]
        return [latest] if (now - latest).total_seconds() <= horizon else []
    if policy == Schedule.CatchUpPolicy.RUN_ONCE:
        return instants[-1:]
    return instants


def fire_schedule(schedule: Schedule, *, now=None) -> FireResult:
    """Materialise every due occurrence for one schedule and enqueue its jobs."""

    now = now or timezone.now()
    created = enqueued = skipped = 0

    try:
        instants = _due_instants(schedule, now=now)
    except ValueError as exc:
        failures = schedule.consecutive_failures + 1
        Schedule.objects.filter(pk=schedule.pk).update(
            consecutive_failures=failures,
            last_error=str(exc)[:2000],
            # Three bad evaluations in a row means the expression is broken, not flaky.
            active=failures < 3,
        )
        logger.error("schedule evaluation failed", extra={"schedule_id": schedule.pk, "error": str(exc)})
        return FireResult(schedule.pk, 0, 0, 0, error=str(exc))

    for instant in instants:
        try:
            with transaction.atomic():
                occurrence = ScheduleOccurrence.objects.create(
                    organization_id=schedule.organization_id,
                    schedule=schedule,
                    scheduled_time=instant,
                    status=ScheduleOccurrence.Status.CLAIMED,
                )
                created += 1

                result = enqueue_job(
                    organization=schedule.organization,
                    queue=schedule.queue,
                    source=f"schedule:{schedule.pk}",
                    handler_type=(
                        Job.HandlerType.HTTP_DELIVERY
                        if schedule.target_handler == "http_delivery"
                        else Job.HandlerType.INTERNAL
                    ),
                    handler=schedule.target_handler,
                    payload={
                        **schedule.payload,
                        "schedule_id": schedule.pk,
                        "occurrence_id": occurrence.pk,
                        "scheduled_time": instant.isoformat(),
                    },
                    # Identity is the instant, not the wall clock: a retried scan produces
                    # the same key and therefore the same single job.
                    idempotency_key=f"schedule:{schedule.pk}:{instant.isoformat()}"[:180],
                    available_at=max(instant, now),
                )
                occurrence.job = result.job
                occurrence.status = ScheduleOccurrence.Status.ENQUEUED
                occurrence.save(update_fields=["job", "status", "updated_at"])
                enqueued += 1
        except IntegrityError:
            # Another scheduler instance claimed this instant first. Correct outcome.
            skipped += 1

    try:
        following = next_run_after(schedule, now)
    except ValueError:
        following = None

    is_finished_one_off = (
        schedule.expression_type == Schedule.ExpressionType.ONE_OFF and following is None and bool(instants)
    )
    Schedule.objects.filter(pk=schedule.pk).update(
        next_run_at=following,
        last_run_at=instants[-1] if instants else schedule.last_run_at,
        consecutive_failures=0,
        last_error="",
        active=schedule.active and not is_finished_one_off,
    )

    if created:
        logger.info(
            "schedule fired",
            extra={"schedule_id": schedule.pk, "occurrences": created, "jobs": enqueued},
        )
    return FireResult(schedule.pk, created, enqueued, skipped)


def scan_due_schedules(*, now=None, limit: int = 200) -> dict:
    """Fire every active schedule that is due. The scheduler process's whole job."""

    now = now or timezone.now()
    horizon = now + timedelta(seconds=getattr(settings, "SCHEDULE_SCAN_HORIZON_SECONDS", 60))

    candidates = (
        Schedule.objects.filter(active=True, organization__deleted_at__isnull=True)
        .filter(Q(next_run_at__lte=horizon) | Q(next_run_at__isnull=True))
        .select_related("queue", "organization")[:limit]
    )

    totals = {"schedules": 0, "occurrences": 0, "jobs": 0, "skipped": 0, "errors": 0}
    for schedule in candidates:
        result = fire_schedule(schedule, now=now)
        totals["schedules"] += 1
        totals["occurrences"] += result.occurrences_created
        totals["jobs"] += result.jobs_enqueued
        totals["skipped"] += result.skipped
        if result.error:
            totals["errors"] += 1
    return totals


def bootstrap_next_run(schedule: Schedule, *, now=None) -> Schedule:
    """Set ``next_run_at`` for a freshly created or edited schedule."""

    now = now or timezone.now()
    if schedule.expression_type == Schedule.ExpressionType.ONE_OFF:
        return schedule
    try:
        schedule.next_run_at = next_run_after(schedule, now)
    except ValueError:
        schedule.next_run_at = None
    schedule.save(update_fields=["next_run_at", "updated_at"])
    return schedule
