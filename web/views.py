"""HTML and HTMX views.

Every view here is authenticated and tenant-scoped. Objects are fetched through
``tenant.scope(...)`` so a foreign id yields 404 rather than another workspace's data.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from kernel.models import (
    Activity,
    ApiKey,
    AuditLog,
    Invitation,
    Membership,
    Notification,
    NotificationPreference,
    Organization,
    SavedView,
)
from kernel.roles import Permission, Role
from kernel.services import (
    accept_invitation,
    change_member_role,
    create_api_key,
    create_organization,
    invite_member,
    record_audit,
    remove_member,
    revoke_api_key,
    soft_delete_organization,
)
from kernel.tenancy import set_active_organization
from ops.logging import get_logger
from queues.models import DeadLetter, Job, Queue
from queues.services import redrive
from schedules.models import Schedule
from schedules.services import bootstrap_next_run, preview_next_runs
from tasks import scanning
from tasks.models import Attachment, Column, Comment, Label, Reminder, Subtask, Task, TaskList
from tasks.notifications import notify_comment
from tasks.quickadd import parse_quick_add
from tasks.services import (
    ConcurrentModification,
    DependencyBlocked,
    WipLimitExceeded,
    archive_task,
    board_lane_page,
    board_snapshot,
    create_task_from_quick_add,
    move_task,
    record_activity,
    toggle_task_completion,
    update_task,
    workspace_counts,
)

from .decorators import feature_required, login_required_view, permission_required, workspace_required
from .filters import ALLOWED_SORTS, apply_filters, apply_sort, deserialize_filters, serialize_filters
from .forms import (
    ApiKeyForm,
    AttachmentForm,
    ColumnForm,
    CommentForm,
    InviteForm,
    LabelForm,
    MemberRoleForm,
    NotificationPreferenceForm,
    OrganizationForm,
    ProjectAreaForm,
    QuickAddForm,
    ScheduleForm,
    SignUpForm,
    SubtaskForm,
    TaskFilterForm,
    TaskForm,
    TaskListForm,
)

logger = get_logger("taskmanager.web")


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False)) or request.headers.get("HX-Request") == "true"


def _task_or_404(request, task_id) -> Task:
    task = (
        request.tenant.scope(
            Task.objects.select_related("task_list", "column", "assignee", "organization")
        )
        .filter(pk=task_id)
        .first()
    )
    if task is None:
        raise Http404("Task not found.")
    return task


def _flash_fragment(message: str, level: str = "error", status: int = 400) -> HttpResponse:
    """HTMX error response that lands in the flash region instead of the clicked element."""

    from django.utils.html import escape

    response = HttpResponse(
        f'<p class="flash flash-{level}" role="alert">{escape(message)}</p>', status=status
    )
    response["HX-Retarget"] = "#flash"
    response["HX-Reswap"] = "innerHTML"
    return response


# --------------------------------------------------------------------------------------
# Onboarding
# --------------------------------------------------------------------------------------


def signup(request):
    """Create an account and, optionally, a first workspace."""

    if request.user.is_authenticated:
        return redirect("dashboard")

    from api.throttling import LoginRateThrottle

    if request.method == "POST":
        if not LoginRateThrottle().allow_request(request, None):
            messages.error(request, "Too many sign-up attempts. Try again shortly.")
            return render(request, "registration/signup.html", {"form": SignUpForm()}, status=429)

        form = SignUpForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                workspace_name = (
                    form.cleaned_data.get("workspace_name") or f"{user.username}'s workspace"
                )
                create_organization(name=workspace_name, owner=user, request=request)
            login(request, user, backend="kernel.auth_backends.LockoutModelBackend")
            record_audit(action="account.created", actor=user, request=request)
            messages.success(request, "Welcome. Your workspace is ready.")
            return redirect("dashboard")
    else:
        form = SignUpForm()

    return render(request, "registration/signup.html", {"form": form})


@login_required_view
def workspace_create(request):
    if request.method == "POST":
        form = OrganizationForm(request.POST)
        if form.is_valid():
            organization = create_organization(
                name=form.cleaned_data["name"],
                owner=request.user,
                timezone_name=form.cleaned_data["timezone"],
                request=request,
            )
            set_active_organization(request, organization)
            messages.success(request, f"Created {organization.name}.")
            return redirect("dashboard")
    else:
        form = OrganizationForm()
    return render(request, "web/workspace_create.html", {"form": form})


@login_required_view
@require_POST
def workspace_switch(request, organization_id):
    organization = (
        Organization.objects.for_user(request.user).filter(pk=organization_id).distinct().first()
    )
    if organization is None:
        raise Http404("Workspace not found.")
    set_active_organization(request, organization)
    return redirect("dashboard")


@login_required_view
def invitation_accept(request, token):
    """Redeem an invitation link."""

    if request.method == "POST":
        try:
            membership = accept_invitation(raw_token=token, user=request.user, request=request)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("dashboard")
        set_active_organization(request, membership.organization)
        messages.success(request, f"You joined {membership.organization.name}.")
        return redirect("dashboard")

    invitation = Invitation.objects.filter(token_hash=Invitation.hash_token(token)).first()
    return render(
        request,
        "web/invitation_accept.html",
        {
            "invitation": invitation,
            "token": token,
            "valid": bool(invitation and invitation.is_usable),
        },
    )


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------


@workspace_required
def dashboard(request):
    tenant = request.tenant
    organization = tenant.organization
    today = timezone.localdate()

    task_list = (
        TaskList.objects.filter(organization=organization, archived_at__isnull=True)
        .order_by("position", "name")
        .first()
    )

    tasks = (
        Task.objects.filter(organization=organization, archived_at__isnull=True)
        .select_related("task_list", "column", "assignee")
        .prefetch_related(
            "labels",
            # Armed only: that is all a task row shows, and a task's sent reminders are
            # history that would otherwise be loaded (with their jobs) on every page view.
            Prefetch(
                "reminders",
                queryset=Reminder.objects.filter(status=Reminder.Status.ARMED).select_related("job"),
            ),
        )
        .order_by("-priority", "due_date", "position")[:30]
    )

    context = {
        "organization": organization,
        "task_list": task_list,
        "counts": workspace_counts(organization=organization, today=today),
        "tasks": tasks,
        "jobs": Job.objects.filter(organization=organization)
        .select_related("queue")
        .order_by("-created_at")[:12],
        "queues": Queue.objects.filter(organization=organization).annotate(
            job_count=Count("jobs", filter=Q(jobs__status__in=Job.WAITING_STATUSES))
        ),
        "schedules": Schedule.objects.filter(organization=organization).select_related("queue")[:8],
        "activities": Activity.objects.filter(organization=organization).select_related("actor")[:10],
        "notifications": Notification.objects.filter(organization=organization, user=request.user)[:8],
        "dead_letter_count": DeadLetter.objects.filter(
            organization=organization, redriven_at__isnull=True
        ).count(),
        "today": today,
    }
    return render(request, "web/dashboard.html", context)


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


@workspace_required
def task_list_view(request):
    """Filterable, sortable, paginated task list."""

    tenant = request.tenant
    organization = tenant.organization

    saved_view = None
    data = request.GET or None
    if request.GET.get("view"):
        saved_view = (
            SavedView.objects.filter(organization=organization)
            .filter(Q(owner=request.user) | Q(is_shared=True))
            .filter(pk=request.GET["view"])
            .first()
        )
        if saved_view is not None:
            data = deserialize_filters(saved_view.filters, organization=organization)

    form = TaskFilterForm(data, organization=organization)
    filters = form.cleaned_data if form.is_bound and form.is_valid() else {}

    sort = request.GET.get("sort") or (saved_view.filters.get("sort") if saved_view else None)
    if sort not in ALLOWED_SORTS:
        sort = None

    queryset = apply_filters(
        Task.objects.filter(organization=organization)
        .select_related("task_list", "column", "assignee")
        .prefetch_related("labels"),
        filters,
    )
    queryset = apply_sort(queryset, sort)

    page = Paginator(queryset, 25).get_page(request.GET.get("page"))

    context = {
        "form": form,
        "page": page,
        "tasks": page.object_list,
        "sort": sort or "priority",
        "saved_view": saved_view,
        "saved_views": SavedView.objects.filter(organization=organization).filter(
            Q(owner=request.user) | Q(is_shared=True)
        ),
    }
    if _is_htmx(request):
        return render(request, "web/partials/task_table.html", context)
    return render(request, "web/tasks.html", context)


@workspace_required
def task_detail(request, task_id):
    task = _task_or_404(request, task_id)
    task = (
        Task.objects.select_related("task_list", "column", "assignee", "creator", "organization")
        .prefetch_related("labels", "subtasks", "comments__author", "attachments", "reminders")
        .get(pk=task.pk)
    )
    return render(
        request,
        "web/task_detail.html",
        {
            "task": task,
            "comment_form": CommentForm(),
            "subtask_form": SubtaskForm(),
            "attachment_form": AttachmentForm(),
            "blockers": task.blocked_by.select_related("blocking_task"),
            "blocking": task.blocks.select_related("blocked_task"),
        },
    )


@permission_required(Permission.TASK_CREATE)
def task_create(request):
    organization = request.tenant.organization
    if request.method == "POST":
        form = TaskForm(request.POST, organization=organization)
        if form.is_valid():
            from tasks.services import next_position, schedule_reminder

            with transaction.atomic():
                task = form.save(commit=False)
                task.organization = organization
                task.creator = request.user
                task.position = next_position(task_list=task.task_list, column=task.column)
                task.save()
                form.save_m2m()
                record_activity(
                    organization=organization,
                    actor=request.user,
                    verb="task.created",
                    target_type="task",
                    target_id=task.pk,
                    summary=f"Created task: {task.title}",
                )
                if task.due_date:
                    schedule_reminder(task)
            messages.success(request, "Task created.")
            return redirect("task_detail", task_id=task.pk)
    else:
        form = TaskForm(organization=organization)
    return render(request, "web/task_form.html", {"form": form, "task": None})


@permission_required(Permission.TASK_EDIT)
def task_edit(request, task_id):
    task = _task_or_404(request, task_id)
    organization = request.tenant.organization

    if request.method == "POST":
        form = TaskForm(request.POST, instance=task, organization=organization)
        if form.is_valid():
            try:
                update_task(
                    task=task,
                    actor=request.user,
                    expected_version=form.cleaned_data.get("expected_version"),
                    title=form.cleaned_data["title"],
                    notes=form.cleaned_data["notes"],
                    due_date=form.cleaned_data["due_date"],
                    due_time=form.cleaned_data["due_time"],
                    priority=form.cleaned_data["priority"],
                    assignee=form.cleaned_data["assignee"],
                    column=form.cleaned_data["column"],
                    estimate_minutes=form.cleaned_data["estimate_minutes"],
                )
            except ConcurrentModification as exc:
                form.add_error(None, str(exc))
            else:
                task.labels.set(form.cleaned_data["labels"])
                messages.success(request, "Task updated.")
                return redirect("task_detail", task_id=task.pk)
    else:
        form = TaskForm(
            instance=task, organization=organization, initial={"expected_version": task.version}
        )
    return render(request, "web/task_form.html", {"form": form, "task": task})


@permission_required(Permission.TASK_DELETE)
@require_POST
def task_archive(request, task_id):
    task = _task_or_404(request, task_id)
    archive_task(task=task, actor=request.user)
    messages.success(request, "Task archived.")
    return redirect("task_list")


@permission_required(Permission.TASK_EDIT)
@require_POST
def toggle_task(request, task_id):
    task = _task_or_404(request, task_id)
    expected = request.POST.get("expected_version") or None
    try:
        toggle_task_completion(task=task, actor=request.user, expected_version=expected)
    except ConcurrentModification as exc:
        if _is_htmx(request):
            return _flash_fragment(str(exc), status=409)
        messages.error(request, str(exc))
        return redirect("dashboard")
    except DependencyBlocked as exc:
        if _is_htmx(request):
            return _flash_fragment(str(exc), status=409)
        messages.error(request, str(exc))
        return redirect("task_detail", task_id=task.pk)

    task.refresh_from_db()
    if _is_htmx(request):
        return render(request, "web/partials/task_row.html", {"task": task})
    return redirect("dashboard")


@permission_required(Permission.TASK_CREATE)
@require_POST
def quick_add_create(request):
    form = QuickAddForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Task title is required.")

    tenant = request.tenant
    task_list = (
        TaskList.objects.filter(organization=tenant.organization, archived_at__isnull=True)
        .order_by("position", "name")
        .first()
    )
    if task_list is None:
        from tasks.services import bootstrap_workspace

        task_list = bootstrap_workspace(organization=tenant.organization, owner=request.user)

    task = create_task_from_quick_add(
        organization=tenant.organization,
        task_list=task_list,
        creator=request.user,
        raw=form.cleaned_data["quick_add"],
    )
    if _is_htmx(request):
        return render(request, "web/partials/task_row.html", {"task": task})
    messages.success(request, f"Added “{task.title}”.")
    return redirect("dashboard")


@workspace_required
@require_POST
def quick_add_preview(request):
    """Live parse preview. Read-only, so it needs no write permission."""

    parsed = parse_quick_add(request.POST.get("quick_add", ""))
    return render(request, "web/partials/quick_add_preview.html", {"parsed": parsed})


# --------------------------------------------------------------------------------------
# Board
# --------------------------------------------------------------------------------------


@feature_required("board_view")
def board(request, list_id=None):
    organization = request.tenant.organization
    lists = TaskList.objects.filter(organization=organization, archived_at__isnull=True).order_by(
        "position", "name"
    )
    task_list = lists.filter(pk=list_id).first() if list_id else lists.first()
    if task_list is None:
        messages.info(request, "Create a list to use the board.")
        return redirect("list_manage")

    task_list.ensure_default_columns()
    lanes, unassigned = board_snapshot(task_list=task_list)
    return render(
        request,
        "web/board.html",
        {"task_list": task_list, "lists": lists, "lanes": lanes, "unassigned": unassigned},
    )


@feature_required("board_view")
@require_GET
def board_lane(request, list_id):
    """HTMX endpoint: the next page of cards in one board lane."""

    organization = request.tenant.organization
    task_list = get_object_or_404(
        TaskList.objects.filter(organization=organization, archived_at__isnull=True), pk=list_id
    )

    column = None
    raw_column = request.GET.get("column", "")
    if raw_column:
        # Looked up through the list, so a column id from another list or workspace is 404.
        column = task_list.columns.filter(pk=raw_column).first() if raw_column.isdigit() else None
        if column is None:
            raise Http404("Column not found.")

    raw_offset = request.GET.get("offset", "")
    offset = int(raw_offset) if raw_offset.isdigit() else 0
    lane = board_lane_page(task_list=task_list, column=column, offset=offset)
    return render(
        request, "web/partials/board_lane_cards.html", {"lane": lane, "task_list": task_list}
    )


@permission_required(Permission.TASK_EDIT)
@require_POST
def board_move(request, task_id):
    """HTMX endpoint for drag-and-drop moves."""

    task = _task_or_404(request, task_id)
    column = None
    column_id = request.POST.get("column")
    if column_id:
        column = request.tenant.scope(Column.objects.all()).filter(pk=column_id).first()
        if column is None:
            raise Http404("Column not found.")

    def as_int(name):
        raw = request.POST.get(name)
        return int(raw) if raw and raw.isdigit() else None

    try:
        task = move_task(
            task=task,
            actor=request.user,
            column=column,
            before_id=as_int("before_id"),
            after_id=as_int("after_id"),
            expected_version=as_int("expected_version"),
        )
    except (ConcurrentModification, WipLimitExceeded) as exc:
        return _flash_fragment(str(exc), status=409)
    except ValidationError as exc:
        return _flash_fragment("; ".join(exc.messages), status=400)

    return render(request, "web/partials/board_card.html", {"task": task})


# --------------------------------------------------------------------------------------
# Task sub-resources
# --------------------------------------------------------------------------------------


@permission_required(Permission.COMMENT_CREATE)
@require_POST
def comment_create(request, task_id):
    task = _task_or_404(request, task_id)
    form = CommentForm(request.POST)
    if not form.is_valid():
        return _flash_fragment("Write something first.", status=400)

    comment = Comment.objects.create(
        organization=task.organization,
        task=task,
        author=request.user,
        body=form.cleaned_data["body"],
    )
    transaction.on_commit(lambda: notify_comment(comment=comment))
    if _is_htmx(request):
        return render(request, "web/partials/comment.html", {"comment": comment})
    return redirect("task_detail", task_id=task.pk)


@permission_required(Permission.TASK_EDIT)
@require_POST
def subtask_create(request, task_id):
    task = _task_or_404(request, task_id)
    form = SubtaskForm(request.POST)
    if not form.is_valid():
        return _flash_fragment("Give the subtask a title.", status=400)
    position = (task.subtasks.aggregate(v=Max("position"))["v"] or 0) + 1
    subtask = Subtask.objects.create(task=task, title=form.cleaned_data["title"], position=position)
    if _is_htmx(request):
        return render(request, "web/partials/subtask.html", {"subtask": subtask})
    return redirect("task_detail", task_id=task.pk)


@permission_required(Permission.TASK_EDIT)
@require_POST
def subtask_toggle(request, task_id, subtask_id):
    task = _task_or_404(request, task_id)
    subtask = get_object_or_404(Subtask, pk=subtask_id, task=task)
    subtask.toggle()
    if _is_htmx(request):
        return render(request, "web/partials/subtask.html", {"subtask": subtask})
    return redirect("task_detail", task_id=task.pk)


@feature_required("attachments")
@require_POST
def attachment_upload(request, task_id):
    request.tenant.require(Permission.ATTACHMENT_UPLOAD)
    task = _task_or_404(request, task_id)
    form = AttachmentForm(request.POST, request.FILES)
    if not form.is_valid():
        return _flash_fragment("; ".join(form.errors.get("file", ["Upload failed."])), status=400)

    uploaded = form.cleaned_data["file"]
    attachment = Attachment.objects.create(
        organization=task.organization,
        task=task,
        uploaded_by=request.user,
        file=uploaded,
        # Stored for display only; the on-disk name is a random UUID (see upload_to).
        original_name=uploaded.name[:255],
        content_type=(getattr(uploaded, "content_type", "") or "")[:120],
        size_bytes=uploaded.size,
        scan_status=scanning.initial_status(),
    )
    if scanning.scanning_enabled():
        # After commit, so the worker can never pick the job up before the row exists.
        transaction.on_commit(lambda: scanning.queue_scan(attachment))
    if _is_htmx(request):
        return render(request, "web/partials/attachment.html", {"attachment": attachment})
    return redirect("task_detail", task_id=task.pk)


@workspace_required
@require_GET
def attachment_download(request, attachment_id):
    """Stream an attachment after re-checking tenancy on every request."""

    attachment = request.tenant.scope(Attachment.objects.all()).filter(pk=attachment_id).first()
    if attachment is None:
        raise Http404("Attachment not found.")

    if attachment.scan_status == Attachment.ScanStatus.INFECTED:
        return HttpResponse("This file was identified as malware and has been removed.", status=410)
    if not attachment.is_downloadable:
        # Fail closed: an unscanned file is unavailable, not served with a warning.
        response = HttpResponse("This file is still being scanned. Try again in a moment.", status=409)
        response["Retry-After"] = "15"
        return response

    from django.http import FileResponse

    request._private_response = True
    response = FileResponse(
        attachment.file.open("rb"), as_attachment=True, filename=attachment.original_name
    )
    # Never let a browser sniff an uploaded file into an executable content type.
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


# --------------------------------------------------------------------------------------
# Workspace structure
# --------------------------------------------------------------------------------------


@permission_required(Permission.LIST_MANAGE)
def list_manage(request):
    organization = request.tenant.organization
    if request.method == "POST":
        form = TaskListForm(request.POST, organization=organization)
        if form.is_valid():
            task_list = form.save()
            task_list.ensure_default_columns()
            messages.success(request, f"Created list {task_list.name}.")
            return redirect("list_manage")
    else:
        form = TaskListForm(organization=organization)

    return render(
        request,
        "web/lists.html",
        {
            "form": form,
            "lists": TaskList.objects.filter(organization=organization).annotate(
                task_count=Count("tasks", filter=Q(tasks__archived_at__isnull=True))
            ),
            "project_form": ProjectAreaForm(organization=organization),
            "label_form": LabelForm(organization=organization),
            "labels": Label.objects.filter(organization=organization).annotate(task_count=Count("tasks")),
        },
    )


@permission_required(Permission.LABEL_MANAGE)
@require_POST
def label_create(request):
    form = LabelForm(request.POST, organization=request.tenant.organization)
    if form.is_valid():
        form.save()
        messages.success(request, "Label created.")
    else:
        messages.error(request, "That label name is already taken.")
    return redirect("list_manage")


@permission_required(Permission.PROJECT_MANAGE)
@require_POST
def project_create(request):
    form = ProjectAreaForm(request.POST, organization=request.tenant.organization)
    if form.is_valid():
        form.save()
        messages.success(request, "Project area created.")
    else:
        messages.error(request, "That project area name is already taken.")
    return redirect("list_manage")


@permission_required(Permission.LIST_MANAGE)
@require_POST
def column_create(request):
    form = ColumnForm(request.POST, organization=request.tenant.organization)
    if form.is_valid():
        column = form.save()
        return redirect("board_list", list_id=column.task_list_id)
    messages.error(request, "Could not create the column.")
    return redirect("list_manage")


# --------------------------------------------------------------------------------------
# Saved views
# --------------------------------------------------------------------------------------


@workspace_required
@require_POST
def saved_view_create(request):
    organization = request.tenant.organization
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Give the view a name.")
        return redirect("task_list")

    form = TaskFilterForm(request.POST, organization=organization)
    form.is_valid()
    payload = serialize_filters({**form.cleaned_data, "sort": request.POST.get("sort", "")})

    is_shared = request.POST.get("is_shared") == "on"
    if is_shared and not request.tenant.can(Permission.SAVED_VIEW_SHARE):
        is_shared = False

    SavedView.objects.update_or_create(
        organization=organization,
        owner=request.user,
        name=name[:120],
        defaults={"filters": payload, "is_shared": is_shared},
    )
    messages.success(request, f"Saved view “{name}”.")
    return redirect("task_list")


@workspace_required
@require_POST
def saved_view_delete(request, view_id):
    saved_view = get_object_or_404(
        SavedView, pk=view_id, organization=request.tenant.organization, owner=request.user
    )
    saved_view.delete()
    return redirect("task_list")


# --------------------------------------------------------------------------------------
# Automation
# --------------------------------------------------------------------------------------


@permission_required(Permission.AUTOMATION_VIEW)
def automation(request):
    organization = request.tenant.organization
    return render(
        request,
        "web/automation.html",
        {
            "queues": Queue.objects.filter(organization=organization).annotate(
                waiting=Count("jobs", filter=Q(jobs__status__in=Job.WAITING_STATUSES)),
                in_flight_count=Count("jobs", filter=Q(jobs__status=Job.Status.LEASED)),
            ),
            "jobs": Job.objects.filter(organization=organization)
            .select_related("queue")
            .order_by("-created_at")[:50],
            "dead_letters": DeadLetter.objects.filter(
                organization=organization, redriven_at__isnull=True
            ).select_related("job")[:25],
            "schedules": Schedule.objects.filter(organization=organization).select_related("queue"),
            "schedule_form": ScheduleForm(organization=organization),
        },
    )


@permission_required(Permission.AUTOMATION_MANAGE)
@require_POST
def schedule_create(request):
    form = ScheduleForm(request.POST, organization=request.tenant.organization)
    if form.is_valid():
        schedule = form.save()
        bootstrap_next_run(schedule)
        messages.success(request, f"Created schedule {schedule.name}.")
    else:
        messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("automation")


@permission_required(Permission.AUTOMATION_VIEW)
def schedule_preview(request, schedule_id):
    schedule = request.tenant.scope(Schedule.objects.all()).filter(pk=schedule_id).first()
    if schedule is None:
        raise Http404("Schedule not found.")
    return render(
        request,
        "web/partials/schedule_preview.html",
        {"schedule": schedule, "runs": preview_next_runs(schedule)},
    )


@permission_required(Permission.JOB_REDRIVE)
@require_POST
def dead_letter_redrive(request, dead_letter_id):
    dead_letter = (
        request.tenant.scope(DeadLetter.objects.select_related("job")).filter(pk=dead_letter_id).first()
    )
    if dead_letter is None:
        raise Http404("Dead letter not found.")
    job = redrive(dead_letter, actor=request.user)
    record_audit(
        action="job.redriven",
        organization=request.tenant.organization,
        actor=request.user,
        request=request,
        target_type="job",
        target_id=job.pk,
    )
    messages.success(request, f"Job {job.pk} returned to the queue.")
    return redirect("automation")


@permission_required(Permission.AUTOMATION_MANAGE)
@require_POST
def queue_toggle_pause(request, queue_id):
    queue = request.tenant.scope(Queue.objects.all()).filter(pk=queue_id).first()
    if queue is None:
        raise Http404("Queue not found.")
    queue.paused = not queue.paused
    queue.save(update_fields=["paused", "updated_at"])
    record_audit(
        action="queue.paused" if queue.paused else "queue.resumed",
        organization=request.tenant.organization,
        actor=request.user,
        request=request,
        target_type="queue",
        target_id=queue.pk,
    )
    return redirect("automation")


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


@permission_required(Permission.ORG_SETTINGS)
def settings_members(request):
    organization = request.tenant.organization
    if request.method == "POST":
        form = InviteForm(request.POST)
        if form.is_valid():
            try:
                invitation, token = invite_member(
                    organization=organization,
                    actor=request.user,
                    actor_role=request.tenant.role,
                    email=form.cleaned_data["email"],
                    role=form.cleaned_data["role"],
                    request=request,
                )
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                from django.conf import settings as django_settings

                from tasks.notifications import queue_email

                link = f"{django_settings.SITE_URL}/invitations/{token}/"
                queue_email(
                    organization=organization,
                    to=[invitation.email],
                    subject=f"You're invited to {organization.name}",
                    body=f"Accept your invitation: {link}",
                    natural_key=f"invite:{invitation.pk}",
                )
                messages.success(request, f"Invitation sent to {invitation.email}.")
            return redirect("settings_members")
    else:
        form = InviteForm()

    return render(
        request,
        "web/settings_members.html",
        {
            "form": form,
            "memberships": Membership.objects.filter(organization=organization).select_related("user"),
            "invitations": Invitation.objects.filter(
                organization=organization, status=Invitation.Status.PENDING
            ),
            "roles": Role.choices,
        },
    )


@permission_required(Permission.MEMBER_MANAGE)
@require_POST
def member_role_change(request, membership_id):
    organization = request.tenant.organization
    membership = get_object_or_404(Membership, pk=membership_id, organization=organization)
    form = MemberRoleForm(request.POST)
    if form.is_valid():
        try:
            change_member_role(
                organization=organization,
                actor=request.user,
                actor_role=request.tenant.role,
                membership=membership,
                role=form.cleaned_data["role"],
                request=request,
            )
            messages.success(request, "Role updated.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("settings_members")


@permission_required(Permission.MEMBER_MANAGE)
@require_POST
def member_remove(request, membership_id):
    organization = request.tenant.organization
    membership = get_object_or_404(Membership, pk=membership_id, organization=organization)
    try:
        remove_member(
            organization=organization,
            actor=request.user,
            actor_role=request.tenant.role,
            membership=membership,
            request=request,
        )
        messages.success(request, "Member removed.")
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("settings_members")


@workspace_required
def settings_notifications(request):
    organization = request.tenant.organization
    prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=request.user)
    if request.method == "POST":
        form = NotificationPreferenceForm(request.POST, instance=prefs)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification preferences saved.")
            return redirect("settings_notifications")
    else:
        form = NotificationPreferenceForm(instance=prefs)
    return render(request, "web/settings_notifications.html", {"form": form})


@feature_required("api_keys")
def settings_api_keys(request):
    request.tenant.require(Permission.API_KEY_MANAGE)
    organization = request.tenant.organization
    new_token = None
    form = ApiKeyForm()

    if request.method == "POST":
        form = ApiKeyForm(request.POST)
        if form.is_valid():
            try:
                _, new_token = create_api_key(
                    organization=organization,
                    actor=request.user,
                    actor_role=request.tenant.role,
                    name=form.cleaned_data["name"],
                    role=form.cleaned_data["role"],
                    expires_at=form.cleaned_data["expires_in_days"],
                    request=request,
                )
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.error(request, "Check the form and try again.")

    return render(
        request,
        "web/settings_api_keys.html",
        {
            "form": form,
            # Shown exactly once, immediately after creation. Never stored in plaintext.
            "new_token": new_token,
            "api_keys": ApiKey.objects.filter(organization=organization),
        },
    )


@permission_required(Permission.API_KEY_MANAGE)
@require_POST
def api_key_revoke(request, key_id):
    api_key = request.tenant.scope(ApiKey.objects.all()).filter(pk=key_id).first()
    if api_key is None:
        raise Http404("API key not found.")
    revoke_api_key(
        organization=request.tenant.organization,
        actor=request.user,
        actor_role=request.tenant.role,
        api_key=api_key,
        request=request,
    )
    messages.success(request, "API key revoked.")
    return redirect("settings_api_keys")


@permission_required(Permission.AUDIT_VIEW)
def settings_audit(request):
    entries = AuditLog.objects.filter(organization=request.tenant.organization).select_related("actor")
    action = request.GET.get("action")
    if action:
        entries = entries.filter(action=action)
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    return render(request, "web/settings_audit.html", {"page": page, "entries": page.object_list})


@permission_required(Permission.ORG_SETTINGS)
def settings_workspace(request):
    organization = request.tenant.organization
    if request.method == "POST":
        form = OrganizationForm(request.POST, instance=organization)
        if form.is_valid():
            form.save()
            record_audit(
                action="organization.updated",
                organization=organization,
                actor=request.user,
                request=request,
                target_type="organization",
                target_id=organization.pk,
            )
            messages.success(request, "Workspace settings saved.")
            return redirect("settings_workspace")
    else:
        form = OrganizationForm(instance=organization)
    return render(request, "web/settings_workspace.html", {"form": form})


@permission_required(Permission.ORG_DELETE)
@require_POST
def workspace_delete(request):
    organization = request.tenant.organization
    # Typing the workspace name is the confirmation step: a stray POST cannot delete a
    # customer's data.
    if request.POST.get("confirm") != organization.name:
        messages.error(request, "Type the workspace name exactly to confirm deletion.")
        return redirect("settings_workspace")

    try:
        soft_delete_organization(organization=organization, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("settings_workspace")
    messages.warning(
        request,
        f"{organization.name} is scheduled for deletion. Contact support within 30 days to restore it.",
    )
    request.session.pop("active_organization_id", None)
    return redirect("dashboard")


# --------------------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------------------


@workspace_required
def notification_list(request):
    notifications = Notification.objects.filter(
        organization=request.tenant.organization, user=request.user
    )
    page = Paginator(notifications, 30).get_page(request.GET.get("page"))
    return render(request, "web/notifications.html", {"page": page, "notifications": page.object_list})


@workspace_required
@require_POST
def notification_mark_read(request, notification_id):
    notification = get_object_or_404(
        Notification, pk=notification_id, organization=request.tenant.organization, user=request.user
    )
    notification.mark_read()
    if _is_htmx(request):
        return HttpResponse(status=204)
    return redirect("notification_list")


@workspace_required
@require_POST
def notification_mark_all_read(request):
    Notification.objects.filter(
        organization=request.tenant.organization,
        user=request.user,
        status=Notification.Status.DELIVERED,
    ).update(status=Notification.Status.READ, read_at=timezone.now())
    return redirect("notification_list")


# --------------------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------------------


@feature_required("analytics")
def analytics(request):
    from tasks.analytics import workspace_analytics

    try:
        days = max(7, min(180, int(request.GET.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    return render(
        request,
        "web/analytics.html",
        {
            "report": workspace_analytics(organization=request.tenant.organization, days=days),
            "days": days,
        },
    )
