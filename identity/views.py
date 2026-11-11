"""Two-step verification and single sign-on views."""

from __future__ import annotations

import time

import segno
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from api.throttling import LoginRateThrottle
from billing.services import has_feature
from kernel.models import Organization
from kernel.roles import Permission, Role
from kernel.services import record_audit
from kernel.tenancy import set_active_organization
from ops.logging import get_logger
from web.decorators import login_required_view, permission_required

from . import mfa, oidc, policy, totp, webauthn_service
from .models import SsoConnection, SsoIdentity, TotpDevice, WebAuthnCredential

logger = get_logger("taskmanager.identity")

#: Session key holding a password-verified login that still owes a second factor.
PENDING_KEY = "mfa_pending"
PENDING_TTL_SECONDS = 300


def _safe_next(request, candidate: str | None) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return settings.LOGIN_REDIRECT_URL


def _throttled(request) -> int | None:
    throttle = LoginRateThrottle()
    if throttle.allow_request(request, None):
        return None
    return int(throttle.wait() or 60)


def start_pending_login(request, user, next_url: str) -> None:
    """Park a password-verified login until the second factor arrives.

    Nothing about the session is authenticated yet: ``request.user`` stays anonymous, so no
    view treats the half-finished login as signed in.
    """

    request.session[PENDING_KEY] = {
        "user_id": user.pk,
        "backend": getattr(user, "backend", settings.AUTHENTICATION_BACKENDS[0]),
        "next": next_url,
        "started": time.time(),
    }


def _pending_user(request):
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - float(pending.get("started", 0)) > PENDING_TTL_SECONDS:
        request.session.pop(PENDING_KEY, None)
        return None, None
    user = get_user_model().objects.filter(pk=pending.get("user_id"), is_active=True).first()
    return user, pending


# --------------------------------------------------------------------------------------
# Verification at sign-in (and step-up for an already signed-in session)
# --------------------------------------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def mfa_verify(request):
    stepping_up = request.user.is_authenticated
    if stepping_up:
        user, pending = request.user, None
        if not mfa.user_has_mfa(user):
            return redirect("account_security")
    else:
        user, pending = _pending_user(request)
        if user is None:
            messages.info(request, "Sign in again to continue.")
            return redirect("login")

    context = {
        "stepping_up": stepping_up,
        "next": request.GET.get("next", ""),
        "has_passkeys": webauthn_service.user_has_passkeys(user),
    }
    if request.method == "POST":
        wait = _throttled(request)
        if wait is not None:
            response = render(
                request, "identity/mfa_verify.html", {**context, "error": "Too many attempts. Try again shortly."}, status=429
            )
            response["Retry-After"] = str(wait)
            return response

        try:
            method = mfa.verify_code(user, request.POST.get("code", ""))
        except mfa.MfaLocked as exc:
            request.session.pop(PENDING_KEY, None)
            record_audit(action="auth.mfa_locked", actor=user, request=request)
            return render(request, "identity/mfa_verify.html", {**context, "error": str(exc), "locked": True}, status=429)

        if method is None:
            record_audit(action="auth.mfa_failed", actor=user, request=request)
            return render(
                request, "identity/mfa_verify.html", {**context, "error": "That code is not correct."}, status=400
            )

        if stepping_up:
            destination = _safe_next(request, request.POST.get("next"))
        else:
            request.session.pop(PENDING_KEY, None)
            login(request, user, backend=pending["backend"])
            destination = _safe_next(request, pending.get("next"))
        request.session[mfa.SESSION_VERIFIED_KEY] = True
        record_audit(
            action="auth.login_succeeded" if not stepping_up else "auth.mfa_step_up",
            actor=user,
            request=request,
            metadata={"second_factor": method},
        )
        if method == "recovery":
            messages.warning(
                request,
                f"You signed in with a recovery code. {mfa.remaining_recovery_codes(user)} remain; "
                "generate a new set if you have lost your authenticator.",
            )
        return redirect(destination)

    return render(request, "identity/mfa_verify.html", context)


# --------------------------------------------------------------------------------------
# Account security page
# --------------------------------------------------------------------------------------


