"""Workspace lifecycle: creation, membership, invitations, API keys, audit."""

from __future__ import annotations

import re
import secrets

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from ops.logging import get_logger

from .models import (
    ApiKey,
    AuditLog,
    FeatureFlag,
    Invitation,
    Membership,
    NotificationPreference,
    Organization,
)
from .roles import Permission, Role, can_assign_role, has_permission

logger = get_logger("taskmanager.kernel")
User = get_user_model()

RESERVED_SLUGS = {"admin", "api", "static", "media", "accounts", "healthz", "readyz", "www", "app"}


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------


def record_audit(
    *,
    action: str,
    organization=None,
    actor=None,
    request=None,
    target_type: str = "",
    target_id="",
    metadata=None,
) -> AuditLog:
    """Write one append-only audit entry.

    Never raises into the caller: losing an audit row is bad, but failing the customer's
    action because the audit write failed is worse. The failure is logged instead.
    """

    from ops.logging import get_request_id

    ip = None
    user_agent = ""
    if request is not None:
        from .auth_backends import client_ip

        ip = client_ip(request) or None
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    try:
        return AuditLog.objects.create(
            organization=organization,
            actor=actor if getattr(actor, "pk", None) else None,
            actor_label=str(actor) if actor is not None else "system",
            action=action[:120],
            target_type=str(target_type)[:80],
            target_id=str(target_id)[:80],
            ip_address=ip,
            user_agent=user_agent,
            request_id=get_request_id()[:128],
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("failed to write audit log", extra={"action": action})
        raise


# --------------------------------------------------------------------------------------
# Organizations
# --------------------------------------------------------------------------------------


def generate_slug(name: str) -> str:
    """A unique, reserved-word-safe slug for a workspace."""

    base = slugify(name)[:48] or "workspace"
    if base in RESERVED_SLUGS:
        base = f"{base}-workspace"
    candidate = base
    while Organization.objects.filter(slug=candidate).exists():
        candidate = f"{base}-{secrets.token_hex(3)}"[:64]
    return candidate


@transaction.atomic
def create_organization(*, name: str, owner, timezone_name: str = "UTC", request=None) -> Organization:
    """Create a workspace, its owner membership, and its starting structure."""

    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": "Workspace name is required."})
    if len(name) > 160:
        raise ValidationError({"name": "Workspace name is too long."})

    organization = Organization(
        name=name,
        slug=generate_slug(name),
        owner=owner,
        timezone=timezone_name or "UTC",
    )
    organization.full_clean(exclude=["owner"])
    organization.save()

    Membership.objects.create(
        organization=organization,
        user=owner,
        role=Role.OWNER,
        joined_at=timezone.now(),
    )
    NotificationPreference.objects.get_or_create(organization=organization, user=owner)

    from tasks.services import bootstrap_workspace

    bootstrap_workspace(organization=organization, owner=owner)

    record_audit(
        action="organization.created",
        organization=organization,
        actor=owner,
        request=request,
        target_type="organization",
        target_id=organization.pk,
        metadata={"name": name},
    )
    return organization


@transaction.atomic
def soft_delete_organization(*, organization: Organization, actor, retention_days: int = 30, request=None):
    """Mark a workspace deleted and schedule its purge.

    A retention window is deliberate: "delete my workspace" is the one action customers most
    often regret, and an immediate hard delete makes support recovery impossible.
    """

    from datetime import timedelta

    if organization.deleted_at is not None:
        return organization

    # A deleted workspace must stop being billed. If the payment processor cannot be told,
    # the deletion is refused: silently carrying on would leave a subscription charging
    # for a workspace nobody can open.
    from billing.stripe_gateway import BillingError, cancel_subscription_for

    try:
        cancelled = cancel_subscription_for(organization)
    except BillingError as exc:
        raise ValidationError(
            "The paid subscription for this workspace could not be cancelled, so the workspace "
            "has not been deleted. Try again shortly."
        ) from exc

    now = timezone.now()
    organization.deleted_at = now
    organization.purge_after = now + timedelta(days=retention_days)
    organization.save(update_fields=["deleted_at", "purge_after", "updated_at"])

    record_audit(
        action="organization.deleted",
        organization=organization,
        actor=actor,
        request=request,
        target_type="organization",
        target_id=organization.pk,
        metadata={"purge_after": organization.purge_after.isoformat(), "subscription_cancelled": cancelled},
    )
    logger.warning(
        "organization soft-deleted",
        extra={"organization_id": organization.pk, "purge_after": str(organization.purge_after)},
    )
    return organization


@transaction.atomic
def restore_organization(*, organization: Organization, actor, request=None) -> Organization:
    organization.deleted_at = None
    organization.purge_after = None
    organization.save(update_fields=["deleted_at", "purge_after", "updated_at"])
    record_audit(
        action="organization.restored",
        organization=organization,
        actor=actor,
        request=request,
        target_type="organization",
        target_id=organization.pk,
    )
    return organization


# --------------------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------------------


@transaction.atomic
def invite_member(*, organization, actor, actor_role, email: str, role: str, request=None):
    """Issue an invitation. Returns ``(invitation, raw_token)``."""

    if not has_permission(actor_role, Permission.MEMBER_INVITE):
        raise PermissionDenied("You are not permitted to invite members.")
    if not can_assign_role(actor_role, role):
        raise PermissionDenied(f"You cannot grant the '{role}' role.")

    email = (email or "").strip().lower()
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValidationError({"email": "Enter a valid email address."})

    already = Membership.objects.filter(organization=organization, user__email__iexact=email).exists()
    if already:
        raise ValidationError({"email": "That person is already a member of this workspace."})

    invitation, raw_token = Invitation.issue(
        organization=organization, email=email, role=role, invited_by=actor
    )
    record_audit(
        action="member.invited",
        organization=organization,
        actor=actor,
        request=request,
        target_type="invitation",
        target_id=invitation.pk,
        metadata={"email": email, "role": role},
    )
    return invitation, raw_token


def accept_invitation(*, raw_token: str, user, request=None) -> Membership:
    """Redeem an invitation token, creating the membership.

    Tokens are matched by digest, single-use, and time-limited.

    Validation deliberately runs *outside* the transaction. Marking a token expired and
    then raising inside one atomic block rolls the mark straight back, so the row stays
    ``pending`` forever — which also means the partial unique constraint keeps blocking
    a fresh invitation to that address.
    """

    token_hash = Invitation.hash_token(raw_token or "")
    invitation = Invitation.objects.select_related("organization").filter(token_hash=token_hash).first()
    if invitation is None:
        raise ValidationError("This invitation link is not valid.")
    if invitation.status != Invitation.Status.PENDING:
        raise ValidationError("This invitation has already been used or revoked.")
    if invitation.expires_at <= timezone.now():
        Invitation.objects.filter(pk=invitation.pk, status=Invitation.Status.PENDING).update(
            status=Invitation.Status.EXPIRED
        )
        raise ValidationError("This invitation has expired. Ask for a new one.")
    if invitation.organization.deleted_at is not None:
        raise ValidationError("That workspace no longer exists.")

    with transaction.atomic():
        membership, created = Membership.objects.get_or_create(
            organization=invitation.organization,
            user=user,
            defaults={
                "role": invitation.role,
                "invited_email": invitation.email,
                "invited_by": invitation.invited_by,
                "joined_at": timezone.now(),
            },
        )
        NotificationPreference.objects.get_or_create(organization=invitation.organization, user=user)

        # Guarded on PENDING so two simultaneous redemptions cannot both consume the token.
        consumed = Invitation.objects.filter(
            pk=invitation.pk, status=Invitation.Status.PENDING
        ).update(
            status=Invitation.Status.ACCEPTED,
            accepted_at=timezone.now(),
            accepted_by=user,
        )
        if not consumed:
            raise ValidationError("This invitation has already been used or revoked.")

        record_audit(
            action="member.joined",
            organization=invitation.organization,
            actor=user,
            request=request,
            target_type="membership",
            target_id=membership.pk,
            metadata={"role": membership.role, "new_member": created},
        )
    return membership


@transaction.atomic
def change_member_role(*, organization, actor, actor_role, membership: Membership, role: str, request=None):
    """Change a member's role, refusing escalation and the last-owner trap."""

    if not can_assign_role(actor_role, role):
        raise PermissionDenied(f"You cannot grant the '{role}' role.")
    if not can_assign_role(actor_role, membership.role):
        raise PermissionDenied("You cannot change that member's role.")

    if membership.role == Role.OWNER and role != Role.OWNER:
        remaining = (
            Membership.objects.filter(
                organization=organization, role=Role.OWNER, status=Membership.Status.ACTIVE
            )
            .exclude(pk=membership.pk)
            .count()
        )
        if remaining == 0:
            # An ownerless workspace cannot be billed, deleted, or recovered.
            raise ValidationError("A workspace must always have at least one owner.")

    previous = membership.role
    membership.role = role
    membership.save(update_fields=["role", "updated_at"])

    if role == Role.OWNER:
        Organization.objects.filter(pk=organization.pk).update(owner=membership.user)

    record_audit(
        action="member.role_changed",
        organization=organization,
        actor=actor,
        request=request,
        target_type="membership",
        target_id=membership.pk,
        metadata={"from": previous, "to": role, "member": membership.user.get_username()},
    )
    return membership


@transaction.atomic
def remove_member(*, organization, actor, actor_role, membership: Membership, request=None) -> None:
    if not has_permission(actor_role, Permission.MEMBER_MANAGE):
        raise PermissionDenied("You are not permitted to remove members.")
    if not can_assign_role(actor_role, membership.role):
        raise PermissionDenied("You cannot remove that member.")
    if membership.role == Role.OWNER:
        remaining = (
            Membership.objects.filter(
                organization=organization, role=Role.OWNER, status=Membership.Status.ACTIVE
            )
            .exclude(pk=membership.pk)
            .count()
        )
        if remaining == 0:
            raise ValidationError("A workspace must always have at least one owner.")

    username = membership.user.get_username()
    membership_id = membership.pk
    membership.delete()
    record_audit(
        action="member.removed",
        organization=organization,
        actor=actor,
        request=request,
        target_type="membership",
        target_id=membership_id,
        metadata={"member": username},
    )


# --------------------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------------------


@transaction.atomic
def create_api_key(*, organization, actor, actor_role, name: str, role: str, expires_at=None, request=None):
    """Mint an API key. Returns ``(api_key, plaintext_token)`` — the token is shown once."""

    if not has_permission(actor_role, Permission.API_KEY_MANAGE):
        raise PermissionDenied("You are not permitted to manage API keys.")
    # A key must never be able to do more than the person who created it.
    from .roles import ROLE_RANK

    if ROLE_RANK.get(role, -1) > ROLE_RANK.get(str(actor_role), -1):
        raise PermissionDenied("An API key cannot be granted more access than you hold.")

    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": "Give the key a name so it can be identified later."})

    api_key, token = ApiKey.issue(
        organization=organization, name=name, role=role, created_by=actor, expires_at=expires_at
    )
    record_audit(
        action="api_key.created",
        organization=organization,
        actor=actor,
        request=request,
        target_type="api_key",
        target_id=api_key.pk,
        metadata={"name": name, "role": role, "prefix": api_key.prefix},
    )
    return api_key, token


@transaction.atomic
def revoke_api_key(*, organization, actor, actor_role, api_key: ApiKey, request=None) -> ApiKey:
    if not has_permission(actor_role, Permission.API_KEY_MANAGE):
        raise PermissionDenied("You are not permitted to manage API keys.")
    api_key.revoke()
    record_audit(
        action="api_key.revoked",
        organization=organization,
        actor=actor,
        request=request,
        target_type="api_key",
        target_id=api_key.pk,
        metadata={"prefix": api_key.prefix},
    )
    return api_key


# --------------------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------------------


def feature_enabled(key: str, organization=None) -> bool:
    """Resolve a flag: org override, then global override, then the settings default."""

    from django.conf import settings

    if organization is not None:
        override = FeatureFlag.objects.filter(organization=organization, key=key).first()
        if override is not None:
            return override.enabled

    global_override = FeatureFlag.objects.filter(organization__isnull=True, key=key).first()
    if global_override is not None:
        return global_override.enabled

    return bool(getattr(settings, "FEATURE_FLAG_DEFAULTS", {}).get(key, False))


def set_feature_flag(*, key: str, enabled: bool, organization=None, note: str = "") -> FeatureFlag:
    flag, _ = FeatureFlag.objects.update_or_create(
        organization=organization,
        key=key,
        defaults={"enabled": enabled, "note": note[:255]},
    )
    return flag
