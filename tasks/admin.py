from django.contrib import admin

from .models import (
    Attachment,
    Column,
    Comment,
    Label,
    ListMembership,
    ProjectArea,
    RecurrenceRule,
    Reminder,
    Subtask,
    Task,
    TaskDependency,
    TaskList,
)


@admin.register(ProjectArea)
class ProjectAreaAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "position", "archived_at")
    list_filter = ("organization",)
    search_fields = ("name",)


@admin.register(TaskList)
class TaskListAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "project_area", "position", "archived_at")
    list_filter = ("organization",)
    search_fields = ("name",)


@admin.register(ListMembership)
class ListMembershipAdmin(admin.ModelAdmin):
    list_display = ("task_list", "user", "role")
    list_filter = ("role",)


@admin.register(Column)
class ColumnAdmin(admin.ModelAdmin):
    list_display = ("name", "task_list", "position", "is_done", "wip_limit")
    list_filter = ("is_done",)


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "color")
    search_fields = ("name",)


class SubtaskInline(admin.TabularInline):
    model = Subtask
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "task_list", "status", "priority", "due_date", "assignee")
    list_filter = ("status", "priority", "organization")
    search_fields = ("title", "notes")
    readonly_fields = ("version", "completed_at", "created_at", "updated_at")
    inlines = [SubtaskInline]
    list_select_related = ("organization", "task_list", "assignee")


@admin.register(Subtask)
class SubtaskAdmin(admin.ModelAdmin):
    list_display = ("title", "task", "completed_at", "position")


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("task", "author", "created_at")
    search_fields = ("body",)


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "task", "uploaded_by", "size_bytes", "created_at")
    search_fields = ("original_name",)
    readonly_fields = ("size_bytes", "content_type", "created_at", "updated_at")


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("task", "remind_at", "status", "job", "sent_at")
    list_filter = ("status",)


@admin.register(RecurrenceRule)
class RecurrenceRuleAdmin(admin.ModelAdmin):
    list_display = ("source_task", "frequency", "interval", "next_occurrence_date", "active")
    list_filter = ("frequency", "active")


@admin.register(TaskDependency)
class TaskDependencyAdmin(admin.ModelAdmin):
    list_display = ("blocked_task", "blocking_task", "created_at")
