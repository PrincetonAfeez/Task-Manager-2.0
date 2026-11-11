"""Structured logging primitives.

Every log line carries the request correlation id so a customer-reported failure can be
traced from the web tier through the durable queue and into the worker that ran the job.
"""

from __future__ import annotations

import contextvars
import json
import logging
import traceback
from datetime import UTC, datetime

# Set by ops.middleware.RequestIDMiddleware for web requests and by
# queues.runtime.execute_job for worker runs, so a job inherits the id of the request
# that enqueued it.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "request_id",
    }
)


def get_request_id() -> str:
    return request_id_var.get()


def set_request_id(value: str) -> contextvars.Token:
    return request_id_var.set(value or "-")


def reset_request_id(token: contextvars.Token) -> None:
    try:
        request_id_var.reset(token)
    except ValueError:  # pragma: no cover - token from a different context
        request_id_var.set("-")


class RequestIDFilter(logging.Filter):
    """Attach the ambient request id to every record.

    Implemented as a filter rather than a formatter so the value is available to *any*
    handler, including ones a deployment adds on top of this configuration.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line — the format log aggregators can actually query."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload.setdefault(key, _coerce(value))

        return json.dumps(payload, default=str, ensure_ascii=False)


def _coerce(value):
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced application logger.

    Using a single ``taskmanager.*`` tree keeps the logging config small and lets an
    operator raise verbosity for the product without drowning in Django internals.
    """

    return logging.getLogger(name if name.startswith("taskmanager") else f"taskmanager.{name}")


audit_logger = logging.getLogger("taskmanager.audit")
