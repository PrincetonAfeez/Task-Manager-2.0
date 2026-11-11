"""Optional PostgreSQL row-level security helpers.

Application-layer tenancy (``TenantContext.scope`` / ``OrganizationScopedViewSet``) remains
the primary control. When ``ENABLE_POSTGRES_RLS`` is on *and* ``enable_rls`` has been run,
policies reject rows whose ``organization_id`` does not match ``app.current_org_id``.

Staff/superuser connections and management commands that need a cross-tenant view must
either disable the setting or ``SET LOCAL app.bypass_rls = '1'``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import connection

# Tables with a non-null ``organization_id`` column. Child rows without that column
# (subtasks, job attempts, list memberships) inherit isolation via their parents.
TENANT_TABLES = (
    "kernel_membership",
    "kernel_invitation",
    "kernel_activity",
    "kernel_notification",
    "kernel_notificationpreference",
    "kernel_savedview",
    "kernel_apikey",
    "tasks_projectarea",
    "tasks_tasklist",
    "tasks_column",
    "tasks_label",
    "tasks_task",
    "tasks_comment",
    "tasks_attachment",
    "tasks_reminder",
    "tasks_recurrencerule",
    "tasks_timeentry",
    "tasks_tasktemplate",
    "tasks_sharelink",
    "queues_queue",
    "queues_job",
    "queues_deadletter",
    "queues_effectmarker",
    "queues_queuemetricsnapshot",
    "schedules_schedule",
    "schedules_scheduleoccurrence",
    "integrations_webhookendpoint",
    "integrations_webhookdelivery",
    "billing_subscription",
    "billing_usagerecord",
    "billing_billingevent",
)


def rls_enabled() -> bool:
    return bool(getattr(settings, "ENABLE_POSTGRES_RLS", False)) and connection.vendor == "postgresql"


def set_current_organization(organization_id: int | None) -> None:
    """Bind the current transaction to a tenant id for RLS policies."""

    if not rls_enabled():
        return
    with connection.cursor() as cursor:
        if organization_id is None:
            cursor.execute("SELECT set_config('app.current_org_id', '', true)")
            cursor.execute("SELECT set_config('app.bypass_rls', '1', true)")
        else:
            cursor.execute("SELECT set_config('app.bypass_rls', '', true)")
            cursor.execute(
                "SELECT set_config('app.current_org_id', %s, true)",
                [str(organization_id)],
            )


def clear_tenant_binding() -> None:
    if not rls_enabled():
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_org_id', '', true)")
        cursor.execute("SELECT set_config('app.bypass_rls', '', true)")
