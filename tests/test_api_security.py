"""API authentication, authorization and tenant scoping.

The scaffold's API was a set of bare ``ModelViewSet``s over ``.objects.all()`` with no
authentication. Every test here asserts one of the properties that were missing.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from kernel.models import ApiKey, Membership, Notification
from kernel.roles import Role
from tasks.models import Label, Task

pytestmark = pytest.mark.django_db

LIST_ENDPOINTS = [
    "/api/v1/organizations/",
    "/api/v1/members/",
    "/api/v1/projects/",
    "/api/v1/lists/",
    "/api/v1/columns/",
    "/api/v1/labels/",
    "/api/v1/tasks/",
    "/api/v1/comments/",
    "/api/v1/reminders/",
    "/api/v1/recurrences/",
    "/api/v1/queues/",
    "/api/v1/jobs/",
    "/api/v1/dead-letters/",
    "/api/v1/schedules/",
    "/api/v1/schedule-occurrences/",
    "/api/v1/webhooks/",
    "/api/v1/webhook-deliveries/",
    "/api/v1/activity/",
    "/api/v1/notifications/",
    "/api/v1/saved-views/",
]


class TestAnonymousAccess:
    @pytest.mark.parametrize("url", LIST_ENDPOINTS)
    def test_anonymous_is_rejected(self, client, url):
        response = APIClient().get(url)
        assert response.status_code in (401, 403), f"{url} leaked to anonymous callers"

    @pytest.mark.parametrize("url", LIST_ENDPOINTS)
    def test_anonymous_cannot_write(self, url):
        response = APIClient().post(url, {}, format="json")
        assert response.status_code in (401, 403, 405)

    def test_api_root_is_public_but_leaks_nothing(self):
        response = APIClient().get("/api/v1/")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"version", "documentation", "authentication", "organization_header"}

    def test_openapi_schema_is_public(self):
        response = APIClient().get("/api/v1/openapi.json")
        assert response.status_code == 200
        assert response.json()["openapi"].startswith("3.")


class TestListEndpointsWork:
    """Regression guard: `CursorPagination`'s default ordering made every list 500."""

    @pytest.mark.parametrize("url", LIST_ENDPOINTS)
    def test_authenticated_list_returns_200(self, api_client, url, organization):
        response = api_client.get(url)
        assert response.status_code == 200, f"{url} returned {response.status_code}: {response.content[:300]}"

    def test_task_list_is_paginated(self, api_client, organization, task_list, user):
        for index in range(5):
            Task.objects.create(
                organization=organization,
                task_list=task_list,
                creator=user,
                title=f"Task {index}",
                position=index * 1000,
            )
        response = api_client.get("/api/v1/tasks/?page_size=2")
        body = response.json()
        assert body["count"] == 5
        assert len(body["results"]) == 2
        assert body["next"]

    def test_page_size_is_capped(self, api_client, organization):
        response = api_client.get("/api/v1/tasks/?page_size=100000")
        assert response.status_code == 200
        assert response.json()["page_size"] <= 200


class TestCrossTenantIsolation:
    def test_list_excludes_other_tenants(self, api_client, task, other_task):
        titles = [row["title"] for row in api_client.get("/api/v1/tasks/").json()["results"]]
        assert task.title in titles
        assert other_task.title not in titles

    def test_detail_of_foreign_task_is_404_not_403(self, api_client, other_task):
        # 403 would confirm the id exists — an enumeration oracle. 404 is correct.
        assert api_client.get(f"/api/v1/tasks/{other_task.pk}/").status_code == 404

    def test_cannot_update_foreign_task(self, api_client, other_task):
        response = api_client.patch(f"/api/v1/tasks/{other_task.pk}/", {"title": "pwned"}, format="json")
        assert response.status_code == 404
        other_task.refresh_from_db()
        assert other_task.title == "Rival secret plan"

    def test_cannot_delete_foreign_task(self, api_client, other_task):
        assert api_client.delete(f"/api/v1/tasks/{other_task.pk}/").status_code == 404
        assert Task.objects.filter(pk=other_task.pk).exists()

    def test_cannot_create_task_in_foreign_list(self, api_client, other_task_list):
        response = api_client.post(
            "/api/v1/tasks/", {"title": "Injected", "task_list": other_task_list.pk}, format="json"
        )
        assert response.status_code == 400
        assert "task_list" in response.json()["error"].get("fields", {})

    def test_organization_field_is_not_client_writable(self, api_client, task_list, other_organization):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Mine", "task_list": task_list.pk, "organization": other_organization.pk},
            format="json",
        )
        assert response.status_code == 201
        created = Task.objects.get(pk=response.json()["id"])
        assert created.organization_id != other_organization.pk

    def test_cannot_assign_foreign_label(self, api_client, task_list, other_organization):
        foreign_label = Label.objects.create(organization=other_organization, name="Foreign")
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Mine", "task_list": task_list.pk, "label_ids": [foreign_label.pk]},
            format="json",
        )
        assert response.status_code == 400

    def test_cannot_assign_non_member(self, api_client, task_list, other_user):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Mine", "task_list": task_list.pk, "assignee": other_user.pk},
            format="json",
        )
        assert response.status_code == 400

    def test_notifications_are_scoped_to_the_caller(self, api_client, organization, user, member):
        Notification.objects.create(
            organization=organization, user=user, title="Mine", natural_key="a"
        )
        Notification.objects.create(
            organization=organization, user=member.user, title="Theirs", natural_key="b"
        )
        titles = [row["title"] for row in api_client.get("/api/v1/notifications/").json()["results"]]
        assert titles == ["Mine"]


