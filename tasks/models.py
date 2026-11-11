from __future__ import annotations

import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from kernel.models import Organization, TimeStampedModel

#: Board ordering uses sparse fractional positions so inserting between two cards is a
#: single-row update instead of renumbering the whole column.
POSITION_STEP = Decimal("1000")
POSITION_MIN_GAP = Decimal("0.000002")

HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$",
    message="Colour must be a hex value such as #2563eb.",
)


def attachment_upload_path(instance, filename: str) -> str:
    """Store attachments under an unguessable, tenant-partitioned path.

    The user-supplied filename is never used on disk — only its extension — which removes
    path traversal and filename-collision risk in one step.
    """

    extension = os.path.splitext(filename)[1].lower().lstrip(".")[:12]
    suffix = f".{extension}" if extension else ""
    return f"attachments/org-{instance.organization_id}/{uuid.uuid4().hex}{suffix}"


class ProjectArea(TimeStampedModel):
    """A top-level grouping of task lists (a "space" / "area of responsibility")."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="project_areas")
    name = models.CharField(max_length=120)
    color = models.CharField(max_length=20, default="#2563eb", validators=[HEX_COLOR_VALIDATOR])
    icon = models.CharField(max_length=40, blank=True)
    position = models.PositiveIntegerField(default=0)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_project_area_name_per_org"),
        ]

    def __str__(self):
        return self.name


class TaskListQuerySet(models.QuerySet):
    def active(self):
        return self.filter(archived_at__isnull=True)


class TaskList(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="task_lists")
    project_area = models.ForeignKey(
        ProjectArea,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_lists",
    )
    name = models.CharField(max_length=120)
    color = models.CharField(max_length=20, default="#0f766e", validators=[HEX_COLOR_VALIDATOR])
    position = models.PositiveIntegerField(default=0)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TaskListQuerySet.as_manager()

    class Meta:
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_task_list_name_per_org"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.project_area_id and self.project_area.organization_id != self.organization_id:
            raise ValidationError({"project_area": "Project area belongs to a different organization."})

    def ensure_default_columns(self):
        """Create the standard board columns if the list has none.

        A list with zero columns renders an empty board and silently swallows new tasks,
        so this is called on every path that creates a list.
        """

        existing = list(self.columns.order_by("position"))
        if existing:
            return existing
        defaults = (("Backlog", False), ("In Progress", False), ("Blocked", False), ("Done", True))
        return [
            Column.objects.create(
                organization_id=self.organization_id,
                task_list=self,
                name=name,
                position=index,
                is_done=is_done,
            )
            for index, (name, is_done) in enumerate(defaults, start=1)
        ]


class ListMembership(TimeStampedModel):
    """Optional per-list access narrowing within an organization."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    task_list = models.ForeignKey(TaskList, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_list_memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task_list", "user"], name="uniq_list_membership_user"),
        ]

    def __str__(self):
        return f"{self.user} on {self.task_list}"


