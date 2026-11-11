"""Shared pytest fixtures.

Every fixture builds a *fully isolated* tenant. Cross-tenant tests depend on two workspaces
that share nothing, so a scoping regression shows up as a failing assertion rather than as
a test that quietly passes because both fixtures pointed at the same row.
"""

from __future__ import annotations

import os
from datetime import timedelta

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_ENV", "test")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone

from kernel.models import ApiKey, Membership, Organization
from kernel.roles import Role
from kernel.services import create_organization
from queues.models import Job, Queue
from queues.services import enqueue_job, get_internal_queue
from tasks.models import Column, Label, Task, TaskList

User = get_user_model()

PASSWORD = "test-password-123456"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle counters and login lockouts live in the cache and leak between tests."""

    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(username="owner", email="owner@example.com", password=PASSWORD)


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="intruder", email="intruder@example.com", password=PASSWORD)


@pytest.fixture
def member_user(db):
    return User.objects.create_user(username="member", email="member@example.com", password=PASSWORD)


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(username="viewer", email="viewer@example.com", password=PASSWORD)


@pytest.fixture
def organization(db, user) -> Organization:
    return create_organization(name="Acme Workspace", owner=user)


@pytest.fixture
def other_organization(db, other_user) -> Organization:
    """A completely separate tenant, used to prove isolation."""

    return create_organization(name="Rival Workspace", owner=other_user)


@pytest.fixture
def membership(organization, user) -> Membership:
    return Membership.objects.get(organization=organization, user=user)


@pytest.fixture
def member(organization, member_user) -> Membership:
    return Membership.objects.create(organization=organization, user=member_user, role=Role.MEMBER)


@pytest.fixture
def viewer(organization, viewer_user) -> Membership:
    return Membership.objects.create(organization=organization, user=viewer_user, role=Role.VIEWER)


@pytest.fixture
def admin_membership(organization, db) -> Membership:
    admin = User.objects.create_user(username="admin", email="admin@example.com", password=PASSWORD)
    return Membership.objects.create(organization=organization, user=admin, role=Role.ADMIN)


@pytest.fixture
def task_list(organization) -> TaskList:
    return TaskList.objects.filter(organization=organization, name="Inbox").first()


@pytest.fixture
def other_task_list(other_organization) -> TaskList:
    return TaskList.objects.filter(organization=other_organization, name="Inbox").first()


@pytest.fixture
def columns(task_list) -> list[Column]:
    return task_list.ensure_default_columns()


@pytest.fixture
def task(organization, task_list, user, columns) -> Task:
    return Task.objects.create(
        organization=organization,
        task_list=task_list,
        column=columns[0],
        creator=user,
        title="Write the runbook",
        position=1000,
    )


@pytest.fixture
def other_task(other_organization, other_task_list, other_user) -> Task:
    other_task_list.ensure_default_columns()
    return Task.objects.create(
        organization=other_organization,
        task_list=other_task_list,
        creator=other_user,
        title="Rival secret plan",
        position=1000,
    )


@pytest.fixture
def label(organization) -> Label:
    return Label.objects.create(organization=organization, name="Ops")


@pytest.fixture
def queue(organization) -> Queue:
    return get_internal_queue(organization)


@pytest.fixture
def job(organization, queue) -> Job:
    return enqueue_job(
        organization=organization,
        queue=queue,
        source="test:1",
        handler_type=Job.HandlerType.INTERNAL,
        handler="internal:send_notification",
        payload={"user_id": 1, "title": "hello"},
        idempotency_key="test:1",
    ).job


@pytest.fixture
def api_key(organization, user):
    key, token = ApiKey.issue(
        organization=organization, name="Test key", role=Role.ADMIN, created_by=user
    )
    # Both halves are needed: the row for assertions, the plaintext for the header.
    return key, token


@pytest.fixture
def client_owner(client, user, organization):
    # Depends on `organization` deliberately: a signed-in user with no workspace is a real
    # state, but it redirects to onboarding, which would make every view test assert on a
    # redirect instead of the page under test.
    client.force_login(user)
    return client


@pytest.fixture
def client_member(client, member):
    client.force_login(member.user)
    return client


@pytest.fixture
def client_viewer(client, viewer):
    client.force_login(viewer.user)
    return client


@pytest.fixture
def api_client(user, organization):
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=user)
    return api


@pytest.fixture
def api_client_other(other_user, other_organization):
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=other_user)
    return api


@pytest.fixture
def api_client_viewer(viewer):
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=viewer.user)
    return api


@pytest.fixture
def frozen_now():
    """A stable instant for tests that assert on scheduling arithmetic."""

    return timezone.now().replace(microsecond=0)


@pytest.fixture
def tomorrow():
    return timezone.localdate() + timedelta(days=1)
