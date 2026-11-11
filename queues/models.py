from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from kernel.models import Organization, TimeStampedModel


class Queue(TimeStampedModel):
    """A named lane with its own concurrency, rate and retry policy."""

    class Mode(models.TextChoices):
        PUSH = "push", "Push"
        PULL = "pull", "Pull"
        INTERNAL = "internal", "Internal"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="queues")
    name = models.CharField(max_length=120)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.INTERNAL)
    paused = models.BooleanField(default=False)
    concurrency_limit = models.PositiveIntegerField(default=4)
    rate_limit_per_minute = models.PositiveIntegerField(default=120)
    #: How long a claimed job may stay leased before the reaper assumes the worker died.
    visibility_timeout_seconds = models.PositiveIntegerField(default=300)
    #: Lease-level guard: a job redelivered this many times is dead-lettered even if its
    #: attempt budget is untouched (the classic "poison message crashes the worker" case).
    max_receives = models.PositiveIntegerField(default=5)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_queue_name_per_org"),
            models.CheckConstraint(
                condition=models.Q(concurrency_limit__gte=1), name="queue_concurrency_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(visibility_timeout_seconds__gte=1),
                name="queue_visibility_timeout_positive",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def depth(self) -> int:
        """Jobs waiting to run. The number an on-call engineer actually watches."""

        return self.jobs.filter(status__in=Job.WAITING_STATUSES).count()

    @property
    def in_flight(self) -> int:
        return self.jobs.filter(status=Job.Status.LEASED).count()


class JobQuerySet(models.QuerySet):
    def runnable(self, *, now=None):
        now = now or timezone.now()
        return self.filter(
            status__in=Job.CLAIMABLE_STATUSES,
            available_at__lte=now,
            queue__paused=False,
        )

    def stalled(self, *, now=None):
        """Leased jobs whose visibility timeout has elapsed — the reaper's input."""

        return self.filter(status=Job.Status.LEASED, lease_expires_at__lte=now or timezone.now())

    def terminal(self):
        return self.filter(status__in=Job.TERMINAL_STATUSES)


class Job(TimeStampedModel):
    """The durable unit of work.

    This row — not the Celery message — is the ledger. Celery is only a way to get a worker
    to look at a row; if the broker loses the message the job is still here, still
    ``available``, and the next dispatcher tick picks it up.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AVAILABLE = "available", "Available"
        LEASED = "leased", "Leased"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        DEAD = "dead", "Dead"
        CANCELED = "canceled", "Canceled"

    class HandlerType(models.TextChoices):
        INTERNAL = "internal", "Internal"
        HTTP_DELIVERY = "http_delivery", "HTTP Delivery"
        PULL = "pull", "Pull"

    #: ``FAILED`` is a *waiting* state: it means "will retry at ``next_retry_at``". It must
    #: appear in both tuples below — omitting it from CLAIMABLE_STATUSES silently disables
    #: retry entirely, because a failed job then never becomes eligible again.
    WAITING_STATUSES = (Status.PENDING, Status.AVAILABLE, Status.FAILED)
    CLAIMABLE_STATUSES = (Status.PENDING, Status.AVAILABLE, Status.FAILED)
    TERMINAL_STATUSES = (Status.SUCCEEDED, Status.DEAD, Status.CANCELED)

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="jobs")
    queue = models.ForeignKey(Queue, on_delete=models.PROTECT, related_name="jobs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    source = models.CharField(max_length=120)
    handler_type = models.CharField(max_length=30, choices=HandlerType.choices)
    handler = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    payload_version = models.PositiveIntegerField(default=1)
    #: Together with ``source`` this is the enqueue identity. It must be derived only from
    #: stable facts (ids, scheduled instants) — never from ``now()`` — or retries duplicate.
    idempotency_key = models.CharField(max_length=180)
    priority = models.PositiveSmallIntegerField(default=2)
    available_at = models.DateTimeField(db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, blank=True)
    worker_id = models.CharField(max_length=120, blank=True)
    #: Deliveries (lease grants). Increments on redelivery after a lease expiry.
    receives = models.PositiveIntegerField(default=0)
    #: Executions actually started. Distinct from ``receives``: a crashed worker consumes a
    #: receive without producing an attempt outcome.
    attempts_made = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = JobQuerySet.as_manager()

    class Meta:
        ordering = ["-priority", "available_at", "created_at"]
        indexes = [
            models.Index(fields=["organization", "status", "available_at"]),
            models.Index(fields=["queue", "status", "available_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
            models.Index(fields=["organization", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source", "idempotency_key"],
                name="uniq_job_idempotent_enqueue",
            ),
            models.CheckConstraint(
                condition=models.Q(max_attempts__gte=1), name="job_max_attempts_positive"
            ),
        ]

    def __str__(self):
        return f"{self.handler} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts_made)

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    def clean(self):
        if self.queue_id and self.queue.organization_id != self.organization_id:
            raise ValidationError({"queue": "Queue belongs to a different organization."})


class JobAttempt(TimeStampedModel):
    """One execution of a job. The audit trail behind a retry count."""

    class Outcome(models.TextChoices):
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"
        TIMED_OUT = "timed_out", "Timed out"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.PositiveIntegerField()
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.STARTED)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    worker_id = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "attempt_number"], name="uniq_job_attempt_number"),
        ]
        indexes = [models.Index(fields=["job", "-attempt_number"])]

    def __str__(self):
        return f"attempt {self.attempt_number} of job {self.job_id}"

    def finish(self, *, outcome: str, error: str = "", result: dict | None = None):
        now = timezone.now()
        self.outcome = outcome
        self.finished_at = now
        self.duration_ms = int((now - self.started_at).total_seconds() * 1000)
        self.error = (error or "")[:8000]
        self.result = result or {}
        self.save(
            update_fields=["outcome", "finished_at", "duration_ms", "error", "result", "updated_at"]
        )
        return self


class DeadLetter(TimeStampedModel):
    """A job that exhausted its budget. Parked, alertable, and redrivable."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="dead_letters")
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="dead_letter")
    reason = models.TextField()
    redriven_at = models.DateTimeField(null=True, blank=True)
    redriven_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redriven_dead_letters",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self):
        return f"dead letter for job {self.job_id}"


class EffectMarker(TimeStampedModel):
    """Idempotency barrier for side effects that have no natural unique row.

    A handler writes a marker *inside the same transaction* as its effect. If the job is
    redelivered, the insert violates the unique constraint and the handler knows the effect
    already happened — the difference between at-least-once delivery and exactly-once effect.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="effect_markers")
    effect_type = models.CharField(max_length=80)
    natural_key = models.CharField(max_length=180)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "effect_type", "natural_key"],
                name="uniq_internal_effect_marker",
            ),
        ]
        indexes = [models.Index(fields=["organization", "effect_type"])]

    def __str__(self):
        return f"{self.effect_type}:{self.natural_key}"


class QueueMetricSnapshot(TimeStampedModel):
    """Periodic depth/latency sample per queue, written by the monitor task.

    Without a time series, "the queue is backed up" is only ever discovered by a customer.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="queue_metrics"
    )
    queue = models.ForeignKey(Queue, on_delete=models.CASCADE, related_name="metrics")
    depth = models.PositiveIntegerField(default=0)
    in_flight = models.PositiveIntegerField(default=0)
    dead_letters = models.PositiveIntegerField(default=0)
    oldest_available_age_seconds = models.PositiveIntegerField(default=0)
    succeeded_last_interval = models.PositiveIntegerField(default=0)
    failed_last_interval = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["queue", "-created_at"])]

    def __str__(self):
        return f"queue {self.queue_id} depth={self.depth}"
