"""OpenID Connect single sign-on, exercised against an in-process fake identity provider.

The fake signs real RS256 tokens with a real key, so signature, issuer, audience, expiry
and nonce checks are all exercised for real; only the HTTP transport is replaced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core.cache import cache

from billing.models import Plan, Subscription
from billing.services import ensure_default_plans
from conftest import PASSWORD
from identity import oidc, policy
from identity.models import SsoConnection, SsoIdentity
from kernel.models import AuditLog, Membership
from kernel.roles import Role

pytestmark = pytest.mark.django_db

User = get_user_model()
ISSUER = "https://idp.example.test"
CLIENT_ID = "task-manager-client"
CLIENT_SECRET = "s3cret-from-the-idp"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeProvider:
    """Stands in for the provider's discovery, JWKS and token endpoints."""

    def __init__(self, key):
        self.key = key
        self.kid = "key-1"
        self.claims: dict = {}
        self.token_requests: list[dict] = []
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }
        self.id_token_override: str | None = None
        self.sign_with = key
        self.algorithm = "RS256"

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def id_token(self):
        if self.id_token_override is not None:
            return self.id_token_override
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **self.claims}
        return jwt.encode(claims, self.sign_with, algorithm=self.algorithm, headers={"kid": self.kid})

    def fetch(self, url):
        if url.endswith("/.well-known/openid-configuration"):
            return dict(self.discovery)
        if url == self.discovery["jwks_uri"]:
            return self.jwks()
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data):
        assert url == self.discovery["token_endpoint"]
        self.token_requests.append(dict(data))
        return {"access_token": "at", "token_type": "Bearer", "id_token": self.id_token()}


@pytest.fixture
def idp(monkeypatch, signing_key):
    cache.clear()
    provider = FakeProvider(signing_key)
    monkeypatch.setattr(oidc, "_fetch_json", provider.fetch)
    monkeypatch.setattr(oidc, "_post_form", provider.post)
    return provider


@pytest.fixture
def business(organization):
    ensure_default_plans()
    Subscription.objects.update_or_create(
        organization=organization,
        defaults={"plan": Plan.objects.get(code="business"), "status": Subscription.Status.ACTIVE},
    )
    return organization


@pytest.fixture
def sso(business):
    connection = SsoConnection(
        organization=business,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        allowed_domains=["example.com"],
        is_enabled=True,
    )
    connection.client_secret = CLIENT_SECRET
    connection.save()
    return connection


def start(client, connection, next_url=""):
    """Begin a login; return the provider-bound query parameters."""

    suffix = f"?next={next_url}" if next_url else ""
    response = client.get(f"/sso/{connection.organization.slug}/{suffix}")
    assert response.status_code == 302, response.content
    return {key: values[0] for key, values in parse_qs(urlsplit(response.url).query).items()}


def sign_in(client, connection, idp, *, sub="idp|1", email="new.person@example.com", verified=True, **extra):
    params = start(client, connection)
    idp.claims = {"sub": sub, "email": email, "email_verified": verified, "nonce": params["nonce"], **extra}
    return client.get("/sso/callback/", {"code": "auth-code", "state": params["state"]})


def signed_in(client) -> bool:
    return "_auth_user_id" in client.session


class TestAuthorizationRequest:
    def test_redirects_to_the_provider_with_pkce_state_and_nonce(self, client, sso, idp, settings):
        response = client.get(f"/sso/{sso.organization.slug}/")
        assert response.url.startswith(f"{ISSUER}/authorize?")
        params = {k: v[0] for k, v in parse_qs(urlsplit(response.url).query).items()}
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["redirect_uri"] == settings.SITE_URL + "/sso/callback/"
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        assert len(params["state"]) >= 32 and len(params["nonce"]) >= 32
        assert CLIENT_SECRET not in response.url

    def test_each_attempt_gets_fresh_state_and_nonce(self, client, sso, idp):
        first, second = start(client, sso), start(client, sso)
        assert first["state"] != second["state"] and first["nonce"] != second["nonce"]

    def test_token_request_proves_possession_of_the_verifier(self, client, sso, idp):
        params = start(client, sso)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        client.get("/sso/callback/", {"code": "the-code", "state": params["state"]})

        sent = idp.token_requests[0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest())
        assert challenge.rstrip(b"=").decode() == params["code_challenge"]
        assert sent["code"] == "the-code" and sent["grant_type"] == "authorization_code"
        assert sent["client_secret"] == CLIENT_SECRET

    def test_unknown_or_disabled_workspace_is_404(self, client, sso, idp):
        assert client.get("/sso/no-such-workspace/").status_code == 404
        SsoConnection.objects.update(is_enabled=False)
        assert client.get(f"/sso/{sso.organization.slug}/").status_code == 404

    def test_deleted_workspace_is_404(self, client, sso, idp):
        from django.utils import timezone

        type(sso.organization).objects.filter(pk=sso.organization.pk).update(deleted_at=timezone.now())
        assert client.get(f"/sso/{sso.organization.slug}/").status_code == 404


