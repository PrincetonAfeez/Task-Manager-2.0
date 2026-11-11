# Task Manager

A multi-tenant task management SaaS: shared workspaces, a kanban board, natural-language
quick add, and a durable automation layer that actually runs — reminders, recurring tasks,
schedules and signed outbound webhooks, all backed by a database ledger rather than a
message broker.

Django 5.2 · DRF · HTMX · Celery · PostgreSQL · Redis

---

## Quick start

```bash
python -m pip install -r requirements-dev.txt
python manage.py migrate
python manage.py seed_demo          # prints a randomly generated password
python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>. `seed_demo` prints the credentials it generated — they are
not fixed, and the command refuses to run when `DJANGO_ENV=production`.

To see the automation layer do work, run a worker alongside the server:

```bash
python manage.py run_worker
```

Or drain the queue once and exit:

```bash
python manage.py run_worker --once
```

---

## What it does

### Tasks
Natural-language quick add (`Ship release notes friday 5pm #ops @alex !3 weekly`), a kanban
board with drag-and-drop and WIP limits, subtasks, comments, attachments, labels,
dependencies with cycle detection, recurring tasks, saved views, bulk operations, a month
calendar, and time tracking with estimate-accuracy reporting.

### Workspaces
Multiple workspaces per account, four roles (owner / admin / member / viewer) driven by a
single permission matrix, hashed single-use email invitations, and an append-only audit log
that cannot be edited or deleted — including by a workspace owner.

### Automation
A durable job ledger in the database. Jobs are leased with a visibility timeout, retried
with exponential backoff and jitter, dead-lettered when they exhaust their budget, and
redrivable by an operator. Leases held by crashed workers are reclaimed by a reaper.
Schedules fire in the workspace's own timezone with three catch-up policies. Outbound
webhooks are HMAC-signed, SSRF-hardened, and circuit-broken.

### Platform
REST API at `/api/v1/` with session and API-key authentication, per-key rate limits, cursor
pagination, and an OpenAPI document at `/api/v1/openapi.json` generated from the live
router. Plans and quotas. CSV/JSON export. Personal data export and account deletion. Public
read-only share links.

### Identity and payments
Two-step verification (TOTP with recovery codes), which a workspace can require of every
member. Single sign-on over OpenID Connect on the Business plan, optionally mandatory. Paid
plans through Stripe-hosted checkout and customer portal, reconciled by signed webhooks -
no card data reaches the application. Uploaded attachments are scanned by ClamAV and held
back until they are declared clean. See [`docs/IDENTITY.md`](docs/IDENTITY.md) and
[`docs/BILLING.md`](docs/BILLING.md).

---

## Configuration

Copy `.env.example` to `.env`. Nothing security-relevant falls back to an insecure default:
with `DJANGO_ENV=production` the app refuses to start without `DJANGO_SECRET_KEY`,
`DJANGO_ALLOWED_HOSTS`, `REDIS_URL`, and a server database.

