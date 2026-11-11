"""A single, predictable error envelope for the whole API.

Two properties matter to a customer integrating against this: errors are machine-readable
(stable ``code``), and they never leak internals. Unhandled exceptions are logged with the
request id and returned as a generic 500 — the stack trace goes to the log, not the client.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from ops.logging import get_logger, get_request_id

logger = get_logger("taskmanager.api")

_DEFAULT_CODES = {
    400: "bad_request",
    401: "authentication_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    412: "precondition_failed",
    415: "unsupported_media_type",
    428: "precondition_required",
    429: "rate_limited",
    500: "server_error",
    503: "service_unavailable",
}


class ConflictError(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The resource was modified by someone else."
    default_code = "conflict"


class QuotaExceeded(exceptions.APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Plan limit reached."
    default_code = "quota_exceeded"


class FeatureDisabled(exceptions.APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "This feature is not enabled for your workspace."
    default_code = "feature_disabled"


def api_exception_handler(exc, context):
    """Translate any exception into the shared envelope."""

    if isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied(str(exc) or None)
    elif isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(getattr(exc, "message_dict", None) or exc.messages)
    elif isinstance(exc, IntegrityError):
        # A unique/FK violation reaching the view is a conflicting write, not a 500.
        # The driver message can name tables and columns, so it is logged, not returned.
        logger.warning("integrity error surfaced to API", exc_info=exc)
        exc = ConflictError("The request conflicts with existing data.")

    response = drf_exception_handler(exc, context)

    if response is None:
        request = context.get("request")
        logger.exception(
            "unhandled API exception",
            extra={
                "path": getattr(request, "path", ""),
                "method": getattr(request, "method", ""),
                "view": context.get("view").__class__.__name__ if context.get("view") else "",
            },
        )
        return Response(
            {
                "error": {
                    "code": "server_error",
                    "message": "An unexpected error occurred. Reference this request id when reporting it.",
                    "request_id": get_request_id(),
                }
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code = getattr(exc, "default_code", None) or _DEFAULT_CODES.get(response.status_code, "error")
    detail = response.data
    message, fields = _split_detail(detail)

    body = {
        "error": {
            "code": str(code),
            "message": message,
            "request_id": get_request_id(),
        }
    }
    if fields:
        body["error"]["fields"] = fields
    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        retry_after = getattr(exc, "wait", None)
        if retry_after is not None:
            seconds = int(retry_after) + 1
            body["error"]["retry_after"] = seconds
            response["Retry-After"] = str(seconds)

    response.data = body
    return response


def _split_detail(detail):
    """Split DRF's polymorphic ``detail`` into a message plus per-field errors."""

    if isinstance(detail, dict):
        if set(detail) == {"detail"}:
            return str(detail["detail"]), None
        return "Validation failed.", {
            key: [str(item) for item in (value if isinstance(value, list) else [value])]
            for key, value in detail.items()
        }
    if isinstance(detail, list):
        return "Validation failed.", {"non_field_errors": [str(item) for item in detail]}
    return str(detail), None