class TestProvisioningAndLinking:
    def test_first_sign_in_creates_account_and_membership(self, client, sso, idp):
        response = sign_in(client, sso, idp, given_name="New", family_name="Person")

        assert response.status_code == 302 and response.url == "/"
        user = User.objects.get(email="new.person@example.com")
        assert not user.has_usable_password()
        assert (user.first_name, user.last_name) == ("New", "Person")
        membership = Membership.objects.get(organization=sso.organization, user=user)
        assert membership.role == Role.MEMBER
        assert client.session["_auth_user_id"] == str(user.pk)
        assert client.session[policy.SESSION_SSO_ORGS_KEY] == [sso.organization.pk]
        assert AuditLog.objects.filter(action="auth.sso_login", metadata__provisioned=True).exists()
        assert client.get("/tasks/").status_code == 200

    def test_new_members_get_the_configured_role(self, client, sso, idp):
        SsoConnection.objects.update(default_role=Role.VIEWER)
        sign_in(client, sso, idp)
        assert Membership.objects.get(user__email="new.person@example.com").role == Role.VIEWER

    def test_returning_user_is_matched_on_subject_not_email(self, client, sso, idp):
        sign_in(client, sso, idp, sub="idp|7", email="before@example.com")
        client.logout()
        sign_in(client, sso, idp, sub="idp|7", email="after@example.com")

        assert User.objects.filter(email="before@example.com").count() == 1
        assert not User.objects.filter(email="after@example.com").exists()
        assert SsoIdentity.objects.get(subject="idp|7").email == "after@example.com"

    def test_existing_member_with_the_same_email_is_linked(self, client, sso, idp, member):
        member.user.email = "member@example.com"
        member.user.save()
        sign_in(client, sso, idp, sub="idp|m", email="Member@Example.com")

        assert client.session["_auth_user_id"] == str(member.user.pk)
        assert SsoIdentity.objects.get(subject="idp|m").user == member.user
        assert User.objects.filter(email__iexact="member@example.com").count() == 1

    def test_account_outside_the_workspace_is_never_taken_over(self, client, sso, idp, other_user):
        """The provider vouching for an address must not hand over an unrelated account."""

        other_user.email = "intruder@example.com"
        other_user.save()
        response = sign_in(client, sso, idp, email="intruder@example.com")

        assert response.url == "/accounts/login/" and not signed_in(client)
        assert not Membership.objects.filter(organization=sso.organization, user=other_user).exists()
        assert not SsoIdentity.objects.exists()
        assert AuditLog.objects.filter(action="auth.sso_failed").exists()

    def test_a_second_subject_cannot_claim_a_linked_account(self, client, sso, idp):
        sign_in(client, sso, idp, sub="idp|1", email="one@example.com")
        client.logout()
        sign_in(client, sso, idp, sub="idp|2", email="one@example.com")
        assert not signed_in(client)
        assert SsoIdentity.objects.count() == 1

    def test_auto_provisioning_can_be_switched_off(self, client, sso, idp):
        SsoConnection.objects.update(auto_provision=False)
        sign_in(client, sso, idp)
        assert not signed_in(client) and not User.objects.filter(email="new.person@example.com").exists()

    def test_suspended_member_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp)
        client.logout()
        Membership.objects.filter(user__email="new.person@example.com").update(status=Membership.Status.SUSPENDED)
        sign_in(client, sso, idp)
        assert not signed_in(client)

    def test_deactivated_account_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp)
        client.logout()
        User.objects.filter(email="new.person@example.com").update(is_active=False)
        sign_in(client, sso, idp)
        assert not signed_in(client)

    def test_member_limit_is_enforced(self, client, organization, idp, user):
        # Free plan: three members. Fill it, then try to arrive through SSO.
        connection = SsoConnection(organization=organization, issuer=ISSUER, client_id=CLIENT_ID,
                                   allowed_domains=["example.com"], is_enabled=True)
        connection.client_secret = CLIENT_SECRET
        connection.save()
        for index in range(2):
            extra = User.objects.create_user(username=f"filler{index}", password=PASSWORD)
            Membership.objects.create(organization=organization, user=extra, role=Role.MEMBER)

        sign_in(client, connection, idp)

        assert not signed_in(client)
        assert not User.objects.filter(email="new.person@example.com").exists()

    def test_usernames_do_not_collide(self, client, sso, idp):
        User.objects.create_user(username="new.person", password=PASSWORD)
        sign_in(client, sso, idp)
        created = User.objects.get(email="new.person@example.com")
        assert created.username != "new.person" and created.username.startswith("new.person")


