"""Webhook delivery: SSRF protection, signing, retry classification, circuit breaking.

The SSRF tests are the important ones. A customer supplies the URL, so without validation
"send a webhook to http://169.254.169.254/" turns the worker into a proxy into our own
infrastructure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from integrations.delivery import (
    deliver_webhook,
    dispatch_event,
    validate_webhook_url,
)
from integrations.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from queues.models import Job
from queues.registry import PermanentError, RetryableError

pytestmark = pytest.mark.django_db


@pytest.fixture
def endpoint(organization, user, settings):
    settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
    return WebhookEndpoint.objects.create(
        organization=organization,
        name="Customer receiver",
        url="https://hooks.example.com/tm",
        secret=WebhookEndpoint.generate_secret(),
        created_by=user,
    )


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/hook",
            "file:///etc/passwd",
            "gopher://example.com",
            "javascript:alert(1)",
            "",
            "not-a-url",
        ],
    )
    def test_non_http_schemes_are_rejected(self, url, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        with pytest.raises(ValueError):
            validate_webhook_url(url)

    def test_embedded_credentials_are_rejected(self, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        with pytest.raises(ValueError, match="credentials"):
            validate_webhook_url("https://user:pass@example.com/hook")

    @pytest.mark.parametrize(
        "host,resolved",
        [
            ("localhost", "127.0.0.1"),
            ("internal.corp", "10.0.0.5"),
            ("db.internal", "192.168.1.20"),
            ("metadata.google.internal", "169.254.169.254"),  # cloud metadata
            ("carrier.example", "172.16.0.1"),
        ],
    )
    def test_private_and_link_local_targets_are_rejected(self, settings, host, resolved):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with patch("integrations.delivery.socket.getaddrinfo") as resolve:
            resolve.return_value = [(2, 1, 6, "", (resolved, 443))]
            with pytest.raises(ValueError, match="private or reserved"):
                validate_webhook_url(f"https://{host}/hook")

    def test_a_public_address_is_accepted(self, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with patch("integrations.delivery.socket.getaddrinfo") as resolve:
            resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            assert validate_webhook_url("https://example.com/hook")

    def test_dns_rebinding_via_a_second_record_is_caught(self, settings):
        """One public and one private A record must fail: any private answer is fatal."""

        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with patch("integrations.delivery.socket.getaddrinfo") as resolve:
            resolve.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("127.0.0.1", 443)),
            ]
            with pytest.raises(ValueError):
                validate_webhook_url("https://sneaky.example.com/hook")

    def test_unresolvable_hosts_are_rejected(self, settings):
        import socket

        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with patch("integrations.delivery.socket.getaddrinfo", side_effect=socket.gaierror):
            with pytest.raises(ValueError, match="resolve"):
                validate_webhook_url("https://nope.invalid/hook")

    def test_plain_http_is_rejected_in_production(self, settings):
        settings.IS_PRODUCTION = True
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("http://example.com/hook")


class TestSigning:
    def test_signature_is_verifiable(self, endpoint):
        body = json.dumps({"event": "task.created"})
        header = endpoint.sign("1700000000", body)

        assert header.startswith("t=1700000000,v1=")
        signature = header.split("v1=")[1]
        expected = hmac.new(
            endpoint.secret.encode(), f"1700000000.{body}".encode(), hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(signature, expected)

    def test_the_timestamp_is_inside_the_signed_payload(self, endpoint):
        """Otherwise an attacker replays an old body under a fresh timestamp."""

        body = json.dumps({"a": 1})
        assert endpoint.sign("1000", body) != endpoint.sign("2000", body)

    def test_different_bodies_produce_different_signatures(self, endpoint):
        assert endpoint.sign("1000", '{"a":1}') != endpoint.sign("1000", '{"a":2}')

    def test_secrets_are_unique_per_endpoint(self, organization):
        first = WebhookEndpoint.objects.create(
            organization=organization, name="A", url="https://a.example.com/h"
        )
        second = WebhookEndpoint.objects.create(
            organization=organization, name="B", url="https://b.example.com/h"
        )
        assert first.secret != second.secret
        assert first.secret.startswith("whsec_")


class TestSubscription:
    def test_an_empty_event_list_means_everything(self, endpoint):
        assert endpoint.subscribes_to("task.created")
        assert endpoint.subscribes_to("anything.at.all")

    def test_an_explicit_list_filters(self, endpoint):
        endpoint.events = [WebhookEvent.TASK_COMPLETED]
        assert endpoint.subscribes_to("task.completed")
        assert not endpoint.subscribes_to("task.created")

    def test_unknown_events_fail_validation(self, endpoint):
        endpoint.events = ["task.teleported"]
        with pytest.raises(ValidationError):
            endpoint.clean()


class TestDispatch:
    def test_creating_a_task_queues_a_delivery(
        self, organization, task_list, user, endpoint, django_capture_on_commit_callbacks
    ):
        """Events are emitted on commit, so the callbacks have to be run explicitly here.

        That deferral is the point: an event announcing a task that was then rolled back is
        a lie the receiver has no way to detect.
        """

        from tasks.services import create_task_from_quick_add

        with django_capture_on_commit_callbacks(execute=True):
            create_task_from_quick_add(
                organization=organization, task_list=task_list, creator=user, raw="Notify me"
            )

        assert Job.objects.filter(handler="http_delivery", source=f"webhook:{endpoint.pk}").exists()

    def test_a_rolled_back_task_emits_nothing(self, organization, task_list, user, endpoint):
        """The deferral must actually prevent the event, not merely delay it."""

        from django.db import transaction

        from tasks.services import create_task_from_quick_add

        class Rollback(Exception):
            pass

        with pytest.raises(Rollback):
            with transaction.atomic():
                create_task_from_quick_add(
                    organization=organization, task_list=task_list, creator=user, raw="Doomed"
                )
                raise Rollback

        assert not Job.objects.filter(handler="http_delivery").exists()

    def test_a_disabled_endpoint_receives_nothing(self, organization, endpoint):
        endpoint.active = False
        endpoint.save(update_fields=["active"])
        assert dispatch_event(organization=organization, event="task.created", data={}) == 0

    def test_an_auto_disabled_endpoint_receives_nothing(self, organization, endpoint):
        endpoint.disabled_at = timezone.now()
        endpoint.save(update_fields=["disabled_at"])
        assert dispatch_event(organization=organization, event="task.created", data={}) == 0

    def test_endpoints_from_other_tenants_are_not_notified(
        self, organization, other_organization, endpoint
    ):
        WebhookEndpoint.objects.create(
            organization=other_organization, name="Theirs", url="https://theirs.example.com/h"
        )
        dispatch_event(organization=organization, event="task.created", data={})
        assert not Job.objects.filter(organization=other_organization).exists()

    def test_one_dispatch_produces_exactly_one_job(self, organization, endpoint):
        # Two dispatches produce two distinct events, but one dispatch is exactly one job.
        assert dispatch_event(organization=organization, event="task.created", data={}) == 1
        assert Job.objects.filter(source=f"webhook:{endpoint.pk}").count() == 1

    def test_the_flag_can_switch_webhooks_off(self, organization, endpoint):
        from kernel.services import set_feature_flag

        set_feature_flag(key="webhooks", enabled=False, organization=organization)
        assert dispatch_event(organization=organization, event="task.created", data={}) == 0


class TestDelivery:
    def _job(self, organization, endpoint, event="task.created"):
        from queues.services import enqueue_job, get_webhook_queue

        return enqueue_job(
            organization=organization,
            queue=get_webhook_queue(organization),
            source=f"webhook:{endpoint.pk}",
            handler_type=Job.HandlerType.HTTP_DELIVERY,
            handler="http_delivery",
            payload={
                "endpoint_id": endpoint.pk,
                "event": event,
                "body": {"id": "evt_test", "event": event, "data": {}},
            },
            idempotency_key=f"webhook:{endpoint.pk}:evt_test",
        ).job

    def test_a_2xx_response_records_success(self, organization, endpoint):
        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", return_value=(200, "ok")):
            result = deliver_webhook(job)

        assert result["status_code"] == 200
        delivery = WebhookDelivery.objects.get(pk=result["delivery_id"])
        assert delivery.status == WebhookDelivery.Status.SUCCEEDED
        endpoint.refresh_from_db()
        assert endpoint.consecutive_failures == 0
        assert endpoint.last_success_at is not None

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
    def test_retryable_status_codes_raise_retryable(self, organization, endpoint, status_code):
        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", return_value=(status_code, "")):
            with pytest.raises(RetryableError):
                deliver_webhook(job)

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 422])
    def test_client_errors_are_permanent(self, organization, endpoint, status_code):
        """Retrying a 404 five times just wastes both sides' capacity."""

        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", return_value=(status_code, "")):
            with pytest.raises(PermanentError):
                deliver_webhook(job)

    def test_a_transport_failure_is_retryable(self, organization, endpoint):
        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", side_effect=OSError("connection reset")):
            with pytest.raises(RetryableError):
                deliver_webhook(job)

    def test_a_deleted_endpoint_is_skipped_not_failed(self, organization, endpoint):
        job = self._job(organization, endpoint)
        endpoint_id = endpoint.pk
        endpoint.delete()
        assert deliver_webhook(job)["skipped"] == "endpoint_deleted"

    def test_the_response_body_is_truncated(self, organization, endpoint):
        """A hostile receiver could otherwise write megabytes into our database."""

        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", return_value=(200, "x" * 100_000)):
            result = deliver_webhook(job)
        delivery = WebhookDelivery.objects.get(pk=result["delivery_id"])
        assert len(delivery.response_body) <= 2000

    def test_delivery_headers_carry_the_signature_and_event_id(self, organization, endpoint):
        job = self._job(organization, endpoint)
        with patch("integrations.delivery._post", return_value=(200, "")) as post:
            deliver_webhook(job)

        _url, _body, headers = post.call_args[0]
        assert headers["X-TaskManager-Event"] == "task.created"
        assert headers["X-TaskManager-Event-Id"] == "evt_test"
        assert headers["X-TaskManager-Signature"].startswith("t=")

    def test_redirects_are_never_followed(self, organization, endpoint):
        """A receiver could otherwise bounce us to an internal address post-validation."""

        import inspect

        from integrations.delivery import _post

        source = inspect.getsource(_post)
        assert "allow_redirects=False" in source


