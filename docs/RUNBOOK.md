# Operations Runbook

Everything an on-call engineer needs to keep Task Manager serving customers. Each alert
below maps to a concrete action; nothing here says "investigate".

---

## 1. Topology

| Component | Process | What breaks if it stops |
|-----------|---------|--------------------------|
| `web` | `gunicorn config.wsgi` | Customers cannot reach the product. |
| `beat` | `celery -A config beat` | **Nothing else runs.** No jobs dispatch, no schedules fire, no leases are reaped. This is the single most important process to watch. |
| `dispatcher` | `celery -A config worker -Q dispatcher` | Reminders, notifications, emails and webhooks queue up but never execute. |
| `scheduler` | `celery -A config worker -Q scheduler` | Schedules and recurring tasks stop producing work. |
| `reaper` | `celery -A config worker -Q reaper` | Jobs held by crashed workers stay stranded forever. |
| `postgres` | managed | Total outage. |
| `redis` | managed | Rate limiting and login lockout degrade; Celery stops delivering. |

The durable `Job` table — not Redis — is the ledger. A lost broker message costs one poll
interval. A lost `Job` row is lost work, which is why the queue lives in Postgres.

---

## 2. Health endpoints

| Endpoint | Meaning | Wire it to |
|----------|---------|------------|
| `GET /healthz` | Liveness. Dependency-free. | Container restart policy. |
| `GET /readyz` | Database + cache reachable. **503** when not. | Load balancer target group. |
| `GET /healthz/worker` | 503 when a backlog is aging *and* nothing is completing. | Pager. |
| `GET /healthz/scheduler` | 503 when a schedule is more than 10 minutes overdue. | Pager. |
| `GET /healthz/reaper` | 503 when a lease has been expired for more than 10 minutes. | Pager. |
| `GET /version` | Release stamp for verifying a rollout. | Deploy pipeline. |

---

## 3. Alerts to actions

### `worker_healthz` returns 503

Meaning: jobs are waiting, the oldest is older than `QUEUE_OLDEST_JOB_ALERT_SECONDS`, and
nothing has completed in ten minutes.

1. `docker compose ps` — is `dispatcher` running? Is `beat` running?
2. `docker compose logs --tail=200 dispatcher`
3. Check for a paused queue: `GET /api/v1/queues/` — a `paused: true` queue is usually a
   forgotten manual pause.
4. Drain manually to confirm the runtime works: `python manage.py run_worker --once`
5. If a single poison payload is stalling the lane, find it in the dead-letter list and
   leave it parked. It does not block other jobs.

### `scheduler_healthz` returns 503

1. `docker compose logs --tail=200 beat scheduler`
2. Check for schedules that deactivated themselves after three failed evaluations:
   `SELECT name, last_error FROM schedules_schedule WHERE consecutive_failures >= 3;`
   These are almost always an invalid cron expression a customer typed.
3. Fix the expression, set `active = true`, and `python manage.py run_scheduler --once`.

### `reaper_healthz` returns 503

Workers died holding leases and the reaper is not running.

1. `docker compose restart reaper`
2. Force one pass: `python manage.py reap_leases`
3. If leases keep expiring, the jobs are exceeding the visibility timeout — raise
   `Queue.visibility_timeout_seconds` for that queue rather than letting them loop.

### Dead letters are accumulating

1. Look at the Automation page, or `GET /api/v1/dead-letters/`.
2. Read `reason` — it carries the classified failure, not a raw traceback.
3. Fix the cause, then redrive: the **Redrive** button, or
   `POST /api/v1/dead-letters/<id>/redrive/`.
4. Redrive resets the attempt budget, so a still-broken job will re-dead-letter rather than
   loop forever.

### Login failures spike

Credential stuffing. Two independent controls already engage:

- per-account lockout after `LOGIN_LOCKOUT_MAX_ATTEMPTS` (default 8) in a 15-minute window;
- per-IP throttling on the login form (`THROTTLE_AUTH`, default 10/minute).

