"""HTML and HTMX views: authentication, tenancy, permissions, and rendering."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from kernel.models import Activity, ApiKey, AuditLog, Invitation, Membership, Notification, Organization
from kernel.roles import Role
from tasks.models import Comment, Label, Subtask, Task, TaskList

pytestmark = pytest.mark.django_db

# Every route that must never serve an anonymous caller.
PROTECTED_GET_ROUTES = [
    "/",
    "/tasks/",
    "/board/",
    "/lists/",
    "/automation/",
    "/analytics/",
    "/notifications/",
    "/settings/workspace/",
    "/settings/members/",
    "/settings/notifications/",
    "/settings/api-keys/",
    "/settings/audit/",
]


class TestAuthenticationRequired:
    @pytest.mark.parametrize("url", PROTECTED_GET_ROUTES)
    def test_anonymous_is_redirected_to_login(self, client, url):
        """Regression: the dashboard used to silently sign anonymous callers into a demo org."""

        response = client.get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_anonymous_cannot_create_a_task(self, client, task_list):
        response = client.post("/quick-add/create/", {"quick_add": "Sneaky task"})
        assert response.status_code == 302
        assert Task.objects.count() == 0

    def test_anonymous_cannot_toggle_a_task(self, client, task):
        client.post(f"/tasks/{task.pk}/toggle/")
        task.refresh_from_db()
        assert task.status == Task.Status.OPEN

    def test_no_demo_workspace_is_auto_created(self, client):
        client.get("/")
        assert not Organization.objects.filter(slug="demo").exists()

    def test_htmx_gets_a_redirect_header_not_a_login_page(self, client):
        response = client.post("/quick-add/preview/", {"quick_add": "x"}, headers={"HX-Request": "true"})
        assert response.status_code == 401
        assert response["HX-Redirect"] == "/accounts/login/"

    def test_health_endpoints_stay_public(self, client):
        for url in ("/healthz", "/readyz", "/version"):
            assert client.get(url).status_code in (200, 503)


class TestDashboard:
    def test_renders_for_a_member(self, client_owner, organization, task):
        response = client_owner.get("/")
        assert response.status_code == 200
        assert organization.name.encode() in response.content
        assert task.title.encode() in response.content

    def test_shows_only_this_tenants_tasks(self, client_owner, task, other_task):
        content = client_owner.get("/").content
        assert task.title.encode() in content
        assert other_task.title.encode() not in content

    def test_counts_reflect_reality(self, client_owner, organization, task_list, user):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Overdue one",
            due_date=timezone.localdate() - timedelta(days=1),
        )
        counts = client_owner.get("/").context["counts"]
        assert counts["overdue"] == 1

    def test_a_user_without_a_workspace_is_sent_to_onboarding(self, client, other_user):
        client.force_login(other_user)
        response = client.get("/")
        assert response.status_code == 302
        assert "/workspaces/new/" in response["Location"]


class TestSignup:
    def test_creates_an_account_and_a_workspace(self, client):
        response = client.post(
            "/signup/",
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "password1": "a-long-enough-password-9",
                "password2": "a-long-enough-password-9",
                "workspace_name": "Newco",
            },
        )
        assert response.status_code == 302
        organization = Organization.objects.get(name="Newco")
        assert organization.memberships.get().role == Role.OWNER
        # A new workspace must be immediately usable, not an empty shell.
        assert TaskList.objects.filter(organization=organization).exists()

    def test_a_short_password_is_rejected(self, client):
        client.post(
            "/signup/",
            {
                "username": "weak",
                "email": "weak@example.com",
                "password1": "short1",
                "password2": "short1",
            },
        )
        from django.contrib.auth import get_user_model

        assert not get_user_model().objects.filter(username="weak").exists()

    def test_a_duplicate_email_is_rejected(self, client, user):
        client.post(
            "/signup/",
            {
                "username": "clone",
                "email": user.email,
                "password1": "a-long-enough-password-9",
                "password2": "a-long-enough-password-9",
            },
        )
        from django.contrib.auth import get_user_model

        assert not get_user_model().objects.filter(username="clone").exists()

    def test_an_authenticated_user_is_redirected_away(self, client_owner):
        assert client_owner.get("/signup/").status_code == 302


class TestQuickAdd:
    def test_creates_a_task(self, client_owner, organization, task_list):
        response = client_owner.post("/quick-add/create/", {"quick_add": "Ship it tomorrow 5pm #ops !3"})
        assert response.status_code == 302
        task = Task.objects.get(organization=organization)
        assert task.title == "Ship it"
        assert task.priority == 3

    def test_empty_input_is_rejected(self, client_owner):
        assert client_owner.post("/quick-add/create/", {"quick_add": "   "}).status_code == 400

    def test_preview_is_read_only(self, client_owner):
        response = client_owner.post("/quick-add/preview/", {"quick_add": "Preview me tomorrow"})
        assert response.status_code == 200
        assert b"Preview me" in response.content
        assert Task.objects.count() == 0

    def test_a_viewer_cannot_quick_add(self, client_viewer):
        assert client_viewer.post("/quick-add/create/", {"quick_add": "Nope"}).status_code == 403
        assert Task.objects.count() == 0

    def test_a_viewer_can_still_preview(self, client_viewer):
        assert client_viewer.post("/quick-add/preview/", {"quick_add": "x"}).status_code == 200


class TestTaskToggle:
    def test_toggling_completes_the_task(self, client_owner, task):
        client_owner.post(f"/tasks/{task.pk}/toggle/", {"expected_version": task.version})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_exactly_one_activity_row_per_toggle(self, client_owner, task):
        """Regression: the row template fired both an HTMX post and a native form submit."""

        client_owner.post(f"/tasks/{task.pk}/toggle/", {"expected_version": task.version})
        assert Activity.objects.filter(verb="task.completed", target_id=str(task.pk)).count() == 1

    def test_htmx_gets_the_row_fragment_not_a_full_page(self, client_owner, task):
        response = client_owner.post(
            f"/tasks/{task.pk}/toggle/",
            {"expected_version": task.version},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"<html" not in response.content
        assert f'id="task-{task.pk}"'.encode() in response.content

    def test_a_stale_version_returns_409(self, client_owner, task):
        response = client_owner.post(
            f"/tasks/{task.pk}/toggle/",
            {"expected_version": task.version + 3},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 409
        assert response["HX-Retarget"] == "#flash"

    def test_cannot_toggle_another_tenants_task(self, client_owner, other_task):
        assert client_owner.post(f"/tasks/{other_task.pk}/toggle/").status_code == 404
        other_task.refresh_from_db()
        assert other_task.status == Task.Status.OPEN

    def test_a_viewer_cannot_toggle(self, client_viewer, task):
        assert client_viewer.post(f"/tasks/{task.pk}/toggle/").status_code == 403

    def test_get_is_rejected(self, client_owner, task):
        assert client_owner.get(f"/tasks/{task.pk}/toggle/").status_code == 405


class TestTaskCrud:
    def test_detail_renders(self, client_owner, task):
        response = client_owner.get(f"/tasks/{task.pk}/")
        assert response.status_code == 200
        assert task.title.encode() in response.content

    def test_another_tenants_task_is_404(self, client_owner, other_task):
        assert client_owner.get(f"/tasks/{other_task.pk}/").status_code == 404

    def test_create_via_the_form(self, client_owner, organization, task_list):
        response = client_owner.post(
            "/tasks/new/",
            {
                "title": "Formal task",
                "notes": "",
                "task_list": task_list.pk,
                "priority": 2,
            },
        )
        assert response.status_code == 302
        assert Task.objects.filter(title="Formal task").exists()

    def test_edit_updates_and_bumps_version(self, client_owner, task, task_list):
        client_owner.post(
            f"/tasks/{task.pk}/edit/",
            {
                "title": "Renamed",
                "notes": "",
                "task_list": task_list.pk,
                "priority": 2,
                "expected_version": task.version,
            },
        )
        task.refresh_from_db()
        assert task.title == "Renamed"

    def test_archive(self, client_owner, task):
        client_owner.post(f"/tasks/{task.pk}/archive/")
        task.refresh_from_db()
        assert task.archived_at is not None


class TestTaskListView:
    def test_filters_by_status(self, client_owner, organization, task_list, user):
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Open one"
        )
        done = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Closed one"
        )
        done.complete(actor=user)

        content = client_owner.get("/tasks/?status=open").content
        assert b"Open one" in content
        assert b"Closed one" not in content

    def test_search(self, client_owner, organization, task_list, user):
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Findable widget"
        )
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Unrelated"
        )
        content = client_owner.get("/tasks/?q=widget").content
        assert b"Findable widget" in content
        assert b"Unrelated" not in content

    def test_an_arbitrary_sort_key_is_ignored(self, client_owner):
        """User input must never reach order_by()."""

        response = client_owner.get("/tasks/?sort=organization__owner__password")
        assert response.status_code == 200
        assert response.context["sort"] == "priority"

    def test_htmx_returns_only_the_table(self, client_owner, task):
        response = client_owner.get("/tasks/", headers={"HX-Request": "true"})
        assert b"<html" not in response.content
        assert b"task-results" in response.content

    def test_pagination(self, client_owner, organization, task_list, user):
        for index in range(30):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        page = client_owner.get("/tasks/?page=2").context["page"]
        assert page.number == 2


class TestBoard:
    def test_renders_columns(self, client_owner, task_list, columns, task):
        response = client_owner.get("/board/")
        assert response.status_code == 200
        for column in columns:
            assert column.name.encode() in response.content

    def test_move_to_a_done_column_completes(self, client_owner, task, columns):
        done = next(column for column in columns if column.is_done)
        response = client_owner.post(
            f"/tasks/{task.pk}/move/",
            {"column": done.pk, "expected_version": task.version},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_move_to_a_foreign_column_is_404(self, client_owner, task, other_organization):
        from tasks.models import TaskList as TL

        foreign_list = TL.objects.filter(organization=other_organization).first()
        foreign_column = foreign_list.ensure_default_columns()[0]
        response = client_owner.post(f"/tasks/{task.pk}/move/", {"column": foreign_column.pk})
        assert response.status_code == 404

    def test_a_stale_version_blocks_the_move(self, client_owner, task, columns):
        response = client_owner.post(
            f"/tasks/{task.pk}/move/",
            {"column": columns[1].pk, "expected_version": task.version + 9},
        )
        assert response.status_code == 409


class TestComments:
    def test_member_can_comment(self, client_member, task):
        response = client_member.post(f"/tasks/{task.pk}/comments/", {"body": "Looks good"})
        assert response.status_code == 302
        assert Comment.objects.filter(task=task, body="Looks good").exists()

    def test_viewer_cannot_comment(self, client_viewer, task):
        assert client_viewer.post(f"/tasks/{task.pk}/comments/", {"body": "no"}).status_code == 403

    def test_empty_comment_is_rejected(self, client_owner, task):
        assert client_owner.post(f"/tasks/{task.pk}/comments/", {"body": "  "}).status_code == 400

    def test_comment_body_is_escaped_in_the_fragment(self, client_owner, task):
        """Stored XSS check: the payload must come back escaped, not executable."""

        payload = "<script>alert('xss')</script>"
        response = client_owner.post(
            f"/tasks/{task.pk}/comments/", {"body": payload}, headers={"HX-Request": "true"}
        )
        assert b"<script>alert" not in response.content
        assert b"&lt;script&gt;" in response.content


class TestSubtasks:
    def test_create_and_toggle(self, client_owner, task):
        client_owner.post(f"/tasks/{task.pk}/subtasks/", {"title": "Step one"})
        subtask = Subtask.objects.get(task=task)

        client_owner.post(f"/tasks/{task.pk}/subtasks/{subtask.pk}/toggle/")
        subtask.refresh_from_db()
        assert subtask.is_done

    def test_a_subtask_from_another_task_is_404(self, client_owner, task, organization, task_list, user):
        other = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Other"
        )
        subtask = Subtask.objects.create(task=other, title="Theirs")
        assert client_owner.post(f"/tasks/{task.pk}/subtasks/{subtask.pk}/toggle/").status_code == 404


class TestMemberManagement:
    def test_owner_can_invite(self, client_owner, organization):
        response = client_owner.post(
            "/settings/members/", {"email": "new@example.com", "role": "member"}
        )
        assert response.status_code == 302
        assert Invitation.objects.filter(email="new@example.com").exists()

    def test_member_cannot_reach_the_page(self, client_member):
        assert client_member.get("/settings/members/").status_code == 403

    def test_owner_can_change_a_role(self, client_owner, member):
        client_owner.post(f"/settings/members/{member.pk}/role/", {"role": Role.ADMIN})
        member.refresh_from_db()
        assert member.role == Role.ADMIN

    def test_the_last_owner_cannot_be_demoted(self, client_owner, membership):
        client_owner.post(f"/settings/members/{membership.pk}/role/", {"role": Role.MEMBER})
        membership.refresh_from_db()
        assert membership.role == Role.OWNER

    def test_the_last_owner_cannot_be_removed(self, client_owner, membership):
        client_owner.post(f"/settings/members/{membership.pk}/remove/")
        assert Membership.objects.filter(pk=membership.pk).exists()

    def test_owner_can_remove_a_member(self, client_owner, member):
        client_owner.post(f"/settings/members/{member.pk}/remove/")
        assert not Membership.objects.filter(pk=member.pk).exists()

    def test_cannot_manage_a_member_of_another_workspace(self, client_owner, other_organization, other_user):
        foreign = Membership.objects.get(organization=other_organization, user=other_user)
        assert client_owner.post(f"/settings/members/{foreign.pk}/remove/").status_code == 404


class TestInvitations:
    def test_accepting_creates_a_membership(self, client, organization, other_user, user):
        invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        client.force_login(other_user)
        response = client.post(f"/invitations/{token}/")
        assert response.status_code == 302
        assert Membership.objects.filter(organization=organization, user=other_user).exists()

    def test_a_token_is_single_use(self, client, organization, other_user, member_user, user):
        invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        client.force_login(other_user)
        client.post(f"/invitations/{token}/")

        client.force_login(member_user)
        client.post(f"/invitations/{token}/")
        assert not Membership.objects.filter(organization=organization, user=member_user).exists()

    def test_an_expired_token_is_refused(self, client, organization, other_user, user):
        invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        Invitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        client.force_login(other_user)
        client.post(f"/invitations/{token}/")
        assert not Membership.objects.filter(organization=organization, user=other_user).exists()

    def test_a_forged_token_is_refused(self, client, organization, other_user):
        client.force_login(other_user)
        client.post("/invitations/not-a-real-token/")
        assert not Membership.objects.filter(organization=organization, user=other_user).exists()

    def test_the_raw_token_is_never_stored(self, organization, user):
        invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        assert invitation.token_hash != token
        assert invitation.token_hash == Invitation.hash_token(token)


class TestApiKeyPage:
    def test_owner_can_mint_a_key_and_sees_it_once(self, client_owner, organization):
        response = client_owner.post(
            "/settings/api-keys/", {"name": "CI pipeline", "role": Role.MEMBER}
        )
        assert response.status_code == 200
        assert response.context["new_token"].startswith("tmk_")
        # A subsequent page load must not show it again.
        assert client_owner.get("/settings/api-keys/").context["new_token"] is None

    def test_member_cannot_reach_the_page(self, client_member):
        assert client_member.get("/settings/api-keys/").status_code == 403

    def test_revoke(self, client_owner, organization, user):
        key, _ = ApiKey.issue(
            organization=organization, name="Old", role=Role.MEMBER, created_by=user
        )
        client_owner.post(f"/settings/api-keys/{key.pk}/revoke/")
        key.refresh_from_db()
        assert key.revoked_at is not None

    def test_cannot_revoke_another_tenants_key(self, client_owner, other_organization, other_user):
        key, _ = ApiKey.issue(
            organization=other_organization, name="Theirs", role=Role.MEMBER, created_by=other_user
        )
        assert client_owner.post(f"/settings/api-keys/{key.pk}/revoke/").status_code == 404
        key.refresh_from_db()
        assert key.revoked_at is None


class TestWorkspaceSettings:
    def test_owner_can_rename(self, client_owner, organization):
        client_owner.post("/settings/workspace/", {"name": "Renamed Co", "timezone": "UTC"})
        organization.refresh_from_db()
        assert organization.name == "Renamed Co"

    def test_deletion_requires_the_exact_name(self, client_owner, organization):
        client_owner.post("/settings/workspace/delete/", {"confirm": "wrong"})
        organization.refresh_from_db()
        assert organization.deleted_at is None

    def test_deletion_with_the_exact_name_soft_deletes(self, client_owner, organization):
        client_owner.post("/settings/workspace/delete/", {"confirm": organization.name})
        organization.refresh_from_db()
        assert organization.deleted_at is not None
        assert organization.purge_after is not None

    def test_an_admin_cannot_delete_the_workspace(self, client, admin_membership, organization):
        client.force_login(admin_membership.user)
        client.post("/settings/workspace/delete/", {"confirm": organization.name})
        organization.refresh_from_db()
        assert organization.deleted_at is None


class TestWorkspaceSwitching:
    def test_switching_changes_the_active_workspace(self, client, user, organization):
        second = Organization.objects.create(
            name="Second", slug="second-ws", owner=user, timezone="UTC"
        )
        Membership.objects.create(organization=second, user=user, role=Role.OWNER)
        client.force_login(user)

        client.post(f"/workspaces/{second.pk}/switch/")
        from kernel.tenancy import SESSION_ORG_KEY

        assert client.session[SESSION_ORG_KEY] == second.pk

    def test_cannot_switch_into_a_workspace_you_do_not_belong_to(
        self, client_owner, other_organization
    ):
        assert client_owner.post(f"/workspaces/{other_organization.pk}/switch/").status_code == 404


class TestNotifications:
    def test_list_shows_only_your_own(self, client_owner, organization, user, member):
        Notification.objects.create(
            organization=organization, user=user, title="Yours", natural_key="a"
        )
        Notification.objects.create(
            organization=organization, user=member.user, title="Theirs", natural_key="b"
        )
        content = client_owner.get("/notifications/").content
        assert b"Yours" in content
        assert b"Theirs" not in content

    def test_mark_read(self, client_owner, organization, user):
        notification = Notification.objects.create(
            organization=organization,
            user=user,
            title="Ping",
            natural_key="c",
            status=Notification.Status.DELIVERED,
        )
        client_owner.post(f"/notifications/{notification.pk}/read/")
        notification.refresh_from_db()
        assert notification.status == Notification.Status.READ

    def test_cannot_mark_someone_elses_notification_read(self, client_owner, organization, member):
        notification = Notification.objects.create(
            organization=organization, user=member.user, title="Theirs", natural_key="d"
        )
        assert client_owner.post(f"/notifications/{notification.pk}/read/").status_code == 404


class TestAuditPage:
    def test_owner_can_view(self, client_owner):
        assert client_owner.get("/settings/audit/").status_code == 200

    def test_member_cannot_view(self, client_member):
        assert client_member.get("/settings/audit/").status_code == 403

    def test_entries_are_scoped_to_the_tenant(self, client_owner, organization, other_organization):
        from kernel.services import record_audit

        record_audit(action="mine.event", organization=organization)
        record_audit(action="theirs.event", organization=other_organization)
        content = client_owner.get("/settings/audit/").content
        assert b"mine.event" in content
        assert b"theirs.event" not in content


class TestAutomationPage:
    def test_renders_for_a_member(self, client_member, organization, job):
        assert client_member.get("/automation/").status_code == 200

    def test_only_admins_can_pause_a_queue(self, client_member, queue):
        assert client_member.post(f"/automation/queues/{queue.pk}/pause/").status_code == 403

    def test_owner_can_pause_and_resume(self, client_owner, queue):
        client_owner.post(f"/automation/queues/{queue.pk}/pause/")
        queue.refresh_from_db()
        assert queue.paused is True

        client_owner.post(f"/automation/queues/{queue.pk}/pause/")
        queue.refresh_from_db()
        assert queue.paused is False

    def test_pausing_is_audited(self, client_owner, queue, organization):
        client_owner.post(f"/automation/queues/{queue.pk}/pause/")
        assert AuditLog.objects.filter(action="queue.paused", organization=organization).exists()


class TestAnalytics:
    def test_renders(self, client_owner, task):
        response = client_owner.get("/analytics/")
        assert response.status_code == 200
        assert "report" in response.context

    def test_the_window_is_clamped(self, client_owner):
        assert client_owner.get("/analytics/?days=99999").context["days"] == 180
        assert client_owner.get("/analytics/?days=-5").context["days"] == 7
        assert client_owner.get("/analytics/?days=abc").context["days"] == 30


class TestSavedViews:
    def test_create_and_apply(self, client_owner, organization):
        client_owner.post("/views/save/", {"name": "Overdue work", "due": "overdue"})
        from kernel.models import SavedView

        view = SavedView.objects.get(organization=organization, name="Overdue work")
        assert view.filters["due"] == "overdue"
        assert client_owner.get(f"/tasks/?view={view.pk}").status_code == 200

    def test_a_member_cannot_share_without_permission(self, client_viewer, organization):
        client_viewer.post("/views/save/", {"name": "Mine", "is_shared": "on"})
        from kernel.models import SavedView

        view = SavedView.objects.filter(organization=organization, name="Mine").first()
        if view is not None:
            assert view.is_shared is False


class TestLabelsAndLists:
    def test_owner_can_create_a_list(self, client_owner, organization):
        client_owner.post("/lists/", {"name": "Roadmap", "color": "#2563eb", "position": 3})
        task_list = TaskList.objects.get(organization=organization, name="Roadmap")
        # A list with no columns renders an empty board and swallows tasks.
        assert task_list.columns.exists()

    def test_member_cannot_manage_lists(self, client_member):
        assert client_member.get("/lists/").status_code == 403

    def test_label_creation(self, client_owner, organization):
        client_owner.post("/labels/new/", {"name": "Backend", "color": "#7c3aed"})
        assert Label.objects.filter(organization=organization, name="Backend").exists()
