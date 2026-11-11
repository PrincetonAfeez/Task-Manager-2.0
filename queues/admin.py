from django.contrib import admin

from .models import DeadLetter, EffectMarker, Job, JobAttempt, Queue, QueueMetricSnapshot


@admin.register(Queue)
class QueueAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "mode",
        "paused",
        "concurrency_limit",
        "rate_limit_per_minute",
        "visibility_timeout_seconds",
    )
    list_filter = ("mode", "paused")
    search_fields = ("name",)


class JobAttemptInline(admin.TabularInline):
    model = JobAttempt
    extra = 0
    readonly_fields = ("attempt_number", "outcome", "started_at", "finished_at", "duration_ms", "error")
    can_delete = False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "handler", "status", "queue", "organization", "available_at", "attempts_made")
    list_filter = ("status", "handler_type", "queue")
    search_fields = ("source", "handler", "idempotency_key", "correlation_id")
    readonly_fields = ("lease_token", "created_at", "updated_at")
    inlines = [JobAttemptInline]
    list_select_related = ("queue", "organization")

    def has_add_permission(self, request):
        # Jobs are created by domain services so the transactional-outbox invariant holds;
        # a hand-made row would have no committed state behind it.
        return False


@admin.register(JobAttempt)
class JobAttemptAdmin(admin.ModelAdmin):
    list_display = ("job", "attempt_number", "outcome", "started_at", "duration_ms", "worker_id")
    list_filter = ("outcome",)


@admin.register(DeadLetter)
class DeadLetterAdmin(admin.ModelAdmin):
    list_display = ("job", "organization", "created_at", "redriven_at", "acknowledged_at")
    search_fields = ("reason",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(EffectMarker)
class EffectMarkerAdmin(admin.ModelAdmin):
    list_display = ("effect_type", "natural_key", "organization", "created_at")
    search_fields = ("effect_type", "natural_key")


@admin.register(QueueMetricSnapshot)
class QueueMetricSnapshotAdmin(admin.ModelAdmin):
    list_display = ("queue", "depth", "in_flight", "dead_letters", "oldest_available_age_seconds", "created_at")
    list_filter = ("queue",)
