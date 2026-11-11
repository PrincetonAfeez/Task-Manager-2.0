"""Time-based one-time passwords (RFC 6238) on top of HOTP (RFC 4226).

Implemented directly on the standard library: the algorithm is thirty lines, and the RFC
publishes test vectors, which the test suite checks this module against.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD_SECONDS = 30
#: Steps accepted either side of "now". One step tolerates a phone clock that is up to
#: thirty seconds out without widening the guessing window much (3 valid codes in 10^6).
DRIFT_STEPS = 1


def generate_secret() -> str:
    """160 bits, the size RFC 4226 recommends, base32 encoded for authenticator apps."""

    return base64.b32encode(secrets.token_bytes(20)).decode()


def _key(secret: str) -> bytes:
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


def hotp(secret: str, counter: int, *, digits: int = DIGITS, algorithm: str = "sha1") -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def current_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // PERIOD_SECONDS)


def code_at(secret: str, at: float | None = None) -> str:
    return hotp(secret, current_step(at))


def matching_step(
    secret: str, code: str, *, at: float | None = None, after_step: int = 0
) -> int | None:
    """Return the time step ``code`` is valid for, or ``None``.

    Steps at or before ``after_step`` are refused: a code that has been accepted once must
    not be accepted again, or anyone who watched it being typed has thirty seconds to
    replay it.
    """

    candidate = "".join(ch for ch in str(code) if ch.isdigit())
    if len(candidate) != DIGITS:
        return None

    now = current_step(at)
    matched = None
    # Every candidate step is compared, with a constant-time comparison, so response time
    # does not reveal which step (if any) matched.
    for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
        if hmac.compare_digest(hotp(secret, step), candidate) and step > after_step:
            matched = step
    return matched


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI authenticator apps read from a QR code."""

    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"
