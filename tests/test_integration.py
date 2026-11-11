"""End-to-end journeys.

Each test crosses several app boundaries deliberately. Unit tests prove the parts work;
these prove the seams do — which is where this codebase's most serious bugs lived.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from kernel.models import AuditLog, Membership, Notification, Organization
from kernel.roles import Role
from queues.models import DeadLetter, Job, JobAttempt
from queues.runtime import run_once
from queues.services import claim_jobs, get_internal_queue, reap_expired_leases, redrive
from schedules.models import Schedule, ScheduleOccurrence
from schedules.services import scan_due_schedules
from tasks.models import Reminder, Task
from tasks.recurrence import sweep_due_recurrences
from tasks.services import create_task_from_quick_add, toggle_task_completion

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestReminderJourney:
    def test_quick_add_to_delivered_notification(self, organization, task_list, user):
        """The single most important path in the product, and the one that was entirely
        broken: quick add -> reminder -> durable job -> worker -> in-app notification."""

        task = create_task_from_quick_add(
            organization=organization,
            task_list=task_list,
            creator=user,
            raw="Ship the release tomorrow 5pm #ops !3",
        )
        assert task.title == "Ship the release"
        assert task.priority == 3

        reminder = Reminder.objects.get(task=task)
        assert reminder.status == Reminder.Status.ARMED
        assert reminder.job is not None

        # Fast-forward to the reminder instant.
        Job.objects.filter(pk=reminder.job_id).update(available_at=timezone.now())
        summary = run_once(queue=get_internal_queue(organization))
        assert summary["succeeded"] >= 1

        reminder.refresh_from_db()
        assert reminder.status == Reminder.Status.SENT
        assert Notification.objects.filter(
            user=user, category=Notification.Category.REMINDER
        ).exists()

    def test_completing_before_the_reminder_fires_cancels_it(self, organization, task_list, user):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Do it tomorrow 5pm"
        )
        reminder = Reminder.objects.get(task=task)

        toggle_task_completion(task=task, actor=user)

        reminder.refresh_from_db()
        assert reminder.status == Reminder.Status.CANCELED
        assert Job.objects.get(pk=reminder.job_id).status == Job.Status.CANCELED

    def test_a_reminder_for_a_deleted_task_is_skipped_not_retried(
        self, organization, task_list, user
    ):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Vanishing tomorrow 5pm"
        )
        job = Reminder.objects.get(task=task).job
        Task.objects.filter(pk=task.pk).delete()
        Job.objects.filter(pk=job.pk).update(available_at=timezone.now())

        run_once(queue=get_internal_queue(organization))
        job.refresh_from_db()
        assert job.status == Job.Status.SUCCEEDED
        assert job.result["skipped"] == "task_missing"


class TestScheduleJourney:
    def test_schedule_to_executed_job(self, organization, queue):
        schedule = Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Every minute",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            target_handler="internal:schedule_fire",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )

        assert scan_due_schedules()["occurrences"] == 1
        occurrence = ScheduleOccurrence.objects.get(schedule=schedule)
        assert occurrence.status == ScheduleOccurrence.Status.ENQUEUED

        Job.objects.filter(pk=occurrence.job_id).update(available_at=timezone.now())
        run_once(queue=queue)

        occurrence.refresh_from_db()
        assert Job.objects.get(pk=occurrence.job_id).status == Job.Status.SUCCEEDED

        schedule.refresh_from_db()
        assert schedule.next_run_at > timezone.now()

    def test_two_scans_of_the_same_tick_produce_one_job(self, organization, queue):
        """Two scheduler instances racing must not double-fire."""

        schedule = Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Racing",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            target_handler="internal:schedule_fire",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        instant = schedule.next_run_at

        scan_due_schedules()
        Schedule.objects.filter(pk=schedule.pk).update(next_run_at=instant)
        scan_due_schedules()

        assert ScheduleOccurrence.objects.filter(schedule=schedule).count() == 1


class TestRecurrenceJourney:
    def test_completing_a_recurring_task_produces_the_next_one(
        self, organization, task_list, user
    ):
        task = create_task_from_quick_add(
            organization=organization,
            task_list=task_list,
            creator=user,
            raw="Weekly report tomorrow weekly",
        )
        assert hasattr(task, "recurrence")

        toggle_task_completion(task=task, actor=user)

        job = Job.objects.get(handler="internal:materialize_recurrence")
        Job.objects.filter(pk=job.pk).update(available_at=timezone.now())
        run_once(queue=get_internal_queue(organization))

        assert Task.objects.filter(recurrence_source=task).count() == 1
        follow_up = Task.objects.get(recurrence_source=task)
        assert follow_up.title == task.title
        assert follow_up.status == Task.Status.OPEN

    def test_the_sweep_materialises_date_driven_recurrences(self, organization, task_list, user):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Daily standup daily"
        )
        task.recurrence.next_occurrence_date = timezone.localdate()
        task.recurrence.save()

        assert sweep_due_recurrences()["created"] == 1


class TestFailureJourney:
    def test_failure_to_retry_to_dead_letter_to_redrive(self, organization, queue, user):
        """The whole failure lifecycle, which previously did not exist at all."""

        from queues.registry import register
        from queues.services import enqueue_job

        attempts = {"count": 0}

        @register("test:always_fails_integration", max_attempts=2)
        def always_fails(job, context):
            attempts["count"] += 1
            raise RuntimeError("nope")

        job = enqueue_job(
            organization=organization,
            queue=queue,
            source="integration",
            handler_type=Job.HandlerType.INTERNAL,
            handler="test:always_fails_integration",
            payload={},
            idempotency_key="integration:fail",
            max_attempts=2,
        ).job

        # Attempt 1 -> retry scheduled.
        run_once(queue=queue)
        job.refresh_from_db()
        assert job.status == Job.Status.FAILED
        assert job.next_retry_at > timezone.now()

        # Attempt 2 -> budget exhausted -> dead letter.
        Job.objects.filter(pk=job.pk).update(available_at=timezone.now())
        run_once(queue=queue)
        job.refresh_from_db()
        assert job.status == Job.Status.DEAD
        assert attempts["count"] == 2

        dead_letter = DeadLetter.objects.get(job=job)
        assert JobAttempt.objects.filter(job=job).count() == 2

        # An operator fixes the cause and redrives.
        restored = redrive(dead_letter, actor=user)
        assert restored.status == Job.Status.AVAILABLE
        assert restored.attempts_made == 0

    def test_a_crashed_worker_does_not_strand_its_jobs(self, organization, queue):
        from queues.services import enqueue_job, start_attempt

        enqueue_job(
            organization=organization,
            queue=queue,
            source="integration",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="integration:crash",
        )
        claimed = claim_jobs(queue=queue, worker_id="doomed-worker")[0]
        start_attempt(claimed, worker_id="doomed-worker")

        # The worker dies. Nothing reports an outcome; the lease simply lapses.
        Job.objects.filter(pk=claimed.job.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=10)
        )
        assert reap_expired_leases()["reclaimed"] == 1

        claimed.job.refresh_from_db()
        assert claimed.job.status == Job.Status.AVAILABLE
        assert JobAttempt.objects.get(job=claimed.job).outcome == JobAttempt.Outcome.TIMED_OUT


class TestOnboardingJourney:
    def test_signup_invite_accept_and_collaborate(self, client, other_user):
        # 1. A founder signs up.
        client.post(
            "/signup/",
            {
                "username": "founder",
                "email": "founder@example.com",
                "password1": "a-long-enough-password-9",
                "password2": "a-long-enough-password-9",
                "workspace_name": "Startup Co",
            },
        )
        organization = Organization.objects.get(name="Startup Co")

        # 2. They add a task.
        client.post("/quick-add/create/", {"quick_add": "Set up the office tomorrow 9am #admin"})
        assert Task.objects.filter(organization=organization).count() == 1

        # 3. They invite a colleague.
        client.post("/settings/members/", {"email": "colleague@example.com", "role": "member"})
        from kernel.models import Invitation

        invitation = Invitation.objects.get(email="colleague@example.com")

        # 4. The colleague accepts. The raw token only exists in the emailed link, so it is
        #    re-issued here the same way the service would.
        from kernel.services import invite_member

        founder = organization.owner
        Invitation.objects.filter(pk=invitation.pk).update(status=Invitation.Status.REVOKED)
        _fresh, token = invite_member(
            organization=organization,
            actor=founder,
            actor_role=Role.OWNER,
            email="colleague@example.com",
            role=Role.MEMBER,
        )

        client.force_login(other_user)
        client.post(f"/invitations/{token}/")
        assert Membership.objects.filter(organization=organization, user=other_user).exists()

        # 5. The colleague sees the workspace's task and can act on it.
        response = client.get("/")
        assert b"Set up the office" in response.content

        task = Task.objects.get(organization=organization)
        client.post(f"/tasks/{task.pk}/toggle/", {"expected_version": task.version})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestApiKeyJourney:
    def test_mint_a_key_then_use_it_from_a_script(self, client_owner, organization, task_list):
        from rest_framework.test import APIClient

        # 1. An admin mints a key in the UI.
        response = client_owner.post(
            "/settings/api-keys/", {"name": "Deploy bot", "role": Role.MEMBER}
        )
        token = response.context["new_token"]

        # 2. A script uses it, with no session at all.
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Api-Key {token}")

        created = api.post(
            "/api/v1/tasks/", {"title": "Created by CI", "task_list": task_list.pk}, format="json"
        )
        assert created.status_code == 201
        assert Task.objects.filter(title="Created by CI").exists()

        # 3. The key's role ceiling still applies: a member cannot manage keys.
        assert api.get("/api/v1/api-keys/").status_code == 403

        # 4. Revoking stops it dead.
        from kernel.models import ApiKey

        ApiKey.objects.filter(organization=organization).update(revoked_at=timezone.now())
        assert api.get("/api/v1/tasks/").status_code == 401


class TestWebhookJourney:
    def test_task_creation_to_signed_delivery(self, organization, task_list, user, settings,
                                              django_capture_on_commit_callbacks):
        from integrations.models import WebhookDelivery, WebhookEndpoint

        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        endpoint = WebhookEndpoint.objects.create(
            organization=organization, name="CRM", url="https://hooks.example.com/tm"
        )

        with django_capture_on_commit_callbacks(execute=True):
            create_task_from_quick_add(
                organization=organization, task_list=task_list, creator=user, raw="Notify the CRM"
            )

        job = Job.objects.get(handler="http_delivery")
        with patch("integrations.delivery._post", return_value=(200, "ok")) as post:
            from queues.services import get_webhook_queue

            run_once(queue=get_webhook_queue(organization))

        job.refresh_from_db()
        assert job.status == Job.Status.SUCCEEDED

        delivery = WebhookDelivery.objects.get(endpoint=endpoint)
        assert delivery.status == WebhookDelivery.Status.SUCCEEDED

        # The receiver got a verifiable signature and a stable event id.
        _url, body, headers = post.call_args[0]
        assert headers["X-TaskManager-Signature"].startswith("t=")
        assert "Notify the CRM" in body


class TestWorkspaceLifecycleJourney:
    def test_create_use_delete_and_purge(self, user, organization, task_list):
        from kernel.purge import purge_organization_data
        from kernel.services import soft_delete_organization

        # 1. Real usage.
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Some work tomorrow"
        )
        assert Job.objects.filter(organization=organization).exists()

        # 2. The owner deletes the workspace.
        soft_delete_organization(organization=organization, actor=user)
        organization.refresh_from_db()
        assert organization.deleted_at is not None
        # It disappears immediately from every query.
        assert list(Organization.objects.for_user(user)) == []
        # But the data is still recoverable during the window.
        assert Task.objects.filter(pk=task.pk).exists()

        # 3. The retention window elapses and the purge runs.
        result = purge_organization_data(organization.pk)
        assert result["purged"] is True
        assert not Task.objects.filter(pk=task.pk).exists()
        assert not Job.objects.filter(organization_id=organization.pk).exists()
        assert not Organization.objects.filter(pk=organization.pk).exists()

        # 4. The audit trail survives, detached.
        assert AuditLog.objects.filter(action="organization.deleted").exists()


class TestConcurrencyJourney:
    def test_two_editors_do_not_silently_clobber_each_other(self, api_client, task):
        """The scenario `Task.version` existed for and never actually protected against."""

        original_version = task.version

        first = api_client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Alice's title", "expected_version": original_version},
            format="json",
        )
        assert first.status_code == 200

        second = api_client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Bob's title", "expected_version": original_version},
            format="json",
        )
        assert second.status_code == 409

        task.refresh_from_db()
        assert task.title == "Alice's title"

    def test_two_workers_never_run_the_same_job(self, organization, queue):
        from queues.services import enqueue_job

        enqueue_job(
            organization=organization,
            queue=queue,
            source="integration",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="integration:exclusive",
        )
        first = claim_jobs(queue=queue, worker_id="worker-a", limit=5)
        second = claim_jobs(queue=queue, worker_id="worker-b", limit=5)

        assert len(first) == 1
        assert second == []


class TestPermissionJourney:
    def test_a_viewer_is_read_only_across_the_whole_product(self, client_viewer, task, queue):
        # Reads succeed.
        assert client_viewer.get("/").status_code == 200
        assert client_viewer.get("/tasks/").status_code == 200
        assert client_viewer.get(f"/tasks/{task.pk}/").status_code == 200

        # Every write is refused.
        for method, url, data in [
            ("post", "/quick-add/create/", {"quick_add": "Nope"}),
            ("post", f"/tasks/{task.pk}/toggle/", {}),
            ("post", f"/tasks/{task.pk}/comments/", {"body": "Nope"}),
            ("post", "/tasks/bulk/", {"task_ids": [task.pk], "action": "complete"}),
            ("post", f"/automation/queues/{queue.pk}/pause/", {}),
        ]:
            response = getattr(client_viewer, method)(url, data)
            assert response.status_code == 403, f"{url} allowed a viewer to write"

        # Nothing changed.
        task.refresh_from_db()
        assert task.status == Task.Status.OPEN
        assert Task.objects.count() == 1
