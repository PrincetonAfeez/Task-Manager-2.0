"""Stripe integration: hosted checkout, the customer portal, and webhook reconciliation.

Design choices worth knowing before changing this file:

* **No card data touches this application.** Payment happens on Stripe-hosted pages; the
  product only ever holds customer and subscription ids.
* **Webhooks are a trigger, not a source of truth.** Stripe does not guarantee delivery
  order, so a handler never trusts the subscription object inside an event. It re-reads the
  subscription from the API and applies that. A late "updated" arriving after "deleted" then
  converges on the right state instead of resurrecting a cancelled plan.
* **Every event is applied at most once** (``BillingEvent`` is unique on the event id), and
  the record is written in the same transaction as its effect, so a crash mid-handler means
  Stripe's retry does the work again rather than it being silently skipped.
* The REST API is called directly with ``requests``; the surface used is five endpoints and
  does not justify an SDK dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ops.logging import get_logger

from .models import BillingEvent, Plan, Subscription
from .services import get_default_plan, get_subscription

logger = get_logger("taskmanager.billing.stripe")

API_BASE = "https://api.stripe.com"
PROCESSOR = "stripe"
SIGNATURE_TOLERANCE_SECONDS = 300
_TIMEOUT = (5, 15)

#: Stripe subscription status -> ours. ``incomplete`` is absent on purpose: the first
#: payment has not succeeded yet, so nothing is granted and nothing is taken away.
_STATUS_MAP = {
    "trialing": Subscription.Status.TRIALING,
    "active": Subscription.Status.ACTIVE,
    "past_due": Subscription.Status.PAST_DUE,
    "unpaid": Subscription.Status.PAST_DUE,
}
_ENDED_STATUSES = {"canceled", "incomplete_expired", "paused"}


class BillingError(Exception):
    """A payment-processor failure with a message that is safe to show the user."""


class SignatureError(BillingError):
    """The webhook did not come from Stripe (or is too old to trust)."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def price_map() -> dict[str, str]:
    """``{plan_code: stripe_price_id}`` from ``STRIPE_PRICE_IDS``."""

    return dict(getattr(settings, "STRIPE_PRICE_IDS", {}) or {})


def is_configured() -> bool:
    return bool(getattr(settings, "STRIPE_SECRET_KEY", "") and price_map())


def plan_for_price(price_id: str) -> Plan | None:
    for code, configured in price_map().items():
        if configured == price_id:
            return Plan.objects.filter(code=code).first()
    return None


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


