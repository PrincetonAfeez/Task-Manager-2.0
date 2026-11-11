from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .schema import openapi_schema
from .views import (
    ActivityViewSet,
    ApiKeyViewSet,
    AuditLogViewSet,
    ColumnViewSet,
    CommentViewSet,
    DeadLetterViewSet,
    InvitationViewSet,
    JobViewSet,
    LabelViewSet,
    MembershipViewSet,
    NotificationPreferenceViewSet,
    NotificationViewSet,
    OrganizationViewSet,
    ProjectAreaViewSet,
    QueueViewSet,
    RecurrenceRuleViewSet,
    ReminderViewSet,
    SavedViewViewSet,
    ScheduleOccurrenceViewSet,
    ScheduleViewSet,
    TaskListViewSet,
    TaskViewSet,
    WebhookDeliveryViewSet,
    WebhookEndpointViewSet,
    api_root,
    api_stats,
)

router = DefaultRouter()
# Workspace
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("members", MembershipViewSet, basename="membership")
router.register("invitations", InvitationViewSet, basename="invitation")
router.register("api-keys", ApiKeyViewSet, basename="apikey")
router.register("saved-views", SavedViewViewSet, basename="savedview")
router.register(
    "notification-preferences", NotificationPreferenceViewSet, basename="notificationpreference"
)
# Tasks
router.register("projects", ProjectAreaViewSet, basename="projectarea")
router.register("lists", TaskListViewSet, basename="tasklist")
router.register("columns", ColumnViewSet, basename="column")
router.register("labels", LabelViewSet, basename="label")
router.register("tasks", TaskViewSet, basename="task")
router.register("comments", CommentViewSet, basename="comment")
router.register("reminders", ReminderViewSet, basename="reminder")
router.register("recurrences", RecurrenceRuleViewSet, basename="recurrencerule")
# Automation
router.register("queues", QueueViewSet, basename="queue")
router.register("jobs", JobViewSet, basename="job")
router.register("dead-letters", DeadLetterViewSet, basename="deadletter")
router.register("schedules", ScheduleViewSet, basename="schedule")
router.register("schedule-occurrences", ScheduleOccurrenceViewSet, basename="scheduleoccurrence")
router.register("webhooks", WebhookEndpointViewSet, basename="webhook")
router.register("webhook-deliveries", WebhookDeliveryViewSet, basename="webhookdelivery")
# Feeds
router.register("activity", ActivityViewSet, basename="activity")
router.register("audit-log", AuditLogViewSet, basename="auditlog")
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = [
    path("", api_root, name="api_root"),
    path("openapi.json", openapi_schema, name="api_schema"),
    path("stats/", api_stats, name="api_stats"),
    path("", include(router.urls)),
]
