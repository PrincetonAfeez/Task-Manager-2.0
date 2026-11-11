"""Account lifecycle: personal data export and account deletion.

These are the two flows a customer is entitled to under GDPR-style regimes, and the two the
audit framework checks for. They are separate from *workspace* deletion: a user may leave
without destroying a workspace their colleagues still use.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from kernel.models import Membership, Organization
from kernel.roles import Role
from kernel.services import record_audit, soft_delete_organization
from ops.logging import get_logger

logger = get_logger("taskmanager.support")
User = get_user_model()


def build_personal_export(user) -> dict:
    """Everything the system holds about one person, across every workspace they are in.

    Deliberately excludes other people's content: a colleague's comment is that colleague's
    data, not this user's, and exporting it would leak.
    """

    from kernel.models import Activity, AuditLog, Notification
    from tasks.models import Comment, Task

    memberships = Membership.objects.filter(user=user).select_related("organization")

    return {
        "meta": {"exported_at": timezone.now().isoformat(), "format_version": 1},
        "account": {
            "username": user.get_username(),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "date_joined": user.date_joined.isoformat(),
            "last_login": user.last_login.isoformat() if user.last_login else None,
            # Never include the password hash, even in the subject's own export.
        },
        "workspaces": [
            {
                "name": membership.organization.name,
                "slug": membership.organization.slug,
                "role": membership.role,
                "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
            }
            for membership in memberships
        ],
        "tasks_created": list(
            Task.objects.filter(creator=user).values(
                "id", "title", "status", "due_date", "created_at", "organization__slug"
            )[:10_000]
        ),
        "tasks_assigned": list(
            Task.objects.filter(assignee=user).values(
                "id", "title", "status", "due_date", "organization__slug"
            )[:10_000]
        ),
        "comments": list(
            Comment.objects.filter(author=user).values(
                "id", "task_id", "body", "created_at", "organization__slug"
            )[:10_000]
        ),
        "notifications": list(
            Notification.objects.filter(user=user).values(
                "title", "body", "category", "status", "created_at"
            )[:5_000]
        ),
        "activity": list(
            Activity.objects.filter(actor=user).values("verb", "summary", "created_at")[:5_000]
        ),
        "security_events": list(
            AuditLog.objects.filter(actor=user).values(
                "action", "ip_address", "created_at", "target_type", "target_id"
            )[:5_000]
        ),
    }


def blocking_owned_workspaces(user):
    """Workspaces where this user is the only remaining owner.

    Deleting the account without resolving these would leave a workspace nobody can
    administer, bill, or delete.
    """

    blocking = []
    for membership in Membership.objects.filter(user=user, role=Role.OWNER).select_related(
        "organization"
    ):
        others = (
            Membership.objects.filter(
                organization=membership.organization,
                role=Role.OWNER,
                status=Membership.Status.ACTIVE,
            )
            .exclude(user=user)
            .count()
        )
        if others == 0:
            blocking.append(membership.organization)
    return blocking


@transaction.atomic
def delete_account(*, user, request=None, delete_sole_owned_workspaces: bool = False) -> dict:
    """Delete a user account.

    Content the user authored is *anonymised* rather than removed: deleting a comment
    thread or every task in a shared board would destroy other people's work in the name of
    one person's erasure request. The identifying link is what gets severed.
    """

    from kernel.models import Activity, Notification
    from tasks.models import Attachment, Comment, Task

    blocking = blocking_owned_workspaces(user)
    if blocking and not delete_sole_owned_workspaces:
        raise ValidationError(
            "You are the only owner of "
            + ", ".join(org.name for org in blocking)
            + ". Transfer ownership or confirm that these workspaces should be deleted."
        )

    username = user.get_username()
    user_id = user.pk
    summary = {"workspaces_deleted": 0, "tasks_anonymised": 0, "comments_anonymised": 0}

    for organization in blocking:
        soft_delete_organization(organization=organization, actor=user, request=request)
        summary["workspaces_deleted"] += 1

    placeholder, _ = User.objects.get_or_create(
        username="deleted-user",
        defaults={"is_active": False, "first_name": "Deleted", "last_name": "user"},
    )
    if placeholder.has_usable_password():
        placeholder.set_unusable_password()
        placeholder.save(update_fields=["password"])

    # Task.creator and Comment.author are PROTECT: they must be repointed, not orphaned.
    summary["tasks_anonymised"] = Task.objects.filter(creator=user).update(creator=placeholder)
    summary["comments_anonymised"] = Comment.objects.filter(author=user).update(author=placeholder)
    Task.objects.filter(assignee=user).update(assignee=None)
    Task.objects.filter(completed_by=user).update(completed_by=None)
    Attachment.objects.filter(uploaded_by=user).update(uploaded_by=None)
    Notification.objects.filter(user=user).delete()
    Activity.objects.filter(actor=user).update(actor=None)
    Membership.objects.filter(user=user).delete()
    Organization.objects.filter(owner=user).update(owner=placeholder)

    # Written before the delete so the audit row survives with a usable label.
    record_audit(
        action="account.deleted",
        actor=None,
        request=request,
        target_type="user",
        target_id=user_id,
        metadata={"username": username, **summary},
    )

    user.delete()
    logger.warning("account deleted", extra={"username": username, **summary})
    return summary


@transaction.atomic
def leave_workspace(*, user, organization, request=None) -> None:
    """Remove yourself from a workspace, refusing if you are its last owner."""

    membership = Membership.objects.filter(organization=organization, user=user).first()
    if membership is None:
        raise ValidationError("You are not a member of that workspace.")

    if membership.role == Role.OWNER:
        others = (
            Membership.objects.filter(
                organization=organization, role=Role.OWNER, status=Membership.Status.ACTIVE
            )
            .exclude(user=user)
            .count()
        )
        if others == 0:
            raise ValidationError(
                "You are the only owner. Promote someone else before leaving, or delete the workspace."
            )

    membership.delete()
    record_audit(
        action="member.left",
        organization=organization,
        actor=user,
        request=request,
        target_type="membership",
        target_id=user.pk,
    )