class TestRolePermissions:
    def test_viewer_cannot_create_tasks(self, api_client_viewer, task_list):
        response = api_client_viewer.post(
            "/api/v1/tasks/", {"title": "Nope", "task_list": task_list.pk}, format="json"
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    def test_viewer_can_read_tasks(self, api_client_viewer, task):
        assert api_client_viewer.get("/api/v1/tasks/").status_code == 200

    def test_viewer_cannot_edit_tasks(self, api_client_viewer, task):
        assert (
            api_client_viewer.patch(f"/api/v1/tasks/{task.pk}/", {"title": "x"}, format="json").status_code
            == 403
        )

    def test_member_cannot_manage_api_keys(self, member):
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert api.get("/api/v1/api-keys/").status_code == 403

    def test_member_cannot_read_audit_log(self, member):
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert api.get("/api/v1/audit-log/").status_code == 403

    def test_owner_can_read_audit_log(self, api_client):
        assert api_client.get("/api/v1/audit-log/").status_code == 200

    def test_member_cannot_invite(self, member):
        api = APIClient()
        api.force_authenticate(user=member.user)
        response = api.post(
            "/api/v1/invitations/", {"email": "x@example.com", "role": "member"}, format="json"
        )
        assert response.status_code == 403


class TestApiKeyAuthentication:
    def _client(self, token):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Api-Key {token}")
        return api

    def test_valid_key_authenticates(self, api_key, task):
        _, token = api_key
        response = self._client(token).get("/api/v1/tasks/")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_invalid_key_is_rejected(self):
        assert self._client("tmk_deadbeef_nope").get("/api/v1/tasks/").status_code == 401

    def test_malformed_header_is_rejected(self):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION="Api-Key")
        assert api.get("/api/v1/tasks/").status_code in (401, 403)

    def test_revoked_key_is_rejected(self, api_key):
        key, token = api_key
        key.revoke()
        assert self._client(token).get("/api/v1/tasks/").status_code == 401

    def test_expired_key_is_rejected(self, api_key):
        from datetime import timedelta

        from django.utils import timezone

        key, token = api_key
        ApiKey.objects.filter(pk=key.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        assert self._client(token).get("/api/v1/tasks/").status_code == 401

    def test_key_is_pinned_to_its_workspace(self, api_key, other_task):
        _, token = api_key
        titles = [r["title"] for r in self._client(token).get("/api/v1/tasks/").json()["results"]]
        assert other_task.title not in titles

    def test_header_cannot_widen_a_key_to_another_workspace(self, api_key, other_organization, other_task):
        _, token = api_key
        api = self._client(token)
        response = api.get("/api/v1/tasks/", headers={"X-Organization": other_organization.slug})
        assert other_task.title not in [r["title"] for r in response.json()["results"]]

    def test_key_role_limits_writes(self, organization, user, task_list):
        key, token = ApiKey.issue(
            organization=organization, name="Read only", role=Role.VIEWER, created_by=user
        )
        response = self._client(token).post(
            "/api/v1/tasks/", {"title": "Nope", "task_list": task_list.pk}, format="json"
        )
        assert response.status_code == 403

    def test_plaintext_is_never_stored(self, api_key):
        key, token = api_key
        assert token not in key.token_hash
        # The separator must be one base64url never emits, or the split lands inside the
        # secret and verification silently fails for ~1 key in 3.
        prefix, separator, secret = token.partition(ApiKey.TOKEN_SEPARATOR)
        assert separator == "."
        assert prefix == key.prefix
        assert key.token_hash == ApiKey.hash_secret(secret)

    def test_secret_containing_underscores_still_verifies(self, organization, user):
        """Regression: the secret is base64url and legitimately contains ``_`` and ``-``."""

        from unittest.mock import patch

        with patch("kernel.models.secrets.token_urlsafe", return_value="aa_bb-cc_dd"):
            _, token = ApiKey.issue(
                organization=organization, name="Underscored", role=Role.MEMBER, created_by=user
            )
        response = self._client(token).get("/api/v1/tasks/")
        assert response.status_code == 200

    def test_key_creation_returns_token_once(self, api_client, organization):
        response = api_client.post(
            "/api/v1/api-keys/", {"name": "CI", "role": "member"}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["token"].startswith("tmk_")
        # A later read must not contain it.
        listing = api_client.get("/api/v1/api-keys/").json()["results"]
        assert all("token" not in row for row in listing)

    def test_cannot_mint_a_key_above_your_own_role(self, member, organization):
        Membership.objects.filter(pk=member.pk).update(role=Role.ADMIN)
        api = APIClient()
        api.force_authenticate(user=member.user)
        response = api.post("/api/v1/api-keys/", {"name": "Escalate", "role": "owner"}, format="json")
        assert response.status_code == 403


class TestApiKeyPrincipal:
    """An API key acts for an organization, not a person.

    ``request.user`` is therefore an ``ApiKeyUser`` with no primary key. Assigning it to any
    model ForeignKey raises ``ValueError`` — which is exactly what turned every
    key-authenticated write into a 500 before these tests existed.
    """

    def _client(self, token):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Api-Key {token}")
        return api

    def test_a_key_can_create_a_task(self, api_key, task_list):
        _, token = api_key
        response = self._client(token).post(
            "/api/v1/tasks/", {"title": "From CI", "task_list": task_list.pk}, format="json"
        )
        assert response.status_code == 201

    def test_the_write_is_attributed_to_the_keys_creator(self, api_key, task_list, user):
        """The honest answer for an audit trail: a person authorised this automation."""

        key, token = api_key
        response = self._client(token).post(
            "/api/v1/tasks/", {"title": "Attributed", "task_list": task_list.pk}, format="json"
        )
        assert Task.objects.get(pk=response.json()["id"]).creator_id == user.pk

    def test_a_key_can_comment(self, api_key, task):
        _, token = api_key
        response = self._client(token).post(
            f"/api/v1/tasks/{task.pk}/comments/", {"body": "Automated note"}, format="json"
        )
        assert response.status_code == 201

    def test_a_key_can_toggle_a_task(self, api_key, task):
        _, token = api_key
        assert self._client(token).post(f"/api/v1/tasks/{task.pk}/toggle/").status_code == 200

    def test_a_key_can_create_a_saved_view(self, api_key):
        _, token = api_key
        response = self._client(token).post(
            "/api/v1/saved-views/", {"name": "Automated", "filters": {}}, format="json"
        )
        assert response.status_code == 201

    def test_a_key_sees_only_shared_saved_views(self, api_key, organization, user):
        from kernel.models import SavedView

        SavedView.objects.create(organization=organization, owner=user, name="Private")
        SavedView.objects.create(
            organization=organization, owner=user, name="Shared", is_shared=True
        )
        _, token = api_key
        names = [row["name"] for row in self._client(token).get("/api/v1/saved-views/").json()["results"]]
        assert names == ["Shared"]

    def test_personal_endpoints_refuse_a_key(self, api_key):
        """"My notifications" has no meaning for a workspace credential; answering with
        the key creator's inbox would be a quiet privacy leak."""

        _, token = api_key
        api = self._client(token)
        assert api.get("/api/v1/notifications/").status_code == 403
        assert api.get("/api/v1/notification-preferences/").status_code == 403

    def test_a_key_never_masquerades_as_its_creator_in_the_principal(self, api_key, user):
        _, token = api_key
        response = self._client(token).get("/api/v1/stats/")
        assert response.status_code == 200
        # The principal is the key, not the person.
        assert response.wsgi_request.user.username.startswith("apikey:")


class TestErrorEnvelope:
    def test_validation_error_shape(self, api_client, task_list):
        response = api_client.post("/api/v1/tasks/", {"task_list": task_list.pk}, format="json")
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid"
        assert "title" in error["fields"]
        assert error["request_id"]

    def test_not_found_shape(self, api_client):
        error = api_client.get("/api/v1/tasks/999999/").json()["error"]
        assert error["code"] == "not_found"
        assert error["request_id"]

    def test_no_stack_trace_or_sql_leaks(self, api_client):
        body = api_client.get("/api/v1/tasks/999999/").content.decode()
        for marker in ("Traceback", "SELECT ", "django.db", "site-packages"):
            assert marker not in body
