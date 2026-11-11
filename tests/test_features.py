"""Phase 2 feature audit: billing quotas, exports, account lifecycle, time tracking,
templates, bulk operations, calendar and public share links."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import Plan, QuotaExceeded, Subscription, UsageRecord
from billing.services import (
    change_plan,
    check_quota,
    current_usage,
    ensure_default_plans,
    get_subscription,
    has_feature,
    record_usage,
)
from exports.services import build_workspace_archive, export_to_string, sanitize_cell
from kernel.models import Membership, Organization
from kernel.roles import Role
from support.services import blocking_owned_workspaces, build_personal_export, delete_account, leave_workspace
from tasks.models import ShareLink, Subtask, Task, TaskTemplate, TimeEntry
from tasks.services_extra import (
    apply_template,
    bulk_update_tasks,
    calendar_month,
    log_time,
    resolve_share_link,
    shared_tasks,
    start_timer,
    stop_timer,
    template_from_task,
    time_summary,
)

pytestmark = pytest.mark.django_db


# ======================================================================================
# Billing
# ======================================================================================


class TestPlans:
    def test_default_plans_are_seeded_idempotently(self):
        ensure_default_plans()
        first = Plan.objects.count()
        ensure_default_plans()
        assert Plan.objects.count() == first
        assert first >= 3

    def test_a_workspace_gets_a_subscription_lazily(self, organization):
        subscription = get_subscription(organization)
        assert subscription.plan is not None
        # A second call must not create a duplicate.
        assert get_subscription(organization).pk == subscription.pk
        assert Subscription.objects.filter(organization=organization).count() == 1

    def test_price_display(self):
        ensure_default_plans()
        assert Plan.objects.get(code="free").price_display == "Free"
        assert "12.00" in Plan.objects.get(code="team").price_display

    def test_unlimited_is_none_not_zero(self):
        """0 would mean "none allowed", which is a different thing entirely."""

        ensure_default_plans()
        assert Plan.objects.get(code="business").max_tasks is None


class TestQuotas:
    def _set_plan(self, organization, **limits):
        plan = Plan.objects.create(code="tiny", name="Tiny", position=99, **limits)
        subscription = get_subscription(organization)
        subscription.plan = plan
        subscription.status = Subscription.Status.ACTIVE
        subscription.save()
        return plan

    def test_under_the_limit_passes(self, organization):
        self._set_plan(organization, max_tasks=10)
        check_quota(organization, "tasks")

    def test_at_the_limit_raises(self, organization, task_list, user):
        self._set_plan(organization, max_tasks=2)
        for index in range(2):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        with pytest.raises(QuotaExceeded) as exc:
            check_quota(organization, "tasks")
        assert "Tiny" in str(exc.value)

    def test_an_unlimited_plan_never_raises(self, organization, task_list, user):
        self._set_plan(organization, max_tasks=None)
        for index in range(5):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        check_quota(organization, "tasks")

    def test_quick_add_enforces_the_task_quota(self, organization, task_list, user):
        from tasks.services import create_task_from_quick_add

        self._set_plan(organization, max_tasks=1)
        create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="First"
        )
        with pytest.raises(QuotaExceeded):
            create_task_from_quick_add(
                organization=organization, task_list=task_list, creator=user, raw="Second"
            )

    def test_archived_tasks_do_not_count(self, organization, task_list, user):
        self._set_plan(organization, max_tasks=2)
        task = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Old"
        )
        task.archive()
        check_quota(organization, "tasks", additional=2)

    def test_quotas_are_per_tenant(self, organization, other_organization, other_task_list, other_user):
        self._set_plan(organization, max_tasks=1)
        for index in range(5):
            Task.objects.create(
                organization=other_organization,
                task_list=other_task_list,
                creator=other_user,
                title=f"T{index}",
            )
        # The other tenant's volume must not affect this one.
        check_quota(organization, "tasks")

    def test_current_usage_reports_every_resource(self, organization):
        report = current_usage(organization)
        for resource in ("members", "tasks", "task_lists", "api_keys", "webhooks", "schedules"):
            assert resource in report["usage"]
        assert report["usage"]["members"]["used"] >= 1


class TestPlanChanges:
    def test_upgrade_succeeds(self, organization):
        ensure_default_plans()
        business = Plan.objects.get(code="business")
        subscription = change_plan(organization=organization, plan=business)
        assert subscription.plan == business
        assert subscription.status == Subscription.Status.ACTIVE

    def test_a_downgrade_that_does_not_fit_is_refused(self, organization, task_list, user):
        """Silently accepting it would leave the workspace permanently over quota."""

        ensure_default_plans()
        change_plan(organization=organization, plan=Plan.objects.get(code="business"))
        tiny = Plan.objects.create(code="micro", name="Micro", max_tasks=1, position=98)
        for index in range(3):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        with pytest.raises(QuotaExceeded):
            change_plan(organization=organization, plan=tiny)

    def test_plan_changes_are_audited(self, organization, user):
        from kernel.models import AuditLog

        ensure_default_plans()
        change_plan(
            organization=organization, plan=Plan.objects.get(code="team"), actor=user
        )
        assert AuditLog.objects.filter(action="billing.plan_changed").exists()


class TestEntitlement:
    def test_a_canceled_subscription_loses_features(self, organization):
        subscription = get_subscription(organization)
        subscription.status = Subscription.Status.CANCELED
        subscription.save()
        assert not has_feature(organization, "webhooks")

    def test_past_due_keeps_access(self, organization):
        """Cutting a customer off the moment a card fails loses more than it protects."""

        ensure_default_plans()
        change_plan(organization=organization, plan=Plan.objects.get(code="business"))
        subscription = get_subscription(organization)
        subscription.status = Subscription.Status.PAST_DUE
        subscription.save()
        assert has_feature(organization, "webhooks")

    def test_an_expired_trial_loses_access(self, organization):
        subscription = get_subscription(organization)
        subscription.status = Subscription.Status.TRIALING
        subscription.trial_ends_at = timezone.now() - timedelta(days=1)
        subscription.save()
        assert not has_feature(organization, "webhooks")

    def test_a_plan_without_the_feature_denies_it(self, organization):
        ensure_default_plans()
        change_plan(organization=organization, plan=Plan.objects.get(code="free"))
        assert not has_feature(organization, "webhooks")
        assert has_feature(organization, "board_view")


class TestUsageRecords:
    def test_record_usage_accumulates(self, organization):
        record_usage(organization, "jobs", amount=3)
        record_usage(organization, "jobs", amount=2)
        assert UsageRecord.objects.get(organization=organization, metric="jobs").value == 5

    def test_records_are_per_day(self, organization):
        record_usage(organization, "jobs", day=date(2026, 1, 1))
        record_usage(organization, "jobs", day=date(2026, 1, 2))
        assert UsageRecord.objects.filter(organization=organization, metric="jobs").count() == 2

    def test_duplicate_day_rows_are_impossible(self, organization):
        record_usage(organization, "jobs")
        with pytest.raises(IntegrityError), transaction.atomic():
            UsageRecord.objects.create(
                organization=organization, metric="jobs", day=timezone.localdate(), value=1
            )


# ======================================================================================
# Exports
# ======================================================================================


class TestCsvInjection:
    @pytest.mark.parametrize("payload", ["=1+1", "+1+1", "-1+1", "@SUM(A1)", "=cmd|'/c calc'!A0"])
    def test_formula_prefixes_are_neutralised(self, payload):
        """A spreadsheet evaluates these the moment the file is opened."""

        assert sanitize_cell(payload).startswith("'")

    def test_ordinary_text_is_untouched(self):
        assert sanitize_cell("Write the report") == "Write the report"
        assert sanitize_cell("A - B") == "A - B"

    def test_none_becomes_empty(self):
        assert sanitize_cell(None) == ""

    def test_a_malicious_task_title_is_escaped_in_the_export(self, organization, task_list, user):
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="=HYPERLINK(1)"
        )
        output = export_to_string(organization, fmt="csv")
        rows = list(csv.reader(io.StringIO(output)))
        titles = [row[1] for row in rows[1:]]
        assert "'=HYPERLINK(1)" in titles


class TestExports:
    def test_csv_has_a_header_and_a_row_per_task(self, organization, task_list, user):
        for index in range(3):
            Task.objects.create(
                organization=organization, task_list=task_list, creator=user, title=f"T{index}"
            )
        rows = list(csv.reader(io.StringIO(export_to_string(organization, fmt="csv"))))
        assert rows[0][0] == "id"
        assert len(rows) == 4

    def test_archived_tasks_are_excluded_by_default(self, organization, task_list, user):
        task = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Archived"
        )
        task.archive()
        assert "Archived" not in export_to_string(organization, fmt="csv")
        assert "Archived" in export_to_string(organization, fmt="csv", include_archived=True)

    def test_exports_never_include_other_tenants(self, organization, task, other_task):
        output = export_to_string(organization, fmt="csv")
        assert task.title in output
        assert other_task.title not in output

    def test_workspace_archive_is_complete_and_scoped(self, organization, task, label, member):
        archive = build_workspace_archive(organization)
        assert archive["meta"]["organization"]["slug"] == organization.slug
        assert any(row["title"] == task.title for row in archive["tasks"])
        assert any(row["name"] == label.name for row in archive["labels"])
        assert len(archive["members"]) >= 2

    def test_the_endpoint_streams_csv(self, client_owner, task):
        response = client_owner.get("/export/tasks/?format=csv")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        assert "attachment" in response["Content-Disposition"]
        body = b"".join(response.streaming_content).decode()
        assert task.title in body

    def test_the_endpoint_streams_json(self, client_owner, task):
        response = client_owner.get("/export/tasks/?format=json")
        payload = json.loads(b"".join(response.streaming_content).decode())
        assert payload["meta"]["organization"]["slug"]
        assert payload["tasks"]

    def test_a_member_cannot_export(self, client_member):
        assert client_member.get("/export/tasks/").status_code == 403

    def test_exporting_is_audited(self, client_owner, organization):
        from kernel.models import AuditLog

        client_owner.get("/export/tasks/")
        assert AuditLog.objects.filter(action="data.exported", organization=organization).exists()


# ======================================================================================
# Account lifecycle
# ======================================================================================


class TestPersonalExport:
    def test_includes_the_account_and_its_workspaces(self, user, organization, task):
        export = build_personal_export(user)
        assert export["account"]["username"] == user.get_username()
        assert export["workspaces"][0]["slug"] == organization.slug
        assert any(row["title"] == task.title for row in export["tasks_created"])

    def test_never_includes_the_password_hash(self, user, organization):
        """Even in the subject's own export."""

        serialised = json.dumps(build_personal_export(user), default=str)
        assert "pbkdf2" not in serialised
        assert "password" not in serialised

    def test_the_endpoint_returns_a_download(self, client_owner):
        response = client_owner.get("/account/export/")
        assert response.status_code == 200
        assert "attachment" in response["Content-Disposition"]


class TestAccountDeletion:
    def test_sole_ownership_blocks_deletion(self, user, organization):
        assert blocking_owned_workspaces(user) == [organization]
        with pytest.raises(ValidationError, match="only owner"):
            delete_account(user=user)

    def test_a_second_owner_unblocks_it(self, user, organization, member):
        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        assert blocking_owned_workspaces(user) == []
        delete_account(user=user)

    def test_authored_content_is_anonymised_not_destroyed(self, user, organization, member, task):
        """Deleting a shared board in the name of one person's erasure destroys other
        people's work; severing the identifying link does not."""

        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        title = task.title
        delete_account(user=user)

        surviving = Task.objects.get(pk=task.pk)
        assert surviving.title == title
        assert surviving.creator.get_username() == "deleted-user"

    def test_the_user_row_is_gone(self, user, organization, member):
        from django.contrib.auth import get_user_model

        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        username = user.get_username()
        delete_account(user=user)
        assert not get_user_model().objects.filter(username=username).exists()

    def test_memberships_are_removed(self, user, organization, member):
        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        user_id = user.pk
        delete_account(user=user)
        assert not Membership.objects.filter(user_id=user_id).exists()

    def test_opting_in_deletes_sole_owned_workspaces(self, user, organization):
        delete_account(user=user, delete_sole_owned_workspaces=True)
        organization.refresh_from_db()
        assert organization.deleted_at is not None

    def test_deletion_is_audited(self, user, organization, member):
        from kernel.models import AuditLog

        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        delete_account(user=user)
        assert AuditLog.objects.filter(action="account.deleted").exists()

    def test_the_view_requires_the_exact_username(self, client_owner, user):
        from django.contrib.auth import get_user_model

        client_owner.post("/account/delete/", {"confirm": "wrong"})
        assert get_user_model().objects.filter(pk=user.pk).exists()


