from __future__ import annotations

from django.conf import settings
from django.db import models

from kernel.crypto import decrypt_text, encrypt_text
from kernel.models import Organization, TimeStampedModel
from kernel.roles import Role


class TotpDevice(TimeStampedModel):
    """A user's authenticator app. One per user; unconfirmed until a code proves it works."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="totp_device"
    )
    #: Encrypted, not hashed: verifying a code needs the seed itself.
    secret_encrypted = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    #: The last time step a code was accepted for; replays at or before it are refused.
    last_used_step = models.BigIntegerField(default=0)

    def __str__(self):
        return f"TOTP device for user {self.user_id}"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def secret(self) -> str:
        return decrypt_text(self.secret_encrypted)

    @secret.setter
    def secret(self, value: str) -> None:
        self.secret_encrypted = encrypt_text(value)


class WebAuthnCredential(TimeStampedModel):
    """A WebAuthn passkey registered for passwordless or second-factor sign-in."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="webauthn_credentials"
    )
    #: Raw credential id from the authenticator; looked up on every authentication ceremony.
    credential_id = models.BinaryField(max_length=1024, unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=255)
    #: Browser-reported transport hints (usb, ble, …) for ``allowCredentials``.
    transports = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Passkey {self.label!r} for user {self.user_id}"


class RecoveryCode(TimeStampedModel):
    """A single-use fallback for a lost authenticator. Stored hashed, like a password."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes"
    )
    code_hash = models.CharField(max_length=64, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "code_hash"], name="uniq_recovery_code_per_user"),
        ]

    def __str__(self):
        return f"Recovery code for user {self.user_id}"


class SsoConnection(TimeStampedModel):
    """A workspace's OpenID Connect identity provider."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="sso_connection"
    )
    issuer = models.URLField(max_length=255)
    client_id = models.CharField(max_length=255)
    client_secret_encrypted = models.TextField(blank=True)
    #: Lower-case email domains whose users may sign in through this connection.
    allowed_domains = models.JSONField(default=list, blank=True)
    is_enabled = models.BooleanField(default=False)
    #: When set, members must arrive through the identity provider to open the workspace.
    #: Owners stay exempt so a broken provider cannot lock the workspace out of its own
    #: settings.
    is_required = models.BooleanField(default=False)
    #: Create an account and membership for a first-time user the provider vouches for.
    auto_provision = models.BooleanField(default=True)
    default_role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    #: Some providers (notably Microsoft Entra ID) never send ``email_verified``.
    trust_unverified_email = models.BooleanField(default=False)
    last_login_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"SSO for {self.organization}"

    @property
    def client_secret(self) -> str:
        return decrypt_text(self.client_secret_encrypted) if self.client_secret_encrypted else ""

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_encrypted = encrypt_text(value) if value else ""

    def allows_email(self, email: str) -> bool:
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        return bool(domain) and domain in {d.lower() for d in self.allowed_domains}


class SsoIdentity(TimeStampedModel):
    """Binds a provider's stable subject identifier to a local user.

    The link is on ``sub``, never on email: an email address can be reassigned at the
    provider, and whoever holds it next must not inherit the previous person's account.
    """

    connection = models.ForeignKey(SsoConnection, on_delete=models.CASCADE, related_name="identities")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sso_identities"
    )
    subject = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "subject"], name="uniq_sso_subject"),
            models.UniqueConstraint(fields=["connection", "user"], name="uniq_sso_user"),
        ]

    def __str__(self):
        return f"{self.subject} via {self.connection_id}"
