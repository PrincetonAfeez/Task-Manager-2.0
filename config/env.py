"""Environment parsing helpers.

Settings must never silently fall back to an insecure value in production. Every helper
here takes an explicit default and every *security-relevant* caller in ``config.settings``
passes a default that is safe when the environment is empty.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f", ""}


def load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a ``.env`` file without overwriting real env vars.

    Deliberately dependency-free and forgiving: a malformed line is skipped rather than
    crashing startup, because a broken ``.env`` should not be able to take down a container
    that already has its configuration injected by the platform.
    """

    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if required:
            raise ImproperlyConfigured(f"Required environment variable {name} is not set.")
        return default if default is not None else ""
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ImproperlyConfigured(f"Environment variable {name}={raw!r} is not a valid boolean.")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"Environment variable {name}={raw!r} is not an integer.") from exc


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Split a comma-separated env var, dropping empty entries.

    ``"".split(",")`` yields ``[""]``, which silently poisons ALLOWED_HOSTS and
    CSRF_TRUSTED_ORIGINS. This helper never returns empty strings.
    """

    raw = os.environ.get(name)
    if raw is None:
        return list(default or [])
    items = [item.strip() for item in raw.split(",")]
    return [item for item in items if item]
