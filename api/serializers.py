"""API serializers.

Two rules run through every serializer here:

1. ``organization`` is **never** writable. It is derived from the authenticated context, so
   a client cannot create or move a row into another tenant.
2. Every relational field is validated against the caller's organization via
   :class:`TenantPrimaryKeyRelatedField`, which closes the "valid id, wrong tenant" hole
   that plain ``PrimaryKeyRelatedField`` leaves open.
"""

from __future__ import annotations

import zoneinfo

from django.contrib.auth import get_user_model
from rest_framework import serializers

from integrations.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from kernel.models import (
    Activity,
    ApiKey,
    AuditLog,
    Invitation,
    Membership,
    Notification,
    NotificationPreference,
    Organization,
    SavedView,
)
from kernel.roles import Role
from queues.models import DeadLetter, Job, JobAttempt, Queue
from schedules.models import Schedule, ScheduleOccurrence
from schedules.services import validate_expression
from tasks.models import (
    Column,
    Comment,
    Label,
    ProjectArea,
    RecurrenceRule,
    Reminder,
    Subtask,
    Task,
    TaskList,
)

User = get_user_model()


class TenantPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """A related field whose choices are restricted to the caller's organization."""

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.context.get("organization")
        if organization is None:
            return queryset.none()
        model = queryset.model
        field_names = {field.name for field in model._meta.get_fields()}
        if "organization" in field_names:
            return queryset.filter(organization=organization)
        return queryset


class OrganizationMemberField(serializers.PrimaryKeyRelatedField):
    """User references limited to active members of the caller's organization."""

    def get_queryset(self):
        organization = self.context.get("organization")
        if organization is None:
            return User.objects.none()
        return User.objects.filter(
            memberships__organization=organization,
            memberships__status=Membership.Status.ACTIVE,
        ).distinct()


class TenantScopedSerializer(serializers.ModelSerializer):
    """Base class that pins ``organization`` to the request context."""

    organization = serializers.PrimaryKeyRelatedField(read_only=True)

    def create(self, validated_data):
        validated_data["organization"] = self._organization()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Belt and braces: even if a subclass makes the field writable by accident,
        # it can never be retargeted at another tenant.
        validated_data.pop("organization", None)
        return super().update(instance, validated_data)

    def _organization(self):
        organization = self.context.get("organization")
        if organization is None:
            raise serializers.ValidationError("No active workspace for this request.")
        return organization


def _validate_timezone(value: str) -> str:
    try:
        zoneinfo.ZoneInfo(value)
    except Exception as exc:
        raise serializers.ValidationError(f"Unknown timezone {value!r}.") from exc
    return value


# --------------------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------------------


class OrganizationSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "timezone", "role", "member_count", "created_at"]
        read_only_fields = ["id", "slug", "role", "member_count", "created_at"]

    def get_role(self, obj) -> str | None:
        tenant = self.context.get("tenant")
        if tenant is not None and tenant.organization.pk == obj.pk:
            return tenant.role
        request = self.context.get("request")
        return obj.role_for(getattr(request, "user", None))

    def get_member_count(self, obj) -> int:
        return obj.memberships.filter(status=Membership.Status.ACTIVE).count()

    def validate_timezone(self, value):
        return _validate_timezone(value)


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = [
            "id",
            "organization",
            "user",
            "username",
            "email",
            "role",
            "status",
            "joined_at",
            "last_active_at",
        ]
        read_only_fields = ["id", "organization", "user", "joined_at", "last_active_at"]


class InvitationSerializer(TenantScopedSerializer):
    class Meta:
        model = Invitation
        fields = ["id", "organization", "email", "role", "status", "expires_at", "created_at"]
        read_only_fields = ["id", "organization", "status", "expires_at", "created_at"]

    def validate_role(self, value):
        if value == Role.OWNER:
            raise serializers.ValidationError("Ownership is transferred, not invited.")
        return value


