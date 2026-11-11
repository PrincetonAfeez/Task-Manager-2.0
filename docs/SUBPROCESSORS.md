# Sub-processors

Third parties that may process customer data on our behalf. Maintained because a customer
cannot assess their own risk without knowing who else is in the chain.

Last reviewed: 2026-07-24.

---

## Required

| Sub-processor | Purpose | Data processed | Region |
|---------------|---------|----------------|--------|
| Cloud hosting provider | Compute, storage, networking | All customer data at rest and in transit | Deployment region |
| PostgreSQL (managed or self-hosted) | Primary datastore | All customer data | Deployment region |
| Redis (managed or self-hosted) | Cache, rate limiting, Celery broker | Session identifiers, throttle counters, job references. **No task content.** | Deployment region |

## Optional - only if configured

| Sub-processor | Purpose | Data processed | Enabled by |
|---------------|---------|----------------|------------|
| SMTP provider | Transactional email | Recipient address, subject, body (may contain task titles) | `EMAIL_HOST` |
| Sentry | Error tracking | Stack traces, request metadata, correlation ids. `send_default_pii=False`. | `SENTRY_DSN` |
| Stripe | Payments for paid plans | Workspace name, the purchasing user's email, plan purchased. Card details are entered on Stripe's pages and never reach this application. | `STRIPE_SECRET_KEY` |
| The customer's own identity provider | Single sign-on | Nothing is sent beyond the sign-in request; it *supplies* a user's subject id, email and name. Chosen and operated by the customer, per workspace. | Workspace SSO settings |

If neither is configured, email falls back to console output and errors go only to the
application log. The product is fully functional with zero optional sub-processors.

## Explicitly not used

- **No analytics or tracking scripts.** The Content-Security-Policy is `script-src 'self'`;
  a third-party tag could not load even if one were added.
- **No CDN for application assets.** WhiteNoise serves them from the application itself.
- **No LLM or AI provider.** No customer data is sent to any model API.
- **No advertising or marketing platform.**
- **No third-party malware-scanning service.** Attachment scanning uses a ClamAV daemon
  run inside the deployment; uploaded files are not sent to anyone.

## Outbound connections the customer controls

Webhook endpoints are configured by the customer and are **their** processors, not ours.
Deliveries are HMAC-signed, and destinations are validated against private, loopback,
link-local and reserved address ranges before every attempt.

## Change process

Adding a sub-processor requires: an entry in this table, a note in the release notes, and
30 days' notice to customers before it begins processing their data.