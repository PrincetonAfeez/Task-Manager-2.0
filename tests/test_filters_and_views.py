"""Filter semantics and the remaining web view paths."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from kernel.models import Organization, SavedView
from kernel.roles import Role
from tasks.models import Attachment, Column, Label, ProjectArea, Task, TaskList
from web.filters import (
    ALLOWED_SORTS,
    apply_filters,
    apply_sort,
    deserialize_filters,
    serialize_filters,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus(organization, task_list, user, member, label):
    today = timezone.localdate()
    tasks = {}
    tasks["plain"] = Task.objects.create(
        organization=organization, task_list=task_list, creator=user, title="Plain widget"
    )
    tasks["plain"].labels.add(label)
    tasks["overdue"] = Task.objects.create(
        organization=organization,
        task_list=task_list,
        creator=user,
        title="Overdue item",
        due_date=today - timedelta(days=3),
    )
    tasks["today"] = Task.objects.create(
        organization=organization,
        task_list=task_list,
        creator=user,
        title="Due today item",
        due_date=today,
    )
    tasks["soon"] = Task.objects.create(
        organization=organization,
        task_list=task_list,
        creator=user,
        title="Due soon item",
        due_date=today + timedelta(days=3),
    )
    tasks["later"] = Task.objects.create(
        organization=organization,
        task_list=task_list,
        creator=user,
        title="Far future item",
        due_date=today + timedelta(days=60),
    )
    tasks["assigned"] = Task.objects.create(
        organization=organization,
        task_list=task_list,
        creator=user,
        assignee=member.user,
        title="Assigned item",
        priority=4,
    )
    tasks["done"] = Task.objects.create(
        organization=organization, task_list=task_list, creator=user, title="Finished item"
    )
    tasks["done"].complete(actor=user)
    return tasks


def _titles(queryset):
    return sorted(queryset.values_list("title", flat=True))


class TestApplyFilters:
    def _base(self, organization):
        return Task.objects.filter(organization=organization)

    def test_no_filters_excludes_archived(self, organization, corpus, user):
        from tasks.services import archive_task

        archive_task(task=corpus["plain"], actor=user)
        assert "Plain widget" not in _titles(apply_filters(self._base(organization), {}))

    def test_an_explicit_status_overrides_the_archive_exclusion(self, organization, corpus, user):
        from tasks.services import archive_task

        archive_task(task=corpus["plain"], actor=user)
        result = apply_filters(self._base(organization), {"status": Task.Status.ARCHIVED})
        assert "Plain widget" in _titles(result)

    def test_search_matches_title_and_notes(self, organization, corpus):
        corpus["plain"].notes = "mentions kubernetes"
        corpus["plain"].save(update_fields=["notes"])

        assert _titles(apply_filters(self._base(organization), {"q": "widget"})) == ["Plain widget"]
        assert _titles(apply_filters(self._base(organization), {"q": "kubernetes"})) == ["Plain widget"]

    def test_search_is_case_insensitive(self, organization, corpus):
        assert _titles(apply_filters(self._base(organization), {"q": "WIDGET"})) == ["Plain widget"]

    def test_a_whitespace_only_search_is_ignored(self, organization, corpus):
        assert len(apply_filters(self._base(organization), {"q": "   "})) == 7

    def test_filter_by_priority(self, organization, corpus):
        assert _titles(apply_filters(self._base(organization), {"priority": 4})) == ["Assigned item"]

    def test_filter_by_assignee(self, organization, corpus, member):
        result = apply_filters(self._base(organization), {"assignee": member.user})
        assert _titles(result) == ["Assigned item"]

    def test_filter_by_label(self, organization, corpus, label):
        assert _titles(apply_filters(self._base(organization), {"label": label})) == ["Plain widget"]

    def test_filter_by_task_list(self, organization, corpus, task_list):
        other = TaskList.objects.create(organization=organization, name="Other", position=5)
        assert apply_filters(self._base(organization), {"task_list": other}).count() == 0
        assert apply_filters(self._base(organization), {"task_list": task_list}).count() == 7

    def test_due_overdue(self, organization, corpus):
        assert _titles(apply_filters(self._base(organization), {"due": "overdue"})) == ["Overdue item"]

    def test_due_today(self, organization, corpus):
        assert _titles(apply_filters(self._base(organization), {"due": "today"})) == ["Due today item"]

    def test_due_this_week(self, organization, corpus):
        result = _titles(apply_filters(self._base(organization), {"due": "week"}))
        assert "Due today item" in result
        assert "Due soon item" in result
        assert "Far future item" not in result
        assert "Overdue item" not in result

    def test_due_none(self, organization, corpus):
        result = _titles(apply_filters(self._base(organization), {"due": "none"}))
        assert "Plain widget" in result
        assert "Overdue item" not in result

    def test_an_unknown_due_value_is_ignored(self, organization, corpus):
        assert apply_filters(self._base(organization), {"due": "yesterday-ish"}).count() == 7

    def test_unknown_keys_are_ignored(self, organization, corpus):
        """Saved views persist filters across releases; a removed filter must not break them."""

        assert apply_filters(self._base(organization), {"colour": "blue", "q": "widget"}).count() == 1

    def test_filters_combine(self, organization, corpus, member):
        result = apply_filters(
            self._base(organization), {"assignee": member.user, "priority": 4, "status": "open"}
        )
        assert _titles(result) == ["Assigned item"]

    def test_results_are_deduplicated(self, organization, corpus, label):
        """A label join can otherwise multiply rows."""

        second = Label.objects.create(organization=organization, name="Second")
        corpus["plain"].labels.add(second)
        assert apply_filters(self._base(organization), {"q": "widget"}).count() == 1


class TestApplySort:
    def test_each_allowed_key_produces_an_ordering(self, organization, corpus):
        for key in ALLOWED_SORTS:
            assert apply_sort(Task.objects.filter(organization=organization), key).query.order_by

    def test_an_unknown_key_falls_back_to_the_default(self, organization, corpus):
        default = list(apply_sort(Task.objects.filter(organization=organization), None))
        hostile = list(
            apply_sort(Task.objects.filter(organization=organization), "organization__owner__password")
        )
        assert [t.pk for t in hostile] == [t.pk for t in default]

    def test_sorting_by_title(self, organization, corpus):
        titles = list(
            apply_sort(Task.objects.filter(organization=organization), "title").values_list(
                "title", flat=True
            )
        )
        assert titles == sorted(titles)

    def test_sorting_by_due_puts_nulls_last(self, organization, corpus):
        due_dates = list(
            apply_sort(Task.objects.filter(organization=organization), "due").values_list(
                "due_date", flat=True
            )
        )
        first_null = next((i for i, d in enumerate(due_dates) if d is None), len(due_dates))
        assert all(d is None for d in due_dates[first_null:])


class TestFilterSerialisation:
    def test_round_trip(self, organization, member, label, task_list):
        payload = serialize_filters(
            {
                "q": "report",
                "status": "open",
                "due": "week",
                "priority": "3",
                "sort": "due",
                "assignee": member.user,
                "task_list": task_list,
                "label": label,
            }
        )
        assert payload["q"] == "report"
        assert payload["assignee"] == member.user.pk

        restored = deserialize_filters(payload, organization=organization)
        assert restored["assignee"] == member.user
        assert restored["task_list"] == task_list
        assert restored["label"] == label

    def test_empty_values_are_dropped(self):
        assert serialize_filters({"q": "", "status": None, "due": "overdue"}) == {"due": "overdue"}

    def test_deleted_references_deserialise_to_none(self, organization):
        restored = deserialize_filters(
            {"assignee": 999999, "task_list": 999999, "label": 999999}, organization=organization
        )
        assert restored["assignee"] is None
        assert restored["task_list"] is None
        assert restored["label"] is None

    def test_a_reference_from_another_tenant_deserialises_to_none(
        self, organization, other_organization
    ):
        foreign = Label.objects.create(organization=other_organization, name="Foreign")
        assert deserialize_filters({"label": foreign.pk}, organization=organization)["label"] is None


class TestSavedViewApplication:
    def test_a_saved_view_applies_its_filters(self, client_owner, organization, user, corpus):
        view = SavedView.objects.create(
            organization=organization, owner=user, name="Overdue", filters={"due": "overdue"}
        )
        response = client_owner.get(f"/tasks/?view={view.pk}")
        assert response.status_code == 200
        assert [t.title for t in response.context["tasks"]] == ["Overdue item"]

    def test_a_shared_view_from_a_colleague_applies(self, client_owner, organization, member, corpus):
        view = SavedView.objects.create(
            organization=organization,
            owner=member.user,
            name="Shared overdue",
            filters={"due": "overdue"},
            is_shared=True,
        )
        assert client_owner.get(f"/tasks/?view={view.pk}").status_code == 200

    def test_a_private_view_from_a_colleague_is_ignored(self, client_owner, organization, member):
        view = SavedView.objects.create(
            organization=organization, owner=member.user, name="Private", filters={"due": "overdue"}
        )
        assert client_owner.get(f"/tasks/?view={view.pk}").context["saved_view"] is None

    def test_a_view_from_another_tenant_is_ignored(
        self, client_owner, other_organization, other_user
    ):
        view = SavedView.objects.create(
            organization=other_organization, owner=other_user, name="Theirs", is_shared=True
        )
        assert client_owner.get(f"/tasks/?view={view.pk}").context["saved_view"] is None

    def test_deleting_your_own_view(self, client_owner, organization, user):
        view = SavedView.objects.create(organization=organization, owner=user, name="Doomed")
        client_owner.post(f"/views/{view.pk}/delete/")
        assert not SavedView.objects.filter(pk=view.pk).exists()

    def test_cannot_delete_a_colleagues_view(self, client_owner, organization, member):
        view = SavedView.objects.create(organization=organization, owner=member.user, name="Theirs")
        assert client_owner.post(f"/views/{view.pk}/delete/").status_code == 404

    def test_saving_without_a_name_is_refused(self, client_owner, organization):
        client_owner.post("/views/save/", {"name": "  ", "due": "overdue"})
        assert not SavedView.objects.filter(organization=organization).exists()


class TestWorkspaceStructureViews:
    def test_creating_a_project_area(self, client_owner, organization):
        client_owner.post("/projects/new/", {"name": "Platform", "color": "#2563eb", "position": 1})
        assert ProjectArea.objects.filter(organization=organization, name="Platform").exists()

    def test_a_duplicate_project_area_reports_an_error(self, client_owner, organization):
        ProjectArea.objects.create(organization=organization, name="Platform")
        response = client_owner.post("/projects/new/", {"name": "Platform", "color": "#2563eb"})
        assert response.status_code == 302
        assert ProjectArea.objects.filter(name="Platform").count() == 1

    def test_creating_a_column(self, client_owner, task_list):
        client_owner.post(
            "/lists/columns/new/",
            {"task_list": task_list.pk, "name": "Review", "position": 9, "wip_limit": 3},
        )
        assert Column.objects.filter(task_list=task_list, name="Review", wip_limit=3).exists()

    def test_cannot_add_a_column_to_another_tenants_list(self, client_owner, other_task_list):
        client_owner.post(
            "/lists/columns/new/", {"task_list": other_task_list.pk, "name": "Injected"}
        )
        assert not Column.objects.filter(name="Injected").exists()

    def test_a_duplicate_label_reports_an_error(self, client_owner, organization, label):
        response = client_owner.post("/labels/new/", {"name": label.name, "color": "#111111"})
        assert response.status_code == 302
        assert Label.objects.filter(organization=organization, name=label.name).count() == 1

    def test_an_invalid_colour_is_rejected(self, client_owner, organization):
        client_owner.post("/labels/new/", {"name": "Bad colour", "color": "not-a-colour"})
        assert not Label.objects.filter(name="Bad colour").exists()


class TestAttachments:
    def _upload(self, client, task, *, name="notes.txt", content=b"hello", content_type="text/plain"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return client.post(
            f"/tasks/{task.pk}/attachments/",
            {"file": SimpleUploadedFile(name, content, content_type=content_type)},
        )

    def test_upload_and_download(self, client_owner, task):
        assert self._upload(client_owner, task).status_code == 302
        attachment = Attachment.objects.get(task=task)
        assert attachment.original_name == "notes.txt"

        response = client_owner.get(f"/attachments/{attachment.pk}/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"hello"

    def test_the_stored_path_never_uses_the_supplied_filename(self, client_owner, task):
        """Removes path traversal and filename collision in one step."""

        self._upload(client_owner, task, name="../../etc/passwd.txt")
        attachment = Attachment.objects.get(task=task)
        assert ".." not in attachment.file.name
        assert "passwd" not in attachment.file.name
        assert attachment.file.name.endswith(".txt")
        # Two independent layers: Django's UploadedFile strips the directory component,
        # and `attachment_upload_path` discards the name entirely in favour of a UUID.
        # Neither alone would be something to rely on.
        assert ".." not in attachment.original_name
        assert "/" not in attachment.original_name

    def test_an_extensionless_file_is_rejected(self, client_owner, task):
        assert self._upload(client_owner, task, name="Dockerfile").status_code == 400

    def test_a_disallowed_extension_is_rejected(self, client_owner, task):
        response = self._upload(client_owner, task, name="payload.exe", content_type="application/x-msdownload")
        assert response.status_code == 400
        assert not Attachment.objects.filter(task=task).exists()

    def test_an_oversized_file_is_rejected(self, client_owner, task, settings):
        settings.ATTACHMENT_MAX_BYTES = 10
        assert self._upload(client_owner, task, content=b"x" * 100).status_code == 400

    def test_downloads_are_marked_private_and_non_sniffable(self, client_owner, task):
        self._upload(client_owner, task)
        attachment = Attachment.objects.get(task=task)
        response = client_owner.get(f"/attachments/{attachment.pk}/")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response["Content-Security-Policy"]

    def test_cannot_download_another_tenants_attachment(
        self, client_owner, other_organization, other_task, other_user
    ):
        from django.core.files.uploadedfile import SimpleUploadedFile

        attachment = Attachment.objects.create(
            organization=other_organization,
            task=other_task,
            uploaded_by=other_user,
            file=SimpleUploadedFile("secret.txt", b"theirs"),
            original_name="secret.txt",
            size_bytes=6,
        )
        assert client_owner.get(f"/attachments/{attachment.pk}/").status_code == 404

    def test_a_viewer_cannot_upload(self, client_viewer, task):
        assert self._upload(client_viewer, task).status_code == 403


class TestWorkspaceCreationView:
    def test_creating_a_second_workspace_switches_to_it(self, client_owner, user):
        response = client_owner.post(
            "/workspaces/new/", {"name": "Second Venture", "timezone": "UTC"}
        )
        assert response.status_code == 302
        organization = Organization.objects.get(name="Second Venture")
        from kernel.tenancy import SESSION_ORG_KEY

        assert client_owner.session[SESSION_ORG_KEY] == organization.pk

    def test_an_invalid_timezone_is_refused(self, client_owner):
        client_owner.post("/workspaces/new/", {"name": "Bad", "timezone": "Mars/Olympus"})
        assert not Organization.objects.filter(name="Bad").exists()

    def test_the_page_renders_for_a_user_without_a_workspace(self, client, other_user):
        client.force_login(other_user)
        assert client.get("/workspaces/new/").status_code == 200


class TestInvitationAcceptPage:
    def test_a_valid_token_shows_the_workspace(self, client, organization, user, other_user):
        from kernel.models import Invitation

        _invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        client.force_login(other_user)
        response = client.get(f"/invitations/{token}/")
        assert response.status_code == 200
        assert response.context["valid"] is True
        assert organization.name.encode() in response.content

    def test_an_invalid_token_shows_an_error(self, client, other_user):
        client.force_login(other_user)
        response = client.get("/invitations/bogus-token/")
        assert response.status_code == 200
        assert response.context["valid"] is False

    def test_accepting_requires_a_session(self, client, organization, user):
        from kernel.models import Invitation

        _invitation, token = Invitation.issue(
            organization=organization, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        response = client.get(f"/invitations/{token}/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


class TestScheduleWebViews:
    def test_creating_a_schedule(self, client_owner, organization, queue):
        from schedules.models import Schedule

        client_owner.post(
            "/automation/schedules/new/",
            {
                "name": "Nightly sync",
                "queue": queue.pk,
                "expression_type": "cron",
                "expression": "0 2 * * *",
                "timezone": "UTC",
                "target_handler": "internal:schedule_fire",
                "catch_up_policy": "run_once",
                "payload": "{}",
                "active": "on",
            },
        )
        schedule = Schedule.objects.get(organization=organization, name="Nightly sync")
        assert schedule.next_run_at is not None

    def test_an_invalid_expression_is_reported(self, client_owner, organization, queue):
        from schedules.models import Schedule

        client_owner.post(
            "/automation/schedules/new/",
            {
                "name": "Broken",
                "queue": queue.pk,
                "expression_type": "cron",
                "expression": "not a cron",
                "timezone": "UTC",
                "payload": "{}",
            },
        )
        assert not Schedule.objects.filter(name="Broken").exists()

    def test_the_preview_fragment_renders(self, client_owner, organization, queue):
        from schedules.models import Schedule

        schedule = Schedule.objects.create(
            organization=organization,
            queue=queue,
            name="Preview me",
            expression_type=Schedule.ExpressionType.CRON,
            expression="*/30 * * * *",
        )
        response = client_owner.get(f"/schedules/{schedule.pk}/preview/")
        assert response.status_code == 200
        assert b"<html" not in response.content

    def test_cannot_preview_another_tenants_schedule(self, client_owner, other_organization):
        from queues.services import get_internal_queue
        from schedules.models import Schedule

        foreign = Schedule.objects.create(
            organization=other_organization,
            queue=get_internal_queue(other_organization),
            name="Theirs",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
        )
        assert client_owner.get(f"/schedules/{foreign.pk}/preview/").status_code == 404


class TestDeadLetterViews:
    def test_redriving_from_the_ui(self, client_owner, organization, job):
        from queues.models import DeadLetter, Job

        dead_letter = DeadLetter.objects.create(
            organization=organization, job=job, reason="exhausted"
        )
        Job.objects.filter(pk=job.pk).update(status=Job.Status.DEAD)

        client_owner.post(f"/automation/dead-letters/{dead_letter.pk}/redrive/")
        job.refresh_from_db()
        assert job.status == Job.Status.AVAILABLE

    def test_a_member_cannot_redrive(self, client_member, organization, job):
        from queues.models import DeadLetter

        dead_letter = DeadLetter.objects.create(
            organization=organization, job=job, reason="exhausted"
        )
        assert client_member.post(
            f"/automation/dead-letters/{dead_letter.pk}/redrive/"
        ).status_code == 403

    def test_cannot_redrive_another_tenants_dead_letter(
        self, client_owner, other_organization
    ):
        from queues.models import DeadLetter, Job
        from queues.services import enqueue_job, get_internal_queue

        foreign_job = enqueue_job(
            organization=other_organization,
            queue=get_internal_queue(other_organization),
            source="x",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key="foreign",
        ).job
        dead_letter = DeadLetter.objects.create(
            organization=other_organization, job=foreign_job, reason="x"
        )
        assert client_owner.post(
            f"/automation/dead-letters/{dead_letter.pk}/redrive/"
        ).status_code == 404
