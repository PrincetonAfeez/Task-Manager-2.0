from django.urls import path

from . import views, webauthn_views

urlpatterns = [
    path("accounts/mfa/", views.mfa_verify, name="mfa_verify"),
    path("accounts/mfa/webauthn/begin/", webauthn_views.webauthn_authenticate_begin, name="webauthn_auth_begin"),
    path("accounts/mfa/webauthn/finish/", webauthn_views.webauthn_authenticate_finish, name="webauthn_auth_finish"),
    path("account/security/", views.account_security, name="account_security"),
    path("account/security/mfa/start/", views.mfa_start, name="mfa_start"),
    path("account/security/mfa/setup/", views.mfa_setup, name="mfa_setup"),
    path("account/security/mfa/qr.svg", views.mfa_qr, name="mfa_qr"),
    path("account/security/mfa/disable/", views.mfa_disable, name="mfa_disable"),
    path("account/security/mfa/recovery-codes/", views.mfa_recovery_codes, name="mfa_recovery_codes"),
    path(
        "account/security/webauthn/register/begin/",
        webauthn_views.webauthn_register_begin,
        name="webauthn_register_begin",
    ),
    path(
        "account/security/webauthn/register/finish/",
        webauthn_views.webauthn_register_finish,
        name="webauthn_register_finish",
    ),
    path(
        "account/security/webauthn/<int:credential_id>/delete/",
        webauthn_views.webauthn_delete,
        name="webauthn_delete",
    ),
    path("settings/security/", views.workspace_security, name="workspace_security"),
    path("sso/", views.sso_start, name="sso_start"),
    # Registered before the slug route so "callback" is never read as a workspace slug.
    path("sso/callback/", views.sso_callback, name="sso_callback"),
    path("sso/<slug:slug>/", views.sso_login, name="sso_login"),
]
