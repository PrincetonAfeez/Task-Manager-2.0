"""Durable queue semantics: idempotency, leasing, retry, dead-letter, redrive."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from queues.models import DeadLetter, EffectMarker, Job, JobAttempt, Queue
from queues.registry import PermanentError, RetryableError, UnknownHandlerError, get_handler, register
from queues.runtime import execute_job, run_forever, run_once
from queues.services import (
    cancel_job,
    claim_effect,
    claim_jobs,
    complete_job,
    enqueue_job,
    fail_job,
    get_internal_queue,
    reap_expired_leases,
    redrive,
    retry_delay_seconds,
    snapshot_queue_metrics,
    start_attempt,
)

pytestmark = pytest.mark.django_db


def _enqueue(organization, queue, *, key="k1", handler="internal:noop", payload=None, **kwargs):
    return enqueue_job(
        organization=organization,
        queue=queue,
        source="test",
        handler_type=Job.HandlerType.INTERNAL,
        handler=handler,
        payload=payload or {},
        idempotency_key=key,
        **kwargs,
    )


class TestIdempotency:
    def test_same_key_returns_the_same_job(self, organization, queue):
        first = _enqueue(organization, queue)
        second = _enqueue(organization, queue)
        assert first.created is True
        assert second.created is False
        assert first.job.pk == second.job.pk
        assert Job.objects.count() == 1

    def test_different_keys_create_separate_jobs(self, organization, queue):
        _enqueue(organization, queue, key="a")
        _enqueue(organization, queue, key="b")
        assert Job.objects.count() == 2

    def test_same_key_in_another_tenant_is_a_separate_job(self, organization, other_organization, queue):
        other_queue = get_internal_queue(other_organization)
        _enqueue(organization, queue, key="shared")
        _enqueue(other_organization, other_queue, key="shared")
        assert Job.objects.count() == 2

    def test_enqueue_rejects_a_queue_from_another_tenant(self, organization, other_organization):
        foreign = get_internal_queue(other_organization)
        with pytest.raises(ValueError):
            _enqueue(organization, foreign)

    def test_reminder_key_is_stable_across_calls(self, task, organization):
        """Regression: the original key embedded now() and changed on every call."""

        from tasks.services import schedule_reminder

        task.due_date = timezone.localdate() - timedelta(days=5)  # past due
        task.save(update_fields=["due_date"])

        first = schedule_reminder(task)
        second = schedule_reminder(task)
        assert first is not None
        assert Job.objects.filter(handler="internal:send_reminder").count() == 1
        assert second.pk == first.pk


class TestClaiming:
    def test_claim_leases_a_job(self, organization, queue):
        job = _enqueue(organization, queue).job
        claimed = claim_jobs(queue=queue, worker_id="w1", limit=5)
        assert len(claimed) == 1
        job.refresh_from_db()
        assert job.status == Job.Status.LEASED
        assert job.lease_expires_at is not None
        assert job.worker_id == "w1"
        assert job.receives == 1

    def test_a_leased_job_is_not_claimed_twice(self, organization, queue):
        _enqueue(organization, queue)
        assert len(claim_jobs(queue=queue, worker_id="w1", limit=5)) == 1
        assert claim_jobs(queue=queue, worker_id="w2", limit=5) == []

    def test_future_jobs_are_not_claimed(self, organization, queue):
        _enqueue(organization, queue, available_at=timezone.now() + timedelta(hours=1))
        assert claim_jobs(queue=queue, worker_id="w1") == []

    def test_paused_queue_yields_nothing(self, organization, queue):
        _enqueue(organization, queue)
        Queue.objects.filter(pk=queue.pk).update(paused=True)
        queue.refresh_from_db()
        assert claim_jobs(queue=queue, worker_id="w1") == []

    def test_concurrency_limit_is_respected(self, organization, queue):
        Queue.objects.filter(pk=queue.pk).update(concurrency_limit=2)
        queue.refresh_from_db()
        for index in range(5):
            _enqueue(organization, queue, key=f"k{index}")
        assert len(claim_jobs(queue=queue, worker_id="w1", limit=10)) == 2

    def test_higher_priority_is_claimed_first(self, organization, queue):
        _enqueue(organization, queue, key="low", priority=1)
        high = _enqueue(organization, queue, key="high", priority=9).job
        claimed = claim_jobs(queue=queue, worker_id="w1", limit=1)
        assert claimed[0].job.pk == high.pk

    def test_excess_receives_dead_letters_the_job(self, organization, queue):
        job = _enqueue(organization, queue).job
        Job.objects.filter(pk=job.pk).update(receives=queue.max_receives)
        assert claim_jobs(queue=queue, worker_id="w1") == []
        job.refresh_from_db()
        assert job.status == Job.Status.DEAD
        assert DeadLetter.objects.filter(job=job).exists()


class TestCompletion:
    def test_complete_marks_succeeded(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        attempt = start_attempt(claimed, worker_id="w1")
        assert complete_job(claimed.job, lease_token=claimed.lease_token, result={"ok": 1}, attempt=attempt)

        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.SUCCEEDED
        assert claimed.job.result == {"ok": 1}
        assert claimed.job.finished_at is not None
        attempt.refresh_from_db()
        assert attempt.outcome == JobAttempt.Outcome.SUCCEEDED
        assert attempt.duration_ms is not None

    def test_a_stale_lease_cannot_complete_a_job(self, organization, queue):
        """The reaper may have re-leased the job; the old holder must not win."""

        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        assert complete_job(claimed.job, lease_token="wrong-token") is False
        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.LEASED

    def test_start_attempt_is_idempotent(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        first = start_attempt(claimed, worker_id="w1")
        second = start_attempt(claimed, worker_id="w1")
        assert first.pk == second.pk
        assert JobAttempt.objects.filter(job=claimed.job).count() == 1


class TestFailureAndRetry:
    def test_failure_schedules_a_retry(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        attempt = start_attempt(claimed, worker_id="w1")
        fail_job(claimed.job, lease_token=claimed.lease_token, error="boom", attempt=attempt)

        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.FAILED
        assert claimed.job.next_retry_at > timezone.now()
        assert claimed.job.available_at == claimed.job.next_retry_at
        assert "boom" in claimed.job.last_error

    def test_permanent_failure_dead_letters_immediately(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        attempt = start_attempt(claimed, worker_id="w1")
        fail_job(
            claimed.job, lease_token=claimed.lease_token, error="bad payload", attempt=attempt, permanent=True
        )

        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.DEAD
        assert claimed.job.attempts_made == 1  # no retries burned
        assert DeadLetter.objects.filter(job=claimed.job).exists()

    def test_exhausted_attempts_dead_letter(self, organization, queue):
        job = _enqueue(organization, queue, max_attempts=2).job
        for _ in range(2):
            claimed = claim_jobs(queue=queue, worker_id="w1")[0]
            attempt = start_attempt(claimed, worker_id="w1")
            fail_job(claimed.job, lease_token=claimed.lease_token, error="nope", attempt=attempt)
            Job.objects.filter(pk=job.pk).update(available_at=timezone.now())

        job.refresh_from_db()
        assert job.status == Job.Status.DEAD
        assert DeadLetter.objects.filter(job=job).exists()

    def test_backoff_grows_and_is_capped(self):
        with patch("queues.services.random.uniform", return_value=0):
            assert retry_delay_seconds(1, base=10, cap=3600) == 10
            assert retry_delay_seconds(2, base=10, cap=3600) == 20
            assert retry_delay_seconds(3, base=10, cap=3600) == 40
            assert retry_delay_seconds(20, base=10, cap=3600) == 3600

    def test_backoff_is_jittered(self):
        """Without jitter every job that failed during an outage retries at the same instant."""

        delays = {retry_delay_seconds(5, base=10, cap=3600) for _ in range(40)}
        assert len(delays) > 1

    def test_retry_after_is_honoured(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        start_attempt(claimed, worker_id="w1")
        fail_job(
            claimed.job, lease_token=claimed.lease_token, error="429", retry_after_seconds=1800
        )
        claimed.job.refresh_from_db()
        delta = (claimed.job.next_retry_at - timezone.now()).total_seconds()
        assert 1700 < delta < 1900


class TestReaper:
    def test_expired_lease_returns_the_job(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="dead-worker")[0]
        start_attempt(claimed, worker_id="dead-worker")
        Job.objects.filter(pk=claimed.job.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=5)
        )

        result = reap_expired_leases()
        assert result["reclaimed"] == 1
        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.AVAILABLE
        assert claimed.job.lease_token == ""

    def test_reaper_closes_the_open_attempt(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        attempt = start_attempt(claimed, worker_id="w1")
        Job.objects.filter(pk=claimed.job.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=5)
        )
        reap_expired_leases()
        attempt.refresh_from_db()
        assert attempt.outcome == JobAttempt.Outcome.TIMED_OUT

    def test_reaper_dead_letters_when_no_attempts_remain(self, organization, queue):
        job = _enqueue(organization, queue, max_attempts=1).job
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        start_attempt(claimed, worker_id="w1")
        Job.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=5))

        result = reap_expired_leases()
        assert result["dead_lettered"] == 1
        job.refresh_from_db()
        assert job.status == Job.Status.DEAD

    def test_a_live_lease_is_untouched(self, organization, queue):
        _enqueue(organization, queue)
        claim_jobs(queue=queue, worker_id="w1")
        assert reap_expired_leases()["reclaimed"] == 0


class TestRedriveAndCancel:
    def test_redrive_returns_a_dead_letter_to_the_queue(self, organization, queue, user):
        job = _enqueue(organization, queue).job
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        start_attempt(claimed, worker_id="w1")
        fail_job(claimed.job, lease_token=claimed.lease_token, error="x", permanent=True)

        dead_letter = DeadLetter.objects.get(job=job)
        restored = redrive(dead_letter, actor=user)
        assert restored.status == Job.Status.AVAILABLE
        assert restored.attempts_made == 0
        dead_letter.refresh_from_db()
        assert dead_letter.redriven_at is not None
        assert dead_letter.redriven_by == user

    def test_cancel_stops_a_pending_job(self, organization, queue):
        job = _enqueue(organization, queue).job
        assert cancel_job(job, reason="not needed")
        job.refresh_from_db()
        assert job.status == Job.Status.CANCELED

    def test_cancel_will_not_touch_a_leased_job(self, organization, queue):
        _enqueue(organization, queue)
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        assert cancel_job(claimed.job) is False


class TestEffectIdempotency:
    def test_first_claim_wins_and_the_second_is_refused(self, organization):
        assert claim_effect(organization=organization, effect_type="email", natural_key="k") is True
        assert claim_effect(organization=organization, effect_type="email", natural_key="k") is False
        assert EffectMarker.objects.count() == 1

    def test_markers_are_per_tenant(self, organization, other_organization):
        assert claim_effect(organization=organization, effect_type="email", natural_key="k")
        assert claim_effect(organization=other_organization, effect_type="email", natural_key="k")
        assert EffectMarker.objects.count() == 2

    def test_different_effect_types_do_not_collide(self, organization):
        assert claim_effect(organization=organization, effect_type="email", natural_key="k")
        assert claim_effect(organization=organization, effect_type="sms", natural_key="k")


class TestRegistry:
    def test_unknown_handler_raises(self):
        with pytest.raises(UnknownHandlerError):
            get_handler("internal:does_not_exist")

    def test_builtin_handlers_are_registered(self):
        for name in (
            "internal:send_reminder",
            "internal:send_notification",
            "internal:send_email",
            "internal:materialize_recurrence",
            "http_delivery",
        ):
            assert get_handler(name).func is not None

    def test_duplicate_registration_is_rejected(self):
        @register("test:unique-name-xyz")
        def handler(job, context):
            return {}

        with pytest.raises(RuntimeError):

            @register("test:unique-name-xyz")
            def other(job, context):
                return {}


class TestRuntime:
    def test_execute_runs_the_handler_and_completes(self, organization, queue, user):
        _enqueue(
            organization,
            queue,
            handler="internal:send_notification",
            payload={"user_id": user.pk, "title": "Hi", "natural_key": "n1"},
        )
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        result = execute_job(claimed, worker_id="w1")
        assert result["outcome"] == "succeeded"
        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.SUCCEEDED

    def test_unknown_handler_dead_letters_without_burning_retries(self, organization, queue):
        _enqueue(organization, queue, handler="internal:vanished")
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        result = execute_job(claimed, worker_id="w1")
        assert result["outcome"] == "dead"
        assert result["reason"] == "unknown_handler"

    def test_a_raising_handler_does_not_crash_the_worker(self, organization, queue):
        @register("test:explodes")
        def explode(job, context):
            raise ZeroDivisionError("boom")

        _enqueue(organization, queue, handler="test:explodes")
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        result = execute_job(claimed, worker_id="w1")
        assert result["outcome"] == "retry"
        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.FAILED

    def test_permanent_error_is_not_retried(self, organization, queue):
        @register("test:permanent")
        def permanent(job, context):
            raise PermanentError("malformed")

        _enqueue(organization, queue, handler="test:permanent")
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        assert execute_job(claimed, worker_id="w1")["outcome"] == "dead"

    def test_retryable_error_schedules_a_retry(self, organization, queue):
        @register("test:retryable")
        def retryable(job, context):
            raise RetryableError("timeout", retry_after_seconds=60)

        _enqueue(organization, queue, handler="test:retryable")
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        assert execute_job(claimed, worker_id="w1")["outcome"] == "retry"

    def test_run_once_reports_a_batch(self, organization, queue, user):
        for index in range(3):
            _enqueue(
                organization,
                queue,
                key=f"batch-{index}",
                handler="internal:send_notification",
                payload={"user_id": user.pk, "title": f"n{index}", "natural_key": f"nk{index}"},
            )
        summary = run_once(queue=queue)
        assert summary["claimed"] == 3
        assert summary["succeeded"] == 3

    def test_run_forever_stops_at_max_iterations(self, organization, queue):
        summary = run_forever(queue=queue, max_iterations=3, poll_interval=0)
        assert summary["iterations"] == 3

    def test_correlation_id_flows_from_enqueue_to_execution(self, organization, queue, user):
        result = _enqueue(
            organization,
            queue,
            handler="internal:send_notification",
            payload={"user_id": user.pk, "title": "Hi", "natural_key": "corr"},
            correlation_id="req-abc-123",
        )
        assert result.job.correlation_id == "req-abc-123"


class TestMetrics:
    def test_snapshot_records_depth(self, organization, queue):
        for index in range(3):
            _enqueue(organization, queue, key=f"m{index}")
        snapshots = snapshot_queue_metrics(organization=organization)
        internal = next(s for s in snapshots if s.queue_id == queue.pk)
        assert internal.depth == 3
        assert internal.in_flight == 0

    def test_snapshot_counts_open_dead_letters(self, organization, queue):
        job = _enqueue(organization, queue).job
        claimed = claim_jobs(queue=queue, worker_id="w1")[0]
        start_attempt(claimed, worker_id="w1")
        fail_job(claimed.job, lease_token=claimed.lease_token, error="x", permanent=True)

        snapshots = snapshot_queue_metrics(organization=organization)
        internal = next(s for s in snapshots if s.queue_id == queue.pk)
        assert internal.dead_letters == 1


class TestQueueModel:
    def test_depth_counts_waiting_states_only(self, organization, queue):
        _enqueue(organization, queue, key="a")
        _enqueue(organization, queue, key="b")
        claim_jobs(queue=queue, worker_id="w1", limit=1)
        queue.refresh_from_db()
        assert queue.depth == 1
        assert queue.in_flight == 1

    def test_job_rejects_a_queue_from_another_tenant(self, organization, other_organization):
        from django.core.exceptions import ValidationError

        job = Job(
            organization=organization,
            queue=get_internal_queue(other_organization),
            source="s",
            handler_type=Job.HandlerType.INTERNAL,
            handler="h",
            idempotency_key="k",
            available_at=timezone.now(),
        )
        with pytest.raises(ValidationError):
            job.clean()