class TestClaimValidation:
    def test_domain_outside_the_allow_list_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp, email="someone@elsewhere.org")
        assert not signed_in(client) and not User.objects.filter(email="someone@elsewhere.org").exists()

    def test_lookalike_domain_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp, email="someone@example.com.evil.org")
        assert not signed_in(client)

    def test_unverified_email_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp, verified=False)
        assert not signed_in(client)

    def test_unverified_email_can_be_trusted_explicitly(self, client, sso, idp):
        SsoConnection.objects.update(trust_unverified_email=True)
        sign_in(client, sso, idp, verified=False)
        assert signed_in(client)

    def test_missing_email_is_refused(self, client, sso, idp):
        sign_in(client, sso, idp, email="")
        assert not signed_in(client)


class TestTokenVerification:
    def _attempt(self, client, sso, idp, **claims):
        params = start(client, sso)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"], **claims}
        return params

    def _callback(self, client, params):
        return client.get("/sso/callback/", {"code": "c", "state": params["state"]})

    def test_wrong_nonce_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp, nonce="someone-elses-nonce")
        self._callback(client, params)
        assert not signed_in(client)

    def test_wrong_audience_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp, aud="another-application")
        self._callback(client, params)
        assert not signed_in(client)

    def test_wrong_issuer_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp, iss="https://evil.example.test")
        self._callback(client, params)
        assert not signed_in(client)

    def test_expired_token_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp, exp=int(time.time()) - 3600)
        self._callback(client, params)
        assert not signed_in(client)

    def test_token_signed_by_another_key_is_refused(self, client, sso, idp, other_key):
        params = self._attempt(client, sso, idp)
        idp.sign_with = other_key
        self._callback(client, params)
        assert not signed_in(client)

    def test_unsigned_token_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp)
        now = int(time.time())
        idp.id_token_override = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **idp.claims},
            key=None, algorithm="none",
        )
        self._callback(client, params)
        assert not signed_in(client)

    def test_hmac_token_keyed_with_the_client_secret_is_refused(self, client, sso, idp):
        """Algorithm confusion: a symmetric signature must never satisfy an asymmetric check."""

        params = self._attempt(client, sso, idp)
        now = int(time.time())
        idp.id_token_override = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **idp.claims},
            CLIENT_SECRET.ljust(32, "x"), algorithm="HS256", headers={"kid": idp.kid},
        )
        self._callback(client, params)
        assert not signed_in(client)

    def test_unknown_key_id_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp)
        token = idp.id_token()
        idp.kid = "rotated-away"
        idp.id_token_override = token
        self._callback(client, params)
        assert not signed_in(client)

    def test_garbage_token_is_refused(self, client, sso, idp):
        params = self._attempt(client, sso, idp)
        idp.id_token_override = "not.a.jwt"
        self._callback(client, params)
        assert not signed_in(client)

    def test_missing_id_token_is_refused(self, client, sso, idp, monkeypatch):
        params = self._attempt(client, sso, idp)
        monkeypatch.setattr(oidc, "_post_form", lambda url, data: {"access_token": "at"})
        self._callback(client, params)
        assert not signed_in(client)


