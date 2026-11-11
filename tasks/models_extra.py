"""Second-round feature models: time tracking, templates, and public share links.

Kept in a separate module from :mod:`tasks.models` purely for readability — Django loads
them via the import at the bottom of ``tasks/models.py``, so they are ordinary members of
the ``tasks`` app.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from kernel.models import Organization, TimeStampedModel


class TimeEntry(TimeStampedModel):
    """Tracked work against a task.

    Supports both a running timer (``ended_at`` null) and a manual entry. The invariant
    that matters is that one person cannot have two timers running at once, or every
    reported duration becomes double-counted nonsense.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="time_entries")
    task = models.ForeignKey("tasks.Task", on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_entries")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    #: Denormalised on stop so reporting never has to recompute across millions of rows.
    duration_seconds = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True)
    billable = models.BooleanField(default=True)

    #: A timer left running overnight is almost always forgotten, not worked.
    MAX_RUNNING = timedelta(hours=12)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            # One running timer per person, enforced by the database rather than by a
            # check-then-write that races with a second browser tab.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(ended_at__isnull=True),
                name="uniq_running_timer_per_user",
            ),
            models.CheckConstraint(
                condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("started_at")),
                name="time_entry_ends_after_it_starts",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "-started_at"]),
            models.Index(fields=["task", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.user} on task {self.task_id}"

    @property
    def is_running(self) -> bool:
        return self.ended_at is None

    @property
    def elapsed_seconds(self) -> int:
        if self.ended_at:
            return self.duration_seconds
        return int((timezone.now() - self.started_at).total_seconds())

    def clean(self):
        if self.ended_at and self.ended_at < self.started_at:
            raise ValidationError({"ended_at": "A time entry cannot end before it starts."})

    def stop(self, *, at=None):
        if self.ended_at is not None:
            return self
        end = at or timezone.now()
        # Cap rather than record an absurd duration: a 40-hour "session" corrupts every
        # average it touches.
        if end - self.started_at > self.MAX_RUNNING:
            end = self.started_at + self.MAX_RUNNING
        self.ended_at = end
        self.duration_seconds = int((end - self.started_at).total_seconds())
        self.save(update_fields=["ended_at", "duration_seconds", "updated_at"])
        return self


class TaskTemplate(TimeStampedModel):
    """A reusable task blueprint, including its subtasks.

    The point is repeatable process: an onboarding checklist typed by hand every time
    drifts, and the drift is invisible until something is missed.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="task_templates")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    title_template = models.CharField(max_length=240)
    notes = models.TextField(blank=True)
    task_list = models.ForeignKey(
        "tasks.TaskList", on_delete=models.SET_NULL, null=True, blank=True, related_name="templates"
    )
    priority = models.PositiveSmallIntegerField(default=2)
    estimate_minutes = models.PositiveIntegerField(null=True, blank=True)
    #: Days from application to the generated due date. ``None`` means no due date.
    due_in_days = models.PositiveSmallIntegerField(null=True, blank=True)
    labels = models.ManyToManyField("tasks.Label", blank=True, related_name="templates")
    #: Ordered list of subtask titles.
    subtask_titles = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_templates",
    )
    times_used = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_template_name_per_org"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if not isinstance(self.subtask_titles, list) or not all(
            isinstance(item, str) for item in self.subtask_titles
        ):
            raise ValidationError({"subtask_titles": "Subtasks must be a list of strings."})
        if len(self.subtask_titles) > 100:
            raise ValidationError({"subtask_titles": "A template may define at most 100 subtasks."})
        if self.task_list_id and self.task_list.organization_id != self.organization_id:
            raise ValidationError({"task_list": "Task list belongs to a different organization."})


class ShareLink(TimeStampedModel):
    """A read-only public URL for one task list.

    Deliberately narrow: anyone with the link sees one list, read-only, with no member
    identities, and the link can be expired or revoked at any time.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="share_links")
    task_list = models.ForeignKey("tasks.TaskList", on_delete=models.CASCADE, related_name="share_links")
    #: SHA-256 of the token. A database leak yields no working share URLs.
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_share_links",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    #: Completed work is often the sensitive part; sharing it is opt-in.
    include_completed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"share link for list {self.task_list_id}"

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, organization, task_list, created_by=None, expires_at=None, label="", include_completed=False):
        raw = secrets.token_urlsafe(32)
        link = cls.objects.create(
            organization=organization,
            task_list=task_list,
            token_hash=cls.hash_token(raw),
            created_by=created_by,
            expires_at=expires_at,
            label=label[:120],
            include_completed=include_completed,
        )
        return link, raw

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return self.organization.deleted_at is None

    def record_view(self):
        ShareLink.objects.filter(pk=self.pk).update(
            view_count=models.F("view_count") + 1, last_viewed_at=timezone.now()
        )

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])