class Column(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="columns")
    task_list = models.ForeignKey(TaskList, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField(default=0)
    #: Moving a task into an ``is_done`` column completes it, and vice versa.
    is_done = models.BooleanField(default=False)
    #: 0 means unlimited. Enforced by ``tasks.services.move_task``.
    wip_limit = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(fields=["task_list", "name"], name="uniq_column_name_per_list"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.task_list_id and self.task_list.organization_id != self.organization_id:
            raise ValidationError({"task_list": "Task list belongs to a different organization."})

    def save(self, *args, **kwargs):
        # The denormalised organization exists so board queries need no join; keeping it
        # authoritative here means it can never drift from the parent list.
        if self.task_list_id and not self.organization_id:
            self.organization_id = self.task_list.organization_id
        return super().save(*args, **kwargs)


class Label(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=80)
    color = models.CharField(max_length=20, default="#7c3aed", validators=[HEX_COLOR_VALIDATOR])

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_label_name_per_org"),
        ]

    def __str__(self):
        return self.name


class TaskQuerySet(models.QuerySet):
    def active(self):
        return self.filter(archived_at__isnull=True)

    def open(self):
        return self.filter(status=Task.Status.OPEN, archived_at__isnull=True)

    def overdue(self, as_of=None):
        return self.open().filter(due_date__lt=as_of or timezone.localdate())

    def due_on(self, day):
        return self.open().filter(due_date=day)

    def for_board(self):
        return self.select_related("column", "task_list", "assignee").prefetch_related("labels")


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    class Priority(models.IntegerChoices):
        LOW = 1, "Low"
        NORMAL = 2, "Normal"
        HIGH = 3, "High"
        URGENT = 4, "Urgent"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="tasks")
    task_list = models.ForeignKey(TaskList, on_delete=models.PROTECT, related_name="tasks")
    column = models.ForeignKey(
        Column, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_tasks",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tasks",
    )
    title = models.CharField(max_length=240)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    due_time = models.TimeField(null=True, blank=True)
    priority = models.PositiveSmallIntegerField(choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    position = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    #: Optimistic concurrency token. Bumped on every domain mutation; a client that sends a
    #: stale value gets 409 instead of silently clobbering a concurrent edit.
    version = models.PositiveIntegerField(default=1)
    estimate_minutes = models.PositiveIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_tasks",
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    labels = models.ManyToManyField(Label, blank=True, related_name="tasks")
    #: Set when a task is materialised from a RecurrenceRule, so occurrences are traceable.
    recurrence_source = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recurrence_instances",
    )

    objects = TaskQuerySet.as_manager()

    class Meta:
        # Explicit ordering: priority descending then due date. The previous ordering keyed
        # on `status`, which sorts alphabetically (archived < completed < open) and put
        # archived work at the top of every list.
        ordering = ["-priority", models.F("due_date").asc(nulls_last=True), "position", "-created_at"]
        indexes = [
            models.Index(fields=["organization", "status", "due_date"]),
            models.Index(fields=["organization", "assignee", "status"]),
            models.Index(fields=["task_list", "column", "position"]),
            models.Index(fields=["organization", "-created_at"]),
            # Matches the default ordering exactly, so "the first page of a workspace's
            # tasks" is an index read of one page instead of a sort of every task in the
            # workspace. Measured on a 43,000-task workspace: 11 ms -> 0.09 ms.
            models.Index(
                models.F("organization"),
                models.F("priority").desc(),
                # Plain ascending: PostgreSQL already sorts NULLs last for ASC, which is what
                # the default ordering asks for, and SQLite cannot declare NULLS LAST in an index.
                models.F("due_date").asc(),
                models.F("position"),
                models.F("created_at").desc(),
                name="task_default_order_idx",
                condition=models.Q(archived_at__isnull=True),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(due_time__isnull=True) | models.Q(due_date__isnull=False),
                name="task_due_time_requires_date",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="completed") | models.Q(completed_at__isnull=False),
                name="task_completed_requires_timestamp",
            ),
            # Recurrence occurrence identity. A read-then-write check in the materialiser
            # races under concurrent sweeps; this makes the duplicate impossible at the
            # database. NULL recurrence_source rows never collide, so ordinary tasks are
            # unaffected.
            models.UniqueConstraint(
                fields=["recurrence_source", "due_date"],
                condition=models.Q(recurrence_source__isnull=False),
                name="uniq_recurrence_occurrence_per_date",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self) -> bool:
        return (
            self.status == self.Status.OPEN
            and self.due_date is not None
            and self.due_date < timezone.localdate()
        )

    @property
    def is_done(self) -> bool:
        return self.status == self.Status.COMPLETED

    def clean(self):
        if self.column_id and self.task_list_id and self.column.task_list_id != self.task_list_id:
            raise ValidationError({"column": "Column does not belong to this task list."})
        if self.due_time and not self.due_date:
            raise ValidationError({"due_time": "A due time requires a due date."})

    def complete(self, *, actor=None):
        if self.status == self.Status.COMPLETED:
            return self
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.completed_by = actor if getattr(actor, "pk", None) else None
        self.version += 1
        self.save(update_fields=["status", "completed_at", "completed_by", "version", "updated_at"])
        return self

    def reopen(self):
        if self.status == self.Status.OPEN:
            return self
        self.status = self.Status.OPEN
        self.completed_at = None
        self.completed_by = None
        self.version += 1
        self.save(update_fields=["status", "completed_at", "completed_by", "version", "updated_at"])
        return self

    def archive(self):
        if self.archived_at is not None:
            return self
        self.archived_at = timezone.now()
        self.status = self.Status.ARCHIVED
        self.version += 1
        self.save(update_fields=["archived_at", "status", "version", "updated_at"])
        return self

    def unarchive(self):
        if self.archived_at is None:
            return self
        self.archived_at = None
        self.status = self.Status.COMPLETED if self.completed_at else self.Status.OPEN
        self.version += 1
        self.save(update_fields=["archived_at", "status", "version", "updated_at"])
        return self


class Subtask(TimeStampedModel):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="subtasks")
    title = models.CharField(max_length=180)
    completed_at = models.DateTimeField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "created_at"]
        indexes = [models.Index(fields=["task", "position"])]

    def __str__(self):
        return self.title

    @property
    def is_done(self) -> bool:
        return self.completed_at is not None

    def toggle(self):
        self.completed_at = None if self.completed_at else timezone.now()
        self.save(update_fields=["completed_at", "updated_at"])
        return self


