from django.contrib import admin

from .models import WebhookDelivery, WebhookEndpoint


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "url", "active", "consecutive_failures", "last_success_at")
    list_filter = ("active",)
    search_fields = ("name", "url", "organization__name")
    # The secret verifies our signatures; it never belongs on a screen.
    exclude = ("secret",)
    readonly_fields = ("consecutive_failures", "disabled_at", "last_success_at", "last_failure_at")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event", "endpoint", "status", "status_code", "duration_ms", "created_at")
    list_filter = ("status", "event")
    search_fields = ("event_id",)
    readonly_fields = [field.name for field in WebhookDelivery._meta.fields]

    def has_add_permission(self, request):
        return False