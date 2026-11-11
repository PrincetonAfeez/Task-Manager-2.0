from django.contrib import admin

from .models import BillingEvent, Plan, Subscription, UsageRecord


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price_display", "max_members", "max_tasks", "is_public")
    list_filter = ("is_public",)
    search_fields = ("name", "code")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "status", "processor", "trial_ends_at", "current_period_end")
    list_filter = ("status", "plan", "processor")
    search_fields = ("organization__name", "processor_customer_id", "external_reference")


@admin.register(BillingEvent)
class BillingEventAdmin(admin.ModelAdmin):
    """Read-only: this is the record of which processor events were applied."""

    list_display = ("event_id", "event_type", "organization", "created_at")
    list_filter = ("event_type",)
    search_fields = ("event_id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = ("organization", "metric", "day", "value")
    list_filter = ("metric",)