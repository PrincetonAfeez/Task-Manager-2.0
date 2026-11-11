"""Real concurrency, on a real server database.

SQLite serialises writers and has no ``SELECT ... FOR UPDATE SKIP LOCKED``, so the queue's
production claim path never runs there. These tests use several threads with their own
connections and are skipped on SQLite, where they would prove nothing.
"""

from __future__ import annotations

import threading

import pytest
from django.db import connection, connections

from kernel.services import create_organization
from queues.models import Job, Queue
from queues.services import claim_jobs, enqueue_job, get_internal_queue
from schedules.models import Schedule, ScheduleOccurrence
from schedules.services import fire_schedule

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="exercises PostgreSQL row locking"
    ),
]

WORKERS = 6


def _run_in_threads(target, count=WORKERS):
    """Start ``count`` threads at the same instant and collect results or errors."""

    barrier = threading.Barrier(count)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index):
        try:
            barrier.wait(timeout=10)
            value = target(index)
            with lock:
                results.append(value)
        except Exception as exc:  # surfaced by the assertion below
            with lock:
                errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    return results


@pytest.fixture
def workspace(django_user_model):
    owner = django_user_model.objects.create_user(username="pg-owner", password="x" * 14)
    organization = create_organization(name="Concurrency Co", owner=owner)
    queue = get_internal_queue(organization)
    Queue.objects.filter(pk=queue.pk).update(concurrency_limit=1000)
    queue.refresh_from_db()
    return organization, queue, owner


def test_concurrent_workers_never_claim_the_same_job(workspace):
    organization, queue, _owner = workspace
    total = 60
    for index in range(total):
        enqueue_job(
            organization=organization,
            queue=queue,
            source="pg",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key=f"pg-{index}",
        )

    def claim(index):
        return [c.job.pk for c in claim_jobs(queue=queue, worker_id=f"w{index}", limit=total)]

    batches = _run_in_threads(claim)
    claimed = [pk for batch in batches for pk in batch]

    assert len(claimed) == len(set(claimed)), "a job was leased to two workers"
    assert len(claimed) == total
    assert Job.objects.filter(status=Job.Status.LEASED).count() == total
    # Each job was delivered exactly once.
    assert set(Job.objects.values_list("receives", flat=True)) == {1}


def test_concurrent_enqueue_of_one_identity_creates_one_job(workspace):
    organization, queue, _owner = workspace

    def enqueue(_index):
        return enqueue_job(
            organization=organization,
            queue=queue,
            source="pg-race",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="same-key",
        )

    results = _run_in_threads(enqueue)
    assert Job.objects.filter(source="pg-race").count() == 1
    assert sum(1 for result in results if result.created) == 1
    assert len({result.job.pk for result in results}) == 1


def test_concurrent_schedulers_fire_a_tick_once(workspace):
    from datetime import timedelta

    from django.utils import timezone

    organization, queue, _owner = workspace
    due = (timezone.now() - timedelta(minutes=1)).replace(second=0, microsecond=0)
    schedule = Schedule.objects.create(
        organization=organization,
        queue=queue,
        name="Raced",
        expression_type=Schedule.ExpressionType.CRON,
        expression="* * * * *",
        target_handler="internal:schedule_fire",
        catch_up_policy=Schedule.CatchUpPolicy.RUN_ONCE,
        next_run_at=due,
    )
    now = timezone.now()

    def fire(_index):
        # Every thread starts from the same stale view of the schedule, as two scheduler
        # processes would after reading it in the same instant.
        stale = Schedule.objects.select_related("queue", "organization").get(pk=schedule.pk)
        stale.next_run_at = due
        return fire_schedule(stale, now=now)

    results = _run_in_threads(fire)

    assert ScheduleOccurrence.objects.filter(schedule=schedule).count() == 1
    assert Job.objects.filter(source=f"schedule:{schedule.pk}").count() == 1
    assert sum(result.occurrences_created for result in results) == 1


def test_concurrent_timer_starts_leave_one_running_timer(workspace):
    from tasks.models import Task, TaskList, TimeEntry
    from tasks.services_extra import TimerAlreadyRunning, start_timer

    organization, _queue, owner = workspace
    task_list = TaskList.objects.get(organization=organization, name="Inbox")
    tasks = [
        Task.objects.create(
            organization=organization, task_list=task_list, creator=owner, title=f"T{i}"
        )
        for i in range(WORKERS)
    ]

    def start(index):
        try:
            return start_timer(task=tasks[index], user=owner).pk
        except TimerAlreadyRunning:
            return None

    _run_in_threads(start)
    assert TimeEntry.objects.filter(user=owner, ended_at__isnull=True).count() == 1
