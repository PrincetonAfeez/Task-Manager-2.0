# Identity: two-step verification and single sign-on

How sign-in works beyond a password, what an administrator configures, and what each control
does and does not protect against.

---

## Two-step verification (TOTP)

Any user can turn it on at **Account security** (`/account/security/`).

| Property | Implementation |
|----------|----------------|
| Algorithm | RFC 6238 TOTP (SHA-1, 6 digits, 30 s), checked against the RFC's test vectors |
| Clock drift | One step either side (±30 s) |
| Replay | A code is accepted once: the last accepted time step is stored and enforced with a conditional update, so two concurrent replays cannot both win |
| Seed storage | Encrypted (Fernet / AES-128-CBC + HMAC), never hashed and never logged |
| Recovery | Ten single-use codes, 80 bits each, stored as keyed HMAC-SHA256 |
| Guessing | Five wrong codes lock verification for 15 minutes (`MFA_MAX_FAILURES`, `MFA_LOCKOUT_SECONDS`), on top of the per-address login throttle |
| Enrolment QR | Rendered server-side and served as a same-origin, `no-store` image that exists only while setup is pending. No third-party QR service sees the seed |

**Sign-in.** A correct password does not create a session for a user with two-step
verification on. The half-finished login is parked in the session for five minutes while
`request.user` stays anonymous; only a valid code completes it. The Django admin's own login
form is routed through the same flow, so it is not a way around.

**Turning it off or replacing recovery codes** requires the password (when the account has
one) and a current code.

**Workspace policy.** An owner or admin can require two-step verification for everyone in a
workspace at **Security & SSO** (`/settings/security/`). It is enforced on every workspace
request, not just at sign-in, so it applies to sessions that were already open. A member
without it is sent to set it up; a member with it whose session predates the policy is asked
for a code. Session-authenticated API calls are held to the same rule. API keys are exempt:
they are workspace credentials with their own role, revocation and audit trail.

### Operator notes

- Set `FIELD_ENCRYPTION_KEYS` in production (`python manage.py generate_field_key`). Without
  it the encryption key is derived from `DJANGO_SECRET_KEY`, and rotating that key would make
  every enrolled authenticator unreadable. `check --deploy` warns (`identity.W001`).
- Rotation: put the new key **first** in the comma-separated list and keep the old one after
  it. Every key decrypts; only the first encrypts.
- **Staff MFA.** With `STAFF_REQUIRE_MFA=1` (the production default), every `is_staff` /
  `is_superuser` account must enrol and pass TOTP before using the admin or the product UI.
  The middleware exempts the MFA enrol/verify routes so staff can satisfy the requirement.
- A user who has lost both their authenticator and their recovery codes is recovered by an
  operator deleting their device in the admin (`Identity → Totp devices`), after verifying
  who they are out of band. The admin never displays a seed.

### Passkeys (WebAuthn)

Users can register platform or security-key passkeys at **Account security**
(`/account/security/`). A passkey alone satisfies `user_has_mfa()` the same way a confirmed
TOTP device does; TOTP and passkeys can coexist.

| Property | Implementation |
|----------|----------------|
| Library | [`webauthn`](https://pypi.org/project/webauthn/) (registration and authentication ceremonies) |
| RP ID / origin | Parsed from `SITE_URL` (hostname and full origin) |
| Credential storage | `WebAuthnCredential` — binary `credential_id` (unique), `public_key`, monotonic `sign_count`, optional `transports` JSON |
| Sign-in | JSON endpoints under `/accounts/mfa/webauthn/`; successful verification sets `SESSION_VERIFIED_KEY` like TOTP |
| Registration | Logged-in JSON endpoints under `/account/security/webauthn/` |

Challenges live in the session only for the duration of the ceremony. Cross-account use of a
credential id is rejected before cryptographic verification.

**Manual check.** Set `SITE_URL` to the origin you open in the browser (e.g.
`http://127.0.0.1:8000`). Register a passkey on **Account security**, sign out, sign in with
password, and choose **Use a passkey** on the MFA page.

---

## Single sign-on (OpenID Connect)

Available on the **Business** plan. Configured per workspace at `/settings/security/`.
Tested against an in-process provider that signs real tokens; **not yet exercised against a
commercial identity provider** (Okta, Entra ID, Google Workspace) — do that with your own
tenant before relying on it.

### Setting up

1. In your identity provider, register an OIDC web application with the redirect URI shown
   on the settings page (`<SITE_URL>/sso/callback/`), authorization-code flow.
2. Enter the issuer URL, client ID, client secret and the email domains allowed to sign in.
3. Enable it. Saving validates the issuer by fetching its discovery document, so a typo is
   caught before anyone depends on it.
4. Share the workspace sign-in link (`<SITE_URL>/sso/<workspace-slug>/`), or have people use
   **Sign in with SSO** on the login page and enter their work email.

### What is verified on every sign-in

| Check | Why |
|-------|-----|
| ID token signature against the provider's JWKS, asymmetric algorithms only | `none` and HMAC tokens are refused, including the classic algorithm-confusion forgery |
| `iss`, `aud`, `exp`, `iat` | The token is from this provider, for this application, and current |
| `nonce` | The token was issued for *this* login attempt |
| `state`, bound to the browser session, single use, 10-minute lifetime | The callback cannot be forged, replayed, or injected into another browser |
| PKCE (`S256`) | An intercepted authorization code is useless without the verifier |
| `email_verified` (unless explicitly trusted) and the domain allow-list | The provider is vouching for an address it actually controls |
| Provider URLs are HTTPS, never redirect, and pass the outbound SSRF rules | A provider configuration cannot be used to reach internal addresses |

### Accounts

- A person is linked to the provider's stable subject (`sub`), not their email, so a
  reassigned address does not inherit the previous holder's account.
- First sign-in creates the account and membership (role configurable; never Owner) when
  **Create accounts on first sign-in** is on, subject to the plan's member limit.
- An existing account is linked only if it **already belongs to the workspace**. An account
  that exists elsewhere is refused: otherwise a workspace's provider could assert someone
  else's address and take over an account that has access to other workspaces.
- Suspended members and deactivated accounts are refused.

### Requiring SSO

With **Require single sign-on for members** on, a member's password session cannot open the
workspace (web or session API) and is sent to the provider. Owners keep password access so a
misconfigured provider cannot lock the workspace out of its own settings. A session that
arrived through the workspace's provider also satisfies that workspace's two-step
requirement — the provider is responsible for the second factor. Authenticating to one
workspace's provider does not unlock another workspace.

### Not implemented

- **SAML** and **SCIM** — parked; see `docs/PARKED.md`. Deprovisioning is by suspending or
  removing the member; a user removed at the provider simply cannot sign in again, but their
  membership row remains until an admin removes it.
- Single logout. Signing out here does not sign the user out of the provider.
- Group-to-role mapping. Everyone provisioned through SSO gets the one configured role.

---

## Audit trail

Every outcome is written to the audit log: `auth.login_succeeded` (with the second factor
used), `auth.password_accepted`, `auth.mfa_failed`, `auth.mfa_locked`, `auth.mfa_enabled`,
`auth.mfa_disabled`, `auth.mfa_recovery_codes_regenerated`, `auth.sso_login`,
`auth.sso_failed`, `org.mfa_requirement_changed`, `org.sso_configured`. Secrets, seeds and
codes are never included.
