"""Schedule evaluation, timezone fidelity, catch-up policy and double-fire protection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from queues.models import Job
from schedules.models import Schedule, ScheduleOccurrence
from schedules.services import (
    bootstrap_next_run,
    fire_schedule,
    next_run_after,
    parse_interval,
    preview_next_runs,
    scan_due_schedules,
    validate_expression,
)

pytestmark = pytest.mark.django_db


def make_schedule(organization, queue, **kwargs):
    defaults = {
        "organization": organization,
        "queue": queue,
        "name": kwargs.pop("name", "Test schedule"),
        "expression_type": Schedule.ExpressionType.CRON,
        "expression": "*/15 * * * *",
        "timezone": "UTC",
        "target_handler": "internal:schedule_fire",
    }
    defaults.update(kwargs)
    return Schedule.objects.create(**defaults)


class TestIntervalParsing:
    @pytest.mark.parametrize(
        "text,seconds",
        [
            ("30s", 30),
            ("90 seconds", 90),
            ("15m", 900),
            ("15 minutes", 900),
            ("2h", 7200),
            ("2 hours", 7200),
            ("1d", 86400),
            ("7 days", 604800),
        ],
    )
    def test_valid_intervals(self, text, seconds):
        assert parse_interval(text).total_seconds() == seconds

    @pytest.mark.parametrize("text", ["", "abc", "5x", "-1m", "1 fortnight", None])
    def test_invalid_intervals_raise(self, text):
        with pytest.raises(ValueError):
            parse_interval(text)

    def test_sub_minimum_interval_is_rejected(self):
        """A 5-second schedule would silently under-fire against a ~60s scanner."""

        with pytest.raises(ValueError, match="at least"):
            parse_interval("5s")

    def test_absurdly_long_interval_is_rejected(self):
        with pytest.raises(ValueError, match="at most"):
            parse_interval("400 days")


class TestExpressionValidation:
    def test_valid_cron_passes(self):
        validate_expression(Schedule.ExpressionType.CRON, "*/15 * * * *", "UTC")

    @pytest.mark.parametrize("expression", ["", "   ", "not a cron", "* * *", "99 * * * *"])
    def test_invalid_cron_raises(self, expression):
        with pytest.raises(ValueError):
            validate_expression(Schedule.ExpressionType.CRON, expression, "UTC")

    def test_unknown_timezone_raises(self):
        with pytest.raises(ValueError, match="timezone"):
            validate_expression(Schedule.ExpressionType.CRON, "* * * * *", "Mars/Olympus")

    def test_one_off_needs_a_time(self):
        with pytest.raises(ValueError):
            validate_expression(Schedule.ExpressionType.ONE_OFF, "", "UTC", next_run_at=None)
        validate_expression(
            Schedule.ExpressionType.ONE_OFF, "", "UTC", next_run_at=timezone.now()
        )

    def test_model_clean_rejects_a_bad_expression(self, organization, queue):
        schedule = Schedule(
            organization=organization,
            queue=queue,
            name="Bad",
            expression_type=Schedule.ExpressionType.CRON,
            expression="nonsense",
        )
        with pytest.raises(ValidationError):
            schedule.clean()

    def test_model_clean_rejects_a_foreign_queue(self, organization, other_organization):
        from queues.services import get_internal_queue

        schedule = Schedule(
            organization=organization,
            queue=get_internal_queue(other_organization),
            name="Cross",
            expression_type=Schedule.ExpressionType.CRON,
            expression="* * * * *",
        )
        with pytest.raises(ValidationError):
            schedule.clean()


class TestTimezoneHandling:
    def test_cron_is_evaluated_in_the_schedule_timezone(self, organization, queue):
        """Regression: the original code ignored Schedule.timezone entirely."""

        tokyo = make_schedule(
            organization, queue, name="Tokyo 9am", expression="0 9 * * *", timezone="Asia/Tokyo"
        )
        utc = make_schedule(
            organization, queue, name="UTC 9am", expression="0 9 * * *", timezone="UTC"
        )
        after = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)

        tokyo_run = next_run_after(tokyo, after)
        utc_run = next_run_after(utc, after)

        # Tokyo is UTC+9, so its 09:00 is 00:00 UTC — nine hours before the UTC schedule.
        assert tokyo_run.hour == 0
        assert utc_run.hour == 9
        assert tokyo_run != utc_run

    def test_dst_transition_keeps_local_wall_time(self, organization, queue):
        """"09:00 daily" must stay 09:00 local across a DST change, not drift by an hour."""

        import zoneinfo

        schedule = make_schedule(
            organization, queue, expression="0 9 * * *", timezone="America/New_York"
        )
        eastern = zoneinfo.ZoneInfo("America/New_York")

        before_dst = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        after_dst = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

        assert next_run_after(schedule, before_dst).astimezone(eastern).hour == 9
        assert next_run_after(schedule, after_dst).astimezone(eastern).hour == 9

    def test_returned_instants_are_utc(self, organization, queue):
        schedule = make_schedule(organization, queue, timezone="Australia/Sydney")
        run = next_run_after(schedule, timezone.now())
        assert run.utcoffset().total_seconds() == 0


class TestNextRun:
    def test_cron_advances(self, organization, queue):
        schedule = make_schedule(organization, queue, expression="*/15 * * * *")
        first = next_run_after(schedule, timezone.now())
        second = next_run_after(schedule, first)
        assert second > first
        assert (second - first).total_seconds() == 900

    def test_interval_advances_from_a_fixed_anchor(self, organization, queue):
        """Regression: without an anchor the cadence drifts by the scan latency each fire."""

        anchor = timezone.now() - timedelta(hours=1)
        schedule = make_schedule(
            organization,
            queue,
            expression_type=Schedule.ExpressionType.INTERVAL,
            expression="15m",
            last_run_at=anchor,
        )
        run = next_run_after(schedule, timezone.now())
        offset = (run - anchor).total_seconds()
        assert offset % 900 == 0

    def test_one_off_returns_none_once_past(self, organization, queue):
        schedule = make_schedule(
            organization,
            queue,
            expression_type=Schedule.ExpressionType.ONE_OFF,
            expression="",
            next_run_at=timezone.now() - timedelta(hours=1),
        )
        assert next_run_after(schedule, timezone.now()) is None

    def test_bootstrap_sets_next_run(self, organization, queue):
        schedule = make_schedule(organization, queue)
        assert schedule.next_run_at is None
        bootstrap_next_run(schedule)
        schedule.refresh_from_db()
        assert schedule.next_run_at is not None


class TestPreview:
    def test_preview_returns_ascending_runs(self, organization, queue):
        schedule = make_schedule(organization, queue)
        runs = preview_next_runs(schedule, 5)
        assert len(runs) == 5
        assert runs == sorted(runs)

    def test_preview_never_raises_on_a_bad_expression(self, organization, queue):
        schedule = make_schedule(organization, queue)
        Schedule.objects.filter(pk=schedule.pk).update(expression="garbage")
        schedule.refresh_from_db()
        assert preview_next_runs(schedule) == []

    def test_preview_count_is_bounded(self, organization, queue):
        schedule = make_schedule(organization, queue)
        assert len(preview_next_runs(schedule, 10_000)) <= 50

    def test_preview_of_zero_is_empty(self, organization, queue):
        assert preview_next_runs(make_schedule(organization, queue), 0) == []


class TestFiring:
    def test_firing_creates_an_occurrence_and_a_job(self, organization, queue):
        schedule = make_schedule(
            organization, queue, next_run_at=timezone.now() - timedelta(minutes=1)
        )
        result = fire_schedule(schedule)
        assert result.occurrences_created == 1
        assert result.jobs_enqueued == 1
        assert ScheduleOccurrence.objects.filter(schedule=schedule).count() == 1
        assert Job.objects.filter(source=f"schedule:{schedule.pk}").count() == 1

    def test_occurrence_identity_prevents_a_double_fire(self, organization, queue):
        schedule = make_schedule(organization, queue)
        instant = timezone.now().replace(second=0, microsecond=0)
        ScheduleOccurrence.objects.create(
            organization=organization, schedule=schedule, scheduled_time=instant
        )
        with pytest.raises(IntegrityError):
            ScheduleOccurrence.objects.create(
                organization=organization, schedule=schedule, scheduled_time=instant
            )

    def test_a_concurrent_second_fire_is_a_no_op(self, organization, queue):
        """Two scheduler instances on the same tick: one wins, one silently skips."""

        schedule = make_schedule(
            organization, queue, next_run_at=timezone.now() - timedelta(minutes=1)
        )
        first = fire_schedule(schedule)
        schedule.refresh_from_db()

        # Rewind so the same instant is due again, simulating the losing racer.
        Schedule.objects.filter(pk=schedule.pk).update(
            next_run_at=ScheduleOccurrence.objects.get(schedule=schedule).scheduled_time
        )
        schedule.refresh_from_db()
        second = fire_schedule(schedule)

        assert first.occurrences_created == 1
        assert second.skipped == 1
        assert second.jobs_enqueued == 0

    def test_the_enqueued_job_is_idempotent_on_the_instant(self, organization, queue):
        schedule = make_schedule(
            organization, queue, next_run_at=timezone.now() - timedelta(minutes=1)
        )
        fire_schedule(schedule)
        job = Job.objects.get(source=f"schedule:{schedule.pk}")
        assert job.idempotency_key.startswith(f"schedule:{schedule.pk}:")
        # The key is the scheduled instant, not the wall clock.
        assert str(job.payload["scheduled_time"])[:10] in job.idempotency_key

    def test_a_future_schedule_does_not_fire(self, organization, queue):
        schedule = make_schedule(
            organization, queue, next_run_at=timezone.now() + timedelta(hours=1)
        )
        assert fire_schedule(schedule).occurrences_created == 0

    def test_repeated_evaluation_failures_deactivate_the_schedule(self, organization, queue):
        schedule = make_schedule(organization, queue, next_run_at=timezone.now() - timedelta(minutes=1))
        Schedule.objects.filter(pk=schedule.pk).update(expression="garbage")

        for _ in range(3):
            schedule.refresh_from_db()
            fire_schedule(schedule)

        schedule.refresh_from_db()
        assert schedule.active is False
        assert schedule.consecutive_failures >= 3
        assert schedule.last_error


class TestCatchUpPolicies:
    # A fixed "now" seven and a half minutes past a quarter-hour boundary. Deriving it from
    # the wall clock makes SKIP flaky: it fires when the newest missed tick happens to fall
    # inside the freshness horizon, which depends on what minute the suite runs at.
    NOW = datetime(2026, 6, 15, 14, 7, 30, tzinfo=UTC)

    def _overdue(self, organization, queue, policy):
        return make_schedule(
            organization,
            queue,
            expression="*/15 * * * *",
            catch_up_policy=policy,
            # Two hours behind: eight missed 15-minute ticks.
            next_run_at=self.NOW - timedelta(hours=2),
        )

    def test_run_once_collapses_the_backlog(self, organization, queue):
        schedule = self._overdue(organization, queue, Schedule.CatchUpPolicy.RUN_ONCE)
        assert fire_schedule(schedule, now=self.NOW).occurrences_created == 1

    def test_skip_drops_stale_ticks_entirely(self, organization, queue):
        schedule = self._overdue(organization, queue, Schedule.CatchUpPolicy.SKIP)
        assert fire_schedule(schedule, now=self.NOW).occurrences_created == 0

    def test_skip_still_fires_a_fresh_tick(self, organization, queue):
        """SKIP means "abandon stale work", not "never fire"."""

        fresh = self.NOW.replace(minute=15, second=10)
        schedule = make_schedule(
            organization,
            queue,
            expression="*/15 * * * *",
            catch_up_policy=Schedule.CatchUpPolicy.SKIP,
            next_run_at=fresh.replace(second=0),
        )
        assert fire_schedule(schedule, now=fresh).occurrences_created == 1

    def test_run_all_replays_every_missed_tick(self, organization, queue):
        schedule = self._overdue(organization, queue, Schedule.CatchUpPolicy.RUN_ALL)
        assert fire_schedule(schedule, now=self.NOW).occurrences_created >= 7

    def test_run_all_is_bounded(self, organization, queue, settings):
        """A scheduler down for a week must not enqueue ten thousand jobs on restart."""

        settings.SCHEDULE_CATCH_UP_MAX_OCCURRENCES = 5
        schedule = make_schedule(
            organization,
            queue,
            expression="* * * * *",
            catch_up_policy=Schedule.CatchUpPolicy.RUN_ALL,
            next_run_at=self.NOW - timedelta(days=7),
        )
        assert fire_schedule(schedule, now=self.NOW).occurrences_created <= 5


class TestScan:
    def test_scan_fires_due_schedules(self, organization, queue):
        make_schedule(
            organization, queue, name="Due", next_run_at=timezone.now() - timedelta(minutes=1)
        )
        make_schedule(
            organization, queue, name="Later", next_run_at=timezone.now() + timedelta(days=1)
        )
        totals = scan_due_schedules()
        assert totals["occurrences"] == 1

    def test_inactive_schedules_are_skipped(self, organization, queue):
        make_schedule(
            organization,
            queue,
            active=False,
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        assert scan_due_schedules()["schedules"] == 0

    def test_deleted_workspaces_are_skipped(self, organization, queue):
        from kernel.models import Organization

        make_schedule(organization, queue, next_run_at=timezone.now() - timedelta(minutes=1))
        Organization.objects.filter(pk=organization.pk).update(deleted_at=timezone.now())
        assert scan_due_schedules()["schedules"] == 0

    def test_a_never_evaluated_schedule_is_picked_up(self, organization, queue):
        make_schedule(organization, queue, expression="* * * * *", next_run_at=None)
        assert scan_due_schedules()["schedules"] == 1
