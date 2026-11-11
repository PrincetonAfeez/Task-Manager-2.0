"""Public /status aggregates health without authentication."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


class TestStatusPage:
    def test_html_is_public(self, client):
        response = client.get("/status")
        assert response.status_code == 200
        assert b"Task Manager status" in response.content

    def test_json_when_accepted(self, client):
        response = client.get("/status", HTTP_ACCEPT="application/json")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in {"operational", "degraded"}
        assert "components" in body
        assert "contractual" in body["note"].lower() or "SLA" in body["note"]
