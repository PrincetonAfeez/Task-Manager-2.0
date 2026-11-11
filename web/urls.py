from django.urls import path

from . import views, views_extra, views_features, views_legal

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Onboarding
    path("signup/", views.signup, name="signup"),
    path("workspaces/new/", views.workspace_create, name="workspace_create"),
    path("workspaces/<int:organization_id>/switch/", views.workspace_switch, name="workspace_switch"),
    path("invitations/<str:token>/", views.invitation_accept, name="invitation_accept"),
    # Tasks
    path("tasks/", views.task_list_view, name="task_list"),
    path("tasks/new/", views.task_create, name="task_create"),
    path("tasks/<int:task_id>/", views.task_detail, name="task_detail"),
    path("tasks/<int:task_id>/edit/", views.task_edit, name="task_edit"),
    path("tasks/<int:task_id>/archive/", views.task_archive, name="task_archive"),
    path("tasks/<int:task_id>/toggle/", views.toggle_task, name="toggle_task"),
    path("tasks/<int:task_id>/move/", views.board_move, name="board_move"),
    path("tasks/<int:task_id>/comments/", views.comment_create, name="comment_create"),
    path("tasks/<int:task_id>/subtasks/", views.subtask_create, name="subtask_create"),
    path(
        "tasks/<int:task_id>/subtasks/<int:subtask_id>/toggle/",
        views.subtask_toggle,
        name="subtask_toggle",
    ),
    path("tasks/<int:task_id>/attachments/", views.attachment_upload, name="attachment_upload"),
    path("attachments/<int:attachment_id>/", views.attachment_download, name="attachment_download"),
    # Quick add
    path("quick-add/preview/", views.quick_add_preview, name="quick_add_preview"),
    path("quick-add/create/", views.quick_add_create, name="quick_add_create"),
    # Board
    path("board/", views.board, name="board"),
    path("board/<int:list_id>/", views.board, name="board_list"),
    path("board/<int:list_id>/lane/", views.board_lane, name="board_lane"),
    # Structure
    path("lists/", views.list_manage, name="list_manage"),
    path("lists/columns/new/", views.column_create, name="column_create"),
    path("labels/new/", views.label_create, name="label_create"),
    path("projects/new/", views.project_create, name="project_create"),
    # Saved views
    path("views/save/", views.saved_view_create, name="saved_view_create"),
    path("views/<int:view_id>/delete/", views.saved_view_delete, name="saved_view_delete"),
    # Automation
    path("automation/", views.automation, name="automation"),
    path("automation/schedules/new/", views.schedule_create, name="schedule_create"),
    path("schedules/<int:schedule_id>/preview/", views.schedule_preview, name="schedule_preview"),
    path(
        "automation/dead-letters/<int:dead_letter_id>/redrive/",
        views.dead_letter_redrive,
        name="dead_letter_redrive",
    ),
    path("automation/queues/<int:queue_id>/pause/", views.queue_toggle_pause, name="queue_toggle_pause"),
    # Notifications
    path("notifications/", views.notification_list, name="notification_list"),
    path(
        "notifications/<int:notification_id>/read/",
        views.notification_mark_read,
        name="notification_mark_read",
    ),
    path("notifications/read-all/", views.notification_mark_all_read, name="notification_mark_all_read"),
    # Analytics
    path("analytics/", views.analytics, name="analytics"),
    # Settings
    path("settings/workspace/", views.settings_workspace, name="settings_workspace"),
    path("settings/workspace/delete/", views.workspace_delete, name="workspace_delete"),
    path("settings/members/", views.settings_members, name="settings_members"),
    path("settings/members/<int:membership_id>/role/", views.member_role_change, name="member_role_change"),
    path("settings/members/<int:membership_id>/remove/", views.member_remove, name="member_remove"),
    path("settings/notifications/", views.settings_notifications, name="settings_notifications"),
    path("settings/api-keys/", views.settings_api_keys, name="settings_api_keys"),
    path("settings/api-keys/<int:key_id>/revoke/", views.api_key_revoke, name="api_key_revoke"),
    path("settings/audit/", views.settings_audit, name="settings_audit"),
    # Webhooks
    path("settings/webhooks/", views_features.webhook_list, name="webhook_list"),
    path("settings/webhooks/<int:endpoint_id>/", views_features.webhook_detail, name="webhook_detail"),
    path(
        "settings/webhooks/<int:endpoint_id>/toggle/",
        views_features.webhook_toggle,
        name="webhook_toggle",
    ),
    path(
        "settings/webhooks/<int:endpoint_id>/delete/",
        views_features.webhook_delete,
        name="webhook_delete",
    ),
    path("settings/webhooks/<int:endpoint_id>/test/", views_features.webhook_test, name="webhook_test"),
    # Billing
    path("settings/billing/", views_features.billing_overview, name="billing_overview"),
    path("settings/billing/change/", views_features.billing_change_plan, name="billing_change_plan"),
    path("settings/billing/portal/", views_features.billing_portal, name="billing_portal"),
    path("billing/webhooks/stripe/", views_features.stripe_webhook, name="stripe_webhook"),
    # Exports
    path("export/tasks/", views_features.export_tasks, name="export_tasks"),
    path("export/workspace/", views_features.export_workspace, name="export_workspace"),
    # Account lifecycle
    path("account/", views_features.account_settings, name="account_settings"),
    path("account/export/", views_features.account_export, name="account_export"),
    path("account/delete/", views_features.account_delete, name="account_delete"),
    path("settings/workspace/leave/", views_features.workspace_leave, name="workspace_leave"),
    # Time tracking
    path("tasks/<int:task_id>/timer/start/", views_extra.timer_start, name="timer_start"),
    path("tasks/<int:task_id>/time/", views_extra.time_log, name="time_log"),
    path("timer/stop/", views_extra.timer_stop, name="timer_stop"),
    path("time/", views_extra.time_report, name="time_report"),
    # Templates
    path("templates/", views_extra.template_list, name="template_list"),
    path("templates/<int:template_id>/apply/", views_extra.template_apply, name="template_apply"),
    path("templates/<int:template_id>/delete/", views_extra.template_delete, name="template_delete"),
    path(
        "tasks/<int:task_id>/save-template/",
        views_extra.template_from_task_view,
        name="template_from_task",
    ),
    # Bulk operations
    path("tasks/bulk/", views_extra.bulk_action, name="bulk_action"),
    # Calendar
    path("calendar/", views_extra.calendar_view, name="calendar"),
    # Share links
    path("settings/sharing/", views_extra.share_link_manage, name="share_link_manage"),
    path("settings/sharing/<int:link_id>/revoke/", views_extra.share_link_revoke, name="share_link_revoke"),
    path("shared/<str:token>/", views_extra.shared_board, name="shared_board"),
    # Legal. Referenced from the signup form, so the links must resolve.
    path("terms/", views_legal.terms, name="terms"),
    path("privacy/", views_legal.privacy, name="privacy"),
]
