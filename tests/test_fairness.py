"""Weighted tenant fairness when claiming across workspaces."""

from __future__ import annotations

import pytest

from queues.models import Job
from queues.services import claim_jobs, enqueue_job, get_internal_queue

pytestmark = pytest.mark.django_db


def _seed(organization, *, n: int, prefix: str):
    queue = get_internal_queue(organization)
    for i in range(n):
        enqueue_job(
            organization=organization,
            queue=queue,
            source="fairness",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:send_notification",
            payload={},
            idempotency_key=f"{prefix}:{i}",
        )
    return queue


class TestFairness:
    def test_busy_tenant_does_not_monopolise_a_claim_batch(
        self, organization, other_organization, settings
    ):
        settings.QUEUE_FAIRNESS_ENABLED = True
        settings.QUEUE_FAIRNESS_WINDOW_SECONDS = 300
        _seed(organization, n=20, prefix="a")
        _seed(other_organization, n=20, prefix="b")

        # First tick: both orgs should appear when asking for more than one job.
        first = claim_jobs(worker_id="w1", limit=4)
        orgs = {job.job.organization_id for job in first}
        assert organization.pk in orgs
        assert other_organization.pk in orgs

        # After A has been claimed from, B should be preferred next.
        second = claim_jobs(worker_id="w1", limit=2)
        assert second
        assert second[0].job.organization_id == other_organization.pk or {
            j.job.organization_id for j in second
        } == {organization.pk, other_organization.pk}
