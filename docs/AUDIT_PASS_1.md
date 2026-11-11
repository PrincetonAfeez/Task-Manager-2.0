# Audit Pass 1 — Security, Authentication, and Configuration

> "This is an enterprise-grade, production-ready SaaS Product. Analyze the code, identify all
> bugs, errors, gaps, and inconsistencies, and document the corresponding fixes. Give me a
> definitive, finite list."

Scope of this pass: everything an attacker can reach, plus configuration that decides whether the
product is safe to deploy at all.

| # | Severity | Finding | Evidence | Fix |
|---|----------|---------|----------|-----|
| 1.1 | Critical | Entire `/api/v1/` surface is unauthenticated and unscoped. Every viewset is a bare `ModelViewSet` over `.objects.all()`, so any anonymous caller can read, mutate, and delete **every tenant's** organizations, tasks, jobs, schedules and notifications. | `api/views.py:1-72` — no `permission_classes`, no `authentication_classes`, no queryset filtering | Add project-wide DRF defaults (`IsAuthenticated` + session/API-key auth), an `OrganizationScopedViewSet` base that filters by the caller's memberships, and role-based write permissions. |
| 1.2 | Critical | Cross-tenant object access via primary key. `retrieve`/`update`/`destroy` resolve objects from the global queryset, so `GET /api/v1/tasks/1/` returns another tenant's task. | `api/views.py:41` `Task.objects...all()` | Scope `get_queryset()` per request; enforce on detail routes too. |
| 1.3 | Critical | Anonymous users are silently logged into a shared "demo" workspace with a known password. `get_request_context()` calls `get_or_create_demo_context()` for *any* unauthenticated request, which creates/keeps a `demo/demo` superuser-adjacent account and hands the anonymous caller write access to that org. | `tasks/services.py:47-59`; `web/views.py:22` | Require authentication on all web views. Move demo provisioning to the `seed_demo` management command only, and refuse to run it when `DJANGO_ENV=production`. |
| 1.4 | High | Every API list endpoint returns HTTP 500. `CursorPagination` defaults to `ordering = "-created"`, a field no model has. | `config/settings.py:147-150`; verified: `FieldError: Cannot resolve keyword 'created' into field` on all 11 routes | Replace with an explicit `PageNumberPagination`/`CursorPagination` subclass ordering on `-created_at`, and add `ORDERING`/filter backends. |
| 1.5 | High | `DEBUG` defaults to **on**. A deploy that forgets `DJANGO_DEBUG=0` leaks stack traces, settings and SQL. | `config/settings.py:32` `os.environ.get("DJANGO_DEBUG", "1") == "1"` | Default `DEBUG` off; derive from an explicit `DJANGO_ENV`, and hard-fail if `DEBUG` is on in production. |
| 1.6 | High | Hardcoded fallback `SECRET_KEY` ships in source and is used whenever the env var is missing — including in production. | `config/settings.py:26-29` | Require `DJANGO_SECRET_KEY` in production (`ImproperlyConfigured` otherwise); generate an ephemeral key only for local/dev/test. |
| 1.7 | High | No transport or cookie security settings at all: no `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_PROXY_SSL_HEADER`, `X_FRAME_OPTIONS`, or referrer policy. | `config/settings.py` — absent | Add a production-gated security block; `manage.py check --deploy` must come back clean. |
| 1.8 | High | `ALLOWED_HOSTS` silently accepts an empty string entry (`"".split(",") == [""]`) and `CSRF_TRUSTED_ORIGINS` is never configured, so HTTPS deployments behind a proxy fail CSRF. | `config/settings.py:34` | Parse and strip the list; add `CSRF_TRUSTED_ORIGINS` from env. |
| 1.9 | High | No brute-force protection on login, and no rate limiting on any endpoint. | no throttle classes in `config/settings.py`; `config/urls.py:19` uses stock `LoginView` | Add DRF throttling (anon/user/burst/auth scopes) and a login-attempt lockout backed by the cache. |
| 1.10 | High | Static files are unserved in production. `whitenoise` is a declared dependency but is not in `MIDDLEWARE` and no `STORAGES` backend is configured; with `DEBUG=0` every asset 404s. | `requirements.txt:9` vs `config/settings.py:59-68` | Insert `WhiteNoiseMiddleware` and configure the compressed manifest static storage. |
| 1.11 | High | A populated `db.sqlite3` (548 KB, real user rows and password hashes) sits in the working tree with no `.gitignore` anywhere in the repo. | `db.sqlite3`; no `.gitignore` file exists | Add a comprehensive `.gitignore`; document that the checked-in DB must be purged. |
| 1.12 | High | No logging configuration. Unhandled 500s vanish; there is no request correlation and no error sink. | `config/settings.py` — no `LOGGING` | Add structured JSON logging, a request-ID middleware/filter, and optional Sentry wiring. |
| 1.13 | Medium | CSRF/XSS exposure from an unpinned third-party script tag: `htmx` is loaded from `unpkg.com` with no SRI hash and no CSP. | `templates/base.html:11` | Vendor htmx locally, serve it from static, and add a Content-Security-Policy header. |
| 1.14 | Medium | `web.views.toggle_task` and `quick_add_create` are state-changing but have no `login_required`; combined with 1.3 they are anonymous write endpoints. | `web/views.py:37-60` | `@login_required` on every web view plus org-membership resolution. |
| 1.15 | Medium | Passwords use Django's default PBKDF2 with default iterations and no Argon2 available. | `config/settings.py` — no `PASSWORD_HASHERS` | Prefer Argon2 when installed, keep PBKDF2 fallback; add `argon2-cffi` to requirements. |
| 1.16 | Medium | `AUTH_PASSWORD_VALIDATORS` uses the default 8-character minimum. | `config/settings.py:108-121` | Raise `MinimumLengthValidator` to 12 for a product holding customer data. |
| 1.17 | Medium | Session cookies never expire on browser close and have no idle timeout; there is no session invalidation on password change beyond Django's default. | `config/settings.py` — no `SESSION_COOKIE_AGE` | Configure `SESSION_COOKIE_AGE`, `SESSION_EXPIRE_AT_BROWSER_CLOSE`, `SameSite=Lax`. |
| 1.18 | Medium | `Notification`, `AuditLog` and `Activity` are per-organization but the API exposed them with no scoping *and* `NotificationViewSet` allowed cross-user writes. | `api/views.py:66-72` | Scope notifications to `request.user` within the active org; make `Activity`/`AuditLog` read-only. |
| 1.19 | Medium | `DATABASES` has no `CONN_MAX_AGE`, no connection health checks, and no `ATOMIC_REQUESTS`; on Postgres this opens a new connection per request. | `config/settings.py:93-102` | Add `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`, and SQLite pragmas for local dev. |
| 1.20 | Medium | No cache backend configured, so throttling, lockouts and idempotency have nowhere to live. | `config/settings.py` — no `CACHES` | Configure Redis cache with a LocMem fallback for dev/test. |
| 1.21 | Low | Django admin is mounted at the guessable default `/admin/` with no extra gating. | `config/urls.py:23` | Make the admin path configurable via `DJANGO_ADMIN_URL` and default it to a non-obvious value in production. |
| 1.22 | Low | `LOGOUT_REDIRECT_URL = "dashboard"` sends signed-out users to a login-required page, producing a redirect bounce. | `config/settings.py:145` | Redirect to `login`. |
| 1.23 | Low | Dead scaffolding: `# Create your models here.` / `# Create your views here.` trailers in nine modules, and `api/models.py`, `exports/*`, `support/*` are empty stubs registered in `INSTALLED_APPS`. | `kernel/models.py:105`, `api/views.py:72`, etc. | Remove the trailers; give the placeholder apps real content (later passes) or drop them. |

## Verification

- `python manage.py check --deploy --fail-level WARNING` with `DJANGO_ENV=production` returns clean.
- New tests assert: anonymous API access is 401/403, cross-tenant reads are 404, list endpoints return 200,
  login throttling engages, and security headers are present.
