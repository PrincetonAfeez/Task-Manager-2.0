from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .roles import Role


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OrganizationQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def for_user(self, user):
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.alive().filter(
            memberships__user=user,
            memberships__status=Membership.Status.ACTIVE,
        )


class Organization(TimeStampedModel):
    """A tenant. Every customer-owned row in the system hangs off exactly one of these."""

    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True, max_length=64)
    timezone = models.CharField(max_length=64, default="UTC")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
    )
    # Soft delete gives a recovery window and keeps foreign keys intact while the
    # purge job walks the tenant's data.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    purge_after = models.DateTimeField(null=True, blank=True)
    settings_json = models.JSONField(default=dict, blank=True)

    objects = OrganizationQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["deleted_at", "name"])]

    def __str__(self):
        return self.name

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def clean(self):
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValidationError({"timezone": f"Unknown timezone {self.timezone!r}."}) from exc

    def role_for(self, user) -> str | None:
        """Return the active role ``user`` holds in this organization, or ``None``."""

        if not getattr(user, "is_authenticated", False):
            return None
        membership = self.memberships.filter(user=user, status=Membership.Status.ACTIVE).first()
        return membership.role if membership else None


class MembershipQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=Membership.Status.ACTIVE)


class Membership(TimeStampedModel):
    """Binds a user to an organization with a role. The unit of tenant authorization."""

    Role = Role

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    invited_email = models.EmailField(blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_memberships",
    )
    joined_at = models.DateTimeField(default=timezone.now)
    last_active_at = models.DateTimeField(null=True, blank=True)

    objects = MembershipQuerySet.as_manager()

    class Meta:
        ordering = ["organization__name", "user__username"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="uniq_membership_org_user"),
        ]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"{self.user} in {self.organization}"


class Invitation(TimeStampedModel):
    """A pending, single-use, time-limited invitation to join an organization.

    The raw token is never stored: only its SHA-256 digest, so a database leak cannot be
    replayed to join a workspace.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    TOKEN_TTL = timedelta(days=7)

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invitations",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitations",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live invitation per address per org; re-inviting revokes and re-issues.
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(status="pending"),
                name="uniq_pending_invitation_per_email",
            ),
        ]
        indexes = [models.Index(fields=["status", "expires_at"])]

    def __str__(self):
        return f"{self.email} -> {self.organization}"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, organization, email, role, invited_by=None) -> tuple[Invitation, str]:
        """Create an invitation and return it alongside the one-time raw token."""

        cls.objects.filter(
            organization=organization, email__iexact=email, status=cls.Status.PENDING
        ).update(status=cls.Status.REVOKED)
        raw_token = secrets.token_urlsafe(32)
        invitation = cls.objects.create(
            organization=organization,
            email=email.lower(),
            role=role,
            token_hash=cls.hash_token(raw_token),
            invited_by=invited_by,
            expires_at=timezone.now() + cls.TOKEN_TTL,
        )
        return invitation, raw_token

    @property
    def is_usable(self) -> bool:
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()


class Activity(TimeStampedModel):
    """Human-facing workspace timeline. Distinct from :class:`AuditLog`, which is forensic."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
    )
    verb = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=80)
    summary = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "target_type", "target_id"]),
        ]
        verbose_name_plural = "activities"

    def __str__(self):
        return self.summary


class Notification(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"

    class Category(models.TextChoices):
        REMINDER = "reminder", "Reminder"
        ASSIGNMENT = "assignment", "Assignment"
        MENTION = "mention", "Mention"
        COMMENT = "comment", "Comment"
        DIGEST = "digest", "Digest"
        SYSTEM = "system", "System"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="notifications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.SYSTEM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Doubles as the idempotency key: the unique constraint below makes redelivery of the
    # same logical event a no-op instead of a duplicate inbox entry.
    natural_key = models.CharField(max_length=160)
    link_url = models.CharField(max_length=400, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user", "natural_key"],
                name="uniq_notification_effect_marker",
            ),
        ]
        indexes = [models.Index(fields=["organization", "user", "status", "-created_at"])]

    def __str__(self):
        return self.title

    def mark_delivered(self):
        if self.status != self.Status.PENDING:
            return
        self.status = self.Status.DELIVERED
        self.delivered_at = timezone.now()
        self.save(update_fields=["status", "delivered_at", "updated_at"])

    def mark_read(self):
        if self.status == self.Status.READ:
            return
        now = timezone.now()
        self.status = self.Status.READ
        self.read_at = now
        if self.delivered_at is None:
            self.delivered_at = now
        self.save(update_fields=["status", "read_at", "delivered_at", "updated_at"])


