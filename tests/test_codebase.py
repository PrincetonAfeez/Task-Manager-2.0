"""Whole-codebase invariants.

These catch classes of bug that unit tests only find by accident, and that were each hit
for real during this project's audit.
"""

from __future__ import annotations

import ast
import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {".venv", "venv", "__pycache__", "staticfiles", "Older Scope", ".backup", "node_modules"}


def _project_files(*patterns: str):
    for pattern in patterns:
        for path in PROJECT_ROOT.rglob(pattern):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            yield path


def _python_files():
    return _project_files("*.py")


class TestLoggingSafety:
    """`logging` reserves a set of LogRecord attribute names.

    Passing one in ``extra`` raises ``KeyError`` *inside the log call*. In this codebase
    that once happened at the end of the workspace purge, inside an ``@transaction.atomic``
    block --" so the exception rolled the entire erasure back after it had already run.
    """

    RESERVED = frozenset(
        {
            "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
            "pathname", "process", "processName", "relativeCreated", "stack_info",
            "thread", "threadName", "taskName",
        }
    )

    def test_no_extra_dict_uses_a_reserved_field_name(self):
        collisions = []
        for path in _python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                        for key in keyword.value.keys:
                            if isinstance(key, ast.Constant) and key.value in self.RESERVED:
                                collisions.append(
                                    f"{path.relative_to(PROJECT_ROOT)}:{key.lineno} uses {key.value!r}"
                                )
        assert not collisions, "Reserved LogRecord field in logging extra:\n  " + "\n  ".join(collisions)


class TestEncoding:
    """A UTF-8 BOM at the start of a template renders as a stray character on the page,
    and at the start of a Python file is a syntax error."""

    def test_no_source_file_starts_with_a_byte_order_mark(self):
        offenders = []
        for path in _project_files("*.py", "*.html", "*.css", "*.js", "*.txt", "*.md", "*.yml"):
            with path.open("rb") as handle:
                if handle.read(3) == b"\xef\xbb\xbf":
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert not offenders, "Files with a UTF-8 BOM:\n  " + "\n  ".join(offenders)

    def test_every_python_file_is_valid_utf8(self):
        for path in _python_files():
            path.read_text(encoding="utf-8")

    def test_no_file_contains_double_encoded_text(self):
        """Mojibake: UTF-8 bytes decoded as Latin-1 and re-encoded.

        It renders as garbage on the page and, in a docstring, is invisible to every test
        except this one. It happened here for real when a file was written through a tool
        that assumed the wrong codec.
        """

        # Written as escapes, not literal characters, so this file's own bytes stay
        # pure ASCII. A literal marker would make the scan flag its own source, and would
        # itself be unreadable in an editor.
        markers = [
            "\u00e2\u0080",  # start of a mangled em dash or curly quote
            "\u00c3\u00a9",  # mangled e-acute
            "\u00c3\u00a2",  # mangled a-circumflex
            "\u00c3\u00bc",  # mangled u-umlaut
            "\u00c2\u00a0",  # mangled non-breaking space
        ]
        offenders = []
        for path in _project_files("*.py", "*.html", "*.css", "*.js", "*.md"):
            text = path.read_text(encoding="utf-8")
            for marker in markers:
                if marker in text:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)} contains mojibake")
                    break
        assert not offenders, "Double-encoded text:\n  " + "\n  ".join(offenders)


class TestTenantScoping:
    """Structural guard: no view module may reach an unscoped manager directly.

    Tenant isolation is only reliable while it is *structural*. A single
    ``Task.objects.filter(...)`` in a view --" without the tenant filter --" is exactly the
    bug the original code shipped, and a reviewer will not reliably catch the next one.
    """

    #: Modules whose querysets are scoped by construction or are deliberately global.
    ALLOWED = {
        "api/mixins.py",       # defines the scoping
        "kernel/tenancy.py",   # defines the scoping
        "kernel/purge.py",     # deliberately crosses tenants, guarded by deleted_at
        "kernel/services.py",  # workspace lifecycle, argument-scoped
    }

    UNSCOPED_PATTERNS = (
        "Task.objects.all()",
        "Job.objects.all()",
        "Organization.objects.all()",
    )

    def test_view_modules_do_not_use_unscoped_managers(self):
        offenders = []
        for path in _project_files("views*.py"):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            if relative in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in self.UNSCOPED_PATTERNS:
                if pattern in text:
                    offenders.append(f"{relative} uses {pattern}")
        assert not offenders, "Unscoped manager in a view:\n  " + "\n  ".join(offenders)

    def test_every_organization_scoped_model_indexes_the_tenant_column(self):
        """An unindexed tenant filter is a full scan on every query the product makes."""

        import django

        django.setup()
        from django.apps import apps

        missing = []
        project_apps = {
            "kernel", "tasks", "queues", "schedules", "billing", "integrations",
        }
        for model in apps.get_models():
            if model._meta.app_label not in project_apps:
                continue
            field_names = {field.name for field in model._meta.get_fields()}
            if "organization" not in field_names:
                continue

            field = model._meta.get_field("organization")
            # A ForeignKey is indexed by default unless db_index was explicitly disabled.
            if getattr(field, "db_index", False) or getattr(field, "many_to_one", False):
                continue
            missing.append(f"{model._meta.label}")
        assert not missing, "Tenant column without an index:\n  " + "\n  ".join(missing)


