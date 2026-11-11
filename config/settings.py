"""Django settings for the Task Manager SaaS.

Configuration is environment-first. ``DJANGO_ENV`` selects a profile:

* ``development`` (default) — debug on, permissive hosts, local-memory cache.
* ``test`` — debug off, deterministic in-memory backends, no external services.
* ``production`` — debug forced off, secrets required, TLS/HSTS/secure cookies enforced.

Nothing security-relevant falls back to an insecure default in production; the module
raises ``ImproperlyConfigured`` at import time instead of booting a vulnerable process.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .env import env_bool, env_int, env_list, env_str, load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------------------
# Environment profile
# --------------------------------------------------------------------------------------

ENVIRONMENT = env_str("DJANGO_ENV", "development").strip().lower()
if ENVIRONMENT not in {"development", "test", "production"}:
    raise ImproperlyConfigured(
        f"DJANGO_ENV must be one of development|test|production, got {ENVIRONMENT!r}."
    )

RUNNING_TESTS = (
    ENVIRONMENT == "test"
    or "test" in sys.argv
    or "pytest" in Path(sys.argv[0]).name
    or bool(env_str("PYTEST_CURRENT_TEST", ""))
)
IS_PRODUCTION = ENVIRONMENT == "production"

# Debug defaults OFF. A deploy that forgets the flag fails closed, not open.
DEBUG = env_bool("DJANGO_DEBUG", default=not IS_PRODUCTION and not RUNNING_TESTS)
if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured("DJANGO_DEBUG must be disabled when DJANGO_ENV=production.")

# --------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------

MIN_SECRET_KEY_LENGTH = 50

SECRET_KEY = env_str("DJANGO_SECRET_KEY", required=IS_PRODUCTION)
if not SECRET_KEY:
    # Ephemeral per-process key: usable for local dev and tests, useless to an attacker
    # and impossible to accidentally promote to production (production requires the env var).
    SECRET_KEY = "dev-only-" + secrets.token_urlsafe(50)
elif IS_PRODUCTION:
    # Fail at boot rather than relying on someone remembering to run `check --deploy`.
    # A short or low-entropy key undermines session signing, password reset tokens and
    # every other signed value in the framework.
    #
    # The placeholder check runs first because its message is the more actionable one:
    # "replace the generated key" tells the operator exactly what happened.
    if SECRET_KEY.startswith("django-insecure-"):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is a Django-generated placeholder. Replace it before deploying."
        )
    if len(SECRET_KEY) < MIN_SECRET_KEY_LENGTH or len(set(SECRET_KEY)) < 10:
        raise ImproperlyConfigured(
            f"DJANGO_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters with "
            "meaningful entropy. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )

# Rotation support: old keys keep existing sessions/signatures valid after a key change.
SECRET_KEY_FALLBACKS = env_list("DJANGO_SECRET_KEY_FALLBACKS")

# --------------------------------------------------------------------------------------
# Hosts and origins
# --------------------------------------------------------------------------------------

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost", "[::1]"])
if IS_PRODUCTION and not env_list("DJANGO_ALLOWED_HOSTS"):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set when DJANGO_ENV=production.")
if RUNNING_TESTS:
    ALLOWED_HOSTS = sorted({*ALLOWED_HOSTS, "testserver"})

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

SITE_URL = env_str("SITE_URL", "http://127.0.0.1:8000").rstrip("/")

# --------------------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "rest_framework",
    "kernel",
    "tasks",
    "queues",
    "schedules",
    "billing",
    "integrations",
    "identity",
    "api",
    "web",
    "support",
    "exports",
    "ops",
]

MIDDLEWARE = [
    "ops.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "kernel.middleware.ActiveOrganizationMiddleware",
    "identity.middleware.StaffMfaMiddleware",
    "kernel.middleware.TenantRLSMiddleware",
    "ops.middleware.SecurityHeadersMiddleware",
    "ops.middleware.RequestTimingMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.context_processors.workspace",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

DB_ENGINE = env_str("DB_ENGINE", "django.db.backends.sqlite3")
_IS_SQLITE = DB_ENGINE.endswith("sqlite3")

DATABASES = {
    "default": {
        "ENGINE": DB_ENGINE,
        "NAME": env_str("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER": env_str("DB_USER", ""),
        "PASSWORD": env_str("DB_PASSWORD", ""),
        "HOST": env_str("DB_HOST", ""),
        "PORT": env_str("DB_PORT", ""),
        # Persistent connections: a fresh TCP+TLS handshake per request is the single
        # biggest avoidable latency cost on managed Postgres.
        "CONN_MAX_AGE": 0 if _IS_SQLITE else env_int("DB_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": not _IS_SQLITE,
        "OPTIONS": {},
    }
}

if _IS_SQLITE:
    # WAL + a real busy timeout stops "database is locked" under the worker/web split.
    DATABASES["default"]["OPTIONS"] = {
        "timeout": 20,
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;",
        "transaction_mode": "IMMEDIATE",
    }
elif env_bool("DB_REQUIRE_SSL", default=IS_PRODUCTION):
    DATABASES["default"]["OPTIONS"]["sslmode"] = env_str("DB_SSLMODE", "require")

if IS_PRODUCTION and _IS_SQLITE:
    raise ImproperlyConfigured(
        "SQLite is not supported when DJANGO_ENV=production. Set DB_ENGINE to a "
        "server database (e.g. django.db.backends.postgresql)."
    )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------------------
# Cache — backs throttling, login lockout, and idempotency guards
# --------------------------------------------------------------------------------------

REDIS_URL = env_str("REDIS_URL", "")

if REDIS_URL and not RUNNING_TESTS:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": env_str("CACHE_KEY_PREFIX", "taskmgr"),
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "task-manager-default",
            "KEY_PREFIX": env_str("CACHE_KEY_PREFIX", "taskmgr"),
        }
    }

if IS_PRODUCTION and not REDIS_URL:
    raise ImproperlyConfigured(
        "REDIS_URL must be set when DJANGO_ENV=production: rate limiting and login "
        "lockouts are unsafe on a per-process local-memory cache."
    )

# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": env_int("PASSWORD_MIN_LENGTH", 12)},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

try:  # pragma: no cover - depends on optional dependency
    import argon2  # noqa: F401

    PASSWORD_HASHERS.insert(0, "django.contrib.auth.hashers.Argon2PasswordHasher")
except ImportError:  # pragma: no cover
    pass

if RUNNING_TESTS:
    # Tests create hundreds of users; a real KDF makes the suite far slower for no signal.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

AUTHENTICATION_BACKENDS = ["kernel.auth_backends.LockoutModelBackend"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 60 * 60 * 12)
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("SESSION_EXPIRE_AT_BROWSER_CLOSE", False)
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the cookie to set the X-CSRFToken header.

# Login brute-force lockout (see kernel/auth_backends.py).
LOGIN_LOCKOUT_MAX_ATTEMPTS = env_int("LOGIN_LOCKOUT_MAX_ATTEMPTS", 8)
LOGIN_LOCKOUT_WINDOW_SECONDS = env_int("LOGIN_LOCKOUT_WINDOW_SECONDS", 900)
LOGIN_LOCKOUT_COOLDOWN_SECONDS = env_int("LOGIN_LOCKOUT_COOLDOWN_SECONDS", 900)

# --------------------------------------------------------------------------------------
# Security headers / transport
# --------------------------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

BEHIND_TLS_PROXY = env_bool("BEHIND_TLS_PROXY", default=IS_PRODUCTION)
if BEHIND_TLS_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", True)

SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=IS_PRODUCTION)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", default=IS_PRODUCTION)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", default=IS_PRODUCTION)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000 if IS_PRODUCTION else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", IS_PRODUCTION)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", IS_PRODUCTION)

# Content-Security-Policy is applied by ops.middleware.SecurityHeadersMiddleware.
# All first-party assets are served locally, so the policy needs no CDN allowances.
CONTENT_SECURITY_POLICY = env_str(
    "CONTENT_SECURITY_POLICY",
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'",
)
PERMISSIONS_POLICY = env_str(
    "PERMISSIONS_POLICY",
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=()",
)

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("FILE_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env_int("DATA_UPLOAD_MAX_NUMBER_FIELDS", 1000)

ADMIN_URL = env_str("DJANGO_ADMIN_URL", "admin/").strip("/") + "/"

# --------------------------------------------------------------------------------------
# Internationalization
# --------------------------------------------------------------------------------------

LANGUAGE_CODE = env_str("DJANGO_LANGUAGE_CODE", "en-us")
TIME_ZONE = env_str("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------------------
# Static and media files
# --------------------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = Path(env_str("MEDIA_ROOT", str(BASE_DIR / "media")))
# filesystem (default) | s3 — see config.storage and docs/STORAGE.md
MEDIA_BACKEND = env_str("MEDIA_BACKEND", "filesystem")

from .storage import build_default_storage  # noqa: E402

STORAGES = {
    "default": build_default_storage(
        backend="filesystem" if RUNNING_TESTS else MEDIA_BACKEND,
        env_str=env_str,
        env_bool=env_bool,
    ),
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or RUNNING_TESTS
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
WHITENOISE_MAX_AGE = env_int("WHITENOISE_MAX_AGE", 31536000)

# Attachment limits (see tasks.models.Attachment).
ATTACHMENT_MAX_BYTES = env_int("ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)
ATTACHMENT_ALLOWED_EXTENSIONS = env_list(
    "ATTACHMENT_ALLOWED_EXTENSIONS",
    ["png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "md", "csv", "json", "log", "zip"],
)
ATTACHMENT_URL_TTL_SECONDS = env_int("ATTACHMENT_URL_TTL_SECONDS", 300)

# Malware scanning of uploads. "" disables it; "clamav" sends every upload to clamd and
# blocks downloads until it is declared clean.
ATTACHMENT_SCAN_BACKEND = env_str("ATTACHMENT_SCAN_BACKEND", "").lower()
if ATTACHMENT_SCAN_BACKEND not in ("", "clamav"):
    raise ImproperlyConfigured("ATTACHMENT_SCAN_BACKEND must be empty or 'clamav'.")
CLAMAV_HOST = env_str("CLAMAV_HOST", "clamav")
CLAMAV_PORT = env_int("CLAMAV_PORT", 3310)
CLAMAV_TIMEOUT_SECONDS = env_int("CLAMAV_TIMEOUT_SECONDS", 30)

# --------------------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------------------

if RUNNING_TESTS:
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
else:
    EMAIL_BACKEND = env_str("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

EMAIL_HOST = env_str("EMAIL_HOST", "localhost")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 10)
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", "Task Manager <no-reply@example.com>")
SERVER_EMAIL = env_str("SERVER_EMAIL", DEFAULT_FROM_EMAIL)

PASSWORD_RESET_TIMEOUT = env_int("PASSWORD_RESET_TIMEOUT", 60 * 60 * 2)

# Two-step verification and single sign-on.
# Keys that encrypt stored TOTP seeds and SSO client secrets, newest first; all can
# decrypt. Empty means a key derived from DJANGO_SECRET_KEY (see identity.W001).
FIELD_ENCRYPTION_KEYS = env_list("FIELD_ENCRYPTION_KEYS")
MFA_ISSUER = env_str("MFA_ISSUER", "Task Manager")
MFA_MAX_FAILURES = env_int("MFA_MAX_FAILURES", 5)
MFA_LOCKOUT_SECONDS = env_int("MFA_LOCKOUT_SECONDS", 900)
# Platform staff/superusers must enrol and pass TOTP. On by default in production.
STAFF_REQUIRE_MFA = env_bool("STAFF_REQUIRE_MFA", IS_PRODUCTION)
# Plain-http identity providers are for local development against a test IdP only.
SSO_ALLOW_INSECURE_PROVIDERS = env_bool("SSO_ALLOW_INSECURE_PROVIDERS", False) and not IS_PRODUCTION

# --------------------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.ApiKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "api.pagination.StandardPagination",
    "PAGE_SIZE": env_int("API_PAGE_SIZE", 50),
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "api.throttling.ScopedUserRateThrottle",
        "api.throttling.ScopedAnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env_str("THROTTLE_ANON", "30/minute"),
        "user": env_str("THROTTLE_USER", "1000/hour"),
        "burst": env_str("THROTTLE_BURST", "60/minute"),
        "auth": env_str("THROTTLE_AUTH", "10/minute"),
        "write": env_str("THROTTLE_WRITE", "300/hour"),
        "export": env_str("THROTTLE_EXPORT", "10/hour"),
    },
    "EXCEPTION_HANDLER": "api.exceptions.api_exception_handler",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + ([] if IS_PRODUCTION else ["rest_framework.renderers.BrowsableAPIRenderer"]),
}

API_KEY_PREFIX = "tmk"

# Throttling is unreliable when every test shares one LocMem cache; tests that exercise
# throttling opt in explicitly with @override_settings.
API_THROTTLING_ENABLED = env_bool("API_THROTTLING_ENABLED", not RUNNING_TESTS)

# --------------------------------------------------------------------------------------
# Celery / durable queue runtime
# --------------------------------------------------------------------------------------

CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", REDIS_URL or "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env_str("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", RUNNING_TESTS)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = env_int("CELERY_WORKER_PREFETCH_MULTIPLIER", 1)
CELERY_WORKER_MAX_TASKS_PER_CHILD = env_int("CELERY_WORKER_MAX_TASKS_PER_CHILD", 500)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT", 240)
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 300)
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

# Dispatcher / worker / reaper tuning. These are product-level knobs, not Celery ones.
QUEUE_DISPATCH_BATCH_SIZE = env_int("QUEUE_DISPATCH_BATCH_SIZE", 100)
# How long one dispatcher tick keeps claiming. Must stay below the beat interval (10s).
QUEUE_DISPATCH_TIME_BUDGET_SECONDS = env_int("QUEUE_DISPATCH_TIME_BUDGET_SECONDS", 8)
QUEUE_DEFAULT_LEASE_SECONDS = env_int("QUEUE_DEFAULT_LEASE_SECONDS", 300)
QUEUE_RETRY_BASE_SECONDS = env_int("QUEUE_RETRY_BASE_SECONDS", 10)
QUEUE_RETRY_MAX_SECONDS = env_int("QUEUE_RETRY_MAX_SECONDS", 3600)
QUEUE_RETRY_JITTER_RATIO = float(env_str("QUEUE_RETRY_JITTER_RATIO", "0.2"))
QUEUE_BACKLOG_ALERT_THRESHOLD = env_int("QUEUE_BACKLOG_ALERT_THRESHOLD", 500)
QUEUE_OLDEST_JOB_ALERT_SECONDS = env_int("QUEUE_OLDEST_JOB_ALERT_SECONDS", 900)
# Weighted tenant fairness when claiming across many workspaces. Off only for diagnostics.
QUEUE_FAIRNESS_ENABLED = env_bool("QUEUE_FAIRNESS_ENABLED", True)
QUEUE_FAIRNESS_WINDOW_SECONDS = env_int("QUEUE_FAIRNESS_WINDOW_SECONDS", 60)

SCHEDULE_SCAN_HORIZON_SECONDS = env_int("SCHEDULE_SCAN_HORIZON_SECONDS", 60)
SCHEDULE_CATCH_UP_MAX_OCCURRENCES = env_int("SCHEDULE_CATCH_UP_MAX_OCCURRENCES", 100)

# Outbound HTTP delivery (webhooks).
WEBHOOK_CONNECT_TIMEOUT = float(env_str("WEBHOOK_CONNECT_TIMEOUT", "5"))
WEBHOOK_READ_TIMEOUT = float(env_str("WEBHOOK_READ_TIMEOUT", "10"))
WEBHOOK_MAX_RESPONSE_BYTES = env_int("WEBHOOK_MAX_RESPONSE_BYTES", 64 * 1024)
WEBHOOK_ALLOW_PRIVATE_NETWORKS = env_bool("WEBHOOK_ALLOW_PRIVATE_NETWORKS", not IS_PRODUCTION)
WEBHOOK_USER_AGENT = env_str("WEBHOOK_USER_AGENT", "TaskManager-Webhooks/1.0")

# --------------------------------------------------------------------------------------
# Plans and quotas
# --------------------------------------------------------------------------------------

DEFAULT_PLAN = env_str("DEFAULT_PLAN", "free")
BILLING_TRIAL_DAYS = env_int("BILLING_TRIAL_DAYS", 14)

# Optional Postgres row-level security. Application tenancy remains the primary control;
# when enabled, SET LOCAL app.current_org_id scopes SELECT/UPDATE/DELETE on tenant tables.
# Requires running ``python manage.py enable_rls`` once against the database.
ENABLE_POSTGRES_RLS = env_bool("ENABLE_POSTGRES_RLS", False)

# Payment processor (Stripe). Leave STRIPE_SECRET_KEY empty to run without one.
STRIPE_SECRET_KEY = env_str("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env_str("STRIPE_WEBHOOK_SECRET", "")
# "team=price_123,business=price_456": which Stripe price sells which plan.
STRIPE_PRICE_IDS = dict(
    item.split("=", 1) for item in env_list("STRIPE_PRICE_IDS") if "=" in item
)
if STRIPE_SECRET_KEY and not STRIPE_WEBHOOK_SECRET:
    raise ImproperlyConfigured(
        "STRIPE_WEBHOOK_SECRET must be set when STRIPE_SECRET_KEY is: without it webhooks "
        "cannot be authenticated and paid subscriptions would never be applied."
    )
if IS_PRODUCTION and STRIPE_SECRET_KEY.startswith("sk_test_") and not env_bool("STRIPE_ALLOW_TEST_KEY", False):
    raise ImproperlyConfigured(
        "STRIPE_SECRET_KEY is a test-mode key but DJANGO_ENV=production. Set a live key, or "
        "STRIPE_ALLOW_TEST_KEY=1 for a staging environment."
    )
# Without a processor, may a workspace move itself onto a paid plan for free? Fine for
# development and demos; in production it would be giving the product away.
BILLING_ALLOW_UNPAID_PLAN_CHANGES = env_bool("BILLING_ALLOW_UNPAID_PLAN_CHANGES", not IS_PRODUCTION)

# --------------------------------------------------------------------------------------
# Feature flags (per-org overrides live in kernel.models.FeatureFlag)
# --------------------------------------------------------------------------------------

FEATURE_FLAG_DEFAULTS = {
    "board_view": True,
    "recurring_tasks": True,
    "webhooks": True,
    "attachments": True,
    "exports": True,
    "api_keys": True,
    "digest_email": True,
    "analytics": True,
    "sso": True,
}

# --------------------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------------------

LOG_LEVEL = env_str("DJANGO_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = env_str("DJANGO_LOG_FORMAT", "json" if IS_PRODUCTION else "console").lower()
if LOG_FORMAT not in {"json", "console"}:
    LOG_FORMAT = "console"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "ops.logging.RequestIDFilter"},
    },
    "formatters": {
        "json": {"()": "ops.logging.JSONFormatter"},
        "console": {
            "format": "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": LOG_FORMAT,
        },
        "null": {"class": "logging.NullHandler"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.db.backends": {"handlers": ["null"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "taskmanager": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "taskmanager.audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

if RUNNING_TESTS:
    LOGGING["root"]["handlers"] = ["null"]
    for _logger in LOGGING["loggers"].values():
        _logger["handlers"] = ["null"]

SENTRY_DSN = env_str("SENTRY_DSN", "")
APP_RELEASE = env_str("APP_RELEASE", "dev")

if SENTRY_DSN and not RUNNING_TESTS:  # pragma: no cover - optional dependency
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration(), CeleryIntegration()],
            environment=ENVIRONMENT,
            release=APP_RELEASE,
            traces_sample_rate=float(env_str("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
            send_default_pii=False,
        )
    except ImportError:
        pass

# Slow-request visibility (see ops.middleware.RequestTimingMiddleware).
SLOW_REQUEST_MS = env_int("SLOW_REQUEST_MS", 1000)

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
