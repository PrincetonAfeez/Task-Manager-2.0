"""Domain event bus.

A single place where "something happened" becomes activity, notifications and outbound
webhooks. Without it, each producer has to remember all three, and one of them always gets
forgotten.

Emission is intentionally best-effort and post-commit: a failure to notify must never roll
back the customer's actual change.
"""

from __future__ import annotations

from django.db import transaction

from ops.logging import get_logger

logger = get_logger("taskmanager.events")


def emit(*, organization, event: str, data: dict, defer: bool = True) -> None:
    """Publish a domain event to every subscriber.

    ``defer`` sends it on transaction commit, which is almost always what you want: an
    event announcing a task that was then rolled back is a lie the receiver cannot detect.
    """

    def _dispatch():
        try:
            from integrations.delivery import dispatch_event

            dispatch_event(organization=organization, event=event, data=data)
        except Exception:
            logger.exception("failed to dispatch webhook event", extra={"event": event})

    if defer:
        transaction.on_commit(_dispatch)
    else:
        _dispatch()


def task_payload(task) -> dict:
    """The public shape of a task in a webhook body. Changing this is a breaking change."""

    return {
        "id": task.pk,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "due_time": task.due_time.isoformat() if task.due_time else None,
        "task_list": {"id": task.task_list_id, "name": task.task_list.name}
        if task.task_list_id
        else None,
        "column": {"id": task.column_id, "name": task.column.name} if task.column_id else None,
        "assignee": task.assignee.get_username() if task.assignee_id else None,
        "labels": [label.name for label in task.labels.all()],
        "version": task.version,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def comment_payload(comment) -> dict:
    return {
        "id": comment.pk,
        "task_id": comment.task_id,
        "author": comment.author.get_username() if comment.author_id else None,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }
