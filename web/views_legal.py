"""Terms and privacy pages.

Kept as content in the codebase rather than a CMS: the audit framework requires that terms
and a privacy policy are *referenced where accounts are created*, and a link to a page that
does not exist is worse than no link at all.

The text below describes what this software actually does. It is a factual description of
data handling, not a substitute for legal review before commercial use.
"""

from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_GET

LAST_UPDATED = "24 July 2026"

TERMS_BODY = """
<h2>1. What this service is</h2>
<p>
  Task Manager is a multi-tenant task management application. You create a workspace, invite
  colleagues, and record work in it. You keep ownership of everything you put in.
</p>

<h2>2. Your account</h2>
<p>
  You are responsible for keeping your credentials secure and for activity under your
  account. Use a unique password. Sign-in attempts are rate limited and repeated failures
  temporarily lock the account; this protects you and is not something you can opt out of.
</p>

<h2>3. Your content</h2>
<p>
  You retain all rights to the content you create. We store and process it solely to provide
  the service. We do not read it, mine it, sell it, use it for advertising, or send it to any
  model provider.
</p>

<h2>4. Acceptable use</h2>
<p>You may not use the service to:</p>
<ul>
  <li>store or distribute unlawful content;</li>
  <li>attempt to access another workspace's data;</li>
  <li>probe, scan or disrupt the service or its infrastructure;</li>
  <li>use webhooks or the API to attack a third party;</li>
  <li>circumvent plan limits or rate limits.</li>
</ul>

<h2>5. API and webhooks</h2>
<p>
  API keys act on behalf of your workspace and carry a role ceiling set at creation. Treat a
  key as a credential: anyone holding it can act with its permissions. Revoke it immediately
  if it may have leaked. Webhook endpoints you configure are your systems; deliveries are
  signed so you can verify they came from us.
</p>

<h2>6. Availability</h2>
<p>
  The service is provided on a best-effort basis. Internal reliability objectives are
  published in the project documentation, but <strong>no contractual uptime commitment is
  offered</strong>. Maintenance may cause brief interruptions.
</p>

<h2>7. Deleting your data</h2>
<p>
  You can export or delete your data at any time without contacting support. Deleting a
  workspace hides it immediately and permanently erases it after 30 days. Deleting your
  account removes it immediately; content you authored in shared workspaces is anonymised
  rather than destroyed, so your colleagues do not lose their own work.
</p>

<h2>8. Termination</h2>
<p>
  You may stop using the service at any time. We may suspend an account that materially
  breaches section 4, with notice where it is safe to give it.
</p>

<h2>9. Changes</h2>
<p>
  Material changes to these terms will be announced before they take effect. Continuing to
  use the service after that constitutes acceptance.
</p>

<h2>10. Liability</h2>
<p>
  The service is provided "as is". To the extent permitted by law, we are not liable for
  indirect or consequential loss. Nothing here limits liability that cannot lawfully be
  limited.
</p>
"""

PRIVACY_BODY = """
<h2>1. What we collect</h2>
<table class="data-table">
  <thead><tr><th scope="col">Data</th><th scope="col">Why</th></tr></thead>
  <tbody>
    <tr><td>Username, email, password hash</td><td>To create and secure your account</td></tr>
    <tr><td>Workspace and membership records</td><td>To decide what you are allowed to see</td></tr>
    <tr><td>Tasks, comments, attachments</td><td>Because that is the product</td></tr>
    <tr><td>Activity and audit records</td><td>So you can see who changed what, and for security</td></tr>
    <tr><td>IP address and user agent on security events</td><td>To detect and investigate abuse</td></tr>
    <tr><td>Request logs with a correlation id</td><td>To diagnose failures you report</td></tr>
  </tbody>
</table>

<h2>2. What we do not do</h2>
<ul>
  <li>We do not sell or rent personal data.</li>
  <li>We do not use your content for advertising or profiling.</li>
  <li>We do not send your content to any AI or model provider.</li>
  <li>We run no third-party analytics or tracking scripts. The site's Content-Security-Policy
      forbids loading third-party scripts at all.</li>
</ul>

<h2>3. How your password is stored</h2>
<p>
  Hashed with Argon2 (or PBKDF2 where Argon2 is unavailable) and a per-user salt. We cannot
  read it, recover it, or tell you what it is. API keys, invitation tokens and share links
  are likewise stored only as one-way digests.
</p>

<h2>4. How long we keep things</h2>
<ul>
  <li><strong>Live workspace data</strong> — while the workspace exists.</li>
  <li><strong>Deleted workspaces</strong> — 30 days, then permanently erased.</li>
  <li><strong>Deleted accounts</strong> — immediately; authored content is anonymised.</li>
  <li><strong>Audit records</strong> — 2 years. These survive workspace deletion in detached
      form, because a record of who deleted what is the only evidence available afterwards.</li>
  <li><strong>Operational logs</strong> — 30 days.</li>
</ul>

<h2>5. Who else processes it</h2>
<p>
  A hosting provider, a database, and a cache. Optionally an email provider and an error
  tracking service if the operator configures them. The full list, including what each one
  sees, is maintained in the project's <code>docs/SUBPROCESSORS.md</code>.
</p>

<h2>6. Where it lives</h2>
<p>
  In a single region chosen at deployment. There is no per-customer data residency control.
  If you have a residency requirement, this deployment cannot meet it.
</p>

<h2>7. Your rights</h2>
<p>All of these are self-service — none requires a support request:</p>
<ul>
  <li><strong>Access and portability</strong> — download everything we hold about you from
      your account page, or export a whole workspace as CSV or JSON.</li>
  <li><strong>Erasure</strong> — delete your account, or delete a workspace you own.</li>
  <li><strong>Rectification</strong> — edit any field directly in the product.</li>
  <li><strong>Restriction</strong> — leave a workspace at any time.</li>
</ul>

<h2>8. Security</h2>
<p>
  TLS in transit, encryption at rest, tenant isolation enforced in code and verified by an
  automated test suite, an append-only audit log, rate limiting and account lockout, and a
  strict Content-Security-Policy. Full detail is in the project's security review, which also
  states plainly what has <em>not</em> been done — notably that no third-party penetration
  test has been performed.
</p>

<h2>9. Breach notification</h2>
<p>
  If a breach affects your data we will notify affected workspace owners without undue delay,
  describing what happened, what data was involved, and what we did about it.
</p>

<h2>10. Contact</h2>
<p>
  Privacy questions and rights requests go to the operator of this deployment.
</p>
"""


@require_GET
def terms(request):
    return render(
        request,
        "web/legal.html",
        {"title": "Terms of Service", "body": TERMS_BODY, "updated": LAST_UPDATED},
    )


@require_GET
def privacy(request):
    return render(
        request,
        "web/legal.html",
        {"title": "Privacy Policy", "body": PRIVACY_BODY, "updated": LAST_UPDATED},
    )
