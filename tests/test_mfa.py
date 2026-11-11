"""Two-step verification: the algorithm, enrolment, the login gate and workspace policy."""

from __future__ import annotations

import time

import pytest
from django.core.cache import cache

from conftest import PASSWORD
from identity import mfa, policy, totp
from identity.models import RecoveryCode, TotpDevice
from kernel.crypto import DecryptionError, decrypt_text, encrypt_text, generate_key
from kernel.models import AuditLog

pytestmark = pytest.mark.django_db

# RFC 6238 Appendix B: the ASCII seed "12345678901234567890", base32 encoded.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def enroll(user):
    """Give ``user`` a confirmed device and return (secret, recovery_codes)."""

    _, secret = mfa.begin_enrollment(user)
    codes = mfa.confirm_enrollment(user, totp.code_at(secret))
    return secret, codes


def fresh_code(user, secret):
    """A code for a step the device has not accepted yet (enrolment consumed the current one)."""

    step = TotpDevice.objects.get(user=user).last_used_step + 1
    return totp.hotp(secret, step), step * totp.PERIOD_SECONDS


class TestTotpAlgorithm:
    @pytest.mark.parametrize(
        ("at", "expected"),
        [
            (59, "287082"),
            (1111111109, "081804"),
            (1111111111, "050471"),
            (1234567890, "005924"),
            (2000000000, "279037"),
            (20000000000, "353130"),
        ],
    )
    def test_matches_rfc_6238_vectors(self, at, expected):
        # The RFC lists 8-digit values; a 6-digit code is their last six digits.
        assert totp.code_at(RFC_SECRET, at) == expected

    def test_hotp_matches_rfc_4226_vectors(self):
        expected = ["755224", "287082", "359152", "969429", "338314", "254676"]
        assert [totp.hotp(RFC_SECRET, counter) for counter in range(6)] == expected

    def test_accepts_one_step_of_clock_drift(self):
        now = 1_700_000_000
        for drift in (-30, 0, 30):
            assert totp.matching_step(RFC_SECRET, totp.code_at(RFC_SECRET, now + drift), at=now)

    def test_rejects_two_steps_of_drift(self):
        now = 1_700_000_000
        assert totp.matching_step(RFC_SECRET, totp.code_at(RFC_SECRET, now + 61), at=now) is None

    def test_rejects_a_step_already_used(self):
        now = 1_700_000_000
        code = totp.code_at(RFC_SECRET, now)
        step = totp.matching_step(RFC_SECRET, code, at=now)
        assert totp.matching_step(RFC_SECRET, code, at=now, after_step=step) is None

    @pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34"])
    def test_rejects_malformed_codes(self, code):
        assert totp.matching_step(RFC_SECRET, code) is None

    def test_tolerates_spaces_in_a_valid_code(self):
        now = 1_700_000_000
        code = totp.code_at(RFC_SECRET, now)
        assert totp.matching_step(RFC_SECRET, f"{code[:3]} {code[3:]}", at=now)

    def test_generated_secrets_are_unique_160_bit_values(self):
        import base64

        secrets_ = {totp.generate_secret() for _ in range(50)}
        assert len(secrets_) == 50
        assert all(len(base64.b32decode(secret)) == 20 for secret in secrets_)

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri(RFC_SECRET, account="a b@example.com", issuer="Task Manager")
        assert uri.startswith("otpauth://totp/Task%20Manager%3Aa%20b%40example.com?")
        assert f"secret={RFC_SECRET}" in uri and "issuer=Task+Manager" in uri