class NotificationPreference(TimeStampedModel):
    """Per-user, per-organization delivery preferences, enforced server-side."""

    class Digest(models.TextChoices):
        OFF = "off", "Off"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    email_reminders = models.BooleanField(default=True)
    email_assignments = models.BooleanField(default=True)
    email_mentions = models.BooleanField(default=True)
    email_comments = models.BooleanField(default=False)
    digest = models.CharField(max_length=10, choices=Digest.choices, default=Digest.DAILY)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    # Set when the recipient uses a one-click unsubscribe link; overrides every email flag.
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="uniq_notification_preference_per_member"
            ),
        ]

    def __str__(self):
        return f"prefs({self.user}@{self.organization})"

    def allows_email(self, category: str) -> bool:
        if self.unsubscribed_at is not None or not self.email_enabled:
            return False
        return {
            Notification.Category.REMINDER: self.email_reminders,
            Notification.Category.ASSIGNMENT: self.email_assignments,
            Notification.Category.MENTION: self.email_mentions,
            Notification.Category.COMMENT: self.email_comments,
            Notification.Category.DIGEST: self.digest != self.Digest.OFF,
        }.get(category, True)

    def in_quiet_hours(self, moment=None) -> bool:
        """Quiet hours may wrap midnight (e.g. 22:00 -> 07:00)."""

        if self.quiet_hours_start is None or self.quiet_hours_end is None:
            return False
        current = (moment or timezone.localtime()).time()
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start <= end:
            return start <= current < end
        return current >= start or current < end


class AuditLog(TimeStampedModel):
    """Append-only forensic record of security-relevant actions.

    Immutability is enforced in ``save``/``delete`` rather than only by convention, because
    "the audit log is trustworthy" is a claim the product makes to customers.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_logs", null=True, blank=True
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=120)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.action} by {self.actor_label or self.actor_id or 'system'}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("Audit log entries are immutable once written.")
        if self.actor_id and not self.actor_label:
            self.actor_label = str(self.actor)
        # Truncate rather than raise: an over-long User-Agent must not lose the audit row.
        self.user_agent = (self.user_agent or "")[:300]
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Audit log entries are append-only and cannot be deleted.")


class SavedView(TimeStampedModel):
    """A named filter set. Shared views are visible to the whole organization."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="saved_views")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=120)
    filters = models.JSONField(default=dict, blank=True)
    is_shared = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "owner", "name"], name="uniq_saved_view_owner_name"
            ),
        ]

    def __str__(self):
        return self.name


class FeatureFlag(TimeStampedModel):
    """Per-organization override of a ``settings.FEATURE_FLAG_DEFAULTS`` entry.

    ``organization = NULL`` is a global override, which is how a flag gets turned off
    fleet-wide during an incident without a deploy.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="feature_flags", null=True, blank=True
    )
    key = models.CharField(max_length=80)
    enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "key"],
                condition=models.Q(organization__isnull=False),
                name="uniq_feature_flag_per_org",
            ),
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(organization__isnull=True),
                name="uniq_global_feature_flag",
            ),
        ]

    def __str__(self):
        scope = self.organization.slug if self.organization_id else "global"
        return f"{self.key}@{scope}={self.enabled}"


class ApiKey(TimeStampedModel):
    """A hashed, scoped, revocable credential for the public API.

    Only ``token_hash`` is persisted. The plaintext is shown exactly once at creation â€”
    a leaked database therefore yields no usable API credentials.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="api_keys")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_api_keys",
    )
    name = models.CharField(max_length=120)
    # Non-secret, indexed lookup handle so verification is one indexed row read.
    prefix = models.CharField(max_length=24, unique=True, db_index=True)
    token_hash = models.CharField(max_length=64)
    # Effective role ceiling for the key: never wider than the creating member's role.
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_api_key_name_per_org"),
        ]

    def __str__(self):
        return f"{self.name} ({self.prefix}â€¦)"

    @staticmethod
    def hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    #: Separates the public prefix from the secret. Must be a character ``token_urlsafe``
    #: can never emit â€” its alphabet includes ``-`` and ``_``, so splitting on those would
    #: cut inside the secret and silently break verification.
    TOKEN_SEPARATOR = "."  # noqa: S105

    @classmethod
    def issue(cls, *, organization, name, role, created_by=None, expires_at=None):
        """Create a key, returning ``(api_key, plaintext_token)``.

        Token layout is ``<env_prefix>_<random>.<secret>``: the part before the separator is
        a non-secret lookup handle that leak scanners can grep for, and the secret half
        never leaves this call.
        """

        env_prefix = getattr(settings, "API_KEY_PREFIX", "tmk")
        public_prefix = f"{env_prefix}_{secrets.token_hex(6)}"
        secret = secrets.token_urlsafe(32)
        api_key = cls.objects.create(
            organization=organization,
            created_by=created_by,
            name=name,
            role=role,
            prefix=public_prefix,
            token_hash=cls.hash_secret(secret),
            expires_at=expires_at,
        )
        return api_key, f"{public_prefix}{cls.TOKEN_SEPARATOR}{secret}"

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])
