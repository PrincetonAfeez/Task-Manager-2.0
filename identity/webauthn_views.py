"""JSON endpoints for WebAuthn ceremonies in the browser."""

from __future__ import annotations

import json

from django.contrib.auth import login
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from kernel.services import record_audit
from web.decorators import login_required_view

from . import mfa, webauthn_service
from .models import WebAuthnCredential
from .views import PENDING_KEY, _pending_user, _safe_next

# Re-exported for tests and middleware path checks.
__all__ = [
    "webauthn_authenticate_begin",
    "webauthn_authenticate_finish",
    "webauthn_delete",
    "webauthn_register_begin",
    "webauthn_register_finish",
]


def _json_error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _parse_json_body(request) -> dict | None:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _mfa_subject(request):
    if request.user.is_authenticated:
        return request.user, None, True
    user, pending = _pending_user(request)
    if user is None:
        return None, None, False
    return user, pending, False


def _complete_mfa_success(request, *, user, pending, stepping_up: bool, next_url: str | None) -> JsonResponse:
    if stepping_up:
        destination = _safe_next(request, next_url)
    else:
        request.session.pop(PENDING_KEY, None)
        login(request, user, backend=pending["backend"])
        destination = _safe_next(request, pending.get("next"))
    request.session[mfa.SESSION_VERIFIED_KEY] = True
    mfa._clear_failures(user)
    record_audit(
        action="auth.login_succeeded" if not stepping_up else "auth.mfa_step_up",
        actor=user,
        request=request,
        metadata={"second_factor": "webauthn"},
    )
    return JsonResponse({"redirect": destination})


@login_required_view
@never_cache
@require_POST
def webauthn_register_begin(request):
    try:
        options = webauthn_service.begin_registration(request, request.user)
    except webauthn_service.WebAuthnError as exc:
        return _json_error(str(exc))
    return JsonResponse({"options": options})


@login_required_view
@never_cache
@require_POST
def webauthn_register_finish(request):
    payload = _parse_json_body(request)
    if payload is None:
        return _json_error("Invalid JSON.")
    credential = payload.get("credential")
    if not isinstance(credential, dict):
        return _json_error("A passkey response is required.")

    label = str(payload.get("label", "Passkey"))
    try:
        device = webauthn_service.finish_registration(request, request.user, credential, label)
    except webauthn_service.WebAuthnError as exc:
        return _json_error(str(exc))

    record_audit(
        action="auth.webauthn_registered",
        actor=request.user,
        request=request,
        target_type="webauthn_credential",
        target_id=device.pk,
        metadata={"label": device.label},
    )
    return JsonResponse({"ok": True, "id": device.pk, "label": device.label})


@never_cache
@require_POST
def webauthn_authenticate_begin(request):
    user, _pending, stepping_up = _mfa_subject(request)
    if user is None:
        return _json_error("Sign in again to continue.", status=401)
    if stepping_up and not mfa.user_has_mfa(user):
        return _json_error("Two-step verification is not enabled.", status=403)

    try:
        options = webauthn_service.begin_authentication(request, user)
    except webauthn_service.WebAuthnError as exc:
        return _json_error(str(exc))
    return JsonResponse({"options": options})


@never_cache
@require_POST
def webauthn_authenticate_finish(request):
    payload = _parse_json_body(request)
    if payload is None:
        return _json_error("Invalid JSON.")
    credential = payload.get("credential")
    if not isinstance(credential, dict):
        return _json_error("A passkey response is required.")

    user, pending, stepping_up = _mfa_subject(request)
    if user is None:
        return _json_error("Sign in again to continue.", status=401)

    try:
        webauthn_service.finish_authentication(request, user, credential)
    except webauthn_service.WebAuthnError as exc:
        record_audit(action="auth.mfa_failed", actor=user, request=request)
        return _json_error(str(exc))

    return _complete_mfa_success(
        request,
        user=user,
        pending=pending,
        stepping_up=stepping_up,
        next_url=payload.get("next"),
    )


@login_required_view
@require_POST
def webauthn_delete(request, credential_id: int):
    deleted, _ = WebAuthnCredential.objects.filter(pk=credential_id, user=request.user).delete()
    if not deleted:
        raise Http404("Passkey not found.")
    record_audit(action="auth.webauthn_deleted", actor=request.user, request=request, target_id=credential_id)
    return redirect("account_security")