class TestFieldEncryption:
    def test_round_trip(self):
        token = encrypt_text("seed-value")
        assert token != "seed-value" and decrypt_text(token) == "seed-value"

    def test_ciphertext_differs_each_time(self):
        assert encrypt_text("same") != encrypt_text("same")

    def test_tampered_ciphertext_is_refused(self):
        token = encrypt_text("seed-value")
        with pytest.raises(DecryptionError):
            decrypt_text(token[:-4] + "AAAA")

    def test_key_rotation_keeps_old_values_readable(self, settings):
        old, new = generate_key(), generate_key()
        settings.FIELD_ENCRYPTION_KEYS = [old]
        token = encrypt_text("seed-value")

        settings.FIELD_ENCRYPTION_KEYS = [new, old]
        assert decrypt_text(token) == "seed-value"

        settings.FIELD_ENCRYPTION_KEYS = [new]
        with pytest.raises(DecryptionError):
            decrypt_text(token)

    def test_the_seed_is_not_stored_in_plaintext(self, user):
        _, secret = mfa.begin_enrollment(user)
        stored = TotpDevice.objects.get(user=user).secret_encrypted
        assert secret not in stored

    def test_deploy_check_warns_without_a_dedicated_key(self, settings):
        from identity.checks import field_encryption_key_configured

        settings.FIELD_ENCRYPTION_KEYS = []
        assert [w.id for w in field_encryption_key_configured(None)] == ["identity.W001"]
        settings.FIELD_ENCRYPTION_KEYS = [generate_key()]
        assert field_encryption_key_configured(None) == []


class TestEnrolment:
    def test_device_is_inactive_until_confirmed(self, user):
        mfa.begin_enrollment(user)
        assert not mfa.user_has_mfa(user)

    def test_confirming_enables_and_issues_recovery_codes(self, user):
        _, codes = enroll(user)
        assert mfa.user_has_mfa(user)
        assert len(codes) == len(set(codes)) == mfa.RECOVERY_CODE_COUNT
        assert mfa.remaining_recovery_codes(user) == mfa.RECOVERY_CODE_COUNT

    def test_recovery_codes_are_stored_hashed(self, user):
        _, codes = enroll(user)
        stored = set(RecoveryCode.objects.filter(user=user).values_list("code_hash", flat=True))
        assert not stored & set(codes)
        assert not stored & {mfa.normalize_recovery_code(code) for code in codes}

    def test_wrong_code_does_not_confirm(self, user):
        mfa.begin_enrollment(user)
        with pytest.raises(mfa.MfaError):
            mfa.confirm_enrollment(user, "000000")
        assert not mfa.user_has_mfa(user)

    def test_restarting_setup_replaces_the_pending_seed(self, user):
        _, first = mfa.begin_enrollment(user)
        _, second = mfa.begin_enrollment(user)
        assert first != second
        with pytest.raises(mfa.MfaError):
            mfa.confirm_enrollment(user, totp.code_at(first))

    def test_cannot_start_setup_when_already_enabled(self, user):
        enroll(user)
        with pytest.raises(mfa.MfaError):
            mfa.begin_enrollment(user)

    def test_disable_removes_device_and_codes(self, user):
        enroll(user)
        mfa.disable(user)
        assert not mfa.user_has_mfa(user)
        assert not RecoveryCode.objects.filter(user=user).exists()


class TestVerification:
    def test_enrolment_code_cannot_be_replayed_at_login(self, user):
        _, secret = mfa.begin_enrollment(user)
        code = totp.code_at(secret)
        mfa.confirm_enrollment(user, code)
        assert mfa.verify_code(user, code) is None

    def test_a_fresh_code_is_accepted_once(self, user, monkeypatch):
        secret, _ = enroll(user)
        code, at = fresh_code(user, secret)
        monkeypatch.setattr(time, "time", lambda: at)
        assert mfa.verify_code(user, code) == "totp"
        assert mfa.verify_code(user, code) is None

    def test_recovery_code_works_once(self, user):
        _, codes = enroll(user)
        assert mfa.verify_code(user, codes[0]) == "recovery"
        assert mfa.verify_code(user, codes[0]) is None
        assert mfa.remaining_recovery_codes(user) == mfa.RECOVERY_CODE_COUNT - 1

    def test_recovery_code_is_case_and_dash_insensitive(self, user):
        _, codes = enroll(user)
        assert mfa.verify_code(user, codes[1].lower().replace("-", " ")) == "recovery"

    def test_another_users_recovery_code_is_refused(self, user, other_user):
        _, codes = enroll(user)
        enroll(other_user)
        assert mfa.verify_code(other_user, codes[0]) is None
        assert mfa.remaining_recovery_codes(user) == mfa.RECOVERY_CODE_COUNT

    def test_regenerating_invalidates_old_recovery_codes(self, user):
        _, old = enroll(user)
        new = mfa.regenerate_recovery_codes(user)
        assert mfa.verify_code(user, old[0]) is None
        assert mfa.verify_code(user, new[0]) == "recovery"

    def test_lockout_after_repeated_failures(self, user, settings):
        settings.MFA_MAX_FAILURES = 3
        _, codes = enroll(user)
        for _ in range(3):
            assert mfa.verify_code(user, "000000") is None
        # Even a correct code is refused while locked, so the lockout cannot be probed.
        with pytest.raises(mfa.MfaLocked):
            mfa.verify_code(user, codes[0])
        assert mfa.remaining_recovery_codes(user) == mfa.RECOVERY_CODE_COUNT

    def test_success_resets_the_failure_count(self, user, settings):
        settings.MFA_MAX_FAILURES = 3
        _, codes = enroll(user)
        mfa.verify_code(user, "000000")
        mfa.verify_code(user, "000000")
        assert mfa.verify_code(user, codes[0]) == "recovery"
        mfa.verify_code(user, "000000")
        mfa.verify_code(user, "000000")
        assert not mfa.is_locked(user)

    def test_user_without_a_device_never_verifies(self, user):
        assert mfa.verify_code(user, "123456") is None


