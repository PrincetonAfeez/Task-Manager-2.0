"""OpenID Connect single sign-on (authorization code flow with PKCE).

Security properties this module is responsible for, each covered by a test:

* the ID token's signature is verified against the provider's published keys, with an
  explicit allow-list of asymmetric algorithms (``none`` and HMAC are never accepted);
* ``iss``, ``aud``, ``exp`` and the per-login ``nonce`` are all checked;
* ``state`` binds the callback to the browser session that started the flow;
* accounts are linked on the provider's ``sub``, never on email alone, and an existing
  account is linked only if it already belongs to the workspace — otherwise a workspace's
  identity provider could assert someone else's address and take over an account that has
  access to *other* workspaces;
* every provider URL is validated against the same SSRF rules as outbound webhooks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from kernel.models import Membership
from ops.logging import get_logger

from .models import SsoConnection, SsoIdentity

logger = get_logger("taskmanager.identity.sso")

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
SESSION_FLOW_KEY = "sso_flow"
FLOW_TTL_SECONDS = 600
DISCOVERY_CACHE_SECONDS = 3600
MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
_TIMEOUT = (5, 10)


class SsoError(Exception):
    """A sign-on failure with a message that is safe to show the user."""


# --------------------------------------------------------------------------------------
# Provider HTTP (the only two functions that touch the network)
# --------------------------------------------------------------------------------------


def check_provider_url(url: str) -> str:
    """Reject provider URLs that are not HTTPS or that resolve to a private address."""

    from integrations.delivery import validate_webhook_url

    parts = urlsplit(url)
    insecure_ok = getattr(settings, "SSO_ALLOW_INSECURE_PROVIDERS", False)
    if parts.scheme != "https" and not (insecure_ok and parts.scheme == "http"):
        raise SsoError("The identity provider must be reached over HTTPS.")
    try:
        return validate_webhook_url(url)
    except ValueError as exc:
        raise SsoError("The identity provider address is not allowed.") from exc


def _read_json(response) -> dict:
    if response.status_code != 200:
        raise SsoError("The identity provider returned an error.")
    body = response.raw.read(MAX_PROVIDER_RESPONSE_BYTES + 1, decode_content=True)
    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise SsoError("The identity provider returned an unexpectedly large response.")
    try:
        import json

        data = json.loads(body)
    except ValueError as exc:
        raise SsoError("The identity provider returned an unreadable response.") from exc
    if not isinstance(data, dict):
        raise SsoError("The identity provider returned an unreadable response.")
    return data


def _fetch_json(url: str) -> dict:
    check_provider_url(url)
    try:
        # Redirects are refused: following one would let a provider URL that passed
        # validation bounce the request to an internal address.
        response = requests.get(
            url, timeout=_TIMEOUT, allow_redirects=False, stream=True,
            headers={"Accept": "application/json"},
        )
        return _read_json(response)
    except requests.RequestException as exc:
        raise SsoError("The identity provider could not be reached.") from exc


def _post_form(url: str, data: dict) -> dict:
    check_provider_url(url)
    try:
        response = requests.post(
            url, data=data, timeout=_TIMEOUT, allow_redirects=False, stream=True,
            headers={"Accept": "application/json"},
        )
        return _read_json(response)
    except requests.RequestException as exc:
        raise SsoError("The identity provider could not be reached.") from exc


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


def _same_issuer(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def discover(issuer: str, *, use_cache: bool = True) -> dict:
    """Fetch and validate the provider's OpenID configuration document."""

    key = "sso:discovery:" + hashlib.sha256(issuer.encode()).hexdigest()
    if use_cache:
        cached = cache.get(key)
        if cached:
            return cached

    document = _fetch_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    # A document that names a different issuer is how one provider impersonates another.
    if not _same_issuer(str(document.get("issuer", "")), issuer):
        raise SsoError("The identity provider's configuration does not match its issuer URL.")

    metadata = {"issuer": document["issuer"]}
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = document.get(field)
        if not value or not isinstance(value, str):
            raise SsoError("The identity provider's configuration is incomplete.")
        check_provider_url(value)
        metadata[field] = value

    cache.set(key, metadata, DISCOVERY_CACHE_SECONDS)
    return metadata


# --------------------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------------------


def redirect_uri() -> str:
    return settings.SITE_URL + reverse("sso_callback")