class TestSecretsHygiene:
    def test_no_hardcoded_secret_key_fallback(self):
        """The original settings shipped a usable SECRET_KEY that production would adopt.

        The literal may still appear as the *subject* of a rejection check; what must not
        exist is an assignment that hands it to Django as a real key.
        """

        settings_text = (PROJECT_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
        assignments = re.findall(r"SECRET_KEY\s*=\s*(.+)", settings_text)
        for assignment in assignments:
            assert "django-insecure" not in assignment, f"insecure fallback key: {assignment}"
        # It may only be referenced by the guard that refuses it.
        for line in settings_text.splitlines():
            if "django-insecure" in line:
                assert "startswith" in line or "#" in line, f"unexpected use: {line.strip()}"

    def test_env_example_ships_no_real_values(self):
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for line in example.splitlines():
            if line.startswith("DJANGO_SECRET_KEY") or line.startswith("EMAIL_HOST_PASSWORD"):
                assert line.split("=", 1)[1].strip() == "", f"{line} carries a value"

    def test_gitignore_covers_secrets_and_the_database(self):
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in (".env", "db.sqlite3", "*.pem", "*.key"):
            assert entry in ignored, f"{entry} is not gitignored"

    def test_dockerignore_excludes_the_developer_database(self):
        """It holds real password hashes; baking it into an image ships them."""

        ignored = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
        assert "db.sqlite3" in ignored
        assert ".env" in ignored


class TestMigrations:
    def test_every_project_migration_is_reversible(self):
        """An irreversible migration means a release cannot be rolled back."""

        import django

        django.setup()
        from django.db.migrations.loader import MigrationLoader

        project_apps = {
            "kernel", "tasks", "queues", "schedules", "billing", "integrations",
        }
        irreversible = []
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for (app_label, name), migration in loader.disk_migrations.items():
            if app_label not in project_apps:
                continue
            for operation in migration.operations:
                if not operation.reversible:
                    irreversible.append(f"{app_label}.{name}: {type(operation).__name__}")
        assert not irreversible, "Irreversible migrations:\n  " + "\n  ".join(irreversible)


class TestTemplates:
    def test_every_state_changing_form_carries_a_csrf_token(self):
        offenders = []
        for path in _project_files("*.html"):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            post_forms = lowered.count('method="post"')
            if post_forms and lowered.count("{% csrf_token %}") < post_forms:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert not offenders, "POST form without a CSRF token:\n  " + "\n  ".join(offenders)

    def test_no_template_disables_autoescaping(self):
        """`autoescape off` and `|safe` on user data are how stored XSS ships."""

        offenders = []
        for path in _project_files("*.html"):
            text = path.read_text(encoding="utf-8")
            if "{% autoescape off %}" in text:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert not offenders, "Autoescaping disabled in:\n  " + "\n  ".join(offenders)

    def test_no_template_loads_a_third_party_script(self):
        """A CDN <script> is both a supply-chain dependency and a CSP violation."""

        offenders = []
        for path in _project_files("*.html"):
            text = path.read_text(encoding="utf-8")
            for marker in ("unpkg.com", "cdn.jsdelivr", "cdnjs.cloudflare", "ajax.googleapis"):
                if marker in text:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {marker}")
        assert not offenders, "Third-party script tag:\n  " + "\n  ".join(offenders)


class TestDocumentation:
    def test_the_readme_does_not_advertise_a_shared_demo_password(self):
        """The original README published `demo` / `demo` for a live login."""

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert "password: demo\n" not in readme

    def test_the_runbook_documents_backup_and_rollback(self):
        runbook = (PROJECT_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8").lower()
        for topic in ("backup", "restore", "rollback", "escalation", "rto", "rpo"):
            assert topic in runbook, f"RUNBOOK.md does not cover {topic}"
