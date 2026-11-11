"""Handler registry for the durable queue.

A job row names its handler as a string. That string is resolved here rather than by
importing an arbitrary dotted path from the database — a job payload must never be able to
choose which code runs, or the queue becomes a remote code execution primitive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ops.logging import get_logger

logger = get_logger("taskmanager.queues.registry")


class HandlerError(Exception):
    """Base for handler failures."""


class RetryableError(HandlerError):
    """The effect did not happen but may succeed later (timeout, 503, lock contention)."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentError(HandlerError):
    """The effect can never succeed with this payload (bad request, 404, malformed data).

    Retrying is pure waste and delays the dead-letter signal, so these skip the backoff
    ladder and park the job immediately.
    """


class UnknownHandlerError(PermanentError):
    """The job names a handler that no longer exists in this deployment."""


@dataclass(frozen=True)
class HandlerSpec:
    name: str
    func: Callable
    max_attempts: int = 5
    description: str = ""


_REGISTRY: dict[str, HandlerSpec] = {}


def register(name: str, *, max_attempts: int = 5, description: str = ""):
    """Decorator registering a job handler under ``name``.

    Handlers receive ``(job, context)`` and return a JSON-serialisable dict. Raising
    :class:`RetryableError` schedules a retry; :class:`PermanentError` dead-letters at once;
    any other exception is treated as retryable, because an unclassified failure is more
    often transient than not.
    """

    def decorator(func: Callable) -> Callable:
        if name in _REGISTRY and _REGISTRY[name].func is not func:
            raise RuntimeError(f"Handler {name!r} is already registered.")
        _REGISTRY[name] = HandlerSpec(
            name=name, func=func, max_attempts=max_attempts, description=description or (func.__doc__ or "")
        )
        return func

    return decorator


def get_handler(name: str) -> HandlerSpec:
    spec = _REGISTRY.get(name)
    if spec is None:
        raise UnknownHandlerError(f"No handler registered for {name!r}.")
    return spec


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def registered_handlers() -> dict[str, HandlerSpec]:
    return dict(_REGISTRY)


def clear_registry() -> None:  # pragma: no cover - test helper
    _REGISTRY.clear()