Confirm from the audit log: `SELECT ip_address, COUNT(*) FROM kernel_auditlog WHERE action =
'auth.login_failed' AND created_at > now() - interval '1 hour' GROUP BY 1 ORDER BY 2 DESC;`
To harden temporarily, lower `LOGIN_LOCKOUT_MAX_ATTEMPTS` and redeploy, or block the source
at the edge.

---

## 4. Backups

**Schedule:** nightly, retained 7 days locally plus whatever the platform snapshot policy
provides.

```bash
python manage.py backup_db
```

**Verify — this is not optional.** An untested backup is not a backup:

```bash
python manage.py backup_db --verify
```

This performs a full round trip on both engines:

- **PostgreSQL:** `pg_dump` inside an exported snapshot, `pg_restore` into a scratch database
  (`<name>_verify_<stamp>`), then a per-table row-count comparison against counts taken in
  that same snapshot, so concurrent writes cannot cause a false mismatch. The scratch
  database is dropped afterwards. Needs `CREATEDB`, and `pg_dump`/`pg_restore` of the
  server's major version or newer (the image ships them).
- **SQLite:** dump, restore into a scratch file, `PRAGMA integrity_check`, row counts.

Last run in the container stack on 2026-11-10: 50 tables verified.

Uploaded attachments are **not** in the database backup. They live on the `media_data`
volume (or object storage); back that up on the same schedule.

### Postgres restore drill (run quarterly)

The automated check above proves the dump restores. The manual drill additionally proves a
person can do it under pressure:

```bash
# 1. Take a backup
python manage.py backup_db

# 2. Create a scratch database
createdb task_manager_restore_drill

# 3. Restore into it
pg_restore --clean --if-exists --no-owner --dbname=task_manager_restore_drill backups/backup-<stamp>.dump

# 4. Prove the schema is current and complete
DB_NAME=task_manager_restore_drill python manage.py migrate --check

# 5. Spot-check row counts against production
DB_NAME=task_manager_restore_drill python manage.py shell -c \
  "from tasks.models import Task; print(Task.objects.count())"

# 6. Tear down
dropdb task_manager_restore_drill
```

Record the date and outcome of each drill. **RPO: 24 hours. RTO: 1 hour.**

---

## 5. Restore (real incident)

```bash
# Stop everything that writes.
docker compose stop web dispatcher scheduler reaper beat

python manage.py restore_db backups/backup-<stamp>.sqlite3 --confirm
# Production additionally requires --i-understand-this-is-production

python manage.py migrate --check
docker compose start web dispatcher scheduler reaper beat
```

The restore command preserves the pre-restore database alongside the target, so restoring
from the wrong backup is itself undoable.

---

## 6. Deploys and rollback

### Migration policy — expand/contract

Every migration must be reversible; CI fails the build otherwise. Schema changes ship in
two releases so the previous version keeps working during a rollout:

1. **Expand.** Add the new column as nullable, or add the new table. Deploy. Both the old
   and new code run against this schema.
2. **Migrate data.** Backfill in a separate, resumable job.
3. **Contract.** Only after the new version is confirmed stable, drop the old column.

**Index migrations on a large table.** A plain `CREATE INDEX` blocks writes to the table
while it builds. `tasks.0005` (the default-ordering index) was applied this way to the
load-test corpus of roughly 100,000 tasks inside the normal migrate step; build time on a
much larger table has not been measured. Where blocking writes is not acceptable, take the
statement from `python manage.py sqlmigrate tasks 0005`, run it by hand as
`CREATE INDEX CONCURRENTLY ...` with the same index name, and then record the migration as
applied with `python manage.py migrate tasks 0005 --fake`. Django will not skip an index
that already exists, so do not let the migration run normally afterwards.

Never combine an add and a drop in one release: a rollback then has nowhere to go.

### Deploying

```bash
export APP_RELEASE=$(git rev-parse --short HEAD)
docker compose build
docker compose up -d           # the `migrate` service runs first, once
curl -f https://<host>/version # confirm the release stamp changed
```

### Rolling back

```bash
export APP_RELEASE=<previous-sha>
docker compose up -d
```

