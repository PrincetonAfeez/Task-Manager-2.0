"""REST surface for outbound webhook endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from integrations.models import WebhookEndpoint

pytestmark = pytest.mark.django_db


class TestWebhookEndpointAPI:
    def test_owner_can_create_and_sees_secret_once(self, api_client, organization, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        with patch("integrations.delivery.socket.getaddrinfo") as resolve:
            resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            response = api_client.post(
                "/api/v1/webhooks/",
                {
                    "name": "Hooks",
                    "url": "https://hooks.example.com/tm",
                    "events": ["task.created"],
                },
                format="json",
            )
        assert response.status_code == 201, response.content
        assert response.data["secret"].startswith("whsec_")
        endpoint_id = response.data["id"]

        listed = api_client.get("/api/v1/webhooks/")
        assert listed.status_code == 200
        assert "secret" not in listed.data["results"][0]
        assert WebhookEndpoint.objects.filter(organization=organization, pk=endpoint_id).exists()

    def test_member_cannot_manage_webhooks(self, member, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        api = APIClient()
        api.force_authenticate(user=member.user)
        response = api.post(
            "/api/v1/webhooks/",
            {"name": "Nope", "url": "https://hooks.example.com/x"},
            format="json",
        )
        assert response.status_code == 403

    def test_foreign_tenant_is_404(self, api_client, other_organization, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        foreign = WebhookEndpoint.objects.create(
            organization=other_organization,
            name="Other",
            url="https://hooks.example.com/other",
            secret=WebhookEndpoint.generate_secret(),
        )
        response = api_client.get(f"/api/v1/webhooks/{foreign.pk}/")
        assert response.status_code == 404

    def test_api_key_can_toggle(self, organization, api_key, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        endpoint = WebhookEndpoint.objects.create(
            organization=organization,
            name="Hooks",
            url="https://hooks.example.com/tm",
            secret=WebhookEndpoint.generate_secret(),
            active=True,
        )
        _key, token = api_key
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Api-Key {token}")
        response = client.post(f"/api/v1/webhooks/{endpoint.pk}/toggle/")
        assert response.status_code == 200, response.content
        endpoint.refresh_from_db()
        assert endpoint.active is False

    def test_deliveries_list_is_tenant_scoped(self, api_client, organization, other_organization, settings):
        settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
        ours = WebhookEndpoint.objects.create(
            organization=organization,
            name="Ours",
            url="https://hooks.example.com/ours",
            secret=WebhookEndpoint.generate_secret(),
        )
        theirs = WebhookEndpoint.objects.create(
            organization=other_organization,
            name="Theirs",
            url="https://hooks.example.com/theirs",
            secret=WebhookEndpoint.generate_secret(),
        )
        from integrations.models import WebhookDelivery

        WebhookDelivery.objects.create(
            organization=organization, endpoint=ours, event="ping", event_id="evt_1"
        )
        WebhookDelivery.objects.create(
            organization=other_organization, endpoint=theirs, event="ping", event_id="evt_2"
        )
        response = api_client.get("/api/v1/webhook-deliveries/")
        assert response.status_code == 200
        ids = {row["event_id"] for row in response.data["results"]}
        assert ids == {"evt_1"}
