# Service Level Objectives

Targets the product is engineered and monitored against. These are **internal objectives**,
not a contractual SLA — the README states plainly that no uptime commitment is offered.

---

## Core endpoint objectives

| Journey | Endpoint | Latency objective | Availability objective |
|---------|----------|-------------------|------------------------|
| Load the dashboard | `GET /` | p95 < 400 ms, p99 < 900 ms | 99.5% monthly |
| List and filter tasks | `GET /tasks/` | p95 < 500 ms, p99 < 1200 ms | 99.5% monthly |
| Quick add a task | `POST /quick-add/create/` | p95 < 300 ms | 99.9% monthly |
| Complete a task | `POST /tasks/<id>/toggle/` | p95 < 250 ms | 99.9% monthly |
| API list | `GET /api/v1/tasks/` | p95 < 350 ms | 99.5% monthly |
| Sign in | `POST /accounts/login/` | p95 < 600 ms | 99.9% monthly |

Write paths carry the tighter availability target deliberately: a customer who cannot read
their tasks is inconvenienced, and a customer who cannot *record* one loses information.

## Automation objectives

| Objective | Target |
|-----------|--------|
| Reminder fires within its window | 99% within 60 s of `remind_at` |
| Schedule fires within its window | 99% within 60 s of `scheduled_time` |
| Webhook first-attempt delivery | 95% within 30 s of the event |
| Queue backlog age (p95) | < 120 s |
| Job success rate excluding customer-caused 4xx | > 99% |
| Dead letters per 10k jobs | < 5 |

## Error budget

99.5% monthly availability allows roughly **3h 39m** of downtime per 30 days.

Policy when the budget is exhausted:

1. Feature work stops; reliability work takes priority.
2. Every incident in the window gets a written post-mortem.
3. Deploys are restricted to fixes until the trailing 30-day window recovers.

## Instrumentation

Everything above is measurable from what the application already emits — no objective is
stated here that cannot actually be observed:

| Signal | Source |
|--------|--------|
| Request latency | `Server-Timing` header and the `slow request` log line (`SLOW_REQUEST_MS`) |
| Request outcome | Structured access logs, correlated by `X-Request-ID` |
| Queue depth and age | `queues_queuemetricsnapshot`, sampled every 5 minutes |
| Job outcomes | `queues_job.status` and `queues_jobattempt` |
| Webhook delivery | `integrations_webhookdelivery.status` and `duration_ms` |
| Component health | `/readyz`, `/healthz/worker`, `/healthz/scheduler`, `/healthz/reaper` |

## Alerting

| Alert | Condition | Route |
|-------|-----------|-------|
| Site down | `/readyz` failing for 2 min | Page on-call |
| Worker stalled | `/healthz/worker` 503 for 5 min | Page on-call |
| Scheduler stalled | `/healthz/scheduler` 503 for 10 min | Page on-call |
| Leases stranded | `/healthz/reaper` 503 for 10 min | Page on-call |
| Queue backlog | depth > 500 or oldest > 15 min | Ticket, page if sustained 30 min |
| Dead letters | > 10 unacknowledged | Ticket |
| Error rate | 5xx > 1% over 5 min | Page on-call |
| Latency | p95 above objective for 15 min | Ticket |

Alert-to-action procedures are in [`RUNBOOK.md`](RUNBOOK.md). Every alert above maps to a
named action there; an alert with no documented response is a pager that trains people to
ignore it.

## Capacity

Measured on 2026-11-10 on a 4-core laptop with every component sharing the CPU, against one
55,000-task workspace (see `docs/CAPACITY.md` for method, raw numbers and caveats). These
are a floor, not a sizing guide:

| Dimension | Observed | Action threshold |
|-----------|----------|------------------|
| Dashboard, 3 web workers | 47 req/s at p95 114 ms (4 concurrent); saturated at about 18 req/s with p95 1.8 s (16 concurrent, no think time) | Raise `WEB_CONCURRENCY` or add a replica when `Server-Timing` p95 exceeds the objective |
| Dashboard, 6 web workers | 27 req/s at 16 concurrent, 29 req/s at 32 | Web tier is CPU-bound: about one worker per core |
| Job execution | About 15 jobs/s for one dispatcher at concurrency 4 | Add a dispatcher replica when backlog age p95 nears 120 s |
| Tasks per workspace | Tested to 55,000 in one workspace | Search (`icontains`) is the first thing to degrade; move to full-text search |
| Workspaces | Tested with 21 | Not load-tested at scale |

**Objectives versus measurement.** Within capacity (4 concurrent requests) every latency
objective above was met with margin: dashboard p95 114 ms against 400 ms, task list 81 ms
against 500 ms, quick add 104 ms against 300 ms, toggle 172 ms against 250 ms, API list
153 ms against 350 ms. At saturation none of the read objectives is met; that is a capacity
signal, and the alert on `Server-Timing` exists to catch it before users do. Sign-in latency
and every availability objective are **targets only** - there is no production traffic to
measure them against yet.

## Review

Reviewed monthly against actual measurements. An objective that is consistently met by a
wide margin is too loose; one that is consistently missed is either wrong or a signal that
the system needs work. Both are worth acting on.