| Variable | Purpose |
|----------|---------|
| `DJANGO_ENV` | `development` \| `test` \| `production` |
| `DJANGO_SECRET_KEY` | Required in production |
| `DJANGO_ALLOWED_HOSTS` | Required in production |
| `DB_ENGINE` / `DB_*` | SQLite for local work, PostgreSQL in production |
| `REDIS_URL` | Cache, rate limiting, login lockout |
| `DJANGO_ADMIN_URL` | Move the admin off the default path |
| `WEBHOOK_ALLOW_PRIVATE_NETWORKS` | Keep `0` in production — it is the SSRF guard |
| `FIELD_ENCRYPTION_KEYS` | Encrypts TOTP seeds and SSO client secrets; set it in production |
| `ATTACHMENT_SCAN_BACKEND` | `clamav` to scan uploads; `check --deploy` warns when unset |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_IDS` | Payments; all empty runs without a processor |
| `WEB_CONCURRENCY` | gunicorn workers, about one per core |

Full list in `.env.example`.

---

## Running the whole stack

```bash
cp .env.example .env    # then fill in DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up -d
```

Brings up web, Postgres, Redis, Celery beat, and the dispatcher / scheduler / reaper
workers. Migrations run once in a dedicated service that every other service waits on, so
no replica ever observes a half-migrated schema. Uploads live on a named volume shared by
the web and worker containers.

To scan attachments for malware, add the scanner (first start downloads the signature
database; clamd needs about 1.5 GB of memory):

```bash
ATTACHMENT_SCAN_BACKEND=clamav docker compose --profile scanning up -d
```

---

## Development

```bash
make test              # full pytest suite
make coverage          # with an 85% floor
make lint              # ruff
make check             # Django system checks, deploy-strict
make check-migrations  # fails if a model change has no migration
make audit             # pip-audit + bandit
make backup-verify     # real backup-and-restore round trip
make ci                # everything CI runs
```

| Command | Purpose |
|---------|---------|
| `run_worker` | Execute durable jobs (no broker required) |
| `run_scheduler` | Fire due schedules and recurrences |
| `reap_leases` | Reclaim leases from crashed workers |
| `backup_db --verify` | Back up and prove the backup restores |
| `restore_db <path> --confirm` | Restore from a backup |
| `seed_demo` | Development data |
| `seed_load` | Large corpus for load testing (refuses production without `--force`) |
| `generate_field_key` | A new key for `FIELD_ENCRYPTION_KEYS` |
| `scan_attachments` | Queue a malware scan for every attachment without a verdict |

Load testing: `python scripts/loadtest.py --help`; method and results in
[`docs/CAPACITY.md`](docs/CAPACITY.md).

---

## Health and operations

| Endpoint | Meaning |
|----------|---------|
| `/healthz` | Liveness, dependency-free |
| `/readyz` | Database and cache reachable; **503** when not |
| `/healthz/worker` | 503 when the backlog is aging and nothing is completing |
| `/healthz/scheduler` | 503 when a schedule is overdue |
| `/healthz/reaper` | 503 when a lease has been stranded |
| `/version` | Release stamp |

These measure real state. See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for alert-to-action
procedures, the backup and restore drill, the expand/contract migration policy, and
escalation.

---

## Architecture notes

- **The database is the ledger, not the broker.** Domain code writes a durable `Job` row
  inside its own transaction. Celery only tells a worker to look at the table, so a lost
  broker message costs one poll interval rather than the work.
- **At-least-once delivery, exactly-once effect.** Handlers take an `EffectMarker` inside
  the same transaction as their side effect, or rely on a naturally unique row.
- **Firing identity is a row.** A schedule cannot fire twice for the same instant because
  `(schedule, scheduled_time)` is unique. Same pattern for recurrence occurrences.
- **Tenant isolation is structural.** Every API endpoint inherits a base class that scopes
  `get_queryset`, so detail routes are covered by the same rule as lists. A foreign id is a
  404, never a 403 — 403 would confirm the id exists.
- **Authorization is data.** One permission matrix in `kernel/roles.py`; views, viewsets and
  templates all consult it rather than re-deriving "is this person an admin?".

---

## Documentation

| Document | Contents |
|----------|----------|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | On-call procedures, backups, rollback, escalation |
| [`docs/IDENTITY.md`](docs/IDENTITY.md) | Two-step verification and single sign-on |
| [`docs/BILLING.md`](docs/BILLING.md) | Stripe integration and its first-run checklist |
| [`docs/PARKED.md`](docs/PARKED.md) | Work explicitly deferred (pen test, live Stripe, SAML/SCIM, SLA) |
| [`docs/STORAGE.md`](docs/STORAGE.md) | Filesystem vs S3-compatible attachment storage |
| [`docs/RLS.md`](docs/RLS.md) | Optional Postgres row-level security |
| [`docs/LOCAL_STACK.md`](docs/LOCAL_STACK.md) | Compose profiles: MinIO, ClamAV, Keycloak |
| [`docs/CAPACITY.md`](docs/CAPACITY.md) | Load-test method, measured results, scaling limits |
| [`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md) | Internal security review and residual risk |
| [`docs/AUDIT_PASS_1.md`](docs/AUDIT_PASS_1.md) | Security, authentication, configuration |
| [`docs/AUDIT_PASS_2.md`](docs/AUDIT_PASS_2.md) | Domain correctness and data integrity |
| [`docs/AUDIT_PASS_3.md`](docs/AUDIT_PASS_3.md) | Deployability, operations, observability |
| [`docs/AUDIT_PASS_4.md`](docs/AUDIT_PASS_4.md) | API contract, UX, accessibility, coverage |
| [`docs/PHASE_2_FEATURES.md`](docs/PHASE_2_FEATURES.md) | Feature work and its rationale |
| [`docs/AUDIT_PASS_5_NEW_FEATURES.md`](docs/AUDIT_PASS_5_NEW_FEATURES.md) | Audit of the new features |

---

## Accepted limitations

Stated plainly, because a gap you have not declared is a gap you are hiding:

- **Parked (see [`docs/PARKED.md`](docs/PARKED.md)):** third-party penetration test; real
  Stripe account / test-mode checklist; SAML; SCIM; contractual SLA; status-page
  integration. These are not active work.
- **SSO is OpenID Connect only, verified against a test provider.** No group-to-role
  mapping; not yet run against Okta, Entra ID or Google Workspace (a local IdP is fine for
  practice; commercial IdP proof is optional until unparked with SSO expansion).
- **CI runs on GitHub Actions** (`.github/workflows/ci.yml`) on push/PR to `main`.
- **No realtime push.** Updates are HTMX request/response; there are no websockets.
- **No mobile applications.** The web UI is responsive; that is the whole client story.
- **English only.** `USE_I18N` is on but no translation catalogues are shipped.
- **Single region.** No data-residency or multi-region controls.
- **Best-effort availability.** Internal SLOs are in the runbook; there is no contractual
  uptime commitment (parked with status-page work).
- **Attachments default to the local filesystem.** Set `MEDIA_BACKEND=s3` for
  S3-compatible object storage (see [`docs/STORAGE.md`](docs/STORAGE.md)).

## Licence

Provided as-is for evaluation.
