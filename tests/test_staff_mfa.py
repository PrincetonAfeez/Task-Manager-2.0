"""Platform staff must enrol / pass TOTP when STAFF_REQUIRE_MFA is on."""

from __future__ import annotations

import pytest
from django.urls import reverse

from identity import mfa, totp
from identity.mfa import SESSION_VERIFIED_KEY

pytestmark = pytest.mark.django_db


class TestStaffMfa:
    def test_staff_without_mfa_is_sent_to_enrol(self, client, user, settings):
        settings.STAFF_REQUIRE_MFA = True
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        client.force_login(user)
        response = client.get("/")
        assert response.status_code == 302
        assert reverse("account_security") in response["Location"]

    def test_staff_with_verified_session_passes(self, client, user, settings, organization):
        settings.STAFF_REQUIRE_MFA = True
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        _, secret = mfa.begin_enrollment(user)
        mfa.confirm_enrollment(user, totp.code_at(secret))
        client.force_login(user)
        session = client.session
        session[SESSION_VERIFIED_KEY] = True
        session.save()
        response = client.get("/")
        assert response.status_code == 200

    def test_non_staff_unaffected(self, client_owner, settings):
        settings.STAFF_REQUIRE_MFA = True
        response = client_owner.get("/")
        assert response.status_code == 200
