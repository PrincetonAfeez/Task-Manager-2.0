"""Web views for the Phase 2 feature set, plus auth views and Celery entry points."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.utils import timezone

from billing.models import Plan
from billing.services import ensure_default_plans, get_subscription
from integrations.models import WebhookDelivery, WebhookEndpoint
from kernel.models import AuditLog, Membership
from tasks.models import ShareLink, Subtask, Task, TaskTemplate, TimeEntry

pytestmark = pytest.mark.django_db


class TestTimerViews:
    def test_start_and_stop_via_the_ui(self, client_owner, task, user):
        client_owner.post(f"/tasks/{task.pk}/timer/start/")
        assert TimeEntry.objects.filter(user=user, ended_at__isnull=True).exists()

        client_owner.post("/timer/stop/")
        assert not TimeEntry.objects.filter(user=user, ended_at__isnull=True).exists()

    def test_htmx_gets_the_widget_fragment(self, client_owner, task):
        response = client_owner.post(
            f"/tasks/{task.pk}/timer/start/", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        assert b"<html" not in response.content
        assert b"timer-widget" in response.content

    def test_stopping_with_no_timer_is_harmless(self, client_owner):
        assert client_owner.post("/timer/stop/").status_code == 302

    def test_a_viewer_cannot_start_a_timer(self, client_viewer, task):
        assert client_viewer.post(f"/tasks/{task.pk}/timer/start/").status_code == 403

    def test_logging_time_manually(self, client_owner, task, user):
        client_owner.post(f"/tasks/{task.pk}/time/", {"minutes": 45, "note": "Pairing"})
        assert TimeEntry.objects.get(task=task, user=user).duration_seconds == 45 * 60

    def test_an_invalid_duration_is_rejected(self, client_owner, task):
        client_owner.post(f"/tasks/{task.pk}/time/", {"minutes": -5})
        assert not TimeEntry.objects.filter(task=task).exists()

    def test_cannot_time_another_tenants_task(self, client_owner, other_task):
        assert client_owner.post(f"/tasks/{other_task.pk}/timer/start/").status_code == 404

    def test_the_report_renders(self, client_owner, task, user):
        from tasks.services_extra import log_time

        log_time(task=task, user=user, minutes=60)
        response = client_owner.get("/time/")
        assert response.status_code == 200
        assert response.context["summary"]["total_hours"] == 1.0

    def test_the_report_window_is_clamped(self, client_owner):
        assert client_owner.get("/time/?days=99999").context["days"] == 365
        assert client_owner.get("/time/?days=abc").context["days"] == 30

    def test_the_report_can_be_scoped_to_me(self, client_owner, task, user, member):
        from tasks.services_extra import log_time

        log_time(task=task, user=member.user, minutes=30)
        log_time(task=task, user=user, minutes=60)
        assert client_owner.get("/time/?scope=mine").context["summary"]["total_hours"] == 1.0


class TestTemplateViews:
    def test_create_and_apply(self, client_owner, organization, task_list):
        client_owner.post(
            "/templates/",
            {
                "name": "Onboarding",
                "title_template": "Onboard a hire",
                "task_list": task_list.pk,
                "priority": 2,
                "subtask_titles_text": "Send laptop\nCreate accounts",
            },
        )
        template = TaskTemplate.objects.get(organization=organization, name="Onboarding")
        assert template.subtask_titles == ["Send laptop", "Create accounts"]

        client_owner.post(f"/templates/{template.pk}/apply/")
        task = Task.objects.get(title="Onboard a hire")
        assert Subtask.objects.filter(task=task).count() == 2

    def test_save_an_existing_task_as_a_template(self, client_owner, task, organization):
        client_owner.post(f"/tasks/{task.pk}/save-template/", {"name": "Captured"})
        assert TaskTemplate.objects.filter(organization=organization, name="Captured").exists()

    def test_delete(self, client_owner, organization, task_list):
        template = TaskTemplate.objects.create(
            organization=organization, name="Doomed", title_template="x", task_list=task_list
        )
        client_owner.post(f"/templates/{template.pk}/delete/")
        assert not TaskTemplate.objects.filter(pk=template.pk).exists()

    def test_cannot_apply_another_tenants_template(self, client_owner, other_organization):
        foreign = TaskTemplate.objects.create(
            organization=other_organization, name="Theirs", title_template="x"
        )
        assert client_owner.post(f"/templates/{foreign.pk}/apply/").status_code == 404

    def test_a_viewer_cannot_create_a_template(self, client_viewer, task_list):
        response = client_viewer.post(
            "/templates/", {"name": "Nope", "title_template": "x", "task_list": task_list.pk}
        )
        assert response.status_code == 403

    def test_a_viewer_can_read_the_list(self, client_viewer):
        assert client_viewer.get("/templates/").status_code == 200

    def test_a_duplicate_name_shows_an_error_rather_than_a_500(
        self, client_owner, organization, task
    ):
        TaskTemplate.objects.create(
            organization=organization, name="Taken", title_template="x"
        )
        response = client_owner.post(f"/tasks/{task.pk}/save-template/", {"name": "Taken"})
        assert response.status_code == 302
        assert TaskTemplate.objects.filter(name="Taken").count() == 1


class TestCalendarView:
    def test_renders_the_current_month(self, client_owner):
        response = client_owner.get("/calendar/")
        assert response.status_code == 200
        assert response.context["weeks"]

    def test_a_specific_month(self, client_owner):
        response = client_owner.get("/calendar/?year=2026&month=3")
        assert response.context["month"] == 3
        assert response.context["month_name"] == "March"

    def test_only_mine(self, client_owner, organization, task_list, user, member):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=member.user,
            title="Theirs",
            due_date=timezone.localdate(),
        )
        response = client_owner.get("/calendar/?mine=true")
        assert response.context["task_count"] == 0

    @pytest.mark.parametrize("query", ["?year=abc", "?month=99", "?year=1&month=0"])
    def test_malformed_parameters_fall_back(self, client_owner, query):
        assert client_owner.get(f"/calendar/{query}").status_code == 200


class TestShareLinkViews:
    def test_creation_shows_the_url_once(self, client_owner, task_list):
        response = client_owner.post(
            "/settings/sharing/", {"task_list": task_list.pk, "expires_in_days": 7}
        )
        assert "/shared/" in response.context["new_url"]
        assert client_owner.get("/settings/sharing/").context["new_url"] is None

    def test_creation_is_audited(self, client_owner, task_list, organization):
        client_owner.post("/settings/sharing/", {"task_list": task_list.pk})
        assert AuditLog.objects.filter(
            action="share_link.created", organization=organization
        ).exists()

    def test_revoking_kills_the_link(self, client_owner, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        client_owner.post(f"/settings/sharing/{link.pk}/revoke/")
        link.refresh_from_db()
        assert link.revoked_at is not None

    def test_cannot_revoke_another_tenants_link(self, client_owner, other_organization, other_task_list):
        link, _ = ShareLink.issue(
            organization=other_organization, task_list=other_task_list
        )
        assert client_owner.post(f"/settings/sharing/{link.pk}/revoke/").status_code == 404

    def test_cannot_share_another_tenants_list(self, client_owner, other_task_list):
        response = client_owner.post("/settings/sharing/", {"task_list": other_task_list.pk})
        assert response.context["new_url"] is None


class TestWebhookViewsExtra:
    @pytest.fixture
    def endpoint(self, organization, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        return WebhookEndpoint.objects.create(
            organization=organization, name="Receiver", url="https://hooks.example.com/x"
        )

    def test_detail_lists_deliveries(self, client_owner, organization, endpoint):
        WebhookDelivery.objects.create(
            organization=organization,
            endpoint=endpoint,
            event="task.created",
            event_id="evt_1",
            status=WebhookDelivery.Status.SUCCEEDED,
            status_code=200,
        )
        response = client_owner.get(f"/settings/webhooks/{endpoint.pk}/")
        assert response.status_code == 200
        assert b"task.created" in response.content

    def test_delete(self, client_owner, endpoint):
        client_owner.post(f"/settings/webhooks/{endpoint.pk}/delete/")
        assert not WebhookEndpoint.objects.filter(pk=endpoint.pk).exists()

    def test_deletion_is_audited(self, client_owner, endpoint, organization):
        client_owner.post(f"/settings/webhooks/{endpoint.pk}/delete/")
        assert AuditLog.objects.filter(action="webhook.deleted").exists()

    def test_the_quota_is_enforced(self, client_owner, organization, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        plan = Plan.objects.create(code="nohooks", name="No hooks", max_webhooks=0, position=90)
        subscription = get_subscription(organization)
        subscription.plan = plan
        subscription.save()

        client_owner.post(
            "/settings/webhooks/", {"name": "Blocked", "url": "https://hooks.example.com/b"}
        )
        assert not WebhookEndpoint.objects.filter(name="Blocked").exists()


class TestBillingViews:
    def test_the_overview_renders_usage_and_plans(self, client_owner):
        response = client_owner.get("/settings/billing/")
        assert response.status_code == 200
        assert response.context["usage"]["members"]["used"] >= 1
        assert response.context["plans"]

    def test_an_owner_can_change_plan(self, client_owner, organization):
        ensure_default_plans()
        client_owner.post("/settings/billing/change/", {"plan": "team"})
        assert get_subscription(organization).plan.code == "team"

    def test_an_admin_cannot_change_plan(self, client, admin_membership, organization):
        ensure_default_plans()
        client.force_login(admin_membership.user)
        assert client.post("/settings/billing/change/", {"plan": "team"}).status_code == 403

    def test_a_member_can_see_the_page_but_not_the_controls(self, client_member):
        response = client_member.get("/settings/billing/")
        assert response.status_code == 200
        assert response.context["can_manage"] is False

    def test_an_unknown_plan_is_404(self, client_owner):
        assert client_owner.post("/settings/billing/change/", {"plan": "unicorn"}).status_code == 404

    def test_a_downgrade_that_does_not_fit_is_reported_not_applied(
        self, client_owner, organization, task_list, user
    ):
        ensure_default_plans()
        tiny = Plan.objects.create(code="onetask", name="One Task", max_tasks=1, position=89)
        for index in range(3):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        client_owner.post("/settings/billing/change/", {"plan": tiny.code})
        assert get_subscription(organization).plan.code != tiny.code


class TestExportViews:
    def test_the_workspace_archive_downloads(self, client_owner, task):
        response = client_owner.get("/export/workspace/")
        assert response.status_code == 200
        assert "attachment" in response["Content-Disposition"]
        assert task.title in response.content.decode()

    def test_the_archive_is_audited(self, client_owner, organization):
        client_owner.get("/export/workspace/")
        assert AuditLog.objects.filter(action="workspace.exported").exists()

    def test_include_archived(self, client_owner, task, user):
        from tasks.services import archive_task

        archive_task(task=task, actor=user)
        plain = b"".join(client_owner.get("/export/tasks/").streaming_content).decode()
        with_archived = b"".join(
            client_owner.get("/export/tasks/?include_archived=true").streaming_content
        ).decode()
        assert task.title not in plain
        assert task.title in with_archived

    def test_a_member_cannot_export_the_workspace(self, client_member):
        assert client_member.get("/export/workspace/").status_code == 403


class TestAccountViews:
    def test_the_settings_page_renders(self, client_owner, organization):
        response = client_owner.get("/account/")
        assert response.status_code == 200
        # The signed-in user solely owns the fixture workspace.
        assert list(response.context["blocking_workspaces"]) == [organization]

    def test_leaving_a_workspace(self, client_member, organization, member_user):
        client_member.post("/settings/workspace/leave/")
        assert not Membership.objects.filter(organization=organization, user=member_user).exists()

    def test_the_last_owner_cannot_leave(self, client_owner, organization, user):
        client_owner.post("/settings/workspace/leave/")
        assert Membership.objects.filter(organization=organization, user=user).exists()

    def test_account_deletion_needs_the_workspace_opt_in(self, client_owner, user):
        from django.contrib.auth import get_user_model

        client_owner.post("/account/delete/", {"confirm": user.get_username()})
        assert get_user_model().objects.filter(pk=user.pk).exists()

    def test_account_deletion_with_the_opt_in_succeeds(self, client_owner, user):
        from django.contrib.auth import get_user_model

        response = client_owner.post(
            "/account/delete/", {"confirm": user.get_username(), "delete_workspaces": "on"}
        )
        assert response.status_code == 302
        assert not get_user_model().objects.filter(pk=user.pk).exists()


class TestAuthViews:
    def test_a_successful_login_is_audited(self, client, user):
        client.post(
            "/accounts/login/", {"username": user.username, "password": "test-password-123456"}
        )
        assert AuditLog.objects.filter(action="auth.login_succeeded").exists()

    def test_a_failed_login_is_audited_without_the_password(self, client, user):
        client.post("/accounts/login/", {"username": user.username, "password": "hunter2"})
        entry = AuditLog.objects.get(action="auth.login_failed")
        assert "hunter2" not in str(entry.metadata)

    def _hammer(self, client, user, attempts):
        """Exercises the *shipped* rate (THROTTLE_AUTH, 10/minute) rather than a value
        invented by the test, so a bad production default cannot pass here."""

        return [
            client.post(
                "/accounts/login/", {"username": user.username, "password": "wrong"}
            ).status_code
            for _ in range(attempts)
        ]

    def test_login_is_rate_limited(self, client, user, settings):
        settings.API_THROTTLING_ENABLED = True
        statuses = self._hammer(client, user, 14)
        assert 429 in statuses, "the login form accepted unlimited attempts"

    def test_a_throttled_login_carries_retry_after(self, client, user, settings):
        """Without it a rate-limited client has no idea when to come back, so it hammers."""

        settings.API_THROTTLING_ENABLED = True
        self._hammer(client, user, 12)
        response = client.post(
            "/accounts/login/", {"username": user.username, "password": "wrong"}
        )
        assert response.status_code == 429
        assert int(response["Retry-After"]) > 0

    def test_the_account_lockout_engages_before_the_ip_throttle(self, client, user, settings):
        """Two independent controls: one address trying many accounts, and many addresses
        trying one account. The account lockout is the tighter of the two."""

        from kernel.auth_backends import is_locked_out

        settings.API_THROTTLING_ENABLED = False
        settings.LOGIN_LOCKOUT_MAX_ATTEMPTS = 4
        self._hammer(client, user, 4)
        assert is_locked_out(user.username, "")

        # The correct password is refused while the account is locked.
        response = client.post(
            "/accounts/login/", {"username": user.username, "password": "test-password-123456"}
        )
        assert response.status_code == 200  # re-rendered form, not a redirect
        assert "_auth_user_id" not in client.session

    def test_password_reset_flow_pages_render(self, client):
        assert client.get("/accounts/password-reset/").status_code == 200
        assert client.get("/accounts/password-reset/done/").status_code == 200
        assert client.get("/accounts/reset/done/").status_code == 200

    def test_password_reset_sends_an_email_for_a_known_address(self, client, user):
        client.post("/accounts/password-reset/", {"email": user.email})
        assert len(mail.outbox) == 1
        assert "/accounts/reset/" in mail.outbox[0].body

    def test_password_reset_reveals_nothing_for_an_unknown_address(self, client):
        response = client.post("/accounts/password-reset/", {"email": "nobody@example.com"})
        assert response.status_code == 302
        assert len(mail.outbox) == 0

    def test_password_change_requires_a_session(self, client):
        assert client.get("/accounts/password-change/").status_code == 302

    def test_logout_redirects_to_login(self, client_owner):
        """Redirecting to a login-required page produced a bounce."""

        response = client_owner.post("/accounts/logout/")
        assert response.status_code == 302
        assert "login" in response["Location"]


class TestCeleryEntryPoints:
    """The Celery layer is thin, but if the wrappers are wrong nothing ever runs."""

    def test_dispatch_task(self, organization, queue, user):
        from queues.models import Job
        from queues.services import enqueue_job
        from queues.tasks import dispatch_available_jobs

        enqueue_job(
            organization=organization,
            queue=queue,
            source="celery",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={"user_id": user.pk, "title": "Hi", "natural_key": "celery-1"},
            idempotency_key="celery-1",
        )
        # The tick keeps claiming until the queue is empty, so the delivery job this
        # notification enqueues is drained by the same call.
        assert dispatch_available_jobs()["succeeded"] >= 1
        assert not Job.objects.filter(status__in=Job.CLAIMABLE_STATUSES).exists()

    def test_reaper_task(self):
        from queues.tasks import reap_expired_leases_task

        assert reap_expired_leases_task()["reclaimed"] == 0

    def test_metrics_task(self, organization, queue):
        from queues.tasks import snapshot_metrics_task

        assert snapshot_metrics_task()["snapshots"] >= 1

    def test_schedule_scan_task(self, organization, queue):
        from queues.tasks import scan_due_schedules_task
        from schedules.models import Schedule

        Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Due",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            target_handler="internal:schedule_fire",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        assert scan_due_schedules_task()["occurrences"] == 1

    def test_recurrence_sweep_task(self, task):
        from queues.tasks import sweep_recurrences_task
        from tasks.models import RecurrenceRule

        RecurrenceRule.objects.create(
            organization=task.organization,
            source_task=task,
            frequency=RecurrenceRule.Frequency.DAILY,
            next_occurrence_date=timezone.localdate(),
        )
        assert sweep_recurrences_task()["created"] == 1

    def test_digest_task(self, organization, user, membership):
        from queues.tasks import enqueue_due_digests_task

        assert "queued" in enqueue_due_digests_task()

    def test_purge_task(self, organization, user):
        from kernel.services import soft_delete_organization
        from queues.tasks import purge_expired_organizations_task

        soft_delete_organization(organization=organization, actor=user, retention_days=0)
        assert purge_expired_organizations_task()["queued"] == 1

    def test_the_beat_schedule_covers_every_periodic_task(self):
        """A task with no beat entry never runs, however correct it is."""

        from config.celery import app

        scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
        for name in (
            "queues.dispatch_available_jobs",
            "queues.reap_expired_leases",
            "schedules.scan_due_schedules",
            "tasks.sweep_recurrences",
            "kernel.purge_expired_organizations",
        ):
            assert name in scheduled, f"{name} has no beat schedule entry"

    def test_every_beat_entry_has_an_expiry(self):
        """Without one, a stalled beat fires its whole backlog at once on recovery."""

        from config.celery import app

        for name, entry in app.conf.beat_schedule.items():
            assert entry.get("options", {}).get("expires"), f"{name} has no expires"

    def test_routed_queues_match_the_beat_entries(self):
        from config.celery import app

        for task_name, route in app.conf.task_routes.items():
            assert route["queue"] in {"dispatcher", "scheduler", "reaper"}


class TestSchedulerCommand:
    def test_one_tick(self, organization, queue):
        from io import StringIO

        from schedules.models import Schedule

        Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Due",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            target_handler="internal:schedule_fire",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        out = StringIO()
        call_command("run_scheduler", "--once", stdout=out)
        assert "schedules" in out.getvalue()

    def test_a_bounded_loop_terminates(self, organization):
        from io import StringIO

        out = StringIO()
        call_command("run_scheduler", "--max-iterations", "2", "--interval", "0", stdout=out)
        assert "Scheduler stopped" in out.getvalue()


class TestRestoreCommand:
    def test_a_corrupt_backup_is_refused(self, tmp_path):
        from io import StringIO

        bogus = tmp_path / "not-a-database.sqlite3"
        bogus.write_bytes(b"this is not a sqlite file")
        with pytest.raises(CommandError):
            call_command("restore_db", str(bogus), "--confirm", stdout=StringIO())

    def test_production_needs_the_extra_acknowledgement(self, tmp_path, settings):
        from io import StringIO

        settings.IS_PRODUCTION = True
        call_command("backup_db", "--output-dir", str(tmp_path), stdout=StringIO())
        backup = next(tmp_path.glob("backup-*"))
        with pytest.raises(CommandError, match="production"):
            call_command("restore_db", str(backup), "--confirm", stdout=StringIO())