class ApiKeySerializer(TenantScopedSerializer):
    """Never exposes the secret. The plaintext is returned only by the create action."""

    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            "id",
            "organization",
            "name",
            "prefix",
            "role",
            "is_active",
            "last_used_at",
            "expires_at",
            "revoked_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "organization",
            "prefix",
            "is_active",
            "last_used_at",
            "revoked_at",
            "created_at",
        ]


class SavedViewSerializer(TenantScopedSerializer):
    class Meta:
        model = SavedView
        fields = [
            "id",
            "organization",
            "owner",
            "name",
            "filters",
            "is_shared",
            "position",
            "created_at",
        ]
        read_only_fields = ["id", "organization", "owner", "created_at"]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = [
            "id",
            "organization",
            "in_app_enabled",
            "email_enabled",
            "email_reminders",
            "email_assignments",
            "email_mentions",
            "email_comments",
            "digest",
            "quiet_hours_start",
            "quiet_hours_end",
            "unsubscribed_at",
        ]
        read_only_fields = ["id", "organization", "unsubscribed_at"]

    def validate(self, attrs):
        start = attrs.get("quiet_hours_start", getattr(self.instance, "quiet_hours_start", None))
        end = attrs.get("quiet_hours_end", getattr(self.instance, "quiet_hours_end", None))
        if (start is None) != (end is None):
            raise serializers.ValidationError("Quiet hours need both a start and an end time.")
        return attrs


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


class ProjectAreaSerializer(TenantScopedSerializer):
    class Meta:
        model = ProjectArea
        fields = [
            "id",
            "organization",
            "name",
            "color",
            "icon",
            "position",
            "archived_at",
            "created_at",
        ]
        read_only_fields = ["id", "organization", "created_at"]


