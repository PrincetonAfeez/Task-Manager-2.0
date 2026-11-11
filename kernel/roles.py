"""Role definitions and the authorization matrix.

Authorization is expressed once, here, as data. Views, viewsets, templates and services
all consult :func:`has_permission` rather than re-deriving "is this person an admin?" —
which is how role checks drift apart and become privilege-escalation bugs.
"""

from __future__ import annotations

from django.db import models


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


#: Higher rank implies every permission of every lower rank.
ROLE_RANK: dict[str, int] = {
    Role.VIEWER: 0,
    Role.MEMBER: 10,
    Role.ADMIN: 20,
    Role.OWNER: 30,
}


class Permission:
    """Named capabilities. Strings, not enums, so they serialize into audit metadata."""

    # Tasks and task data
    TASK_VIEW = "task.view"
    TASK_CREATE = "task.create"
    TASK_EDIT = "task.edit"
    TASK_DELETE = "task.delete"
    TASK_ASSIGN = "task.assign"
    COMMENT_CREATE = "comment.create"
    ATTACHMENT_UPLOAD = "attachment.upload"

    # Workspace structure
    LIST_MANAGE = "list.manage"
    LABEL_MANAGE = "label.manage"
    PROJECT_MANAGE = "project.manage"
    SAVED_VIEW_SHARE = "saved_view.share"

    # Automation
    AUTOMATION_VIEW = "automation.view"
    AUTOMATION_MANAGE = "automation.manage"
    JOB_REDRIVE = "job.redrive"

    # Administration
    MEMBER_INVITE = "member.invite"
    MEMBER_MANAGE = "member.manage"
    ORG_SETTINGS = "org.settings"
    ORG_DELETE = "org.delete"
    BILLING_MANAGE = "billing.manage"
    AUDIT_VIEW = "audit.view"
    API_KEY_MANAGE = "api_key.manage"
    WEBHOOK_MANAGE = "webhook.manage"
    DATA_EXPORT = "data.export"
    FEATURE_FLAG_MANAGE = "feature_flag.manage"


#: Minimum role rank required for each permission.
_REQUIRED_RANK: dict[str, int] = {
    Permission.TASK_VIEW: ROLE_RANK[Role.VIEWER],
    Permission.AUTOMATION_VIEW: ROLE_RANK[Role.VIEWER],
    Permission.TASK_CREATE: ROLE_RANK[Role.MEMBER],
    Permission.TASK_EDIT: ROLE_RANK[Role.MEMBER],
    Permission.TASK_DELETE: ROLE_RANK[Role.MEMBER],
    Permission.TASK_ASSIGN: ROLE_RANK[Role.MEMBER],
    Permission.COMMENT_CREATE: ROLE_RANK[Role.MEMBER],
    Permission.ATTACHMENT_UPLOAD: ROLE_RANK[Role.MEMBER],
    Permission.LABEL_MANAGE: ROLE_RANK[Role.MEMBER],
    Permission.SAVED_VIEW_SHARE: ROLE_RANK[Role.MEMBER],
    Permission.LIST_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.PROJECT_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.AUTOMATION_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.JOB_REDRIVE: ROLE_RANK[Role.ADMIN],
    Permission.MEMBER_INVITE: ROLE_RANK[Role.ADMIN],
    Permission.MEMBER_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.ORG_SETTINGS: ROLE_RANK[Role.ADMIN],
    Permission.AUDIT_VIEW: ROLE_RANK[Role.ADMIN],
    Permission.API_KEY_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.WEBHOOK_MANAGE: ROLE_RANK[Role.ADMIN],
    Permission.DATA_EXPORT: ROLE_RANK[Role.ADMIN],
    Permission.FEATURE_FLAG_MANAGE: ROLE_RANK[Role.ADMIN],
    # Owner-only: destroying or paying for the workspace.
    Permission.ORG_DELETE: ROLE_RANK[Role.OWNER],
    Permission.BILLING_MANAGE: ROLE_RANK[Role.OWNER],
}

ALL_PERMISSIONS: tuple[str, ...] = tuple(sorted(_REQUIRED_RANK))


def has_permission(role: str | None, permission: str) -> bool:
    """Return whether ``role`` satisfies ``permission``.

    Unknown roles and unknown permissions both deny. Failing closed matters more than
    a helpful error: an unrecognised permission string is a bug, and a bug in an
    authorization check must not grant access.
    """

    if role is None:
        return False
    required = _REQUIRED_RANK.get(permission)
    if required is None:
        return False
    return ROLE_RANK.get(str(role), -1) >= required


def permissions_for(role: str | None) -> frozenset[str]:
    """All permissions granted to ``role`` — used to build UI capability maps."""

    if role is None:
        return frozenset()
    rank = ROLE_RANK.get(str(role), -1)
    return frozenset(name for name, required in _REQUIRED_RANK.items() if rank >= required)


def can_assign_role(actor_role: str | None, target_role: str) -> bool:
    """Prevent horizontal and vertical privilege escalation on role changes.

    An admin may manage members and viewers but may not mint another owner, and may not
    demote or promote a peer admin. Only an owner can create or change owners/admins.
    """

    if not has_permission(actor_role, Permission.MEMBER_MANAGE):
        return False
    actor_rank = ROLE_RANK.get(str(actor_role), -1)
    target_rank = ROLE_RANK.get(str(target_role), -1)
    if target_rank < 0:
        return False
    if actor_rank >= ROLE_RANK[Role.OWNER]:
        return True
    return target_rank < actor_rank
