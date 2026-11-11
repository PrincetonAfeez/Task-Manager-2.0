# Security Review

**Scope:** the whole application.
**Type:** internal structured review with automated tooling.
**Date:** 2026-07-24.

> **This is not a third-party penetration test.** No external firm has assessed this
> application. That gap is stated rather than papered over, because a customer reading a
> document called "Security Review" is entitled to know exactly what it is.

---

## Method

1. **Structured audit passes** over the whole codebase - security and configuration, domain
   correctness, operations, and API/UX. Findings and fixes are recorded in
   `AUDIT_PASS_1.md` through `AUDIT_PASS_4.md`, and `AUDIT_PASS_5_NEW_FEATURES.md` for the
   features added afterwards.
2. **Adversarial test authoring.** Tests were written to *attack* each control, not to
   confirm it: cross-tenant reads, id enumeration, privilege escalation, replay, SSRF,
   injection, and concurrency races.
3. **Automated scanning.** `bandit` (static security lint), `pip-audit` (dependency CVEs),
   `ruff` with the security rule set, and `manage.py check --deploy --fail-level WARNING`
   against the production profile.

## Attack surface

| Surface | Exposure | Primary control |
|---------|----------|-----------------|
| Web UI | Authenticated session | `workspace_required` / `permission_required` on every view |
| Login and signup | Public | Per-IP throttle plus per-account lockout |
| Password reset | Public | Single-use, 2-hour token; no account enumeration |
| REST API | Session or API key | Tenant-scoped `get_queryset`, role matrix, throttles |
| Public share links | Public, tokenised | Hashed token, read-only, one list, revocable |
| Health endpoints | Public | No customer data in any response |
| Django admin | Staff session | Configurable path; audit log is read-only there |
| Outbound webhooks | We are the client | SSRF validation, no redirects, hard timeouts |

## Findings and disposition

| Class | Found | Status |
|-------|-------|--------|
| Unauthenticated access to all tenant data via the API | 1 | Fixed - `AUDIT_PASS_1.md` 1.1 |
| Cross-tenant object access by id | 1 | Fixed - 1.2 |
| Anonymous auto-login to a shared demo workspace | 1 | Fixed - 1.3 |
| Insecure-by-default configuration (DEBUG, SECRET_KEY, cookies, HSTS) | 5 | Fixed - 1.5-1.8 |
| Missing rate limiting and brute-force protection | 1 | Fixed - 1.9 |
| API key verification failure on ~1 key in 3 | 1 | Fixed - `AUDIT_PASS_5` 5.2 |
| Authorization check that passed for anonymous callers | 1 | Fixed - 5.3 |
| SSRF via customer-supplied webhook URLs | 1 | Mitigated by design - 5.7 |
| CSV formula injection in exports | 1 | Fixed - 5.10 |
| Bulk operations trusting a client-supplied id list | 1 | Fixed - 5.9 |
| Purge crashing and rolling back a completed erasure | 1 | Fixed - 5.x |
| Expired invitations never marked, blocking re-invites | 1 | Fixed - 5.x |

## Controls verified by test

Each of the following has at least one test that fails if the control is removed:

- Anonymous access to every API route returns 401/403, never data.
- A foreign object id returns **404, not 403** - 403 would confirm the id exists.
- `organization` is never writable from a request body.
- An API key cannot be granted more access than its creator holds.
- A key cannot be widened to another workspace by any header.
- An admin cannot escalate to owner, nor change a peer admin.
- The last owner cannot be removed or demoted.
- Audit entries cannot be modified or deleted, including by an owner.
- Webhook URLs resolving to private, loopback or link-local addresses are refused.
- Redirects are never followed on outbound delivery.
- Stored XSS payloads come back escaped.
- Every POST form carries a CSRF token (enforced by a codebase-wide test).
- Two concurrent editors cannot silently overwrite each other.
- Two workers cannot execute the same job.
- A correct password alone does not create a session for an account with two-step
  verification; the admin login form cannot be used to skip the second step.
- A one-time code cannot be replayed, and five wrong codes lock verification.
- TOTP seeds and SSO client secrets are encrypted at rest; recovery codes are stored as
  keyed hashes.
