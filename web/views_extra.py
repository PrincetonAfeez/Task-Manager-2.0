"""Views for the second feature round: time tracking, templates, bulk ops, calendar, sharing."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from kernel.roles import Permission
from kernel.services import record_audit
from ops.logging import get_logger
from tasks.models import ShareLink, TaskTemplate
from tasks.services_extra import (
    apply_template,
    bulk_update_tasks,
    calendar_month,
    estimate_accuracy,
    log_time,
    resolve_share_link,
    running_timer,
    shared_tasks,
    start_timer,
    stop_timer,
    template_from_task,
    time_summary,
)

from .decorators import permission_required, workspace_required
from .forms import LogTimeForm, ShareLinkForm, TaskTemplateForm

logger = get_logger("taskmanager.web.extra")


def _task_or_404(request, task_id):
    from tasks.models import Task

    task = request.tenant.scope(Task.objects.select_related("organization")).filter(pk=task_id).first()
    if task is None:
        raise Http404("Task not found.")
    return task


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False)) or request.headers.get("HX-Request") == "true"


# --------------------------------------------------------------------------------------
# Time tracking
# --------------------------------------------------------------------------------------


@permission_required(Permission.TASK_EDIT)
@require_POST
def timer_start(request, task_id):
    task = _task_or_404(request, task_id)
    entry = start_timer(task=task, user=request.user, note=request.POST.get("note", ""))
    if _is_htmx(request):
        return render(request, "web/partials/timer.html", {"entry": entry, "task": task})
    return redirect("task_detail", task_id=task.pk)


@workspace_required
@require_POST
def timer_stop(request):
    entry = stop_timer(user=request.user)
    if _is_htmx(request):
        return render(request, "web/partials/timer.html", {"entry": None, "stopped": entry})
    if entry is None:
        messages.info(request, "No timer was running.")
    else:
        minutes = round(entry.duration_seconds / 60, 1)
        messages.success(request, f"Logged {minutes} minutes.")
    return redirect(request.POST.get("next") or "dashboard")


@permission_required(Permission.TASK_EDIT)
@require_POST
def time_log(request, task_id):
    task = _task_or_404(request, task_id)
    form = LogTimeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid number of minutes.")
        return redirect("task_detail", task_id=task.pk)

    try:
        log_time(
            task=task,
            user=request.user,
            minutes=form.cleaned_data["minutes"],
            note=form.cleaned_data.get("note", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Time logged.")
    return redirect("task_detail", task_id=task.pk)


@workspace_required
def time_report(request):
    organization = request.tenant.organization
    try:
        days = max(1, min(365, int(request.GET.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    scope_mine = request.GET.get("scope", "workspace") == "mine"
    summary = time_summary(
        organization=organization,
        since=timezone.now() - timedelta(days=days),
        user=request.user if scope_mine else None,
    )
    return render(
        request,
        "web/time_report.html",
        {
            "summary": summary,
            "days": days,
            "scope_mine": scope_mine,
            "accuracy": estimate_accuracy(organization=organization),
            "running": running_timer(request.user),
        },
    )


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------


@workspace_required
def template_list(request):
    organization = request.tenant.organization

    if request.method == "POST":
        request.tenant.require(Permission.TASK_CREATE)
        form = TaskTemplateForm(request.POST, organization=organization)
        if form.is_valid():
            template = form.save(commit=False)
            template.organization = organization
            template.created_by = request.user
            template.subtask_titles = form.cleaned_data["subtask_titles"]
            template.save()
            form.save_m2m()
            messages.success(request, f"Template '{template.name}' created.")
            return redirect("template_list")
        messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    else:
        form = TaskTemplateForm(organization=organization)

    return render(
        request,
        "web/templates.html",
        {"form": form, "templates": TaskTemplate.objects.filter(organization=organization)},
    )


@permission_required(Permission.TASK_CREATE)
@require_POST
def template_apply(request, template_id):
    template = request.tenant.scope(TaskTemplate.objects.all()).filter(pk=template_id).first()
    if template is None:
        raise Http404("Template not found.")

    from billing.models import QuotaExceeded

    try:
        task = apply_template(template=template, creator=request.user)
    except (ValidationError, QuotaExceeded) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("template_list")

    messages.success(request, f"Created '{task.title}' from the template.")
    return redirect("task_detail", task_id=task.pk)


@permission_required(Permission.TASK_CREATE)
@require_POST
def template_from_task_view(request, task_id):
    task = _task_or_404(request, task_id)
    name = (request.POST.get("name") or task.title)[:120]
    try:
        template = template_from_task(task=task, name=name, created_by=request.user)
    except Exception as exc:
        messages.error(request, f"Could not save the template: {exc}")
        return redirect("task_detail", task_id=task.pk)

    messages.success(request, f"Saved '{template.name}' as a template.")
    return redirect("template_list")


@permission_required(Permission.TASK_CREATE)
@require_POST
def template_delete(request, template_id):
    template = request.tenant.scope(TaskTemplate.objects.all()).filter(pk=template_id).first()
    if template is None:
        raise Http404("Template not found.")
    template.delete()
    messages.success(request, "Template deleted.")
    return redirect("template_list")


# --------------------------------------------------------------------------------------
# Bulk operations
# --------------------------------------------------------------------------------------


@permission_required(Permission.TASK_EDIT)
@require_POST
def bulk_action(request):
    task_ids = request.POST.getlist("task_ids")
    action = request.POST.get("action", "")
    value = request.POST.get("value") or None

    try:
        result = bulk_update_tasks(
            organization=request.tenant.organization,
            actor=request.user,
            task_ids=task_ids,
            action=action,
            value=value,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("task_list")

    if result["updated"]:
        messages.success(request, f"Updated {result['updated']} task(s).")
    if result["skipped"]:
        messages.warning(request, f"{result['skipped']} task(s) were skipped.")
    return redirect("task_list")


# --------------------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------------------


@workspace_required
def calendar_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month

    try:
        data = calendar_month(
            organization=request.tenant.organization,
            year=year,
            month=month,
            assignee=request.user if request.GET.get("mine") == "true" else None,
        )
    except ValidationError:
        data = calendar_month(
            organization=request.tenant.organization, year=today.year, month=today.month
        )

    return render(request, "web/calendar.html", {**data, "mine": request.GET.get("mine") == "true"})


# --------------------------------------------------------------------------------------
# Share links
# --------------------------------------------------------------------------------------


@permission_required(Permission.LIST_MANAGE)
def share_link_manage(request):
    organization = request.tenant.organization
    new_url = None

    if request.method == "POST":
        form = ShareLinkForm(request.POST, organization=organization)
        if form.is_valid():
            expires_at = None
            days = form.cleaned_data.get("expires_in_days")
            if days:
                expires_at = timezone.now() + timedelta(days=days)

            link, raw = ShareLink.issue(
                organization=organization,
                task_list=form.cleaned_data["task_list"],
                created_by=request.user,
                expires_at=expires_at,
                label=form.cleaned_data.get("label", ""),
                include_completed=form.cleaned_data.get("include_completed", False),
            )
            from django.conf import settings

            # Shown once: the raw token is not recoverable from the stored digest.
            new_url = f"{settings.SITE_URL}/shared/{raw}/"
            record_audit(
                action="share_link.created",
                organization=organization,
                actor=request.user,
                request=request,
                target_type="share_link",
                target_id=link.pk,
                metadata={"task_list": link.task_list.name},
            )
            messages.success(request, "Share link created.")
        else:
            messages.error(request, "Check the form and try again.")
    else:
        form = ShareLinkForm(organization=organization)

    return render(
        request,
        "web/share_links.html",
        {
            "form": form,
            "new_url": new_url,
            "links": ShareLink.objects.filter(organization=organization).select_related("task_list"),
        },
    )


@permission_required(Permission.LIST_MANAGE)
@require_POST
def share_link_revoke(request, link_id):
    link = request.tenant.scope(ShareLink.objects.all()).filter(pk=link_id).first()
    if link is None:
        raise Http404("Share link not found.")
    link.revoke()
    record_audit(
        action="share_link.revoked",
        organization=request.tenant.organization,
        actor=request.user,
        request=request,
        target_type="share_link",
        target_id=link.pk,
    )
    messages.success(request, "Share link revoked.")
    return redirect("share_link_manage")


@require_GET
def shared_board(request, token):
    """Public, unauthenticated, read-only view of one task list.

    The only endpoint in the product that serves customer data without a session, so it is
    deliberately narrow: one list, no member identities, no write path, and the response is
    marked non-indexable.
    """

    link = resolve_share_link(token)
    if link is None:
        raise Http404("This link is not valid or has expired.")

    link.record_view()
    response = render(
        request,
        "web/shared_board.html",
        {
            "link": link,
            "task_list": link.task_list,
            "organization_name": link.organization.name,
            "tasks": shared_tasks(link)[:500],
        },
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    return response
