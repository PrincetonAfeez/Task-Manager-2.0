"""WebAuthn passkey registration, authentication, and MFA integration."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client
from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
from webauthn.helpers.bytes_to_base64url import bytes_to_base64url
from webauthn.helpers.structs import AttestationFormat, CredentialDeviceType, PublicKeyCredentialType
from webauthn.registration.verify_registration_response import VerifiedRegistration

from conftest import PASSWORD
from identity import mfa
from identity.models import WebAuthnCredential

pytestmark = pytest.mark.django_db

FAKE_CRED_ID = b"test-credential-id-bytes!!"
FAKE_PUBKEY = b"fake-public-key-bytes-here"
CRED_B64 = bytes_to_base64url(FAKE_CRED_ID)
FAKE_CREDENTIAL_JSON = {
    "id": CRED_B64,
    "rawId": CRED_B64,
    "type": "public-key",
    "response": {
        "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0a.g.geti",
        "authenticatorData": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MFAAAAAA",
        "signature": "MEUCIQ",
        "attestationObject": "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YVg",
    },
    "transports": ["internal"],
}


def _verified_registration() -> VerifiedRegistration:
    return VerifiedRegistration(
        credential_id=FAKE_CRED_ID,
        credential_public_key=FAKE_PUBKEY,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"attest",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def _verified_authentication() -> VerifiedAuthentication:
    return VerifiedAuthentication(
        credential_id=FAKE_CRED_ID,
        new_sign_count=1,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
        user_verified=True,
    )


@pytest.fixture
def authed_client(client, user) -> Client:
    client.force_login(user)
    return client


class TestRegistration:
    def test_register_creates_credential(self, authed_client, user):
        begin = authed_client.post("/account/security/webauthn/register/begin/")
        assert begin.status_code == 200
        assert "options" in begin.json()

        with patch(
            "identity.webauthn_service.verify_registration_response",
            return_value=_verified_registration(),
        ):
            finish = authed_client.post(
                "/account/security/webauthn/register/finish/",
                data=json.dumps({"credential": FAKE_CREDENTIAL_JSON, "label": "Laptop"}),
                content_type="application/json",
            )

        assert finish.status_code == 200 and finish.json().get("ok") is True
        row = WebAuthnCredential.objects.get(user=user)
        assert row.label == "Laptop"
        assert bytes(row.credential_id) == FAKE_CRED_ID
        assert mfa.user_has_mfa(user)


class TestAuthentication:
    def _pending_login(self, client, user):
        return client.post("/accounts/login/", {"username": user.username, "password": PASSWORD})

    def test_auth_sets_session_verified(self, client, user, organization):
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=FAKE_CRED_ID,
            public_key=FAKE_PUBKEY,
            sign_count=0,
            label="Test",
        )
        self._pending_login(client, user)
        assert client.post("/accounts/mfa/webauthn/begin/").status_code == 200

        with patch(
            "identity.webauthn_service.verify_authentication_response",
            return_value=_verified_authentication(),
        ):
            finish = client.post(
                "/accounts/mfa/webauthn/finish/",
                data=json.dumps({"credential": FAKE_CREDENTIAL_JSON}),
                content_type="application/json",
            )

        assert finish.status_code == 200
        assert finish.json()["redirect"] == "/"
        assert client.session.get(mfa.SESSION_VERIFIED_KEY) is True
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_user_without_credentials_cannot_begin(self, client, user, organization):
        from tests.test_mfa import enroll

        enroll(user)
        self._pending_login(client, user)
        response = client.post("/accounts/mfa/webauthn/begin/")
        assert response.status_code == 400
        assert "No passkeys" in response.json()["error"]

    def test_cross_user_credential_is_rejected(self, client, user, other_user, organization):
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=FAKE_CRED_ID,
            public_key=FAKE_PUBKEY,
            sign_count=0,
            label="Owner key",
        )
        WebAuthnCredential.objects.create(
            user=other_user,
            credential_id=b"other-users-credential-id!!!!",
            public_key=FAKE_PUBKEY,
            sign_count=0,
            label="Intruder key",
        )
        self._pending_login(client, other_user)
        assert client.post("/accounts/mfa/webauthn/begin/").status_code == 200

        with patch("identity.webauthn_service.verify_authentication_response") as verify:
            finish = client.post(
                "/accounts/mfa/webauthn/finish/",
                data=json.dumps({"credential": FAKE_CREDENTIAL_JSON}),
                content_type="application/json",
            )
        assert finish.status_code == 400
        verify.assert_not_called()