class TestLeaveWorkspace:
    def test_a_member_can_leave(self, member_user, organization, member):
        leave_workspace(user=member_user, organization=organization)
        assert not Membership.objects.filter(user=member_user, organization=organization).exists()

    def test_the_last_owner_cannot_leave(self, user, organization):
        with pytest.raises(ValidationError, match="only owner"):
            leave_workspace(user=user, organization=organization)

    def test_a_non_member_cannot_leave(self, other_user, organization):
        with pytest.raises(ValidationError, match="not a member"):
            leave_workspace(user=other_user, organization=organization)


# ======================================================================================
# Time tracking
# ======================================================================================


class TestTimers:
    def test_starting_creates_a_running_entry(self, task, user):
        entry = start_timer(task=task, user=user)
        assert entry.is_running
        assert entry.ended_at is None

    def test_starting_a_second_timer_stops_the_first(self, task, user, organization, task_list):
        """Switching tasks is the normal case; refusing would just produce forgotten timers."""

        other = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Other"
        )
        first = start_timer(task=task, user=user)
        second = start_timer(task=other, user=user)

        first.refresh_from_db()
        assert not first.is_running
        assert second.is_running
        assert TimeEntry.objects.filter(user=user, ended_at__isnull=True).count() == 1

    def test_restarting_the_same_task_is_a_no_op(self, task, user):
        first = start_timer(task=task, user=user)
        assert start_timer(task=task, user=user).pk == first.pk

    def test_two_running_timers_are_impossible_at_the_database(self, task, user, organization):
        start_timer(task=task, user=user)
        with pytest.raises(IntegrityError), transaction.atomic():
            TimeEntry.objects.create(organization=organization, task=task, user=user)

    def test_different_users_may_each_have_a_timer(self, task, user, member_user, member):
        start_timer(task=task, user=user)
        start_timer(task=task, user=member_user)
        assert TimeEntry.objects.filter(ended_at__isnull=True).count() == 2

    def test_stopping_records_the_duration(self, task, user):
        entry = start_timer(task=task, user=user)
        TimeEntry.objects.filter(pk=entry.pk).update(
            started_at=timezone.now() - timedelta(minutes=30)
        )
        entry.refresh_from_db()

        stopped = stop_timer(user=user, entry=entry)
        assert 1750 <= stopped.duration_seconds <= 1850

    def test_a_runaway_timer_is_capped(self, task, user):
        """A 40-hour "session" corrupts every average it touches."""

        entry = start_timer(task=task, user=user)
        TimeEntry.objects.filter(pk=entry.pk).update(started_at=timezone.now() - timedelta(days=2))
        entry.refresh_from_db()

        stopped = entry.stop()
        assert stopped.duration_seconds <= TimeEntry.MAX_RUNNING.total_seconds()

    def test_stopping_with_no_timer_returns_none(self, user):
        assert stop_timer(user=user) is None


