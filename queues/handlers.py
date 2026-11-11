"""Built-in job handlers.

Each handler is idempotent. That is not a stylistic preference: the queue guarantees
at-least-once *delivery*, so exactly-once *effect* has to be established here — either by a
naturally unique row (``Notification.natural_key``) or by an explicit
:func:`queues.services.claim_effect` marker taken in the same transaction as the effect.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ops.logging import get_logger

from .registry import PermanentError, RetryableError, register
from .services import claim_effect

logger = get_logger("taskmanager.queues.handlers")


def _require(payload: dict, key: str):
    """Fetch a required payload key, treating absence as permanent (never retryable)."""

    if key not in payload:
        raise PermanentError(f"Payload is missing required key {key!r}.")
    return payload[key]


@register("internal:send_reminder", max_attempts=5, description="Deliver a task reminder.")
def send_reminder(job, context):
    """Create the in-app notification for a task reminder and mark the reminder sent."""

    from kernel.models import Notification
    from tasks.models import Reminder, Task
    from tasks.notifications import deliver_notification

    task_id = _require(job.payload, "task_id")
    reminder_id = job.payload.get("reminder_id")

    task = Task.objects.filter(pk=task_id, organization_id=job.organization_id).first()
    if task is None:
        # The task was deleted between enqueue and execution. Nothing to remind about, and
        # no amount of retrying brings it back.
        return {"skipped": "task_missing", "task_id": task_id}

    if task.status != Task.Status.OPEN:
        return {"skipped": "task_not_open", "task_id": task_id}

    reminder = None
    if reminder_id:
        reminder = Reminder.objects.filter(pk=reminder_id, organization_id=job.organization_id).first()
        if reminder is not None and reminder.status != Reminder.Status.ARMED:
            return {"skipped": "reminder_not_armed", "reminder_id": reminder_id}

    recipient = task.assignee or task.creator
    natural_key = f"reminder:{task.pk}:{job.payload.get('remind_at', job.idempotency_key)}"[:160]

    with transaction.atomic():
        notification = deliver_notification(
            organization=task.organization,
            user=recipient,
            title=f"Reminder: {task.title}",
            body=f"Due {task.due_date} {task.due_time or ''}".strip(),
            category=Notification.Category.REMINDER,
            natural_key=natural_key,
            link_url=f"/tasks/{task.pk}/",
        )
        if reminder is not None:
            Reminder.objects.filter(pk=reminder.pk, status=Reminder.Status.ARMED).update(
                status=Reminder.Status.SENT, sent_at=timezone.now()
            )

    return {"notification_id": getattr(notification, "pk", None), "task_id": task.pk}


@register("internal:send_notification", max_attempts=5, description="Deliver a generic notification.")
def send_notification(job, context):
    from kernel.models import Notification
    from tasks.notifications import deliver_notification

    payload = job.payload
    user_id = _require(payload, "user_id")
    title = _require(payload, "title")

    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return {"skipped": "user_missing", "user_id": user_id}

    notification = deliver_notification(
        organization=job.organization,
        user=user,
        title=title,
        body=payload.get("body", ""),
        category=payload.get("category", Notification.Category.SYSTEM),
        natural_key=payload.get("natural_key", job.idempotency_key)[:160],
        link_url=payload.get("link_url", ""),
    )
    return {"notification_id": getattr(notification, "pk", None)}


@register("internal:scan_attachment", max_attempts=8, description="Scan an uploaded attachment for malware.")
def scan_attachment(job, context):
    """Scan one attachment. Safe to repeat: a file with a verdict is left alone."""

    from tasks.models import Attachment
    from tasks.scanning import ScanError
    from tasks.scanning import scan_attachment as run_scan

    attachment_id = _require(job.payload, "attachment_id")
    attachment = Attachment.objects.filter(pk=attachment_id, organization_id=job.organization_id).first()
    if attachment is None:
        return {"skipped": "attachment_missing", "attachment_id": attachment_id}

    try:
        result = run_scan(attachment)
    except ScanError as exc:
        # The file stays "pending" (and therefore undownloadable) while this retries.
        raise RetryableError(str(exc)) from exc

    if result is None:
        return {"skipped": "already_resolved", "attachment_id": attachment_id}
    return {"attachment_id": attachment_id, "clean": result.clean, "signature": result.signature}


@register("internal:materialize_recurrence", max_attempts=5, description="Create the next recurring task.")
def materialize_recurrence(job, context):
    from tasks.models import RecurrenceRule
    from tasks.recurrence import materialize_next_occurrence

    rule_id = _require(job.payload, "recurrence_id")
    rule = RecurrenceRule.objects.filter(pk=rule_id, organization_id=job.organization_id).first()
    if rule is None:
        return {"skipped": "rule_missing", "recurrence_id": rule_id}

    created = materialize_next_occurrence(rule)
    return {"created_task_id": getattr(created, "pk", None), "recurrence_id": rule_id}


@register("internal:schedule_fire", max_attempts=5, description="Run a schedule occurrence.")
def schedule_fire(job, context):
    """Terminal handler for schedules whose target is internal work.

    The occurrence row already provides fire identity, so this only needs to record that
    the tick was observed.
    """

    from schedules.models import ScheduleOccurrence

    occurrence_id = job.payload.get("occurrence_id")
    if occurrence_id:
        ScheduleOccurrence.objects.filter(pk=occurrence_id, organization_id=job.organization_id).update(
            status=ScheduleOccurrence.Status.ENQUEUED
        )
    return {"occurrence_id": occurrence_id, "schedule_id": job.payload.get("schedule_id")}


@register("internal:send_email", max_attempts=5, description="Send a transactional email.")
def send_email(job, context):
    """Send one email, exactly once, honouring the recipient's preferences."""

    from django.core.mail import EmailMultiAlternatives, get_connection

    payload = job.payload
    to = payload.get("to") or []
    if isinstance(to, str):
        to = [to]
    to = [address for address in to if address]
    if not to:
        raise PermanentError("Email job has no recipients.")

    subject = _require(payload, "subject")
    body = payload.get("body", "")
    natural_key = payload.get("natural_key") or job.idempotency_key

    if not claim_effect(
        organization=job.organization,
        effect_type="email",
        natural_key=natural_key,
        metadata={"to": to, "subject": subject},
    ):
        return {"skipped": "already_sent", "natural_key": natural_key}

    try:
        connection = get_connection(fail_silently=False)
        message = EmailMultiAlternatives(subject=subject, body=body, to=to, connection=connection)
        if payload.get("html"):
            message.attach_alternative(payload["html"], "text/html")
        message.send()
    except Exception as exc:
        # The marker was committed in its own transaction, so a failed send must release it
        # or the retry would silently skip. Delete it and re-raise as retryable.
        from .models import EffectMarker

        EffectMarker.objects.filter(
            organization_id=job.organization_id, effect_type="email", natural_key=natural_key[:180]
        ).delete()
        raise RetryableError(f"SMTP delivery failed: {exc}") from exc

    return {"sent_to": to, "subject": subject}


@register("http_delivery", max_attempts=6, description="Deliver a signed outbound webhook.")
def http_delivery(job, context):
    from integrations.delivery import deliver_webhook

    return deliver_webhook(job)


@register("internal:purge_organization", max_attempts=3, description="Erase a deleted workspace.")
def purge_organization(job, context):
    from kernel.purge import purge_organization_data

    organization_id = _require(job.payload, "organization_id")
    return purge_organization_data(organization_id)


@register("internal:send_digest", max_attempts=3, description="Send a daily/weekly digest email.")
def send_digest(job, context):
    from tasks.digests import send_digest_for_member

    membership_id = _require(job.payload, "membership_id")
    period = job.payload.get("period", "daily")
    return send_digest_for_member(membership_id, period=period, natural_key=job.idempotency_key)
