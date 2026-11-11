"""Workspace sign-in policy: who may open a workspace with which kind of session.

The two requirements are stored on ``Organization.settings_json`` so enforcing them costs
no extra query on a request that has already loaded the organization.
"""

from __future__ import annotations

from django.conf import settings

from kernel.roles import Role

from .mfa import SESSION_VERIFIED_KEY, user_has_mfa

REQUIRE_MFA_KEY = "require_mfa"
SSO_REQUIRED_KEY = "sso_required"
#: Session key: ids of the workspaces this session authenticated to through their IdP.
SESSION_SSO_ORGS_KEY = "sso_orgs"

NEEDS_SSO = "sso"
NEEDS_MFA_SETUP = "mfa_setup"
NEEDS_MFA_VERIFY = "mfa_verify"


def organization_requires_mfa(organization) -> bool:
    return bool((organization.settings_json or {}).get(REQUIRE_MFA_KEY))


def organization_requires_sso(organization) -> bool:
    return bool((organization.settings_json or {}).get(SSO_REQUIRED_KEY))


def set_policy_flag(organization, key: str, value: bool) -> None:
    data = dict(organization.settings_json or {})
    if value:
        data[key] = True
    else:
        data.pop(key, None)
    organization.settings_json = data
    organization.save(update_fields=["settings_json", "updated_at"])


def session_used_sso(request, organization) -> bool:
    session = getattr(request, "session", None)
    return session is not None and organization.pk in (session.get(SESSION_SSO_ORGS_KEY) or [])


def staff_mfa_requirement(request) -> str | None:
    """What a Django staff/superuser session still owes before using the product or admin.

    Controlled by ``STAFF_REQUIRE_MFA`` (on by default in production). API keys are never
    staff sessions. Paths that enrol or verify MFA are exempt so the user can satisfy the
    requirement.
    """

    if not getattr(settings, "STAFF_REQUIRE_MFA", False):
        return None
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if not (user.is_staff or user.is_superuser):
        return None
    session = getattr(request, "session", None)
    if session is not None and session.get(SESSION_VERIFIED_KEY):
        return None
    return NEEDS_MFA_VERIFY if user_has_mfa(user) else NEEDS_MFA_SETUP


def access_requirement(request, tenant) -> str | None:
    """What this session still has to do before it may use ``tenant``'s workspace.

    API keys are exempt: they are workspace credentials issued by an administrator, not a
    person's sign-in, and have their own lifecycle (scoped role, revocation, audit).
    """

    staff_need = staff_mfa_requirement(request)
    if staff_need is not None:
        return staff_need

    if tenant is None or getattr(tenant, "via_api_key", False):
        return None

    organization = tenant.organization
    via_sso = session_used_sso(request, organization)

    if organization_requires_sso(organization) and not via_sso and tenant.role != Role.OWNER:
        return NEEDS_SSO

    # A session that came through the workspace's identity provider has satisfied whatever
    # second factor the provider enforces; asking for a local code as well adds nothing.
    if organization_requires_mfa(organization) and not via_sso:
        session = getattr(request, "session", None)
        if session is None or not session.get(SESSION_VERIFIED_KEY):
            return NEEDS_MFA_VERIFY if user_has_mfa(request.user) else NEEDS_MFA_SETUP
    return None
