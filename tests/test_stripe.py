"""Stripe integration, exercised against a fake of Stripe's REST API.

Nothing here talks to Stripe. What *is* real: the form encoding, the webhook signature
scheme (HMAC-SHA256 over ``"{t}.{body}"``), idempotency, and every state transition.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from billing import stripe_gateway as gateway
from billing.models import BillingEvent, Plan, Subscription
from billing.services import ensure_default_plans, get_subscription
from kernel.models import AuditLog

pytestmark = pytest.mark.django_db

WEBHOOK_SECRET = "whsec_test_secret"
PRICES = {"team": "price_team", "business": "price_business"}


@pytest.fixture
def stripe_on(settings):
    ensure_default_plans()
    settings.STRIPE_SECRET_KEY = "sk_test_not_a_real_key"
    settings.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.STRIPE_PRICE_IDS = dict(PRICES)
    return settings


class FakeStripe:
    """Records calls and serves subscriptions from an in-memory table."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.subscriptions: dict[str, dict] = {}
        self.fail = False

    def __call__(self, method, path, data=None, *, idempotency_key=None, missing_ok=False):
        self.calls.append((method, path, data, idempotency_key))
        if self.fail:
            raise gateway.BillingError("The payment provider could not be reached. Try again shortly.")
        if path == "/v1/customers":
            return {"id": "cus_123"}
        if path == "/v1/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.stripe.test/c/pay/cs_1"}
        if path == "/v1/billing_portal/sessions":
            return {"id": "bps_1", "url": "https://billing.stripe.test/p/session/bps_1"}
        if path.startswith("/v1/subscriptions/") and method == "DELETE":
            remote = self.subscriptions.get(path.rsplit("/", 1)[-1])
            return {**remote, "status": "canceled"} if remote else None
        if path.startswith("/v1/subscriptions/"):
            return self.subscriptions[path.rsplit("/", 1)[-1]]
        raise AssertionError(f"unexpected Stripe call {method} {path}")

    def paths(self):
        return [call[1] for call in self.calls]


@pytest.fixture
def stripe(monkeypatch, stripe_on):
    fake = FakeStripe()
    monkeypatch.setattr(gateway, "_request", fake)
    return fake


def remote_subscription(organization, *, sub_id="sub_1", status="active", price="price_team", customer="cus_123", **extra):
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": False,
        "current_period_end": int(time.time()) + 30 * 86400,
        "metadata": {"organization_id": str(organization.pk)},
        "items": {"data": [{"price": {"id": price}}]},
        **extra,
    }


