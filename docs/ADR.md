# Architecture Decision Records

The decisions that shaped this system, why they were made, and what they cost. Recorded so
the next person can tell a deliberate trade-off from an accident.

---

## ADR-001 — The database is the job ledger; the broker is not

**Status:** Accepted

**Context.** The product promises reminders, recurring tasks, scheduled triggers and
webhooks. A conventional Celery setup puts that work in Redis. Redis is not durable by
default, and even with AOF a lost message is silently lost work — a reminder that never
fires produces no error anywhere, and the customer discovers it by missing a deadline.

**Decision.** Every unit of work is a `Job` row written inside the caller's transaction.
Celery only nudges a worker to look at the table. The dispatcher claims rows with
`SELECT ... FOR UPDATE SKIP LOCKED`, leases them with a visibility timeout, and reports
outcomes back to the row.

**Consequences.**
- A lost broker message costs one poll interval, not the work.
- A job and the state it acts on commit together or not at all (transactional outbox).
- The full history — attempts, errors, timings — is queryable in SQL.
- Cost: more database write load, and a poll interval of scheduling latency.
- Cost: the claim path differs on SQLite (no `SKIP LOCKED`), so CI runs against Postgres too.

**Alternatives rejected.** Plain Celery (not durable). A hosted queue such as SQS (adds a
sub-processor and still needs a local ledger for the audit trail).

---

## ADR-002 — Firing identity is a database row, not a lock

**Status:** Accepted

**Context.** A schedule that fires twice for the same instant sends two emails. Distributed
locks, leader election and "check then insert" all have a window where two schedulers both
believe they won.

**Decision.** A firing is a `ScheduleOccurrence` row with a unique constraint on
`(schedule, scheduled_time)`. Two schedulers racing on the same tick means one insert
succeeds and one raises `IntegrityError`; the loser does nothing. The same pattern gives
recurrence its identity, via a partial unique constraint on `Task(recurrence_source, due_date)`.

**Consequences.**
- Double-firing is impossible, not merely unlikely.
- No lock service, no leader election, no lease renewal for the scheduler.
- The occurrence table is also the customer-facing history of what fired and when.
- Cost: occurrence rows accumulate and need pruning.

---

## ADR-003 — Tenant isolation is structural, not per-developer discipline

**Status:** Accepted

**Context.** The original code was a set of bare `ModelViewSet`s over `.objects.all()`.
Every endpoint leaked every tenant's data. The failure was not one missing filter — it was
that filtering was each developer's responsibility on each endpoint.

**Decision.** Customer-data endpoints inherit `OrganizationScopedViewSet`, which scopes
`get_queryset()`. Because detail routes resolve objects *from* the queryset, list and
detail are covered by one rule. Serializers use `TenantPrimaryKeyRelatedField`, so a
related id from another tenant is a validation error rather than a silent cross-tenant
write. A codebase-wide test fails the build if a view module touches an unscoped manager.

**Consequences.**
- Adding an endpoint cannot forget the filter; it can only forget to declare a permission.
- A foreign id is **404, not 403** — 403 confirms the id exists and is an enumeration oracle.
- Cost: `tenant.scope()` raises on models without an `organization` field, so genuinely
  global models must be scoped explicitly.

**Alternative rejected.** PostgreSQL row-level security. Stronger, but it ties the product
to one database and makes the rules invisible in the code. Worth adding later as defence in
depth, not as the only layer.

---

## ADR-004 — Authorization is data, in one place

**Status:** Accepted

**Context.** Role checks scattered as `if user.is_admin` drift. The drift is invisible until
one of them is wrong, and the one that is wrong is a privilege-escalation bug.

**Decision.** `kernel/roles.py` holds a permission matrix: named permissions mapped to a
minimum role rank. Views, viewsets, templates and services all consult `has_permission`.
Unknown roles and unknown permissions both **deny** — a typo in a permission string fails
closed.

**Consequences.**
- The whole authorization model is auditable in one file.
- Templates use the same matrix, so the UI never offers what the server would refuse.
- `can_assign_role` prevents both vertical and horizontal escalation in one function.
- Cost: coarser than per-object ACLs. Adequate for workspace-scoped collaboration.

---

## ADR-005 — At-least-once delivery, exactly-once effect

**Status:** Accepted

**Context.** Any retrying queue delivers at least once. A handler that sends an email on
every delivery sends duplicates. Exactly-once *delivery* is not achievable; exactly-once
*effect* is.

