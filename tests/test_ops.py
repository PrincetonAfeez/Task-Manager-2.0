"""Operational surface: health endpoints, logging, middleware, management commands.

The original health endpoints returned ``{"status": "ok"}`` unconditionally. Every test
here asserts a degraded condition actually produces a 503, because a health check that
cannot fail is worse than no health check at all.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from queues.models import Job
from queues.services import claim_jobs, enqueue_job, get_internal_queue
from schedules.models import Schedule

pytestmark = pytest.mark.django_db


class TestHealthEndpoints:
    def test_healthz_is_dependency_free(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "web"

    def test_healthz_needs_no_authentication(self, client):
        assert client.get("/healthz").status_code == 200

    def test_readyz_reports_each_dependency(self, client):
        body = client.get("/readyz").json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["status"] == "ok"
        assert body["checks"]["cache"]["status"] == "ok"

    def test_readyz_returns_503_when_the_database_is_unreachable(self, client):
        """A load balancer must be able to act on this; a 500 traceback is not actionable."""

        with patch("ops.views.connection") as fake:
            fake.cursor.side_effect = RuntimeError("connection refused")
            response = client.get("/readyz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["database"]["status"] == "error"
        # The driver message may name hosts and credentials; only the type is exposed.
        assert "connection refused" not in response.content.decode()

    def test_version_endpoint(self, client):
        body = client.get("/version").json()
        assert body["api_version"] == "v1"
        assert "release" in body


class TestWorkerHealth:
    def test_healthy_when_idle(self, client):
        assert client.get("/healthz/worker").status_code == 200

    def test_degraded_when_the_backlog_is_stale(self, client, organization, settings):
        """Regression: this endpoint used to return ok while the fleet was down."""

        settings.QUEUE_OLDEST_JOB_ALERT_SECONDS = 60
        queue = get_internal_queue(organization)
        enqueue_job(
            organization=organization,
            queue=queue,
            source="stale",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="stale-1",
            available_at=timezone.now() - timedelta(hours=2),
        )
        response = client.get("/healthz/worker")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_a_draining_backlog_is_healthy(self, client, organization, settings):
        """A busy queue that is completing work is healthy, however deep it is."""

        settings.QUEUE_OLDEST_JOB_ALERT_SECONDS = 60
        queue = get_internal_queue(organization)
        enqueue_job(
            organization=organization,
            queue=queue,
            source="old",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="old-1",
            available_at=timezone.now() - timedelta(hours=2),
        )
        recent = enqueue_job(
            organization=organization,
            queue=queue,
            source="recent",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="recent-1",
        ).job
        Job.objects.filter(pk=recent.pk).update(
            status=Job.Status.SUCCEEDED, finished_at=timezone.now()
        )
        assert client.get("/healthz/worker").status_code == 200


class TestSchedulerHealth:
    def test_healthy_with_no_schedules(self, client):
        assert client.get("/healthz/scheduler").status_code == 200

    def test_degraded_when_a_schedule_is_overdue(self, client, organization):
        Schedule.objects.create(
            organization=organization,
            queue=get_internal_queue(organization),
            name="Stuck",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            next_run_at=timezone.now() - timedelta(hours=1),
        )
        response = client.get("/healthz/scheduler")
        assert response.status_code == 503
        assert response.json()["overdue_schedules"] == 1

    def test_inactive_schedules_do_not_degrade_health(self, client, organization):
        Schedule.objects.create(
            organization=organization,
            queue=get_internal_queue(organization),
            name="Retired",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            active=False,
            next_run_at=timezone.now() - timedelta(days=5),
        )
        assert client.get("/healthz/scheduler").status_code == 200


class TestReaperHealth:
    def test_healthy_with_no_stranded_leases(self, client):
        assert client.get("/healthz/reaper").status_code == 200

    def test_degraded_when_a_lease_is_long_expired(self, client, organization, queue):
        enqueue_job(
            organization=organization,
            queue=queue,
            source="x",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="lease-1",
        )
        claimed = claim_jobs(queue=queue, worker_id="dead")[0]
        Job.objects.filter(pk=claimed.job.pk).update(
            lease_expires_at=timezone.now() - timedelta(hours=1)
        )
        response = client.get("/healthz/reaper")
        assert response.status_code == 503
        assert response.json()["stranded_leases"] == 1


class TestRequestIDMiddleware:
    def test_every_response_carries_a_request_id(self, client):
        assert client.get("/healthz")["X-Request-ID"]

    def test_a_well_formed_inbound_id_is_adopted(self, client):
        response = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
        assert response["X-Request-ID"] == "trace-abc-123"

    @pytest.mark.parametrize(
        "hostile",
        ["with spaces", "a" * 200, "inject\nX-Evil: 1", "semi;colon", "<script>"],
    )
    def test_hostile_inbound_ids_are_replaced(self, client, hostile):
        """Echoing arbitrary client bytes into logs is a log-injection primitive."""

        response = client.get("/healthz", headers={"X-Request-ID": hostile})
        assert response["X-Request-ID"] != hostile
        assert len(response["X-Request-ID"]) == 32

    def test_ids_differ_between_requests(self, client):
        first = client.get("/healthz")["X-Request-ID"]
        second = client.get("/healthz")["X-Request-ID"]
        assert first != second


class TestSecurityHeaders:
    def test_content_security_policy_is_applied(self, client):
        csp = client.get("/healthz")["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    def test_csp_forbids_inline_script(self):
        from django.conf import settings

        script_src = [
            directive
            for directive in settings.CONTENT_SECURITY_POLICY.split(";")
            if directive.strip().startswith("script-src")
        ][0]
        assert "unsafe-inline" not in script_src
        assert "unsafe-eval" not in script_src

    def test_permissions_policy_is_applied(self, client):
        assert "geolocation=()" in client.get("/healthz")["Permissions-Policy"]

    def test_nosniff_and_frame_options(self, client):
        response = client.get("/healthz")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["X-Frame-Options"] == "DENY"

    def test_server_timing_is_exposed(self, client):
        assert client.get("/healthz")["Server-Timing"].startswith("app;dur=")


class TestJSONLogging:
    def test_records_serialise_to_one_json_object(self):
        from ops.logging import JSONFormatter, RequestIDFilter

        record = logging.LogRecord(
            name="taskmanager.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="job enqueued",
            args=(),
            exc_info=None,
        )
        record.job_id = 42
        RequestIDFilter().filter(record)

        payload = json.loads(JSONFormatter().format(record))
        assert payload["level"] == "INFO"
        assert payload["message"] == "job enqueued"
        assert payload["job_id"] == 42
        assert "request_id" in payload

    def test_exceptions_are_captured(self):
        from ops.logging import JSONFormatter

        try:
            raise ValueError("kaboom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="t", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="failed", args=(), exc_info=sys.exc_info(),
            )
        payload = json.loads(JSONFormatter().format(record))
        assert "kaboom" in payload["exception"]

    def test_request_id_context_propagates(self):
        from ops.logging import get_request_id, reset_request_id, set_request_id

        token = set_request_id("abc123")
        try:
            assert get_request_id() == "abc123"
        finally:
            reset_request_id(token)
        assert get_request_id() == "-"


class TestErrorHandlers:
    def test_unknown_page_returns_404_without_internals(self, client):
        response = client.get("/this-page-does-not-exist/")
        assert response.status_code == 404
        body = response.content.decode()
        assert "Traceback" not in body
        assert "site-packages" not in body

    def test_api_404_uses_the_json_envelope(self, client):
        response = client.get("/api/v1/nope/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["request_id"]


class TestManagementCommands:
    def test_run_worker_once_drains(self, organization, queue, user):
        enqueue_job(
            organization=organization,
            queue=queue,
            source="cmd",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={"user_id": user.pk, "title": "Hi", "natural_key": "cmd-1"},
            idempotency_key="cmd-1",
        )
        out = StringIO()
        call_command("run_worker", "--once", stdout=out)
        assert "Drained one batch" in out.getvalue()
        assert Job.objects.filter(status=Job.Status.SUCCEEDED).count() == 1

    def test_run_worker_rejects_an_unknown_queue(self):
        with pytest.raises(CommandError, match="No queue named"):
            call_command("run_worker", "--once", "--queue", "does-not-exist")

    def test_run_scheduler_once(self, organization, queue):
        Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Due now",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
            target_handler="internal:schedule_fire",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        out = StringIO()
        call_command("run_scheduler", "--once", stdout=out)
        assert "schedules" in out.getvalue()

    def test_reap_leases_reports(self, organization, queue):
        out = StringIO()
        call_command("reap_leases", stdout=out)
        assert "Reaped" in out.getvalue()

    def test_seed_demo_refuses_in_production(self, settings):
        settings.IS_PRODUCTION = True
        with pytest.raises(CommandError, match="production"):
            call_command("seed_demo")

    def test_seed_demo_builds_a_usable_workspace(self):
        out = StringIO()
        call_command("seed_demo", "--password", "seeded-password-123", stdout=out)

        from kernel.models import Organization
        from tasks.models import Task

        organization = Organization.objects.get(slug="demo")
        assert Task.objects.filter(organization=organization).exists()
        assert Schedule.objects.filter(organization=organization).exists()
        assert "seeded-password-123" in out.getvalue()

    def test_seed_demo_is_idempotent(self):
        call_command("seed_demo", "--password", "p1", stdout=StringIO())
        call_command("seed_demo", "--password", "p2", stdout=StringIO())

        from kernel.models import Organization

        assert Organization.objects.filter(slug="demo").count() == 1

    def test_backup_and_verify_round_trip(self, tmp_path):
        """An untested backup is not a backup."""

        out = StringIO()
        call_command("backup_db", "--output-dir", str(tmp_path), "--verify", stdout=out)
        output = out.getvalue()
        assert "Backup written" in output
        assert "Restore verified" in output
        assert list(tmp_path.glob("backup-*"))

    def test_backup_prunes_old_files(self, tmp_path):
        for index in range(4):
            call_command("backup_db", "--output-dir", str(tmp_path), "--keep", "2", stdout=StringIO())
        assert len(list(tmp_path.glob("backup-*"))) <= 2

    def test_restore_requires_confirmation(self, tmp_path):
        call_command("backup_db", "--output-dir", str(tmp_path), stdout=StringIO())
        backup = next(tmp_path.glob("backup-*"))
        with pytest.raises(CommandError, match="--confirm"):
            call_command("restore_db", str(backup))

    def test_restore_rejects_a_missing_file(self):
        with pytest.raises(CommandError, match="No such backup"):
            call_command("restore_db", "/nope/missing.sqlite3", "--confirm")


class TestDeployChecks:
    def test_system_checks_are_clean(self):
        out = StringIO()
        call_command("check", stdout=out)
        assert "no issues" in out.getvalue()

    def test_models_and_migrations_are_in_sync(self):
        """A model change without a migration breaks every deploy after it."""

        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
