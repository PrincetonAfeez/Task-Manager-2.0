"""Plan entitlement and quota enforcement.

Quotas are checked at the *service* boundary, not in views, so every path that can create a
resource — web form, REST API, recurrence engine, seed command — is covered by one rule.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ops.logging import get_logger

from .models import Plan, QuotaExceeded, Subscription, UsageRecord

logger = get_logger("taskmanager.billing")

#: Shipped plans. Seeded idempotently so a fresh install has something to attach to.
DEFAULT_PLANS = [
    {
        "code": "free",
        "name": "Free",
        "description": "For individuals and small teams getting started.",
        "monthly_price_cents": 0,
        "max_members": 3,
        "max_tasks": 500,
        "max_task_lists": 3,
        "max_api_keys": 1,
        "max_webhooks": 1,
        "max_schedules": 2,
        "max_attachment_bytes": 100 * 1024 * 1024,
        "max_jobs_per_day": 500,
        "features": ["board_view", "recurring_tasks", "exports"],
        "position": 1,
    },
    {
        "code": "team",
        "name": "Team",
        "description": "For teams that need automation and integrations.",
        "monthly_price_cents": 1200,
        "max_members": 25,
        "max_tasks": 25_000,
        "max_task_lists": 50,
        "max_api_keys": 10,
        "max_webhooks": 10,
        "max_schedules": 50,
        "max_attachment_bytes": 10 * 1024 * 1024 * 1024,
        "max_jobs_per_day": 25_000,
        "features": [
            "board_view",
            "recurring_tasks",
            "exports",
            "webhooks",
            "attachments",
            "api_keys",
            "analytics",
            "digest_email",
        ],
        "position": 2,
    },
    {
        "code": "business",
        "name": "Business",
        "description": "Unlimited scale with full audit and retention controls.",
        "monthly_price_cents": 4900,
        "max_members": None,
        "max_tasks": None,
        "max_task_lists": None,
        "max_api_keys": 100,
        "max_webhooks": 100,
        "max_schedules": None,
        "max_attachment_bytes": None,
        "max_jobs_per_day": None,
        "features": [
            "board_view",
            "recurring_tasks",
            "exports",
            "webhooks",
            "attachments",
            "api_keys",
            "analytics",
            "digest_email",
            "sso",
        ],
        "position": 3,
    },
]


def ensure_default_plans() -> list[Plan]:
    plans = []
    for spec in DEFAULT_PLANS:
        plan, _ = Plan.objects.update_or_create(code=spec["code"], defaults=spec)
        plans.append(plan)
    return plans


def get_default_plan() -> Plan:
    from django.conf import settings

    code = getattr(settings, "DEFAULT_PLAN", "free")
    plan = Plan.objects.filter(code=code).first()
    if plan is None:
        ensure_default_plans()
        plan = Plan.objects.filter(code=code).first() or Plan.objects.order_by("position").first()
    return plan


def get_subscription(organization) -> Subscription:
    """Every workspace has a subscription; one is created on first access if missing.

    Lazily creating it means a workspace made before billing existed still gets entitlements
    rather than raising on every quota check.
    """

    subscription = Subscription.objects.filter(organization=organization).select_related("plan").first()
    if subscription is not None:
        return subscription

    from django.conf import settings

    plan = get_default_plan()
    subscription = Subscription.objects.create(
        organization=organization,
        plan=plan,
        status=Subscription.Status.TRIALING if plan.monthly_price_cents else Subscription.Status.ACTIVE,
        trial_ends_at=(
            timezone.now() + timezone.timedelta(days=getattr(settings, "BILLING_TRIAL_DAYS", 14))
            if plan.monthly_price_cents
            else None
        ),
    )
    return subscription


#: How each quota is measured. Keeping the counting logic here means a new caller cannot
#: accidentally count a different thing than the enforcement does.
_COUNTERS = {
    "members": lambda org: org.memberships.filter(status="active").count(),
    "tasks": lambda org: org.tasks.filter(archived_at__isnull=True).count(),
    "task_lists": lambda org: org.task_lists.filter(archived_at__isnull=True).count(),
    "api_keys": lambda org: org.api_keys.filter(revoked_at__isnull=True).count(),
    "schedules": lambda org: org.schedules.count(),
}


def _count(resource: str, organization) -> int:
    counter = _COUNTERS.get(resource)
    if counter is not None:
        return counter(organization)
    if resource == "webhooks":
        from integrations.models import WebhookEndpoint

        return WebhookEndpoint.objects.filter(organization=organization, active=True).count()
    if resource == "attachment_bytes":
        from django.db.models import Sum

        from tasks.models import Attachment

        return (
            Attachment.objects.filter(organization=organization).aggregate(total=Sum("size_bytes"))[
                "total"
            ]
            or 0
        )
    raise ValueError(f"No counter defined for resource {resource!r}.")


def current_usage(organization) -> dict:
    """Every measurable resource alongside its limit — the billing page's data source."""

    subscription = get_subscription(organization)
    plan = subscription.plan
    usage = {}
    for resource in ("members", "tasks", "task_lists", "api_keys", "webhooks", "schedules"):
        limit = plan.limit_for(resource)
        used = _count(resource, organization)
        usage[resource] = {
            "used": used,
            "limit": limit,
            "unlimited": limit is None,
            "percent": None if limit in (None, 0) else round(100 * used / limit, 1),
            "exceeded": limit is not None and used >= limit,
        }

    attachment_limit = plan.max_attachment_bytes
    attachment_used = _count("attachment_bytes", organization)
    usage["attachment_bytes"] = {
        "used": attachment_used,
        "limit": attachment_limit,
        "unlimited": attachment_limit is None,
        "percent": None
        if attachment_limit in (None, 0)
        else round(100 * attachment_used / attachment_limit, 1),
        "exceeded": attachment_limit is not None and attachment_used >= attachment_limit,
    }

    jobs_today = UsageRecord.objects.filter(
        organization=organization, metric="jobs", day=timezone.localdate()
    ).first()
    usage["jobs_today"] = {
        "used": jobs_today.value if jobs_today else 0,
        "limit": plan.max_jobs_per_day,
        "unlimited": plan.max_jobs_per_day is None,
    }

    return {
        "plan": plan,
        "subscription": subscription,
        "usage": usage,
        "trial_days_remaining": subscription.trial_days_remaining,
    }


