# Capacity and Load Testing

What was measured, how, and what it does and does not tell you.

> **Measured 2026-11-10** on a developer laptop, not on production-class hardware. Treat the
> absolute numbers as a floor and a shape, not as a sizing guide: the load generator, the
> database and every application container shared one 4-core CPU. The figures that were in
> this document before this date were not measurements and have been removed.

---

## Method

```bash
# 1. Seed a corpus and get credentials for the generator
python manage.py seed_load --workspaces 20 --tasks-per-workspace 2500 --jobs 2000 --password <pw>

# 2. Drive load (closed loop: each virtual user sends its next request as soon as the
#    previous one returns - there is no think time)
python scripts/loadtest.py --base http://localhost:8010 --password <pw> --api-key <key> \
    --users 16 --duration 20 --warmup 3
```

- One scenario at a time, 20 s each after a 3 s warm-up, against the stack in
  `docker-compose.yml` running with `DJANGO_ENV=production`.
- Latency is reported twice: as seen by the client, and as measured inside the application
  (the `Server-Timing` header). The gap between them is time spent queued for a worker.
- **Every virtual user hit the same workspace**, which grew from about 43,000 to 56,000
  tasks during the run. This is the worst case for the per-workspace queries; a fleet of
  small workspaces behaves better.
- Rate limiting was switched off (`API_THROTTLING_ENABLED=0`) so the limiter was not what
  was being measured. It is on by default.
- "16 users" with no think time is far heavier than 16 people. At one request every 20
  seconds per person, 16 closed-loop users at 20 req/s correspond to roughly 400 people
  active at once.

## Environment

| Component | As tested |
|-----------|-----------|
| Host | Windows 11 laptop, Intel i7-1185G7 (4 cores / 8 threads), Docker Desktop 29.9, 16 GB to the VM |
| Web | 1 container, gunicorn, 2 threads per worker, `WEB_CONCURRENCY` 3 or 6 |
| Database | PostgreSQL 17 (container, default configuration, no tuning) |
| Cache / broker | Redis 7 (container) |
| Workers | 1 dispatcher (concurrency 4), 1 scheduler, 1 reaper, beat; ClamAV running idle |
| Corpus | 21 workspaces, 103,000 tasks in total, 55,760 in the workspace under load |
| Load generator | Same machine, Python threads |

## Results

No request failed in any run (0 errors across every scenario).

### 3 web workers

| Scenario | 4 users: req/s | p50 | p95 | 16 users: req/s | p50 | p95 |
|----------|---:|---:|---:|---:|---:|---:|
| `GET /` (dashboard) | 46.9 | 81 ms | 114 ms | 17.7 | 636 ms | 1,846 ms |
| `GET /tasks/` | 67.1 | 55 ms | 81 ms | 20.2 | 512 ms | 2,086 ms |
| `GET /tasks/?q=…` (search) | 39.0 | 101 ms | 127 ms | 10.8 | 961 ms | 3,422 ms |
| `GET /board/` | 17.5 | 185 ms | 429 ms | 4.7 | 2,752 ms | 7,210 ms |
| `GET /api/v1/tasks/` | 44.8 | 83 ms | 153 ms | 18.6 | 740 ms | 1,907 ms |
| `POST /quick-add/create/` | 62.2 | 59 ms | 104 ms | 41.8 | 353 ms | 779 ms |
| `POST /tasks/<id>/toggle/` | 37.2 | 101 ms | 172 ms | 34.5 | 426 ms | 1,046 ms |
| `POST /api/v1/tasks/` | 41.9 | 85 ms | 159 ms | 78.7 | 178 ms | 399 ms |

### 6 web workers

| Scenario | 16 users: req/s | p50 | p95 | 32 users: req/s | p50 | p95 |
|----------|---:|---:|---:|---:|---:|---:|
| `GET /` (dashboard) | 26.6 | 539 ms | 1,316 ms | 29.1 | 1,066 ms | 2,038 ms |
| `GET /tasks/` | 41.7 | 320 ms | 877 ms | 50.5 | 608 ms | 1,175 ms |
| `GET /tasks/?q=…` (search) | 25.6 | 540 ms | 1,259 ms | 21.1 | 1,390 ms | 3,064 ms |
| `GET /board/` | 12.4 | 1,143 ms | 2,923 ms | 12.0 | 2,618 ms | 4,784 ms |
| `GET /api/v1/tasks/` | 48.6 | 285 ms | 638 ms | 42.6 | 551 ms | 1,977 ms |
| `POST /quick-add/create/` | 73.6 | 145 ms | 567 ms | 82.7 | 389 ms | 675 ms |
| `POST /tasks/<id>/toggle/` | 69.0 | 163 ms | 595 ms | 56.5 | 577 ms | 992 ms |
| `POST /api/v1/tasks/` | 112.2 | 126 ms | 267 ms | 75.7 | 328 ms | 1,005 ms |