def begin_login(request, connection: SsoConnection, *, next_url: str = "") -> str:
    """Start a login and return the provider URL to send the browser to."""

    if not connection.is_enabled:
        raise SsoError("Single sign-on is not enabled for this workspace.")
    metadata = discover(connection.issuer)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    request.session[SESSION_FLOW_KEY] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "connection_id": connection.pk,
        "next": next_url,
        "started": time.time(),
    }
    query = urlencode(
        {
            "response_type": "code",
            "client_id": connection.client_id,
            "redirect_uri": redirect_uri(),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    endpoint = metadata["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


def verify_id_token(id_token: str, connection: SsoConnection, metadata: dict, *, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SsoError("The identity provider returned an invalid token.") from exc

    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise SsoError("The identity provider signed its token with an unsupported algorithm.")

    keys = _fetch_json(metadata["jwks_uri"]).get("keys") or []
    kid = header.get("kid")
    candidates = [k for k in keys if isinstance(k, dict) and (kid is None or k.get("kid") == kid)]
    if len(candidates) != 1:
        raise SsoError("The identity provider's signing key could not be identified.")

    try:
        signing_key = jwt.PyJWK.from_dict(candidates[0], algorithm=algorithm).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=[algorithm],
            audience=connection.client_id,
            issuer=metadata["issuer"],
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning(
            "sso token rejected",
            extra={"connection_id": connection.pk, "reason": type(exc).__name__},
        )
        raise SsoError("The identity provider's token could not be verified.") from exc

    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SsoError("The sign-on response does not match this browser session.")
    return claims


def _unique_username(email: str) -> str:
    User = get_user_model()
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0].lower())[:24] or "user"
    candidate = base
    while User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


@transaction.atomic
def resolve_user(connection: SsoConnection, claims: dict):
    """Map verified claims to a local user, creating or linking only when it is safe to."""

    from billing.models import QuotaExceeded
    from billing.services import check_quota

    User = get_user_model()
    organization = connection.organization
    subject = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").strip().lower()

    if not subject or not email:
        raise SsoError("The identity provider did not supply an email address.")
    if claims.get("email_verified") is not True and not connection.trust_unverified_email:
        raise SsoError("Your email address has not been verified by the identity provider.")
    if not connection.allows_email(email):
        raise SsoError("Your email domain is not permitted to sign in to this workspace.")

    identity = (
        SsoIdentity.objects.select_related("user").filter(connection=connection, subject=subject).first()
    )
    created_user = False
    if identity is not None:
        user = identity.user
    else:
        matches = list(User.objects.filter(email__iexact=email)[:2])
        if len(matches) > 1:
            raise SsoError("More than one account uses this email address. Contact your administrator.")
        if matches:
            user = matches[0]
            is_member = Membership.objects.filter(
                organization=organization, user=user, status=Membership.Status.ACTIVE
            ).exists()
            if not is_member:
                raise SsoError(
                    "An account with this email already exists outside this workspace. "
                    "Ask a workspace administrator to invite it first."
                )
        else:
            if not connection.auto_provision:
                raise SsoError("You do not have an account in this workspace. Ask an administrator for an invitation.")
            user = User(
                username=_unique_username(email),
                email=email,
                first_name=str(claims.get("given_name") or "")[:150],
                last_name=str(claims.get("family_name") or "")[:150],
            )
            user.set_unusable_password()
            user.save()
            created_user = True
        if SsoIdentity.objects.filter(connection=connection, user=user).exists():
            # The account is already bound to a different subject at this provider.
            raise SsoError("This account is linked to a different identity. Contact your administrator.")
        identity = SsoIdentity.objects.create(
            connection=connection, user=user, subject=subject, email=email
        )

    if not user.is_active:
        raise SsoError("This account has been deactivated.")

    membership = Membership.objects.filter(organization=organization, user=user).first()
    if membership is None:
        if not connection.auto_provision:
            raise SsoError("You are no longer a member of this workspace.")
        try:
            check_quota(organization, "members")
        except QuotaExceeded as exc:
            raise SsoError("This workspace has reached its member limit.") from exc
        Membership.objects.create(organization=organization, user=user, role=connection.default_role)
    elif membership.status != Membership.Status.ACTIVE:
        raise SsoError("Your access to this workspace has been suspended.")

    now = timezone.now()
    SsoIdentity.objects.filter(pk=identity.pk).update(last_login_at=now, email=email)
    SsoConnection.objects.filter(pk=connection.pk).update(last_login_at=now)
    return user, created_user


def complete_login(request, *, code: str, state: str):
    """Finish the flow. Returns ``(user, connection, next_url, created_user)``."""

    flow = request.session.pop(SESSION_FLOW_KEY, None)
    if (
        not flow
        or not code
        or not state
        or not hmac.compare_digest(str(flow.get("state", "")), state)
        or time.time() - float(flow.get("started", 0)) > FLOW_TTL_SECONDS
    ):
        raise SsoError("This sign-on attempt has expired or is not valid. Start again.")

    connection = (
        SsoConnection.objects.select_related("organization")
        .filter(pk=flow.get("connection_id"), is_enabled=True, organization__deleted_at__isnull=True)
        .first()
    )
    if connection is None:
        raise SsoError("Single sign-on is not enabled for this workspace.")

    metadata = discover(connection.issuer)
    tokens = _post_form(
        metadata["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": connection.client_id,
            "client_secret": connection.client_secret,
            "code_verifier": flow["verifier"],
        },
    )
    id_token = tokens.get("id_token")
    if not id_token or not isinstance(id_token, str):
        raise SsoError("The identity provider did not return an identity token.")

    claims = verify_id_token(id_token, connection, metadata, nonce=flow["nonce"])
    user, created_user = resolve_user(connection, claims)
    return user, connection, flow.get("next") or "", created_user


def find_connection_for_email(email: str) -> SsoConnection | None:
    """The enabled connection whose domain list covers ``email``, if exactly one does."""

    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    if not domain:
        return None
    matches = [
        connection
        for connection in SsoConnection.objects.select_related("organization").filter(
            is_enabled=True, organization__deleted_at__isnull=True
        )
        if domain in {d.lower() for d in connection.allowed_domains}
    ]
    return matches[0] if len(matches) == 1 else None
