from django.contrib import admin

from .models import Schedule, ScheduleOccurrence


class ScheduleOccurrenceInline(admin.TabularInline):
    model = ScheduleOccurrence
    extra = 0
    readonly_fields = ("scheduled_time", "status", "job", "error")
    can_delete = False


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "expression_type",
        "expression",
        "timezone",
        "active",
        "next_run_at",
        "consecutive_failures",
    )
    list_filter = ("expression_type", "active", "catch_up_policy")
    search_fields = ("name", "expression")
    readonly_fields = ("last_run_at", "consecutive_failures", "last_error")
    inlines = [ScheduleOccurrenceInline]


@admin.register(ScheduleOccurrence)
class ScheduleOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("schedule", "scheduled_time", "status", "job")
    list_filter = ("status",)
