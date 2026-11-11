"""Hard deletion of soft-deleted workspaces.

GDPR-style erasure is a promise the product makes; a soft delete alone does not keep it.
This module is the second half — it runs after the retention window and removes the tenant's
rows for real, in an order that respects ``PROTECT`` foreign keys.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ops.logging import get_logger

from .models import Organization

logger = get_logger("taskmanager.purge")


def enqueue_due_purges(*, now=None) -> dict:
    """Queue a purge job for every workspace past its retention window."""

    from queues.models import Job
    from queues.services import enqueue_job, get_internal_queue

    now = now or timezone.now()
    due = Organization.objects.filter(deleted_at__isnull=False, purge_after__lte=now)

    queued = 0
    for organization in due:
        enqueue_job(
            organization=organization,
            queue=get_internal_queue(organization),
            source=f"purge:{organization.pk}",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:purge_organization",
            payload={"organization_id": organization.pk},
            idempotency_key=f"purge:{organization.pk}",
            priority=1,
        )
        queued += 1
    return {"queued": queued}


@transaction.atomic
def purge_organization_data(organization_id: int) -> dict:
    """Erase every row belonging to one workspace.

    Deletion order is explicit rather than relying on cascade: ``Task.task_list`` and
    ``Job.queue`` are ``PROTECT``, so a naive ``organization.delete()`` raises instead of
    completing the erasure.
    """

    from queues.models import DeadLetter, EffectMarker, Job, JobAttempt, Queue, QueueMetricSnapshot
    from schedules.models import Schedule, ScheduleOccurrence
    from tasks.models import (
        Attachment,
        Column,
        Comment,
        Label,
        ProjectArea,
        RecurrenceRule,
        Reminder,
        Subtask,
        Task,
        TaskDependency,
        TaskList,
    )

    from .models import (
        Activity,
        ApiKey,
        AuditLog,
        FeatureFlag,
        Invitation,
        Membership,
        Notification,
        NotificationPreference,
        SavedView,
    )

    organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        return {"purged": False, "reason": "organization_missing"}
    if organization.deleted_at is None:
        # Refusing here is the point: a purge job for a live workspace is a bug or an
        # attack, and either way it must not delete a paying customer's data.
        logger.error("refused to purge a live organization", extra={"organization_id": organization_id})
        return {"purged": False, "reason": "organization_not_deleted"}

    counts: dict[str, int] = {}

    def wipe(label, queryset):
        deleted, _ = queryset.delete()
        counts[label] = deleted

    # Attachments hold files as well as rows; drop the blobs before the rows disappear.
    for attachment in Attachment.objects.filter(organization=organization).iterator():
        try:
            attachment.file.delete(save=False)
        except Exception:
            logger.warning("attachment blob missing during purge", extra={"attachment_id": attachment.pk})

    wipe("attachments", Attachment.objects.filter(organization=organization))
    wipe("subtasks", Subtask.objects.filter(task__organization=organization))
    wipe("comments", Comment.objects.filter(organization=organization))
    wipe("dependencies", TaskDependency.objects.filter(organization=organization))
    wipe("reminders", Reminder.objects.filter(organization=organization))
    wipe("recurrences", RecurrenceRule.objects.filter(organization=organization))
    wipe("schedule_occurrences", ScheduleOccurrence.objects.filter(organization=organization))
    wipe("schedules", Schedule.objects.filter(organization=organization))
    wipe("dead_letters", DeadLetter.objects.filter(organization=organization))
    wipe("job_attempts", JobAttempt.objects.filter(job__organization=organization))
    wipe("queue_metrics", QueueMetricSnapshot.objects.filter(organization=organization))
    wipe("effect_markers", EffectMarker.objects.filter(organization=organization))
    wipe("jobs", Job.objects.filter(organization=organization))
    wipe("queues", Queue.objects.filter(organization=organization))
    wipe("tasks", Task.objects.filter(organization=organization))
    wipe("columns", Column.objects.filter(organization=organization))
    wipe("task_lists", TaskList.objects.filter(organization=organization))
    wipe("project_areas", ProjectArea.objects.filter(organization=organization))
    wipe("labels", Label.objects.filter(organization=organization))
    wipe("notifications", Notification.objects.filter(organization=organization))
    wipe("notification_preferences", NotificationPreference.objects.filter(organization=organization))
    wipe("saved_views", SavedView.objects.filter(organization=organization))
    wipe("activities", Activity.objects.filter(organization=organization))
    wipe("api_keys", ApiKey.objects.filter(organization=organization))
    wipe("invitations", Invitation.objects.filter(organization=organization))
    wipe("feature_flags", FeatureFlag.objects.filter(organization=organization))
    wipe("memberships", Membership.objects.filter(organization=organization))

    try:
        from integrations.models import WebhookDelivery, WebhookEndpoint

        wipe("webhook_deliveries", WebhookDelivery.objects.filter(organization=organization))
        wipe("webhook_endpoints", WebhookEndpoint.objects.filter(organization=organization))
    except ImportError:  # pragma: no cover - integrations app is optional
        pass

    # The audit trail outlives the workspace by design: it records who deleted what, and a
    # purge that erased its own evidence would be worthless. It is detached instead.
    AuditLog.objects.filter(organization=organization).update(organization=None)

    organization_name = organization.name
    Organization.objects.filter(pk=organization.pk).delete()

    # Nested under one key, not spread: `logging` reserves attribute names such as `name`,
    # `module` and `args`, and colliding with one raises KeyError *inside* the log call.
    # Here that exception would propagate out of an @transaction.atomic block and roll the
    # entire erasure back after it had already run.
    logger.warning(
        "organization purged",
        extra={
            "organization_id": organization_id,
            "organization_name": organization_name,
            "deleted_counts": counts,
        },
    )
    return {"purged": True, "organization_id": organization_id, "counts": counts}
