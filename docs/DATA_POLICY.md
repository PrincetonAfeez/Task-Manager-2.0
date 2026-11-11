# Data Retention, Residency and Protection

What the product stores, for how long, where, and what a customer can demand of it.

---

## 1. What is stored

| Category | Examples | Contains PII |
|----------|----------|--------------|
| Account | username, email, password hash, last login | Yes |
| Workspace | name, slug, timezone, plan | No |
| Membership | user ↔ workspace, role, join date | Yes (by reference) |
| Task content | titles, notes, comments, attachments | Customer-determined |
| Activity | who did what, when | Yes (by reference) |
| Audit log | security actions, actor, IP address, user agent | Yes |
| Automation | job payloads, schedules, webhook deliveries | Customer-determined |
| Operational | request logs with correlation ids | IP addresses |

**Customer-determined** means we cannot know whether a field holds personal data — a task
titled "Call Jane about her contract" does. It is treated as if it does.

## 2. Retention

| Data | Retention | Mechanism |
|------|-----------|-----------|
| Live workspace data | While the workspace exists | — |
| Deleted workspace | **30 days**, then hard-purged | `Organization.purge_after`, nightly purge job |
| Deleted account | Immediate; authored content anonymised | `support.services.delete_account` |
| Audit log | **2 years**, survives workspace deletion (detached) | Retained deliberately; see §5 |
| Job rows | 90 days after reaching a terminal state | Operational pruning |
| Queue metric snapshots | 30 days | Operational pruning |
| Webhook deliveries | 30 days | Operational pruning |
| Application logs | 30 days | Log platform retention |
| Database backups | 7 daily local, plus platform snapshots | `backup_db --keep` |

## 3. Residency

**Single region.** All data lives in one region, chosen at deployment time. There is no
per-customer residency control and no multi-region replication.

This is an accepted limitation, stated in the README. A customer with a residency
requirement cannot be served by this deployment, and telling them so is the only honest
answer.

## 4. Protection

| Control | Implementation |
|---------|----------------|
| Passwords | Argon2 when available, PBKDF2 otherwise; never logged, never exported |
| API keys | SHA-256 digest only; plaintext shown once and never recoverable |
| Invitation tokens | SHA-256 digest only; single-use, 7-day expiry |
| Share link tokens | SHA-256 digest only; revocable, optionally expiring |
| Webhook secrets | Stored per endpoint; used only to sign, never transmitted |
| In transit | TLS enforced in production (`SECURE_SSL_REDIRECT`, HSTS with preload) |
| At rest | Provided by the platform's volume encryption |
| Tenant isolation | Enforced in `get_queryset`, verified by a dedicated test module |
| Access logging | Append-only audit log; immutable at the model layer |

### What is deliberately not logged

Passwords, password hashes, API key plaintexts, invitation and share tokens, webhook
secrets, and task or comment bodies. Job payloads *may* contain customer content and are
therefore treated as customer data, not as operational telemetry.

## 5. Why the audit log outlives the workspace

When a workspace is purged, its audit entries are **detached** (`organization` set to NULL)
rather than deleted.

A record of who deleted what, and when, is the only evidence available after the fact. A
purge that erased its own evidence would make the audit log worthless precisely when it
matters most. The entries retain the action, timestamp and actor reference; the workspace
contents they described are gone.

## 6. Customer rights

| Right | How it is served | Where |
|-------|------------------|-------|
| Access | Personal data export (JSON) | `/account/export/` |
| Portability | Workspace export (CSV/JSON) | `/export/tasks/`, `/export/workspace/` |
| Erasure — account | Self-service deletion | `/account/` |
| Erasure — workspace | Owner deletes; purged after 30 days | `/settings/workspace/` |
| Rectification | Edit any field in the product | — |
| Restriction | Leave a workspace, or suspend a membership | `/settings/workspace/leave/` |

All are self-service. None requires a support ticket, which is the difference between a
right that exists and a right a customer can actually exercise.

## 7. Sub-processors

See [`SUBPROCESSORS.md`](SUBPROCESSORS.md).

## 8. Breach response

Covered in [`RUNBOOK.md`](RUNBOOK.md) §8, "Suspected data exposure". First actions: revoke
the workspace's API keys, pull the immutable audit trail, rotate the signing key, escalate.

## Identity and billing data

Added with two-step verification, single sign-on, payments and attachment scanning.

| Data | Stored as | Removed when |
|------|-----------|--------------|
| Authenticator (TOTP) seed | Encrypted with `FIELD_ENCRYPTION_KEYS` | The user turns two-step verification off, or deletes their account |
| Recovery codes | Keyed hash only; shown once | Regenerated, two-step verification turned off, or account deleted |
| SSO identity (provider subject id, email) | Plain | Account deleted, or the workspace is purged |
| SSO client secret | Encrypted with `FIELD_ENCRYPTION_KEYS` | Workspace purged |
| Stripe customer and subscription ids | Plain. **No card data is stored or handled.** | Workspace purged. The customer record at Stripe is governed by Stripe's own retention and must be deleted there |
| Applied payment events | Event id and type only, no payload | Detached from the workspace on purge, like audit entries |
| Attachment scan verdict and signature name | Plain | With the attachment |

A personal data export includes account and membership data; it does not include the
authenticator seed, which is a credential rather than personal data and is never exposed.

