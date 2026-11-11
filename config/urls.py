"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import views as auth_views
from django.urls import include, path

from web.forms import BrandedAuthenticationForm
from web.views_auth import ThrottledLoginView

# The admin has its own login form, which knows nothing about two-step verification or
# login throttling. Routing it through the product's sign-in closes that side door.
admin.site.login = staff_member_required(admin.site.login, login_url="login")

handler400 = "ops.views.bad_request"
handler403 = "ops.views.permission_denied"
handler404 = "ops.views.page_not_found"
handler500 = "ops.views.server_error"

urlpatterns = [
    path("", include("ops.urls")),
    path("api/v1/", include("api.urls")),
    # Authentication. LoginView is wrapped so the HTML form gets the same rate limiting
    # the API does; without it the login endpoint is the softest target in the app.
    path(
        "accounts/login/",
        ThrottledLoginView.as_view(
            template_name="registration/login.html",
            authentication_form=BrandedAuthenticationForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change_form.html",
            success_url="/accounts/password-change/done/",
        ),
        name="password_change",
    ),
    path(
        "accounts/password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html"
        ),
        name="password_change_done",
    ),
    path(
        "accounts/password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            success_url="/accounts/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "accounts/password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url="/accounts/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "accounts/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
    path("", include("identity.urls")),
    # The admin path is configurable so production can move it off the guessable default.
    path(settings.ADMIN_URL, admin.site.urls),
    # Registered last: web.urls owns "" and would otherwise shadow everything above.
    path("", include("web.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
