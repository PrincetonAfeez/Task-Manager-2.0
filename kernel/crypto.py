"""Encryption for secrets that must be read back.

Passwords, API keys and recovery codes are hashed, because the server only ever needs to
compare them. A TOTP seed or an OIDC client secret is different: the server has to *use*
the original value, so it is encrypted instead, and a database dump alone is not enough to
generate codes or impersonate the workspace to its identity provider.

Keys come from ``FIELD_ENCRYPTION_KEYS`` (comma separated, newest first). Every key can
decrypt; only the first encrypts — that is what makes rotation possible without downtime.
When the setting is empty a key is derived from ``SECRET_KEY``, which is convenient for
development but means rotating ``SECRET_KEY`` invalidates every stored secret; the
``identity`` deploy check warns about that configuration in production.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


class DecryptionError(ValueError):
    """The stored value could not be decrypted with any configured key."""


def _derived_key() -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"taskmanager.field-encryption.v1"
    )
    return base64.urlsafe_b64encode(hkdf.derive(settings.SECRET_KEY.encode()))


@lru_cache(maxsize=4)
def _cipher(configured: tuple[str, ...], secret_key: str) -> MultiFernet:
    keys = [key.encode() for key in configured] or [_derived_key()]
    return MultiFernet([Fernet(key) for key in keys])


def _current() -> MultiFernet:
    configured = tuple(getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or ())
    return _cipher(configured, settings.SECRET_KEY)


def encrypt_text(value: str) -> str:
    return _current().encrypt(value.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return _current().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        # Deliberately no detail: the token is a secret-bearing value and must not be logged.
        raise DecryptionError("Stored secret could not be decrypted.") from exc


def generate_key() -> str:
    """A fresh key suitable for ``FIELD_ENCRYPTION_KEYS``."""

    return Fernet.generate_key().decode()
