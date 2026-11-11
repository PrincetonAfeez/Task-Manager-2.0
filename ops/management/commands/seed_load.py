"""Seed a large, realistic corpus for load testing.

Creates one login that belongs to every generated workspace, so a single session can drive
load against any of them. Refuses to run in production unless forced, because it creates an
account with a caller-supplied password and tens of thousands of rows.
"""

from __future__ import annotations

import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from billing.models import Plan, Subscription
from billing.services import ensure_default_plans
from kernel.models import ApiKey, Organization
from kernel.roles import Role
from kernel.services import create_organization
from queues.models import Job
from queues.services import get_internal_queue
from tasks.models import Label, Task, TaskList

User = get_user_model()

WORDS = (
    "review deploy draft audit migrate refactor document test release plan invoice "
    "onboard renew rotate archive benchmark triage escalate reconcile schedule widget "
    "report pipeline webhook customer contract budget roadmap incident runbook"
).split()


class Command(BaseCommand):
    help = "Seed a load-test corpus (development / disposable environments only)."

    def add_arguments(self, parser):
        parser.add_argument("--workspaces", type=int, default=10)
        parser.add_argument("--tasks-per-workspace", type=int, default=2000)
        parser.add_argument("--jobs", type=int, default=0, help="Also enqueue this many jobs.")
        parser.add_argument("--username", default="loadtest")
        parser.add_argument("--password", required=True)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        if getattr(settings, "IS_PRODUCTION", False) and not options["force"]:
            raise CommandError("seed_load will not run with DJANGO_ENV=production without --force.")

        rng = random.Random(20260724)  # noqa: S311 - reproducible test data, not security
        ensure_default_plans()
        business = Plan.objects.get(code="business")

        user, _ = User.objects.get_or_create(
            username=options["username"], defaults={"email": f"{options['username']}@example.com"}
        )
        user.set_password(options["password"])
        user.save(update_fields=["password"])

        today = timezone.localdate()
        total_tasks = 0
        first_org = None

        for index in range(options["workspaces"]):
            name = f"Load Workspace {index + 1:03d}"
            organization = Organization.objects.filter(name=name, owner=user).first()
            if organization is None:
                organization = create_organization(name=name, owner=user)
            first_org = first_org or organization

            Subscription.objects.update_or_create(
                organization=organization,
                defaults={"plan": business, "status": Subscription.Status.ACTIVE},
            )

            task_list = TaskList.objects.get(organization=organization, name="Inbox")
            columns = task_list.ensure_default_columns()
            labels = [
                Label.objects.get_or_create(organization=organization, name=label)[0]
                for label in ("ops", "api", "urgent", "billing")
            ]

            existing = Task.objects.filter(organization=organization).count()
            to_create = max(0, options["tasks_per_workspace"] - existing)
            batch = []
            for n in range(to_create):
                column = rng.choice(columns)
                done = column.is_done
                due = today + timedelta(days=rng.randint(-20, 40)) if rng.random() < 0.7 else None
                batch.append(
                    Task(
                        organization=organization,
                        task_list=task_list,
                        column=column,
                        creator=user,
                        assignee=user if rng.random() < 0.4 else None,
                        title=" ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 6))).capitalize(),
                        notes="load test corpus" if rng.random() < 0.3 else "",
                        due_date=due,
                        priority=rng.choice([1, 2, 2, 2, 3, 3, 4]),
                        status=Task.Status.COMPLETED if done else Task.Status.OPEN,
                        completed_at=timezone.now() if done else None,
                        position=(existing + n + 1) * 1000,
                    )
                )
            created = Task.objects.bulk_create(batch, batch_size=1000)
            through = Task.labels.through
            through.objects.bulk_create(
                [
                    through(task_id=task.pk, label_id=rng.choice(labels).pk)
                    for task in created
                    if rng.random() < 0.5
                ],
                batch_size=2000,
            )
            total_tasks += len(created)

        jobs = 0
        if options["jobs"] and first_org is not None:
            queue = get_internal_queue(first_org)
            stamp = timezone.now().strftime("%Y%m%d%H%M%S")
            Job.objects.bulk_create(
                [
                    Job(
                        organization=first_org,
                        queue=queue,
                        source="loadtest",
                        handler_type=Job.HandlerType.INTERNAL,
                        handler="internal:send_notification",
                        payload={
                            "user_id": user.pk,
                            "title": f"Load notification {n}",
                            "natural_key": f"load:{stamp}:{n}",
                            "category": "system",
                        },
                        idempotency_key=f"load:{stamp}:{n}",
                        available_at=timezone.now(),
                    )
                    for n in range(options["jobs"])
                ],
                batch_size=1000,
            )
            jobs = options["jobs"]

        token = ""
        if first_org is not None:
            ApiKey.objects.filter(organization=first_org, name="loadtest").delete()
            _key, token = ApiKey.issue(
                organization=first_org, name="loadtest", role=Role.MEMBER, created_by=user
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {options['workspaces']} workspaces, {total_tasks} new tasks, {jobs} jobs."
            )
        )
        if token:
            # Printed once for the load generator; it is a disposable key for a disposable DB.
            self.stdout.write(f"API_KEY={token}")
