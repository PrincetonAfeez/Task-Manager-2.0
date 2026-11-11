"""Workspace data export.

Two properties matter here:

* **Streaming.** A workspace with 100k tasks must not be materialised in memory before the
  first byte is sent, or a large export is a reliable way to OOM the web tier.
* **CSV injection safety.** A task titled ``=cmd|'/c calc'!A0`` is a formula the moment a
  spreadsheet opens the file. Every cell is neutralised.
"""

from __future__ import annotations

import csv
import io
import json

from django.http import StreamingHttpResponse
from django.utils import timezone

from ops.logging import get_logger

logger = get_logger("taskmanager.exports")

#: Leading characters a spreadsheet treats as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value) -> str:
    """Neutralise spreadsheet formula injection while keeping the value readable."""

    if value is None:
        return ""
    text = str(value)
    if text and text[0] in _FORMULA_PREFIXES:
        # A leading apostrophe is the conventional escape: Excel and Sheets both render the
        # original text and refuse to evaluate it.
        return "'" + text
    return text


class _Echo:
    """A file-like object whose ``write`` returns the value, for ``csv.writer`` streaming."""

    def write(self, value):
        return value


TASK_COLUMNS = [
    "id",
    "title",
    "notes",
    "status",
    "priority",
    "due_date",
    "due_time",
    "task_list",
    "column",
    "assignee",
    "creator",
    "labels",
    "estimate_minutes",
    "completed_at",
    "archived_at",
    "created_at",
    "updated_at",
]


def _task_rows(organization, *, include_archived: bool):
    from tasks.models import Task

    queryset = (
        Task.objects.filter(organization=organization)
        .select_related("task_list", "column", "assignee", "creator")
        .prefetch_related("labels")
        .order_by("id")
    )
    if not include_archived:
        queryset = queryset.filter(archived_at__isnull=True)

    # iterator() with a chunk size keeps memory flat regardless of workspace size.
    for task in queryset.iterator(chunk_size=500):
        yield {
            "id": task.pk,
            "title": task.title,
            "notes": task.notes,
            "status": task.status,
            "priority": task.priority,
            "due_date": task.due_date or "",
            "due_time": task.due_time or "",
            "task_list": task.task_list.name if task.task_list_id else "",
            "column": task.column.name if task.column_id else "",
            "assignee": task.assignee.get_username() if task.assignee_id else "",
            "creator": task.creator.get_username() if task.creator_id else "",
            "labels": ", ".join(label.name for label in task.labels.all()),
            "estimate_minutes": task.estimate_minutes or "",
            "completed_at": task.completed_at.isoformat() if task.completed_at else "",
            "archived_at": task.archived_at.isoformat() if task.archived_at else "",
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }


def stream_tasks_csv(organization, *, include_archived: bool = False) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())

    def rows():
        yield writer.writerow(TASK_COLUMNS)
        for row in _task_rows(organization, include_archived=include_archived):
            yield writer.writerow([sanitize_cell(row[column]) for column in TASK_COLUMNS])

    filename = f"tasks-{organization.slug}-{timezone.localdate().isoformat()}.csv"
    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def stream_tasks_json(organization, *, include_archived: bool = False) -> StreamingHttpResponse:
    def chunks():
        header = {
            "organization": {"id": organization.pk, "slug": organization.slug, "name": organization.name},
            "exported_at": timezone.now().isoformat(),
            "format_version": 1,
        }
        yield '{"meta":' + json.dumps(header) + ',"tasks":['
        first = True
        for row in _task_rows(organization, include_archived=include_archived):
            yield ("" if first else ",") + json.dumps(row, default=str)
            first = False
        yield "]}"

    filename = f"tasks-{organization.slug}-{timezone.localdate().isoformat()}.json"
    response = StreamingHttpResponse(chunks(), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def build_workspace_archive(organization) -> dict:
    """A complete, in-memory snapshot of a workspace.

    Bounded by design and used for the "download everything" flow, where the customer is
    entitled to a single coherent document rather than a stream.
    """

    from kernel.models import Activity, Membership
    from tasks.models import Comment, Label, ProjectArea, TaskList

    return {
        "meta": {
            "organization": {
                "id": organization.pk,
                "name": organization.name,
                "slug": organization.slug,
                "timezone": organization.timezone,
                "created_at": organization.created_at.isoformat(),
            },
            "exported_at": timezone.now().isoformat(),
            "format_version": 1,
        },
        "members": [
            {
                "username": membership.user.get_username(),
                "email": membership.user.email,
                "role": membership.role,
                "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
            }
            for membership in Membership.objects.filter(organization=organization).select_related("user")
        ],
        "project_areas": list(
            ProjectArea.objects.filter(organization=organization).values("id", "name", "color", "position")
        ),
        "task_lists": list(
            TaskList.objects.filter(organization=organization).values(
                "id", "name", "color", "position", "project_area_id"
            )
        ),
        "labels": list(Label.objects.filter(organization=organization).values("id", "name", "color")),
        "tasks": list(_task_rows(organization, include_archived=True)),
        "comments": [
            {
                "task_id": comment.task_id,
                "author": comment.author.get_username() if comment.author_id else None,
                "body": comment.body,
                "created_at": comment.created_at.isoformat(),
            }
            for comment in Comment.objects.filter(organization=organization).select_related("author")
        ],
        "activity": [
            {
                "verb": item.verb,
                "summary": item.summary,
                "actor": item.actor.get_username() if item.actor_id else None,
                "created_at": item.created_at.isoformat(),
            }
            for item in Activity.objects.filter(organization=organization).select_related("actor")[:5000]
        ],
    }


def export_to_string(organization, *, fmt: str = "csv", include_archived: bool = False) -> str:
    """Non-streaming variant, for tests and for attaching an export to an email."""

    if fmt == "json":
        return json.dumps(build_workspace_archive(organization), default=str, indent=2)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TASK_COLUMNS)
    for row in _task_rows(organization, include_archived=include_archived):
        writer.writerow([sanitize_cell(row[column]) for column in TASK_COLUMNS])
    return buffer.getvalue()