class TestCallbackBinding:
    def test_state_mismatch_is_refused(self, client, sso, idp):
        params = start(client, sso)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        client.get("/sso/callback/", {"code": "c", "state": "forged"})
        assert not signed_in(client)

    def test_callback_without_a_started_flow_is_refused(self, client, sso, idp):
        client.get("/sso/callback/", {"code": "c", "state": "anything"})
        assert not signed_in(client)

    def test_a_callback_cannot_be_replayed(self, client, sso, idp):
        params = start(client, sso)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        client.get("/sso/callback/", {"code": "c", "state": params["state"]})
        client.logout()
        client.get("/sso/callback/", {"code": "c", "state": params["state"]})
        assert not signed_in(client)

    def test_flow_started_in_another_browser_is_refused(self, client, sso, idp):
        from django.test import Client

        params = start(client, sso)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        victim = Client()
        victim.get("/sso/callback/", {"code": "c", "state": params["state"]})
        assert not signed_in(victim)

    def test_stale_flow_is_refused(self, client, sso, idp):
        params = start(client, sso)
        session = client.session
        session[oidc.SESSION_FLOW_KEY] = {**session[oidc.SESSION_FLOW_KEY], "started": time.time() - 3600}
        session.save()
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        client.get("/sso/callback/", {"code": "c", "state": params["state"]})
        assert not signed_in(client)

    def test_provider_error_is_reported_and_audited(self, client, sso, idp):
        start(client, sso)
        response = client.get("/sso/callback/", {"error": "access_denied"})
        assert response.url == "/accounts/login/" and not signed_in(client)
        assert AuditLog.objects.filter(action="auth.sso_failed", metadata__reason="provider_error").exists()

    def test_next_is_honoured_only_when_local(self, client, sso, idp):
        params = start(client, sso, next_url="/board/")
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        assert client.get("/sso/callback/", {"code": "c", "state": params["state"]}).url == "/board/"

        client.logout()
        params = start(client, sso, next_url="https://evil.example/")
        idp.claims["nonce"] = params["nonce"]
        assert client.get("/sso/callback/", {"code": "c", "state": params["state"]}).url == "/"

    def test_connection_disabled_mid_flow_is_refused(self, client, sso, idp):
        params = start(client, sso)
        SsoConnection.objects.update(is_enabled=False)
        idp.claims = {"sub": "s", "email": "a@example.com", "email_verified": True, "nonce": params["nonce"]}
        client.get("/sso/callback/", {"code": "c", "state": params["state"]})
        assert not signed_in(client)


class TestDiscoveryAndProviderUrls:
    def test_issuer_mismatch_is_refused(self, sso, idp):
        idp.discovery["issuer"] = "https://someone-else.example.test"
        with pytest.raises(oidc.SsoError):
            oidc.discover(ISSUER, use_cache=False)

    @pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_incomplete_configuration_is_refused(self, sso, idp, field):
        del idp.discovery[field]
        with pytest.raises(oidc.SsoError):
            oidc.discover(ISSUER, use_cache=False)

    def test_plain_http_endpoints_are_refused(self, sso, idp):
        idp.discovery["token_endpoint"] = "http://idp.example.test/token"
        with pytest.raises(oidc.SsoError):
            oidc.discover(ISSUER, use_cache=False)

    def test_discovery_is_cached(self, sso, idp, monkeypatch):
        calls = []
        monkeypatch.setattr(oidc, "_fetch_json", lambda url: calls.append(url) or dict(idp.discovery))
        oidc.discover(ISSUER)
        oidc.discover(ISSUER)
        assert len(calls) == 1

    def test_http_provider_url_is_refused(self):
        with pytest.raises(oidc.SsoError):
            oidc.check_provider_url("http://idp.example.test/x")

    @pytest.mark.parametrize(
        "url", ["https://127.0.0.1/x", "https://169.254.169.254/latest/meta-data", "https://10.0.0.5/x", "https://[::1]/x"]
    )
    def test_private_and_metadata_addresses_are_refused(self, settings, url):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with pytest.raises(oidc.SsoError):
            oidc.check_provider_url(url)

    def test_credentials_in_the_url_are_refused(self):
        with pytest.raises(oidc.SsoError):
            oidc.check_provider_url("https://user:pass@idp.example.test/x")

    def test_provider_requests_never_follow_redirects(self, monkeypatch):
        seen = {}

        class Response:
            status_code = 302

        def fake_get(url, **kwargs):
            seen.update(kwargs)
            return Response()

        monkeypatch.setattr(oidc.requests, "get", fake_get)
        with pytest.raises(oidc.SsoError):
            oidc._fetch_json("https://idp.example.test/.well-known/openid-configuration")
        assert seen["allow_redirects"] is False and seen["timeout"]

    def test_oversized_provider_response_is_refused(self, monkeypatch):
        class Raw:
            def read(self, amount, decode_content=True):
                return b"x" * amount

        class Response:
            status_code = 200
            raw = Raw()

        monkeypatch.setattr(oidc.requests, "get", lambda url, **kwargs: Response())
        with pytest.raises(oidc.SsoError):
            oidc._fetch_json("https://idp.example.test/jwks")


