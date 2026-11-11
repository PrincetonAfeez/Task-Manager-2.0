"""Daily and weekly digest emails.

Each digest is one durable job with an idempotency key derived from the member and the
period being summarised, so a scheduler that ticks twice cannot send the same digest twice.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from kernel.models import Membership, NotificationPreference
from ops.logging import get_logger
from queues.models import Job
from queues.services import claim_effect, enqueue_job, get_internal_queue

from .models import Task

logger = get_logger("taskmanager.digests")

#: Local hour at which a digest is considered due.
DIGEST_HOUR = 8


def build_digest(*, organization, user, period: str = "daily") -> dict:
    """Summarise a member's workload. Pure read — safe to call for a preview."""

    today = timezone.localdate()
    horizon = today + timedelta(days=7 if period == "weekly" else 1)

    assigned = Task.objects.filter(
        organization=organization, assignee=user, status=Task.Status.OPEN, archived_at__isnull=True
    )
    overdue = list(assigned.filter(due_date__lt=today).order_by("due_date")[:20])
    due_today = list(assigned.filter(due_date=today).order_by("due_time")[:20])
    upcoming = list(assigned.filter(due_date__gt=today, due_date__lte=horizon).order_by("due_date")[:20])
    unscheduled = assigned.filter(due_date__isnull=True).count()

    completed_since = today - timedelta(days=7 if period == "weekly" else 1)
    completed = Task.objects.filter(
        organization=organization,
        completed_by=user,
        completed_at__date__gte=completed_since,
    ).count()

    return {
        "period": period,
        "organization": organization.name,
        "overdue": overdue,
        "due_today": due_today,
        "upcoming": upcoming,
        "unscheduled_count": unscheduled,
        "completed_count": completed,
        "has_content": bool(overdue or due_today or upcoming or completed),
    }


def render_digest(summary: dict) -> tuple[str, str]:
    """Render the digest to (subject, plain-text body)."""

    label = "Weekly" if summary["period"] == "weekly" else "Daily"
    subject = f"{label} digest — {summary['organization']}"

    lines = [f"{label} summary for {summary['organization']}", ""]
    if summary["overdue"]:
        lines.append(f"Overdue ({len(summary['overdue'])}):")
        lines += [f"  - {task.title} (due {task.due_date})" for task in summary["overdue"]]
        lines.append("")
    if summary["due_today"]:
        lines.append(f"Due today ({len(summary['due_today'])}):")
        lines += [f"  - {task.title}" for task in summary["due_today"]]
        lines.append("")
    if summary["upcoming"]:
        lines.append(f"Coming up ({len(summary['upcoming'])}):")
        lines += [f"  - {task.title} (due {task.due_date})" for task in summary["upcoming"]]
        lines.append("")
    if summary["unscheduled_count"]:
        lines.append(f"{summary['unscheduled_count']} assigned task(s) have no due date.")
    if summary["completed_count"]:
        lines.append(f"You completed {summary['completed_count']} task(s) in this period.")
    if not summary["has_content"]:
        lines.append("Nothing needs your attention. Enjoy the quiet.")

    return subject, "\n".join(lines)


def send_digest_for_member(membership_id: int, *, period: str = "daily", natural_key: str = "") -> dict:
    """Job handler body: render and send one member's digest, exactly once."""

    membership = (
        Membership.objects.select_related("organization", "user")
        .filter(pk=membership_id, status=Membership.Status.ACTIVE)
        .first()
    )
    if membership is None:
        return {"skipped": "membership_missing", "membership_id": membership_id}

    user = membership.user
    if not user.email:
        return {"skipped": "no_email", "membership_id": membership_id}

    prefs, _ = NotificationPreference.objects.get_or_create(
        organization=membership.organization, user=user
    )
    if prefs.unsubscribed_at is not None or prefs.digest == NotificationPreference.Digest.OFF:
        return {"skipped": "digest_disabled", "membership_id": membership_id}

    summary = build_digest(organization=membership.organization, user=user, period=period)
    if not summary["has_content"]:
        # Sending "you have nothing to do" every morning is how a digest gets filtered
        # into a folder nobody reads.
        return {"skipped": "empty_digest", "membership_id": membership_id}

    key = natural_key or f"digest:{membership_id}:{period}:{timezone.localdate().isoformat()}"
    if not claim_effect(
        organization=membership.organization,
        effect_type="digest",
        natural_key=key,
        metadata={"period": period},
    ):
        return {"skipped": "already_sent", "membership_id": membership_id}

    subject, body = render_digest(summary)

    from django.core.mail import send_mail

    send_mail(subject=subject, message=body, from_email=None, recipient_list=[user.email])
    return {"sent": True, "membership_id": membership_id, "period": period}


def enqueue_due_digests(*, now=None) -> dict:
    """Queue a digest job for every member whose local digest hour has arrived."""

    import zoneinfo

    now = now or timezone.now()
    today = timezone.localdate()
    queued = 0

    preferences = (
        NotificationPreference.objects.filter(
            unsubscribed_at__isnull=True,
            email_enabled=True,
            organization__deleted_at__isnull=True,
        )
        .exclude(digest=NotificationPreference.Digest.OFF)
        .select_related("organization", "user")
    )

    for prefs in preferences:
        try:
            zone = zoneinfo.ZoneInfo(prefs.organization.timezone or "UTC")
        except Exception:
            zone = zoneinfo.ZoneInfo("UTC")
        local_now = now.astimezone(zone)
        if local_now.hour != DIGEST_HOUR:
            continue
        if prefs.digest == NotificationPreference.Digest.WEEKLY and local_now.isoweekday() != 1:
            continue

        membership = Membership.objects.filter(
            organization=prefs.organization, user=prefs.user, status=Membership.Status.ACTIVE
        ).first()
        if membership is None:
            continue

        enqueue_job(
            organization=prefs.organization,
            queue=get_internal_queue(prefs.organization),
            source=f"digest:{membership.pk}",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_digest",
            payload={"membership_id": membership.pk, "period": prefs.digest},
            # Date-keyed, not clock-keyed: repeated ticks within the hour collapse to one job.
            idempotency_key=f"digest:{membership.pk}:{prefs.digest}:{today.isoformat()}"[:180],
        )
        queued += 1

    return {"queued": queued}
