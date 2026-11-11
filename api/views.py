"""Public REST API, version 1.

Every endpoint is authenticated (session or API key), tenant-scoped, role-gated, throttled
and paginated. Nothing here reaches the unscoped manager: the base classes in
:mod:`api.mixins` filter by the caller's active organization in ``get_queryset``, which
covers list *and* detail routes.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from billing.models import QuotaExceeded
from billing.services import check_quota
from integrations.models import WebhookDelivery, WebhookEndpoint
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
from kernel.roles import Permission
from kernel.services import create_api_key, invite_member, record_audit, revoke_api_key
from kernel.tenancy import get_tenant
from queues.models import DeadLetter, Job, Queue
from queues.services import enqueue_job, get_webhook_queue
from queues.services import redrive as redrive_dead_letter
from schedules.models import Schedule, ScheduleOccurrence
from schedules.services import bootstrap_next_run, preview_next_runs
from tasks.models import Column, Comment, Label, ProjectArea, RecurrenceRule, Reminder, Task, TaskList
from tasks.services import (
    ConcurrentModification,
    DependencyBlocked,
    WipLimitExceeded,
    move_task,
    schedule_reminder,
    toggle_task_completion,
)

from .exceptions import ConflictError
from .mixins import OrganizationScopedViewSet, ReadOnlyOrganizationScopedViewSet, TenantMixin
from .pagination import TimelineCursorPagination
from .permissions import HasActiveOrganization, HasRolePermission
from .serializers import (
    ActivitySerializer,
    ApiKeySerializer,
    AuditLogSerializer,
    ColumnSerializer,
    CommentSerializer,
    DeadLetterSerializer,
    InvitationSerializer,
    JobSerializer,
    LabelSerializer,
    MembershipSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
    OrganizationSerializer,
    ProjectAreaSerializer,
    QueueSerializer,
    RecurrenceRuleSerializer,
    ReminderSerializer,
    SavedViewSerializer,
    ScheduleOccurrenceSerializer,
    ScheduleSerializer,
    TaskListSerializer,
    TaskMoveSerializer,
    TaskSerializer,
    WebhookDeliverySerializer,
    WebhookEndpointSerializer,
)
from .throttling import BurstRateThrottle, ScopedUserRateThrottle, WriteRateThrottle

# --------------------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------------------


class OrganizationViewSet(TenantMixin, viewsets.ReadOnlyModelViewSet):
    """The caller's own workspaces. Creation and deletion happen in the web app."""

    serializer_class = OrganizationSerializer
    permission_classes = [HasActiveOrganization]
    throttle_classes = [ScopedUserRateThrottle, BurstRateThrottle]
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        if getattr(self.request.user, "pk", None):
            return Organization.objects.for_user(self.request.user).distinct().order_by("name")
        # API-key callers are pinned to the issuing workspace.
        return Organization.objects.filter(pk=self.organization.pk)

    @action(detail=False, methods=["get"], url_path="current")
    def current(self, request):
        return Response(self.get_serializer(self.organization).data)


