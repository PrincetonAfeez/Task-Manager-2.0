"""Durable queue services: enqueue, claim, complete, fail, redrive.

The contract this module implements:

* **Enqueue is idempotent** on ``(organization, source, idempotency_key)`` and happens in
  the caller's transaction, so a job is never visible before the data it describes.
* **Claiming is exclusive.** ``SELECT ... FOR UPDATE SKIP LOCKED`` (Postgres) or a
  compare-and-swap update (SQLite) means two dispatchers cannot hand the same row to two
  workers.
* **Leases expire.** A worker that dies mid-job does not strand it; the reaper returns it.
* **Failure is bounded.** Exponential backoff with jitter, then a dead letter — never an
  infinite retry loop.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Min
from django.utils import timezone

from kernel.models import Organization
from ops.logging import get_logger, get_request_id

from .models import DeadLetter, EffectMarker, Job, JobAttempt, Queue, QueueMetricSnapshot
from .registry import PermanentError, RetryableError

logger = get_logger("taskmanager.queues")

INTERNAL_QUEUE_NAME = "internal"
WEBHOOK_QUEUE_NAME = "webhooks"


@dataclass(frozen=True)
class EnqueueResult:
    job: Job
    created: bool


@dataclass(frozen=True)
class ClaimedJob:
    job: Job
    lease_token: str
    attempt_number: int


def _supports_skip_locked() -> bool:
    from django.db import connection

    return connection.features.has_select_for_update_skip_locked


def get_internal_queue(organization: Organization) -> Queue:
    queue, _ = Queue.objects.get_or_create(
        organization=organization,
        name=INTERNAL_QUEUE_NAME,
        defaults={
            "mode": Queue.Mode.INTERNAL,
            "concurrency_limit": 8,
            "rate_limit_per_minute": 600,
        },
    )
    return queue


def get_webhook_queue(organization: Organization) -> Queue:
    queue, _ = Queue.objects.get_or_create(
        organization=organization,
        name=WEBHOOK_QUEUE_NAME,
        defaults={
            "mode": Queue.Mode.PUSH,
            "concurrency_limit": 4,
            "rate_limit_per_minute": 120,
            "visibility_timeout_seconds": 120,
        },
    )
    return queue


# --------------------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------------------


def enqueue_job(
    *,
    organization: Organization,
    queue: Queue,
    source: str,
    handler_type: str,
    handler: str,
    payload: dict,
    idempotency_key: str,
    payload_version: int = 1,
    available_at=None,
    priority: int = 2,
    max_attempts: int | None = None,
    correlation_id: str = "",
) -> EnqueueResult:
    """Transactional-outbox boundary: insert a durable Job row.

    Runs inside the caller's transaction on purpose. If the caller rolls back, the job
    disappears with it — the job and the state it acts on commit together or not at all.
    """

    if queue.organization_id != organization.pk:
        raise ValueError("Queue belongs to a different organization.")

    defaults = {
        "queue": queue,
        "status": Job.Status.AVAILABLE,
        "handler_type": handler_type,
        "handler": handler,
        "payload": payload,
        "payload_version": payload_version,
        "available_at": available_at or timezone.now(),
        "priority": priority,
        "max_attempts": max_attempts or 5,
        "correlation_id": (correlation_id or get_request_id())[:128],
    }

    try:
        # Nested atomic block: an IntegrityError must not poison the caller's transaction.
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                organization=organization,
                source=source,
                idempotency_key=idempotency_key,
                defaults=defaults,
            )
    except IntegrityError:
        # Lost a race with a concurrent enqueue of the same identity. The other writer's
        # row is the winner; returning it keeps enqueue idempotent under concurrency,
        # which get_or_create alone does not guarantee.
        job = Job.objects.get(organization=organization, source=source, idempotency_key=idempotency_key)
        created = False

    if created:
        logger.info(
            "job enqueued",
            extra={
                "job_id": job.pk,
                "handler": handler,
                "queue": queue.name,
                "organization_id": organization.pk,
            },
        )
    return EnqueueResult(job=job, created=created)


# --------------------------------------------------------------------------------------
# Claim / lease
# --------------------------------------------------------------------------------------


def _fairness_key(organization_id: int) -> str:
    return f"queue:fairness:claims:{organization_id}"


def _record_fairness_claim(organization_id: int) -> None:
    from django.core.cache import cache

    window = getattr(settings, "QUEUE_FAIRNESS_WINDOW_SECONDS", 60)
    key = _fairness_key(organization_id)
    if not cache.add(key, 1, timeout=window):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window)


def _order_queues_fairly(queues: list[Queue]) -> list[Queue]:
    """Prefer workspaces that have received fewer claims in the fairness window.

    Equal counts keep stable ordering by queue id so the sort is deterministic in tests.
    Weight is currently 1 per organization; a future plan-tier weight multiplies the score.
    """

    if not getattr(settings, "QUEUE_FAIRNESS_ENABLED", True) or len(queues) <= 1:
        return queues

    from django.core.cache import cache

    scored = []
    for queue in queues:
        org_id = queue.organization_id
        claims = int(cache.get(_fairness_key(org_id)) or 0)
        # Lower score = more starved = claimed sooner. Weight placeholder is 1.0.
        weight = 1.0
        scored.append((claims / weight, queue.pk, queue))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def claim_jobs(*, queue: Queue | None = None, worker_id: str, limit: int = 10) -> list[ClaimedJob]:
    """Atomically lease up to ``limit`` runnable jobs for ``worker_id``.

    Respects queue pause state and per-queue concurrency. Returns leases, not promises:
    each returned job is already marked ``leased`` with an expiry, so no other worker can
    take it until the lease lapses.

    When claiming across queues, workspaces are ordered by weighted fairness so one busy
    tenant cannot monopolise a dispatcher tick.
    """

    now = timezone.now()
    claimed: list[ClaimedJob] = []
    if queue is not None:
        queues = [queue]
    else:
        queues = _order_queues_fairly(
            list(Queue.objects.filter(paused=False).select_related("organization"))
        )

    fairness = getattr(settings, "QUEUE_FAIRNESS_ENABLED", True) and queue is None
    # Round-robin across workspaces so one busy tenant cannot drain an entire tick.
    # When fairness is off (or a single queue was named), take the usual per-queue budget.
    while len(claimed) < limit:
        progressed = False
        for target in queues:
            if len(claimed) >= limit:
                break
            if target.paused:
                continue
            headroom = target.concurrency_limit - target.in_flight
            if headroom <= 0:
                continue
            take = 1 if fairness else min(headroom, limit - len(claimed))
            batch = _claim_from_queue(target, worker_id=worker_id, limit=take, now=now)
            if not batch:
                continue
            if fairness:
                _record_fairness_claim(target.organization_id)
            claimed.extend(batch)
            progressed = True
            if not fairness and len(claimed) >= limit:
                break
        if not fairness or not progressed:
            break

    return claimed


def _claim_from_queue(queue: Queue, *, worker_id: str, limit: int, now) -> list[ClaimedJob]:
    results: list[ClaimedJob] = []
    lease_seconds = queue.visibility_timeout_seconds or getattr(
        settings, "QUEUE_DEFAULT_LEASE_SECONDS", 300
    )
    skip_locked = _supports_skip_locked()

    for _ in range(limit):
        with transaction.atomic():
            candidates = Job.objects.filter(
                queue=queue,
                # FAILED is included deliberately: it means "awaiting retry at
                # available_at", and excluding it would make retry a no-op.
                status__in=Job.CLAIMABLE_STATUSES,
                available_at__lte=now,
            ).order_by("-priority", "available_at", "created_at")
            if skip_locked:
                candidates = candidates.select_for_update(skip_locked=True)
            job = candidates.first()
            if job is None:
                break

            if job.receives >= queue.max_receives:
                # Redelivered too many times without ever finishing: a poison payload.
                _dead_letter(
                    job,
                    reason=f"Exceeded queue max_receives ({queue.max_receives}) without completing.",
                )
                continue

            lease_token = secrets.token_hex(16)
            updated = Job.objects.filter(pk=job.pk, status=job.status).update(
                status=Job.Status.LEASED,
                locked_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                lease_token=lease_token,
                worker_id=worker_id[:120],
                receives=F("receives") + 1,
                updated_at=now,
            )
            if not updated:
                # Compare-and-swap lost (SQLite path, no SKIP LOCKED). Try the next row.
                continue

            job.refresh_from_db()
            attempt_number = job.attempts_made + 1
            results.append(ClaimedJob(job=job, lease_token=lease_token, attempt_number=attempt_number))

    return results


def start_attempt(claimed: ClaimedJob, *, worker_id: str) -> JobAttempt:
    """Open a JobAttempt row for a leased job and count the execution."""

    job = claimed.job
    attempt, created = JobAttempt.objects.get_or_create(
        job=job,
        attempt_number=claimed.attempt_number,
        defaults={
            "started_at": timezone.now(),
            "worker_id": worker_id[:120],
            "outcome": JobAttempt.Outcome.STARTED,
        },
    )
    if created:
        Job.objects.filter(pk=job.pk).update(
            attempts_made=F("attempts_made") + 1,
            started_at=job.started_at or attempt.started_at,
            updated_at=timezone.now(),
        )
        job.attempts_made += 1
    return attempt


# --------------------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------------------


def complete_job(
    job: Job, *, lease_token: str, result: dict | None = None, attempt: JobAttempt | None = None
) -> bool:
    """Mark a job succeeded. Refuses to act on a lease the caller no longer holds."""

    now = timezone.now()
    with transaction.atomic():
        updated = Job.objects.filter(
            pk=job.pk, lease_token=lease_token, status=Job.Status.LEASED
        ).update(
            status=Job.Status.SUCCEEDED,
            result=result or {},
            finished_at=now,
            lease_expires_at=None,
            lease_token="",
            last_error="",
            next_retry_at=None,
            updated_at=now,
        )
        if not updated:
            # The lease was reaped and the job re-leased elsewhere. Discarding this result
            # is correct: the current lease holder owns the outcome.
            logger.warning("stale completion ignored", extra={"job_id": job.pk})
            return False
        if attempt is not None:
            attempt.finish(outcome=JobAttempt.Outcome.SUCCEEDED, result=result or {})
    job.refresh_from_db()
    return True


def retry_delay_seconds(attempt_number: int, *, base: int | None = None, cap: int | None = None) -> int:
    """Exponential backoff with jitter.

    Jitter is not decoration: without it, every job that failed during an outage retries at
    the same instant and re-creates the outage.
    """

    base = base or getattr(settings, "QUEUE_RETRY_BASE_SECONDS", 10)
    cap = cap or getattr(settings, "QUEUE_RETRY_MAX_SECONDS", 3600)
    ratio = getattr(settings, "QUEUE_RETRY_JITTER_RATIO", 0.2)
    raw = min(cap, base * (2 ** max(0, attempt_number - 1)))
    jitter = raw * ratio
    return max(1, int(raw + random.uniform(-jitter, jitter)))


def fail_job(
    job: Job,
    *,
    lease_token: str,
    error: str,
    attempt: JobAttempt | None = None,
    permanent: bool = False,
    retry_after_seconds: int | None = None,
) -> bool:
    """Record a failure and either schedule a retry or dead-letter the job."""

    now = timezone.now()
    with transaction.atomic():
        fresh = Job.objects.filter(pk=job.pk, lease_token=lease_token, status=Job.Status.LEASED).first()
        if fresh is None:
            logger.warning("stale failure ignored", extra={"job_id": job.pk})
            return False

        truncated = (error or "")[:8000]
        exhausted = permanent or fresh.attempts_made >= fresh.max_attempts

        if attempt is not None:
            attempt.finish(outcome=JobAttempt.Outcome.FAILED, error=truncated)

        if exhausted:
            Job.objects.filter(pk=fresh.pk).update(
                status=Job.Status.DEAD,
                last_error=truncated,
                finished_at=now,
                lease_token="",
                lease_expires_at=None,
                next_retry_at=None,
                updated_at=now,
            )
            fresh.refresh_from_db()
            _dead_letter(
                fresh,
                reason=("Permanent failure: " if permanent else "Attempts exhausted: ") + truncated,
                already_dead=True,
            )
            return True

        delay = (
            retry_after_seconds
            if retry_after_seconds is not None
            else retry_delay_seconds(fresh.attempts_made)
        )
        next_at = now + timedelta(seconds=delay)
        Job.objects.filter(pk=fresh.pk).update(
            status=Job.Status.FAILED,
            last_error=truncated,
            next_retry_at=next_at,
            available_at=next_at,
            lease_token="",
            lease_expires_at=None,
            locked_at=None,
            updated_at=now,
        )
        logger.info(
            "job retry scheduled",
            extra={"job_id": fresh.pk, "delay_seconds": delay, "attempt": fresh.attempts_made},
        )
    return True


def _dead_letter(job: Job, *, reason: str, already_dead: bool = False) -> DeadLetter:
    now = timezone.now()
    if not already_dead:
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.DEAD,
            finished_at=now,
            lease_token="",
            lease_expires_at=None,
            next_retry_at=None,
            last_error=reason[:8000],
            updated_at=now,
        )
    dead_letter, _ = DeadLetter.objects.get_or_create(
        job=job,
        defaults={"organization_id": job.organization_id, "reason": reason[:8000]},
    )
    logger.error("job dead-lettered", extra={"job_id": job.pk, "handler": job.handler})
    return dead_letter


def redrive(dead_letter: DeadLetter, *, actor=None, reset_attempts: bool = True) -> Job:
    """Return a dead-lettered job to its queue.

    Operators need this after fixing the underlying cause; without it a dead letter is just
    a tombstone and the work is lost.
    """

    now = timezone.now()
    job = dead_letter.job
    with transaction.atomic():
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.AVAILABLE,
            available_at=now,
            next_retry_at=None,
            lease_token="",
            lease_expires_at=None,
            locked_at=None,
            finished_at=None,
            last_error="",
            receives=0 if reset_attempts else F("receives"),
            attempts_made=0 if reset_attempts else F("attempts_made"),
            updated_at=now,
        )
        DeadLetter.objects.filter(pk=dead_letter.pk).update(
            redriven_at=now,
            redriven_by=actor if getattr(actor, "pk", None) else None,
        )
    job.refresh_from_db()
    logger.info("dead letter redriven", extra={"job_id": job.pk})
    return job


def cancel_job(job: Job, *, reason: str = "") -> bool:
    """Cancel a job that has not started. Leased jobs are left to finish or expire."""

    now = timezone.now()
    with transaction.atomic():
        updated = Job.objects.filter(pk=job.pk, status__in=Job.CLAIMABLE_STATUSES).update(
            status=Job.Status.CANCELED,
            finished_at=now,
            last_error=reason[:8000],
            next_retry_at=None,
            updated_at=now,
        )
    return bool(updated)


# --------------------------------------------------------------------------------------
# Reaper
# --------------------------------------------------------------------------------------


def reap_expired_leases(*, now=None, limit: int = 200) -> dict:
    """Return jobs whose worker vanished to the runnable pool.

    Without this, one OOM-killed worker silently strands every job it held.
    """

    now = now or timezone.now()
    reclaimed = 0
    dead = 0

    stalled = list(Job.objects.stalled(now=now).select_related("queue")[:limit])
    for job in stalled:
        with transaction.atomic():
            fresh = Job.objects.filter(pk=job.pk, status=Job.Status.LEASED).first()
            if fresh is None or (fresh.lease_expires_at and fresh.lease_expires_at > now):
                continue

            open_attempt = fresh.attempts.filter(outcome=JobAttempt.Outcome.STARTED).first()
            if open_attempt is not None:
                open_attempt.finish(
                    outcome=JobAttempt.Outcome.TIMED_OUT,
                    error="Lease expired before the worker reported an outcome.",
                )

            if fresh.attempts_made >= fresh.max_attempts or fresh.receives >= fresh.queue.max_receives:
                Job.objects.filter(pk=fresh.pk).update(
                    status=Job.Status.DEAD,
                    finished_at=now,
                    lease_token="",
                    lease_expires_at=None,
                    last_error="Lease expired and no attempts remain.",
                    updated_at=now,
                )
                fresh.refresh_from_db()
                _dead_letter(fresh, reason="Lease expired and no attempts remain.", already_dead=True)
                dead += 1
                continue

            delay = retry_delay_seconds(max(1, fresh.attempts_made))
            Job.objects.filter(pk=fresh.pk).update(
                status=Job.Status.AVAILABLE,
                available_at=now + timedelta(seconds=delay),
                lease_token="",
                lease_expires_at=None,
                locked_at=None,
                last_error="Lease expired; job returned to the queue.",
                updated_at=now,
            )
            reclaimed += 1

    if reclaimed or dead:
        logger.warning("reaper reclaimed leases", extra={"reclaimed": reclaimed, "dead_lettered": dead})
    return {"reclaimed": reclaimed, "dead_lettered": dead, "scanned": len(stalled)}


# --------------------------------------------------------------------------------------
# Effect idempotency
# --------------------------------------------------------------------------------------


def claim_effect(*, organization: Organization, effect_type: str, natural_key: str, metadata=None) -> bool:
    """Claim the right to perform a side effect exactly once.

    Returns ``True`` for the first caller and ``False`` for every redelivery. Must be called
    inside the same transaction as the effect itself, otherwise a crash between the marker
    and the effect loses the effect permanently.
    """

    try:
        with transaction.atomic():
            EffectMarker.objects.create(
                organization=organization,
                effect_type=effect_type,
                natural_key=natural_key[:180],
                metadata=metadata or {},
            )
        return True
    except IntegrityError:
        return False


# --------------------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------------------


def snapshot_queue_metrics(*, organization: Organization | None = None) -> list[QueueMetricSnapshot]:
    """Record a depth/latency sample per queue and warn when a queue is backing up."""

    now = timezone.now()
    queues = Queue.objects.all()
    if organization is not None:
        queues = queues.filter(organization=organization)

    threshold = getattr(settings, "QUEUE_BACKLOG_ALERT_THRESHOLD", 500)
    age_threshold = getattr(settings, "QUEUE_OLDEST_JOB_ALERT_SECONDS", 900)
    snapshots = []

    for queue in queues.select_related("organization"):
        waiting = queue.jobs.filter(status__in=Job.WAITING_STATUSES)
        oldest = waiting.aggregate(oldest=Min("available_at"))["oldest"]
        age = int((now - oldest).total_seconds()) if oldest and oldest < now else 0
        snapshot = QueueMetricSnapshot.objects.create(
            organization=queue.organization,
            queue=queue,
            depth=waiting.count(),
            in_flight=queue.jobs.filter(status=Job.Status.LEASED).count(),
            dead_letters=DeadLetter.objects.filter(job__queue=queue, redriven_at__isnull=True).count(),
            oldest_available_age_seconds=age,
        )
        snapshots.append(snapshot)

        if snapshot.depth >= threshold or snapshot.oldest_available_age_seconds >= age_threshold:
            logger.error(
                "queue backlog alert",
                extra={
                    "queue": queue.name,
                    "organization_id": queue.organization_id,
                    "depth": snapshot.depth,
                    "oldest_age_seconds": snapshot.oldest_available_age_seconds,
                },
            )

    return snapshots


__all__ = [
    "ClaimedJob",
    "EnqueueResult",
    "PermanentError",
    "RetryableError",
    "cancel_job",
    "claim_effect",
    "claim_jobs",
    "complete_job",
    "enqueue_job",
    "fail_job",
    "get_internal_queue",
    "get_webhook_queue",
    "reap_expired_leases",
    "redrive",
    "retry_delay_seconds",
    "snapshot_queue_metrics",
    "start_attempt",
]
