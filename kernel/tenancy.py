"""Tenant resolution and enforcement.

Every request that touches customer data must resolve exactly one active organization and
carry the caller's role in it. This module is the single place that decision is made; the
rest of the codebase consumes :class:`TenantContext` and never re-derives membership.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet

from .models import Membership, Organization
from .roles import Permission, has_permission, permissions_for

SESSION_ORG_KEY = "active_organization_id"
ORG_HEADER = "HTTP_X_ORGANIZATION"

#: Sentinel distinguishing "not resolved yet" from a resolved-to-nothing result.
_UNRESOLVED = object()


class NoOrganizationError(Exception):
    """Raised when an authenticated user belongs to no live organization."""


@dataclass(frozen=True)
class TenantContext:
    """The resolved tenant for a request: which org, which role, what it may do."""

    organization: Organization
    role: str
    membership: Membership | None = None
    via_api_key: bool = False

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.role)

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)

    def require(self, permission: str) -> None:
        """Raise :class:`PermissionDenied` unless the caller holds ``permission``."""

        if not self.can(permission):
            raise PermissionDenied(
                f"Your role ({self.role}) is not permitted to perform '{permission}'."
            )

    def scope(self, queryset: QuerySet) -> QuerySet:
        """Constrain ``queryset`` to this tenant.

        Works for any model with an ``organization`` FK, and for ``Organization`` itself.
        Anything else is a programming error and raises rather than returning unscoped rows.
        """

        model = queryset.model
        if model is Organization:
            return queryset.filter(pk=self.organization.pk)
        field_names = {field.name for field in model._meta.get_fields()}
        if "organization" in field_names:
            return queryset.filter(organization=self.organization)
        raise TypeError(
            f"{model.__name__} has no organization field; it cannot be tenant-scoped "
            "automatically. Scope it explicitly."
        )


def get_tenant(request) -> TenantContext | None:
    """Resolve — and memoise — the tenant for ``request``, or ``None``.

    This is the accessor every permission check and view uses. It deliberately does *not*
    wrap the result in ``SimpleLazyObject``: a lazy proxy around ``None`` is never ``is
    None`` and never falsy before evaluation, so ``if request.tenant is None`` silently
    passed for anonymous callers and turned an authorization check into a 500.

    The cache lives on the underlying Django request so a DRF ``Request`` and the request
    it wraps share one answer, while resolution reads from ``request`` itself — DRF sets
    the authenticated user and the API-key context on the wrapper first.
    """

    cache_target = getattr(request, "_request", None) or request
    cached = getattr(cache_target, "_tenant_context", _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    try:
        resolved = resolve_tenant(request)
    except NoOrganizationError:
        # Deliberately not cached. Middleware runs *before* DRF authenticates, so a
        # negative answer at that point would be memoised and then returned to the
        # permission check that runs after the API key has been accepted — turning every
        # key-authenticated request into a 403.
        return None

    cache_target._tenant_context = resolved
    if cache_target is not request:
        request._tenant_context = resolved
    return resolved


def clear_tenant_cache(request) -> None:
    """Drop the memoised tenant — used after switching workspaces mid-request."""

    for target in {request, getattr(request, "_request", None)}:
        if target is not None and hasattr(target, "_tenant_context"):
            delattr(target, "_tenant_context")


def user_organizations(user) -> QuerySet[Organization]:
    """Live organizations the user is an active member of, ordered for stable UI."""

    return Organization.objects.for_user(user).distinct().order_by("name")


def resolve_tenant(request, *, organization_id=None) -> TenantContext:
    """Resolve the active tenant for ``request``.

    Precedence: an explicit argument, then the ``X-Organization`` header (API clients),
    then the session's sticky selection, then the user's first membership. A requested
    organization the caller is not a member of is treated as *not found*, never as a
    permission error, so the API does not confirm the existence of other tenants.
    """

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise NoOrganizationError("Request is not authenticated.")

    # An API key pins the tenant; a header or session value cannot widen it.
    key_context = getattr(request, "api_key_context", None)
    if key_context is not None:
        return key_context

    memberships = (
        Membership.objects.select_related("organization")
        .filter(user=user, status=Membership.Status.ACTIVE, organization__deleted_at__isnull=True)
        .order_by("organization__name")
    )

    requested = organization_id
    if requested is None:
        header_value = request.META.get(ORG_HEADER)
        if header_value:
            requested = header_value
    if requested is None:
        requested = (getattr(request, "session", None) or {}).get(SESSION_ORG_KEY)

    membership = None
    if requested is not None:
        membership = _match_membership(memberships, requested)
        if membership is None and organization_id is not None:
            raise NoOrganizationError(f"No accessible organization for {organization_id!r}.")

    if membership is None:
        membership = memberships.first()

    if membership is None:
        raise NoOrganizationError("User has no active organization membership.")

    return TenantContext(
        organization=membership.organization,
        role=membership.role,
        membership=membership,
    )


def _match_membership(memberships, requested):
    """Match a membership by primary key or slug, tolerating string ids from headers."""

    text = str(requested).strip()
    if not text:
        return None
    if text.isdigit():
        return memberships.filter(organization_id=int(text)).first()
    return memberships.filter(organization__slug=text).first()


def set_active_organization(request, organization: Organization) -> None:
    """Persist the tenant selection for subsequent requests in this session."""

    session = getattr(request, "session", None)
    if session is not None:
        session[SESSION_ORG_KEY] = organization.pk


def require_permission(request, permission: str) -> TenantContext:
    """Convenience for function-based views: resolve the tenant and assert a permission."""

    tenant = getattr(request, "tenant", None)
    if tenant is None:
        tenant = resolve_tenant(request)
    tenant.require(permission)
    return tenant


__all__ = [
    "ORG_HEADER",
    "SESSION_ORG_KEY",
    "NoOrganizationError",
    "Permission",
    "TenantContext",
    "clear_tenant_cache",
    "get_tenant",
    "require_permission",
    "resolve_tenant",
    "set_active_organization",
    "user_organizations",
]