class TestLoginFlow:
    def _login(self, client, user):
        return client.post("/accounts/login/", {"username": user.username, "password": PASSWORD})

    def test_login_without_mfa_is_unchanged(self, client, user, organization):
        response = self._login(client, user)
        assert response.status_code == 302 and response.url == "/"
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_password_alone_does_not_sign_in(self, client, user, organization):
        enroll(user)
        response = self._login(client, user)
        assert response.status_code == 302 and response.url == "/accounts/mfa/"
        assert "_auth_user_id" not in client.session
        # The half-finished login cannot reach anything that needs a session.
        assert client.get("/tasks/").status_code == 302
        assert client.get("/api/v1/tasks/").status_code in (401, 403)

    def test_correct_code_completes_the_login(self, client, user, organization, monkeypatch):
        secret, _ = enroll(user)
        self._login(client, user)
        code, at = fresh_code(user, secret)
        monkeypatch.setattr("identity.totp.time.time", lambda: at)

        response = client.post("/accounts/mfa/", {"code": code})

        assert response.status_code == 302 and response.url == "/"
        assert client.session.get("_auth_user_id") == str(user.pk)
        assert client.session.get(mfa.SESSION_VERIFIED_KEY) is True
        assert AuditLog.objects.filter(action="auth.login_succeeded", metadata__second_factor="totp").exists()

    def test_next_is_preserved_through_the_challenge(self, client, user, organization):
        _, codes = enroll(user)
        client.post("/accounts/login/?next=/board/", {"username": user.username, "password": PASSWORD, "next": "/board/"})
        response = client.post("/accounts/mfa/", {"code": codes[0]})
        assert response.url == "/board/"

    def test_wrong_code_is_rejected_and_audited(self, client, user, organization):
        enroll(user)
        self._login(client, user)
        response = client.post("/accounts/mfa/", {"code": "000000"})
        assert response.status_code == 400
        assert "_auth_user_id" not in client.session
        assert AuditLog.objects.filter(action="auth.mfa_failed").exists()

    def test_recovery_code_signs_in_and_warns(self, client, user, organization):
        _, codes = enroll(user)
        self._login(client, user)
        response = client.post("/accounts/mfa/", {"code": codes[0]}, follow=True)
        assert response.status_code == 200
        assert b"recovery code" in response.content

    def test_lockout_drops_the_pending_login(self, client, user, organization, settings):
        settings.MFA_MAX_FAILURES = 2
        _, codes = enroll(user)
        self._login(client, user)
        client.post("/accounts/mfa/", {"code": "000000"})
        client.post("/accounts/mfa/", {"code": "000000"})
        response = client.post("/accounts/mfa/", {"code": codes[0]})
        assert response.status_code == 429
        assert "_auth_user_id" not in client.session
        assert "mfa_pending" not in client.session

    def test_challenge_without_a_pending_login_redirects(self, client):
        assert client.get("/accounts/mfa/").url == "/accounts/login/"
        assert client.post("/accounts/mfa/", {"code": "123456"}).url == "/accounts/login/"

    def test_pending_login_expires(self, client, user, organization):
        _, codes = enroll(user)
        self._login(client, user)
        session = client.session
        session["mfa_pending"] = {**session["mfa_pending"], "started": time.time() - 3600}
        session.save()
        assert client.post("/accounts/mfa/", {"code": codes[0]}).url == "/accounts/login/"
        assert "_auth_user_id" not in client.session

    def test_open_redirect_is_refused(self, client, user, organization):
        _, codes = enroll(user)
        self._login(client, user)
        session = client.session
        session["mfa_pending"] = {**session["mfa_pending"], "next": "https://evil.example/"}
        session.save()
        assert client.post("/accounts/mfa/", {"code": codes[0]}).url == "/"

    def test_challenge_is_rate_limited(self, client, user, organization, settings):
        settings.API_THROTTLING_ENABLED = True
        cache.clear()
        enroll(user)
        self._login(client, user)
        statuses = [client.post("/accounts/mfa/", {"code": "000000"}).status_code for _ in range(14)]
        assert 429 in statuses

    def test_admin_login_cannot_bypass_the_second_factor(self, client, user):
        user.is_staff = user.is_superuser = True
        user.save()
        enroll(user)
        response = client.get("/admin/login/")
        assert response.status_code == 302 and "/accounts/login/" in response.url
        response = client.post("/admin/login/", {"username": user.username, "password": PASSWORD})
        assert response.status_code == 302 and "/accounts/login/" in response.url
        assert "_auth_user_id" not in client.session


