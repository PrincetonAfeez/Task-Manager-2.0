"""Deploy-time checks (``manage.py check --deploy``)."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def field_encryption_key_configured(app_configs, **kwargs):
    if getattr(settings, "FIELD_ENCRYPTION_KEYS", None):
        return []
    return [
        Warning(
            "FIELD_ENCRYPTION_KEYS is not set; stored TOTP seeds and SSO client secrets are "
            "encrypted with a key derived from DJANGO_SECRET_KEY.",
            hint=(
                "Rotating DJANGO_SECRET_KEY would make every enrolled authenticator and SSO "
                "connection unreadable. Generate a dedicated key with "
                "`python manage.py generate_field_key` and set FIELD_ENCRYPTION_KEYS."
            ),
            id="identity.W001",
        )
    ]