class TestManualTimeLogging:
    def test_logging_creates_a_closed_entry(self, task, user):
        entry = log_time(task=task, user=user, minutes=45, note="Pairing")
        assert entry.duration_seconds == 45 * 60
        assert not entry.is_running

    @pytest.mark.parametrize("minutes", [0, -10, 24 * 60 + 1])
    def test_invalid_durations_are_rejected(self, task, user, minutes):
        with pytest.raises(ValidationError):
            log_time(task=task, user=user, minutes=minutes)

    def test_summary_aggregates(self, task, user):
        log_time(task=task, user=user, minutes=30)
        log_time(task=task, user=user, minutes=90)
        summary = time_summary(organization=task.organization, task=task)
        assert summary["total_hours"] == 2.0
        assert summary["entry_count"] == 2

    def test_summary_is_tenant_scoped(self, organization, other_task, other_user, task, user):
        log_time(task=task, user=user, minutes=60)
        log_time(task=other_task, user=other_user, minutes=120)
        assert time_summary(organization=organization)["total_hours"] == 1.0

    def test_an_entry_cannot_end_before_it_starts(self, task, user, organization):
        now = timezone.now()
        with pytest.raises(IntegrityError), transaction.atomic():
            TimeEntry.objects.create(
                organization=organization,
                task=task,
                user=user,
                started_at=now,
                ended_at=now - timedelta(hours=1),
            )


