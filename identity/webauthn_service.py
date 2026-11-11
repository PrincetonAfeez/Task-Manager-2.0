"""WebAuthn passkey registration and authentication ceremonies."""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from webauthn.authentication.generate_authentication_options import generate_authentication_options
from webauthn.authentication.verify_authentication_response import verify_authentication_response
from webauthn.helpers import base64url_to_bytes, options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.generate_registration_options import generate_registration_options
from webauthn.registration.verify_registration_response import verify_registration_response

from .models import WebAuthnCredential

SESSION_REG_CHALLENGE_KEY = "webauthn_reg_challenge"
SESSION_AUTH_CHALLENGE_KEY = "webauthn_auth_challenge"


class WebAuthnError(Exception):
    """A user-facing WebAuthn failure."""


def _encode_challenge(challenge: bytes) -> str:
    return base64.urlsafe_b64encode(challenge).decode().rstrip("=")


def _decode_challenge(stored: str) -> bytes:
    if not stored:
        raise WebAuthnError("The ceremony expired. Start again.")
    pad = "=" * (-len(stored) % 4)
    return base64.urlsafe_b64decode(stored + pad)


def rp_id_and_origin() -> tuple[str, str]:
    parsed = urlparse(settings.SITE_URL)
    if not parsed.scheme or not parsed.hostname:
        raise WebAuthnError("SITE_URL must be a full URL with a host.")
    return parsed.hostname, settings.SITE_URL


def webauthn_user_handle(user) -> bytes:
    #: Opaque and stable per account so re-registration does not mint a new handle.
    digest = hashlib.sha256(f"webauthn-user:{user.pk}:{settings.SECRET_KEY}".encode()).digest()
    return digest[:32]


def user_has_passkeys(user) -> bool:
    if not getattr(user, "pk", None):
        return False
    return WebAuthnCredential.objects.filter(user=user).exists()


def _descriptors(user) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(
            id=bytes(row.credential_id),
            transports=row.transports or None,
        )
        for row in WebAuthnCredential.objects.filter(user=user)
    ]


def begin_registration(request, user) -> dict[str, Any]:
    rp_id, _ = rp_id_and_origin()
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=getattr(settings, "MFA_ISSUER", "Task Manager"),
        user_name=user.email or user.get_username(),
        user_id=webauthn_user_handle(user),
        user_display_name=user.get_username(),
        exclude_credentials=_descriptors(user) or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request.session[SESSION_REG_CHALLENGE_KEY] = _encode_challenge(options.challenge)
    request.session.modified = True
    return options_to_json_dict(options)


@transaction.atomic
def finish_registration(request, user, credential: dict[str, Any], label: str) -> WebAuthnCredential:
    rp_id, origin = rp_id_and_origin()
    challenge = _decode_challenge(request.session.pop(SESSION_REG_CHALLENGE_KEY, ""))

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=False,
        )
    except Exception as exc:
        raise WebAuthnError("This passkey could not be verified.") from exc

    if WebAuthnCredential.objects.filter(credential_id=verified.credential_id).exists():
        raise WebAuthnError("This passkey is already registered.")

    clean_label = (label or "Passkey").strip()[:255] or "Passkey"
    transports = credential.get("transports")
    if transports is None:
        transports = []

    return WebAuthnCredential.objects.create(
        user=user,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        label=clean_label,
        transports=list(transports),
    )


def begin_authentication(request, user) -> dict[str, Any]:
    rp_id, _ = rp_id_and_origin()
    allow = _descriptors(user)
    if not allow:
        raise WebAuthnError("No passkeys are registered for this account.")

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session[SESSION_AUTH_CHALLENGE_KEY] = _encode_challenge(options.challenge)
    request.session.modified = True
    return options_to_json_dict(options)


def _credential_id_from_payload(credential: dict[str, Any]) -> bytes:
    raw_id = credential.get("rawId") or credential.get("id")
    if raw_id is None:
        raise WebAuthnError("Malformed passkey response.")
    if isinstance(raw_id, str):
        return base64url_to_bytes(raw_id)
    return bytes(raw_id)


@transaction.atomic
def finish_authentication(request, user, credential: dict[str, Any]) -> WebAuthnCredential:
    rp_id, origin = rp_id_and_origin()
    challenge = _decode_challenge(request.session.pop(SESSION_AUTH_CHALLENGE_KEY, ""))
    cred_id = _credential_id_from_payload(credential)

    device = WebAuthnCredential.objects.select_for_update().filter(credential_id=cred_id).first()
    if device is None or device.user_id != user.pk:
        raise WebAuthnError("That passkey does not belong to this account.")

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(device.public_key),
            credential_current_sign_count=device.sign_count,
            require_user_verification=False,
        )
    except Exception as exc:
        raise WebAuthnError("Passkey verification failed.") from exc

    device.sign_count = verified.new_sign_count
    device.save(update_fields=["sign_count", "updated_at"])
    return device