class Comment(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="comments")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_comments"
    )
    body = models.TextField()
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["task", "created_at"])]

    def __str__(self):
        return f"comment on task {self.task_id}"


class Attachment(TimeStampedModel):
    """A file attached to a task.

    Size and extension are validated server-side; the stored path is randomised and the
    download URL is signed and short-lived (see ``web.views.attachment_download``).
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="attachments")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_attachments",
    )
    file = models.FileField(upload_to=attachment_upload_path)
    #: The name the user saw. Displayed escaped; never used to build a filesystem path.
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)

    class ScanStatus(models.TextChoices):
        NOT_SCANNED = "not_scanned", "Not scanned"
        PENDING = "pending", "Scan pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"

    scan_status = models.CharField(
        max_length=20, choices=ScanStatus.choices, default=ScanStatus.NOT_SCANNED, db_index=True
    )
    #: What the scanner called it, when infected. Shown to admins; never executed or parsed.
    scan_signature = models.CharField(max_length=200, blank=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "task"])]

    def __str__(self):
        return self.original_name

    @property
    def is_downloadable(self) -> bool:
        from .scanning import is_downloadable

        return is_downloadable(self)


class Reminder(TimeStampedModel):
    class Status(models.TextChoices):
        ARMED = "armed", "Armed"
        SENT = "sent", "Sent"
        CANCELED = "canceled", "Canceled"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="reminders")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="reminders")
    remind_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ARMED)
    job = models.ForeignKey(
        "queues.Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["remind_at"]
        constraints = [
            models.UniqueConstraint(fields=["task", "remind_at"], name="uniq_task_reminder_time"),
        ]
        indexes = [models.Index(fields=["status", "remind_at"])]

    def __str__(self):
        return f"reminder for task {self.task_id}"

    def cancel(self):
        if self.status != self.Status.ARMED:
            return self
        self.status = self.Status.CANCELED
        self.save(update_fields=["status", "updated_at"])
        return self


class RecurrenceRule(TimeStampedModel):
    """Generates follow-up tasks after the source task completes or its date passes."""

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="recurrences")
    source_task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="recurrence")
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    interval = models.PositiveSmallIntegerField(default=1)
    #: Weekly rules only: ISO weekdays (1=Mon … 7=Sun). Empty means "same weekday as source".
    weekdays = models.JSONField(default=list, blank=True)
    next_occurrence_date = models.DateField()
    until_date = models.DateField(null=True, blank=True)
    max_occurrences = models.PositiveIntegerField(null=True, blank=True)
    occurrences_created = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["next_occurrence_date"]
        constraints = [
            # ``source_task`` is a OneToOne, so a (source_task, next_occurrence_date)
            # constraint would add nothing. The occurrence-identity guard that actually
            # matters lives on Task: uniq_recurrence_occurrence_per_date.
            models.CheckConstraint(
                condition=models.Q(interval__gte=1), name="recurrence_interval_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(until_date__isnull=True)
                | models.Q(until_date__gte=models.F("next_occurrence_date")),
                name="recurrence_until_after_next",
            ),
        ]

    def __str__(self):
        return f"{self.get_frequency_display()} x{self.interval}"

    def clean(self):
        if self.frequency == self.Frequency.WEEKLY and self.weekdays:
            if not isinstance(self.weekdays, list) or not all(
                isinstance(day, int) and 1 <= day <= 7 for day in self.weekdays
            ):
                raise ValidationError({"weekdays": "Weekdays must be a list of integers 1-7."})
        if self.until_date and self.until_date < self.next_occurrence_date:
            raise ValidationError({"until_date": "End date cannot precede the next occurrence."})

    @property
    def is_exhausted(self) -> bool:
        if not self.active:
            return True
        if self.max_occurrences is not None and self.occurrences_created >= self.max_occurrences:
            return True
        if self.until_date is not None and self.next_occurrence_date > self.until_date:
            return True
        return False


class TaskDependency(TimeStampedModel):
    """``blocked_task`` cannot be completed until ``blocking_task`` is done."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="task_dependencies"
    )
    blocked_task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="blocked_by")
    blocking_task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="blocks")

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "task dependencies"
        constraints = [
            models.UniqueConstraint(
                fields=["blocked_task", "blocking_task"], name="uniq_task_dependency_pair"
            ),
            models.CheckConstraint(
                condition=~models.Q(blocked_task=models.F("blocking_task")),
                name="task_cannot_block_itself",
            ),
        ]

    def __str__(self):
        return f"{self.blocked_task_id} blocked by {self.blocking_task_id}"


# Time tracking, templates and share links live in a sibling module for readability. The
# import must stay at the bottom: those models reference the classes defined above.
from .models_extra import ShareLink, TaskTemplate, TimeEntry  # noqa: E402,F401
