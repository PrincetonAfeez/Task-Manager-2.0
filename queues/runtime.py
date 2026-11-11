"""Worker runtime: turn leased Job rows into executed effects.

This is the piece the scaffold was missing entirely — the ledger existed, nothing ran it.
The loop is deliberately simple and crash-safe:

    claim -> start attempt -> run handler -> complete | fail -> repeat

Every step is durable. A process killed at any point leaves the job leased, and the reaper
returns it to the queue once the lease expires.
"""

from __future__ import annotations

import os
import socket
import time
import traceback
from dataclasses import dataclass

from django.conf import settings
from django.db import close_old_connections, connection

from ops.logging import get_logger, reset_request_id, set_request_id

from . import handlers  # noqa: F401 - importing registers the built-in handlers
from .models import Job, Queue
from .registry import PermanentError, RetryableError, UnknownHandlerError, get_handler
from .services import ClaimedJob, claim_jobs, complete_job, fail_job, start_attempt

logger = get_logger("taskmanager.queues.runtime")


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class ExecutionContext:
    """What a handler is allowed to know about its own execution."""

    job: Job
    attempt_number: int
    worker_id: str
    organization: object

    @property
    def is_retry(self) -> bool:
        return self.attempt_number > 1


def execute_job(claimed: ClaimedJob, *, worker_id: str | None = None) -> dict:
    """Run one leased job to a terminal or retryable outcome.

    Never raises for handler failures — a worker that crashes on a bad payload takes down
    every other job it was holding. Failures are recorded, not propagated.
    """

    worker_id = worker_id or worker_identity()
    job = claimed.job
    # Inherit the correlation id of the request that enqueued the job so the whole causal
    # chain shares one id in the logs.
    token = set_request_id(job.correlation_id or f"job-{job.pk}")

    attempt = start_attempt(claimed, worker_id=worker_id)
    context = ExecutionContext(
        job=job,
        attempt_number=claimed.attempt_number,
        worker_id=worker_id,
        organization=job.organization,
    )

    try:
        spec = get_handler(job.handler)
    except UnknownHandlerError as exc:
        # A handler that no longer exists will never succeed. Park it immediately so the
        # dead-letter queue shows the deploy drift instead of burning five retries.
        fail_job(job, lease_token=claimed.lease_token, error=str(exc), attempt=attempt, permanent=True)
        reset_request_id(token)
        return {"job_id": job.pk, "outcome": "dead", "reason": "unknown_handler"}

    started = time.perf_counter()
    try:
        result = spec.func(job, context) or {}
        if not isinstance(result, dict):
            result = {"value": str(result)}
        complete_job(job, lease_token=claimed.lease_token, result=result, attempt=attempt)
        logger.info(
            "job succeeded",
            extra={
                "job_id": job.pk,
                "handler": job.handler,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        outcome = {"job_id": job.pk, "outcome": "succeeded", "result": result}
    except PermanentError as exc:
        fail_job(
            job,
            lease_token=claimed.lease_token,
            error=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            permanent=True,
        )
        logger.warning("job failed permanently", extra={"job_id": job.pk, "handler": job.handler})
        outcome = {"job_id": job.pk, "outcome": "dead", "reason": str(exc)}
    except RetryableError as exc:
        fail_job(
            job,
            lease_token=claimed.lease_token,
            error=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            retry_after_seconds=exc.retry_after_seconds,
        )
        outcome = {"job_id": job.pk, "outcome": "retry", "reason": str(exc)}
    except Exception as exc:
        # Unclassified failures are treated as retryable: transient causes (network, lock
        # contention, restarts) are far more common than genuinely impossible work, and the
        # attempt budget bounds the cost of being wrong.
        detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        fail_job(job, lease_token=claimed.lease_token, error=detail, attempt=attempt)
        logger.exception("job raised", extra={"job_id": job.pk, "handler": job.handler})
        outcome = {"job_id": job.pk, "outcome": "retry", "reason": str(exc)}
    finally:
        reset_request_id(token)

    return outcome


def run_once(*, queue: Queue | None = None, limit: int | None = None, worker_id: str | None = None) -> dict:
    """Claim and execute a batch. The unit the Celery task and the CLI both call."""

    worker_id = worker_id or worker_identity()
    limit = limit or getattr(settings, "QUEUE_DISPATCH_BATCH_SIZE", 100)
    claimed = claim_jobs(queue=queue, worker_id=worker_id, limit=limit)
    results = [execute_job(job, worker_id=worker_id) for job in claimed]
    return {
        "claimed": len(claimed),
        "succeeded": sum(1 for r in results if r["outcome"] == "succeeded"),
        "retried": sum(1 for r in results if r["outcome"] == "retry"),
        "dead": sum(1 for r in results if r["outcome"] == "dead"),
    }


def run_forever(
    *,
    queue: Queue | None = None,
    poll_interval: float = 1.0,
    batch_size: int | None = None,
    max_iterations: int | None = None,
    stop_event=None,
) -> dict:
    """Poll loop for the standalone worker command.

    Sleeps only when a poll came back empty, so a busy queue drains at full speed while an
    idle one costs one query per interval.
    """

    worker_id = worker_identity()
    totals = {"claimed": 0, "succeeded": 0, "retried": 0, "dead": 0, "iterations": 0}
    logger.info("worker started", extra={"worker_id": worker_id})

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if max_iterations is not None and totals["iterations"] >= max_iterations:
            break

        # Long-lived workers outlive database connections; recycle dead ones each tick.
        # Never inside a transaction: Django treats a connection that is mid-transaction
        # as obsolete and closes it, discarding the caller's work. Found by running the
        # suite on PostgreSQL; SQLite's in-memory test database ignores close().
        if not connection.in_atomic_block:
            close_old_connections()
        batch = run_once(queue=queue, limit=batch_size, worker_id=worker_id)
        totals["iterations"] += 1
        for key in ("claimed", "succeeded", "retried", "dead"):
            totals[key] += batch[key]

        if batch["claimed"] == 0:
            if max_iterations is not None:
                # Bounded runs (tests, one-shot drains) must not sleep out the clock.
                continue
            time.sleep(poll_interval)

    logger.info("worker stopped", extra={"worker_id": worker_id, **totals})
    return totals
