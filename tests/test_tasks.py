"""Task domain: quick-add parsing, positioning, concurrency, dependencies, reminders."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from kernel.models import Activity
from queues.models import Job
from tasks.models import Column, Label, Reminder, Task, TaskDependency, TaskList
from tasks.quickadd import combine_due_datetime, parse_quick_add
from tasks.services import (
    ConcurrentModification,
    DependencyBlocked,
    WipLimitExceeded,
    add_dependency,
    archive_task,
    board_snapshot,
    cancel_reminders,
    create_task_from_quick_add,
    move_task,
    needs_renumber,
    next_position,
    position_between,
    renumber_column,
    schedule_reminder,
    toggle_task_completion,
    update_task,
    workspace_counts,
)

pytestmark = pytest.mark.django_db

# A fixed Wednesday so weekday arithmetic is deterministic.
NOW = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.get_current_timezone())


class TestQuickAddParsing:
    def test_plain_title_survives_intact(self):
        parsed = parse_quick_add("Write the incident report", now=NOW)
        assert parsed.title == "Write the incident report"
        assert parsed.due_date is None
        assert parsed.priority == 2

    def test_full_expression(self):
        parsed = parse_quick_add("Send report tomorrow 5pm #Ops @alex !3", now=NOW)
        assert parsed.title == "Send report"
        assert parsed.due_date == date(2026, 6, 11)
        assert parsed.due_time == time(17, 0)
        assert parsed.labels == ["Ops"]
        assert parsed.mentions == ["alex"]
        assert parsed.priority == 3

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("5pm", time(17, 0)),
            ("5:30pm", time(17, 30)),
            ("9am", time(9, 0)),
            ("12pm", time(12, 0)),
            ("12am", time(0, 0)),
            ("11:59pm", time(23, 59)),
            ("14:30", time(14, 30)),
            ("00:15", time(0, 15)),
        ],
    )
    def test_time_formats(self, text, expected):
        assert parse_quick_add(f"Standup {text}", now=NOW).due_time == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("today", date(2026, 6, 10)),
            ("tomorrow", date(2026, 6, 11)),
            ("in 3 days", date(2026, 6, 13)),
            ("in 2 weeks", date(2026, 6, 24)),
            ("2026-12-25", date(2026, 12, 25)),
        ],
    )
    def test_date_formats(self, text, expected):
        assert parse_quick_add(f"Ship it {text}", now=NOW).due_date == expected

    def test_weekday_resolves_to_the_next_occurrence(self):
        # NOW is a Wednesday; "friday" is two days later.
        assert parse_quick_add("Deploy friday", now=NOW).due_date == date(2026, 6, 12)

    def test_same_weekday_means_next_week_not_today(self):
        """A task typed on Wednesday and due "wednesday" means the following one."""

        assert parse_quick_add("Retro wednesday", now=NOW).due_date == date(2026, 6, 17)

    def test_invalid_iso_date_stays_in_the_title(self):
        parsed = parse_quick_add("Audit 2026-02-31", now=NOW)
        assert parsed.due_date is None
        assert "2026-02-31" in parsed.title

    @pytest.mark.parametrize("priority", [1, 2, 3, 4])
    def test_priority_markers(self, priority):
        assert parse_quick_add(f"Task !{priority}", now=NOW).priority == priority

    def test_out_of_range_priority_is_not_a_marker(self):
        parsed = parse_quick_add("Task !9", now=NOW)
        assert parsed.priority == 2
        assert "!9" in parsed.title

    def test_labels_are_deduplicated_case_insensitively(self):
        parsed = parse_quick_add("Task #ops #OPS #api", now=NOW)
        assert parsed.labels == ["ops", "api"]

    def test_label_count_is_bounded(self):
        raw = "Task " + " ".join(f"#l{index}" for index in range(50))
        assert len(parse_quick_add(raw, now=NOW).labels) <= 10

    def test_input_length_is_bounded(self):
        """An unbounded paste against several regexes is a cheap DoS on the preview."""

        parsed = parse_quick_add("x" * 100_000, now=NOW)
        assert len(parsed.title) <= 240

    def test_empty_input_gets_a_placeholder(self):
        assert parse_quick_add("", now=NOW).title == "Untitled task"
        assert parse_quick_add("   ", now=NOW).title == "Untitled task"
        assert parse_quick_add(None, now=NOW).title == "Untitled task"

    def test_a_title_of_only_modifiers_falls_back_to_the_raw_text(self):
        parsed = parse_quick_add("#ops !3", now=NOW)
        assert parsed.title == "#ops !3"

    def test_recurrence_words(self):
        for word, expected in (("daily", "daily"), ("everyday", "daily"), ("annually", "yearly")):
            assert parse_quick_add(f"Chore {word}", now=NOW).recurrence == expected

    def test_unrecognised_tokens_are_kept(self):
        """Never silently drop the user's words."""

        parsed = parse_quick_add("Review PR #1234-abc for @someone soon", now=NOW)
        assert "Review PR" in parsed.title
        assert "soon" in parsed.title

    def test_has_due_property(self):
        assert parse_quick_add("Task tomorrow", now=NOW).has_due
        assert not parse_quick_add("Task", now=NOW).has_due


