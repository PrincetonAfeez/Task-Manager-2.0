# Audit Pass 3 — Deployability, Operations and Observability

> "This is an enterprise-grade, production-ready SaaS Product. Analyze the code, identify all
> bugs, errors, gaps, and inconsistencies, and document the corresponding fixes. Give me a
> definitive, finite list."

Scope of this pass: everything between "the code is correct" and "it runs, survives, and can
be recovered".

| # | Severity | Finding | Evidence | Fix |
|---|----------|---------|----------|-----|
| 3.1 | Critical | **Health endpoints lied.** `worker_healthz`, `scheduler_healthz` and `reaper_healthz` returned `{"status": "ok"}` unconditionally with a `"placeholder"` note. Any uptime monitor pointed at them would report green while the entire worker fleet was down. | `ops/views.py:16-27` (original) | Each endpoint now measures real state (recent completions, backlog age, overdue schedules, stranded leases) and returns **503** when degraded, so a load balancer or pager actually reacts. |
| 3.2 | Critical | **`readyz` raised instead of failing.** An unreachable database produced an unhandled exception → HTTP 500 with a traceback, not a structured 503. | `ops/views.py:9-13` (original) | Catches per-dependency, returns `503` with a per-check breakdown, and never leaks the exception text. |
| 3.3 | High | **No backup or restore procedure existed anywhere.** No script, no documentation, no tested path. | absent from repo | `ops/management/commands/backup_db.py` + `restore_db.py`, and `docs/RUNBOOK.md` with a **tested** restore drill (`make backup-verify` restores into a scratch database and runs `migrate --check`). |
| 3.4 | High | **No rollback story.** No versioned releases, no migration reversibility policy, no documented procedure. | absent | `docs/RUNBOOK.md` documents the expand/contract migration rule, `APP_RELEASE` stamping, and a step-by-step rollback; `make check-migrations` fails the build on a non-reversible migration. |
| 3.5 | High | **Docker image could not serve static files.** No `collectstatic` in the build, so with `DEBUG=0` every CSS and JS asset 404s. | `Dockerfile:1-12` (original) | `collectstatic --noinput` at build time, plus WhiteNoise middleware and the compressed manifest storage backend. |
| 3.6 | High | **Nothing ran migrations.** `docker-compose.yml` started `gunicorn` directly against a database that had never been migrated. | `docker-compose.yml:3` (original) | An `entrypoint.sh` that waits for the database, runs `migrate --noinput`, then execs the process; plus a dedicated one-shot `migrate` service that the app services `depends_on`. |
| 3.7 | High | **The Compose stack had no health checks and no dependency ordering.** `depends_on` without `condition: service_healthy` starts the web container before Postgres accepts connections. | `docker-compose.yml:23-25` (original) | Health checks on Postgres, Redis, web, and every worker; `depends_on: condition: service_healthy` throughout. |
| 3.8 | High | **No CI.** Nothing ran the tests, the deploy check, or a dependency audit on any change. | no `.github/` directory | `.github/workflows/ci.yml`: lint → `check --deploy` → migration reversibility → full pytest against **both** SQLite and Postgres → `pip-audit` → `bandit` → Docker build. |
| 3.9 | High | **Secrets were hardcoded in `docker-compose.yml`** (`POSTGRES_PASSWORD: task_manager`) and duplicated across five services via a YAML anchor, so rotating one meant editing five places. | `docker-compose.yml:35-38` (original) | All configuration moves to `.env` (gitignored) with `.env.example` as the template; Compose reads `${VAR:?err}` so a missing secret fails the stack instead of silently defaulting. |
| 3.10 | High | **Celery had no beat schedule.** Even with a working dispatcher, nothing would ever have *triggered* the dispatcher, reaper, scheduler scan or recurrence sweep. | `config/celery.py:1-6` (original) | `beat_schedule` defines all six periodic tasks with explicit intervals and queue routing. |
| 3.11 | Medium | **`docker-compose` ran `celery -A config worker -Q dispatcher`** against queues no task was ever routed to, so those workers idled forever. | `docker-compose.yml:28` (original) | Explicit `task_routes` in `config/celery.py` matching the queue names the worker services consume. |
| 3.12 | Medium | **No graceful shutdown.** A deploy during a long job killed it mid-flight with no drain. | no signal handling | Celery `--max-tasks-per-child` and warm-shutdown defaults, a `stop_grace_period` in Compose, and the lease/reaper design that makes an ungraceful kill *recoverable* rather than lossy. |
| 3.13 | Medium | **No structured logging or request correlation.** A customer-reported failure could not be traced from the web tier into the worker. | no `LOGGING` in settings | JSON formatter, `X-Request-ID` middleware, a `contextvar` the worker inherits via `Job.correlation_id`, and per-logger levels. |
| 3.14 | Medium | **No error tracking integration.** 500s vanished into stdout. | absent | Optional Sentry wiring gated on `SENTRY_DSN`, with `send_default_pii=False`. |
| 3.15 | Medium | **No slow-request visibility.** Latency regressions were invisible until a customer complained. | absent | `RequestTimingMiddleware` emits a `Server-Timing` header and logs anything over `SLOW_REQUEST_MS`. |
| 3.16 | Medium | **`Makefile` targets did not match reality** — `make test` called `manage.py test`, and there was no lint, audit, worker, or scheduler target. | `Makefile:1-17` (original) | Rewritten around pytest with `lint`, `audit`, `worker`, `scheduler`, `reaper`, `backup`, `restore`, `smoke` and `ci` targets. |
| 3.17 | Medium | **`README.md` documented a login (`demo` / `demo`) that is now a security bug**, and described features that did not work (reminders, schedules). | `README.md:24-29` (original) | Rewritten to match actual behaviour, with an explicit "Known limitations" section (see 3.22). |
| 3.18 | Medium | **No dependency pinning or vulnerability scanning.** `psycopg[binary]>=3.2` and friends were unbounded ranges. | `requirements.txt:7-9` (original) | Upper bounds on every dependency, a split `requirements-dev.txt`, `pip-audit` in CI, and Dependabot config. |
| 3.19 | Medium | **The Docker image ran as root during build and copied the whole context**, including `db.sqlite3` with real password hashes. | `Dockerfile:10` `COPY . .` with no `.dockerignore` | Added `.dockerignore`; multi-stage build; non-root user owns only what it needs. |
| 3.20 | Medium | **No incident or on-call documentation.** Nobody knew who to page or what to do. | absent | `docs/RUNBOOK.md` covers alert→action for every alarm the system can raise, plus escalation and status-communication steps. |
| 3.21 | Low | **No `HEALTHCHECK` in the Dockerfile**, so an orchestrator could not tell a wedged container from a healthy one. | `Dockerfile` | Added, pointing at `/healthz`. |
| 3.22 | Low | **Scope dishonesty.** The README described the project as a "capstone scaffold" while `docker-compose.yml` presented a five-service production topology. | `README.md:3` vs `docker-compose.yml` | The README now states the stage, what is implemented, and what is explicitly out of scope. |
| 3.23 | Low | **`exports/`, `support/` apps were empty stubs in `INSTALLED_APPS`**, implying features that did not exist. | `exports/models.py:2` | `exports` now implements real CSV/JSON export; `support` holds the account-lifecycle (data export / deletion) code. |

## Verification

- `make ci` runs the full gate locally and mirrors `.github/workflows/ci.yml`.
- `make backup-verify` performs a real backup-and-restore round trip and asserts the
  restored database is migration-clean — an untested backup is not a backup.
- `tests/test_ops.py` asserts each health endpoint returns 503 under the degraded condition
  it is supposed to detect.
