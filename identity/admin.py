from django.contrib import admin

from .models import SsoConnection, SsoIdentity, TotpDevice, WebAuthnCredential


@admin.register(WebAuthnCredential)
class WebAuthnCredentialAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "sign_count", "created_at")
    search_fields = ("user__username", "user__email", "label")
    readonly_fields = ("user", "label", "sign_count", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False


@admin.register(TotpDevice)
class TotpDeviceAdmin(admin.ModelAdmin):
    """Support can see *that* a device exists and remove it; the seed is never shown."""

    list_display = ("user", "confirmed_at", "created_at")
    search_fields = ("user__username", "user__email")
    fields = ("user", "confirmed_at")
    readonly_fields = ("user", "confirmed_at")

    def has_add_permission(self, request):
        return False


@admin.register(SsoConnection)
class SsoConnectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "issuer", "is_enabled", "is_required", "last_login_at")
    search_fields = ("organization__name", "issuer")
    exclude = ("client_secret_encrypted",)
    readonly_fields = ("organization", "last_login_at")

    def has_add_permission(self, request):
        return False


@admin.register(SsoIdentity)
class SsoIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "connection", "email", "last_login_at")
    search_fields = ("user__username", "email", "subject")
    readonly_fields = ("connection", "user", "subject", "email", "last_login_at")

    def has_add_permission(self, request):
        return False
