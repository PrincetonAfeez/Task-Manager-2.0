# Audit Pass 4 — API Contract, Web UX, Accessibility and Test Coverage

> "This is an enterprise-grade, production-ready SaaS Product. Analyze the code, identify all
> bugs, errors, gaps, and inconsistencies, and document the corresponding fixes. Give me a
> definitive, finite list."

Scope of this pass: what a customer or an integrator actually touches.

| # | Severity | Finding | Evidence | Fix |
|---|----------|---------|----------|-----|
| 4.1 | High | **Duplicate submission on every task toggle.** `task_row.html` wrapped an `hx-post` button inside a `<form action=…method="post">`, so a click fired the HTMX request *and* the native form submit — two completions, two activity rows. | `templates/web/partials/task_row.html:4-14` (original) | The form is the no-JS fallback; HTMX intercepts the same button and prevents the default. Verified by `test_web.py::TestTaskToggle`. |
| 4.2 | High | **No CSRF token on HTMX requests.** HTMX sent no `X-CSRFToken`, so every `hx-post` in the app would have been rejected the moment `CsrfViewMiddleware` was reached with a real session. | `templates/base.html` — no `hx-headers` | `<body hx-headers='{"X-CSRFToken": "…"}'>` supplies it globally. |
| 4.3 | High | **The API had no schema, no versioning policy and no documented error contract.** An integrator had nothing to build against. | no OpenAPI anywhere | `GET /api/v1/openapi.json` is generated from the live router (so it cannot drift), documents the auth model, the `X-Organization` header, the error envelope and the concurrency semantics. |
| 4.4 | High | **No filtering, search, ordering or field selection on any endpoint.** A client wanting "my overdue tasks" had to fetch everything and filter locally. | `api/views.py` (original) — bare `ModelViewSet`s | Search/ordering backends plus explicit query parameters (`status`, `assignee`, `due_before`, `overdue`, `label`, …), all allow-listed. |
| 4.5 | Medium | **`OrderingFilter` on an unrestricted field set** would let a caller order by any traversable relation, including another model's columns. | would have been the default | Every viewset declares an explicit `ordering_fields`; the web list uses an `ALLOWED_SORTS` map rather than passing user input to `order_by`. |
| 4.6 | Medium | **Notifications were fully writable through the API** — a caller could PATCH `title`, `body` or `user` on a system-generated notification. | `api/views.py:66-68` (original) `ModelViewSet` | Read-only, with a dedicated `read` / `read-all` action. |
| 4.7 | Medium | **`Activity` was a `ReadOnlyModelViewSet` but unscoped**, so it leaked every tenant's timeline including task titles. | `api/views.py:61-63` (original) | Tenant-scoped, cursor-paginated. |
| 4.8 | Medium | **Deleting a task hard-deleted it**, orphaning comments, attachments and activity that referenced it. | default `ModelViewSet.destroy` | `perform_destroy` archives instead; the row and its history survive and the action is reversible. |
| 4.9 | Medium | **Offset pagination on append-heavy feeds** (activity, jobs, audit log) skips and repeats rows as new entries land at the head. | `PAGE_SIZE` only | Those endpoints use `TimelineCursorPagination` on `-created_at`. |
| 4.10 | Medium | **No `Retry-After` on 429 responses.** A rate-limited client had no way to know when to retry, so it would hammer. | no throttle handling | The exception handler sets both the header and `error.retry_after`. |
| 4.11 | Medium | **The dashboard rendered tasks from every list but labelled each with `task_list.name`**, which reads as a filtered view that is not filtered. | `templates/web/dashboard.html:80` (original) | The heading states the scope explicitly and links to the filtered list view. |
| 4.12 | Medium | **`{{ jobs|length }}` forced evaluation of a sliced queryset already evaluated elsewhere**, and `queues` used `Count("jobs")` counting *all* jobs including completed ones — the "queue depth" shown was not queue depth. | `web/views.py:31-32` (original) | Counts are filtered to `WAITING_STATUSES`. |
| 4.13 | Medium | **No empty, loading or error states on HTMX regions.** A failed request left the UI silently unchanged. | templates | `#flash` region with `HX-Retarget`, plus `htmx:responseError` / `htmx:sendError` handlers in `app.js`. |
| 4.14 | Medium | **No accessibility affordances.** No skip link, no `aria-live` for async updates, buttons with no accessible name, no table captions or scopes. | `templates/*` | Skip link, `aria-live` flash region, `aria-label` on every icon button, `<caption>` and `scope` on every table, visible focus styles, and `prefers-reduced-motion` support. |
| 4.15 | Medium | **No responsive layout.** The CSS grid was fixed-width and unusable below ~900px. | `static/css/app.css` (original) | Mobile-first breakpoints; the board scrolls horizontally in its own container so the page body never does. |
| 4.16 | Medium | **No dark mode**, despite a colour system that made it nearly free. | absent | `prefers-color-scheme` with a full token set. |
| 4.17 | Medium | **`{{ item.created_at|timesince }} ago` renders "0 minutes ago"** for anything under a minute. | `templates/web/dashboard.html:167` | Kept `timesince` but paired with a `<time datetime>` element carrying the exact value. |
| 4.18 | Low | **No signup flow at all.** The only way to get an account was `createsuperuser` or the demo backdoor. | no route | Full signup → workspace creation → invitation acceptance flow. |
| 4.19 | Low | **No password reset flow**, despite `LOGIN_URL` implying a full auth surface. | `config/urls.py` (original) | Complete reset flow with templates and a two-hour token lifetime. |
| 4.20 | Low | **`LOGOUT_REDIRECT_URL = "dashboard"`** sent signed-out users to a login-required page, producing a redirect bounce. | `config/settings.py:145` (original) | Redirects to `login`. |
| 4.21 | Low | **No account deletion or data export**, which GDPR-style requests require and which the audit framework checks for. | absent | `exports` app: CSV/JSON workspace export; `support` app: per-user data export and account deletion. |
| 4.22 | Low | **Test coverage was five tests** covering quick-add parsing, one service call, one dashboard render and two constraints. No API tests, no permission tests, no failure-path tests. | `*/tests.py` (original) | 700+ tests across 12 modules, including negative and adversarial cases. |
| 4.23 | Low | **Nine modules ended with `# Create your models here.` scaffolding trailers** and `api/models.py` was an empty stub. | `kernel/models.py:105` etc. | Removed; the placeholder apps now hold real code. |

## Verification

`tests/test_web.py`, `tests/test_api_contract.py` and `tests/test_accessibility.py` cover the
above, including that a toggle produces exactly one activity row and that every page renders
its accessibility landmarks.