# ======================================================================================
# Templates
# ======================================================================================


class TestTemplates:
    def _template(self, organization, task_list, **kwargs):
        defaults = {
            "organization": organization,
            "name": "Onboarding",
            "title_template": "Onboard a new hire",
            "task_list": task_list,
            "priority": 3,
            "subtask_titles": ["Send laptop", "Create accounts", "Book intro call"],
        }
        defaults.update(kwargs)
        return TaskTemplate.objects.create(**defaults)

    def test_applying_creates_the_task_and_its_subtasks(self, organization, task_list, user):
        template = self._template(organization, task_list)
        task = apply_template(template=template, creator=user)

        assert task.title == "Onboard a new hire"
        assert task.priority == 3
        assert Subtask.objects.filter(task=task).count() == 3
        assert list(
            Subtask.objects.filter(task=task).order_by("position").values_list("title", flat=True)
        ) == ["Send laptop", "Create accounts", "Book intro call"]

    def test_due_in_days_becomes_a_due_date(self, organization, task_list, user):
        template = self._template(organization, task_list, due_in_days=7)
        task = apply_template(template=template, creator=user)
        assert task.due_date == timezone.localdate() + timedelta(days=7)

    def test_usage_is_counted(self, organization, task_list, user):
        template = self._template(organization, task_list)
        apply_template(template=template, creator=user)
        apply_template(template=template, creator=user)
        template.refresh_from_db()
        assert template.times_used == 2

    def test_applying_respects_the_task_quota(self, organization, task_list, user):
        plan = Plan.objects.create(code="tiny2", name="Tiny", max_tasks=0, position=97)
        subscription = get_subscription(organization)
        subscription.plan = plan
        subscription.save()

        with pytest.raises(QuotaExceeded):
            apply_template(template=self._template(organization, task_list), creator=user)

    def test_a_foreign_task_list_is_rejected(self, organization, task_list, other_task_list, user):
        template = self._template(organization, task_list)
        with pytest.raises(ValidationError):
            apply_template(template=template, creator=user, task_list=other_task_list)

    def test_capturing_a_task_as_a_template(self, task, user, label):
        task.labels.add(label)
        Subtask.objects.create(task=task, title="Step one", position=1)
        Subtask.objects.create(task=task, title="Step two", position=2)

        template = template_from_task(task=task, name="Captured", created_by=user)
        assert template.title_template == task.title
        assert template.subtask_titles == ["Step one", "Step two"]
        assert list(template.labels.all()) == [label]

    def test_too_many_subtasks_fail_validation(self, organization, task_list):
        template = TaskTemplate(
            organization=organization,
            name="Huge",
            title_template="x",
            subtask_titles=[f"s{index}" for index in range(200)],
        )
        with pytest.raises(ValidationError):
            template.clean()

    def test_names_are_unique_per_workspace(self, organization, task_list):
        self._template(organization, task_list)
        with pytest.raises(IntegrityError), transaction.atomic():
            self._template(organization, task_list)


# ======================================================================================
# Bulk operations
# ======================================================================================