class TestAccountSecurityPages:
    def test_requires_login(self, client):
        assert client.get("/account/security/").status_code == 302

    def test_setup_flow_end_to_end(self, client_owner, user):
        assert b"Set up two-step verification" in client_owner.get("/account/security/").content

        assert client_owner.post("/account/security/mfa/start/").url == "/account/security/mfa/setup/"
        page = client_owner.get("/account/security/mfa/setup/")
        assert page.status_code == 200 and b'src="/account/security/mfa/qr.svg"' in page.content
        assert "no-store" in page["Cache-Control"]

        qr = client_owner.get("/account/security/mfa/qr.svg")
        assert qr.status_code == 200 and qr["Content-Type"] == "image/svg+xml"
        assert b"<svg" in qr.content and "no-store" in qr["Cache-Control"]

        secret = TotpDevice.objects.get(user=user).secret
        done = client_owner.post("/account/security/mfa/setup/", {"code": totp.code_at(secret)})
        assert done.status_code == 200
        assert done.content.count(b"<li><code>") == mfa.RECOVERY_CODE_COUNT
        assert mfa.user_has_mfa(user)
        assert AuditLog.objects.filter(action="auth.mfa_enabled").exists()

    def test_setup_rejects_a_wrong_code(self, client_owner, user):
        client_owner.post("/account/security/mfa/start/")
        response = client_owner.post("/account/security/mfa/setup/", {"code": "000000"})
        assert response.status_code == 400 and not mfa.user_has_mfa(user)

    def test_setup_page_without_a_pending_device_redirects(self, client_owner):
        assert client_owner.get("/account/security/mfa/setup/").url == "/account/security/"

    def test_qr_code_exists_only_while_setup_is_pending(self, client_owner, user, client):
        assert client_owner.get("/account/security/mfa/qr.svg").status_code == 404
        enroll(user)
        assert client_owner.get("/account/security/mfa/qr.svg").status_code == 404

    def test_qr_code_requires_login(self, client):
        assert client.get("/account/security/mfa/qr.svg").status_code == 302

    def test_disable_requires_password_and_code(self, client_owner, user):
        _, codes = enroll(user)
        client_owner.post("/account/security/mfa/disable/", {"password": "wrong", "code": codes[0]})
        assert mfa.user_has_mfa(user)
        client_owner.post("/account/security/mfa/disable/", {"password": PASSWORD, "code": "000000"})
        assert mfa.user_has_mfa(user)
        client_owner.post("/account/security/mfa/disable/", {"password": PASSWORD, "code": codes[0]})
        assert not mfa.user_has_mfa(user)
        assert AuditLog.objects.filter(action="auth.mfa_disabled").exists()

    def test_regenerating_recovery_codes_requires_reauthentication(self, client_owner, user):
        _, codes = enroll(user)
        refused = client_owner.post("/account/security/mfa/recovery-codes/", {"password": "wrong", "code": codes[0]})
        assert refused.status_code == 302
        shown = client_owner.post("/account/security/mfa/recovery-codes/", {"password": PASSWORD, "code": codes[1]})
        assert shown.status_code == 200 and shown.content.count(b"<li><code>") == mfa.RECOVERY_CODE_COUNT

    @pytest.mark.parametrize(
        "path",
        ["/account/security/mfa/start/", "/account/security/mfa/disable/", "/account/security/mfa/recovery-codes/"],
    )
    def test_state_changes_reject_get(self, client_owner, path):
        assert client_owner.get(path).status_code == 405

    def test_account_deletion_removes_mfa_material(self, user, organization):
        from support.services import delete_account

        enroll(user)
        delete_account(user=user, delete_sole_owned_workspaces=True)
        assert not TotpDevice.objects.exists() and not RecoveryCode.objects.exists()


