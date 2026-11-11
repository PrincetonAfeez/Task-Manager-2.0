# Parked work

Deliberately **not** on the active backlog. Do not start these until the owner unparks them.

Shipped since this list was written (do not re-open as active work): webhook REST API,
S3/MinIO media config, staff MFA enforcement + enrol helper, weighted queue fairness,
optional Postgres RLS (Docker default on), GitHub Actions CI, WebAuthn passkeys, public
in-app `/status`, local Keycloak + ClamAV Compose profiles.

| Item | Why parked | Unpark when |
|------|------------|-------------|
| **Third-party penetration test** | Cost and scheduling; internal review + adversarial tests stand in for now | A customer or sales motion requires an external letter |
| **Real Stripe account / test-mode checklist** | No live processor credentials yet; fake-API suite covers the code paths | Ready to take (or simulate) a real payment in Stripe test mode — see `docs/BILLING.md` |
| **SAML** | OIDC covers current SSO needs | An enterprise prospect requires SAML |
| **SCIM** | Deprovisioning is manual (suspend/remove member) | An IdP-driven lifecycle is a hard requirement |
| **Contractual SLA** | Product offers best-effort availability and internal SLOs only | Legal/commercial commits to uptime |
| **Third-party status-page vendor** | Public `/status` aggregates health probes in-app; no Statuspage.io (etc.) integration | External status product is a sales requirement |

Related accepted limitations remain in the README. Residual risk text in
`docs/SECURITY_REVIEW.md` still names pen test and live Stripe/SSO validation — that is
risk disclosure, not an active to-do.