Because migrations are expand-only within a release, the previous image runs against the
current schema without a schema rollback. If a migration genuinely must be reverted:

```bash
python manage.py migrate <app> <previous_migration_number>
```

Take a backup first. Always.

---

## 7. Scaling

| Symptom | Action |
|---------|--------|
| Queue depth climbing, workers at capacity | Scale `dispatcher` replicas; raise `Queue.concurrency_limit`. |
| One tenant starving the others | Lower that queue's `rate_limit_per_minute`, or pause it. |
| Web latency high, `Server-Timing` large | The web tier is CPU-bound. Raise `WEB_CONCURRENCY` toward one worker per core, then add `web` replicas; check `SLOW_REQUEST_MS` log lines for the offending path. Measured limits are in `CAPACITY.md`. |
| Backlog drains slowly although workers are idle | A dispatcher tick stops after `QUEUE_DISPATCH_TIME_BUDGET_SECONDS` (8 s of every 10 s). Check handler duration before adding replicas. |
| Database CPU high | Check for a missing index; every hot query path has one (see `Meta.indexes`). |

Queue depth history is in `queues_queuemetricsnapshot`, sampled every five minutes.

---

### Attachments stuck on "Scanning for malware"

Uploads are held until clamd returns a verdict, so a scanner outage shows up as files nobody
can download - never as unscanned files being served.

1. `docker compose --profile scanning ps clamav` - is it healthy? It needs about 1.5 GB of
   memory and several minutes after a cold start to load signatures.
2. Scan jobs retry with backoff (8 attempts). Any that ran out are in the dead-letter list;
   redrive them from **Automation** once the scanner is back.
3. After enabling scanning on an existing deployment, run
   `python manage.py scan_attachments` once: older uploads are `not_scanned` and stay held
   until they have been checked.

### A user is locked out of two-step verification

Verify who they are out of band, then delete their device in the admin
(`Identity -> Totp devices`). They can sign in with their password and enrol again. This is
logged; the admin never shows a seed. A workspace locked out by a broken SSO provider is
recovered by an owner, who can always sign in with a password.

### Payment webhooks failing

`POST /billing/webhooks/stripe/` returns 400 for a bad signature (check
`STRIPE_WEBHOOK_SECRET` matches the endpoint in Stripe) and 503 when Stripe's API could not
be reached (Stripe retries on its own). Applied events are listed in the admin under
`Billing -> Billing events`; resend a missing one from the Stripe dashboard.

---

## 8. Escalation

1. **On-call engineer** — first responder, owns the alert for 30 minutes.
2. **Engineering lead** — paged if unresolved after 30 minutes, or immediately for any
   suspected data exposure.
3. **Status communication** — post to the status page within 15 minutes of confirming
   customer impact; update every 30 minutes until resolved.

### Suspected data exposure — do this first

1. Revoke every API key for the affected workspace: `POST /api/v1/api-keys/<id>/` (DELETE).
2. Pull the audit trail: `SELECT * FROM kernel_auditlog WHERE organization_id = <id> ORDER BY
   created_at DESC;` — the log is append-only and cannot have been edited, including by a
   workspace owner.
3. Force re-authentication by rotating `DJANGO_SECRET_KEY` (move the current key into
   `DJANGO_SECRET_KEY_FALLBACKS` only if you need existing sessions to survive; for a
   suspected compromise, do **not**).
4. Escalate to the engineering lead immediately.

---

## 9. Data lifecycle

- **Workspace deletion** is a soft delete with a 30-day retention window
  (`Organization.deleted_at` / `purge_after`). It disappears from every query immediately.
- **Purge** runs nightly at 03:30 and hard-deletes everything belonging to workspaces past
  their window, in an order that respects `PROTECT` foreign keys.
- **Audit logs survive the purge**, detached from the deleted organization. A purge that
  erased its own evidence would be worthless.
- **Restoring within the window:**
  ```bash
  python manage.py shell -c "
  from kernel.models import Organization
  from kernel.services import restore_organization
  org = Organization.objects.get(slug='<slug>')
  restore_organization(organization=org, actor=None)"
  ```