class MembershipViewSet(TenantMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = MembershipSerializer
    permission_classes = [HasActiveOrganization, HasRolePermission]
    throttle_classes = [ScopedUserRateThrottle, BurstRateThrottle]
    required_permissions = {"read": Permission.TASK_VIEW}
    search_fields = ["user__username", "user__email"]
    ordering_fields = ["role", "joined_at"]

    def get_queryset(self):
        return Membership.objects.filter(organization=self.organization).select_related("user")


class InvitationViewSet(OrganizationScopedViewSet):
    serializer_class = InvitationSerializer
    base_queryset = Invitation.objects.all()
    required_permissions = {"read": Permission.MEMBER_INVITE, "write": Permission.MEMBER_INVITE}
    http_method_names = ["get", "post", "delete", "head", "options"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation, _token = invite_member(
            organization=self.organization,
            actor=self.acting_user,
            actor_role=self.tenant.role,
            email=serializer.validated_data["email"],
            role=serializer.validated_data["role"],
            request=request,
        )
        # The token is intentionally absent from the API response: it is delivered to the
        # invitee by email so that read access to the API cannot grant workspace access.
        return Response(self.get_serializer(invitation).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        instance.status = Invitation.Status.REVOKED
        instance.save(update_fields=["status", "updated_at"])


class ApiKeyViewSet(OrganizationScopedViewSet):
    serializer_class = ApiKeySerializer
    base_queryset = ApiKey.objects.all()
    required_permissions = {"read": Permission.API_KEY_MANAGE, "write": Permission.API_KEY_MANAGE}
    http_method_names = ["get", "post", "delete", "head", "options"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        api_key, token = create_api_key(
            organization=self.organization,
            actor=self.acting_user,
            actor_role=self.tenant.role,
            name=serializer.validated_data["name"],
            role=serializer.validated_data["role"],
            expires_at=serializer.validated_data.get("expires_at"),
            request=request,
        )
        payload = self.get_serializer(api_key).data
        # The only moment the plaintext exists outside the client's hands.
        payload["token"] = token
        return Response(payload, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        revoke_api_key(
            organization=self.organization,
            actor=self.acting_user,
            actor_role=self.tenant.role,
            api_key=instance,
            request=self.request,
        )


class SavedViewViewSet(OrganizationScopedViewSet):
    serializer_class = SavedViewSerializer
    base_queryset = SavedView.objects.all()
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.TASK_VIEW}

    def get_queryset(self):
        # A key-authenticated caller has no personal views, only the workspace's shared ones.
        owner = self.request.user if getattr(self.request.user, "pk", None) else None
        if owner is None:
            return super().get_queryset().filter(is_shared=True)
        return super().get_queryset().filter(Q(owner=owner) | Q(is_shared=True))

    def perform_create(self, serializer):
        serializer.save(organization=self.organization, owner=self.acting_user)


class NotificationPreferenceViewSet(TenantMixin, viewsets.GenericViewSet):
    """A singleton resource: the caller's own preferences in the active workspace."""

    serializer_class = NotificationPreferenceSerializer
    permission_classes = [HasActiveOrganization]
    throttle_classes = [ScopedUserRateThrottle, WriteRateThrottle]

    def _instance(self):
        prefs, _ = NotificationPreference.objects.get_or_create(
            organization=self.organization, user=self.require_personal_identity()
        )
        return prefs

    def list(self, request):
        return Response(self.get_serializer(self._instance()).data)

    @action(detail=False, methods=["put", "patch"], url_path="update")
    def update_preferences(self, request):
        serializer = self.get_serializer(self._instance(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# --------------------------------------------------------------------------------------
# Task structure
# --------------------------------------------------------------------------------------


class ProjectAreaViewSet(OrganizationScopedViewSet):
    serializer_class = ProjectAreaSerializer
    base_queryset = ProjectArea.objects.all()
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.PROJECT_MANAGE}
    search_fields = ["name"]
    ordering_fields = ["name", "position", "created_at"]


class TaskListViewSet(OrganizationScopedViewSet):
    serializer_class = TaskListSerializer
    base_queryset = TaskList.objects.all()
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.LIST_MANAGE}
    search_fields = ["name"]
    ordering_fields = ["name", "position", "created_at"]

    def perform_create(self, serializer):
        task_list = serializer.save(organization=self.organization)
        # A list with no columns renders an empty board, so never ship one.
        task_list.ensure_default_columns()


class ColumnViewSet(OrganizationScopedViewSet):
    serializer_class = ColumnSerializer
    base_queryset = Column.objects.select_related("task_list")
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.LIST_MANAGE}
    ordering_fields = ["position", "name"]


class LabelViewSet(OrganizationScopedViewSet):
    serializer_class = LabelSerializer
    base_queryset = Label.objects.all()
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.LABEL_MANAGE}
    search_fields = ["name"]
    ordering_fields = ["name"]


class TaskViewSet(OrganizationScopedViewSet):
    serializer_class = TaskSerializer
    base_queryset = Task.objects.select_related(
        "task_list", "column", "assignee", "creator"
    ).prefetch_related("labels", "subtasks")
    required_permissions = {
        "read": Permission.TASK_VIEW,
        "create": Permission.TASK_CREATE,
        "update": Permission.TASK_EDIT,
        "delete": Permission.TASK_DELETE,
    }
    search_fields = ["title", "notes"]
    ordering_fields = ["due_date", "priority", "created_at", "updated_at", "title", "position"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params

        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        elif params.get("include_archived") != "true":
            queryset = queryset.filter(archived_at__isnull=True)

        for param, field in (
            ("task_list", "task_list_id"),
            ("column", "column_id"),
            ("assignee", "assignee_id"),
            ("priority", "priority"),
            ("label", "labels__id"),
        ):
            value = params.get(param)
            if value and str(value).isdigit():
                queryset = queryset.filter(**{field: int(value)})

        if params.get("due_before"):
            queryset = queryset.filter(due_date__lte=params["due_before"])
        if params.get("due_after"):
            queryset = queryset.filter(due_date__gte=params["due_after"])
        if params.get("overdue") == "true":
            queryset = queryset.filter(due_date__lt=timezone.localdate(), status=Task.Status.OPEN)

        # No DISTINCT: filtering on one label id cannot duplicate a task (see web.filters).
        return queryset

    def perform_create(self, serializer):
        from tasks.services import next_position, record_activity

        task = serializer.save(organization=self.organization, creator=self.acting_user)
        if not task.position:
            task.position = next_position(task_list=task.task_list, column=task.column)
            task.save(update_fields=["position"])
        record_activity(
            organization=self.organization,
            actor=self.acting_user,
            verb="task.created",
            target_type="task",
            target_id=task.pk,
            summary=f"Created task: {task.title}",
            metadata={"via": "api"},
        )
        if task.due_date:
            schedule_reminder(task)

    def perform_update(self, serializer):
        instance = serializer.instance
        expected = self.request.data.get("expected_version")
        if expected is not None and str(expected).isdigit() and int(expected) != instance.version:
            raise ConflictError(
                f"This task is at version {instance.version}; you sent {expected}. Re-fetch and retry."
            )
        serializer.save(version=instance.version + 1)

    def perform_destroy(self, instance):
        # Tasks are archived, never hard-deleted: comments, attachments and activity all
        # reference them, and a customer deleting a task by accident must be recoverable.
        from tasks.services import archive_task

        archive_task(task=instance, actor=self.acting_user)

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        task = self.get_object()
        try:
            task = toggle_task_completion(
                task=task, actor=self.acting_user, expected_version=request.data.get("expected_version")
            )
        except (ConcurrentModification, DependencyBlocked) as exc:
            raise ConflictError(str(exc)) from exc
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        task = self.get_object()
        serializer = TaskMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        column = None
        if data.get("column"):
            column = self.tenant.scope(Column.objects.all()).filter(pk=data["column"]).first()
            if column is None:
                return Response(
                    {"error": {"code": "not_found", "message": "Column not found."}},
                    status=status.HTTP_404_NOT_FOUND,
                )
        try:
            task = move_task(
                task=task,
                actor=self.acting_user,
                column=column,
                before_id=data.get("before_id"),
                after_id=data.get("after_id"),
                expected_version=data.get("expected_version"),
            )
        except (ConcurrentModification, WipLimitExceeded) as exc:
            raise ConflictError(str(exc)) from exc
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        task = self.get_object()
        if request.method == "POST":
            self.tenant.require(Permission.COMMENT_CREATE)
            serializer = CommentSerializer(
                data={**request.data, "task": task.pk}, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)
            comment = serializer.save(organization=self.organization, author=self.acting_user, task=task)
            from tasks.notifications import notify_comment

            notify_comment(comment=comment)
            return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        return Response(CommentSerializer(task.comments.select_related("author"), many=True).data)


class CommentViewSet(OrganizationScopedViewSet):
    serializer_class = CommentSerializer
    base_queryset = Comment.objects.select_related("author", "task")
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.COMMENT_CREATE}
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        serializer.save(organization=self.organization, author=self.acting_user)


class ReminderViewSet(OrganizationScopedViewSet):
    serializer_class = ReminderSerializer
    base_queryset = Reminder.objects.select_related("task", "job")
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.TASK_EDIT}
    ordering_fields = ["remind_at"]


class RecurrenceRuleViewSet(OrganizationScopedViewSet):
    serializer_class = RecurrenceRuleSerializer
    base_queryset = RecurrenceRule.objects.select_related("source_task")
    required_permissions = {"read": Permission.TASK_VIEW, "write": Permission.TASK_EDIT}
    ordering_fields = ["next_occurrence_date"]


# --------------------------------------------------------------------------------------
# Automation
# --------------------------------------------------------------------------------------


class QueueViewSet(OrganizationScopedViewSet):
    serializer_class = QueueSerializer
    base_queryset = Queue.objects.all()
    required_permissions = {"read": Permission.AUTOMATION_VIEW, "write": Permission.AUTOMATION_MANAGE}
    search_fields = ["name"]
    ordering_fields = ["name"]

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        queue = self.get_object()
        queue.paused = True
        queue.save(update_fields=["paused", "updated_at"])
        return Response(self.get_serializer(queue).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        queue = self.get_object()
        queue.paused = False
        queue.save(update_fields=["paused", "updated_at"])
        return Response(self.get_serializer(queue).data)


class JobViewSet(ReadOnlyOrganizationScopedViewSet):
    """Read-only by design: jobs are produced by domain services, not by API clients."""

    serializer_class = JobSerializer
    base_queryset = Job.objects.select_related("queue").prefetch_related("attempts")
    required_permissions = {"read": Permission.AUTOMATION_VIEW}
    pagination_class = TimelineCursorPagination
    search_fields = ["handler", "source", "correlation_id"]
    ordering_fields = ["created_at", "available_at", "status"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("queue") and params["queue"].isdigit():
            queryset = queryset.filter(queue_id=int(params["queue"]))
        if params.get("handler"):
            queryset = queryset.filter(handler=params["handler"])
        return queryset

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        self.tenant.require(Permission.AUTOMATION_MANAGE)
        from queues.services import cancel_job

        job = self.get_object()
        if not cancel_job(job, reason="Cancelled via API."):
            raise ConflictError("That job has already started or finished.")
        job.refresh_from_db()
        return Response(self.get_serializer(job).data)


class DeadLetterViewSet(ReadOnlyOrganizationScopedViewSet):
    serializer_class = DeadLetterSerializer
    base_queryset = DeadLetter.objects.select_related("job")
    required_permissions = {"read": Permission.AUTOMATION_VIEW}
    pagination_class = TimelineCursorPagination

    @action(detail=True, methods=["post"])
    def redrive(self, request, pk=None):
        self.tenant.require(Permission.JOB_REDRIVE)
        job = redrive_dead_letter(self.get_object(), actor=self.acting_user)
        return Response(JobSerializer(job).data)


class ScheduleViewSet(OrganizationScopedViewSet):
    serializer_class = ScheduleSerializer
    base_queryset = Schedule.objects.select_related("queue")
    required_permissions = {"read": Permission.AUTOMATION_VIEW, "write": Permission.AUTOMATION_MANAGE}
    search_fields = ["name", "expression"]
    ordering_fields = ["name", "next_run_at"]

    def perform_create(self, serializer):
        bootstrap_next_run(serializer.save(organization=self.organization))

    def perform_update(self, serializer):
        bootstrap_next_run(serializer.save())

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        schedule = self.get_object()
        try:
            count = max(1, min(20, int(request.query_params.get("count", 5))))
        except (TypeError, ValueError):
            count = 5
        runs = preview_next_runs(schedule, count)
        return Response({"schedule": schedule.pk, "runs": [run.isoformat() for run in runs]})


class ScheduleOccurrenceViewSet(ReadOnlyOrganizationScopedViewSet):
    serializer_class = ScheduleOccurrenceSerializer
    base_queryset = ScheduleOccurrence.objects.select_related("schedule", "job")
    required_permissions = {"read": Permission.AUTOMATION_VIEW}
    pagination_class = TimelineCursorPagination
    ordering_fields = ["scheduled_time"]


# --------------------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------------------


class ActivityViewSet(ReadOnlyOrganizationScopedViewSet):
    serializer_class = ActivitySerializer
    base_queryset = Activity.objects.select_related("actor")
    required_permissions = {"read": Permission.TASK_VIEW}
    pagination_class = TimelineCursorPagination
    search_fields = ["summary", "verb"]


class AuditLogViewSet(ReadOnlyOrganizationScopedViewSet):
    serializer_class = AuditLogSerializer
    base_queryset = AuditLog.objects.select_related("actor")
    required_permissions = {"read": Permission.AUDIT_VIEW}
    pagination_class = TimelineCursorPagination
    search_fields = ["action", "actor_label"]


class NotificationViewSet(TenantMixin, viewsets.ReadOnlyModelViewSet):
    """Only ever the caller's own notifications, never the whole workspace's."""

    serializer_class = NotificationSerializer
    permission_classes = [HasActiveOrganization]
    throttle_classes = [ScopedUserRateThrottle, BurstRateThrottle]
    pagination_class = TimelineCursorPagination

    def get_queryset(self):
        queryset = Notification.objects.filter(
            organization=self.organization, user=self.require_personal_identity()
        )
        if self.request.query_params.get("unread") == "true":
            queryset = queryset.exclude(status=Notification.Status.READ)
        return queryset

    @action(detail=True, methods=["post"], url_path="read")
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.mark_read()
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def mark_all_read(self, request):
        updated = (
            self.get_queryset()
            .filter(status=Notification.Status.DELIVERED)
            .update(status=Notification.Status.READ, read_at=timezone.now())
        )
        return Response({"marked_read": updated})


# --------------------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------------------


class WebhookEndpointViewSet(OrganizationScopedViewSet):
    """Outbound webhook endpoints. Secret is returned once on create, never again."""

    serializer_class = WebhookEndpointSerializer
    base_queryset = WebhookEndpoint.objects.all()
    required_permissions = {
        "read": Permission.WEBHOOK_MANAGE,
        "write": Permission.WEBHOOK_MANAGE,
    }
    search_fields = ["name", "url"]
    ordering_fields = ["name", "created_at", "last_success_at"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            check_quota(self.organization, "webhooks")
        except QuotaExceeded as exc:
            return Response(
                {"error": {"code": "quota_exceeded", "message": str(exc)}},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        secret = WebhookEndpoint.generate_secret()
        endpoint = serializer.save(
            organization=self.organization,
            created_by=self.acting_user,
            secret=secret,
        )
        record_audit(
            action="webhook.created",
            actor=self.acting_user,
            organization=self.organization,
            target_type="webhook",
            target_id=str(endpoint.pk),
            metadata={"name": endpoint.name, "url": endpoint.url},
            request=request,
        )
        payload = self.get_serializer(endpoint).data
        payload["secret"] = secret
        return Response(payload, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        record_audit(
            action="webhook.deleted",
            actor=self.acting_user,
            organization=self.organization,
            target_type="webhook",
            target_id=str(instance.pk),
            metadata={"name": instance.name},
            request=self.request,
        )
        instance.delete()

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        endpoint = self.get_object()
        endpoint.active = not endpoint.active
        if endpoint.active:
            endpoint.disabled_at = None
            endpoint.consecutive_failures = 0
        endpoint.save(update_fields=["active", "disabled_at", "consecutive_failures", "updated_at"])
        return Response(self.get_serializer(endpoint).data)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        endpoint = self.get_object()
        stamp = timezone.now().isoformat()
        enqueue_job(
            organization=self.organization,
            queue=get_webhook_queue(self.organization),
            source=f"webhook:{endpoint.pk}",
            handler_type=Job.HandlerType.HTTP_DELIVERY,
            handler="http_delivery",
            payload={
                "endpoint_id": endpoint.pk,
                "event": "ping",
                "body": {"id": f"evt_test_{stamp}", "event": "ping", "data": {"test": True}},
            },
            idempotency_key=f"webhook:{endpoint.pk}:test:{stamp}"[:180],
        )
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


class WebhookDeliveryViewSet(ReadOnlyOrganizationScopedViewSet):
    serializer_class = WebhookDeliverySerializer
    base_queryset = WebhookDelivery.objects.select_related("endpoint")
    required_permissions = {"read": Permission.WEBHOOK_MANAGE}
    pagination_class = TimelineCursorPagination
    ordering_fields = ["created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get("endpoint") and str(params["endpoint"]).isdigit():
            queryset = queryset.filter(endpoint_id=int(params["endpoint"]))
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("event"):
            queryset = queryset.filter(event=params["event"])
        return queryset


@api_view(["GET"])
@permission_classes([AllowAny])
def api_root(request):
    """Discovery document. Public on purpose -- it exposes shape, never data."""

    from django.urls import reverse

    return Response(
        {
            "version": "v1",
            "documentation": request.build_absolute_uri(reverse("api_schema")),
            "authentication": {
                "session": "Django session cookie (browser).",
                "api_key": "Authorization: Api-Key <token>",
            },
            "organization_header": "X-Organization: <id or slug> selects the active workspace.",
        }
    )


@api_view(["GET"])
def api_stats(request):
    """Lightweight workspace counters for dashboards and integration health checks."""

    tenant = get_tenant(request)
    if tenant is None:
        return Response(
            {"error": {"code": "no_workspace", "message": "No active workspace."}},
            status=status.HTTP_403_FORBIDDEN,
        )

    organization = tenant.organization
    tasks = Task.objects.filter(organization=organization, archived_at__isnull=True).aggregate(
        open=Count("id", filter=Q(status=Task.Status.OPEN)),
        completed=Count("id", filter=Q(status=Task.Status.COMPLETED)),
        overdue=Count("id", filter=Q(status=Task.Status.OPEN, due_date__lt=timezone.localdate())),
    )
    jobs = Job.objects.filter(organization=organization).aggregate(
        waiting=Count("id", filter=Q(status__in=Job.WAITING_STATUSES)),
        leased=Count("id", filter=Q(status=Job.Status.LEASED)),
        dead=Count("id", filter=Q(status=Job.Status.DEAD)),
    )
    return Response(
        {
            "organization": {"id": organization.pk, "slug": organization.slug, "name": organization.name},
            "tasks": tasks,
            "jobs": jobs,
            "members": Membership.objects.filter(
                organization=organization, status=Membership.Status.ACTIVE
            ).count(),
        }
    )



