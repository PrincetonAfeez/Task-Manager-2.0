"""Shared task filtering.

Used by the HTML list view, the saved-view resolver, and the export builder so a saved
filter means the same thing everywhere it is applied.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from tasks.models import Task

# NULL ordering is *not* portable: SQLite sorts NULLs first on ASC, PostgreSQL sorts them
# last. Stating it explicitly means the task list looks the same in development and in
# production instead of silently reordering on deploy.
ALLOWED_SORTS = {
    "priority": ["-priority", F("due_date").asc(nulls_last=True), "position"],
    "due": [F("due_date").asc(nulls_last=True), "-priority", "position"],
    "created": ["-created_at"],
    "updated": ["-updated_at"],
    "title": ["title"],
}
DEFAULT_SORT = "priority"


def apply_filters(queryset, filters: dict, *, today=None):
    """Narrow a task queryset by a validated filter dict.

    Only recognised keys are honoured; unknown keys are ignored rather than raising, because
    saved views persist filters across releases and a removed filter must not break them.
    """

    today = today or timezone.localdate()

    search = (filters.get("q") or "").strip()
    if search:
        # icontains over title/notes is adequate at this scale and, unlike a raw SQL LIKE,
        # is parameterised by the ORM.
        queryset = queryset.filter(Q(title__icontains=search) | Q(notes__icontains=search))

    status = filters.get("status")
    if status:
        queryset = queryset.filter(status=status)
    else:
        # Archived work is excluded unless it is explicitly asked for.
        queryset = queryset.filter(archived_at__isnull=True)

    priority = filters.get("priority")
    if priority:
        queryset = queryset.filter(priority=priority)

    assignee = filters.get("assignee")
    if assignee:
        queryset = queryset.filter(assignee=assignee)

    task_list = filters.get("task_list")
    if task_list:
        queryset = queryset.filter(task_list=task_list)

    label = filters.get("label")
    if label:
        queryset = queryset.filter(labels=label)

    due = filters.get("due")
    if due == "overdue":
        queryset = queryset.filter(due_date__lt=today, status=Task.Status.OPEN)
    elif due == "today":
        queryset = queryset.filter(due_date=today)
    elif due == "week":
        queryset = queryset.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    elif due == "none":
        queryset = queryset.filter(due_date__isnull=True)

    # No DISTINCT: the only join is to a *single* label, and the task/label link is unique,
    # so a task can match at most once. DISTINCT here made Postgres de-duplicate every wide
    # row in the workspace before it could sort or count (55 ms + 27 ms at 19k tasks).
    return queryset


def apply_sort(queryset, sort: str | None):
    """Order by an allow-listed key. Arbitrary ``order_by`` from a query string is not safe."""

    return queryset.order_by(*ALLOWED_SORTS.get(sort or DEFAULT_SORT, ALLOWED_SORTS[DEFAULT_SORT]))


def serialize_filters(cleaned: dict) -> dict:
    """Convert a cleaned form dict into JSON suitable for a SavedView."""

    payload = {}
    for key in ("q", "status", "due", "priority", "sort"):
        value = cleaned.get(key)
        if value:
            payload[key] = str(value)
    for key in ("assignee", "task_list", "label"):
        value = cleaned.get(key)
        if value is not None:
            payload[key] = getattr(value, "pk", value)
    return payload


def deserialize_filters(payload: dict, *, organization) -> dict:
    """Rehydrate a SavedView's filters into model instances, dropping deleted references."""

    from django.contrib.auth import get_user_model

    from kernel.models import Membership
    from tasks.models import Label, TaskList

    User = get_user_model()
    resolved = {key: payload.get(key, "") for key in ("q", "status", "due", "priority", "sort")}

    if payload.get("assignee"):
        resolved["assignee"] = User.objects.filter(
            pk=payload["assignee"],
            memberships__organization=organization,
            memberships__status=Membership.Status.ACTIVE,
        ).first()
    if payload.get("task_list"):
        resolved["task_list"] = TaskList.objects.filter(
            pk=payload["task_list"], organization=organization
        ).first()
    if payload.get("label"):
        resolved["label"] = Label.objects.filter(pk=payload["label"], organization=organization).first()

    return resolved
