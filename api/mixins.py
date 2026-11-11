"""Base viewsets that make tenant isolation structural rather than per-developer discipline.

Every customer-data endpoint inherits :class:`OrganizationScopedViewSet`. It scopes the
queryset by the caller's active organization in ``get_queryset``, which means detail routes
(``/tasks/42/``) are covered by the same rule as list routes — an object belonging to
another tenant is simply not in the queryset and resolves to 404.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from rest_framework import viewsets
from rest_framework.exceptions import NotFound

from kernel.models import Organization
from kernel.tenancy import TenantContext, get_tenant

from .permissions import HasActiveOrganization, HasRolePermission
from .throttling import BurstRateThrottle, ScopedUserRateThrottle, WriteRateThrottle


class TenantMixin:
    """Shared tenant plumbing for API views."""

    #: ``{"read"|"create"|"update"|"delete"|"write": Permission}`` consumed by HasRolePermission.
    required_permissions: dict[str, str] = {}

    @property
    def tenant(self) -> TenantContext:
        tenant = get_tenant(self.request)
        if tenant is None:
            # Reached only if a subclass drops HasActiveOrganization. Fail closed loudly.
            raise PermissionDenied("No active workspace for this request.")
        return tenant

    @property
    def organization(self) -> Organization:
        return self.tenant.organization

    @property
    def acting_user(self):
        """A real ``User`` row to attribute writes to.

        An API key acts on behalf of an *organization*, not a person, so ``request.user`` is
        an :class:`~api.authentication.ApiKeyUser` with no primary key. Assigning it to a
        model ForeignKey raises ``ValueError`` and produces a 500. Key-authenticated writes
        are therefore attributed to the person who created the key — which is also the
        honest answer for an audit trail — falling back to the workspace owner if that
        account has since been removed.
        """

        user = getattr(self.request, "user", None)
        if getattr(user, "pk", None):
            return user

        api_key = getattr(self.request, "auth", None)
        created_by = getattr(api_key, "created_by", None)
        if created_by is not None and getattr(created_by, "pk", None):
            return created_by
        return self.organization.owner

    @property
    def is_api_key_request(self) -> bool:
        return bool(getattr(getattr(self.request, "tenant", None), "via_api_key", False)) or not getattr(
            getattr(self.request, "user", None), "pk", None
        )

    def require_personal_identity(self):
        """Reject key-authenticated callers on endpoints that are inherently per-person.

        "My notifications" has no meaning for a workspace-scoped credential; answering with
        the key creator's inbox would be a quiet privacy leak.
        """

        from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

        if not getattr(getattr(self.request, "user", None), "pk", None):
            raise DRFPermissionDenied(
                "This endpoint acts on your own account and cannot be used with an API key."
            )
        return self.request.user

    def get_serializer_context(self):
        context = super().get_serializer_context()
        tenant = get_tenant(self.request)
        context["tenant"] = tenant
        context["organization"] = tenant.organization if tenant else None
        return context


class OrganizationScopedViewSet(TenantMixin, viewsets.ModelViewSet):
    permission_classes = [HasActiveOrganization, HasRolePermission]
    throttle_classes = [ScopedUserRateThrottle, BurstRateThrottle, WriteRateThrottle]

    #: Subclasses set ``base_queryset`` instead of ``queryset`` so the unscoped manager is
    #: never reachable from the router or from ``self.queryset``.
    base_queryset = None

    def get_queryset(self):
        queryset = self.base_queryset
        if queryset is None:  # pragma: no cover - programming error
            raise NotImplementedError(f"{type(self).__name__} must define base_queryset.")
        return self.tenant.scope(queryset.all())

    def perform_create(self, serializer):
        # organization is never client-supplied; it comes from the authenticated context.
        serializer.save(organization=self.organization)

    def perform_destroy(self, instance):
        instance.delete()


class ReadOnlyOrganizationScopedViewSet(TenantMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = [HasActiveOrganization, HasRolePermission]
    throttle_classes = [ScopedUserRateThrottle, BurstRateThrottle]
    base_queryset = None

    def get_queryset(self):
        queryset = self.base_queryset
        if queryset is None:  # pragma: no cover - programming error
            raise NotImplementedError(f"{type(self).__name__} must define base_queryset.")
        return self.tenant.scope(queryset.all())


def get_tenant_object(tenant: TenantContext, queryset, **lookup):
    """Fetch one row inside the tenant, raising 404 (never 403) when it is foreign.

    Returning 403 for another tenant's id would confirm that the id exists — a cheap
    enumeration oracle. 404 is the correct answer to "this is not your object".
    """

    obj = tenant.scope(queryset).filter(**lookup).first()
    if obj is None:
        raise NotFound("Not found.")
    return obj
