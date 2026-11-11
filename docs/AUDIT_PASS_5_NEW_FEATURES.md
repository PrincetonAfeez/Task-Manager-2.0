# Phase 3 — Audit Pass Over the New Features

> "This is an enterprise-grade, production-ready SaaS Product. Analyze the code, identify all
> bugs, errors, gaps, and inconsistencies, and document the corresponding fixes. Give me a
> definitive, finite list."

Everything built in Phase 2, audited on the same terms as the original code. Findings are
listed whether they were caught in review or by a failing test.

| # | Severity | Finding | How it surfaced | Fix |
|---|----------|---------|-----------------|-----|
| 5.1 | Critical | **Retry was silently dead.** `Job.CLAIMABLE_STATUSES` omitted `FAILED`, so a job scheduled for retry could never be claimed again. Every "retry" was a permanent stall that never reached the dead-letter queue either. | `test_queues.py::test_exhausted_attempts_dead_letter` failed on the second claim | `FAILED` added to `CLAIMABLE_STATUSES`, with a comment stating that omitting it disables retry entirely. |
| 5.2 | Critical | **API key verification broke for ~1 key in 3.** The token was split with `rpartition("_")`, but the secret is `token_urlsafe`, whose alphabet includes `_`. Any secret containing an underscore split in the wrong place and failed to verify. | `test_api_security.py::test_plaintext_is_never_stored` | Separator changed to `.`, a character base64url can never emit. Regression test constructs a secret containing `_` and `-`. |
| 5.3 | Critical | **Every anonymous API request returned 500 instead of 401.** `ActiveOrganizationMiddleware` set `request.tenant` to a `SimpleLazyObject`, and a lazy proxy wrapping `None` is never `is None` — so the "do you have a workspace?" check passed for anonymous callers and the view then crashed. | 32 tests in `test_api_security.py` | Replaced with `kernel.tenancy.get_tenant()`, which returns a real object or a real `None` and memoises per request. |
| 5.4 | High | **The tenant cache poisoned API-key authentication.** Once `get_tenant` cached a negative result, middleware (which runs *before* DRF authenticates) would cache `None` and the permission check that ran after the key was accepted would read that stale `None` — turning every key-authenticated request into a 403. | probe run during Pass 1 | Negative results are never cached; the reason is documented in the function. |
| 5.5 | High | **Template usage count had a lost-update race.** `update(times_used=template.times_used + 1)` reads a possibly-stale in-memory value, so two concurrent applications both write the same number. | `test_features.py::test_usage_is_counted` | `F("times_used") + 1`. |
| 5.6 | High | **Recurrence occurrence identity was enforced only by a racy read-then-write.** The `(source_task, next_occurrence_date)` unique constraint was redundant — `source_task` is a `OneToOneField`, so it guaranteed nothing. Two concurrent sweeps could both pass the existence check and both create a task. | `test_recurrence.py::test_max_occurrences_exhausts` exposed the redundancy | New partial unique constraint on `Task(recurrence_source, due_date)`, plus an `IntegrityError` catch so the loser advances the rule instead of raising. |
| 5.7 | High | **Webhooks were an SSRF vector by construction.** The URL is customer-supplied; without validation, `http://169.254.169.254/` reaches cloud metadata from inside the worker. | designed in, tested adversarially | Scheme allow-list, credential rejection, DNS resolution with every returned address checked against private/loopback/link-local/reserved ranges, redirects never followed, and production-only HTTPS enforcement. 11 tests including a two-record DNS-rebinding case. |
| 5.8 | High | **A hostile receiver could write unbounded data into our database** via the webhook response body. | review | Response read is capped at `WEBHOOK_MAX_RESPONSE_BYTES` and truncated to 2000 characters before storage. |
| 5.9 | High | **Bulk operations trusted the submitted id list.** A crafted `task_ids` could otherwise reach another tenant's rows. | designed in, tested | Every id is re-scoped through the organization filter before any write; foreign ids are reported as skipped. Batch size capped at 500. |
| 5.10 | High | **CSV export was a formula-injection vector.** A task titled `=cmd\|'/c calc'!A0` executes when the file is opened in Excel. | review | Every cell whose first character is `= + - @ \t \r` is prefixed with `'`. Five parametrised tests. |
| 5.11 | Medium | **Two running timers per user were representable**, double-counting every duration they overlapped. A check-then-write would still race with a second browser tab. | designed in, tested | Partial unique constraint `uniq_running_timer_per_user` on `ended_at IS NULL`. |
| 5.12 | Medium | **A forgotten timer produced absurd durations** that corrupt every average they touch. | review | Capped at 12 hours on stop. |
| 5.13 | Medium | **Account deletion would have destroyed colleagues' work.** Cascading from `User` would remove tasks and comment threads other people depend on; `Task.creator` and `Comment.author` are also `PROTECT`, so a naive delete raises. | review | Authored content is repointed to an inactive `deleted-user` placeholder — the identifying link is severed, the shared history survives. |
| 5.14 | Medium | **Deleting the last owner's account would orphan a workspace** with nobody able to administer, bill or delete it. | review | `blocking_owned_workspaces()` refuses, with an explicit opt-in to delete those workspaces instead. |
| 5.15 | Medium | **A downgrade below current usage would strand the workspace** permanently over quota and unable to create anything. | review | `change_plan` refuses and names which resources are over. |
| 5.16 | Medium | **Bulk "move to list" left the old column attached**, orphaning the card on the new board. | review | `column` is cleared on a list move. |
| 5.17 | Medium | **The purge would have raised rather than completing.** `Task.task_list` and `Job.queue` are `PROTECT`, so `organization.delete()` fails partway, leaving a half-erased tenant. | review | `purge_organization_data` deletes in an explicit dependency order and drops attachment blobs before their rows. |
| 5.18 | Medium | **A purge job for a live workspace would have erased a paying customer's data.** | review | The purge refuses unless `deleted_at` is set, and logs the refusal as an error. |
| 5.19 | Medium | **Share links exposed member identities.** A public URL that enumerates a company's staff is a real leak. | review | The public template renders no assignee, and the response carries `X-Robots-Tag: noindex` and `Cache-Control: no-store`. |
| 5.20 | Medium | **The email handler could silently skip a retry.** The idempotency marker is committed in its own transaction, so an SMTP failure after the marker was taken meant the retry saw "already sent" and dropped the mail. | review | The marker is deleted on send failure before re-raising as retryable. |
| 5.21 | Medium | **Events emitted inside a transaction could announce work that was then rolled back** — a lie the receiver cannot detect. | review | `kernel.events.emit` defers to `transaction.on_commit`; a test asserts a rolled-back task emits nothing. |
| 5.22 | Low | **A webhook endpoint that had been failing for days would occupy the queue forever.** | review | Circuit breaker disables it after 20 consecutive failures; re-enabling from the UI clears the breaker. |
| 5.23 | Low | **`ShareLink` and `Invitation` tokens would have been stored in plaintext**, making a database leak directly replayable. | review | Both store only a SHA-256 digest; the raw value is shown exactly once. |
| 5.24 | Low | **Calendar and analytics views crashed on malformed query parameters** (`?year=abc`, `?days=99999`). | tested | Parsed defensively and clamped to sane ranges. |
| 5.25 | Low | **Exports loaded the whole workspace into memory**, making a large export a reliable way to OOM the web tier. | review | `StreamingHttpResponse` with `iterator(chunk_size=500)`. |

## Verification

`tests/test_webhooks.py` (56 tests) and `tests/test_features.py` (109 tests) cover the above.
Total suite: **640 tests, all passing.**
