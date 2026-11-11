from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from kernel.models import Organization, TimeStampedModel


class WebhookEvent(models.TextChoices):
    """Events a customer can subscribe to. Stable strings — they are a public contract."""

    TASK_CREATED = "task.created", "Task created"
    TASK_UPDATED = "task.updated", "Task updated"
    TASK_COMPLETED = "task.completed", "Task completed"
    TASK_ASSIGNED = "task.assigned", "Task assigned"
    TASK_ARCHIVED = "task.archived", "Task archived"
    COMMENT_CREATED = "comment.created", "Comment created"
    MEMBER_JOINED = "member.joined", "Member joined"
    SCHEDULE_FIRED = "schedule.fired", "Schedule fired"


class WebhookEndpoint(TimeStampedModel):
    """A customer-controlled HTTPS URL that receives signed event deliveries."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="webhook_endpoints"
    )
    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500)
    #: Shared secret used for the HMAC signature. Generated here, shown to the customer
    #: once, and never needed in plaintext again by us.
    secret = models.CharField(max_length=80)
    events = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)

    #: Circuit breaker. An endpoint that has been failing for a long time is disabled so
    #: the queue is not permanently occupied delivering to a dead host.
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)

    FAILURE_THRESHOLD = 20

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_webhooks",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "url"], name="uniq_webhook_url_per_org"
            ),
        ]
        indexes = [models.Index(fields=["organization", "active"])]

    def __str__(self):
        return f"{self.name} -> {self.url}"

    @staticmethod
    def generate_secret() -> str:
        return f"whsec_{secrets.token_urlsafe(32)}"

    def save(self, *args, **kwargs):
        if not self.secret:
            self.secret = self.generate_secret()
        return super().save(*args, **kwargs)

    def clean(self):
        from .delivery import validate_webhook_url

        try:
            validate_webhook_url(self.url)
        except ValueError as exc:
            raise ValidationError({"url": str(exc)}) from exc

        unknown = set(self.events or []) - {choice.value for choice in WebhookEvent}
        if unknown:
            raise ValidationError({"events": f"Unknown events: {', '.join(sorted(unknown))}"})

    @property
    def is_deliverable(self) -> bool:
        return self.active and self.disabled_at is None

    def subscribes_to(self, event: str) -> bool:
        # An empty list means "everything" — the least surprising default for a new endpoint.
        return not self.events or event in self.events

    def sign(self, timestamp: str, body: str) -> str:
        """Stripe-style signature over ``timestamp.body``.

        Including the timestamp in the signed payload is what makes replay detection
        possible: a receiver rejects anything older than its tolerance window, and an
        attacker cannot re-sign a stale body with a fresh timestamp.
        """

        message = f"{timestamp}.{body}".encode()
        digest = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={digest}"

    def record_success(self, status_code: int):
        WebhookEndpoint.objects.filter(pk=self.pk).update(
            consecutive_failures=0,
            last_success_at=timezone.now(),
            last_status_code=status_code,
            disabled_at=None,
        )

    def record_failure(self, status_code: int | None = None):
        failures = self.consecutive_failures + 1
        disabled = timezone.now() if failures >= self.FAILURE_THRESHOLD else None
        WebhookEndpoint.objects.filter(pk=self.pk).update(
            consecutive_failures=failures,
            last_failure_at=timezone.now(),
            last_status_code=status_code,
            disabled_at=disabled or self.disabled_at,
        )
        return disabled is not None


class WebhookDelivery(TimeStampedModel):
    """One attempt to deliver one event to one endpoint. The customer-facing audit trail."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="webhook_deliveries"
    )
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=60)
    #: Stable per-event id the receiver can use to deduplicate.
    event_id = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Truncated: a hostile endpoint could otherwise return megabytes into our database.
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    attempt = models.PositiveIntegerField(default=1)
    job = models.ForeignKey(
        "queues.Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_deliveries"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["endpoint", "status"]),
        ]

    def __str__(self):
        return f"{self.event} -> {self.endpoint_id} [{self.status}]"
