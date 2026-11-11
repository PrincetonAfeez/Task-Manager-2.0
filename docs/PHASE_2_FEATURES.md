# Phase 2 — Feature Depth

Two rounds of feature work, each answering: *what does this product still need before a
team would choose it over the incumbent and pay for it?*

---

## Round 1 — Make it a SaaS, not an app

The scaffold could hold tasks. It could not be sold, integrated with, or trusted with data.

### 1. Working automation runtime (the biggest gap)

The `Job`/`Queue`/`Schedule` ledger existed with **nothing that executed it**. Every
reminder, notification and webhook the product implied was silently never sent.

- `queues/registry.py` — handler registry. A job names its handler as a *string* resolved
  against a registry, never an importable dotted path from the database: otherwise a job
  payload chooses which code runs, and the queue becomes an RCE primitive.
- `queues/runtime.py` — claim → attempt → complete/fail loop, crash-safe at every step.
- `queues/handlers.py` — seven handlers, each idempotent.
- Lease expiry and a reaper, exponential backoff with jitter, dead letters, operator redrive.
- `run_worker` / `run_scheduler` / `reap_leases` management commands, so the queue is
  operable and testable without a broker.

### 2. Real scheduling

Timezone-correct cron (evaluated in the schedule's own zone, so 09:00 stays 09:00 across
DST), three implemented catch-up policies bounded by a cap, expression validation, and
self-deactivation after three failed evaluations.

### 3. Recurrence

`RecurrenceRule` had no writers at all; quick-add parsed "weekly" and discarded it. Now:
daily/weekly/monthly/yearly with intervals, weekday sets, month-end clamping (31 Jan + 1
month = 28 Feb), until/max bounds, and a database-level occurrence-identity constraint so a
duplicate sweep cannot double-create.

### 4. Multi-user workspaces

Signup, workspace creation, hashed single-use invitations, four roles with a central
permission matrix, escalation-proof role changes, and a last-owner guard on every path that
could otherwise orphan a workspace.

### 5. Public API worth integrating against

Session **and** API-key authentication, keys hashed with a role ceiling, tenant scoping in
`get_queryset` (so detail routes are covered by the same rule as lists), filtering, search,
ordering, cursor pagination on feeds, a stable error envelope, `Retry-After` on 429, and an
OpenAPI document generated from the live router so it cannot drift.

### 6. Board, filters and saved views

Kanban with drag-and-drop, sparse fractional positioning, WIP limits, done-column
completion, optimistic concurrency on every move, filterable/sortable task list with
allow-listed sort keys, and shareable saved views.

### 7. Notifications

Preferences enforced server-side, quiet hours (wrapping midnight correctly), digest emails
that only send when there is something to say, and idempotent delivery keyed on
`natural_key`.

### 8. Outbound webhooks

Signed (`t=…,v1=…` over `timestamp.body`, so replay is detectable), SSRF-hardened (every
resolved address checked; redirects never followed), retry-classified (429/5xx retry, 4xx
does not), circuit-broken after 20 consecutive failures, with a per-delivery audit trail and
a test-ping button.

### 9. Plans, quotas and entitlement

Three plans as *rows* so a limit can change without a deploy. Quotas checked at the service
boundary, so every path — form, API, recurrence, template — is covered by one rule. A
downgrade that does not fit is refused rather than silently accepted.

### 10. Data lifecycle

Workspace export (CSV/JSON, streaming, formula-injection safe), full workspace archive,
per-user personal data export, account deletion that anonymises rather than destroys shared
content, leave-workspace, and a soft-delete → 30-day retention → hard-purge pipeline.

---

## Round 2 — Make teams choose it

### 11. Time tracking

Start/stop timers with a **partial unique index** guaranteeing one running timer per person
(a check-then-write races with a second browser tab), manual logging, a 12-hour cap on
forgotten timers, per-user and per-task reporting, and estimate-accuracy analysis.

### 12. Task templates

Reusable blueprints including ordered subtasks, relative due dates and labels. Capture an
existing task as a template in one click. Repeatable process is the point: a checklist
retyped every time drifts, and the drift is invisible until something is missed.

### 13. Bulk operations

Complete, reopen, archive, re-prioritise, assign, label, or move up to 500 tasks at once.
Every id is re-scoped to the caller's organization before anything is written, so a crafted
id list cannot reach another tenant.

### 14. Calendar

Month grid padded to whole weeks, tasks on their due dates, "only mine" filter, and graceful
fallback on malformed query parameters.

### 15. Public share links

Read-only, tokenised (stored hashed), optionally expiring, revocable, view-counted. The only
endpoint in the product that serves customer data without a session — so it is deliberately
narrow: one list, no member identities, no write path, `X-Robots-Tag: noindex`, `no-store`.

### 16. Analytics

Throughput vs intake, cycle time, overdue ratio, per-assignee and per-list breakdowns, and
automation health — all computed with database aggregation so page cost stays flat as a
workspace grows.

---

## Deliberately out of scope

Stated plainly so the audit can distinguish a gap from a boundary:

- **No payment processor.** Billing models entitlement, not money movement.
- **No SSO/SAML/SCIM.** This is a Launch/Growth-stage product.
- **No realtime push.** Updates are HTMX request/response; no websockets.
- **No mobile apps.** Responsive web only.
- **English only.** i18n is enabled but no catalogues are shipped.
- **Single region.** No data-residency controls.
