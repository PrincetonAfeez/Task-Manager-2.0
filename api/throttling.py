"""Rate limiting.

Throttles are keyed by API key or user *and* organization, so one noisy tenant cannot
exhaust another's budget, and a user with several workspaces is not penalised for it.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle


def _enabled() -> bool:
    return getattr(settings, "API_THROTTLING_ENABLED", True)


class ScopedAnonRateThrottle(AnonRateThrottle):
    def allow_request(self, request, view):
        if not _enabled():
            return True
        return super().allow_request(request, view)


class ScopedUserRateThrottle(UserRateThrottle):
    def allow_request(self, request, view):
        if not _enabled():
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        api_key = getattr(request, "auth", None)
        if api_key is not None and hasattr(api_key, "prefix"):
            ident = f"key:{api_key.prefix}"
        else:
            from kernel.tenancy import get_tenant

            tenant = get_tenant(request)
            org_id = getattr(getattr(tenant, "organization", None), "pk", "none")
            ident = f"user:{request.user.pk}:org:{org_id}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class BurstRateThrottle(ScopedUserRateThrottle):
    """Short-window ceiling layered on top of the hourly budget."""

    scope = "burst"


class WriteRateThrottle(ScopedUserRateThrottle):
    """Applied to mutating endpoints only; reads stay on the general budget."""

    scope = "write"

    def allow_request(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return super().allow_request(request, view)


class ExportRateThrottle(ScopedUserRateThrottle):
    """Exports are expensive; a tight budget stops them being used as a DoS."""

    scope = "export"


class LoginRateThrottle(SimpleRateThrottle):
    """Protects the HTML login form, which DRF throttles do not see.

    Keyed on client IP. The per-account lockout in ``kernel.auth_backends`` covers the
    complementary case of one account attacked from many addresses.
    """

    scope = "auth"

    def get_cache_key(self, request, view=None):
        if not _enabled():
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

