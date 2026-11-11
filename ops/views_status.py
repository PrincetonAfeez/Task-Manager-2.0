"""Public status page — best-effort component health, not a contractual SLA."""

from __future__ import annotations

import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from . import views as ops_views


@never_cache
def status_page(request):
    """Aggregate liveness/readiness and automation probes for humans and monitors.

    Contractual uptime commitments remain parked (``docs/PARKED.md``). This page exposes
    the same signals the runbook already uses, in one place.
    """

    components = [
        {
            "id": "web",
            "name": "Web",
            "status": "operational",
            "detail": {"message": "This process is serving."},
        }
    ]

    ready = ops_views.readyz(request)
    ready_data = json.loads(ready.content)
    components.append(
        {
            "id": "dependencies",
            "name": "Database & cache",
            "status": "operational" if ready.status_code == 200 else "degraded",
            "detail": ready_data.get("checks", {}),
        }
    )

    for label, view in (
        ("worker", ops_views.worker_healthz),
        ("scheduler", ops_views.scheduler_healthz),
        ("reaper", ops_views.reaper_healthz),
    ):
        resp = view(request)
        body = json.loads(resp.content)
        components.append(
            {
                "id": label,
                "name": label.title(),
                "status": "operational" if resp.status_code == 200 else "degraded",
                "detail": body,
            }
        )

    overall = (
        "degraded"
        if any(c["status"] == "degraded" for c in components)
        else "operational"
    )
    payload = {
        "status": overall,
        "updated_at": timezone.now().isoformat(),
        "release": getattr(settings, "APP_RELEASE", "dev"),
        "environment": getattr(settings, "ENVIRONMENT", "unknown"),
        "components": components,
        "note": "Best-effort public status. Not a contractual SLA — see docs/PARKED.md.",
    }

    if "application/json" in (request.headers.get("Accept") or ""):
        return JsonResponse(payload)
    return render(request, "ops/status.html", {"payload": payload})
