"""Forms for the HTML application.

Every form that touches tenant data takes the organization explicitly and narrows its
relational querysets to it, so a crafted POST cannot reference another workspace's rows.
"""

from __future__ import annotations

import zoneinfo

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from kernel.models import Membership, NotificationPreference, Organization, SavedView
from kernel.roles import Role
from schedules.models import Schedule
from schedules.services import validate_expression
from tasks.models import Column, Label, ProjectArea, Task, TaskList

User = get_user_model()

COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Africa/Lagos",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
]


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text="Used for reminders and password reset.")
    workspace_name = forms.CharField(
        max_length=160,
        required=False,
        help_text="Leave blank to join an existing workspace by invitation.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            # Enumeration is a real concern, but so is a user silently creating a duplicate
            # account they can never recover. Registration is rate-limited instead.
            raise ValidationError("An account with that email address already exists.")
        return email


class BrandedAuthenticationForm(AuthenticationForm):
    """Login form that reports lockout state instead of a bare "invalid credentials"."""

    def clean(self):
        from kernel.auth_backends import client_ip, is_locked_out

        username = self.cleaned_data.get("username")
        ip = client_ip(self.request) if self.request is not None else ""
        if is_locked_out(username, ip):
            raise ValidationError(
                "Too many failed sign-in attempts. Try again in a few minutes.",
                code="locked_out",
            )
        return super().clean()


class OrganizationForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=[(tz, tz) for tz in COMMON_TIMEZONES], initial="UTC")

    class Meta:
        model = Organization
        fields = ["name", "timezone"]

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            zoneinfo.ZoneInfo(value)
        except Exception as exc:
            raise ValidationError("Select a valid timezone.") from exc
        return value


class TenantModelForm(forms.ModelForm):
    """Base form that scopes every relational field to one organization."""

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)
        # Stamp the instance *before* validation, not in save(). Model `clean()` methods
        # compare related objects against `self.organization_id`; leaving it unset until
        # save makes every one of those checks compare a real id against None and reject a
        # perfectly valid form with "belongs to a different organization".
        if organization is not None and getattr(self.instance, "organization_id", None) is None:
            self.instance.organization = organization
        self._scope_querysets()

    def _get_validation_exclusions(self):
        """Keep ``organization`` in scope for uniqueness validation.

        Django excludes any field absent from the form, and skips a ``UniqueConstraint``
        whose fields are excluded. Every uniqueness rule in this project is *scoped by
        tenant* â€” ``(organization, name)`` â€” so excluding the column skipped them all, and
        a duplicate label or list surfaced as an IntegrityError 500 instead of a field
        error. The instance already carries the organization by this point (see __init__).
        """

        exclusions = super()._get_validation_exclusions()
        if self.organization is not None:
            exclusions.discard("organization")
        return exclusions

    def _scope_querysets(self):
        if self.organization is None:
            return
        for field in self.fields.values():
            queryset = getattr(field, "queryset", None)
            if queryset is None:
                continue
            model = queryset.model
            field_names = {f.name for f in model._meta.get_fields()}
            if "organization" in field_names:
                field.queryset = queryset.filter(organization=self.organization)

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.organization is not None and hasattr(instance, "organization_id"):
            instance.organization = self.organization
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TaskForm(TenantModelForm):
    labels = forms.ModelMultipleChoiceField(
        queryset=Label.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), required=False)
    #: Round-trips the row version so a stale tab is rejected instead of overwriting.
    expected_version = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Task
        fields = [
            "title",
            "notes",
            "task_list",
            "column",
            "assignee",
            "due_date",
            "due_time",
            "priority",
            "estimate_minutes",
            "labels",
        ]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "due_time": forms.TimeInput(attrs={"type": "time"}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        if organization is not None:
            self.fields["labels"].queryset = Label.objects.filter(organization=organization)
            self.fields["task_list"].queryset = TaskList.objects.filter(
                organization=organization, archived_at__isnull=True
            )
            self.fields["column"].queryset = Column.objects.filter(organization=organization)
            self.fields["assignee"].queryset = User.objects.filter(
                memberships__organization=organization,
                memberships__status=Membership.Status.ACTIVE,
            ).distinct()
        self.fields["column"].required = False

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise ValidationError("Give the task a title.")
        return title

    def clean(self):
        cleaned = super().clean()
        task_list = cleaned.get("task_list")
        column = cleaned.get("column")
        if column and task_list and column.task_list_id != task_list.pk:
            self.add_error("column", "That column belongs to a different list.")
        if cleaned.get("due_time") and not cleaned.get("due_date"):
            self.add_error("due_time", "Pick a due date as well as a time.")
        return cleaned


class QuickAddForm(forms.Form):
    quick_add = forms.CharField(max_length=2000, strip=True)

    def clean_quick_add(self):
        value = (self.cleaned_data.get("quick_add") or "").strip()
        if not value:
            raise ValidationError("Type something to add.")
        return value


class TaskListForm(TenantModelForm):
    class Meta:
        model = TaskList
        fields = ["name", "project_area", "color", "position"]


class ProjectAreaForm(TenantModelForm):
    class Meta:
        model = ProjectArea
        fields = ["name", "color", "icon", "position"]


class LabelForm(TenantModelForm):
    class Meta:
        model = Label
        fields = ["name", "color"]


class ColumnForm(TenantModelForm):
    class Meta:
        model = Column
        fields = ["task_list", "name", "position", "is_done", "wip_limit"]


class CommentForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=10000)

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise ValidationError("Write something first.")
        return body