class TestEmailRouting:
    def test_work_email_routes_to_its_workspace(self, client, sso, idp):
        response = client.post("/sso/", {"email": "anyone@EXAMPLE.com"})
        assert response.status_code == 302 and response.url == f"/sso/{sso.organization.slug}/"

    @pytest.mark.parametrize("email", ["anyone@unknown.org", "not-an-email", ""])
    def test_unknown_domains_get_one_generic_answer(self, client, sso, idp, email):
        response = client.post("/sso/", {"email": email})
        assert response.status_code == 400
        assert b"not set up for that email address" in response.content

    def test_a_domain_claimed_by_two_workspaces_routes_nowhere(self, sso, other_organization):
        SsoConnection.objects.create(
            organization=other_organization, issuer=ISSUER, client_id="x",
            allowed_domains=["example.com"], is_enabled=True,
        )
        assert oidc.find_connection_for_email("a@example.com") is None

    def test_disabled_connections_are_not_discoverable(self, sso):
        SsoConnection.objects.update(is_enabled=False)
        assert oidc.find_connection_for_email("a@example.com") is None

    def test_login_page_links_to_sso(self, client):
        assert b'href="/sso/"' in client.get("/accounts/login/").content


class TestRequiredSso:
    @pytest.fixture
    def required(self, sso):
        sso.is_required = True
        sso.save()
        policy.set_policy_flag(sso.organization, policy.SSO_REQUIRED_KEY, True)
        return sso

    def test_password_session_is_sent_to_the_provider(self, client_member, required):
        response = client_member.get("/tasks/")
        assert response.status_code == 302 and response.url == f"/sso/{required.organization.slug}/"

    def test_password_session_cannot_use_the_api(self, client_member, required):
        response = client_member.get("/api/v1/tasks/")
        assert response.status_code == 403
        assert "single sign-on" in response.json()["error"]["message"]

    def test_owner_keeps_password_access(self, client_owner, required):
        assert client_owner.get("/tasks/").status_code == 200

    def test_sso_session_is_admitted(self, client, required, idp):
        sign_in(client, required, idp)
        assert client.get("/tasks/").status_code == 200
        assert client.get("/api/v1/tasks/").status_code == 200

    def test_sso_session_satisfies_a_two_step_requirement(self, client, required, idp):
        policy.set_policy_flag(required.organization, policy.REQUIRE_MFA_KEY, True)
        sign_in(client, required, idp)
        assert client.get("/tasks/").status_code == 200

    def test_sso_for_one_workspace_does_not_unlock_another(self, client, required, idp, other_organization):
        sign_in(client, required, idp)
        user = User.objects.get(email="new.person@example.com")
        Membership.objects.create(organization=other_organization, user=user, role=Role.MEMBER)
        policy.set_policy_flag(other_organization, policy.SSO_REQUIRED_KEY, True)

        client.post(f"/workspaces/{other_organization.pk}/switch/")
        response = client.get("/tasks/")
        assert response.status_code == 302 and response.url == f"/sso/{other_organization.slug}/"

    def test_api_keys_are_exempt(self, client, required, api_key):
        _, token = api_key
        assert client.get("/api/v1/tasks/", headers={"Authorization": f"Api-Key {token}"}).status_code == 200