### Single request, idle system (55,000-task workspace)

| Page | Total | Of which SQL | Queries |
|------|------:|-------------:|--------:|
| Dashboard | 59 ms | 26 ms | 19 |
| Task list | 54 ms | 24 ms | 15 |
| Board | 130 ms | 54 ms | 21 |
| API task list | 33 ms | 14 ms | 10 |

### Automation

| Scenario | Result |
|----------|--------|
| Backlog drain, 1 dispatcher at concurrency 4, notification jobs | about 15 jobs/s |
| Schedule scan, webhook delivery throughput | **not measured** |

## What the numbers say

1. **Within its capacity the product is fast.** At 4 concurrent requests every page except
   the board answers in about 150 ms at p95, on a workspace far larger than most will be.
2. **The web tier is CPU-bound and that is the first limit.** Going from 3 to 6 workers
   roughly doubled read throughput at 16 users (task list 20 to 42 req/s, API 19 to 49).
   Past saturation, latency is queueing: server-side time stays in the hundreds of
   milliseconds while the client waits seconds. Scale with `WEB_CONCURRENCY` (about one per
   core), then with replicas.
3. **The board is the most expensive page** at roughly four times a task-list request. It is
   bounded (50 cards per column, the rest on demand) but still renders up to 200 cards.
4. **Search is the next scaling limit.** `icontains` cannot use an index, so search cost
   grows with the workspace; it was the only read path that did not benefit from the
   ordering index. PostgreSQL full-text search with a GIN index is the fix when a real
   workspace approaches this size.
5. **Run-to-run variation on this hardware is large** (a laptop throttles under sustained
   load). Individual cells can move by 30% or more between runs; the ordering of the
   scenarios and the effect of adding workers were consistent.

## Defects this test found

Each was invisible to functional tests and is now pinned by `tests/test_scale_regressions.py`.

| Defect | Before | After |
|--------|--------|-------|
| Board rendered every card in the list | 7.5 s median, 1.9 req/s (2,500-task list) | bounded lanes, read through the lane index |
| Dispatcher claimed one batch per 10 s tick | 0.8 jobs/s; a 2,000-job backlog would take about 40 min | loops within a time budget, about 15 jobs/s |
| Task list and API used `DISTINCT` over every row | 55 ms + 27 ms per request at 19,000 tasks | removed; a single-label join cannot duplicate |
| Every reopen of a task added a reminder row, all loaded by the dashboard | about 4,500 objects and 490 ms per dashboard view | one reminder per due time; dashboard loads armed ones only (108 ms) |
| A completed-then-reopened task with a future due date lost its reminder | reminder never re-armed | re-armed with a live job |
| List views sorted the whole workspace on every request | 11 ms for ids alone at 43,000 tasks | 0.09 ms through `task_default_order_idx` |

## Query profile

| Query | Index |
|-------|-------|
| First page of tasks in default order (dashboard, task list, API) | `task_default_order_idx` (partial, unarchived) |
| Dashboard counts | `(organization, status, due_date)` |
| My open tasks | `(organization, assignee, status)` |
| Board lanes | `(task_list, column, position)` |
| Activity and audit feeds | `(organization, -created_at)` |
| Job claiming | `(queue, status, available_at)` |
| Lease reaping | `(status, lease_expires_at)` |

The dashboard issues a fixed 19 queries regardless of workspace size. The board issues one
query per column plus three, regardless of how many tasks the list holds.

## Caching strategy

Deliberately minimal. Almost everything shown is per-tenant, per-user and mutable, so a
cache would mostly serve stale data and add an invalidation bug surface.

| Cached | TTL | Invalidation |
|--------|-----|--------------|
| Throttle counters | Rate window | Expiry |
| Login and two-step lockout counters | 15 min | Expiry, or reset on success |
| SSO discovery documents | 1 h | Expiry |
| Session data | 12 h | Expiry, or logout |
| Static assets | 1 year | Content hash in the filename |

Nothing customer-visible is cached, so there is no invalidation strategy to get wrong.

## Not covered by this test

- Production-class hardware, a separate database host, or more than one web replica.
- Many workspaces under load at once (all load here was on one).
- Sustained runs: each scenario was 20 seconds, which says nothing about memory growth or
  connection behaviour over hours.
- Webhook delivery, schedule scanning, exports and attachment scanning throughput.
- Behaviour with rate limiting on, which is how it runs in production.

## Re-running

Re-measure before any release that changes a query on a hot path, and quarterly otherwise.
Record the date, the hardware and the numbers; a capacity figure without them is not evidence.