def _flatten(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: ``{"a": {"b": 1}, "c": [{"d": 2}]}`` -> ``a[b]=1&c[0][d]=2``."""

    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            pairs.extend(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(_flatten(item, f"{name}[{index}]"))
                else:
                    pairs.append((f"{name}[{index}]", str(item)))
        elif value is not None:
            pairs.append((name, "true" if value is True else "false" if value is False else str(value)))
    return pairs


def _request(
    method: str,
    path: str,
    data: dict | None = None,
    *,
    idempotency_key: str | None = None,
    missing_ok: bool = False,
) -> dict | None:
    headers = {"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}"}
    if idempotency_key:
        # Makes a retried POST safe: Stripe returns the first result instead of acting twice.
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = requests.request(
            method,
            API_BASE + path,
            data=_flatten(data) if data else None,
            headers=headers,
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.error("stripe unreachable", extra={"path": path, "error": type(exc).__name__})
        raise BillingError("The payment provider could not be reached. Try again shortly.") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 404 and missing_ok:
        return None
    if response.status_code >= 400:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        # Stripe's error ``code``/``type`` are safe to log; the message can echo input.
        logger.error(
            "stripe request failed",
            extra={
                "path": path,
                "status": response.status_code,
                "stripe_code": error.get("code"),
                "stripe_type": error.get("type"),
                "request_id": response.headers.get("Request-Id"),
            },
        )
        raise BillingError("The payment provider rejected the request.")
    return body


# --------------------------------------------------------------------------------------
# Checkout and portal
# --------------------------------------------------------------------------------------


def _ensure_customer(organization, subscription: Subscription, actor) -> str:
    if subscription.processor_customer_id:
        return subscription.processor_customer_id

    customer = _request(
        "POST",
        "/v1/customers",
        {
            "name": organization.name,
            "email": getattr(actor, "email", "") or None,
            "metadata": {"organization_id": organization.pk, "organization_slug": organization.slug},
        },
        idempotency_key=f"customer-org-{organization.pk}",
    )
    subscription.processor = PROCESSOR
    subscription.processor_customer_id = customer["id"]
    subscription.save(update_fields=["processor", "processor_customer_id", "updated_at"])
    return customer["id"]


def create_checkout_session(*, organization, plan: Plan, actor) -> str:
    """Return the Stripe-hosted URL where ``organization`` can subscribe to ``plan``."""

    if not is_configured():
        raise BillingError("Online payment is not configured.")
    price_id = price_map().get(plan.code)
    if not price_id:
        raise BillingError(f"The {plan.name} plan cannot be purchased online.")

    subscription = get_subscription(organization)
    if has_processor_subscription(subscription):
        raise BillingError("This workspace already has a paid subscription. Manage it from the billing portal.")

    customer_id = _ensure_customer(organization, subscription, actor)
    billing_url = settings.SITE_URL + reverse("billing_overview")
    session = _request(
        "POST",
        "/v1/checkout/sessions",
        {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": organization.pk,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": billing_url + "?checkout=success",
            "cancel_url": billing_url + "?checkout=cancelled",
            # Copied onto the subscription, so every later webhook can be tied to the
            # workspace without trusting anything the browser sent.
            "subscription_data": {
                "metadata": {"organization_id": organization.pk, "plan_code": plan.code}
            },
        },
    )
    url = session.get("url")
    if not url:
        raise BillingError("The payment provider did not return a checkout page.")
    return url


def create_portal_session(*, organization) -> str:
    """Return the Stripe-hosted URL for changing card, plan, or cancelling."""

    subscription = get_subscription(organization)
    if not subscription.processor_customer_id:
        raise BillingError("This workspace has no billing account yet.")
    session = _request(
        "POST",
        "/v1/billing_portal/sessions",
        {
            "customer": subscription.processor_customer_id,
            "return_url": settings.SITE_URL + reverse("billing_overview"),
        },
    )
    url = session.get("url")
    if not url:
        raise BillingError("The payment provider did not return a portal page.")
    return url


def has_processor_subscription(subscription: Subscription) -> bool:
    return subscription.processor == PROCESSOR and bool(subscription.external_reference)


def cancel_subscription_for(organization) -> bool:
    """Stop billing a workspace immediately. Returns whether there was anything to cancel.

    Raises :class:`BillingError` when the processor cannot be told. Callers that are about
    to make the workspace unreachable must treat that as a reason to stop: the alternative
    is a customer paying for something they have deleted.
    """

    subscription = Subscription.objects.filter(organization=organization).first()
    if subscription is None or not has_processor_subscription(subscription):
        return False
    if not getattr(settings, "STRIPE_SECRET_KEY", ""):
        raise BillingError("This workspace has a paid subscription but payments are not configured.")

    stripe_id = subscription.external_reference
    remote = _request("DELETE", f"/v1/subscriptions/{stripe_id}", missing_ok=True)
    if remote is None:
        # Already gone at the processor; make the local record agree.
        remote = {"id": stripe_id, "status": "canceled", "customer": subscription.processor_customer_id}
    apply_subscription_state(remote)
    return True


# --------------------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------------------


def verify_webhook(payload: bytes, signature_header: str, *, now: float | None = None) -> dict:
    """Authenticate a webhook body and return the parsed event.

    Stripe signs ``"{timestamp}.{body}"`` with the endpoint secret. The timestamp is part of
    the signed material and is checked against the clock, so a captured request cannot be
    replayed later.
    """

    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise SignatureError("Webhook secret is not configured.")

    timestamp = None
    candidates: list[str] = []
    for part in (signature_header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if not timestamp or not timestamp.isdigit() or not candidates:
        raise SignatureError("Malformed signature header.")

    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SignatureError("Signature mismatch.")
    if abs((time.time() if now is None else now) - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError("Signature timestamp is outside the tolerance window.")

    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise SignatureError("Body is not JSON.") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise SignatureError("Body is not an event.")
    return event


def _organization_for(stripe_subscription: dict):
    from kernel.models import Organization

    customer_id = stripe_subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")

    local = (
        Subscription.objects.select_related("organization")
        .filter(processor=PROCESSOR, processor_customer_id=customer_id)
        .first()
        if customer_id
        else None
    )
    if local is not None:
        return local.organization

    # First event for a new customer can race the row written at checkout; fall back to
    # the metadata this application put on the subscription itself.
    org_id = str((stripe_subscription.get("metadata") or {}).get("organization_id", ""))
    if org_id.isdigit():
        return Organization.objects.filter(pk=int(org_id)).first()
    return None


def _period_end(stripe_subscription: dict):
    from datetime import UTC, datetime

    value = stripe_subscription.get("current_period_end")
    if value is None:
        # Newer API versions report the period on the subscription item instead.
        items = (stripe_subscription.get("items") or {}).get("data") or []
        value = items[0].get("current_period_end") if items else None
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None


@transaction.atomic
def apply_subscription_state(stripe_subscription: dict) -> Subscription | None:
    """Make the local subscription match one authoritative Stripe subscription object."""

    from kernel.services import record_audit

    organization = _organization_for(stripe_subscription)
    if organization is None:
        logger.error("stripe subscription has no workspace", extra={"subscription": stripe_subscription.get("id")})
        return None

    subscription = Subscription.objects.select_for_update().filter(organization=organization).first()
    if subscription is None:
        subscription = get_subscription(organization)
    previous = (subscription.plan.code, subscription.status)

    stripe_id = stripe_subscription.get("id", "")
    status = stripe_subscription.get("status", "")
    customer_id = stripe_subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")

    # A workspace has one live subscription. An event about some *other*, already-ended
    # subscription (an abandoned earlier checkout, say) must not cancel the live one.
    if subscription.external_reference and subscription.external_reference != stripe_id and status in _ENDED_STATUSES:
        return subscription

    subscription.processor = PROCESSOR
    if customer_id:
        subscription.processor_customer_id = customer_id

    if status in _ENDED_STATUSES:
        subscription.plan = get_default_plan()
        subscription.status = Subscription.Status.ACTIVE
        subscription.canceled_at = timezone.now()
        subscription.external_reference = ""
        subscription.cancel_at_period_end = False
        subscription.current_period_end = None
        subscription.trial_ends_at = None
    elif status in _STATUS_MAP:
        items = (stripe_subscription.get("items") or {}).get("data") or []
        price_id = ((items[0].get("price") or {}).get("id") if items else None) or ""
        plan = plan_for_price(price_id)
        if plan is None:
            # Never guess an entitlement. Keep what the workspace has and make noise.
            logger.error("stripe price is not mapped to a plan", extra={"price": price_id, "subscription": stripe_id})
        else:
            subscription.plan = plan
        subscription.status = _STATUS_MAP[status]
        subscription.external_reference = stripe_id
        subscription.cancel_at_period_end = bool(stripe_subscription.get("cancel_at_period_end"))
        subscription.current_period_end = _period_end(stripe_subscription)
        subscription.canceled_at = None
        trial_end = stripe_subscription.get("trial_end")
        if status == "trialing" and trial_end:
            from datetime import UTC, datetime

            subscription.trial_ends_at = datetime.fromtimestamp(int(trial_end), tz=UTC)
        else:
            subscription.trial_ends_at = None
    else:
        # "incomplete": first payment still pending. Remember the customer, change nothing.
        subscription.save(update_fields=["processor", "processor_customer_id", "updated_at"])
        return subscription

    subscription.save()
    current = (subscription.plan.code, subscription.status)
    if current != previous:
        record_audit(
            action="billing.subscription_synced",
            organization=organization,
            target_type="subscription",
            target_id=subscription.pk,
            metadata={
                "from_plan": previous[0],
                "to_plan": current[0],
                "from_status": previous[1],
                "to_status": current[1],
                "stripe_status": status,
            },
        )
    return subscription


def sync_subscription(stripe_subscription_id: str) -> Subscription | None:
    """Re-read a subscription from Stripe and apply it."""

    remote = _request("GET", f"/v1/subscriptions/{stripe_subscription_id}")
    return apply_subscription_state(remote)


def _subscription_id_from(event: dict) -> str | None:
    obj = (event.get("data") or {}).get("object") or {}
    kind = event.get("type", "")
    if kind.startswith("customer.subscription."):
        return obj.get("id")
    if kind == "checkout.session.completed":
        return obj.get("subscription") if obj.get("mode", "subscription") == "subscription" else None
    if kind.startswith("invoice."):
        direct = obj.get("subscription")
        if direct:
            return direct if isinstance(direct, str) else direct.get("id")
        details = (obj.get("parent") or {}).get("subscription_details") or {}
        return details.get("subscription")
    return None


HANDLED_EVENT_TYPES = (
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
)


def handle_event(event: dict) -> str:
    """Apply one verified event exactly once. Returns ``processed``/``duplicate``/``ignored``."""

    event_id, kind = event["id"], event["type"]
    if kind not in HANDLED_EVENT_TYPES:
        return "ignored"
    if BillingEvent.objects.filter(event_id=event_id).exists():
        return "duplicate"

    subscription_id = _subscription_id_from(event)
    # Fetched before the transaction opens: no database lock is held across the network.
    remote = _request("GET", f"/v1/subscriptions/{subscription_id}") if subscription_id else None

    try:
        with transaction.atomic():
            record = BillingEvent.objects.create(event_id=event_id, event_type=kind)
            local = apply_subscription_state(remote) if remote else None
            if local is not None:
                record.organization = local.organization
                record.save(update_fields=["organization"])
    except IntegrityError:
        # Two deliveries of the same event raced; the other one is doing the work.
        return "duplicate"

    logger.info("stripe event processed", extra={"event_id": event_id, "event_type": kind})
    return "processed"