class SubtaskForm(forms.Form):
    title = forms.CharField(max_length=180)


class AttachmentForm(forms.Form):
    """Server-side size and extension validation. Client-side ``accept`` is not a control."""

    file = forms.FileField()

    def clean_file(self):
        from django.conf import settings

        uploaded = self.cleaned_data["file"]
        max_bytes = getattr(settings, "ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)
        if uploaded.size > max_bytes:
            raise ValidationError(f"Files must be {max_bytes // (1024 * 1024)} MB or smaller.")

        allowed = {ext.lower() for ext in getattr(settings, "ATTACHMENT_ALLOWED_EXTENSIONS", [])}
        extension = (uploaded.name.rsplit(".", 1)[-1] if "." in uploaded.name else "").lower()
        if allowed and extension not in allowed:
            raise ValidationError(f"'{extension or 'unknown'}' files are not allowed.")
        return uploaded


class InviteForm(forms.Form):
    email = forms.EmailField()
    role = forms.ChoiceField(
        choices=[(r.value, r.label) for r in Role if r != Role.OWNER], initial=Role.MEMBER
    )


class MemberRoleForm(forms.Form):
    role = forms.ChoiceField(choices=Role.choices)


class ScheduleForm(TenantModelForm):
    class Meta:
        model = Schedule
        fields = [
            "name",
            "queue",
            "expression_type",
            "expression",
            "timezone",
            "target_handler",
            "payload",
            "catch_up_policy",
            "next_run_at",
            "active",
        ]
        widgets = {"next_run_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean(self):
        cleaned = super().clean()
        try:
            validate_expression(
                cleaned.get("expression_type"),
                cleaned.get("expression", ""),
                cleaned.get("timezone", "UTC"),
                next_run_at=cleaned.get("next_run_at"),
            )
        except ValueError as exc:
            self.add_error("expression", str(exc))
        return cleaned


class SavedViewForm(TenantModelForm):
    class Meta:
        model = SavedView
        fields = ["name", "is_shared", "position"]


class NotificationPreferenceForm(forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = [
            "in_app_enabled",
            "email_enabled",
            "email_reminders",
            "email_assignments",
            "email_mentions",
            "email_comments",
            "digest",
            "quiet_hours_start",
            "quiet_hours_end",
        ]
        widgets = {
            "quiet_hours_start": forms.TimeInput(attrs={"type": "time"}),
            "quiet_hours_end": forms.TimeInput(attrs={"type": "time"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("quiet_hours_start"), cleaned.get("quiet_hours_end")
        if (start is None) != (end is None):
            raise ValidationError("Set both a start and an end time for quiet hours, or neither.")
        return cleaned


class ApiKeyForm(forms.Form):
    name = forms.CharField(max_length=120)
    role = forms.ChoiceField(choices=Role.choices, initial=Role.MEMBER)
    expires_in_days = forms.IntegerField(required=False, min_value=1, max_value=3650)

    def clean_expires_in_days(self):
        days = self.cleaned_data.get("expires_in_days")
        if not days:
            return None
        from datetime import timedelta

        return timezone.now() + timedelta(days=days)


class WebhookEndpointForm(TenantModelForm):
    events = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave everything unchecked to receive all events.",
    )

    class Meta:
        from integrations.models import WebhookEndpoint

        model = WebhookEndpoint
        fields = ["name", "url", "events", "description", "active"]

    def __init__(self, *args, **kwargs):
        from integrations.models import WebhookEvent

        super().__init__(*args, **kwargs)
        self.fields["events"].choices = WebhookEvent.choices

    def clean_url(self):
        from integrations.delivery import validate_webhook_url

        url = self.cleaned_data["url"]
        try:
            # Resolved and checked here so an SSRF target is rejected at configuration
            # time rather than discovered by the worker at delivery time.
            validate_webhook_url(url)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return url


class LogTimeForm(forms.Form):
    minutes = forms.IntegerField(min_value=1, max_value=24 * 60)
    note = forms.CharField(max_length=255, required=False)


class TaskTemplateForm(TenantModelForm):
    subtask_titles_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        label="Subtasks",
        help_text="One per line.",
    )

    class Meta:
        from tasks.models import TaskTemplate

        model = TaskTemplate
        fields = [
            "name",
            "description",
            "title_template",
            "notes",
            "task_list",
            "priority",
            "estimate_minutes",
            "due_in_days",
            "labels",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance is not None and instance.pk:
            self.fields["subtask_titles_text"].initial = "\n".join(instance.subtask_titles or [])

    def clean(self):
        cleaned = super().clean()
        lines = [
            line.strip()
            for line in (cleaned.get("subtask_titles_text") or "").splitlines()
            if line.strip()
        ]
        if len(lines) > 100:
            self.add_error("subtask_titles_text", "A template may define at most 100 subtasks.")
        cleaned["subtask_titles"] = [line[:180] for line in lines]
        return cleaned


class ShareLinkForm(forms.Form):
    task_list = forms.ModelChoiceField(queryset=TaskList.objects.none())
    label = forms.CharField(max_length=120, required=False)
    expires_in_days = forms.IntegerField(
        min_value=1, max_value=365, required=False, help_text="Leave blank for no expiry."
    )
    include_completed = forms.BooleanField(required=False)

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.fields["task_list"].queryset = TaskList.objects.filter(
                organization=organization, archived_at__isnull=True
            )


class BulkActionForm(forms.Form):
    ACTIONS = [
        ("complete", "Mark complete"),
        ("reopen", "Reopen"),
        ("archive", "Archive"),
        ("priority", "Set priority"),
        ("assign", "Assign to"),
        ("add_label", "Add label"),
        ("move_list", "Move to list"),
    ]

    action = forms.ChoiceField(choices=ACTIONS)
    value = forms.CharField(required=False)


class TaskFilterForm(forms.Form):
    """Read-only filter form for the task list. Every field is optional."""

    STATUS_CHOICES = [("", "Any status"), *Task.Status.choices]
    DUE_CHOICES = [
        ("", "Any due date"),
        ("overdue", "Overdue"),
        ("today", "Due today"),
        ("week", "Next 7 days"),
        ("none", "No due date"),
    ]

    q = forms.CharField(required=False, max_length=200)
    status = forms.ChoiceField(required=False, choices=STATUS_CHOICES)
    due = forms.ChoiceField(required=False, choices=DUE_CHOICES)
    priority = forms.ChoiceField(
        required=False, choices=[("", "Any priority"), *Task.Priority.choices]
    )
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), required=False)
    task_list = forms.ModelChoiceField(queryset=TaskList.objects.none(), required=False)
    label = forms.ModelChoiceField(queryset=Label.objects.none(), required=False)

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.fields["task_list"].queryset = TaskList.objects.filter(
                organization=organization, archived_at__isnull=True
            )
            self.fields["label"].queryset = Label.objects.filter(organization=organization)
            self.fields["assignee"].queryset = User.objects.filter(
                memberships__organization=organization,
                memberships__status=Membership.Status.ACTIVE,
            ).distinct()
