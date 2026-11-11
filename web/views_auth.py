"""Authentication views with rate limiting and audit logging."""

from __future__ import annotations

from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

from api.throttling import LoginRateThrottle
from kernel.services import record_audit
from ops.logging import get_logger

logger = get_logger("taskmanager.auth")


class ThrottledLoginView(LoginView):
    """Rate-limit login attempts per client address and audit both outcomes.

    Django's stock ``LoginView`` has no rate limiting at all. Combined with the per-account
    lockout in ``kernel.auth_backends``, this closes both halves of credential stuffing:
    one address trying many accounts, and many addresses trying one account.
    """

    def post(self, request, *args, **kwargs):
        throttle = LoginRateThrottle()
        if not throttle.allow_request(request, self):
            wait = int(throttle.wait() or 60)
            logger.warning("login throttled", extra={"retry_after": wait})
            response = render(
                request,
                self.template_name,
                {
                    "form": self.get_form(),
                    "throttled": True,
                    "retry_after": wait,
                },
                status=429,
            )
            response["Retry-After"] = str(wait)
            return response
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        from identity import mfa
        from identity.views import start_pending_login

        user = form.get_user()
        if mfa.user_has_mfa(user):
            # The password was right, but the session is not signed in until the second
            # factor is. The half-finished login lives in the session, never in request.user.
            start_pending_login(self.request, user, self.get_success_url())
            record_audit(action="auth.password_accepted", actor=user, request=self.request)
            return redirect("mfa_verify")

        response = super().form_valid(form)
        record_audit(action="auth.login_succeeded", actor=self.request.user, request=self.request)
        return response

    def form_invalid(self, form):
        # Audited without the attempted password, and without confirming whether the
        # username exists — the row records that an attempt happened, nothing more.
        record_audit(
            action="auth.login_failed",
            request=self.request,
            metadata={"reason": next(iter(form.errors), "invalid")},
        )
        return super().form_invalid(form)