class TestCombineDueDatetime:
    def test_defaults_to_nine_am(self):
        parsed = parse_quick_add("Task tomorrow", now=NOW)
        assert combine_due_datetime(parsed).hour == 9

    def test_returns_none_without_a_date(self):
        assert combine_due_datetime(parse_quick_add("Task 5pm", now=NOW)) is None

    def test_result_is_timezone_aware(self):
        assert combine_due_datetime(parse_quick_add("Task tomorrow 3pm", now=NOW)).tzinfo is not None

    def test_dst_spring_forward_does_not_raise(self):
        """`make_aware` raises on non-existent local times; attaching the zone does not."""

        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
        parsed = parse_quick_add("Task 2026-03-08 2:30am", now=NOW)
        result = combine_due_datetime(parsed, tz=eastern)
        assert result is not None


class TestTaskCreation:
    def test_quick_add_creates_everything_it_implies(self, organization, task_list, user):
        task = create_task_from_quick_add(
            organization=organization,
            task_list=task_list,
            creator=user,
            raw="Send report tomorrow 5pm #Ops !3",
        )
        assert task.title == "Send report"
        assert task.priority == 3
        assert task.labels.count() == 1
        assert task.position > 0
        assert Activity.objects.filter(verb="task.created", target_id=str(task.pk)).exists()
        assert Reminder.objects.filter(task=task).exists()
        assert Job.objects.filter(handler="internal:send_reminder").exists()

    def test_labels_are_reused_not_duplicated(self, organization, task_list, user, label):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Thing #Ops"
        )
        assert task.labels.first().pk == label.pk
        assert Label.objects.filter(organization=organization, name="Ops").count() == 1

    def test_task_lands_in_the_first_column(self, organization, task_list, user, columns):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Thing"
        )
        assert task.column_id == columns[0].pk

    def test_no_due_date_means_no_reminder(self, organization, task_list, user):
        task = create_task_from_quick_add(
            organization=organization, task_list=task_list, creator=user, raw="Someday thing"
        )
        assert not Reminder.objects.filter(task=task).exists()


class TestPositioning:
    def test_next_position_appends(self, organization, task_list, user, columns):
        first = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=columns[0],
            creator=user,
            title="A",
            position=next_position(task_list=task_list, column=columns[0]),
        )
        second_position = next_position(task_list=task_list, column=columns[0])
        assert second_position > first.position

    def test_position_between_is_the_midpoint(self):
        assert position_between(Decimal("1000"), Decimal("2000")) == Decimal("1500")

    def test_position_between_handles_open_ends(self):
        assert position_between(None, Decimal("1000")) < Decimal("1000")
        assert position_between(Decimal("1000"), None) > Decimal("1000")
        assert position_between(None, None) > 0

    def test_needs_renumber_detects_a_collapsed_gap(self):
        assert needs_renumber(Decimal("1.000000"), Decimal("1.000001"))
        assert not needs_renumber(Decimal("1000"), Decimal("2000"))
        assert not needs_renumber(None, Decimal("1000"))

    def test_renumber_restores_a_clean_ladder(self, organization, task_list, user, columns):
        for index in range(3):
            Task.objects.create(
                organization=organization,
                task_list=task_list,
                column=columns[0],
                creator=user,
                title=f"T{index}",
                position=Decimal("1") + Decimal(index) / Decimal("1000000"),
            )
        renumber_column(task_list=task_list, column=columns[0])
        positions = list(
            Task.objects.filter(column=columns[0]).order_by("position").values_list("position", flat=True)
        )
        assert positions == [Decimal("1000"), Decimal("2000"), Decimal("3000")]


