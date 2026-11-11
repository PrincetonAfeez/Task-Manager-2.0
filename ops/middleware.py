"""Cross-cutting HTTP middleware: correlation ids, security headers, slow-request logging."""

from __future__ import annotations

import re
import time
import uuid

from django.conf import settings

from .logging import get_logger, reset_request_id, set_request_id

logger = get_logger("taskmanager.http")

# Accept an inbound correlation id only if it looks like one. Echoing arbitrary
# client-controlled bytes into logs is a log-injection primitive.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestIDMiddleware:
    """Assign (or adopt) a request id and expose it on the request and response."""

    header = "HTTP_X_REQUEST_ID"
    response_header = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.header, "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming or "") else uuid.uuid4().hex
        request.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        response[self.response_header] = request_id
        return response


class SecurityHeadersMiddleware:
    """Apply headers Django does not ship settings for (CSP, Permissions-Policy).

    Django's SecurityMiddleware covers HSTS/nosniff/referrer-policy; these two have no
    built-in setting, and shipping without a CSP leaves XSS entirely unmitigated.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        self.permissions_policy = getattr(settings, "PERMISSIONS_POLICY", "")

    def __call__(self, request):
        response = self.get_response(request)
        if self.csp and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = self.csp
        if self.permissions_policy and "Permissions-Policy" not in response:
            response["Permissions-Policy"] = self.permissions_policy
        response.setdefault("X-Content-Type-Options", "nosniff")
        # Signed attachment downloads must never be cached by a shared proxy.
        if getattr(request, "_private_response", False):
            response["Cache-Control"] = "private, no-store"
        return response


class RequestTimingMiddleware:
    """Log requests slower than ``SLOW_REQUEST_MS`` so latency regressions are visible."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.threshold_ms = getattr(settings, "SLOW_REQUEST_MS", 1000)

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        if elapsed_ms >= self.threshold_ms:
            logger.warning(
                "slow request",
                extra={
                    "path": request.path,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round(elapsed_ms, 1),
                },
            )
        return response
