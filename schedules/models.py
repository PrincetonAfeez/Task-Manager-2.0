from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from kernel.models import Organization, TimeStampedModel


class Schedule(TimeStampedModel):
    """A recurring trigger that materialises Job rows.

    Firing identity lives in :class:`ScheduleOccurrence`, not here: a schedule that fires
    twice for the same instant is a duplicate side effect, and the only reliable defence is
    a unique row per ``(schedule, scheduled_time)``.
    """

    class ExpressionType(models.TextChoices):
        CRON = "cron", "Cron"
        INTERVAL = "interval", "Interval"
        ONE_OFF = "one_off", "One-off"

    class CatchUpPolicy(models.TextChoices):
        #: Missed ticks are abandoned; only the next future tick runs.
        SKIP = "skip", "Skip missed"
        #: Collapse the whole missed window into a single run.
        RUN_ONCE = "run_once", "Run once"
        #: Replay every missed tick (bounded by SCHEDULE_CATCH_UP_MAX_OCCURRENCES).
        RUN_ALL = "run_all", "Run all"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="schedules")
    queue = models.ForeignKey("queues.Queue", on_delete=models.PROTECT, related_name="schedules")
    name = models.CharField(max_length=140)
    expression_type = models.CharField(max_length=20, choices=ExpressionType.choices)
    expression = models.CharField(max_length=240, blank=True)
    #: Cron semantics are timezone-dependent: "every day at 09:00" means the org's 09:00,
    #: which is a different UTC instant either side of a DST transition.
    timezone = models.CharField(max_length=64, default="UTC")
    target_handler = models.CharField(max_length=120, default="http_delivery")
    payload = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    catch_up_policy = models.CharField(
        max_length=20, choices=CatchUpPolicy.choices, default=CatchUpPolicy.RUN_ONCE
    )
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    #: Consecutive scan failures; a schedule that cannot be parsed is deactivated rather
    #: than re-attempted forever on every tick.
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_schedule_name_per_org"),
        ]
        indexes = [models.Index(fields=["active", "next_run_at"])]

    def __str__(self):
        return self.name

    def clean(self):
        from .services import validate_expression

        if self.queue_id and self.queue.organization_id != self.organization_id:
            raise ValidationError({"queue": "Queue belongs to a different organization."})
        try:
            validate_expression(
                self.expression_type, self.expression, self.timezone, next_run_at=self.next_run_at
            )
        except ValueError as exc:
            raise ValidationError({"expression": str(exc)}) from exc


class ScheduleOccurrence(TimeStampedModel):
    """One firing instant of a schedule. The unique constraint *is* the double-fire guard."""

    class Status(models.TextChoices):
        CLAIMED = "claimed", "Claimed"
        ENQUEUED = "enqueued", "Enqueued"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="schedule_occurrences"
    )
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name="occurrences")
    scheduled_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CLAIMED)
    job = models.ForeignKey(
        "queues.Job",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedule_occurrences",
    )
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-scheduled_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "scheduled_time"],
                name="uniq_schedule_occurrence_identity",
            ),
        ]
        indexes = [models.Index(fields=["organization", "-scheduled_time"])]

    def __str__(self):
        return f"occurrence {self.schedule_id} @ {self.scheduled_time:%Y-%m-%dT%H:%M}"