class TestWorkspaceMfaPolicy:
    def _require(self, organization):
        policy.set_policy_flag(organization, policy.REQUIRE_MFA_KEY, True)

    def test_member_without_mfa_is_sent_to_setup(self, client_member, organization):
        self._require(organization)
        response = client_member.get("/tasks/")
        assert response.status_code == 302 and response.url == "/account/security/"

    def test_enrolling_in_this_session_lifts_the_block(self, client_member, organization, member):
        self._require(organization)
        client_member.post("/account/security/mfa/start/")
        secret = TotpDevice.objects.get(user=member.user).secret
        client_member.post("/account/security/mfa/setup/", {"code": totp.code_at(secret)})
        assert client_member.get("/tasks/").status_code == 200

    def test_older_session_of_an_enrolled_user_must_step_up(self, client_member, organization, member):
        _, codes = enroll(member.user)
        self._require(organization)

        blocked = client_member.get("/tasks/")
        assert blocked.status_code == 302 and blocked.url.startswith("/accounts/mfa/?next=")

        stepped = client_member.post("/accounts/mfa/", {"code": codes[0], "next": "/tasks/"})
        assert stepped.url == "/tasks/"
        assert client_member.get("/tasks/").status_code == 200

    def test_api_session_calls_are_blocked_too(self, client_member, organization):
        self._require(organization)
        response = client_member.get("/api/v1/tasks/")
        assert response.status_code == 403
        assert "two-step" in response.json()["error"]["message"]

    def test_api_keys_are_exempt(self, client, organization, api_key):
        self._require(organization)
        _, token = api_key
        response = client.get("/api/v1/tasks/", headers={"Authorization": f"Api-Key {token}"})
        assert response.status_code == 200

    def test_htmx_requests_get_a_redirect_header(self, client_member, organization):
        self._require(organization)
        response = client_member.get("/tasks/", headers={"HX-Request": "true"})
        assert response.status_code == 403 and response["HX-Redirect"] == "/account/security/"

    def test_other_workspaces_are_unaffected(self, client, other_user, other_organization, organization):
        self._require(organization)
        client.force_login(other_user)
        assert client.get("/tasks/").status_code == 200

    def test_admin_must_have_mfa_before_requiring_it(self, client_owner, organization, user):
        client_owner.post("/settings/security/", {"form": "mfa", "require_mfa": "on"})
        organization.refresh_from_db()
        assert not policy.organization_requires_mfa(organization)

    def test_admin_with_mfa_can_require_and_relax_it(self, client_owner, organization, user):
        client_owner.post("/account/security/mfa/start/")
        secret = TotpDevice.objects.get(user=user).secret
        client_owner.post("/account/security/mfa/setup/", {"code": totp.code_at(secret)})

        client_owner.post("/settings/security/", {"form": "mfa", "require_mfa": "on"})
        organization.refresh_from_db()
        assert policy.organization_requires_mfa(organization)
        assert AuditLog.objects.filter(action="org.mfa_requirement_changed", metadata__required=True).exists()

        client_owner.post("/settings/security/", {"form": "mfa"})
        organization.refresh_from_db()
        assert not policy.organization_requires_mfa(organization)

    def test_members_cannot_change_the_policy(self, client_member, organization):
        response = client_member.post("/settings/security/", {"form": "mfa", "require_mfa": "on"})
        assert response.status_code == 403
        organization.refresh_from_db()
        assert not policy.organization_requires_mfa(organization)