**Decision.** Handlers establish idempotency one of two ways: a naturally unique row
(`Notification.natural_key`), or an explicit `EffectMarker` claimed in the same transaction
as the effect. Enqueue keys are derived only from stable facts — ids and scheduled instants,
never `now()`.

**Consequences.**
- Retries and redeliveries are safe by construction.
- Cost: every new handler must decide its idempotency key. This is documented in the module
  and enforced by review.
- Subtlety: the email handler *releases* its marker on SMTP failure, or the retry would see
  "already sent" and lose the message permanently.

---

## ADR-006 — Optimistic concurrency, not locking

**Status:** Accepted

**Context.** Two people editing one task is normal. Pessimistic locking on a collaborative
board is a worse experience than a rare conflict.

**Decision.** `Task.version` increments on every domain mutation. Writers may send
`expected_version`; a mismatch returns **409** with a message telling the user to reload.
Omitting it opts out, which keeps simple integrations simple.

**Consequences.**
- The last writer no longer silently wins.
- The UI round-trips the version in a hidden field, so a stale browser tab is caught.
- Cost: clients must handle 409. Documented in the OpenAPI description.

---

## ADR-007 — Server-rendered HTML with HTMX, not an SPA

**Status:** Accepted

**Context.** The product is forms, lists and a board. An SPA would add a build pipeline, a
second routing layer, a client state model, and a bundle that must be kept in sync with the
API.

**Decision.** Django templates with HTMX for partial updates. HTMX is **vendored locally**,
not loaded from a CDN.

**Consequences.**
- One language, one router, one set of permission checks. Authorization cannot drift between
  a client guard and a server guard, because there is only the server guard.
- A strict CSP (`script-src 'self'`) is achievable, and no third-party script can load.
- Every interactive control works without JavaScript; HTMX only enhances it.
- Cost: no offline mode, no optimistic UI, a round trip per interaction.

---

## ADR-008 — Soft delete with a retention window, then a real purge

**Status:** Accepted

**Context.** "Delete my workspace" is the action customers most often regret. An immediate
hard delete makes support recovery impossible. But a soft delete alone does not honour an
erasure request.

**Decision.** Deletion sets `deleted_at` and `purge_after`. The workspace disappears from
every query immediately. After 30 days a nightly job hard-deletes every row, in an explicit
order that respects `PROTECT` foreign keys. Audit entries are **detached**, not deleted.

**Consequences.**
- A mistake is recoverable for 30 days; an erasure request is genuinely honoured after it.
- The purge refuses to run against a workspace that is not soft-deleted — a purge job for a
  live workspace is a bug or an attack, and either way must not proceed.
- Cost: deleted data occupies storage for 30 days, and every query must exclude it.

---

## ADR-009 — Plans and quotas as rows, enforced at the service boundary

**Status:** Accepted

**Context.** Limits hardcoded as constants need a deploy to change and cannot be waived for
one customer. Limits checked in views are checked in *some* views.

**Decision.** `Plan` is a table. `check_quota()` is called from the service layer, so every
path — web form, REST API, recurrence engine, template application — is covered by one
rule. Checks happen *before* the write. A downgrade that does not fit is refused.

**Consequences.**
- A limit changes without a deploy; a bespoke plan is a row.
- A new entry point inherits enforcement by calling the service.
- Refusing an impossible downgrade avoids stranding a workspace permanently over quota.
- Cost: a counting query per check on the write path.

---

## ADR-010 — Health checks measure real state

**Status:** Accepted

**Context.** The original endpoints returned `{"status": "ok"}` unconditionally with a
`"placeholder"` note. A monitor pointed at them would have reported green while the entire
worker fleet was down. A health check that cannot fail is worse than none — it converts an
outage into a silent one.

**Decision.** Each endpoint measures the thing it claims to describe: recent job
completions and backlog age for the worker, overdue schedules for the scheduler, stranded
leases for the reaper, and real database and cache round trips for readiness. Degraded
returns **503**, so a load balancer removes the instance and a pager fires.

**Consequences.**
- `/readyz` is a usable load-balancer target.
- Every alert in `SLO.md` maps to an endpoint that can actually go red.
- Nuance: a deep backlog alone is *not* unhealthy. It is unhealthy when it is also stale —
  a busy queue that is draining is fine, and paging on depth alone trains people to ignore
  the pager.
