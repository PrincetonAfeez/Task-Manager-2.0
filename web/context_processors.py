"""Template context shared by every page."""

from __future__ import annotations

from kernel.models import Notification
from kernel.tenancy import get_tenant, user_organizations


def workspace(request):
    """Expose the active workspace, the caller's capabilities, and the unread badge.

    Kept deliberately cheap: two small queries for authenticated users, zero for anyone
    else, and nothing that touches the task tables.
    """

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"active_organization": None, "user_organizations": [], "can": {}, "unread_count": 0}

    tenant = get_tenant(request)
    if tenant is None:
        return {
            "active_organization": None,
            "user_organizations": list(user_organizations(user)),
            "can": {},
            "unread_count": 0,
        }

    unread = Notification.objects.filter(
        organization=tenant.organization, user=user, status=Notification.Status.DELIVERED
    ).count()

    return {
        "active_organization": tenant.organization,
        "active_role": tenant.role,
        "user_organizations": list(user_organizations(user)),
        # A set exposed to templates as ``{% if "task.create" in can %}``. The UI hides
        # what the server would refuse anyway, so the two never disagree.
        "can": tenant.permissions,
        "unread_count": unread,
    }