class TestBulkOperations:
    @pytest.fixture
    def many_tasks(self, organization, task_list, user):
        return [
            Task.objects.create(
                organization=organization,
                task_list=task_list,
                creator=user,
                title=f"Bulk {index}",
                position=index * 1000,
            )
            for index in range(5)
        ]

    def test_bulk_complete(self, organization, user, many_tasks):
        result = bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[t.pk for t in many_tasks],
            action="complete",
        )
        assert result["updated"] == 5
        assert Task.objects.filter(status=Task.Status.COMPLETED).count() == 5

    def test_bulk_priority(self, organization, user, many_tasks):
        bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[t.pk for t in many_tasks],
            action="priority",
            value=4,
        )
        assert Task.objects.filter(priority=4).count() == 5

    def test_bulk_archive(self, organization, user, many_tasks):
        bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[t.pk for t in many_tasks],
            action="archive",
        )
        assert Task.objects.filter(archived_at__isnull=False).count() == 5

    def test_bulk_assign(self, organization, user, member, many_tasks):
        bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[t.pk for t in many_tasks],
            action="assign",
            value=member.user.pk,
        )
        assert Task.objects.filter(assignee=member.user).count() == 5

    def test_cannot_assign_a_non_member(self, organization, user, other_user, many_tasks):
        with pytest.raises(ValidationError):
            bulk_update_tasks(
                organization=organization,
                actor=user,
                task_ids=[t.pk for t in many_tasks],
                action="assign",
                value=other_user.pk,
            )

    def test_foreign_ids_are_skipped_not_applied(self, organization, user, many_tasks, other_task):
        """A crafted id list must never reach another tenant's rows."""

        result = bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[many_tasks[0].pk, other_task.pk],
            action="complete",
        )
        assert result["skipped"] == 1
        other_task.refresh_from_db()
        assert other_task.status == Task.Status.OPEN

    def test_the_batch_size_is_bounded(self, organization, user, many_tasks):
        from tasks.services_extra import MAX_BULK_ITEMS

        result = bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=list(range(1, MAX_BULK_ITEMS + 2000)),
            action="complete",
        )
        assert result["updated"] + result["skipped"] <= MAX_BULK_ITEMS

    def test_an_unknown_action_is_rejected(self, organization, user, many_tasks):
        with pytest.raises(ValidationError):
            bulk_update_tasks(
                organization=organization,
                actor=user,
                task_ids=[many_tasks[0].pk],
                action="drop_database",
            )

    def test_moving_lists_clears_the_stale_column(self, organization, user, many_tasks, columns):
        """The column belongs to the old list; keeping it would orphan the card."""

        from tasks.models import TaskList

        target = TaskList.objects.create(organization=organization, name="Elsewhere", position=9)
        target.ensure_default_columns()

        bulk_update_tasks(
            organization=organization,
            actor=user,
            task_ids=[t.pk for t in many_tasks],
            action="move_list",
            value=target.pk,
        )
        assert Task.objects.filter(task_list=target, column__isnull=False).count() == 0

    def test_an_empty_selection_is_a_no_op(self, organization, user):
        assert bulk_update_tasks(
            organization=organization, actor=user, task_ids=[], action="complete"
        ) == {"updated": 0, "skipped": 0}

    def test_the_view_applies_the_action(self, client_owner, organization, many_tasks):
        client_owner.post(
            "/tasks/bulk/",
            {"task_ids": [t.pk for t in many_tasks], "action": "priority", "value": "4"},
        )
        assert Task.objects.filter(priority=4).count() == 5

    def test_a_viewer_cannot_bulk_edit(self, client_viewer, many_tasks):
        response = client_viewer.post(
            "/tasks/bulk/", {"task_ids": [many_tasks[0].pk], "action": "complete"}
        )
        assert response.status_code == 403


# ======================================================================================
# Calendar
# ======================================================================================


