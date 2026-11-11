"""DRF permission classes backed by the role matrix in :mod:`kernel.roles`."""

from __future__ import annotations

from rest_framework import permissions

from kernel.roles import Permission
from kernel.tenancy import get_tenant

SAFE_METHODS = permissions.SAFE_METHODS


class HasActiveOrganization(permissions.BasePermission):
    """Reject anonymous callers and authenticated ones with no live workspace.

    Runs after DRF authentication, so an API key presented on this request has already
    pinned its organization by the time ``get_tenant`` is consulted.
    """

    message = "You are not a member of any active workspace."

    def has_permission(self, request, view):
        tenant = get_tenant(request)
        if tenant is None:
            return False

        # The workspace's sign-in policy applies to browser sessions calling the API too;
        # otherwise the API would be a way around it. API keys are exempt (see the policy).
        from identity import policy

        requirement = policy.access_requirement(request, tenant)
        if requirement is not None:
            self.message = {
                policy.NEEDS_SSO: "This workspace requires single sign-on.",
            }.get(requirement, "This workspace requires two-step verification.")
            return False
        return True


class HasRolePermission(permissions.BasePermission):
    """Enforce ``view.required_permissions`` — a ``{method_group: permission}`` map.

    Views declare *what* they need; this class decides *whether* the caller has it. Keeping
    that split means a new endpoint cannot forget to check, only to declare.
    """

    message = "Your role does not allow this action."

    def has_permission(self, request, view):
        tenant = get_tenant(request)
        if tenant is None:
            return False
        required = self._required(request, view)
        if required is None:
            return True
        return tenant.can(required)

    def has_object_permission(self, request, view, obj):
        # Object-level tenancy is enforced by queryset scoping (a foreign object is a 404,
        # not a 403). This hook only re-checks the action permission.
        return self.has_permission(request, view)

    @staticmethod
    def _required(request, view):
        mapping = getattr(view, "required_permissions", None)
        if not mapping:
            return None
        if request.method in SAFE_METHODS:
            return mapping.get("read")
        if request.method == "DELETE":
            return mapping.get("delete") or mapping.get("write")
        if request.method == "POST":
            return mapping.get("create") or mapping.get("write")
        return mapping.get("update") or mapping.get("write")


class ReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


class IsOrganizationAdmin(permissions.BasePermission):
    message = "Workspace administrator role required."

    def has_permission(self, request, view):
        tenant = get_tenant(request)
        return tenant is not None and tenant.can(Permission.ORG_SETTINGS)


class IsOrganizationOwner(permissions.BasePermission):
    message = "Workspace owner role required."

    def has_permission(self, request, view):
        tenant = get_tenant(request)
        return tenant is not None and tenant.can(Permission.ORG_DELETE)