class TestOptimisticConcurrency:
    def test_a_stale_version_is_rejected(self, task, user):
        stale = task.version
        toggle_task_completion(task=task, actor=user)  # bumps to stale + 1
        with pytest.raises(ConcurrentModification):
            toggle_task_completion(task=task, actor=user, expected_version=stale)

    def test_the_current_version_is_accepted(self, task, user):
        toggle_task_completion(task=task, actor=user, expected_version=task.version)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_no_expected_version_skips_the_check(self, task, user):
        toggle_task_completion(task=task, actor=user, expected_version=None)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_update_bumps_the_version(self, task, user):
        before = task.version
        update_task(task=task, actor=user, title="Renamed")
        task.refresh_from_db()
        assert task.version == before + 1

    def test_a_no_op_update_does_not_bump_the_version(self, task, user):
        before = task.version
        update_task(task=task, actor=user, title=task.title)
        task.refresh_from_db()
        assert task.version == before


class TestCompletion:
    def test_completion_sets_the_timestamp_and_actor(self, task, user):
        toggle_task_completion(task=task, actor=user)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert task.completed_at is not None
        assert task.completed_by_id == user.pk

    def test_reopening_clears_them(self, task, user):
        toggle_task_completion(task=task, actor=user)
        toggle_task_completion(task=task, actor=user)
        task.refresh_from_db()
        assert task.status == Task.Status.OPEN
        assert task.completed_at is None
        assert task.completed_by is None

    def test_completion_cancels_armed_reminders(self, task, user):
        task.due_date = timezone.localdate() + timedelta(days=1)
        task.save(update_fields=["due_date"])
        schedule_reminder(task)
        assert Reminder.objects.filter(task=task, status=Reminder.Status.ARMED).exists()

        toggle_task_completion(task=task, actor=user)
        assert not Reminder.objects.filter(task=task, status=Reminder.Status.ARMED).exists()

    def test_completion_records_activity(self, task, user):
        toggle_task_completion(task=task, actor=user)
        assert Activity.objects.filter(verb="task.completed", target_id=str(task.pk)).exists()


class TestDependencies:
    def test_a_blocked_task_cannot_be_completed(self, organization, task_list, user, task):
        blocker = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Blocker", position=1
        )
        add_dependency(blocked_task=task, blocking_task=blocker)
        with pytest.raises(DependencyBlocked):
            toggle_task_completion(task=task, actor=user)

    def test_completing_the_blocker_unblocks(self, organization, task_list, user, task):
        blocker = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Blocker", position=1
        )
        add_dependency(blocked_task=task, blocking_task=blocker)
        toggle_task_completion(task=blocker, actor=user)
        toggle_task_completion(task=task, actor=user)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_self_dependency_is_rejected(self, task):
        with pytest.raises(ValidationError):
            add_dependency(blocked_task=task, blocking_task=task)

    def test_a_cycle_is_rejected(self, organization, task_list, user, task):
        second = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="B", position=1
        )
        third = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="C", position=2
        )
        add_dependency(blocked_task=task, blocking_task=second)
        add_dependency(blocked_task=second, blocking_task=third)
        # third -> task would close the loop.
        with pytest.raises(ValidationError, match="cycle"):
            add_dependency(blocked_task=third, blocking_task=task)

    def test_cross_tenant_dependency_is_rejected(self, task, other_task):
        with pytest.raises(ValidationError):
            add_dependency(blocked_task=task, blocking_task=other_task)

    def test_self_blocking_is_rejected_by_the_database(self, task, organization):
        with pytest.raises(IntegrityError), transaction.atomic():
            TaskDependency.objects.create(
                organization=organization, blocked_task=task, blocking_task=task
            )


