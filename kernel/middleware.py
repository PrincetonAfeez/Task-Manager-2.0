"""Attach the resolved tenant to every request."""

from __future__ import annotations

from . import rls
from .tenancy import get_tenant


class ActiveOrganizationMiddleware:
    """Expose ``request.tenant`` — the caller's :class:`TenantContext` or ``None``.

    Resolution is deferred to :func:`kernel.tenancy.get_tenant`, which memoises per request.
    Nothing is computed here: an anonymous request, a health check, or a static asset must
    not pay for a membership query, and API-key callers are only authenticated later, inside
    DRF. Views assert on the tenant explicitly (see ``web.decorators.workspace_required``)
    because the right failure differs between HTML, HTMX and JSON.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A plain attribute, not a lazy proxy: templates and views compare it against None,
        # and a SimpleLazyObject wrapping None fails that comparison.
        request.tenant = None
        response = self.get_response(request)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Runs after authentication middleware and before the view, which is the earliest
        # point a session-authenticated tenant can be known. DRF views re-resolve after
        # their own authentication runs.
        if getattr(request, "tenant", None) is None:
            request.tenant = get_tenant(request)
        return None


class TenantRLSMiddleware:
    """Bind Postgres RLS to the active organization for the remainder of the request.

    No-op unless ``ENABLE_POSTGRES_RLS`` is on and the database is PostgreSQL. Staff users
    without an active workspace get a bypass so the admin can still list across tenants.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None) or get_tenant(request)
        user = getattr(request, "user", None)
        if tenant is not None:
            rls.set_current_organization(tenant.organization.pk)
        elif user is not None and user.is_authenticated and (user.is_staff or user.is_superuser):
            rls.set_current_organization(None)  # bypass
        else:
            rls.clear_tenant_binding()
        try:
            return self.get_response(request)
        finally:
            rls.clear_tenant_binding()
