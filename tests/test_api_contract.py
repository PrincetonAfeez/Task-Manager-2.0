"""API contract: CRUD semantics, filtering, actions, concurrency, schema."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kernel.models import Notification, SavedView
from queues.models import DeadLetter, Job
from tasks.models import Column, Task, TaskList

pytestmark = pytest.mark.django_db


class TestTaskCrud:
    def test_create_returns_201_and_persists(self, api_client, organization, task_list):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Via API", "task_list": task_list.pk, "priority": 3},
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["title"] == "Via API"
        assert body["organization"] == organization.pk
        assert body["version"] == 1

    def test_creation_records_activity_and_a_position(self, api_client, task_list):
        from kernel.models import Activity

        response = api_client.post(
            "/api/v1/tasks/", {"title": "Positioned", "task_list": task_list.pk}, format="json"
        )
        task = Task.objects.get(pk=response.json()["id"])
        assert task.position > 0
        assert Activity.objects.filter(verb="task.created", target_id=str(task.pk)).exists()

    def test_a_due_date_arms_a_reminder(self, api_client, task_list):
        response = api_client.post(
            "/api/v1/tasks/",
            {
                "title": "Due soon",
                "task_list": task_list.pk,
                "due_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        from tasks.models import Reminder

        assert Reminder.objects.filter(task_id=response.json()["id"]).exists()

    def test_a_blank_title_is_rejected(self, api_client, task_list):
        response = api_client.post(
            "/api/v1/tasks/", {"title": "   ", "task_list": task_list.pk}, format="json"
        )
        assert response.status_code == 400
        assert "title" in response.json()["error"]["fields"]

    def test_a_due_time_without_a_date_is_rejected(self, api_client, task_list):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Floating", "task_list": task_list.pk, "due_time": "17:00"},
            format="json",
        )
        assert response.status_code == 400

    def test_a_column_from_another_list_is_rejected(self, api_client, organization, task_list):
        other = TaskList.objects.create(organization=organization, name="Other", position=5)
        foreign_column = other.ensure_default_columns()[0]
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Mismatched", "task_list": task_list.pk, "column": foreign_column.pk},
            format="json",
        )
        assert response.status_code == 400
        assert "column" in response.json()["error"]["fields"]

    def test_patch_updates_and_bumps_the_version(self, api_client, task):
        response = api_client.patch(
            f"/api/v1/tasks/{task.pk}/", {"title": "Renamed"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        assert response.json()["version"] == task.version + 1

    def test_a_stale_expected_version_is_409(self, api_client, task):
        response = api_client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Clobber", "expected_version": task.version + 5},
            format="json",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        task.refresh_from_db()
        assert task.title != "Clobber"

    def test_the_current_expected_version_succeeds(self, api_client, task):
        response = api_client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Fine", "expected_version": task.version},
            format="json",
        )
        assert response.status_code == 200

    def test_delete_archives_rather_than_destroying(self, api_client, task):
        """Comments, attachments and activity reference the task; a hard delete orphans them."""

        assert api_client.delete(f"/api/v1/tasks/{task.pk}/").status_code == 204
        task.refresh_from_db()
        assert task.archived_at is not None

    def test_read_only_fields_cannot_be_set(self, api_client, task_list, other_user):
        response = api_client.post(
            "/api/v1/tasks/",
            {"title": "Spoofed", "task_list": task_list.pk, "creator": other_user.pk, "version": 99},
            format="json",
        )
        created = Task.objects.get(pk=response.json()["id"])
        assert created.creator_id != other_user.pk
        assert created.version == 1


class TestTaskFiltering:
    @pytest.fixture
    def corpus(self, organization, task_list, user, member, label):
        today = timezone.localdate()
        open_task = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Open widget"
        )
        open_task.labels.add(label)
        overdue = Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Overdue thing",
            due_date=today - timedelta(days=2),
        )
        assigned = Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=member.user,
            title="Assigned thing",
            priority=4,
        )
        done = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Finished thing"
        )
        done.complete(actor=user)
        return {"open": open_task, "overdue": overdue, "assigned": assigned, "done": done}

    def test_archived_are_excluded_by_default(self, api_client, corpus, user):
        from tasks.services import archive_task

        archive_task(task=corpus["open"], actor=user)
        titles = [r["title"] for r in api_client.get("/api/v1/tasks/").json()["results"]]
        assert "Open widget" not in titles

    def test_include_archived(self, api_client, corpus, user):
        from tasks.services import archive_task

        archive_task(task=corpus["open"], actor=user)
        titles = [
            r["title"]
            for r in api_client.get("/api/v1/tasks/?include_archived=true").json()["results"]
        ]
        assert "Open widget" in titles

    def test_filter_by_status(self, api_client, corpus):
        titles = [
            r["title"] for r in api_client.get("/api/v1/tasks/?status=completed").json()["results"]
        ]
        assert titles == ["Finished thing"]

    def test_filter_by_assignee(self, api_client, corpus, member):
        titles = [
            r["title"]
            for r in api_client.get(f"/api/v1/tasks/?assignee={member.user.pk}").json()["results"]
        ]
        assert titles == ["Assigned thing"]

    def test_filter_by_priority(self, api_client, corpus):
        titles = [r["title"] for r in api_client.get("/api/v1/tasks/?priority=4").json()["results"]]
        assert titles == ["Assigned thing"]

    def test_filter_by_label(self, api_client, corpus, label):
        titles = [
            r["title"] for r in api_client.get(f"/api/v1/tasks/?label={label.pk}").json()["results"]
        ]
        assert titles == ["Open widget"]

    def test_filter_overdue(self, api_client, corpus):
        titles = [
            r["title"] for r in api_client.get("/api/v1/tasks/?overdue=true").json()["results"]
        ]
        assert titles == ["Overdue thing"]

    def test_search(self, api_client, corpus):
        titles = [r["title"] for r in api_client.get("/api/v1/tasks/?search=widget").json()["results"]]
        assert titles == ["Open widget"]

    def test_ordering(self, api_client, corpus):
        titles = [r["title"] for r in api_client.get("/api/v1/tasks/?ordering=title").json()["results"]]
        assert titles == sorted(titles)

    def test_a_non_numeric_filter_is_ignored_not_an_error(self, api_client, corpus):
        assert api_client.get("/api/v1/tasks/?assignee=DROP+TABLE").status_code == 200


class TestTaskActions:
    def test_toggle_completes_and_reopens(self, api_client, task):
        assert api_client.post(f"/api/v1/tasks/{task.pk}/toggle/").json()["status"] == "completed"
        assert api_client.post(f"/api/v1/tasks/{task.pk}/toggle/").json()["status"] == "open"

    def test_toggle_with_a_stale_version_is_409(self, api_client, task):
        response = api_client.post(
            f"/api/v1/tasks/{task.pk}/toggle/", {"expected_version": 999}, format="json"
        )
        assert response.status_code == 409

    def test_a_blocked_task_cannot_be_completed(self, api_client, organization, task_list, user, task):
        from tasks.services import add_dependency

        blocker = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Blocker"
        )
        add_dependency(blocked_task=task, blocking_task=blocker)
        response = api_client.post(f"/api/v1/tasks/{task.pk}/toggle/")
        assert response.status_code == 409
        assert "Blocked by" in response.json()["error"]["message"]

    def test_move_changes_the_column(self, api_client, task, columns):
        response = api_client.post(
            f"/api/v1/tasks/{task.pk}/move/", {"column": columns[1].pk}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["column"] == columns[1].pk

    def test_move_to_a_foreign_column_is_404(self, api_client, task, other_organization):
        foreign_list = TaskList.objects.filter(organization=other_organization).first()
        foreign_column = foreign_list.ensure_default_columns()[0]
        response = api_client.post(
            f"/api/v1/tasks/{task.pk}/move/", {"column": foreign_column.pk}, format="json"
        )
        assert response.status_code == 404

    def test_comments_can_be_listed_and_created(self, api_client, task):
        assert api_client.get(f"/api/v1/tasks/{task.pk}/comments/").json() == []
        response = api_client.post(
            f"/api/v1/tasks/{task.pk}/comments/", {"body": "Looks good"}, format="json"
        )
        assert response.status_code == 201
        assert len(api_client.get(f"/api/v1/tasks/{task.pk}/comments/").json()) == 1

    def test_a_viewer_cannot_comment(self, api_client_viewer, task):
        response = api_client_viewer.post(
            f"/api/v1/tasks/{task.pk}/comments/", {"body": "no"}, format="json"
        )
        assert response.status_code == 403


class TestListsAndLabels:
    def test_creating_a_list_gives_it_columns(self, api_client, organization):
        response = api_client.post("/api/v1/lists/", {"name": "Roadmap"}, format="json")
        assert response.status_code == 201
        assert Column.objects.filter(task_list_id=response.json()["id"]).exists()

    def test_a_member_cannot_create_a_list(self, member):
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert api.post("/api/v1/lists/", {"name": "Nope"}, format="json").status_code == 403

    def test_a_member_can_create_a_label(self, member, organization):
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert api.post("/api/v1/labels/", {"name": "Backend"}, format="json").status_code == 201

    def test_duplicate_label_names_are_a_conflict_not_a_500(self, api_client, label):
        response = api_client.post("/api/v1/labels/", {"name": label.name}, format="json")
        assert response.status_code in (400, 409)


class TestQueueAndJobEndpoints:
    def test_jobs_are_read_only(self, api_client, job):
        assert api_client.post("/api/v1/jobs/", {}, format="json").status_code == 405
        assert api_client.delete(f"/api/v1/jobs/{job.pk}/").status_code == 405

    def test_jobs_use_cursor_pagination(self, api_client, job):
        body = api_client.get("/api/v1/jobs/").json()
        assert "results" in body
        assert "next" in body

    def test_filter_jobs_by_status(self, api_client, job):
        assert len(api_client.get("/api/v1/jobs/?status=available").json()["results"]) == 1
        assert len(api_client.get("/api/v1/jobs/?status=dead").json()["results"]) == 0

    def test_cancel_a_pending_job(self, api_client, job):
        assert api_client.post(f"/api/v1/jobs/{job.pk}/cancel/").json()["status"] == "canceled"

    def test_cancelling_twice_is_a_conflict(self, api_client, job):
        api_client.post(f"/api/v1/jobs/{job.pk}/cancel/")
        assert api_client.post(f"/api/v1/jobs/{job.pk}/cancel/").status_code == 409

    def test_a_viewer_cannot_cancel(self, api_client_viewer, job):
        assert api_client_viewer.post(f"/api/v1/jobs/{job.pk}/cancel/").status_code == 403

    def test_pause_and_resume_a_queue(self, api_client, queue):
        assert api_client.post(f"/api/v1/queues/{queue.pk}/pause/").json()["paused"] is True
        assert api_client.post(f"/api/v1/queues/{queue.pk}/resume/").json()["paused"] is False

    def test_redrive_a_dead_letter(self, api_client, organization, job, user):
        dead_letter = DeadLetter.objects.create(
            organization=organization, job=job, reason="exhausted"
        )
        Job.objects.filter(pk=job.pk).update(status=Job.Status.DEAD)
        response = api_client.post(f"/api/v1/dead-letters/{dead_letter.pk}/redrive/")
        assert response.status_code == 200
        assert response.json()["status"] == "available"


class TestScheduleEndpoints:
    def _create(self, api_client, queue, **overrides):
        payload = {
            "name": "Nightly",
            "queue": queue.pk,
            "expression_type": "cron",
            "expression": "0 2 * * *",
            "timezone": "UTC",
            "target_handler": "internal:schedule_fire",
        }
        payload.update(overrides)
        return api_client.post("/api/v1/schedules/", payload, format="json")

    def test_create_sets_next_run_at(self, api_client, queue):
        response = self._create(api_client, queue)
        assert response.status_code == 201
        assert response.json()["next_run_at"] is not None

    def test_an_invalid_cron_expression_is_rejected(self, api_client, queue):
        response = self._create(api_client, queue, expression="not a cron")
        assert response.status_code == 400
        assert "expression" in response.json()["error"]["fields"]

    def test_an_unknown_timezone_is_rejected(self, api_client, queue):
        response = self._create(api_client, queue, timezone="Mars/Olympus")
        assert response.status_code == 400

    def test_a_foreign_queue_is_rejected(self, api_client, other_organization):
        from queues.services import get_internal_queue

        foreign = get_internal_queue(other_organization)
        response = self._create(api_client, foreign)
        assert response.status_code == 400

    def test_preview_returns_upcoming_runs(self, api_client, queue):
        schedule_id = self._create(api_client, queue).json()["id"]
        runs = api_client.get(f"/api/v1/schedules/{schedule_id}/preview/?count=3").json()["runs"]
        assert len(runs) == 3
        assert runs == sorted(runs)

    def test_the_preview_count_is_bounded(self, api_client, queue):
        schedule_id = self._create(api_client, queue).json()["id"]
        runs = api_client.get(f"/api/v1/schedules/{schedule_id}/preview/?count=9999").json()["runs"]
        assert len(runs) <= 20

    def test_a_member_cannot_create_a_schedule(self, member, queue):
        api = APIClient()
        api.force_authenticate(user=member.user)
        assert self._create(api, queue).status_code == 403


class TestNotificationEndpoints:
    def test_mark_one_read(self, api_client, organization, user):
        notification = Notification.objects.create(
            organization=organization,
            user=user,
            title="Ping",
            natural_key="api-1",
            status=Notification.Status.DELIVERED,
        )
        assert api_client.post(f"/api/v1/notifications/{notification.pk}/read/").json()["status"] == "read"

    def test_mark_all_read(self, api_client, organization, user):
        for index in range(3):
            Notification.objects.create(
                organization=organization,
                user=user,
                title=f"N{index}",
                natural_key=f"api-{index}",
                status=Notification.Status.DELIVERED,
            )
        assert api_client.post("/api/v1/notifications/read-all/").json()["marked_read"] == 3

    def test_unread_filter(self, api_client, organization, user):
        read = Notification.objects.create(
            organization=organization, user=user, title="Read", natural_key="r"
        )
        read.mark_read()
        Notification.objects.create(
            organization=organization,
            user=user,
            title="Unread",
            natural_key="u",
            status=Notification.Status.DELIVERED,
        )
        titles = [r["title"] for r in api_client.get("/api/v1/notifications/?unread=true").json()["results"]]
        assert titles == ["Unread"]


class TestPreferencesEndpoint:
    def test_read_and_update(self, api_client):
        assert api_client.get("/api/v1/notification-preferences/").json()["email_enabled"] is True
        response = api_client.patch(
            "/api/v1/notification-preferences/update/", {"email_enabled": False}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["email_enabled"] is False

    def test_half_set_quiet_hours_are_rejected(self, api_client):
        response = api_client.patch(
            "/api/v1/notification-preferences/update/",
            {"quiet_hours_start": "22:00"},
            format="json",
        )
        assert response.status_code == 400


class TestSavedViewEndpoints:
    def test_create_and_list(self, api_client, organization, user):
        response = api_client.post(
            "/api/v1/saved-views/", {"name": "Mine", "filters": {"status": "open"}}, format="json"
        )
        assert response.status_code == 201
        assert SavedView.objects.get(pk=response.json()["id"]).owner_id == user.pk

    def test_another_members_private_view_is_hidden(self, api_client, organization, member):
        SavedView.objects.create(
            organization=organization, owner=member.user, name="Theirs", is_shared=False
        )
        assert api_client.get("/api/v1/saved-views/").json()["count"] == 0

    def test_a_shared_view_is_visible(self, api_client, organization, member):
        SavedView.objects.create(
            organization=organization, owner=member.user, name="Shared", is_shared=True
        )
        assert api_client.get("/api/v1/saved-views/").json()["count"] == 1


class TestMetaEndpoints:
    def test_stats(self, api_client, organization, task, job):
        body = api_client.get("/api/v1/stats/").json()
        assert body["organization"]["slug"] == organization.slug
        assert body["tasks"]["open"] == 1
        assert body["jobs"]["waiting"] == 1

    def test_organizations_current(self, api_client, organization):
        assert api_client.get("/api/v1/organizations/current/").json()["slug"] == organization.slug

    def test_organizations_are_read_only(self, api_client):
        assert api_client.post("/api/v1/organizations/", {"name": "x"}, format="json").status_code == 405


class TestOpenApiSchema:
    def test_the_document_is_well_formed(self, api_client):
        schema = api_client.get("/api/v1/openapi.json").json()
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"]
        assert schema["paths"]

    def test_it_documents_both_auth_schemes(self, api_client):
        schemes = api_client.get("/api/v1/openapi.json").json()["components"]["securitySchemes"]
        assert "ApiKeyAuth" in schemes
        assert "SessionAuth" in schemes

    def test_it_documents_the_error_envelope(self, api_client):
        schema = api_client.get("/api/v1/openapi.json").json()
        assert "Error" in schema["components"]["schemas"]

    def test_the_paths_match_the_live_router(self, api_client):
        """Generated from the router, so it cannot drift from the implementation."""

        schema = api_client.get("/api/v1/openapi.json").json()
        for route in ("/api/v1/tasks/", "/api/v1/jobs/", "/api/v1/schedules/"):
            assert route in schema["paths"]

    def test_documented_routes_actually_respond(self, api_client):
        schema = api_client.get("/api/v1/openapi.json").json()
        collections = [
            path
            for path, methods in schema["paths"].items()
            if "get" in methods and "{" not in path
        ]
        for path in collections[:12]:
            assert api_client.get(path).status_code in (200, 403), f"{path} is documented but broken"