def sign(body: bytes, *, secret=WEBHOOK_SECRET, timestamp=None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def deliver(client, event: dict, **sign_kwargs):
    body = json.dumps(event).encode()
    return client.post(
        "/billing/webhooks/stripe/",
        data=body,
        content_type="application/json",
        headers={"Stripe-Signature": sign(body, **sign_kwargs)},
    )


def event(kind, obj, event_id="evt_1"):
    return {"id": event_id, "type": kind, "data": {"object": obj}}


class TestConfiguration:
    def test_off_without_a_key(self, settings):
        settings.STRIPE_SECRET_KEY = ""
        assert not gateway.is_configured()

    def test_off_without_prices(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_PRICE_IDS = {}
        assert not gateway.is_configured()

    def test_price_lookup(self, stripe_on):
        assert gateway.plan_for_price("price_business").code == "business"
        assert gateway.plan_for_price("price_unknown") is None

    def test_form_encoding_matches_stripes_bracket_syntax(self):
        encoded = gateway._flatten(
            {"mode": "subscription", "line_items": [{"price": "p", "quantity": 1}],
             "subscription_data": {"metadata": {"organization_id": 7}}, "skip": None, "flag": True}
        )
        assert encoded == [
            ("mode", "subscription"),
            ("line_items[0][price]", "p"),
            ("line_items[0][quantity]", "1"),
            ("subscription_data[metadata][organization_id]", "7"),
            ("flag", "true"),
        ]

    def test_requests_carry_the_key_and_idempotency_header(self, stripe_on, monkeypatch):
        seen = {}

        class Response:
            status_code = 200
            headers: dict = {}

            def json(self):
                return {"id": "cus_1"}

        def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            return Response()

        monkeypatch.setattr(gateway.requests, "request", fake_request)
        assert gateway._request("POST", "/v1/customers", {"name": "A"}, idempotency_key="k1") == {"id": "cus_1"}
        assert seen["url"] == "https://api.stripe.com/v1/customers"
        assert seen["headers"]["Authorization"] == "Bearer sk_test_not_a_real_key"
        assert seen["headers"]["Idempotency-Key"] == "k1"
        assert seen["timeout"] and seen["allow_redirects"] is False

    def test_api_errors_do_not_leak_provider_detail(self, stripe_on, monkeypatch):
        class Response:
            status_code = 402
            headers = {"Request-Id": "req_1"}

            def json(self):
                return {"error": {"message": "Your card number sk_test_not_a_real_key is wrong", "code": "card_declined"}}

        monkeypatch.setattr(gateway.requests, "request", lambda *a, **k: Response())
        with pytest.raises(gateway.BillingError) as raised:
            gateway._request("POST", "/v1/customers", {})
        assert "sk_test" not in str(raised.value) and "card" not in str(raised.value)

    def test_network_failure_becomes_a_billing_error(self, stripe_on, monkeypatch):
        def boom(*args, **kwargs):
            raise gateway.requests.ConnectionError("dns")

        monkeypatch.setattr(gateway.requests, "request", boom)
        with pytest.raises(gateway.BillingError):
            gateway._request("GET", "/v1/subscriptions/sub_1")


class TestCheckout:
    def test_paid_plan_goes_to_hosted_checkout(self, client_owner, organization, stripe):
        response = client_owner.post("/settings/billing/change/", {"plan": "team"})

        assert response.status_code == 302 and response.url == "https://checkout.stripe.test/c/pay/cs_1"
        session = next(call for call in stripe.calls if call[1] == "/v1/checkout/sessions")[2]
        assert session["mode"] == "subscription"
        assert session["customer"] == "cus_123"
        assert session["line_items"] == [{"price": "price_team", "quantity": 1}]
        assert session["client_reference_id"] == organization.pk
        assert session["subscription_data"]["metadata"] == {"organization_id": organization.pk, "plan_code": "team"}
        assert session["success_url"].endswith("/settings/billing/?checkout=success")

    def test_starting_checkout_grants_nothing(self, client_owner, organization, stripe):
        client_owner.post("/settings/billing/change/", {"plan": "business"})
        assert get_subscription(organization).plan.code == "free"
        assert AuditLog.objects.filter(action="billing.checkout_started", metadata__plan="business").exists()

    def test_customer_is_created_once_and_remembered(self, client_owner, organization, stripe):
        client_owner.post("/settings/billing/change/", {"plan": "team"})
        client_owner.post("/settings/billing/change/", {"plan": "team"})
        assert stripe.paths().count("/v1/customers") == 1
        subscription = get_subscription(organization)
        assert (subscription.processor, subscription.processor_customer_id) == ("stripe", "cus_123")

    def test_customer_creation_is_idempotent_at_stripe(self, client_owner, organization, stripe):
        client_owner.post("/settings/billing/change/", {"plan": "team"})
        customer_call = next(call for call in stripe.calls if call[1] == "/v1/customers")
        assert customer_call[3] == f"customer-org-{organization.pk}"

    def test_provider_outage_is_reported_not_raised(self, client_owner, organization, stripe):
        stripe.fail = True
        response = client_owner.post("/settings/billing/change/", {"plan": "team"}, follow=True)
        assert response.status_code == 200 and b"could not be reached" in response.content
        assert get_subscription(organization).plan.code == "free"

    def test_plan_without_a_price_cannot_be_bought(self, client_owner, organization, stripe, settings):
        settings.STRIPE_PRICE_IDS = {"team": "price_team"}
        response = client_owner.post("/settings/billing/change/", {"plan": "business"}, follow=True)
        assert b"cannot be purchased online" in response.content
        assert get_subscription(organization).plan.code == "free"

    def test_members_cannot_start_checkout(self, client_member, organization, stripe):
        assert client_member.post("/settings/billing/change/", {"plan": "team"}).status_code == 403
        assert stripe.calls == []

    def test_hidden_plans_cannot_be_selected(self, client_owner, organization, stripe):
        Plan.objects.filter(code="business").update(is_public=False)
        assert client_owner.post("/settings/billing/change/", {"plan": "business"}).status_code == 404

    def test_paying_workspace_is_sent_to_the_portal_instead(self, client_owner, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization))
        response = client_owner.post("/settings/billing/change/", {"plan": "free"})
        assert response.url == "https://billing.stripe.test/p/session/bps_1"
        assert get_subscription(organization).plan.code == "team"

    def test_portal_requires_a_billing_account(self, client_owner, organization, stripe):
        response = client_owner.post("/settings/billing/portal/", follow=True)
        assert b"no billing account" in response.content

    def test_portal_rejects_get(self, client_owner, organization, stripe):
        assert client_owner.get("/settings/billing/portal/").status_code == 405


class TestUnpaidPlanChanges:
    def test_allowed_when_no_processor_and_permitted(self, client_owner, organization, settings):
        ensure_default_plans()
        settings.BILLING_ALLOW_UNPAID_PLAN_CHANGES = True
        client_owner.post("/settings/billing/change/", {"plan": "team"})
        assert get_subscription(organization).plan.code == "team"

    def test_refused_in_a_production_configuration(self, client_owner, organization, settings):
        ensure_default_plans()
        settings.BILLING_ALLOW_UNPAID_PLAN_CHANGES = False
        response = client_owner.post("/settings/billing/change/", {"plan": "team"}, follow=True)
        assert b"cannot be purchased online yet" in response.content
        assert get_subscription(organization).plan.code == "free"

    def test_moving_to_free_is_always_allowed(self, client_owner, organization, settings):
        ensure_default_plans()
        Subscription.objects.update_or_create(
            organization=organization, defaults={"plan": Plan.objects.get(code="team")}
        )
        settings.BILLING_ALLOW_UNPAID_PLAN_CHANGES = False
        client_owner.post("/settings/billing/change/", {"plan": "free"})
        assert get_subscription(organization).plan.code == "free"


class TestWebhookAuthentication:
    def _body(self):
        return json.dumps(event("customer.subscription.updated", {"id": "sub_1"})).encode()

    def test_valid_signature_is_accepted(self, stripe_on):
        body = self._body()
        assert gateway.verify_webhook(body, sign(body))["id"] == "evt_1"

    def test_wrong_secret_is_rejected(self, stripe_on):
        body = self._body()
        with pytest.raises(gateway.SignatureError):
            gateway.verify_webhook(body, sign(body, secret="whsec_wrong"))

    def test_altered_body_is_rejected(self, stripe_on):
        body = self._body()
        with pytest.raises(gateway.SignatureError):
            gateway.verify_webhook(body.replace(b"sub_1", b"sub_2"), sign(body))

    def test_old_timestamp_is_rejected(self, stripe_on):
        body = self._body()
        with pytest.raises(gateway.SignatureError):
            gateway.verify_webhook(body, sign(body, timestamp=int(time.time()) - 3600))

    def test_timestamp_is_part_of_the_signed_material(self, stripe_on):
        body = self._body()
        old = sign(body, timestamp=int(time.time()) - 3600)
        forged = f"t={int(time.time())},{old.split(',', 1)[1]}"
        with pytest.raises(gateway.SignatureError):
            gateway.verify_webhook(body, forged)

    @pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00", "t=1"])
    def test_malformed_headers_are_rejected(self, stripe_on, header):
        with pytest.raises(gateway.SignatureError):
            gateway.verify_webhook(self._body(), header)

    def test_any_matching_v1_signature_is_enough(self, stripe_on):
        """Stripe sends two signatures while an endpoint secret is being rolled."""

        body = self._body()
        good = sign(body)
        timestamp, digest = good.split(",")
        assert gateway.verify_webhook(body, f"{timestamp},v1={'0' * 64},{digest}")

    def test_non_event_bodies_are_rejected(self, stripe_on):
        for body in (b"[]", b"{}", b"not json"):
            with pytest.raises(gateway.SignatureError):
                gateway.verify_webhook(body, sign(body))

    def test_endpoint_rejects_unsigned_requests(self, client, stripe):
        response = client.post("/billing/webhooks/stripe/", data=b"{}", content_type="application/json")
        assert response.status_code == 400 and response.content == b""

    def test_endpoint_does_not_exist_when_unconfigured(self, client, settings):
        settings.STRIPE_WEBHOOK_SECRET = ""
        assert client.post("/billing/webhooks/stripe/", data=b"{}", content_type="application/json").status_code == 404

    def test_endpoint_needs_no_csrf_token_or_session(self, organization, stripe):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        response = deliver(strict, event("customer.subscription.updated", {"id": "sub_1"}))
        assert response.status_code == 200

    def test_endpoint_rejects_get(self, client, stripe):
        assert client.get("/billing/webhooks/stripe/").status_code == 405


class TestWebhookReconciliation:
    def test_completed_checkout_grants_the_plan(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization, price="price_business")

        response = deliver(client, event("checkout.session.completed", {"mode": "subscription", "subscription": "sub_1"}))

        assert response.json() == {"received": True, "outcome": "processed"}
        subscription = get_subscription(organization)
        assert subscription.plan.code == "business" and subscription.status == Subscription.Status.ACTIVE
        assert subscription.external_reference == "sub_1" and subscription.processor_customer_id == "cus_123"
        assert subscription.current_period_end is not None
        assert AuditLog.objects.filter(action="billing.subscription_synced", metadata__to_plan="business").exists()

    def test_state_comes_from_the_api_not_the_event_body(self, client, organization, stripe):
        """An out-of-order or forged-looking payload cannot grant what Stripe does not show."""

        stripe.subscriptions["sub_1"] = remote_subscription(organization, price="price_team")
        stale = remote_subscription(organization, price="price_business")
        deliver(client, event("customer.subscription.updated", stale))
        assert get_subscription(organization).plan.code == "team"

    def test_duplicate_delivery_is_applied_once(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        first = deliver(client, event("customer.subscription.created", {"id": "sub_1"}))
        second = deliver(client, event("customer.subscription.created", {"id": "sub_1"}))

        assert first.json()["outcome"] == "processed" and second.json()["outcome"] == "duplicate"
        assert BillingEvent.objects.count() == 1
        assert stripe.paths().count("/v1/subscriptions/sub_1") == 1

    def test_failed_payment_marks_past_due_but_keeps_access(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        deliver(client, event("customer.subscription.created", {"id": "sub_1"}, "evt_a"))
        stripe.subscriptions["sub_1"] = remote_subscription(organization, status="past_due")
        deliver(client, event("invoice.payment_failed", {"subscription": "sub_1"}, "evt_b"))

        subscription = get_subscription(organization)
        assert subscription.status == Subscription.Status.PAST_DUE and subscription.is_entitled
        assert subscription.plan.code == "team"

    def test_invoice_event_in_the_newer_api_shape(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        obj = {"parent": {"subscription_details": {"subscription": "sub_1"}}}
        assert deliver(client, event("invoice.paid", obj)).json()["outcome"] == "processed"
        assert get_subscription(organization).plan.code == "team"

    def test_period_end_on_the_item_is_understood(self, organization, stripe):
        remote = remote_subscription(organization)
        end = remote.pop("current_period_end")
        remote["items"]["data"][0]["current_period_end"] = end
        subscription = gateway.apply_subscription_state(remote)
        assert int(subscription.current_period_end.timestamp()) == end

    def test_cancellation_returns_the_workspace_to_free(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization, price="price_business")
        deliver(client, event("customer.subscription.created", {"id": "sub_1"}, "evt_a"))
        stripe.subscriptions["sub_1"] = remote_subscription(organization, status="canceled")
        deliver(client, event("customer.subscription.deleted", {"id": "sub_1"}, "evt_b"))

        subscription = get_subscription(organization)
        assert subscription.plan.code == "free" and subscription.status == Subscription.Status.ACTIVE
        assert subscription.external_reference == "" and subscription.canceled_at is not None
        assert subscription.processor_customer_id == "cus_123"  # kept, so they can resubscribe

    def test_late_update_after_cancellation_does_not_resurrect_the_plan(self, client, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        deliver(client, event("customer.subscription.created", {"id": "sub_1"}, "evt_a"))
        stripe.subscriptions["sub_1"] = remote_subscription(organization, status="canceled")
        deliver(client, event("customer.subscription.deleted", {"id": "sub_1"}, "evt_b"))
        # Delivered last, though it happened first.
        deliver(client, event("customer.subscription.updated", remote_subscription(organization), "evt_c"))

        assert get_subscription(organization).plan.code == "free"

    def test_an_old_abandoned_subscription_cannot_cancel_the_live_one(self, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization, sub_id="sub_live"))
        gateway.apply_subscription_state(
            remote_subscription(organization, sub_id="sub_abandoned", status="incomplete_expired")
        )
        subscription = get_subscription(organization)
        assert subscription.plan.code == "team" and subscription.external_reference == "sub_live"

    def test_scheduled_cancellation_is_shown_not_applied(self, client_owner, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization, cancel_at_period_end=True))
        subscription = get_subscription(organization)
        assert subscription.cancel_at_period_end and subscription.plan.code == "team"
        assert b"Cancels on" in client_owner.get("/settings/billing/").content

    def test_incomplete_payment_grants_nothing(self, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization, status="incomplete"))
        subscription = get_subscription(organization)
        assert subscription.plan.code == "free" and subscription.external_reference == ""

    def test_trial_is_tracked(self, organization, stripe):
        ends = int(time.time()) + 7 * 86400
        subscription = gateway.apply_subscription_state(
            remote_subscription(organization, status="trialing", trial_end=ends)
        )
        assert subscription.status == Subscription.Status.TRIALING
        assert int(subscription.trial_ends_at.timestamp()) == ends

    def test_unmapped_price_never_guesses_an_entitlement(self, organization, stripe):
        subscription = gateway.apply_subscription_state(remote_subscription(organization, price="price_mystery"))
        assert subscription.plan.code == "free"

    def test_subscription_for_an_unknown_workspace_is_ignored(self, client, organization, stripe):
        remote = remote_subscription(organization, customer="cus_stranger")
        remote["metadata"] = {"organization_id": "999999"}
        stripe.subscriptions["sub_x"] = remote
        response = deliver(client, event("customer.subscription.updated", {"id": "sub_x"}))
        assert response.status_code == 200
        assert get_subscription(organization).plan.code == "free"

    def test_event_for_one_workspace_never_changes_another(self, client, organization, other_organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization, price="price_business")
        deliver(client, event("customer.subscription.created", {"id": "sub_1"}))
        assert get_subscription(organization).plan.code == "business"
        assert get_subscription(other_organization).plan.code == "free"

    def test_customer_id_wins_over_metadata(self, client, organization, other_organization, stripe):
        """Metadata naming another workspace must not redirect a known customer's subscription."""

        gateway.apply_subscription_state(remote_subscription(organization))
        remote = remote_subscription(organization, price="price_business")
        remote["metadata"] = {"organization_id": str(other_organization.pk)}
        gateway.apply_subscription_state(remote)

        assert get_subscription(organization).plan.code == "business"
        assert get_subscription(other_organization).plan.code == "free"

    def test_unhandled_event_types_are_acknowledged_and_ignored(self, client, organization, stripe):
        response = deliver(client, event("charge.refunded", {"id": "ch_1"}))
        assert response.json()["outcome"] == "ignored"
        assert stripe.calls == [] and not BillingEvent.objects.exists()

    def test_provider_outage_asks_stripe_to_retry(self, client, organization, stripe):
        stripe.fail = True
        response = deliver(client, event("customer.subscription.updated", {"id": "sub_1"}))
        assert response.status_code == 503
        # Not recorded, so the retry is processed rather than dismissed as a duplicate.
        assert not BillingEvent.objects.exists()

    def test_one_time_payment_checkouts_are_ignored(self, client, organization, stripe):
        response = deliver(client, event("checkout.session.completed", {"mode": "payment", "subscription": None}))
        assert response.status_code == 200 and get_subscription(organization).plan.code == "free"


class TestBillingPage:
    def test_shows_subscribe_buttons_when_payments_are_on(self, client_owner, organization, stripe):
        content = client_owner.get("/settings/billing/").content
        assert b"Subscribe to Team" in content and b"Subscribe to Business" in content

    def test_shows_portal_button_for_a_paying_workspace(self, client_owner, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization))
        content = client_owner.get("/settings/billing/").content
        assert b"Manage payment, invoices and cancellation" in content and b"Renews on" in content
        assert b"Subscribe to" not in content

    def test_past_due_warning(self, client_owner, organization, stripe):
        gateway.apply_subscription_state(remote_subscription(organization, status="past_due"))
        assert b"Your last payment failed" in client_owner.get("/settings/billing/").content

    def test_return_from_checkout_does_not_claim_the_plan_changed(self, client_owner, organization, stripe):
        content = client_owner.get("/settings/billing/?checkout=success").content
        assert b"updates as soon as it is confirmed" in content
        assert get_subscription(organization).plan.code == "free"

    def test_no_secret_material_on_the_page(self, client_owner, organization, stripe):
        content = client_owner.get("/settings/billing/").content
        assert b"sk_test" not in content and WEBHOOK_SECRET.encode() not in content


class TestWorkspaceDeletionStopsBilling:
    def _delete(self, client, organization):
        return client.post("/settings/workspace/delete/", {"confirm": organization.name}, follow=True)

    def _subscribe(self, organization, stripe):
        stripe.subscriptions["sub_1"] = remote_subscription(organization)
        gateway.apply_subscription_state(stripe.subscriptions["sub_1"])

    def test_deleting_a_paying_workspace_cancels_its_subscription(self, client_owner, organization, stripe):
        self._subscribe(organization, stripe)

        self._delete(client_owner, organization)

        organization.refresh_from_db()
        assert organization.deleted_at is not None
        assert ("DELETE", "/v1/subscriptions/sub_1") in [(call[0], call[1]) for call in stripe.calls]
        subscription = Subscription.objects.get(organization=organization)
        assert subscription.plan.code == "free" and subscription.external_reference == ""
        assert AuditLog.objects.filter(
            action="organization.deleted", metadata__subscription_cancelled=True
        ).exists()

    def test_deletion_is_refused_when_the_processor_cannot_be_told(self, client_owner, organization, stripe):
        self._subscribe(organization, stripe)
        stripe.fail = True

        response = self._delete(client_owner, organization)

        organization.refresh_from_db()
        assert organization.deleted_at is None
        assert b"could not be cancelled" in response.content
        assert Subscription.objects.get(organization=organization).external_reference == "sub_1"

    def test_subscription_already_gone_at_the_processor_does_not_block(self, client_owner, organization, stripe):
        self._subscribe(organization, stripe)
        del stripe.subscriptions["sub_1"]

        self._delete(client_owner, organization)

        organization.refresh_from_db()
        assert organization.deleted_at is not None
        assert Subscription.objects.get(organization=organization).plan.code == "free"

    def test_unpaid_workspace_deletes_without_calling_the_processor(self, client_owner, organization, stripe):
        self._delete(client_owner, organization)
        organization.refresh_from_db()
        assert organization.deleted_at is not None and stripe.calls == []

    def test_account_deletion_cancels_subscriptions_of_workspaces_it_removes(self, user, organization, stripe):
        from support.services import delete_account

        self._subscribe(organization, stripe)
        delete_account(user=user, delete_sole_owned_workspaces=True)
        assert ("DELETE", "/v1/subscriptions/sub_1") in [(call[0], call[1]) for call in stripe.calls]

    def test_purging_a_workspace_removes_identity_and_detaches_billing_events(self, user, organization, stripe):
        from django.utils import timezone

        from billing.models import BillingEvent
        from identity.models import SsoConnection, SsoIdentity
        from kernel.purge import purge_organization_data

        connection = SsoConnection.objects.create(organization=organization, issuer="https://idp.example.test", client_id="c")
        SsoIdentity.objects.create(connection=connection, user=user, subject="s")
        event_row = BillingEvent.objects.create(event_id="evt_p", event_type="invoice.paid", organization=organization)
        type(organization).objects.filter(pk=organization.pk).update(deleted_at=timezone.now())

        assert purge_organization_data(organization.pk)["purged"] is True

        assert not SsoConnection.objects.exists() and not SsoIdentity.objects.exists()
        event_row.refresh_from_db()
        assert event_row.organization is None

