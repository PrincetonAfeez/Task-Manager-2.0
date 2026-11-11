# Billing and payments

Plans and quotas are enforced by the product itself (`billing/`). Taking money is delegated
to Stripe. This document covers how the two are connected and how to switch it on.

> **Status.** The integration is implemented and covered by 67 tests against a fake of
> Stripe's API (real form encoding, real signature scheme, every state transition). It has
> **not been run against Stripe**, because that needs an account's test keys, which must be
> supplied by the account owner. Do the checklist at the end in Stripe test mode before
> taking a real payment.

---

## What touches what

| Concern | Where it lives |
|---------|----------------|
| Card entry, 3-D Secure, receipts, invoices, tax, dunning emails | Stripe-hosted Checkout and Customer Portal |
| Customer id (`cus_…`) and subscription id (`sub_…`) | `billing.Subscription` |
| Card numbers, CVC, bank details | **Never reach this application** |
| Which plan a workspace is entitled to | `billing.Subscription.plan`, updated from Stripe by webhook |

Because no card data is entered into, transmitted by, or stored in this application, the
PCI DSS scope is the smallest possible one (SAQ A). Confirm that with whoever owns compliance.

## Flow

1. A billing manager clicks **Subscribe** → the server creates a Checkout Session and
   redirects to Stripe. Nothing is granted yet.
2. The customer pays on Stripe.
3. Stripe calls `POST /billing/webhooks/stripe/`. The request is authenticated by its
   signature, the subscription is **re-read from Stripe's API**, and the local plan is set
   from that.
4. Later changes — plan switch, card update, cancellation, failed payment — happen in the
   Customer Portal and arrive the same way.

Design points:

- **Events are a trigger, not a source of truth.** Stripe does not guarantee delivery order.
  Every handler fetches current state from the API, so a late "updated" arriving after
  "deleted" cannot resurrect a cancelled plan.
- **Each event is applied once.** `BillingEvent` is unique on the event id and is written in
  the same transaction as its effect. A crash mid-handler means Stripe's retry redoes the
  work; a duplicate delivery is acknowledged and skipped.
- **A failed payment does not cut access.** The subscription becomes `past_due` and stays
  entitled; Stripe's retry schedule decides when it finally cancels. On cancellation the
  workspace drops to the free plan — data is kept, but creating beyond the free limits is
  refused.
- **An unmapped price never grants anything.** If a subscription's price is not in
  `STRIPE_PRICE_IDS` the plan is left as it is and an error is logged.
- **A paying workspace cannot bypass the processor.** Plan changes for it go to the portal,
  so what is charged and what is granted cannot drift apart.
- **Deleting a workspace stops its billing.** The subscription is cancelled at Stripe before
  the workspace is marked deleted (including when an account deletion takes its solely
  owned workspaces with it). If Stripe cannot be reached the deletion is refused and can be
  retried, rather than leaving a customer paying for something they can no longer open.

## Configuration

| Variable | Purpose |
|----------|---------|
| `STRIPE_SECRET_KEY` | API key. Empty disables payments entirely |
| `STRIPE_WEBHOOK_SECRET` | Signing secret of the webhook endpoint. Required whenever the key is set; the app refuses to start without it |
| `STRIPE_PRICE_IDS` | `team=price_…,business=price_…` — which Stripe price sells which plan |
| `STRIPE_ALLOW_TEST_KEY` | Set to `1` to allow an `sk_test_…` key with `DJANGO_ENV=production` (staging). Otherwise that combination refuses to start |
| `BILLING_ALLOW_UNPAID_PLAN_CHANGES` | With no processor configured, may a workspace move itself to a paid plan for free? Defaults to **off in production**, on in development |

Webhook endpoint to register in Stripe: `https://<your-host>/billing/webhooks/stripe/`,
subscribed to:

`checkout.session.completed`, `customer.subscription.created`,
`customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`,
`invoice.payment_failed`.

Enable the Customer Portal in Stripe and allow plan switching between the prices above if
customers should be able to change plan themselves.

## First-run checklist (Stripe test mode)

**Parked.** Do not run this against a real Stripe account until it is unparked in
`docs/PARKED.md`. The fake-API suite in `tests/test_stripe.py` is the current verification.

Nobody has done these yet. Each one maps to a path that is only simulated in the tests.

1. Create the two recurring prices; put their ids in `STRIPE_PRICE_IDS`.
2. Register the webhook endpoint (or run `stripe listen --forward-to …`) and set
   `STRIPE_WEBHOOK_SECRET`.
3. Subscribe a workspace with test card `4242 4242 4242 4242`. Expect: redirect to Checkout,
   then within seconds the billing page shows the paid plan and a renewal date, and the audit
   log has `billing.subscription_synced`.
4. Open the portal, switch plan. Expect the plan to follow.
5. Cancel in the portal. Expect "Cancels on …" and, at period end (or with immediate
   cancellation), a return to Free.
6. Use card `4000 0000 0000 0341` (attaches, then fails on charge) to exercise `past_due`.
7. Resend an event from the Stripe dashboard. Expect `"outcome": "duplicate"`.
8. Delete a subscribed workspace. Expect the subscription to show as cancelled in Stripe.
9. Confirm the Stripe API version on the account returns `current_period_end` either on the
   subscription or on its first item — both shapes are handled; anything else is not.

## Not implemented

- Per-seat or metered pricing (quantity is always 1).
- Proration previews, coupons and tax configuration — these are Stripe settings, not code.
- In-app invoices; customers get them from the portal.
- Notifying the workspace owner in-app when a payment fails. Stripe's own emails cover it
  when enabled; the billing page shows a warning.
