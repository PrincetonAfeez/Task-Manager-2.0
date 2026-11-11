"""Recurrence engine.

The scaffold parsed "weekly" out of quick-add and discarded it; ``RecurrenceRule`` had no
writers at all. These tests pin the behaviour of the engine that closes that loop.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from tasks.models import RecurrenceRule, Task
from tasks.recurrence import (
    add_months,
    materialize_next_occurrence,
    next_occurrence_date,
    sweep_due_recurrences,
)
from tasks.services import create_recurrence, create_task_from_quick_add, toggle_task_completion

pytestmark = pytest.mark.django_db


def make_rule(task, **kwargs):
    defaults = {
        "organization": task.organization,
        "source_task": task,
        "frequency": RecurrenceRule.Frequency.DAILY,
        "interval": 1,
        "next_occurrence_date": timezone.localdate(),
    }
    defaults.update(kwargs)
    return RecurrenceRule.objects.create(**defaults)


class TestAddMonths:
    def test_simple_addition(self):
        assert add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)

    def test_clamps_to_the_last_valid_day(self):
        """31 January + 1 month is 28 February, not an error and not 3 March."""

        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year

    def test_wraps_the_year(self):
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)

    def test_twelve_months_is_a_year(self):
        assert add_months(date(2026, 6, 10), 12) == date(2027, 6, 10)


class TestNextOccurrenceDate:
    def test_daily(self, task):
        rule = make_rule(task, next_occurrence_date=date(2026, 3, 1))
        assert next_occurrence_date(rule) == date(2026, 3, 2)

    def test_daily_with_interval(self, task):
        rule = make_rule(task, interval=3, next_occurrence_date=date(2026, 3, 1))
        assert next_occurrence_date(rule) == date(2026, 3, 4)

    def test_weekly_without_weekdays(self, task):
        rule = make_rule(
            task, frequency=RecurrenceRule.Frequency.WEEKLY, next_occurrence_date=date(2026, 3, 2)
        )
        assert next_occurrence_date(rule) == date(2026, 3, 9)

    def test_weekly_with_a_weekday_set(self, task):
        # Mondays and Thursdays; starting from Monday 2 March 2026.
        rule = make_rule(
            task,
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[1, 4],
            next_occurrence_date=date(2026, 3, 2),
        )
        assert next_occurrence_date(rule) == date(2026, 3, 5)  # Thursday same week

    def test_weekly_weekday_set_wraps_to_the_next_week(self, task):
        rule = make_rule(
            task,
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[1, 4],
            next_occurrence_date=date(2026, 3, 5),  # Thursday
        )
        assert next_occurrence_date(rule) == date(2026, 3, 9)  # following Monday

    def test_monthly(self, task):
        rule = make_rule(
            task, frequency=RecurrenceRule.Frequency.MONTHLY, next_occurrence_date=date(2026, 1, 31)
        )
        assert next_occurrence_date(rule) == date(2026, 2, 28)

    def test_yearly(self, task):
        rule = make_rule(
            task, frequency=RecurrenceRule.Frequency.YEARLY, next_occurrence_date=date(2026, 5, 4)
        )
        assert next_occurrence_date(rule) == date(2027, 5, 4)

    def test_until_date_ends_the_series(self, task):
        rule = make_rule(
            task, next_occurrence_date=date(2026, 3, 1), until_date=date(2026, 3, 1)
        )
        assert next_occurrence_date(rule) is None


class TestExhaustion:
    def test_inactive_rule_is_exhausted(self, task):
        assert make_rule(task, active=False).is_exhausted

    def test_max_occurrences_exhausts(self, task):
        # source_task is a OneToOne, so a task can only ever carry one rule.
        rule = make_rule(task, max_occurrences=3, occurrences_created=3)
        assert rule.is_exhausted

        RecurrenceRule.objects.filter(pk=rule.pk).update(occurrences_created=2)
        rule.refresh_from_db()
        assert not rule.is_exhausted

    def test_until_date_ends_the_series_after_the_last_occurrence(self, task):
        """The DB check keeps ``until_date >= next_occurrence_date``, so exhaustion is
        reached by advancing past the end, not by constructing an inverted rule."""

        target = timezone.localdate()
        rule = make_rule(task, next_occurrence_date=target, until_date=target)
        assert not rule.is_exhausted

        materialize_next_occurrence(rule)
        rule.refresh_from_db()
        assert rule.active is False
        assert rule.is_exhausted

    def test_inverted_until_date_is_rejected_by_the_database(self, task):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            RecurrenceRule.objects.create(
                organization=task.organization,
                source_task=task,
                frequency=RecurrenceRule.Frequency.DAILY,
                next_occurrence_date=timezone.localdate() + timedelta(days=5),
                until_date=timezone.localdate(),
            )


class TestMaterialisation:
    def test_creates_a_task_for_the_occurrence_date(self, task):
        target = timezone.localdate() + timedelta(days=1)
        rule = make_rule(task, next_occurrence_date=target)
        created = materialize_next_occurrence(rule)

        assert created is not None
        assert created.due_date == target
        assert created.title == task.title
        assert created.recurrence_source_id == task.pk
        assert created.organization_id == task.organization_id

    def test_copies_labels_and_assignee(self, task, label, member):
        task.labels.add(label)
        task.assignee = member.user
        task.save(update_fields=["assignee"])

        created = materialize_next_occurrence(make_rule(task))
        assert list(created.labels.all()) == [label]
        assert created.assignee_id == member.user.pk

    def test_advances_the_rule(self, task):
        rule = make_rule(task, next_occurrence_date=date(2026, 3, 1))
        materialize_next_occurrence(rule)
        rule.refresh_from_db()
        assert rule.next_occurrence_date == date(2026, 3, 2)
        assert rule.occurrences_created == 1

    def test_is_idempotent_for_the_same_date(self, task):
        """A redelivered job must not create a second copy of the same occurrence."""

        target = timezone.localdate() + timedelta(days=1)
        rule = make_rule(task, next_occurrence_date=target)
        first = materialize_next_occurrence(rule)

        rule.refresh_from_db()
        RecurrenceRule.objects.filter(pk=rule.pk).update(next_occurrence_date=target)
        rule.refresh_from_db()
        second = materialize_next_occurrence(rule)

        assert first is not None
        assert second is None
        assert Task.objects.filter(recurrence_source=task, due_date=target).count() == 1

    def test_an_exhausted_rule_produces_nothing(self, task):
        assert materialize_next_occurrence(make_rule(task, active=False)) is None

    def test_reaching_max_occurrences_deactivates_the_rule(self, task):
        rule = make_rule(task, max_occurrences=1)
        materialize_next_occurrence(rule)
        rule.refresh_from_db()
        assert rule.active is False

    def test_the_new_task_gets_a_reminder(self, task):
        from tasks.models import Reminder

        created = materialize_next_occurrence(make_rule(task))
        assert Reminder.objects.filter(task=created).exists()


class TestSweep:
    def test_sweep_materialises_due_rules(self, task):
        make_rule(task, next_occurrence_date=timezone.localdate())
        assert sweep_due_recurrences()["created"] == 1

    def test_future_rules_are_untouched(self, task):
        make_rule(task, next_occurrence_date=timezone.localdate() + timedelta(days=5))
        assert sweep_due_recurrences()["created"] == 0

    def test_a_long_dormant_rule_fast_forwards_instead_of_flooding(self, task):
        """A daily rule dormant for a year must not create 365 tasks in one sweep."""

        make_rule(task, next_occurrence_date=timezone.localdate() - timedelta(days=365))
        result = sweep_due_recurrences()
        assert result["created"] <= 1
        assert Task.objects.filter(recurrence_source=task).count() <= 1

    def test_deleted_workspaces_are_skipped(self, task, organization):
        from kernel.models import Organization

        make_rule(task, next_occurrence_date=timezone.localdate())
        Organization.objects.filter(pk=organization.pk).update(deleted_at=timezone.now())
        assert sweep_due_recurrences()["scanned"] == 0


class TestQuickAddIntegration:
    def test_quick_add_creates_a_recurrence_rule(self, organization, task_list, user):
        """Regression: the parser extracted "weekly" and the service discarded it."""

        created = create_task_from_quick_add(
            organization=organization,
            task_list=task_list,
            creator=user,
            raw="Publish the changelog weekly",
        )
        rule = RecurrenceRule.objects.get(source_task=created)
        assert rule.frequency == RecurrenceRule.Frequency.WEEKLY
        assert rule.active

    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("daily", RecurrenceRule.Frequency.DAILY),
            ("weekly", RecurrenceRule.Frequency.WEEKLY),
            ("monthly", RecurrenceRule.Frequency.MONTHLY),
            ("yearly", RecurrenceRule.Frequency.YEARLY),
        ],
    )
    def test_each_frequency_word(self, organization, task_list, user, phrase, expected):
        created = create_task_from_quick_add(
            organization=organization,
            task_list=task_list,
            creator=user,
            raw=f"Recurring chore {phrase}",
        )
        assert RecurrenceRule.objects.get(source_task=created).frequency == expected

    def test_completion_queues_the_next_occurrence(self, task, user, organization):
        from queues.models import Job

        create_recurrence(task=task, frequency="daily")
        toggle_task_completion(task=task, actor=user)
        assert Job.objects.filter(handler="internal:materialize_recurrence").count() == 1

    def test_an_unsupported_frequency_is_rejected(self, task):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            create_recurrence(task=task, frequency="hourly")


class TestValidation:
    def test_weekday_values_are_validated(self, task):
        from django.core.exceptions import ValidationError

        rule = RecurrenceRule(
            organization=task.organization,
            source_task=task,
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[0, 9],
            next_occurrence_date=timezone.localdate(),
        )
        with pytest.raises(ValidationError):
            rule.clean()

    def test_until_date_cannot_precede_the_next_occurrence(self, task):
        from django.core.exceptions import ValidationError

        rule = RecurrenceRule(
            organization=task.organization,
            source_task=task,
            frequency=RecurrenceRule.Frequency.DAILY,
            next_occurrence_date=timezone.localdate(),
            until_date=timezone.localdate() - timedelta(days=1),
        )
        with pytest.raises(ValidationError):
            rule.clean()
