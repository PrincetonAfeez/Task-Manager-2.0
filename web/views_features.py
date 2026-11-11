"""Views for the Phase 2 feature set: webhooks, billing, exports, account lifecycle."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from billing import stripe_gateway
from billing.models import Plan, QuotaExceeded
from billing.services import change_plan, current_usage, ensure_default_plans, get_subscription
from integrations.models import WebhookDelivery, WebhookEndpoint
from kernel.roles import Permission
from kernel.services import record_audit
from ops.logging import get_logger
from support.services import blocking_owned_workspaces, build_personal_export, delete_account, leave_workspace

from .decorators import feature_required, login_required_view, permission_required, workspace_required
from .forms import WebhookEndpointForm

logger = get_logger("taskmanager.web.features")


# --------------------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------------------


@feature_required("webhooks")
def webhook_list(request):
    request.tenant.require(Permission.WEBHOOK_MANAGE)
    organization = request.tenant.organization
    new_secret = None

    if request.method == "POST":
        form = WebhookEndpointForm(request.POST, organization=organization)
        if form.is_valid():
            from billing.services import check_quota

            try:
                check_quota(organization, "webhooks")
            except QuotaExceeded as exc:
                messages.error(request, str(exc))
            else:
                endpoint = form.save(commit=False)
                endpoint.organization = organization
                endpoint.created_by = request.user
                endpoint.secret = WebhookEndpoint.generate_secret()
                endpoint.save()
                # Shown exactly once. The customer needs it to verify our signatures; we
                # never need the plaintext again.
                new_secret = endpoint.secret
                record_audit(
                    action="webhook.created",
                    organization=organization,
                    actor=request.user,
                    request=request,
                    target_type="webhook",
                    target_id=endpoint.pk,
                    metadata={"url": endpoint.url},
                )
                messages.success(request, f"Webhook '{endpoint.name}' created.")
        else:
            messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    else:
        form = WebhookEndpointForm(organization=organization)

    return render(
        request,
        "web/webhooks.html",
        {
            "form": form,
            "new_secret": new_secret,
            "endpoints": WebhookEndpoint.objects.filter(organization=organization),
            "recent_deliveries": WebhookDelivery.objects.filter(organization=organization)
            .select_related("endpoint")[:25],
        },
    )


@permission_required(Permission.WEBHOOK_MANAGE)
def webhook_detail(request, endpoint_id):
    endpoint = request.tenant.scope(WebhookEndpoint.objects.all()).filter(pk=endpoint_id).first()
    if endpoint is None:
        raise Http404("Webhook not found.")

    deliveries = WebhookDelivery.objects.filter(endpoint=endpoint)
    page = Paginator(deliveries, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "web/webhook_detail.html",
        {"endpoint": endpoint, "page": page, "deliveries": page.object_list},
    )


@permission_required(Permission.WEBHOOK_MANAGE)
@require_POST
def webhook_toggle(request, endpoint_id):
    endpoint = request.tenant.scope(WebhookEndpoint.objects.all()).filter(pk=endpoint_id).first()
    if endpoint is None:
        raise Http404("Webhook not found.")

    endpoint.active = not endpoint.active
    # Re-enabling clears the circuit breaker; otherwise a previously disabled endpoint
    # would look active while still being skipped at delivery time.
    if endpoint.active:
        endpoint.disabled_at = None
        endpoint.consecutive_failures = 0
    endpoint.save(update_fields=["active", "disabled_at", "consecutive_failures", "updated_at"])
    return redirect("webhook_list")


@permission_required(Permission.WEBHOOK_MANAGE)
@require_POST
def webhook_delete(request, endpoint_id):
    endpoint = request.tenant.scope(WebhookEndpoint.objects.all()).filter(pk=endpoint_id).first()
    if endpoint is None:
        raise Http404("Webhook not found.")

    record_audit(
        action="webhook.deleted",
        organization=request.tenant.organization,
        actor=request.user,
        request=request,
        target_type="webhook",
        target_id=endpoint.pk,
        metadata={"url": endpoint.url},
    )
    endpoint.delete()
    messages.success(request, "Webhook deleted.")
    return redirect("webhook_list")


@permission_required(Permission.WEBHOOK_MANAGE)
@require_POST
def webhook_test(request, endpoint_id):
    """Send a synthetic ping so a customer can verify their receiver before relying on it."""

    endpoint = request.tenant.scope(WebhookEndpoint.objects.all()).filter(pk=endpoint_id).first()
    if endpoint is None:
        raise Http404("Webhook not found.")

    from django.utils import timezone

    from queues.models import Job
    from queues.services import enqueue_job, get_webhook_queue

    stamp = timezone.now().isoformat()
    enqueue_job(
        organization=request.tenant.organization,
        queue=get_webhook_queue(request.tenant.organization),
        source=f"webhook:{endpoint.pk}",
        handler_type=Job.HandlerType.HTTP_DELIVERY,
        handler="http_delivery",
        payload={
            "endpoint_id": endpoint.pk,
            "event": "ping",
            "body": {"id": f"evt_test_{stamp}", "event": "ping", "data": {"test": True}},
        },
        idempotency_key=f"webhook:{endpoint.pk}:test:{stamp}"[:180],
    )
    messages.success(request, "Test delivery queued. Refresh in a few seconds to see the result.")
    return redirect("webhook_detail", endpoint_id=endpoint.pk)


# --------------------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------------------


@workspace_required
def billing_overview(request):
    organization = request.tenant.organization
    ensure_default_plans()
    report = current_usage(organization)
    subscription = get_subscription(organization)
    if request.GET.get("checkout") == "success":
        # The plan changes when Stripe's webhook arrives, which can be a moment after the
        # browser gets back; saying "done" here would be claiming something not yet true.
        messages.info(request, "Payment received. Your plan updates as soon as it is confirmed.")
    elif request.GET.get("checkout") == "cancelled":
        messages.info(request, "Checkout was cancelled. Nothing has been charged.")
    return render(
        request,
        "web/billing.html",
        {
            **report,
            "plans": Plan.objects.filter(is_public=True),
            "can_manage": request.tenant.can(Permission.BILLING_MANAGE),
            "subscription": subscription,
            "payments_enabled": stripe_gateway.is_configured(),
            "has_paid_subscription": stripe_gateway.has_processor_subscription(subscription),
            "purchasable": set(stripe_gateway.price_map()),
            "self_serve_changes": settings.BILLING_ALLOW_UNPAID_PLAN_CHANGES,
        },
    )


@permission_required(Permission.BILLING_MANAGE)
@require_POST
def billing_change_plan(request):
    organization = request.tenant.organization
    plan = get_object_or_404(Plan, code=request.POST.get("plan"), is_public=True)
    subscription = get_subscription(organization)

    if stripe_gateway.has_processor_subscription(subscription):
        # A paying workspace changes or cancels through the processor, so what is charged
        # and what is granted can never drift apart.
        return billing_portal(request)

    if plan.monthly_price_cents > 0:
        if stripe_gateway.is_configured():
            try:
                url = stripe_gateway.create_checkout_session(
                    organization=organization, plan=plan, actor=request.user
                )
            except stripe_gateway.BillingError as exc:
                messages.error(request, str(exc))
                return redirect("billing_overview")
            record_audit(
                action="billing.checkout_started",
                organization=organization,
                actor=request.user,
                request=request,
                metadata={"plan": plan.code},
            )
            return redirect(url)
        if not settings.BILLING_ALLOW_UNPAID_PLAN_CHANGES:
            messages.error(request, f"The {plan.name} plan cannot be purchased online yet. Contact us to upgrade.")
            return redirect("billing_overview")

    try:
        change_plan(organization=organization, plan=plan, actor=request.user, request=request)
    except QuotaExceeded as exc:
        # Refusing is kinder than accepting: a silent downgrade leaves the workspace
        # permanently over quota and unable to create anything.
        messages.error(request, f"Cannot switch to {plan.name}: {exc.resource}.")
    else:
        messages.success(request, f"Switched to the {plan.name} plan.")
    return redirect("billing_overview")


@permission_required(Permission.BILLING_MANAGE)
@require_POST
def billing_portal(request):
    """Hand the browser to the processor's portal (card, invoices, plan, cancellation)."""

    try:
        url = stripe_gateway.create_portal_session(organization=request.tenant.organization)
    except stripe_gateway.BillingError as exc:
        messages.error(request, str(exc))
        return redirect("billing_overview")
    return redirect(url)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Receive Stripe events.

    CSRF-exempt because the caller is Stripe, not a browser with a session; it is
    authenticated by the HMAC signature over the raw body instead. Anything that fails
    that check gets a 400 and no detail.
    """

    if not settings.STRIPE_WEBHOOK_SECRET:
        raise Http404("Not found.")
    try:
        event = stripe_gateway.verify_webhook(request.body, request.headers.get("Stripe-Signature", ""))
    except stripe_gateway.SignatureError:
        logger.warning("stripe webhook rejected")
        return HttpResponse(status=400)

    try:
        outcome = stripe_gateway.handle_event(event)
    except stripe_gateway.BillingError:
        # A 5xx tells Stripe to retry; the event has not been recorded, so it will be reapplied.
        return HttpResponse(status=503)
    return JsonResponse({"received": True, "outcome": outcome})


# --------------------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------------------


@feature_required("exports")
@require_GET
def export_tasks(request):
    request.tenant.require(Permission.DATA_EXPORT)
    from api.throttling import ExportRateThrottle

    if not ExportRateThrottle().allow_request(request, None):
        return HttpResponse("Export rate limit reached. Try again later.", status=429)

    organization = request.tenant.organization
    fmt = request.GET.get("format", "csv")
    include_archived = request.GET.get("include_archived") == "true"

    record_audit(
        action="data.exported",
        organization=organization,
        actor=request.user,
        request=request,
        metadata={"format": fmt, "include_archived": include_archived},
    )

    from exports.services import stream_tasks_csv, stream_tasks_json

    if fmt == "json":
        return stream_tasks_json(organization, include_archived=include_archived)
    return stream_tasks_csv(organization, include_archived=include_archived)


@feature_required("exports")
@require_GET
def export_workspace(request):
    """Full workspace archive as a single JSON document."""

    request.tenant.require(Permission.DATA_EXPORT)
    from exports.services import build_workspace_archive

    organization = request.tenant.organization
    record_audit(
        action="workspace.exported",
        organization=organization,
        actor=request.user,
        request=request,
    )
    response = JsonResponse(build_workspace_archive(organization), json_dumps_params={"indent": 2})
    response["Content-Disposition"] = f'attachment; filename="workspace-{organization.slug}.json"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --------------------------------------------------------------------------------------
# Account lifecycle
# --------------------------------------------------------------------------------------


@login_required_view
@require_GET
def account_export(request):
    """Everything the system holds about the signed-in person, across all workspaces."""

    record_audit(action="account.exported", actor=request.user, request=request)
    response = JsonResponse(build_personal_export(request.user), json_dumps_params={"indent": 2})
    response["Content-Disposition"] = (
        f'attachment; filename="my-data-{request.user.get_username()}.json"'
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required_view
def account_settings(request):
    return render(
        request,
        "web/account.html",
        {"blocking_workspaces": blocking_owned_workspaces(request.user)},
    )


@login_required_view
@require_POST
def account_delete(request):
    if request.POST.get("confirm") != request.user.get_username():
        messages.error(request, "Type your username exactly to confirm.")
        return redirect("account_settings")

    try:
        delete_account(
            user=request.user,
            request=request,
            delete_sole_owned_workspaces=request.POST.get("delete_workspaces") == "on",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("account_settings")

    logout(request)
    messages.success(request, "Your account has been deleted.")
    return redirect("login")


@workspace_required
@require_POST
def workspace_leave(request):
    organization = request.tenant.organization
    try:
        leave_workspace(user=request.user, organization=organization, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("settings_workspace")

    request.session.pop("active_organization_id", None)
    messages.success(request, f"You have left {organization.name}.")
    return redirect("dashboard")