@login_required_view
@never_cache
@require_GET
def account_security(request):
    organizations = Organization.objects.for_user(request.user).distinct()
    return render(
        request,
        "identity/account_security.html",
        {
            "has_mfa": mfa.user_has_mfa(request.user),
            "recovery_codes_remaining": mfa.remaining_recovery_codes(request.user),
            "passkeys": WebAuthnCredential.objects.filter(user=request.user).order_by("-created_at"),
            "has_totp": TotpDevice.objects.filter(user=request.user, confirmed_at__isnull=False).exists(),
            "sso_identities": SsoIdentity.objects.filter(user=request.user).select_related(
                "connection__organization"
            ),
            "required_by": [org for org in organizations if policy.organization_requires_mfa(org)],
            "has_password": request.user.has_usable_password(),
        },
    )


@login_required_view
@require_POST
def mfa_start(request):
    try:
        mfa.begin_enrollment(request.user)
    except mfa.MfaError as exc:
        messages.info(request, str(exc))
        return redirect("account_security")
    return redirect("mfa_setup")


@login_required_view
@never_cache
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    device = mfa.pending_device(request.user)
    if device is None:
        return redirect("account_security")

    error = None
    if request.method == "POST":
        try:
            codes = mfa.confirm_enrollment(request.user, request.POST.get("code", ""))
        except mfa.MfaError as exc:
            error = str(exc)
        else:
            request.session[mfa.SESSION_VERIFIED_KEY] = True
            record_audit(action="auth.mfa_enabled", actor=request.user, request=request)
            return render(request, "identity/recovery_codes.html", {"codes": codes, "just_enabled": True})

    secret = device.secret
    return render(
        request,
        "identity/mfa_setup.html",
        {
            "secret_grouped": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            "error": error,
        },
        status=400 if error else 200,
    )


@login_required_view
@never_cache
@require_GET
def mfa_qr(request):
    """The enrolment QR code, as an image the setup page embeds.

    Served as its own same-origin SVG response rather than inlined into the page, so no
    markup is ever marked "safe" by hand and the strict ``img-src 'self'`` policy needs no
    exception. It exists only while a device is pending and is never cacheable: the image
    *is* the seed.
    """

    device = mfa.pending_device(request.user)
    if device is None:
        raise Http404("No authenticator is being set up.")

    import io

    uri = totp.provisioning_uri(
        device.secret,
        account=request.user.email or request.user.get_username(),
        issuer=getattr(settings, "MFA_ISSUER", "Task Manager"),
    )
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=5, border=2, dark="#111", light="#fff")
    response = HttpResponse(buffer.getvalue(), content_type="image/svg+xml")
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _reauthenticate(request) -> str | None:
    """Confirm the person at the keyboard: password (when they have one) plus a code."""

    user = request.user
    if user.has_usable_password() and not user.check_password(request.POST.get("password", "")):
        return "Your password is not correct."
    try:
        if mfa.verify_code(user, request.POST.get("code", "")) is None:
            return "That code is not correct."
    except mfa.MfaLocked as exc:
        return str(exc)
    return None


@login_required_view
@require_POST
def mfa_disable(request):
    if not mfa.user_has_mfa(request.user):
        return redirect("account_security")
    error = _reauthenticate(request)
    if error:
        record_audit(action="auth.mfa_disable_refused", actor=request.user, request=request)
        messages.error(request, error)
        return redirect("account_security")

    mfa.disable(request.user)
    request.session.pop(mfa.SESSION_VERIFIED_KEY, None)
    record_audit(action="auth.mfa_disabled", actor=request.user, request=request)
    messages.success(request, "Two-step verification is off.")
    return redirect("account_security")


@login_required_view
@never_cache
@require_POST
def mfa_recovery_codes(request):
    if not mfa.user_has_mfa(request.user):
        return redirect("account_security")
    error = _reauthenticate(request)
    if error:
        messages.error(request, error)
        return redirect("account_security")

    codes = mfa.regenerate_recovery_codes(request.user)
    record_audit(action="auth.mfa_recovery_codes_regenerated", actor=request.user, request=request)
    return render(request, "identity/recovery_codes.html", {"codes": codes, "just_enabled": False})


