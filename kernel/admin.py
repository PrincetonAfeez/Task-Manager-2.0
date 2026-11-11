from django.contrib import admin

from .models import (
    Activity,
    ApiKey,
    AuditLog,
    FeatureFlag,
    Invitation,
    Membership,
    Notification,
    NotificationPreference,
    Organization,
    SavedView,
)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "timezone", "deleted_at", "created_at")
    list_filter = ("timezone", "deleted_at")
    search_fields = ("name", "slug", "owner__username")
    readonly_fields = ("created_at", "updated_at")
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "role", "status", "joined_at")
    list_filter = ("role", "status")
    search_fields = ("user__username", "user__email", "organization__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "status", "expires_at", "created_at")
    list_filter = ("status", "role")
    search_fields = ("email", "organization__name")
    # The digest is credential-equivalent; showing it in the admin is a leak.
    exclude = ("token_hash",)
    readonly_fields = ("accepted_at", "accepted_by", "created_at", "updated_at")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("organization", "verb", "summary", "actor", "created_at")
    list_filter = ("verb",)
    search_fields = ("summary", "target_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "title", "category", "status", "created_at")
    list_filter = ("status", "category")
    search_fields = ("title", "natural_key")
    readonly_fields = ("created_at", "updated_at")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "email_enabled", "digest", "unsubscribed_at")
    list_filter = ("digest", "email_enabled")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only in the admin: the audit trail is append-only by design."""

    list_display = ("created_at", "action", "actor_label", "organization", "ip_address")
    list_filter = ("action",)
    search_fields = ("action", "actor_label", "target_id", "request_id")
    readonly_fields = [field.name for field in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SavedView)
class SavedViewAdmin(admin.ModelAdmin):
    list_display = ("organization", "owner", "name", "is_shared", "created_at")
    list_filter = ("is_shared",)


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "prefix", "role", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("role",)
    search_fields = ("name", "prefix", "organization__name")
    # token_hash is a verifier; it never belongs on a screen.
    exclude = ("token_hash",)
    readonly_fields = ("prefix", "last_used_at", "created_at", "updated_at")

    def has_add_permission(self, request):
        # Keys must be minted through kernel.services.create_api_key so the plaintext is
        # generated, hashed and shown exactly once.
        return False


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ("key", "organization", "enabled", "note", "updated_at")
    list_filter = ("enabled", "key")
    search_fields = ("key", "note")