class TestSsoSettings:
    def _payload(self, **overrides):
        data = {
            "form": "sso",
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "allowed_domains": "Example.com, @corp.example.com",
            "default_role": Role.MEMBER,
            "is_enabled": "on",
            "auto_provision": "on",
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def test_requires_the_plan_feature(self, client_owner, organization, idp):
        page = client_owner.get("/settings/security/")
        assert b"available on the Business plan" in page.content
        client_owner.post("/settings/security/", self._payload())
        assert not SsoConnection.objects.exists()

    def test_saves_an_encrypted_connection(self, client_owner, business, idp):
        response = client_owner.post("/settings/security/", self._payload())
        assert response.status_code == 302

        connection = SsoConnection.objects.get(organization=business)
        assert connection.is_enabled and connection.allowed_domains == ["example.com", "corp.example.com"]
        assert connection.client_secret == CLIENT_SECRET
        assert CLIENT_SECRET not in connection.client_secret_encrypted

    def test_the_secret_is_never_rendered_or_audited(self, client_owner, business, idp):
        client_owner.post("/settings/security/", self._payload())
        assert CLIENT_SECRET.encode() not in client_owner.get("/settings/security/").content
        entry = AuditLog.objects.get(action="org.sso_configured")
        assert CLIENT_SECRET not in json.dumps(entry.metadata)

    def test_blank_secret_keeps_the_saved_one(self, client_owner, business, idp):
        client_owner.post("/settings/security/", self._payload())
        client_owner.post("/settings/security/", self._payload(client_secret="", client_id="rotated-client"))
        connection = SsoConnection.objects.get(organization=business)
        assert connection.client_id == "rotated-client" and connection.client_secret == CLIENT_SECRET

    def test_cannot_enable_without_a_secret(self, client_owner, business, idp):
        response = client_owner.post("/settings/security/", self._payload(client_secret=""))
        assert response.status_code == 200 and not SsoConnection.objects.exists()

    def test_cannot_enable_an_unreachable_issuer(self, client_owner, business, idp, monkeypatch):
        def unreachable(url):
            raise oidc.SsoError("The identity provider could not be reached.")

        monkeypatch.setattr(oidc, "_fetch_json", unreachable)
        response = client_owner.post("/settings/security/", self._payload())
        assert b"could not be reached" in response.content and not SsoConnection.objects.exists()

    def test_requiring_sso_needs_it_enabled(self, client_owner, business, idp):
        response = client_owner.post("/settings/security/", self._payload(is_enabled=None, is_required="on"))
        assert response.status_code == 200 and not SsoConnection.objects.exists()

    def test_requiring_sso_sets_the_workspace_policy(self, client_owner, business, idp):
        client_owner.post("/settings/security/", self._payload(is_required="on"))
        business.refresh_from_db()
        assert policy.organization_requires_sso(business)

        client_owner.post("/settings/security/", self._payload())
        business.refresh_from_db()
        assert not policy.organization_requires_sso(business)

    @pytest.mark.parametrize("domains", ["", "not a domain", "example", "a@b.com/x"])
    def test_bad_domain_lists_are_rejected(self, client_owner, business, idp, domains):
        client_owner.post("/settings/security/", self._payload(allowed_domains=domains))
        assert not SsoConnection.objects.exists()

    def test_owner_role_cannot_be_the_default(self, client_owner, business, idp):
        client_owner.post("/settings/security/", self._payload(default_role=Role.OWNER))
        assert not SsoConnection.objects.exists()

    def test_members_cannot_open_the_page(self, client_member, business):
        assert client_member.get("/settings/security/").status_code == 403

    def test_another_workspaces_connection_is_untouched(self, client_owner, business, idp, other_organization):
        theirs = SsoConnection.objects.create(
            organization=other_organization, issuer="https://their-idp.example.test", client_id="theirs"
        )
        client_owner.post("/settings/security/", self._payload())
        theirs.refresh_from_db()
        assert theirs.issuer == "https://their-idp.example.test" and theirs.client_id == "theirs"