# --------------------------------------------------------------------------------------
# Workspace sign-in policy and SSO configuration
# --------------------------------------------------------------------------------------


class SsoConnectionForm(forms.Form):
    issuer = forms.URLField(max_length=255, assume_scheme="https", label="Issuer URL")
    client_id = forms.CharField(max_length=255, label="Client ID")
    client_secret = forms.CharField(
        max_length=512,
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        label="Client secret",
        help_text="Leave blank to keep the saved secret.",
    )
    allowed_domains = forms.CharField(
        max_length=500, label="Allowed email domains", help_text="Comma separated, e.g. example.com"
    )
    default_role = forms.ChoiceField(
        choices=[(Role.MEMBER, "Member"), (Role.VIEWER, "Viewer"), (Role.ADMIN, "Admin")],
        label="Role for new members",
    )
    is_enabled = forms.BooleanField(required=False, label="Enable single sign-on")
    is_required = forms.BooleanField(required=False, label="Require single sign-on for members")
    auto_provision = forms.BooleanField(required=False, label="Create accounts on first sign-in")
    trust_unverified_email = forms.BooleanField(
        required=False, label="Accept emails the provider does not mark as verified"
    )

    def __init__(self, *args, connection: SsoConnection | None = None, **kwargs):
        self.connection = connection
        super().__init__(*args, **kwargs)

    def clean_allowed_domains(self):
        domains = []
        for raw in self.cleaned_data["allowed_domains"].replace(";", ",").split(","):
            domain = raw.strip().lower().lstrip("@")
            if not domain:
                continue
            if "." not in domain or any(ch.isspace() or ch in "/@:" for ch in domain):
                raise forms.ValidationError(f"{raw.strip()!r} is not a domain name.")
            if domain not in domains:
                domains.append(domain)
        if not domains:
            raise forms.ValidationError("List at least one email domain.")
        return domains

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        has_secret = bool(data.get("client_secret")) or bool(
            self.connection and self.connection.client_secret_encrypted
        )
        if data.get("is_required") and not data.get("is_enabled"):
            self.add_error("is_required", "Enable single sign-on before requiring it.")
        if data.get("is_enabled"):
            if not has_secret:
                self.add_error("client_secret", "A client secret is required.")
            else:
                try:
                    # Prove the issuer is reachable and consistent *before* anyone depends on it.
                    oidc.discover(data["issuer"], use_cache=False)
                except oidc.SsoError as exc:
                    self.add_error("issuer", str(exc))
        return data