class TestBoardMoves:
    def test_moving_to_a_done_column_completes_the_task(self, task, user, columns):
        done = next(column for column in columns if column.is_done)
        move_task(task=task, actor=user, column=done)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert task.completed_at is not None

    def test_moving_out_of_done_reopens(self, task, user, columns):
        done = next(column for column in columns if column.is_done)
        backlog = columns[0]
        move_task(task=task, actor=user, column=done)
        move_task(task=task, actor=user, column=backlog)
        task.refresh_from_db()
        assert task.status == Task.Status.OPEN
        assert task.completed_at is None

    def test_a_wip_limit_is_enforced(self, organization, task_list, user, columns):
        target = columns[1]
        Column.objects.filter(pk=target.pk).update(wip_limit=1)
        target.refresh_from_db()

        first = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=target,
            creator=user,
            title="Occupant",
            position=1000,
        )
        second = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=columns[0],
            creator=user,
            title="Newcomer",
            position=1000,
        )
        with pytest.raises(WipLimitExceeded):
            move_task(task=second, actor=user, column=target)

    def test_moving_to_a_foreign_list_column_is_rejected(self, organization, task, user):
        other_list = TaskList.objects.create(organization=organization, name="Other", position=9)
        other_columns = other_list.ensure_default_columns()
        with pytest.raises(ValidationError):
            move_task(task=task, actor=user, column=other_columns[0])

    def test_a_stale_version_blocks_the_move(self, task, user, columns):
        with pytest.raises(ConcurrentModification):
            move_task(task=task, actor=user, column=columns[1], expected_version=task.version + 5)

    def test_ordering_between_two_cards(self, organization, task_list, user, columns):
        column = columns[0]
        first = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=column,
            creator=user,
            title="First",
            position=Decimal("1000"),
        )
        third = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=column,
            creator=user,
            title="Third",
            position=Decimal("3000"),
        )
        mover = Task.objects.create(
            organization=organization,
            task_list=task_list,
            column=columns[1],
            creator=user,
            title="Mover",
            position=Decimal("1000"),
        )
        move_task(task=mover, actor=user, column=column, before_id=first.pk, after_id=third.pk)
        mover.refresh_from_db()
        assert first.position < mover.position < third.position

    def test_move_records_activity_only_on_a_column_change(self, task, user, columns):
        move_task(task=task, actor=user, column=columns[1])
        assert Activity.objects.filter(verb="task.moved").count() == 1
        move_task(task=task, actor=user, column=columns[1])
        assert Activity.objects.filter(verb="task.moved").count() == 1


class TestReminders:
    def test_a_reminder_fires_before_the_due_time(self, task):
        task.due_date = timezone.localdate() + timedelta(days=1)
        task.due_time = time(17, 0)
        task.save(update_fields=["due_date", "due_time"])

        reminder = schedule_reminder(task)
        assert reminder is not None
        assert reminder.remind_at < timezone.now() + timedelta(days=1, hours=17)
        assert reminder.job is not None

    def test_a_past_due_task_reminds_immediately(self, task):
        task.due_date = timezone.localdate() - timedelta(days=3)
        task.save(update_fields=["due_date"])
        reminder = schedule_reminder(task)
        assert reminder.remind_at <= timezone.now() + timedelta(seconds=5)

    def test_no_due_date_means_no_reminder(self, task):
        assert schedule_reminder(task) is None

    def test_rescheduling_cancels_the_old_reminder(self, task):
        task.due_date = timezone.localdate() + timedelta(days=1)
        task.save(update_fields=["due_date"])
        first = schedule_reminder(task)

        task.due_date = timezone.localdate() + timedelta(days=5)
        task.save(update_fields=["due_date"])
        second = schedule_reminder(task)

        # The superseded reminder is removed; its cancelled job is the record that it existed.
        assert not Reminder.objects.filter(pk=first.pk).exists()
        first.job.refresh_from_db()
        assert first.job.status == Job.Status.CANCELED
        assert second.status == Reminder.Status.ARMED
        assert Reminder.objects.filter(task=task).count() == 1

    def test_cancel_reminders_cancels_the_job_too(self, task):
        task.due_date = timezone.localdate() + timedelta(days=1)
        task.save(update_fields=["due_date"])
        reminder = schedule_reminder(task)
        job_id = reminder.job_id

        assert cancel_reminders(task) == 1
        assert Job.objects.get(pk=job_id).status == Job.Status.CANCELED


