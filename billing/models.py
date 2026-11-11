from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from kernel.models import Organization, TimeStampedModel


class Plan(TimeStampedModel):
    """A named set of limits and features.

    Plans are rows rather than constants so a limit can be changed, or a bespoke plan
    granted to one customer, without a deploy.
    """

    code = models.SlugField(unique=True, max_length=40)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    monthly_price_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default="USD")

    #: ``None`` means unlimited. 0 would mean "none allowed", which is a different thing.
    max_members = models.PositiveIntegerField(null=True, blank=True)
    max_tasks = models.PositiveIntegerField(null=True, blank=True)
    max_task_lists = models.PositiveIntegerField(null=True, blank=True)
    max_api_keys = models.PositiveIntegerField(null=True, blank=True)
    max_webhooks = models.PositiveIntegerField(null=True, blank=True)
    max_schedules = models.PositiveIntegerField(null=True, blank=True)
    max_attachment_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    max_jobs_per_day = models.PositiveIntegerField(null=True, blank=True)

    #: Feature-flag keys this plan unlocks, on top of the global defaults.
    features = models.JSONField(default=list, blank=True)
    is_public = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "monthly_price_cents"]

    def __str__(self):
        return self.name

    @property
    def price_display(self) -> str:
        if self.monthly_price_cents == 0:
            return "Free"
        return f"{self.currency} {self.monthly_price_cents / 100:,.2f}/mo"

    def limit_for(self, resource: str):
        return getattr(self, f"max_{resource}", None)


class Subscription(TimeStampedModel):
    """A workspace's current plan and lifecycle state.

    This row is the *entitlement*: what the product enforces. When a payment processor is
    configured (see :mod:`billing.stripe_gateway`) it is kept in step with the processor's
    subscription by webhooks; without one it is changed directly.
    """

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIALING)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    #: The processor's subscription id (Stripe ``sub_...``); empty when not paying.
    external_reference = models.CharField(max_length=120, blank=True)
    #: Which processor owns this subscription ("stripe"), or empty.
    processor = models.CharField(max_length=20, blank=True)
    #: The processor's customer id (Stripe ``cus_...``). No card data is ever stored here.
    processor_customer_id = models.CharField(max_length=120, blank=True, db_index=True)
    #: The customer has cancelled; the plan stays until ``current_period_end``.
    cancel_at_period_end = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organization} on {self.plan}"

    @property
    def is_entitled(self) -> bool:
        """Whether the workspace may use paid capabilities right now.

        ``past_due`` stays entitled deliberately: cutting a customer off the moment a card
        fails loses more revenue than it protects, and the grace period is what dunning is for.
        """

        if self.status == self.Status.CANCELED:
            return False
        if self.status == self.Status.TRIALING and self.trial_ends_at:
            return self.trial_ends_at > timezone.now()
        return True

    @property
    def trial_days_remaining(self) -> int | None:
        if self.status != self.Status.TRIALING or not self.trial_ends_at:
            return None
        return max(0, (self.trial_ends_at - timezone.now()).days)

    def start_trial(self, days: int | None = None):
        days = days if days is not None else getattr(settings, "BILLING_TRIAL_DAYS", 14)
        self.status = self.Status.TRIALING
        self.trial_ends_at = timezone.now() + timedelta(days=days)
        self.save(update_fields=["status", "trial_ends_at", "updated_at"])
        return self


class BillingEvent(TimeStampedModel):
    """A processor webhook that has been applied. Unique on the event id, so each is applied once."""

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=120)
    organization = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True, related_name="billing_events"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} {self.event_id}"


class UsageRecord(TimeStampedModel):
    """A daily counter per workspace per metric.

    Rate-style limits (jobs per day) need a time series, not a live COUNT: the underlying
    rows are pruned, and "how many did you use yesterday" must survive that.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="usage_records"
    )
    metric = models.CharField(max_length=60)
    day = models.DateField()
    value = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "metric", "day"], name="uniq_usage_per_metric_day"
            ),
        ]
        indexes = [models.Index(fields=["organization", "metric", "-day"])]

    def __str__(self):
        return f"{self.metric}={self.value} on {self.day}"


class QuotaExceeded(Exception):
    """Raised when an action would take a workspace past its plan limit."""

    def __init__(self, resource: str, limit: int, current: int, plan_name: str = ""):
        self.resource = resource
        self.limit = limit
        self.current = current
        self.plan_name = plan_name
        super().__init__(
            f"Your {plan_name or 'current'} plan allows {limit} {resource.replace('_', ' ')}; "
            f"you have {current}. Upgrade to add more."
        )
