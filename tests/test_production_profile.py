"""The production settings profile.

Configuration is the most common source of a security incident and the least likely to be
tested, because the settings module runs once at import. These tests import it in a
subprocess with a controlled environment, so each profile is exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BASE_PRODUCTION_ENV = {
    "DJANGO_ENV": "production",
    "DJANGO_DEBUG": "0",
    "DJANGO_SECRET_KEY": "x7Kq2mZ9pL4vR8wT1yU6iO3aS5dF0gH!jK@lN#bV%cX^zQ&eW*rT(yU)iO_pA-sD",
    "DJANGO_ALLOWED_HOSTS": "tasks.example.com",
    "DB_ENGINE": "django.db.backends.postgresql",
    "DB_NAME": "task_manager",
    "REDIS_URL": "redis://localhost:6379/0",
}


def load_settings(overrides: dict | None = None, *, keys: list[str] | None = None):
    """Import ``config.settings`` in a subprocess and return the requested values.

    Returns ``{"error": "..."}`` when the module refuses to import, which is the behaviour
    several of these tests are actually asserting.
    """

    env = {**os.environ, **BASE_PRODUCTION_ENV, **(overrides or {})}
    env.pop("PYTEST_CURRENT_TEST", None)
    for key, value in list(env.items()):
        if value is None:
            env.pop(key)

    requested = keys or []
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        try:
            from config import settings
        except Exception as exc:
            print(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}}))
            sys.exit(0)
        wanted = {requested!r}
        out = {{}}
        for name in wanted:
            value = getattr(settings, name, None)
            out[name] = value if isinstance(value, (str, int, float, bool, type(None), list)) else str(value)
        print(json.dumps(out))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=90,
    )
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return {"error": (result.stderr or "no output")[-400:]}
    return json.loads(line[-1])


class TestSecretKey:
    def test_production_requires_a_secret_key(self):
        result = load_settings({"DJANGO_SECRET_KEY": ""}, keys=["SECRET_KEY"])
        assert "error" in result
        assert "DJANGO_SECRET_KEY" in result["error"]

    def test_a_short_key_is_refused(self):
        result = load_settings({"DJANGO_SECRET_KEY": "tooshort"}, keys=["SECRET_KEY"])
        assert "error" in result
        assert "at least" in result["error"]

    def test_a_low_entropy_key_is_refused(self):
        result = load_settings({"DJANGO_SECRET_KEY": "a" * 80}, keys=["SECRET_KEY"])
        assert "error" in result

    def test_a_django_placeholder_key_is_refused(self):
        result = load_settings(
            {"DJANGO_SECRET_KEY": "django-insecure-" + "q7W!e2R@t4Y#u6I$o8P%a1S^d3F&g5H*"},
            keys=["SECRET_KEY"],
        )
        assert "error" in result
        assert "placeholder" in result["error"]

    def test_development_generates_an_ephemeral_key(self):
        result = load_settings(
            {"DJANGO_ENV": "development", "DJANGO_SECRET_KEY": ""}, keys=["SECRET_KEY"]
        )
        assert "error" not in result
        assert result["SECRET_KEY"].startswith("dev-only-")
        assert len(result["SECRET_KEY"]) > 50


class TestProductionGuards:
    def test_debug_cannot_be_enabled(self):
        result = load_settings({"DJANGO_DEBUG": "1"}, keys=["DEBUG"])
        assert "error" in result
        assert "DJANGO_DEBUG" in result["error"]

    def test_allowed_hosts_is_required(self):
        result = load_settings({"DJANGO_ALLOWED_HOSTS": ""}, keys=["ALLOWED_HOSTS"])
        assert "error" in result
        assert "ALLOWED_HOSTS" in result["error"]

    def test_sqlite_is_refused(self):
        """A file database behind multiple workers loses writes and cannot be backed up live."""

        result = load_settings(
            {"DB_ENGINE": "django.db.backends.sqlite3"}, keys=["DATABASES"]
        )
        assert "error" in result
        assert "SQLite" in result["error"]

    def test_redis_is_required(self):
        """Rate limits and lockouts on a per-process cache are not limits at all."""

        result = load_settings({"REDIS_URL": ""}, keys=["CACHES"])
        assert "error" in result
        assert "REDIS_URL" in result["error"]

    def test_an_unknown_environment_name_is_refused(self):
        result = load_settings({"DJANGO_ENV": "staging-ish"}, keys=["ENVIRONMENT"])
        assert "error" in result

    def test_a_malformed_boolean_is_refused(self):
        result = load_settings({"SECURE_SSL_REDIRECT": "maybe"}, keys=["SECURE_SSL_REDIRECT"])
        assert "error" in result


class TestProductionSecurityDefaults:
    """Every one of these was absent from the original settings module."""

    @pytest.fixture(scope="class")
    def production(self):
        return load_settings(
            keys=[
                "DEBUG",
                "SECURE_SSL_REDIRECT",
                "SESSION_COOKIE_SECURE",
                "CSRF_COOKIE_SECURE",
                "SECURE_HSTS_SECONDS",
                "SECURE_HSTS_INCLUDE_SUBDOMAINS",
                "SECURE_HSTS_PRELOAD",
                "SECURE_CONTENT_TYPE_NOSNIFF",
                "X_FRAME_OPTIONS",
                "SECURE_REFERRER_POLICY",
                "SESSION_COOKIE_HTTPONLY",
                "SESSION_COOKIE_SAMESITE",
                "CONTENT_SECURITY_POLICY",
                "ALLOWED_HOSTS",
                "STORAGES",
            ]
        )

    def test_it_imports_cleanly(self, production):
        assert "error" not in production, production.get("error")

    def test_debug_is_off(self, production):
        assert production["DEBUG"] is False

    def test_tls_is_enforced(self, production):
        assert production["SECURE_SSL_REDIRECT"] is True

    def test_cookies_are_secure(self, production):
        assert production["SESSION_COOKIE_SECURE"] is True
        assert production["CSRF_COOKIE_SECURE"] is True
        assert production["SESSION_COOKIE_HTTPONLY"] is True
        assert production["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_hsts_is_a_year_with_subdomains_and_preload(self, production):
        assert production["SECURE_HSTS_SECONDS"] >= 31536000
        assert production["SECURE_HSTS_INCLUDE_SUBDOMAINS"] is True
        assert production["SECURE_HSTS_PRELOAD"] is True

    def test_clickjacking_and_sniffing_are_blocked(self, production):
        assert production["X_FRAME_OPTIONS"] == "DENY"
        assert production["SECURE_CONTENT_TYPE_NOSNIFF"] is True

    def test_the_referrer_policy_does_not_leak_urls(self, production):
        assert production["SECURE_REFERRER_POLICY"] == "same-origin"

    def test_the_csp_forbids_third_party_and_inline_scripts(self, production):
        csp = production["CONTENT_SECURITY_POLICY"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("style-src")[0]
        assert "frame-ancestors 'none'" in csp

    def test_allowed_hosts_has_no_empty_entry(self, production):
        """`"".split(",")` yields `[""]`, which silently poisons the list."""

        assert "" not in production["ALLOWED_HOSTS"]
        assert production["ALLOWED_HOSTS"] == ["tasks.example.com"]

    def test_static_files_use_the_hashed_manifest_backend(self, production):
        """Without it, every asset 404s once DEBUG is off."""

        assert "CompressedManifest" in str(production["STORAGES"])


class TestDevelopmentProfile:
    def test_debug_defaults_on_for_local_work(self):
        result = load_settings(
            {"DJANGO_ENV": "development", "DJANGO_DEBUG": None, "DJANGO_SECRET_KEY": ""},
            keys=["DEBUG", "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE"],
        )
        assert result["DEBUG"] is True
        # ...but TLS enforcement stays off, so local development actually works.
        assert result["SECURE_SSL_REDIRECT"] is False
        assert result["SESSION_COOKIE_SECURE"] is False

    def test_sqlite_is_allowed_locally(self):
        result = load_settings(
            {
                "DJANGO_ENV": "development",
                "DJANGO_SECRET_KEY": "",
                "DB_ENGINE": "django.db.backends.sqlite3",
                "REDIS_URL": "",
            },
            keys=["DATABASES"],
        )
        assert "error" not in result
        assert "sqlite3" in str(result["DATABASES"])
