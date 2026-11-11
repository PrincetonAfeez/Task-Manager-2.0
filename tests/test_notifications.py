"""Notification fan-out, email queueing, digests and analytics."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from kernel.models import Notification, NotificationPreference
from queues.models import Job
from tasks.analytics import workspace_analytics
from tasks.digests import (
    DIGEST_HOUR,
    build_digest,
    enqueue_due_digests,
    render_digest,
    send_digest_for_member,
)
from tasks.models import Comment, Task
from tasks.notifications import (
    deliver_notification,
    get_preferences,
    notify_assignment,
    notify_comment,
    notify_mentions,
    queue_email,
)

pytestmark = pytest.mark.django_db


class TestDeliverNotification:
    def test_creates_an_in_app_notification(self, organization, user):
        notification = deliver_notification(
            organization=organization, user=user, title="Ping", natural_key="n1"
        )
        assert notification is not None
        assert notification.status == Notification.Status.DELIVERED

    def test_redelivery_returns_the_existing_row(self, organization, user):
        """The unique (org, user, natural_key) constraint is the idempotency barrier."""

        first = deliver_notification(
            organization=organization, user=user, title="Ping", natural_key="dup"
        )
        second = deliver_notification(
            organization=organization, user=user, title="Ping again", natural_key="dup"
        )
        assert second.pk == first.pk
        assert Notification.objects.filter(natural_key="dup").count() == 1

    def test_in_app_can_be_disabled(self, organization, user):
        prefs = get_preferences(organization, user)
        prefs.in_app_enabled = False
        prefs.save()
        assert deliver_notification(
            organization=organization, user=user, title="Ping", natural_key="off"
        ) is None

    def test_no_recipient_is_a_no_op(self, organization):
        assert deliver_notification(
            organization=organization, user=None, title="Ping", natural_key="none"
        ) is None

    def test_an_email_job_is_queued_when_preferences_allow(self, organization, user):
        deliver_notification(
            organization=organization,
            user=user,
            title="Reminder",
            category=Notification.Category.REMINDER,
            natural_key="email-yes",
        )
        assert Job.objects.filter(handler="internal:send_email").exists()

    def test_no_email_when_the_category_is_disabled(self, organization, user):
        deliver_notification(
            organization=organization,
            user=user,
            title="Comment",
            category=Notification.Category.COMMENT,
            natural_key="email-no",
        )
        assert not Job.objects.filter(handler="internal:send_email").exists()

    def test_no_email_after_unsubscribing(self, organization, user):
        prefs = get_preferences(organization, user)
        prefs.unsubscribed_at = timezone.now()
        prefs.save()
        deliver_notification(
            organization=organization,
            user=user,
            title="Reminder",
            category=Notification.Category.REMINDER,
            natural_key="unsub",
        )
        assert not Job.objects.filter(handler="internal:send_email").exists()

    def test_quiet_hours_suppress_the_email_but_keep_the_in_app(self, organization, user):
        from datetime import time

        prefs = get_preferences(organization, user)
        now = timezone.localtime()
        # A window that definitely contains "now", wrapping the whole day.
        prefs.quiet_hours_start = time(0, 0)
        prefs.quiet_hours_end = time(23, 59)
        prefs.save()

        notification = deliver_notification(
            organization=organization,
            user=user,
            title="Reminder",
            category=Notification.Category.REMINDER,
            natural_key="quiet",
        )
        assert notification is not None
        assert not Job.objects.filter(handler="internal:send_email").exists()

    def test_a_user_without_an_email_gets_no_email_job(self, organization, member_user, member):
        member_user.email = ""
        member_user.save(update_fields=["email"])
        deliver_notification(
            organization=organization,
            user=member_user,
            title="Reminder",
            category=Notification.Category.REMINDER,
            natural_key="no-email",
        )
        assert not Job.objects.filter(handler="internal:send_email").exists()


class TestQueueEmail:
    def test_queues_a_durable_job_rather_than_sending_inline(self, organization):
        """An SMTP timeout inside a request either blocks the user or loses the message."""

        queue_email(
            organization=organization,
            to=["a@example.com"],
            subject="Hi",
            body="Body",
            natural_key="email:1",
        )
        job = Job.objects.get(handler="internal:send_email")
        assert job.payload["to"] == ["a@example.com"]
        assert len(mail.outbox) == 0

    def test_queueing_is_idempotent(self, organization):
        for _ in range(3):
            queue_email(
                organization=organization,
                to=["a@example.com"],
                subject="Hi",
                body="Body",
                natural_key="email:same",
            )
        assert Job.objects.filter(handler="internal:send_email").count() == 1


class TestEmailHandler:
    def _run(self, organization):
        from queues.runtime import run_once
        from queues.services import get_internal_queue

        return run_once(queue=get_internal_queue(organization))

    def test_the_handler_actually_sends(self, organization):
        queue_email(
            organization=organization,
            to=["dest@example.com"],
            subject="Subject line",
            body="Body text",
            natural_key="send:1",
        )
        self._run(organization)
        assert len(mail.outbox) == 1
        assert mail.outbox[0].subject == "Subject line"

    def test_a_redelivered_job_does_not_send_twice(self, organization):

        queue_email(
            organization=organization,
            to=["dest@example.com"],
            subject="Once",
            body="Body",
            natural_key="send:once",
        )
        self._run(organization)
        assert len(mail.outbox) == 1

        # Force the job back into the pool and run it again.
        Job.objects.filter(handler="internal:send_email").update(
            status=Job.Status.AVAILABLE, lease_token="", available_at=timezone.now()
        )
        self._run(organization)
        assert len(mail.outbox) == 1  # the effect marker held

    def test_a_send_failure_releases_the_marker_so_the_retry_can_send(self, organization):
        """Otherwise the retry sees "already sent" and the mail is lost permanently."""

        from unittest.mock import patch

        from queues.models import EffectMarker

        queue_email(
            organization=organization,
            to=["dest@example.com"],
            subject="Flaky",
            body="Body",
            natural_key="send:flaky",
        )
        with patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")
        ):
            self._run(organization)

        assert len(mail.outbox) == 0
        assert not EffectMarker.objects.filter(effect_type="email").exists()


class TestNotifyHelpers:
    def test_assignment_notifies_the_assignee(self, task, user, member):
        task.assignee = member.user
        task.save(update_fields=["assignee"])
        notify_assignment(task=task, actor=user)
        assert Notification.objects.filter(
            user=member.user, category=Notification.Category.ASSIGNMENT
        ).exists()

    def test_assigning_yourself_notifies_nobody(self, task, user):
        task.assignee = user
        task.save(update_fields=["assignee"])
        notify_assignment(task=task, actor=user)
        assert not Notification.objects.filter(category=Notification.Category.ASSIGNMENT).exists()

    def test_an_unassigned_task_notifies_nobody(self, task, user):
        notify_assignment(task=task, actor=user)
        assert Notification.objects.count() == 0

    def test_a_comment_notifies_the_assignee_and_creator_once_each(
        self, task, user, member, viewer
    ):
        task.assignee = member.user
        task.save(update_fields=["assignee"])
        comment = Comment.objects.create(
            organization=task.organization, task=task, author=viewer.user, body="Hello"
        )
        notify_comment(comment=comment)

        recipients = set(
            Notification.objects.filter(category=Notification.Category.COMMENT).values_list(
                "user_id", flat=True
            )
        )
        assert recipients == {member.user_id, user.pk}

    def test_a_comment_never_notifies_its_own_author(self, task, user):
        comment = Comment.objects.create(
            organization=task.organization, task=task, author=user, body="Note to self"
        )
        notify_comment(comment=comment)
        assert not Notification.objects.filter(user=user).exists()

    def test_mentions_notify_workspace_members(self, task, user, member, organization):
        notify_mentions(
            organization=organization,
            task=task,
            mentions=[member.user.get_username()],
            actor=user,
        )
        assert Notification.objects.filter(
            user=member.user, category=Notification.Category.MENTION
        ).exists()

    def test_mentioning_a_non_member_is_ignored_not_an_error(
        self, task, user, organization, other_user
    ):
        notify_mentions(
            organization=organization,
            task=task,
            mentions=[other_user.get_username()],
            actor=user,
        )
        assert not Notification.objects.filter(user=other_user).exists()

    def test_an_unknown_handle_is_ignored(self, task, user, organization):
        notify_mentions(
            organization=organization, task=task, mentions=["nobody-at-all"], actor=user
        )
        assert Notification.objects.count() == 0


class TestDigests:
    def test_the_digest_summarises_the_right_buckets(self, organization, task_list, user):
        today = timezone.localdate()
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Overdue",
            due_date=today - timedelta(days=2),
        )
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Today",
            due_date=today,
        )
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Someday",
        )

        summary = build_digest(organization=organization, user=user)
        assert len(summary["overdue"]) == 1
        assert len(summary["due_today"]) == 1
        assert summary["unscheduled_count"] == 1
        assert summary["has_content"]

    def test_a_digest_with_nothing_in_it_is_marked_empty(self, organization, user):
        assert not build_digest(organization=organization, user=user)["has_content"]

    def test_rendering_produces_a_subject_and_body(self, organization, task_list, user):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Due now",
            due_date=timezone.localdate(),
        )
        subject, body = render_digest(build_digest(organization=organization, user=user))
        assert organization.name in subject
        assert "Due now" in body

    def test_an_empty_digest_is_not_sent(self, organization, user, membership):
        """"You have nothing to do" every morning is how a digest gets filtered away."""

        result = send_digest_for_member(membership.pk)
        assert result["skipped"] == "empty_digest"
        assert len(mail.outbox) == 0

    def test_a_digest_with_content_is_sent(self, organization, task_list, user, membership):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Due today",
            due_date=timezone.localdate(),
        )
        assert send_digest_for_member(membership.pk)["sent"] is True
        assert len(mail.outbox) == 1

    def test_the_same_digest_is_never_sent_twice(self, organization, task_list, user, membership):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            assignee=user,
            title="Due today",
            due_date=timezone.localdate(),
        )
        send_digest_for_member(membership.pk)
        assert send_digest_for_member(membership.pk)["skipped"] == "already_sent"
        assert len(mail.outbox) == 1

    def test_digest_off_is_honoured(self, organization, task_list, user, membership):
        prefs = get_preferences(organization, user)
        prefs.digest = NotificationPreference.Digest.OFF
        prefs.save()
        assert send_digest_for_member(membership.pk)["skipped"] == "digest_disabled"

    def test_a_missing_membership_is_handled(self):
        assert send_digest_for_member(999999)["skipped"] == "membership_missing"

    def test_queueing_respects_the_local_digest_hour(self, organization, user, membership):
        import zoneinfo
        from datetime import datetime

        get_preferences(organization, user)
        utc = zoneinfo.ZoneInfo("UTC")

        at_hour = datetime(2026, 6, 15, DIGEST_HOUR, 0, tzinfo=utc)
        assert enqueue_due_digests(now=at_hour)["queued"] == 1

        off_hour = datetime(2026, 6, 15, (DIGEST_HOUR + 5) % 24, 0, tzinfo=utc)
        assert enqueue_due_digests(now=off_hour)["queued"] == 0

    def test_weekly_digests_only_queue_on_monday(self, organization, user, membership):
        import zoneinfo
        from datetime import datetime

        prefs = get_preferences(organization, user)
        prefs.digest = NotificationPreference.Digest.WEEKLY
        prefs.save()
        utc = zoneinfo.ZoneInfo("UTC")

        # 15 June 2026 is a Monday; the 16th is a Tuesday.
        assert enqueue_due_digests(
            now=datetime(2026, 6, 15, DIGEST_HOUR, 0, tzinfo=utc)
        )["queued"] == 1
        assert enqueue_due_digests(
            now=datetime(2026, 6, 16, DIGEST_HOUR, 0, tzinfo=utc)
        )["queued"] == 0


class TestAnalytics:
    def test_headline_counts(self, organization, task_list, user):
        today = timezone.localdate()
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Open"
        )
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Overdue",
            due_date=today - timedelta(days=1),
        )
        done = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Done"
        )
        done.complete(actor=user)

        report = workspace_analytics(organization=organization, days=30)
        assert report["counts"]["open"] == 2
        assert report["counts"]["overdue"] == 1
        assert report["counts"]["completed"] == 1

    def test_the_overdue_ratio_is_a_percentage(self, organization, task_list, user):
        Task.objects.create(
            organization=organization,
            task_list=task_list,
            creator=user,
            title="Overdue",
            due_date=timezone.localdate() - timedelta(days=1),
        )
        Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Fine"
        )
        assert workspace_analytics(organization=organization)["overdue_ratio"] == 50.0

    def test_cycle_time_is_reported(self, organization, task_list, user):
        task = Task.objects.create(
            organization=organization, task_list=task_list, creator=user, title="Timed"
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(hours=10))
        task.refresh_from_db()
        task.complete(actor=user)

        report = workspace_analytics(organization=organization)
        assert report["avg_cycle_hours"] is not None
        assert report["avg_cycle_hours"] >= 9

    def test_analytics_are_tenant_scoped(self, organization, other_task):
        assert workspace_analytics(organization=organization)["counts"]["open"] == 0

    def test_an_empty_workspace_does_not_divide_by_zero(self, organization):
        report = workspace_analytics(organization=organization)
        assert report["overdue_ratio"] == 0.0
        assert report["job_success_rate"] is None

    def test_job_success_rate(self, organization, queue):
        from queues.services import enqueue_job

        for index in range(4):
            result = enqueue_job(
                organization=organization,
                queue=queue,
                source="analytics",
                handler_type=Job.HandlerType.INTERNAL,
                handler="internal:send_notification",
                payload={},
                idempotency_key=f"analytics-{index}",
            )
            if index < 3:
                Job.objects.filter(pk=result.job.pk).update(status=Job.Status.SUCCEEDED)

        assert workspace_analytics(organization=organization)["job_success_rate"] == 75.0