class TestCircuitBreaker:
    def test_repeated_failures_disable_the_endpoint(self, organization, endpoint):
        for _ in range(WebhookEndpoint.FAILURE_THRESHOLD):
            endpoint.refresh_from_db()
            endpoint.record_failure(500)

        endpoint.refresh_from_db()
        assert endpoint.disabled_at is not None
        assert not endpoint.is_deliverable

    def test_a_success_resets_the_counter(self, organization, endpoint):
        endpoint.record_failure(500)
        endpoint.refresh_from_db()
        assert endpoint.consecutive_failures == 1

        endpoint.record_success(200)
        endpoint.refresh_from_db()
        assert endpoint.consecutive_failures == 0
        assert endpoint.disabled_at is None


class TestWebhookViews:
    def test_owner_can_create_an_endpoint_and_sees_the_secret_once(
        self, client_owner, organization, settings
    ):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        response = client_owner.post(
            "/settings/webhooks/",
            {"name": "Receiver", "url": "https://hooks.example.com/a", "active": "on"},
        )
        assert response.status_code == 200
        assert response.context["new_secret"].startswith("whsec_")
        assert client_owner.get("/settings/webhooks/").context["new_secret"] is None

    def test_a_member_cannot_manage_webhooks(self, client_member):
        assert client_member.get("/settings/webhooks/").status_code == 403

    def test_an_ssrf_url_is_rejected_by_the_form(self, client_owner, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = False
        with patch("integrations.delivery.socket.getaddrinfo") as resolve:
            resolve.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
            client_owner.post(
                "/settings/webhooks/", {"name": "Evil", "url": "https://evil.example.com/h"}
            )
        assert not WebhookEndpoint.objects.filter(name="Evil").exists()

    def test_cannot_view_another_tenants_endpoint(
        self, client_owner, other_organization
    ):
        foreign = WebhookEndpoint.objects.create(
            organization=other_organization, name="Theirs", url="https://theirs.example.com/h"
        )
        assert client_owner.get(f"/settings/webhooks/{foreign.pk}/").status_code == 404

    def test_toggle_clears_the_circuit_breaker(self, client_owner, endpoint):
        endpoint.active = False
        endpoint.disabled_at = timezone.now()
        endpoint.consecutive_failures = 25
        endpoint.save()

        client_owner.post(f"/settings/webhooks/{endpoint.pk}/toggle/")
        endpoint.refresh_from_db()
        assert endpoint.active
        assert endpoint.disabled_at is None
        assert endpoint.consecutive_failures == 0

    def test_test_delivery_queues_a_job(self, client_owner, endpoint):
        client_owner.post(f"/settings/webhooks/{endpoint.pk}/test/")
        assert Job.objects.filter(handler="http_delivery").exists()

