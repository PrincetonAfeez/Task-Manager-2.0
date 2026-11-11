"""Outbound webhook delivery.

Three properties this module exists to guarantee:

* **No SSRF.** A customer supplies the URL. Without validation, "send a webhook to
  ``http://169.254.169.254/latest/meta-data/``" turns the worker into a proxy into our own
  infrastructure. Every URL is resolved and every resolved address is checked.
* **Authenticity.** Deliveries are HMAC-signed over ``timestamp.body`` so a receiver can
  verify origin and reject replays.
* **Bounded cost.** Connect and read timeouts, a response-size cap, and a circuit breaker
  that disables an endpoint failing persistently.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from ops.logging import get_logger
from queues.registry import PermanentError, RetryableError

from .models import WebhookDelivery, WebhookEndpoint

logger = get_logger("taskmanager.webhooks")

ALLOWED_SCHEMES = {"https", "http"}
MAX_REDIRECTS = 0  # A redirect is a documented SSRF bypass; refuse to follow any.

#: Status codes worth retrying. Everything else is the receiver telling us the request is
#: wrong, and retrying a 400 five times just wastes both sides' capacity.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _is_forbidden_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — cloud metadata lives here
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(url: str) -> str:
    """Reject anything that is not a public HTTP(S) endpoint.

    Resolution happens here so a hostname pointing at ``127.0.0.1`` is caught at
    configuration time rather than at delivery time.
    """

    parsed = urlparse(url or "")
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("Webhook URLs must use http or https.")
    if getattr(settings, "IS_PRODUCTION", False) and parsed.scheme != "https":
        raise ValueError("Webhook URLs must use https.")
    if not parsed.hostname:
        raise ValueError("Webhook URL has no host.")
    if parsed.username or parsed.password:
        raise ValueError("Webhook URLs must not embed credentials.")

    if getattr(settings, "WEBHOOK_ALLOW_PRIVATE_NETWORKS", False):
        # Development convenience only. Production sets this to False.
        return url

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve {parsed.hostname}.") from exc

    for info in infos:
        address = info[4][0]
        if _is_forbidden_address(address):
            raise ValueError(
                f"{parsed.hostname} resolves to a private or reserved address ({address}); "
                "webhooks may only target public hosts."
            )
    return url


def build_event_payload(*, event: str, organization, data: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "event": event,
        "created_at": timezone.now().isoformat(),
        "organization": {"id": organization.pk, "slug": organization.slug},
        "data": data,
    }


def dispatch_event(*, organization, event: str, data: dict) -> int:
    """Queue a delivery job for every subscribed endpoint. Returns how many were queued.

    Enqueueing rather than sending inline is what keeps a slow customer endpoint from
    adding seconds to the request that triggered the event.
    """

    from kernel.services import feature_enabled
    from queues.models import Job
    from queues.services import enqueue_job, get_webhook_queue

    if not feature_enabled("webhooks", organization):
        return 0

    endpoints = WebhookEndpoint.objects.filter(
        organization=organization, active=True, disabled_at__isnull=True
    )
    if not endpoints.exists():
        return 0

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = build_event_payload(event=event, organization=organization, data=data, event_id=event_id)
    queue = get_webhook_queue(organization)
    queued = 0

    for endpoint in endpoints:
        if not endpoint.subscribes_to(event):
            continue
        result = enqueue_job(
            organization=organization,
            queue=queue,
            source=f"webhook:{endpoint.pk}",
            handler_type=Job.HandlerType.HTTP_DELIVERY,
            handler="http_delivery",
            payload={"endpoint_id": endpoint.pk, "event": event, "body": payload},
            # The event id makes the enqueue idempotent: the same logical event can never
            # produce two jobs for the same endpoint.
            idempotency_key=f"webhook:{endpoint.pk}:{event_id}"[:180],
        )
        if result.created:
            queued += 1

    return queued


def deliver_webhook(job) -> dict:
    """Job handler: perform one signed HTTP delivery.

    Raises :class:`RetryableError` for transport failures and retryable status codes, and
    :class:`PermanentError` when the receiver has told us the request itself is wrong.
    """

    payload = job.payload
    endpoint_id = payload.get("endpoint_id")

    if endpoint_id is None:
        # Legacy/seed payloads that carry a bare URL rather than an endpoint reference.
        return _deliver_raw(job)

    endpoint = WebhookEndpoint.objects.filter(
        pk=endpoint_id, organization_id=job.organization_id
    ).first()
    if endpoint is None:
        return {"skipped": "endpoint_deleted", "endpoint_id": endpoint_id}
    if not endpoint.is_deliverable:
        return {"skipped": "endpoint_disabled", "endpoint_id": endpoint_id}

    body_obj = payload.get("body") or {}
    event = payload.get("event", "unknown")
    event_id = body_obj.get("id", job.idempotency_key)

    delivery = WebhookDelivery.objects.create(
        organization_id=job.organization_id,
        endpoint=endpoint,
        event=event,
        event_id=event_id,
        payload=body_obj,
        attempt=job.attempts_made or 1,
        job=job,
    )

    try:
        validate_webhook_url(endpoint.url)
    except ValueError as exc:
        delivery.status = WebhookDelivery.Status.FAILED
        delivery.error = str(exc)
        delivery.save(update_fields=["status", "error", "updated_at"])
        endpoint.record_failure()
        # The URL will not become valid by retrying it.
        raise PermanentError(f"Refusing to deliver: {exc}") from exc

    body = json.dumps(body_obj, separators=(",", ":"), default=str)
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": getattr(settings, "WEBHOOK_USER_AGENT", "TaskManager-Webhooks/1.0"),
        "X-TaskManager-Event": event,
        "X-TaskManager-Event-Id": str(event_id),
        "X-TaskManager-Delivery": str(delivery.pk),
        "X-TaskManager-Signature": endpoint.sign(timestamp, body),
    }

    started = time.perf_counter()
    try:
        status_code, response_text = _post(endpoint.url, body, headers)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        delivery.status = WebhookDelivery.Status.FAILED
        delivery.error = f"{type(exc).__name__}: {exc}"[:2000]
        delivery.duration_ms = elapsed
        delivery.save(update_fields=["status", "error", "duration_ms", "updated_at"])
        if endpoint.record_failure():
            logger.error("webhook endpoint disabled", extra={"endpoint_id": endpoint.pk})
        raise RetryableError(f"Webhook transport failure: {exc}") from exc

    elapsed = int((time.perf_counter() - started) * 1000)
    delivery.status_code = status_code
    delivery.duration_ms = elapsed
    delivery.response_body = response_text[:2000]

    if 200 <= status_code < 300:
        delivery.status = WebhookDelivery.Status.SUCCEEDED
        delivery.save(update_fields=["status", "status_code", "duration_ms", "response_body", "updated_at"])
        endpoint.record_success(status_code)
        return {"delivery_id": delivery.pk, "status_code": status_code, "duration_ms": elapsed}

    delivery.status = WebhookDelivery.Status.FAILED
    delivery.error = f"HTTP {status_code}"
    delivery.save(
        update_fields=["status", "status_code", "duration_ms", "response_body", "error", "updated_at"]
    )
    if endpoint.record_failure(status_code):
        logger.error("webhook endpoint disabled", extra={"endpoint_id": endpoint.pk})

    if status_code in RETRYABLE_STATUS:
        raise RetryableError(f"Receiver returned {status_code}")
    raise PermanentError(f"Receiver rejected the delivery with {status_code}")


def _post(url: str, body: str, headers: dict) -> tuple[int, str]:
    """POST with hard timeouts, no redirects, and a capped response read."""

    import requests

    connect_timeout = getattr(settings, "WEBHOOK_CONNECT_TIMEOUT", 5)
    read_timeout = getattr(settings, "WEBHOOK_READ_TIMEOUT", 10)
    max_bytes = getattr(settings, "WEBHOOK_MAX_RESPONSE_BYTES", 64 * 1024)

    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=(connect_timeout, read_timeout),
        # Following a redirect would let a receiver bounce us at an internal address after
        # the URL passed validation.
        allow_redirects=False,
        stream=True,
    )
    try:
        content = response.raw.read(max_bytes, decode_content=True) or b""
    finally:
        response.close()
    return response.status_code, content.decode("utf-8", errors="replace")


def _deliver_raw(job) -> dict:
    """Deliver a job that carries a URL directly rather than an endpoint reference."""

    url = job.payload.get("url")
    if not url:
        raise PermanentError("HTTP delivery job has neither an endpoint_id nor a url.")

    try:
        validate_webhook_url(url)
    except ValueError as exc:
        raise PermanentError(str(exc)) from exc

    body = json.dumps(job.payload.get("body", {}), separators=(",", ":"), default=str)
    headers = {"Content-Type": "application/json", "User-Agent": settings.WEBHOOK_USER_AGENT}
    try:
        status_code, _ = _post(url, body, headers)
    except Exception as exc:
        raise RetryableError(f"Webhook transport failure: {exc}") from exc

    if 200 <= status_code < 300:
        return {"status_code": status_code, "url": url}
    if status_code in RETRYABLE_STATUS:
        raise RetryableError(f"Receiver returned {status_code}")
    raise PermanentError(f"Receiver rejected the delivery with {status_code}")
