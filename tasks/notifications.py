"""Notification fan-out.

One entry point (:func:`deliver_notification`) so every producer gets the same guarantees:
preferences are honoured server-side, delivery is idempotent on ``natural_key``, and email
is queued as a durable job rather than sent inline on a request thread.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from kernel.models import Notification, NotificationPreference
from ops.logging import get_logger

logger = get_logger("taskmanager.notifications")


def get_preferences(organization, user) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
    return prefs


def deliver_notification(
    *,
    organization,
    user,
    title: str,
    body: str = "",
    category: str = Notification.Category.SYSTEM,
    natural_key: str,
    link_url: str = "",
    send_email: bool | None = None,
) -> Notification | None:
    """Create an in-app notification and optionally queue an email.

    Returns the notification, or the pre-existing one when this is a redelivery. Returns
    ``None`` only when the recipient has disabled in-app delivery for this workspace.
    """

    if user is None or not getattr(user, "pk", None):
        return None

    prefs = get_preferences(organization, user)
    notification = None

    if prefs.in_app_enabled:
        try:
            with transaction.atomic():
                notification = Notification.objects.create(
                    organization=organization,
                    user=user,
                    title=title[:160],
                    body=body,
                    category=category,
                    natural_key=natural_key[:160],
                    link_url=link_url[:400],
                    status=Notification.Status.DELIVERED,
                )
        except IntegrityError:
            # The unique (org, user, natural_key) constraint is the idempotency barrier:
            # a redelivered job finds the existing row instead of duplicating the inbox.
            notification = Notification.objects.filter(
                organization=organization, user=user, natural_key=natural_key[:160]
            ).first()
            return notification

    wants_email = prefs.allows_email(category) if send_email is None else send_email
    if wants_email and getattr(user, "email", ""):
        if prefs.in_quiet_hours():
            logger.info("email suppressed by quiet hours", extra={"user_id": user.pk})
        else:
            queue_email(
                organization=organization,
                to=[user.email],
                subject=title,
                body=body or title,
                natural_key=f"email:{natural_key}"[:160],
            )

    return notification


def queue_email(*, organization, to, subject: str, body: str, natural_key: str, html: str = "") -> None:
    """Enqueue a durable email job.

    Email is never sent inline: an SMTP timeout inside a request would either block the
    user or lose the message entirely, and neither is acceptable for a reminder.
    """

    from queues.models import Job
    from queues.services import enqueue_job, get_internal_queue

    enqueue_job(
        organization=organization,
        queue=get_internal_queue(organization),
        source=f"email:{organization.pk}",
        handler_type=Job.HandlerType.INTERNAL,
        handler="internal:send_email",
        payload={
            "to": list(to),
            "subject": subject,
            "body": body,
            "html": html,
            "natural_key": natural_key,
        },
        idempotency_key=natural_key[:180],
    )


def notify_assignment(*, task, actor) -> None:
    """Tell the assignee they own a task. Never notifies someone about their own action."""

    assignee = task.assignee
    if assignee is None or (actor is not None and getattr(actor, "pk", None) == assignee.pk):
        return
    deliver_notification(
        organization=task.organization,
        user=assignee,
        title=f"Assigned: {task.title}",
        body=f"{actor} assigned this task to you." if actor else "You were assigned this task.",
        category=Notification.Category.ASSIGNMENT,
        natural_key=f"assign:{task.pk}:{assignee.pk}:{task.version}",
        link_url=f"/tasks/{task.pk}/",
    )


def notify_comment(*, comment) -> None:
    """Notify the task's assignee and creator (once each, never the comment author)."""

    task = comment.task
    recipients = {
        user.pk: user
        for user in (task.assignee, task.creator)
        if user is not None and user.pk != comment.author_id
    }
    for user in recipients.values():
        deliver_notification(
            organization=comment.organization,
            user=user,
            title=f"New comment on {task.title}",
            body=comment.body[:500],
            category=Notification.Category.COMMENT,
            natural_key=f"comment:{comment.pk}:{user.pk}",
            link_url=f"/tasks/{task.pk}/",
        )


def notify_mentions(*, organization, task, mentions, actor) -> None:
    """Notify @mentioned members. Unknown handles are ignored, not errors."""

    if not mentions:
        return
    from django.contrib.auth import get_user_model

    from kernel.models import Membership

    User = get_user_model()
    users = User.objects.filter(
        username__in=list(mentions),
        memberships__organization=organization,
        memberships__status=Membership.Status.ACTIVE,
    ).distinct()

    for user in users:
        if actor is not None and getattr(actor, "pk", None) == user.pk:
            continue
        deliver_notification(
            organization=organization,
            user=user,
            title=f"Mentioned: {task.title}",
            body=f"{actor} mentioned you." if actor else "You were mentioned.",
            category=Notification.Category.MENTION,
            natural_key=f"mention:{task.pk}:{user.pk}",
            link_url=f"/tasks/{task.pk}/",
        )
