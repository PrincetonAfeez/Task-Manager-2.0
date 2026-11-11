"""Authentication backend with brute-force lockout.

Django's stock ``ModelBackend`` will happily verify an unlimited number of guesses. This
subclass adds a cache-backed counter keyed on *both* the username and the client IP, so a
credential-stuffing run is throttled whether it rotates usernames or addresses.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core.cache import cache

from ops.logging import get_logger

logger = get_logger("taskmanager.auth")

_PREFIX = "login-lockout"


def _bucket_key(kind: str, value: str) -> str:
    # Hash the identifier: usernames are frequently email addresses, and cache keys end
    # up in logs and monitoring dashboards.
    digest = hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:32]
    return f"{_PREFIX}:{kind}:{digest}"


def client_ip(request) -> str:
    """Best-effort client address.

    ``X-Forwarded-For`` is only consulted when the deployment declares it sits behind a
    trusted TLS proxy; otherwise any client could spoof its way out of a lockout.
    """

    if request is None:
        return ""
    if getattr(settings, "BEHIND_TLS_PROXY", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _max_attempts() -> int:
    return getattr(settings, "LOGIN_LOCKOUT_MAX_ATTEMPTS", 8)


def _window() -> int:
    return getattr(settings, "LOGIN_LOCKOUT_WINDOW_SECONDS", 900)


def _cooldown() -> int:
    return getattr(settings, "LOGIN_LOCKOUT_COOLDOWN_SECONDS", 900)


def is_locked_out(username: str | None, ip: str | None) -> bool:
    limit = _max_attempts()
    for kind, value in (("user", username), ("ip", ip)):
        if not value:
            continue
        if (cache.get(_bucket_key(kind, value)) or 0) >= limit:
            return True
    return False


def register_failure(username: str | None, ip: str | None) -> None:
    for kind, value in (("user", username), ("ip", ip)):
        if not value:
            continue
        key = _bucket_key(kind, value)
        # add() then incr() keeps the TTL anchored to the first failure in the window,
        # so a slow attacker cannot keep the counter alive forever.
        if cache.add(key, 1, timeout=_window()):
            continue
        try:
            count = cache.incr(key)
        except ValueError:  # entry expired between add() and incr()
            cache.set(key, 1, timeout=_window())
            continue
        if count >= _max_attempts():
            cache.touch(key, _cooldown())
            logger.warning("login lockout engaged", extra={"bucket": kind})


def reset_failures(username: str | None, ip: str | None) -> None:
    for kind, value in (("user", username), ("ip", ip)):
        if value:
            cache.delete(_bucket_key(kind, value))


def lockout_seconds_remaining(username: str | None, ip: str | None) -> int:
    """Approximate remaining cooldown, for the "try again later" message."""

    return _cooldown() if is_locked_out(username, ip) else 0


class LockoutModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        username = username or kwargs.get((getattr(self, "UserModel", None) and "username") or "username")
        ip = client_ip(request)

        if is_locked_out(username, ip):
            logger.warning("login attempt while locked out", extra={"path": getattr(request, "path", "")})
            return None

        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is None:
            register_failure(username, ip)
            return None

        reset_failures(username, ip)
        return user

    def user_can_authenticate(self, user):
        """Reject inactive users, matching ``ModelBackend`` but stated explicitly."""

        return super().user_can_authenticate(user)
