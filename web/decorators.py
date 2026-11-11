"""View decorators for authentication, tenancy and authorization.

The failure mode differs by response type, which is why this is not middleware: an HTMX
fragment that gets a login redirect renders the whole login page inside a card, and a JSON
caller needs a status code, not HTML.
"""

from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode

from kernel.tenancy import get_tenant


def _wants_json(request) -> bool:
    return request.headers.get("Accept", "").startswith("application/json")


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False)) or request.headers.get("HX-Request") == "true"


def _deny(request, message: str, status: int = 403):
    if _wants_json(request):
        return JsonResponse({"error": {"code": "permission_denied", "message": message}}, status=status)
    if _is_htmx(request):
        # HX-Retarget/HX-Reswap put the error where the user can see it instead of
        # replacing the element they clicked with an error page.
        response = HttpResponse(f'<p class="error" role="alert">{message}</p>', status=status)
        response["HX-Retarget"] = "#flash"
        response["HX-Reswap"] = "innerHTML"
        return response
    return HttpResponseForbidden(message)


def login_required_view(view_func):
    """Like Django's ``login_required`` but HTMX- and JSON-aware."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if _wants_json(request):
                return JsonResponse(
                    {"error": {"code": "authentication_required", "message": "Sign in required."}},
                    status=401,
                )
            if _is_htmx(request):
                response = HttpResponse(status=401)
                # Tell HTMX to do a full navigation so the user lands on a real login page.
                response["HX-Redirect"] = "/accounts/login/"
                return response
            return redirect_to_login(request.get_full_path())
        return view_func(request, *args, **kwargs)

    return wrapper


def _sign_in_policy_response(request, tenant):
    """Send a session that does not meet the workspace's sign-in policy to the fix.

    Enforced here, on every workspace view, rather than only at login: a policy switched on
    this afternoon has to apply to sessions that signed in this morning.
    """

    from identity import policy

    requirement = policy.access_requirement(request, tenant)
    if requirement is None:
        return None

    name = tenant.organization.name
    if requirement == policy.NEEDS_SSO:
        message = f"{name} requires single sign-on."
        target = reverse("sso_login", kwargs={"slug": tenant.organization.slug})
    elif requirement == policy.NEEDS_MFA_VERIFY:
        message = f"{name} requires two-step verification. Enter a code to continue."
        target = reverse("mfa_verify") + "?" + urlencode({"next": request.get_full_path()})
    else:
        message = f"{name} requires two-step verification. Set it up to continue."
        target = reverse("account_security")

    if _wants_json(request):
        return JsonResponse({"error": {"code": f"{requirement}_required", "message": message}}, status=403)
    if _is_htmx(request):
        response = HttpResponse(status=403)
        response["HX-Redirect"] = target
        return response
    messages.warning(request, message)
    return redirect(target)


def workspace_required(view_func):
    """Ensure ``request.tenant`` is populated, or send the user to onboarding."""

    @wraps(view_func)
    @login_required_view
    def wrapper(request, *args, **kwargs):
        tenant = get_tenant(request)
        request.tenant = tenant
        if tenant is None:
            if _wants_json(request):
                return JsonResponse(
                    {"error": {"code": "no_workspace", "message": "No active workspace."}},
                    status=409,
                )
            messages.info(request, "Create a workspace to get started.")
            return redirect("workspace_create")
        blocked = _sign_in_policy_response(request, tenant)
        if blocked is not None:
            return blocked
        return view_func(request, *args, **kwargs)

    return wrapper


def permission_required(permission: str):
    """Assert a role permission from :mod:`kernel.roles` before running the view."""

    def decorator(view_func):
        @wraps(view_func)
        @workspace_required
        def wrapper(request, *args, **kwargs):
            if not request.tenant.can(permission):
                return _deny(
                    request,
                    f"Your role ({request.tenant.role}) cannot perform this action.",
                )
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def feature_required(flag: str):
    """Gate a view behind a feature flag so a bad feature can be switched off live."""

    def decorator(view_func):
        @wraps(view_func)
        @workspace_required
        def wrapper(request, *args, **kwargs):
            from kernel.services import feature_enabled

            if not feature_enabled(flag, request.tenant.organization):
                return _deny(request, "This feature is not enabled for your workspace.", status=403)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator

