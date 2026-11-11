"""Regressions found by running the product under load.

Both defects here were invisible to functional tests: every assertion passed with a handful
of rows. They only appeared against a 50,000-task corpus and a 2,000-job backlog on the
containerised stack, so each test pins the *shape* that made the difference — bounded work
per request, and more than one claim per dispatcher tick.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from queues.models import Job
from queues.services import enqueue_job
from tasks.models import Task
from tasks.services import BOARD_LANE_PAGE_SIZE, board_lane_page, board_snapshot

pytestmark = pytest.mark.django_db


def _fill(organization, task_list, user, column, count, *, prefix="Card"):
    return Task.objects.bulk_create(
        [
            Task(
                organization=organization,
                task_list=task_list,
                column=column,
                creator=user,
                title=f"{prefix} {index:04d}",
                position=(index + 1) * 1000,
            )
            for index in range(count)
        ]
    )


class TestBoardIsBounded:
    def test_a_lane_shows_one_page_and_reports_its_true_size(
        self, organization, task_list, user, columns
    ):
        _fill(organization, task_list, user, columns[0], BOARD_LANE_PAGE_SIZE + 25)

        lanes, unassigned = board_snapshot(task_list=task_list)

        first = lanes[0]
        assert len(first.cards) == BOARD_LANE_PAGE_SIZE
        assert first.total == BOARD_LANE_PAGE_SIZE + 25
        assert first.remaining == 25
        assert first.next_offset == BOARD_LANE_PAGE_SIZE
        assert unassigned.total == 0

    def test_lanes_are_limited_independently(self, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 7, prefix="A")
        _fill(organization, task_list, user, columns[1], 4, prefix="B")
        _fill(organization, task_list, user, None, 6, prefix="Loose")

        lanes, unassigned = board_snapshot(task_list=task_list, per_lane=5)

        assert [len(lane.cards) for lane in lanes[:2]] == [5, 4]
        assert [lane.total for lane in lanes[:2]] == [7, 4]
        assert len(unassigned.cards) == 5
        assert unassigned.remaining == 1

    def test_cards_keep_board_order(self, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 12)

        lanes, _ = board_snapshot(task_list=task_list, per_lane=5)

        assert [card.title for card in lanes[0].cards] == [f"Card {n:04d}" for n in range(5)]

    def test_pages_cover_the_lane_exactly_once(self, organization, task_list, user, columns):
        created = _fill(organization, task_list, user, columns[0], 23)

        lanes, _ = board_snapshot(task_list=task_list, per_lane=10)
        seen = [card.pk for card in lanes[0].cards]
        offset = lanes[0].next_offset
        while True:
            page = board_lane_page(task_list=task_list, column=columns[0], offset=offset, limit=10)
            seen.extend(card.pk for card in page.cards)
            if not page.remaining:
                break
            offset = page.next_offset

        assert sorted(seen) == sorted(task.pk for task in created)
        assert len(seen) == len(set(seen))

    def test_query_count_depends_on_columns_not_on_tasks(self, organization, task_list, user, columns):
        for column in columns:
            _fill(organization, task_list, user, column, 3, prefix=f"Few{column.pk}")
        with CaptureQueriesContext(connection) as small:
            board_snapshot(task_list=task_list)

        for column in columns:
            _fill(organization, task_list, user, column, 150, prefix=f"Bulk{column.pk}")
        with CaptureQueriesContext(connection) as large:
            board_snapshot(task_list=task_list)

        assert len(large) == len(small)
        # columns + lane totals + one read per lane + one label query for the whole board
        assert len(large) <= len(columns) + 4

    def test_empty_lanes_cost_no_query(self, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 3)
        with CaptureQueriesContext(connection) as queries:
            board_snapshot(task_list=task_list)
        assert len(queries) == 4  # columns, totals, the one non-empty lane, labels

    def test_board_never_ranks_the_whole_list(self, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 5)
        with CaptureQueriesContext(connection) as queries:
            board_snapshot(task_list=task_list)
        assert not any("ROW_NUMBER" in q["sql"].upper() for q in queries)

    def test_default_task_ordering_has_a_matching_index(self):
        index = next(i for i in Task._meta.indexes if i.name == "task_default_order_idx")
        assert index.condition is not None
        assert len(index.expressions) == 5

    def test_archived_cards_are_not_counted(self, organization, task_list, user, columns):
        from django.utils import timezone

        tasks = _fill(organization, task_list, user, columns[0], 3)
        Task.objects.filter(pk=tasks[0].pk).update(archived_at=timezone.now())

        lanes, _ = board_snapshot(task_list=task_list)

        assert lanes[0].total == 2

    def test_negative_offset_is_treated_as_zero(self, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 3)

        page = board_lane_page(task_list=task_list, column=columns[0], offset=-5)

        assert page.offset == 0
        assert len(page.cards) == 3


class TestBoardPage:
    def test_page_renders_a_bounded_number_of_cards(
        self, client_owner, organization, task_list, user, columns
    ):
        _fill(organization, task_list, user, columns[0], BOARD_LANE_PAGE_SIZE + 30)

        response = client_owner.get("/board/")

        assert response.status_code == 200
        assert response.content.count(b'<article class="board-card') == BOARD_LANE_PAGE_SIZE
        assert b"Show more (30 remaining)" in response.content
        # The header reports the lane's real size, not the size of the rendered page.
        assert f"<span>{BOARD_LANE_PAGE_SIZE + 30}</span>".encode() in response.content

    def test_show_more_returns_the_rest(self, client_owner, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], BOARD_LANE_PAGE_SIZE + 30)

        response = client_owner.get(
            f"/board/{task_list.pk}/lane/",
            {"column": columns[0].pk, "offset": BOARD_LANE_PAGE_SIZE},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert response.content.count(b'<article class="board-card') == 30
        assert b"Show more" not in response.content

    def test_show_more_for_cards_without_a_column(
        self, client_owner, organization, task_list, user, columns
    ):
        _fill(organization, task_list, user, None, BOARD_LANE_PAGE_SIZE + 2)

        page = client_owner.get("/board/")
        assert b"No column" in page.content
        assert b"Show more (2 remaining)" in page.content

        response = client_owner.get(
            f"/board/{task_list.pk}/lane/", {"column": "", "offset": BOARD_LANE_PAGE_SIZE}
        )
        assert response.content.count(b'<article class="board-card') == 2

    def test_lane_of_another_workspace_is_404(self, client_owner, other_task_list):
        column = other_task_list.ensure_default_columns()[0]

        response = client_owner.get(f"/board/{other_task_list.pk}/lane/", {"column": column.pk})

        assert response.status_code == 404

    def test_foreign_column_on_own_list_is_404(self, client_owner, task_list, columns, other_task_list):
        foreign = other_task_list.ensure_default_columns()[0]

        response = client_owner.get(f"/board/{task_list.pk}/lane/", {"column": foreign.pk})

        assert response.status_code == 404

    @pytest.mark.parametrize("column", ["abc", "1 OR 1=1", "-1"])
    def test_malformed_column_is_404(self, client_owner, task_list, columns, column):
        response = client_owner.get(f"/board/{task_list.pk}/lane/", {"column": column})

        assert response.status_code == 404

    def test_malformed_offset_falls_back_to_the_start(
        self, client_owner, organization, task_list, user, columns
    ):
        _fill(organization, task_list, user, columns[0], 3)

        response = client_owner.get(
            f"/board/{task_list.pk}/lane/", {"column": columns[0].pk, "offset": "nope"}
        )

        assert response.status_code == 200
        assert response.content.count(b'<article class="board-card') == 3

    def test_lane_requires_login(self, client, task_list, columns):
        response = client.get(f"/board/{task_list.pk}/lane/", {"column": columns[0].pk})

        assert response.status_code in (302, 403)

    def test_lane_rejects_post(self, client_owner, task_list, columns):
        response = client_owner.post(f"/board/{task_list.pk}/lane/", {"column": columns[0].pk})

        assert response.status_code == 405

    def test_viewer_can_page_a_lane(self, client_viewer, organization, task_list, user, columns):
        _fill(organization, task_list, user, columns[0], 2)

        response = client_viewer.get(f"/board/{task_list.pk}/lane/", {"column": columns[0].pk})

        assert response.status_code == 200


class TestDispatcherDrainsABacklog:
    def _enqueue(self, organization, queue, user, count):
        for index in range(count):
            enqueue_job(
                organization=organization,
                queue=queue,
                source="scale",
                handler_type=Job.HandlerType.INTERNAL,
                handler="internal:send_notification",
                payload={"user_id": user.pk, "title": f"N{index}", "natural_key": f"scale-{index}"},
                idempotency_key=f"scale-{index}",
            )

    def test_one_tick_drains_more_than_the_concurrency_limit(self, organization, queue, user):
        """One claim per tick capped throughput at concurrency_limit jobs per beat interval."""

        from queues.tasks import dispatch_available_jobs

        backlog = queue.concurrency_limit * 3 + 1
        self._enqueue(organization, queue, user, backlog)

        result = dispatch_available_jobs()

        # Each notification enqueues a follow-up delivery job; the same tick drains those too.
        assert result["succeeded"] >= backlog
        assert result["succeeded"] > queue.concurrency_limit
        assert result["batches"] > 1
        assert not Job.objects.filter(queue=queue, status__in=Job.CLAIMABLE_STATUSES).exists()

    def test_a_tick_stops_when_its_time_budget_is_spent(self, organization, queue, user, settings):
        """A tick must not outlive the beat interval, or ticks pile up behind each other."""

        from queues.tasks import dispatch_available_jobs

        settings.QUEUE_DISPATCH_TIME_BUDGET_SECONDS = 0
        self._enqueue(organization, queue, user, queue.concurrency_limit * 2)

        result = dispatch_available_jobs()

        assert result["batches"] == 1
        assert result["succeeded"] == queue.concurrency_limit

    def test_an_empty_queue_costs_one_batch(self, organization, queue):
        from queues.tasks import dispatch_available_jobs

        assert dispatch_available_jobs() == {
            "claimed": 0,
            "succeeded": 0,
            "retried": 0,
            "dead": 0,
            "batches": 1,
        }


class TestTaskListAvoidsDistinct:
    """DISTINCT forced Postgres to de-duplicate every wide row before sorting or counting."""

    def _labelled(self, organization, task_list, user, columns):
        from tasks.models import Label

        label = Label.objects.create(organization=organization, name="Scale")
        other = Label.objects.create(organization=organization, name="Other")
        tasks = _fill(organization, task_list, user, columns[0], 6)
        for task in tasks[:4]:
            task.labels.add(label, other)
        return label, tasks

    def test_web_filter_emits_no_distinct(self, organization, task_list, user, columns):
        from web.filters import apply_filters

        label, _ = self._labelled(organization, task_list, user, columns)
        queryset = apply_filters(Task.objects.filter(organization=organization), {"label": label})

        assert "DISTINCT" not in str(queryset.query).upper()

    def test_label_filter_returns_each_task_once(self, organization, task_list, user, columns):
        from web.filters import apply_filters

        label, tasks = self._labelled(organization, task_list, user, columns)
        ids = list(
            apply_filters(Task.objects.filter(organization=organization), {"label": label}).values_list(
                "pk", flat=True
            )
        )

        assert sorted(ids) == sorted(task.pk for task in tasks[:4])

    def test_api_label_filter_returns_each_task_once(
        self, api_client, organization, task_list, user, columns
    ):
        label, tasks = self._labelled(organization, task_list, user, columns)

        with CaptureQueriesContext(connection) as queries:
            response = api_client.get("/api/v1/tasks/", {"label": label.pk})

        assert response.status_code == 200
        ids = [row["id"] for row in response.json()["results"]]
        assert sorted(ids) == sorted(task.pk for task in tasks[:4])
        assert response.json()["count"] == 4
        task_selects = [q["sql"] for q in queries if 'FROM "tasks_task"' in q["sql"]]
        assert task_selects and not any("DISTINCT" in sql.upper() for sql in task_selects)


class TestRemindersStayBounded:
    """Completing and reopening a task must not accumulate reminder rows or notifications."""

    def _due(self, task, days):
        from django.utils import timezone

        task.due_date = timezone.localdate() + timedelta(days=days)
        task.save(update_fields=["due_date"])

    def test_toggling_a_future_task_keeps_one_reminder(self, task, user):
        from tasks.models import Reminder
        from tasks.services import schedule_reminder, toggle_task_completion

        self._due(task, 3)
        schedule_reminder(task)
        for _ in range(6):
            toggle_task_completion(task=task, actor=user)
            task.refresh_from_db()

        assert task.status == Task.Status.OPEN
        assert Reminder.objects.filter(task=task).count() == 1

    def test_reopening_re_arms_the_reminder_with_a_live_job(self, task, user):
        """Returning the cancelled row left a reopened task with no reminder at all."""

        from tasks.models import Reminder
        from tasks.services import schedule_reminder, toggle_task_completion

        self._due(task, 3)
        original = schedule_reminder(task)
        toggle_task_completion(task=task, actor=user)  # complete: cancels
        task.refresh_from_db()
        toggle_task_completion(task=task, actor=user)  # reopen: must arm again
        task.refresh_from_db()

        reminder = Reminder.objects.get(task=task)
        assert reminder.status == Reminder.Status.ARMED
        assert reminder.remind_at == original.remind_at
        assert reminder.job is not None and reminder.job.status in Job.CLAIMABLE_STATUSES
        assert reminder.job_id != original.job_id

    def test_an_overdue_task_is_reminded_once_however_often_it_is_reopened(self, task, user):
        from kernel.models import Notification
        from queues.tasks import dispatch_available_jobs
        from tasks.models import Reminder
        from tasks.services import schedule_reminder, toggle_task_completion

        self._due(task, -2)
        schedule_reminder(task)
        dispatch_available_jobs()
        assert Reminder.objects.filter(task=task, status=Reminder.Status.SENT).count() == 1

        for _ in range(5):
            toggle_task_completion(task=task, actor=user)
            task.refresh_from_db()
            dispatch_available_jobs()

        assert Reminder.objects.filter(task=task).count() == 1
        assert Notification.objects.filter(category=Notification.Category.REMINDER).count() == 1

    def test_a_new_due_date_earns_a_new_reminder(self, task):
        from queues.tasks import dispatch_available_jobs
        from tasks.models import Reminder
        from tasks.services import schedule_reminder

        self._due(task, -2)
        schedule_reminder(task)
        dispatch_available_jobs()

        self._due(task, 4)
        fresh = schedule_reminder(task)

        assert fresh is not None and fresh.status == Reminder.Status.ARMED
        assert Reminder.objects.filter(task=task).count() == 2

    def test_dashboard_loads_only_armed_reminders(self, client_owner, organization, task, user):
        from django.utils import timezone

        from tasks.models import Reminder

        self._due(task, 2)
        Reminder.objects.bulk_create(
            [
                Reminder(
                    organization=organization,
                    task=task,
                    remind_at=timezone.now() - timedelta(days=index + 1),
                    status=Reminder.Status.SENT,
                )
                for index in range(40)
            ]
        )
        with CaptureQueriesContext(connection) as queries:
            response = client_owner.get("/")

        assert response.status_code == 200
        reminder_queries = [q["sql"] for q in queries if 'FROM "tasks_reminder"' in q["sql"]]
        assert reminder_queries and all("'armed'" in sql or "armed" in sql for sql in reminder_queries)