class TaskListSerializer(TenantScopedSerializer):
    project_area = TenantPrimaryKeyRelatedField(
        queryset=ProjectArea.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = TaskList
        fields = [
            "id",
            "organization",
            "project_area",
            "name",
            "color",
            "position",
            "archived_at",
            "created_at",
        ]
        read_only_fields = ["id", "organization", "created_at"]


class ColumnSerializer(TenantScopedSerializer):
    task_list = TenantPrimaryKeyRelatedField(queryset=TaskList.objects.all())

    class Meta:
        model = Column
        fields = ["id", "organization", "task_list", "name", "position", "is_done"]
        read_only_fields = ["id", "organization"]


class LabelSerializer(TenantScopedSerializer):
    class Meta:
        model = Label
        fields = ["id", "organization", "name", "color"]
        read_only_fields = ["id", "organization"]


class SubtaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subtask
        fields = ["id", "task", "title", "completed_at", "position"]
        read_only_fields = ["id", "task"]


class CommentSerializer(TenantScopedSerializer):
    author_username = serializers.CharField(source="author.username", read_only=True)
    task = TenantPrimaryKeyRelatedField(queryset=Task.objects.all())

    class Meta:
        model = Comment
        fields = ["id", "organization", "task", "author", "author_username", "body", "created_at"]
        read_only_fields = ["id", "organization", "author", "author_username", "created_at"]

    def validate_body(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Comment body cannot be empty.")
        return value


class ReminderSerializer(TenantScopedSerializer):
    task = TenantPrimaryKeyRelatedField(queryset=Task.objects.all())

    class Meta:
        model = Reminder
        fields = ["id", "organization", "task", "remind_at", "status", "job", "created_at"]
        read_only_fields = ["id", "organization", "status", "job", "created_at"]


class RecurrenceRuleSerializer(TenantScopedSerializer):
    source_task = TenantPrimaryKeyRelatedField(queryset=Task.objects.all())

    class Meta:
        model = RecurrenceRule
        fields = [
            "id",
            "organization",
            "source_task",
            "frequency",
            "interval",
            "weekdays",
            "next_occurrence_date",
            "until_date",
            "max_occurrences",
            "occurrences_created",
            "active",
        ]
        read_only_fields = ["id", "organization", "occurrences_created"]


class TaskSerializer(TenantScopedSerializer):
    """The core resource. Writes are validated against the tenant and the task's own list."""

    labels = LabelSerializer(many=True, read_only=True)
    label_ids = TenantPrimaryKeyRelatedField(
        queryset=Label.objects.all(), many=True, write_only=True, required=False, source="labels"
    )
    task_list = TenantPrimaryKeyRelatedField(queryset=TaskList.objects.all())
    column = TenantPrimaryKeyRelatedField(
        queryset=Column.objects.all(), required=False, allow_null=True
    )
    assignee = OrganizationMemberField(queryset=User.objects.none(), required=False, allow_null=True)
    subtasks = SubtaskSerializer(many=True, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = [
            "id",
            "organization",
            "task_list",
            "column",
            "creator",
            "assignee",
            "title",
            "notes",
            "due_date",
            "due_time",
            "priority",
            "status",
            "position",
            "version",
            "estimate_minutes",
            "completed_at",
            "archived_at",
            "labels",
            "label_ids",
            "subtasks",
            "is_overdue",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "organization",
            "creator",
            "version",
            "completed_at",
            "archived_at",
            "created_at",
            "updated_at",
        ]

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Title cannot be empty.")
        return value

    def validate(self, attrs):
        task_list = attrs.get("task_list") or getattr(self.instance, "task_list", None)
        column = attrs.get("column", getattr(self.instance, "column", None))
        # A column belongs to exactly one list; allowing a mismatch would render the board
        # inconsistent and silently hide the task.
        if column is not None and task_list is not None and column.task_list_id != task_list.pk:
            raise serializers.ValidationError(
                {"column": "Column does not belong to the selected task list."}
            )
        due_date = attrs.get("due_date", getattr(self.instance, "due_date", None))
        if attrs.get("due_time") and not due_date:
            raise serializers.ValidationError({"due_time": "A due time requires a due date."})
        return attrs

    def create(self, validated_data):
        labels = validated_data.pop("labels", [])
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "pk", None):
            validated_data.setdefault("creator", user)
        task = super().create(validated_data)
        if labels:
            task.labels.set(labels)
        return task

    def update(self, instance, validated_data):
        labels = validated_data.pop("labels", None)
        task = super().update(instance, validated_data)
        if labels is not None:
            task.labels.set(labels)
        return task


class TaskMoveSerializer(serializers.Serializer):
    """Payload for the board-move action, including the optimistic-concurrency token."""

    column = serializers.IntegerField(required=False, allow_null=True)
    before_id = serializers.IntegerField(required=False, allow_null=True)
    after_id = serializers.IntegerField(required=False, allow_null=True)
    expected_version = serializers.IntegerField(required=False, allow_null=True)


# --------------------------------------------------------------------------------------
# Queues and schedules
# --------------------------------------------------------------------------------------


class QueueSerializer(TenantScopedSerializer):
    depth = serializers.IntegerField(read_only=True)

    class Meta:
        model = Queue
        fields = [
            "id",
            "organization",
            "name",
            "mode",
            "paused",
            "concurrency_limit",
            "rate_limit_per_minute",
            "visibility_timeout_seconds",
            "max_receives",
            "depth",
        ]
        read_only_fields = ["id", "organization", "depth"]

    def validate_concurrency_limit(self, value):
        if value < 1:
            raise serializers.ValidationError("Concurrency limit must be at least 1.")
        return value


class JobAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobAttempt
        fields = [
            "id",
            "job",
            "attempt_number",
            "outcome",
            "started_at",
            "finished_at",
            "duration_ms",
            "error",
            "result",
            "worker_id",
        ]
        read_only_fields = fields


class JobSerializer(serializers.ModelSerializer):
    """Read-only. Jobs are created by domain services, never by API clients."""

    attempts = JobAttemptSerializer(many=True, read_only=True)
    queue_name = serializers.CharField(source="queue.name", read_only=True)

    class Meta:
        model = Job
        fields = [
            "id",
            "organization",
            "queue",
            "queue_name",
            "status",
            "source",
            "handler_type",
            "handler",
            "payload",
            "payload_version",
            "idempotency_key",
            "priority",
            "available_at",
            "locked_at",
            "lease_expires_at",
            "receives",
            "attempts_made",
            "max_attempts",
            "next_retry_at",
            "last_error",
            "result",
            "correlation_id",
            "attempts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DeadLetterSerializer(serializers.ModelSerializer):
    job_handler = serializers.CharField(source="job.handler", read_only=True)

    class Meta:
        model = DeadLetter
        fields = ["id", "organization", "job", "job_handler", "reason", "redriven_at", "created_at"]
        read_only_fields = fields


class WebhookEndpointSerializer(TenantScopedSerializer):
    """Secret is write-never; create responses attach ``secret`` once in the view."""

    is_deliverable = serializers.BooleanField(read_only=True)

    class Meta:
        model = WebhookEndpoint
        fields = [
            "id",
            "organization",
            "name",
            "url",
            "events",
            "description",
            "active",
            "is_deliverable",
            "consecutive_failures",
            "disabled_at",
            "last_success_at",
            "last_failure_at",
            "last_status_code",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "organization",
            "is_deliverable",
            "consecutive_failures",
            "disabled_at",
            "last_success_at",
            "last_failure_at",
            "last_status_code",
            "created_at",
            "updated_at",
        ]

    def validate_url(self, value):
        from integrations.delivery import validate_webhook_url

        try:
            validate_webhook_url(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate_events(self, value):
        allowed = {choice.value for choice in WebhookEvent}
        unknown = set(value or []) - allowed
        if unknown:
            raise serializers.ValidationError(f"Unknown events: {', '.join(sorted(unknown))}")
        return value or []


class WebhookDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDelivery
        fields = [
            "id",
            "organization",
            "endpoint",
            "event",
            "event_id",
            "status",
            "status_code",
            "error",
            "duration_ms",
            "attempt",
            "created_at",
        ]
        read_only_fields = fields


class ScheduleSerializer(TenantScopedSerializer):
    queue = TenantPrimaryKeyRelatedField(queryset=Queue.objects.all())

    class Meta:
        model = Schedule
        fields = [
            "id",
            "organization",
            "queue",
            "name",
            "expression_type",
            "expression",
            "timezone",
            "target_handler",
            "payload",
            "active",
            "catch_up_policy",
            "next_run_at",
            "last_run_at",
            "created_at",
        ]
        read_only_fields = ["id", "organization", "last_run_at", "created_at"]

    def validate_timezone(self, value):
        return _validate_timezone(value)

    def validate(self, attrs):
        expression_type = attrs.get("expression_type") or getattr(self.instance, "expression_type", None)
        expression = attrs.get("expression", getattr(self.instance, "expression", ""))
        tz_name = attrs.get("timezone") or getattr(self.instance, "timezone", "UTC")
        next_run_at = attrs.get("next_run_at", getattr(self.instance, "next_run_at", None))
        try:
            validate_expression(expression_type, expression, tz_name, next_run_at=next_run_at)
        except ValueError as exc:
            raise serializers.ValidationError({"expression": str(exc)}) from exc
        return attrs


class ScheduleOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleOccurrence
        fields = [
            "id",
            "organization",
            "schedule",
            "scheduled_time",
            "status",
            "job",
            "error",
            "created_at",
        ]
        read_only_fields = fields


# --------------------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------------------


class ActivitySerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True, default=None)

    class Meta:
        model = Activity
        fields = [
            "id",
            "organization",
            "actor",
            "actor_username",
            "verb",
            "target_type",
            "target_id",
            "summary",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "organization",
            "actor",
            "actor_label",
            "action",
            "target_type",
            "target_id",
            "ip_address",
            "request_id",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "organization",
            "user",
            "title",
            "body",
            "category",
            "status",
            "natural_key",
            "link_url",
            "delivered_at",
            "read_at",
            "created_at",
        ]
        # Notifications are produced by the system. Clients only mark them read, via a
        # dedicated action — not by PATCHing arbitrary fields.
        read_only_fields = fields
