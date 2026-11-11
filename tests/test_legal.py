"""Terms and privacy pages.

The audit framework requires terms and a privacy policy to be referenced where accounts are
created. A link to a page that does not exist is worse than no link at all, so these assert
the links resolve and the content is honest about what the product does.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


class TestLegalPages:
    def test_terms_are_public(self, client):
        response = client.get("/terms/")
        assert response.status_code == 200
        assert b"Terms of Service" in response.content

    def test_privacy_is_public(self, client):
        response = client.get("/privacy/")
        assert response.status_code == 200
        assert b"Privacy Policy" in response.content

    def test_signup_links_to_both(self, client):
        content = client.get("/signup/").content.decode()
        assert 'href="/terms/"' in content
        assert 'href="/privacy/"' in content

    def test_the_footer_links_to_both(self, client_owner):
        content = client_owner.get("/").content.decode()
        assert "/terms/" in content
        assert "/privacy/" in content

    def test_the_privacy_policy_states_the_retention_windows(self, client):
        content = client.get("/privacy/").content.decode()
        assert "30 days" in content
        assert "2 years" in content

    def test_the_privacy_policy_declares_the_single_region_limitation(self, client):
        """A limitation a customer cannot discover is a limitation you are hiding."""

        content = client.get("/privacy/").content.decode().lower()
        assert "single region" in content
        assert "residency" in content

    def test_the_privacy_policy_disclaims_ai_and_advertising_use(self, client):
        content = client.get("/privacy/").content.decode().lower()
        assert "model provider" in content
        assert "advertising" in content

    def test_the_terms_disclaim_any_uptime_commitment(self, client):
        """The README says best-effort; the terms must not imply otherwise."""

        content = client.get("/terms/").content.decode().lower()
        assert "no contractual uptime commitment" in content

    def test_the_pages_render_without_a_session(self, client):
        for url in ("/terms/", "/privacy/"):
            assert client.get(url).status_code == 200