class TestModelConstraints:
    def test_due_time_without_a_date_is_rejected(self, organization, task_list, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            Task.objects.create(
                organization=organization,
                task_list=task_list,
                creator=user,
                title="Floating",
                due_time=time(17, 0),
            )

    def test_completed_without_a_timestamp_is_rejected(self, organization, task_list, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            Task.objects.create(
                organization=organization,
                task_list=task_list,
                creator=user,
                title="Broken",
                status=Task.Status.COMPLETED,
            )

    def test_a_column_from_another_list_fails_validation(self, organization, task, task_list):
        other_list = TaskList.objects.create(organization=organization, name="Elsewhere", position=8)
        foreign_column = other_list.ensure_default_columns()[0]
        task.column = foreign_column
        with pytest.raises(ValidationError):
            task.clean()

    def test_column_inherits_the_list_organization(self, organization, task_list):
        column = Column.objects.create(task_list=task_list, name="Derived", position=99)
        assert column.organization_id == organization.pk

    def test_column_organization_mismatch_fails_validation(self, other_organization, task_list):
        column = Column(organization=other_organization, task_list=task_list, name="Bad")
        with pytest.raises(ValidationError):
            column.clean()

    def test_duplicate_label_names_per_org_are_rejected(self, organization, label):
        with pytest.raises(IntegrityError), transaction.atomic():
            Label.objects.create(organization=organization, name="Ops")

    def test_the_same_label_name_in_another_tenant_is_fine(self, other_organization, label):
        Label.objects.create(organization=other_organization, name="Ops")


class TestReadModels:
    def test_workspace_counts(self, organization, task_list, user):
        today = timezone.localdate()
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Today", due_date=today
        )
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Overdue",
            due_date=today - timedelta(days=2),
        )
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Someday"
        )

        counts = workspace_counts(organization=organization)
        assert counts["open"] == 3
        assert counts["today"] == 1
        assert counts["overdue"] == 1
        assert counts["unscheduled"] == 1

    def test_counts_exclude_other_tenants(self, organization, other_task):
        assert workspace_counts(organization=organization)["open"] == 0

    def test_board_snapshot_groups_by_column(self, task, task_list, columns):
        lanes, unassigned = board_snapshot(task_list=task_list)
        assert len(lanes) == len(columns)
        assert task in lanes[0].cards
        assert lanes[0].total == 1
        assert unassigned.cards == []

    def test_archived_tasks_are_excluded_from_the_board(self, task, task_list, user):
        archive_task(task=task, actor=user)
        lanes, _ = board_snapshot(task_list=task_list)
        assert all(not lane.cards and lane.total == 0 for lane in lanes)


class TestTaskModelMethods:
    def test_is_overdue(self, task):
        assert not task.is_overdue
        task.due_date = timezone.localdate() - timedelta(days=1)
        assert task.is_overdue
        task.status = Task.Status.COMPLETED
        assert not task.is_overdue

    def test_archive_and_unarchive(self, task):
        task.archive()
        assert task.archived_at is not None
        assert task.status == Task.Status.ARCHIVED

        task.unarchive()
        assert task.archived_at is None
        assert task.status == Task.Status.OPEN

    def test_unarchive_restores_completed_state(self, task, user):
        task.complete(actor=user)
        task.archive()
        task.unarchive()
        assert task.status == Task.Status.COMPLETED

    def test_complete_is_idempotent(self, task, user):
        task.complete(actor=user)
        version = task.version
        task.complete(actor=user)
        assert task.version == version

    def test_subtask_toggle(self, task):
        from tasks.models import Subtask

        subtask = Subtask.objects.create(task=task, title="Step one")
        assert not subtask.is_done
        subtask.toggle()
        assert subtask.is_done
        subtask.toggle()
        assert not subtask.is_done


class TestTaskListHelpers:
    def test_ensure_default_columns_creates_a_board(self, organization):
        task_list = TaskList.objects.create(organization=organization, name="Fresh", position=5)
        created = task_list.ensure_default_columns()
        assert len(created) == 4
        assert any(column.is_done for column in created)

    def test_ensure_default_columns_is_idempotent(self, task_list, columns):
        assert len(task_list.ensure_default_columns()) == len(columns)
        assert task_list.columns.count() == len(columns)

    def test_project_area_from_another_tenant_fails_validation(self, organization, other_organization):
        from tasks.models import ProjectArea

        foreign = ProjectArea.objects.create(organization=other_organization, name="Theirs")
        task_list = TaskList(organization=organization, name="Mine", project_area=foreign)
        with pytest.raises(ValidationError):
            task_list.clean()