@permission_required(Permission.ORG_SETTINGS)
@never_cache
@require_http_methods(["GET", "POST"])
def workspace_security(request):
    organization = request.tenant.organization
    connection = SsoConnection.objects.filter(organization=organization).first()
    sso_available = has_feature(organization, "sso")

    if request.method == "POST" and request.POST.get("form") == "mfa":
        required = request.POST.get("require_mfa") == "on"
        if required and not mfa.user_has_mfa(request.user) and not policy.session_used_sso(request, organization):
            messages.error(request, "Turn on two-step verification for your own account first.")
        else:
            policy.set_policy_flag(organization, policy.REQUIRE_MFA_KEY, required)
            record_audit(
                action="org.mfa_requirement_changed",
                organization=organization,
                actor=request.user,
                request=request,
                metadata={"required": required},
            )
            messages.success(request, "Sign-in policy updated.")
        return redirect("workspace_security")

    if request.method == "POST":
        if not sso_available:
            messages.error(request, "Single sign-on is not included in this workspace's plan.")
            return redirect("workspace_security")
        form = SsoConnectionForm(request.POST, connection=connection)
        if form.is_valid():
            data = form.cleaned_data
            connection = connection or SsoConnection(organization=organization)
            connection.issuer = data["issuer"].rstrip("/")
            connection.client_id = data["client_id"]
            if data["client_secret"]:
                connection.client_secret = data["client_secret"]
            connection.allowed_domains = data["allowed_domains"]
            connection.default_role = data["default_role"]
            connection.is_enabled = data["is_enabled"]
            connection.is_required = data["is_required"]
            connection.auto_provision = data["auto_provision"]
            connection.trust_unverified_email = data["trust_unverified_email"]
            connection.save()
            policy.set_policy_flag(organization, policy.SSO_REQUIRED_KEY, connection.is_required)
            record_audit(
                action="org.sso_configured",
                organization=organization,
                actor=request.user,
                request=request,
                target_type="sso_connection",
                target_id=connection.pk,
                # Never the secret; only what an auditor needs to reconstruct the change.
                metadata={
                    "issuer": connection.issuer,
                    "enabled": connection.is_enabled,
                    "required": connection.is_required,
                    "domains": connection.allowed_domains,
                },
            )
            messages.success(request, "Single sign-on settings saved.")
            return redirect("workspace_security")
    else:
        initial = {"default_role": Role.MEMBER, "auto_provision": True}
        if connection is not None:
            initial = {
                "issuer": connection.issuer,
                "client_id": connection.client_id,
                "allowed_domains": ", ".join(connection.allowed_domains),
                "default_role": connection.default_role,
                "is_enabled": connection.is_enabled,
                "is_required": connection.is_required,
                "auto_provision": connection.auto_provision,
                "trust_unverified_email": connection.trust_unverified_email,
            }
        form = SsoConnectionForm(initial=initial, connection=connection)

    return render(
        request,
        "identity/workspace_security.html",
        {
            "form": form,
            "connection": connection,
            "sso_available": sso_available,
            "require_mfa": policy.organization_requires_mfa(organization),
            "redirect_uri": oidc.redirect_uri(),
            "login_url": settings.SITE_URL + f"/sso/{organization.slug}/",
        },
    )


# --------------------------------------------------------------------------------------
# SSO sign-in
# --------------------------------------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def sso_start(request):
    """Ask for a work email and route to that domain's identity provider."""

    error = None
    if request.method == "POST":
        if _throttled(request) is not None:
            error = "Too many attempts. Try again shortly."
        else:
            connection = oidc.find_connection_for_email(request.POST.get("email", ""))
            if connection is not None:
                return redirect("sso_login", slug=connection.organization.slug)
            # Deliberately the same answer for "no such domain" and "ambiguous domain".
            error = "Single sign-on is not set up for that email address."
    return render(request, "identity/sso_start.html", {"error": error}, status=400 if error else 200)


@never_cache
@require_GET
def sso_login(request, slug):
    connection = (
        SsoConnection.objects.select_related("organization")
        .filter(organization__slug=slug, organization__deleted_at__isnull=True, is_enabled=True)
        .first()
    )
    if connection is None:
        raise Http404("Single sign-on is not available for this workspace.")
    try:
        url = oidc.begin_login(request, connection, next_url=request.GET.get("next", ""))
    except oidc.SsoError as exc:
        messages.error(request, str(exc))
        return redirect("login")
    return redirect(url)


@never_cache
@require_GET
def sso_callback(request):
    if _throttled(request) is not None:
        messages.error(request, "Too many attempts. Try again shortly.")
        return redirect("login")
    if request.GET.get("error"):
        request.session.pop(oidc.SESSION_FLOW_KEY, None)
        record_audit(action="auth.sso_failed", request=request, metadata={"reason": "provider_error"})
        messages.error(request, "Your identity provider did not complete the sign-on.")
        return redirect("login")

    try:
        user, connection, next_url, created = oidc.complete_login(
            request, code=request.GET.get("code", ""), state=request.GET.get("state", "")
        )
    except oidc.SsoError as exc:
        record_audit(action="auth.sso_failed", request=request, metadata={"reason": str(exc)[:200]})
        messages.error(request, str(exc))
        return redirect("login")

    organization = connection.organization
    login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
    request.session[policy.SESSION_SSO_ORGS_KEY] = [organization.pk]
    set_active_organization(request, organization)
    record_audit(
        action="auth.sso_login",
        organization=organization,
        actor=user,
        request=request,
        metadata={"provisioned": created},
    )
    return redirect(_safe_next(request, next_url))
