"""Operational endpoints: health, readiness, error handlers.

The original health endpoints returned ``{"status": "ok"}`` for the worker, scheduler and
reaper unconditionally — they were placeholders that would report healthy while the worker
fleet was down. These check real state.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import requires_csrf_token

from .logging import get_logger, get_request_id

logger = get_logger("taskmanager.ops")

#: A component is unhealthy if nothing has progressed within this window.
STALENESS_WINDOW = timedelta(minutes=10)

_ERROR_CODES = {
    400: "bad_request",
    403: "permission_denied",
    404: "not_found",
    500: "server_error",
}


@never_cache
def healthz(request):
    """Liveness: is this process able to serve? Deliberately dependency-free."""

    return JsonResponse(
        {
            "status": "ok",
            "service": "web",
            "release": getattr(settings, "APP_RELEASE", "dev"),
            "environment": getattr(settings, "ENVIRONMENT", "unknown"),
        }
    )


@never_cache
def readyz(request):
    """Readiness: can this process reach everything it needs to serve traffic?

    Returns 503 on failure so a load balancer actually removes the instance — the previous
    implementation would have raised a 500 traceback instead.
    """

    checks: dict[str, dict] = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        healthy = False
        checks["database"] = {"status": "error", "detail": type(exc).__name__}
        logger.error("readiness: database unreachable", exc_info=exc)

    try:
        from django.core.cache import cache

        probe = f"readyz:{get_request_id()}"
        cache.set(probe, "1", timeout=5)
        checks["cache"] = {"status": "ok" if cache.get(probe) == "1" else "error"}
        cache.delete(probe)
        healthy = healthy and checks["cache"]["status"] == "ok"
    except Exception as exc:
        healthy = False
        checks["cache"] = {"status": "error", "detail": type(exc).__name__}

    return JsonResponse(
        {"status": "ready" if healthy else "not_ready", "checks": checks},
        status=200 if healthy else 503,
    )


def _queue_health() -> tuple[bool, dict]:
    """Real worker health: is anything finishing, and is the backlog aging?"""

    from queues.models import Job

    now = timezone.now()
    recent = Job.objects.filter(finished_at__gte=now - STALENESS_WINDOW).count()
    waiting = Job.objects.filter(status__in=Job.WAITING_STATUSES, available_at__lte=now)
    backlog = waiting.count()
    oldest = waiting.order_by("available_at").values_list("available_at", flat=True).first()
    oldest_age = int((now - oldest).total_seconds()) if oldest else 0

    threshold = getattr(settings, "QUEUE_OLDEST_JOB_ALERT_SECONDS", 900)
    # A backlog is only a symptom if it is also *stale*: a busy queue that is draining is
    # healthy, an idle-looking queue whose oldest job is 20 minutes old is not.
    stalled = backlog > 0 and oldest_age > threshold and recent == 0
    return (not stalled), {
        "backlog": backlog,
        "in_flight": Job.objects.filter(status=Job.Status.LEASED).count(),
        "completed_recently": recent,
        "oldest_waiting_seconds": oldest_age,
        "threshold_seconds": threshold,
    }


@never_cache
def worker_healthz(request):
    healthy, detail = _queue_health()
    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "service": "worker", **detail},
        status=200 if healthy else 503,
    )


@never_cache
def scheduler_healthz(request):
    """Healthy when no active schedule is overdue by more than the staleness window."""

    from schedules.models import Schedule

    now = timezone.now()
    overdue = Schedule.objects.filter(
        active=True, next_run_at__isnull=False, next_run_at__lt=now - STALENESS_WINDOW
    ).count()
    broken = Schedule.objects.filter(consecutive_failures__gte=3).count()
    healthy = overdue == 0
    return JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "service": "scheduler",
            "overdue_schedules": overdue,
            "deactivated_schedules": broken,
        },
        status=200 if healthy else 503,
    )


@never_cache
def reaper_healthz(request):
    """Healthy when no lease has been expired longer than the staleness window."""

    from queues.models import Job

    now = timezone.now()
    stranded = Job.objects.filter(
        status=Job.Status.LEASED, lease_expires_at__lt=now - STALENESS_WINDOW
    ).count()
    return JsonResponse(
        {
            "status": "ok" if stranded == 0 else "degraded",
            "service": "reaper",
            "stranded_leases": stranded,
        },
        status=200 if stranded == 0 else 503,
    )


@never_cache
def version(request):
    return JsonResponse(
        {
            "release": getattr(settings, "APP_RELEASE", "dev"),
            "environment": getattr(settings, "ENVIRONMENT", "unknown"),
            "api_version": "v1",
        }
    )


# --------------------------------------------------------------------------------------
# Error handlers — safe messages plus a request id the user can quote to support
# --------------------------------------------------------------------------------------


def _error_response(request, *, status: int, template: str, title: str, message: str):
    wants_json = request.path.startswith("/api/") or request.headers.get("Accept", "").startswith(
        "application/json"
    )
    if wants_json:
        return JsonResponse(
            {
                "error": {
                    "code": _ERROR_CODES[status],
                    "message": message,
                    "request_id": get_request_id(),
                }
            },
            status=status,
        )
    return render(
        request,
        template,
        {"title": title, "message": message, "request_id": get_request_id()},
        status=status,
    )


@requires_csrf_token
def bad_request(request, exception=None):
    return _error_response(
        request,
        status=400,
        template="errors/400.html",
        title="Bad request",
        message="That request could not be understood.",
    )


@requires_csrf_token
def permission_denied(request, exception=None):
    return _error_response(
        request,
        status=403,
        template="errors/403.html",
        title="Not allowed",
        message="You do not have permission to do that.",
    )


@requires_csrf_token
def page_not_found(request, exception=None):
    return _error_response(
        request,
        status=404,
        template="errors/404.html",
        title="Not found",
        message="That page does not exist.",
    )


@requires_csrf_token
def server_error(request):
    # The traceback goes to the logs with the request id attached; the customer gets the
    # id and nothing else.
    logger.error("unhandled server error", extra={"path": request.path})
    return _error_response(
        request,
        status=500,
        template="errors/500.html",
        title="Something went wrong",
        message="An unexpected error occurred. Quote the reference below if you contact support.",
    )
