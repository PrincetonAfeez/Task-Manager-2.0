"""API key authentication for the public API.

Keys are presented as ``Authorization: Api-Key <token>``. A key pins the request to the
organization that issued it and to a role ceiling, so a leaked key can never reach another
tenant or exceed the permissions its creator held.
"""

from __future__ import annotations

import hmac

from django.utils import timezone
from rest_framework import authentication, exceptions

from kernel.models import ApiKey
from kernel.tenancy import TenantContext
from ops.logging import get_logger

logger = get_logger("taskmanager.api.auth")

KEYWORD = "Api-Key"
# last_used_at is a coarse "is this key still in use?" signal, not an access log; writing
# it on every request would add a row update to every authenticated call for no benefit.
LAST_USED_RESOLUTION_SECONDS = 300


class ApiKeyUser:
    """A minimal principal for key-authenticated calls.

    Deliberately *not* a ``User``: an API key acts on behalf of an organization, not a
    person, and letting it masquerade as its creator would make audit trails lie.
    """

    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = False
    is_superuser = False

    def __init__(self, api_key: ApiKey):
        self.api_key = api_key
        self.pk = None
        self.id = None
        self.username = f"apikey:{api_key.prefix}"
        self.organization = api_key.organization

    def __str__(self):
        return self.username

    def get_username(self):
        return self.username

    def has_perm(self, *args, **kwargs):
        return False

    def has_module_perms(self, *args, **kwargs):
        return False


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = KEYWORD

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            return None

        token = parts[1]
        api_key = self._lookup(token)
        if api_key is None:
            # Uniform message: never reveal whether the prefix existed, was revoked, or expired.
            raise exceptions.AuthenticationFailed("Invalid or expired API key.")

        self._touch(api_key)
        context = TenantContext(
            organization=api_key.organization,
            role=api_key.role,
            membership=None,
            via_api_key=True,
        )
        request.api_key_context = context
        # ActiveOrganizationMiddleware closes over the *Django* request, which is a
        # different object from the DRF one, so the context has to land on both.
        underlying = getattr(request, "_request", None)
        if underlying is not None:
            underlying.api_key_context = context
        return ApiKeyUser(api_key), api_key

    def authenticate_header(self, request):
        return self.keyword

    @staticmethod
    def _lookup(token: str) -> ApiKey | None:
        # Token layout: <env>_<random>.<secret>. Split on the separator, never on "_":
        # the secret is base64url and legitimately contains underscores.
        prefix, separator, secret = token.partition(ApiKey.TOKEN_SEPARATOR)
        if not separator or not prefix or not secret:
            return None

        api_key = ApiKey.objects.select_related("organization").filter(prefix=prefix).first()
        if api_key is None:
            return None
        # Constant-time compare so a timing oracle cannot recover the stored digest.
        if not hmac.compare_digest(api_key.token_hash, ApiKey.hash_secret(secret)):
            return None
        if not api_key.is_active or api_key.organization.deleted_at is not None:
            return None
        return api_key

    @staticmethod
    def _touch(api_key: ApiKey) -> None:
        now = timezone.now()
        if api_key.last_used_at and (now - api_key.last_used_at).total_seconds() < LAST_USED_RESOLUTION_SECONDS:
            return
        ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
        api_key.last_used_at = now
