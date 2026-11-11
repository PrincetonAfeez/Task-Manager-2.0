"""Staff two-step verification gate for the Django admin and staff sessions."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode

from . import policy

#: Paths a staff user may still hit while satisfying MFA (enrol / verify / logout / static).
_MFA_EXEMPT_PREFIXES = (
    "/accounts/login/",
    "/accounts/logout/",
    "/accounts/mfa/",
    "/account/security/",
    "/static/",
    "/healthz",
    "/readyz",
    "/version",
    "/status",
)


class StaffMfaMiddleware:
    """Force platform staff through TOTP before admin or the rest of the app.

    Workspace MFA policy is enforced in view decorators / DRF permissions. This middleware
    covers the Django admin (which does not use those decorators) and any staff browsing
    the product UI with ``STAFF_REQUIRE_MFA`` on.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        requirement = policy.staff_mfa_requirement(request)
        if requirement is None:
            return self.get_response(request)

        path = request.path
        if any(path.startswith(prefix) for prefix in _MFA_EXEMPT_PREFIXES):
            return self.get_response(request)

        if requirement == policy.NEEDS_MFA_VERIFY:
            target = reverse("mfa_verify") + "?" + urlencode({"next": request.get_full_path()})
            messages.warning(request, "Staff accounts require two-step verification.")
        else:
            target = reverse("account_security")
            messages.warning(
                request,
                "Staff accounts must enrol two-step verification before continuing.",
            )
        return redirect(target)