- An ID token that is unsigned, HMAC-signed, signed by another key, expired, for another
  audience or issuer, or carrying another login's nonce is refused.
- An SSO callback with a forged, replayed, stale or cross-browser `state` is refused.
- An identity provider cannot take over an account that is not already in its workspace.
- A workspace that requires SSO or two-step verification enforces it on existing sessions
  and on session-authenticated API calls, not only at sign-in.
- A Stripe webhook with a wrong, missing, re-timestamped or stale signature is refused; a
  duplicate event is applied once; state is taken from Stripe's API, never the event body.
- An attachment cannot be downloaded until the scanner has declared it clean; an infected
  file is deleted from storage; a scanner outage holds the file rather than releasing it.

## Automated scan results

| Tool | Result |
|------|--------|
| `bandit --severity-level medium` | 0 medium, 0 high |
| `pip-audit` | No known vulnerabilities |
| `ruff` (incl. security rules) | Clean |
| `manage.py check --deploy --fail-level WARNING` | Clean under the production profile |

### Scanner verified against a real daemon

On 2026-11-10 the attachment pipeline was run against ClamAV (`clamav/clamav:stable`,
signatures daily 28179) in the compose stack. The industry-standard harmless test file was
uploaded alongside a clean file; the web container wrote both to the shared volume and the
dispatcher container scanned them:

| File | Verdict | Stored file | Downloadable | Audit / notification |
|------|---------|-------------|--------------|----------------------|
| clean text | `clean` | kept | yes | - |
| test virus | `infected` (`Eicar-Test-Signature`) | deleted | no (410) | written / sent |

The unit tests use a socket-level fake of clamd; this run is the evidence that the real
protocol matches it.

## Known residual risk

1. **No third-party penetration test.** The most significant gap. An internal review shares
   the blind spots of the people who wrote the code.
2. **Two-step verification is opt-in unless a workspace requires it.** TOTP and WebAuthn
   passkeys are implemented (see `docs/IDENTITY.md`). Platform staff are forced to enrol
   when `STAFF_REQUIRE_MFA=1` (production default); workspace policy can require MFA for
   members. Residual risk is operator misconfiguration turning staff enforcement off.
3. **No formal threat model document.** The review was structured but not STRIDE-style.
4. **Malware scanning is only as good as its configuration and signatures.** It is off
   unless `ATTACHMENT_SCAN_BACKEND=clamav` is set (the deploy check warns), it detects known
   signatures rather than novel malware, and password-protected archives cannot be
   inspected. Files stay size- and extension-limited, stored under randomised paths, and
   served with `nosniff` and a sandboxing CSP regardless.
5. **SSO and Stripe are verified against fakes, not the real services.** The fakes sign real
   tokens and real webhook signatures, but provider-specific behaviour (claim shapes, API
   versions) is unproven until run against a real tenant and a Stripe test account.
6. **Single-region, single-tenant-schema.** Isolation is enforced in application code and
   tested; optional Postgres RLS (`ENABLE_POSTGRES_RLS` / `manage.py enable_rls`) adds
   defence in depth but is not a multi-region or separate-database tenancy model.

## Recommended next steps

**Parked** (do not start — see `docs/PARKED.md`): third-party penetration test; real Stripe
test-mode checklist; SAML; SCIM; contractual SLA; third-party status-page vendor. Public
in-app `/status` is shipped; contractual uptime commitments stay parked. Residual risks
above still apply; parking is a sequencing decision, not a claim that the risk is gone.

Active hardening that does not depend on those:

1. ~~Require two-step verification for platform staff~~ — done (`STAFF_REQUIRE_MFA`,
   `identity.middleware.StaffMfaMiddleware`, `ensure_staff_mfa`).
2. ~~WebAuthn / passkeys~~ — done (registration + MFA step-up; see `docs/IDENTITY.md`).
3. ~~PostgreSQL row-level security~~ — optional via `ENABLE_POSTGRES_RLS` +
   `manage.py enable_rls` (on by default in Docker; see `docs/RLS.md`).
4. ~~Local IdP practice~~ — Keycloak profile in Compose (`docs/LOCAL_STACK.md`); not
   commercial IdP sign-off.