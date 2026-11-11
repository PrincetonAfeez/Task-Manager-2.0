"""Deploy-time checks for the tasks app."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def attachments_are_scanned(app_configs, **kwargs):
    attachments_on = getattr(settings, "FEATURE_FLAG_DEFAULTS", {}).get("attachments", False)
    if not attachments_on or getattr(settings, "ATTACHMENT_SCAN_BACKEND", ""):
        return []
    return [
        Warning(
            "Attachments are enabled but ATTACHMENT_SCAN_BACKEND is not set; uploads are "
            "served to other members without a malware scan.",
            hint="Run clamd (see the `scanning` profile in docker-compose.yml) and set ATTACHMENT_SCAN_BACKEND=clamav.",
            id="tasks.W001",
        )
    ]