class TestCalendar:
    def test_the_grid_is_whole_weeks(self, organization):
        data = calendar_month(organization=organization, year=2026, month=6)
        assert all(len(week) == 7 for week in data["weeks"])
        assert 4 <= len(data["weeks"]) <= 6

    def test_tasks_land_on_their_due_date(self, organization, task_list, user):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Mid-month",
            due_date=date(2026, 6, 15),
        )
        data = calendar_month(organization=organization, year=2026, month=6)
        matching = [
            day for week in data["weeks"] for day in week if day["date"] == date(2026, 6, 15)
        ]
        assert matching and matching[0]["tasks"][0].title == "Mid-month"

    def test_padding_days_are_marked_out_of_month(self, organization):
        data = calendar_month(organization=organization, year=2026, month=6)
        assert any(not day["in_month"] for week in data["weeks"] for day in week)

    def test_other_tenants_are_excluded(self, organization, other_organization, other_task_list, other_user):
        Task.objects.create(
            organization=other_organization,
            task_list=other_task_list,
            creator=other_user,
            title="Theirs",
            due_date=date(2026, 6, 15),
        )
        data = calendar_month(organization=organization, year=2026, month=6)
        assert all(not day["tasks"] for week in data["weeks"] for day in week)

    @pytest.mark.parametrize("month", [0, 13, -1])
    def test_an_invalid_month_is_rejected(self, organization, month):
        with pytest.raises(ValidationError):
            calendar_month(organization=organization, year=2026, month=month)

    def test_the_view_falls_back_on_bad_input(self, client_owner):
        assert client_owner.get("/calendar/?year=abc&month=99").status_code == 200


# ======================================================================================
# Share links
# ======================================================================================


class TestShareLinks:
    def test_a_live_link_resolves(self, organization, task_list, user):
        link, raw = ShareLink.issue(
            organization=organization, task_list=task_list, created_by=user
        )
        assert resolve_share_link(raw).pk == link.pk

    def test_the_raw_token_is_never_stored(self, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        assert link.token_hash != raw
        assert link.token_hash == ShareLink.hash_token(raw)

    def test_a_revoked_link_stops_resolving(self, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        link.revoke()
        assert resolve_share_link(raw) is None

    def test_an_expired_link_stops_resolving(self, organization, task_list):
        link, raw = ShareLink.issue(
            organization=organization,
            task_list=task_list,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        assert resolve_share_link(raw) is None

    def test_a_link_into_a_deleted_workspace_stops_resolving(self, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        Organization.objects.filter(pk=organization.pk).update(deleted_at=timezone.now())
        assert resolve_share_link(raw) is None

    def test_a_forged_token_does_not_resolve(self):
        assert resolve_share_link("not-a-real-token") is None
        assert resolve_share_link("") is None

    def test_completed_tasks_are_hidden_by_default(self, organization, task_list, user, task):
        task.complete(actor=user)
        link, _ = ShareLink.issue(organization=organization, task_list=task_list)
        assert task not in list(shared_tasks(link))

    def test_completed_tasks_can_be_opted_in(self, organization, task_list, user, task):
        task.complete(actor=user)
        link, _ = ShareLink.issue(
            organization=organization, task_list=task_list, include_completed=True
        )
        assert task in list(shared_tasks(link))

    def test_the_public_page_renders_without_a_session(self, client, organization, task_list, task):
        _link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        response = client.get(f"/shared/{raw}/")
        assert response.status_code == 200
        assert task.title.encode() in response.content

    def test_the_public_page_is_not_indexable(self, client, organization, task_list):
        _link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        response = client.get(f"/shared/{raw}/")
        assert "noindex" in response["X-Robots-Tag"]
        assert "no-store" in response["Cache-Control"]

    def test_the_public_page_does_not_leak_member_identities(
        self, client, organization, task_list, task, member
    ):
        task.assignee = member.user
        task.save(update_fields=["assignee"])
        _link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        response = client.get(f"/shared/{raw}/")
        assert member.user.get_username().encode() not in response.content

    def test_an_invalid_token_is_404(self, client):
        assert client.get("/shared/nope/").status_code == 404

    def test_views_are_counted(self, client, organization, task_list):
        link, raw = ShareLink.issue(organization=organization, task_list=task_list)
        client.get(f"/shared/{raw}/")
        client.get(f"/shared/{raw}/")
        link.refresh_from_db()
        assert link.view_count == 2

    def test_only_list_managers_can_create_links(self, client_member):
        assert client_member.get("/settings/sharing/").status_code == 403

    def test_the_url_is_shown_once(self, client_owner, task_list):
        response = client_owner.post(
            "/settings/sharing/", {"task_list": task_list.pk, "label": "Client view"}
        )
        assert "/shared/" in response.context["new_url"]
        assert client_owner.get("/settings/sharing/").context["new_url"] is None
