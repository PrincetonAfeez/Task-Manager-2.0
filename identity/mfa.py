"""Two-step verification: enrolment, verification, recovery codes and lockout."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from ops.logging import get_logger

from . import totp
from .models import RecoveryCode, TotpDevice, WebAuthnCredential

logger = get_logger("taskmanager.identity.mfa")

RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: these get read aloud
_RECOVERY_LENGTH = 16  # 16 symbols of 5 bits = 80 bits

#: Session key set once this session has passed a second factor (or enrolled one).
SESSION_VERIFIED_KEY = "mfa_verified"


class MfaError(Exception):
    """A user-facing two-step verification failure."""


class MfaLocked(MfaError):
    """Too many wrong codes; verification is refused until the lockout expires."""


def _max_failures() -> int:
    return getattr(settings, "MFA_MAX_FAILURES", 5)


def _lockout_seconds() -> int:
    return getattr(settings, "MFA_LOCKOUT_SECONDS", 900)


def _failure_key(user) -> str:
    return f"mfa:failures:{user.pk}"


def is_locked(user) -> bool:
    return (cache.get(_failure_key(user)) or 0) >= _max_failures()


def _register_failure(user) -> None:
    key = _failure_key(user)
    # add() then incr() keeps the counter atomic on Redis; the timeout is set once, so the
    # window is measured from the first failure and cannot be extended by further guesses.
    if not cache.add(key, 1, timeout=_lockout_seconds()):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=_lockout_seconds())


def _clear_failures(user) -> None:
    cache.delete(_failure_key(user))


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def _hash_recovery_code(code: str) -> str:
    # Keyed, so a leaked table cannot be brute-forced without the application secret too.
    return hmac.new(
        settings.SECRET_KEY.encode(), normalize_recovery_code(code).encode(), hashlib.sha256
    ).hexdigest()


def _format_recovery_code(raw: str) -> str:
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def user_has_mfa(user) -> bool:
    if not getattr(user, "pk", None):
        return False
    if TotpDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
        return True
    return WebAuthnCredential.objects.filter(user=user).exists()


def remaining_recovery_codes(user) -> int:
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def begin_enrollment(user) -> tuple[TotpDevice, str]:
    """Create (or replace) an unconfirmed device and return it with its plaintext seed."""

    existing = TotpDevice.objects.filter(user=user).first()
    if existing is not None and existing.is_confirmed:
        raise MfaError("Two-step verification is already enabled.")

    secret = totp.generate_secret()
    device = existing or TotpDevice(user=user)
    device.secret = secret
    device.confirmed_at = None
    device.last_used_step = 0
    device.save()
    return device, secret


def pending_device(user) -> TotpDevice | None:
    return TotpDevice.objects.filter(user=user, confirmed_at__isnull=True).first()


@transaction.atomic
def _issue_recovery_codes(user) -> list[str]:
    RecoveryCode.objects.filter(user=user).delete()
    codes = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    RecoveryCode.objects.bulk_create(
        [RecoveryCode(user=user, code_hash=_hash_recovery_code(code)) for code in codes]
    )
    return [_format_recovery_code(code) for code in codes]


def confirm_enrollment(user, code: str) -> list[str]:
    """Prove the authenticator works, switch the device on, and return recovery codes.

    The recovery codes are returned exactly once, here; only their hashes are stored.
    """

    device = pending_device(user)
    if device is None:
        raise MfaError("Start setup again; there is no pending authenticator.")
    if is_locked(user):
        raise MfaLocked("Too many incorrect codes. Try again later.")

    step = totp.matching_step(device.secret, code)
    if step is None:
        _register_failure(user)
        raise MfaError("That code is not correct. Check the time on your device and try again.")

    device.confirmed_at = timezone.now()
    device.last_used_step = step
    device.save(update_fields=["confirmed_at", "last_used_step", "updated_at"])
    _clear_failures(user)
    return _issue_recovery_codes(user)


def verify_code(user, code: str) -> str | None:
    """Check a login code. Returns ``"totp"``, ``"recovery"`` or ``None``.

    Raises :class:`MfaLocked` when the account has used up its attempts — including when
    the code offered would have been correct, so a lockout cannot be probed.
    """

    if is_locked(user):
        raise MfaLocked("Too many incorrect codes. Try again later.")

    device = TotpDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        return None

    step = totp.matching_step(device.secret, code, after_step=device.last_used_step)
    if step is not None:
        # Conditional update: of two concurrent requests replaying one code, exactly one wins.
        won = TotpDevice.objects.filter(pk=device.pk, last_used_step__lt=step).update(last_used_step=step)
        if won:
            _clear_failures(user)
            return "totp"

    if len(normalize_recovery_code(code)) == _RECOVERY_LENGTH:
        spent = RecoveryCode.objects.filter(
            user=user, code_hash=_hash_recovery_code(code), used_at__isnull=True
        ).update(used_at=timezone.now())
        if spent:
            _clear_failures(user)
            logger.warning("recovery code used", extra={"user_id": user.pk})
            return "recovery"

    _register_failure(user)
    return None


def regenerate_recovery_codes(user) -> list[str]:
    if not user_has_mfa(user):
        raise MfaError("Enable two-step verification first.")
    return _issue_recovery_codes(user)


@transaction.atomic
def disable(user) -> None:
    TotpDevice.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()
    WebAuthnCredential.objects.filter(user=user).delete()
    _clear_failures(user)
