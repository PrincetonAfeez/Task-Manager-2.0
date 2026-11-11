"""Seed a demo workspace.

Refuses to run in production. The original version created a ``demo/demo`` account through
a code path the *request handler* also used, which meant any anonymous visitor to the live
site was silently signed into a shared workspace. Demo data is a development affordance and
lives only here.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from kernel.models import Membership, Notification, NotificationPreference, Organization
from kernel.roles import Role
from kernel.services import create_organization
from queues.models import Job
from queues.services import enqueue_job, get_internal_queue, get_webhook_queue
from schedules.models import Schedule
from schedules.services import bootstrap_next_run
from tasks.models import Column, Label, ProjectArea, Task, TaskList
from tasks.services import create_task_from_quick_add, record_activity

User = get_user_model()

DEMO_USERNAME = "demo"
DEMO_TEAMMATES = [("alex", "alex@example.com", Role.ADMIN), ("sam", "sam@example.com", Role.MEMBER)]


class Command(BaseCommand):
    help = "Seed a demo Task Manager workspace (development only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Password for the demo accounts. A random one is generated and printed if omitted.",
        )
        parser.add_argument("--force", action="store_true", help="Run even outside development.")

    @transaction.atomic
    def handle(self, *args, **options):
        if getattr(settings, "IS_PRODUCTION", False) and not options["force"]:
            raise CommandError(
                "seed_demo creates accounts with known credentials and will not run with "
                "DJANGO_ENV=production. Pass --force only if you are certain."
            )

        password = options.get("password") or secrets.token_urlsafe(12)

        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME,
            defaults={"email": "demo@example.com", "first_name": "Demo"},
        )
        if created or not user.has_usable_password():
            user.set_password(password)
            user.save(update_fields=["password"])
        else:
            password = "(unchanged - the demo user already had one)"  # noqa: S105

        organization = Organization.objects.filter(slug="demo").first()
        if organization is None:
            organization = create_organization(name="Demo Workspace", owner=user)
            Organization.objects.filter(pk=organization.pk).update(slug="demo")
            organization.refresh_from_db()

        inbox = TaskList.objects.filter(organization=organization, name="Inbox").first()
        if inbox is None:
            inbox = TaskList.objects.create(organization=organization, name="Inbox", position=1)
        inbox.ensure_default_columns()

        self._seed_team(organization, password)
        self._seed_structure(organization)
        self._seed_tasks(organization, user, inbox)
        self._seed_automation(organization, user)

        self.stdout.write(self.style.SUCCESS("Demo workspace seeded."))
        self.stdout.write(f"  Sign in as: {DEMO_USERNAME}")
        self.stdout.write(f"  Password:   {password}")
        self.stdout.write("  URL:        http://127.0.0.1:8000/")

    def _seed_team(self, organization, password):
        for username, email, role in DEMO_TEAMMATES:
            member, created = User.objects.get_or_create(username=username, defaults={"email": email})
            if created:
                member.set_password(password)
                member.save(update_fields=["password"])
            Membership.objects.get_or_create(
                organization=organization, user=member, defaults={"role": role}
            )
            NotificationPreference.objects.get_or_create(organization=organization, user=member)

    def _seed_structure(self, organization):
        for index, (name, color) in enumerate((("Operations", "#0f766e"), ("Engineering", "#2563eb")), 1):
            ProjectArea.objects.get_or_create(
                organization=organization, name=name, defaults={"color": color, "position": index}
            )

        ops = ProjectArea.objects.get(organization=organization, name="Operations")
        shared, created = TaskList.objects.get_or_create(
            organization=organization,
            name="Shared Launch",
            defaults={"project_area": ops, "position": 2, "color": "#b45309"},
        )
        if created:
            shared.ensure_default_columns()
            Column.objects.filter(task_list=shared, name="In Progress").update(wip_limit=3)

        for name, color in (("Ops", "#0f766e"), ("API", "#2563eb"), ("Urgent", "#b91c1c")):
            Label.objects.get_or_create(organization=organization, name=name, defaults={"color": color})

    def _seed_tasks(self, organization, user, inbox):
        samples = [
            "Send the weekly report tomorrow 5pm #Ops !2",
            "Review webhook retry policy today 3pm #API !3",
            "Draft the support runbook friday 10am #Ops",
            "Rotate API credentials monthly #Ops !3",
        ]
        for raw in samples:
            stub = raw.split("#")[0].strip()[:20]
            if Task.objects.filter(organization=organization, title__startswith=stub).exists():
                continue
            create_task_from_quick_add(organization=organization, task_list=inbox, creator=user, raw=raw)

        shared = TaskList.objects.filter(organization=organization, name="Shared Launch").first()
        if shared is None:
            return
        task, created = Task.objects.get_or_create(
            organization=organization,
            task_list=shared,
            title="Validate board move conflict behavior",
            defaults={
                "creator": user,
                "column": shared.columns.filter(name="In Progress").first(),
                "due_date": timezone.localdate() + timedelta(days=2),
                "priority": Task.Priority.HIGH,
                "position": 1000,
            },
        )
        if created:
            urgent = Label.objects.filter(organization=organization, name="Urgent").first()
            if urgent:
                task.labels.add(urgent)

    def _seed_automation(self, organization, user):
        internal = get_internal_queue(organization)
        webhooks = get_webhook_queue(organization)

        enqueue_job(
            organization=organization,
            queue=internal,
            source="demo:welcome",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={
                "user_id": user.pk,
                "title": "Demo workspace ready",
                "body": "Seed data has been loaded. Explore Tasks, Board and Automation.",
                "natural_key": "welcome:demo",
            },
            idempotency_key="demo:welcome:1",
        )
        enqueue_job(
            organization=organization,
            queue=webhooks,
            source="demo:webhook",
            handler_type=Job.HandlerType.HTTP_DELIVERY,
            handler="http_delivery",
            payload={"url": "https://example.invalid/webhooks/task-manager", "method": "POST"},
            idempotency_key="demo:webhook:1",
            available_at=timezone.now() + timedelta(minutes=5),
        )

        schedule, created = Schedule.objects.get_or_create(
            organization=organization,
            name="Launch check webhook",
            defaults={
                "queue": webhooks,
                "expression_type": Schedule.ExpressionType.CRON,
                "expression": "*/15 * * * *",
                "timezone": organization.timezone,
                "target_handler": "http_delivery",
                "payload": {"url": "https://example.invalid/webhooks/task-manager"},
                "catch_up_policy": Schedule.CatchUpPolicy.RUN_ONCE,
            },
        )
        if created:
            bootstrap_next_run(schedule)

        Schedule.objects.get_or_create(
            organization=organization,
            name="Nightly digest sweep",
            defaults={
                "queue": internal,
                "expression_type": Schedule.ExpressionType.INTERVAL,
                "expression": "6h",
                "timezone": organization.timezone,
                "target_handler": "internal:schedule_fire",
                "catch_up_policy": Schedule.CatchUpPolicy.SKIP,
            },
        )

        Notification.objects.get_or_create(
            organization=organization,
            user=user,
            natural_key="seed:complete",
            defaults={
                "title": "Seed complete",
                "body": "Demo data is ready.",
                "category": Notification.Category.SYSTEM,
                "status": Notification.Status.DELIVERED,
                "delivered_at": timezone.now(),
            },
        )
        record_activity(
            organization=organization,
            actor=user,
            verb="demo.seeded",
            target_type="organization",
            target_id=organization.pk,
            summary="Seeded the demo workspace",
        )