def check_quota(organization, resource: str, *, additional: int = 1) -> None:
    """Raise :class:`QuotaExceeded` if adding ``additional`` would exceed the plan limit.

    Called *before* the write. Checking afterwards would mean rolling back, which is both
    slower and prone to leaving partial state behind.
    """

    subscription = get_subscription(organization)
    limit = subscription.plan.limit_for(resource)
    if limit is None:
        return

    used = _count(resource, organization)
    if used + additional > limit:
        logger.info(
            "quota exceeded",
            extra={"organization_id": organization.pk, "resource": resource, "limit": limit},
        )
        raise QuotaExceeded(resource, limit, used, subscription.plan.name)


def has_feature(organization, feature: str) -> bool:
    """Plan entitlement plus the global/per-org feature flag.

    Both must agree: the plan says what the customer bought, the flag says whether the
    feature is safe to serve right now.
    """

    from kernel.services import feature_enabled

    subscription = get_subscription(organization)
    if not subscription.is_entitled:
        return False
    if subscription.plan.features and feature not in subscription.plan.features:
        return False
    return feature_enabled(feature, organization)


@transaction.atomic
def record_usage(organization, metric: str, *, amount: int = 1, day=None) -> UsageRecord:
    """Increment a daily counter atomically."""

    day = day or timezone.localdate()
    record, created = UsageRecord.objects.get_or_create(
        organization=organization, metric=metric, day=day, defaults={"value": amount}
    )
    if not created:
        UsageRecord.objects.filter(pk=record.pk).update(value=F("value") + amount)
        record.refresh_from_db()
    return record


@transaction.atomic
def change_plan(*, organization, plan: Plan, actor=None, request=None) -> Subscription:
    """Move a workspace to a different plan, refusing a downgrade it does not fit.

    Silently accepting a downgrade would leave the workspace permanently over quota with no
    way to create anything, which is a worse experience than a clear refusal.
    """

    subscription = get_subscription(organization)
    previous = subscription.plan

    over = []
    for resource in ("members", "tasks", "task_lists", "api_keys", "webhooks", "schedules"):
        limit = plan.limit_for(resource)
        if limit is not None and _count(resource, organization) > limit:
            over.append(f"{resource.replace('_', ' ')} ({_count(resource, organization)} > {limit})")
    if over:
        raise QuotaExceeded(
            resource=", ".join(over),
            limit=0,
            current=0,
            plan_name=plan.name,
        )

    subscription.plan = plan
    subscription.status = Subscription.Status.ACTIVE
    subscription.canceled_at = None
    subscription.save(update_fields=["plan", "status", "canceled_at", "updated_at"])

    from kernel.services import record_audit

    record_audit(
        action="billing.plan_changed",
        organization=organization,
        actor=actor,
        request=request,
        target_type="subscription",
        target_id=subscription.pk,
        metadata={"from": previous.code, "to": plan.code},
    )
    return subscription
