from __future__ import annotations

import json
from dataclasses import dataclass
import uuid

import stripe

from app.config import get_settings


@dataclass
class CheckoutSessionResult:
    url: str
    session_id: str | None = None
    simulated: bool = False


def create_checkout_session(*, plan_code: str, unit_amount_cents: int, customer_email: str, metadata: dict) -> CheckoutSessionResult:
    settings = get_settings()
    if not settings.stripe_secret_key:
        # Sviluppo senza chiavi: restituisco URL simulato per non bloccare i test UI.
        fake_url = f"https://checkout.stripe.local/simulated?plan={plan_code}&email={customer_email}"
        return CheckoutSessionResult(url=fake_url, session_id=f"cs_sim_{plan_code.lower()}_{uuid.uuid4().hex[:8]}", simulated=True)

    stripe.api_key = settings.stripe_secret_key
    session = stripe.checkout.Session.create(
        mode="subscription",
        success_url=settings.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=settings.stripe_cancel_url,
        customer_email=customer_email,
        line_items=[{
            "price_data": {
                "currency": settings.default_currency.lower(),
                "product_data": {"name": f"SoftiBridge {plan_code}"},
                "recurring": {"interval": "month"},
                "unit_amount": unit_amount_cents,
            },
            "quantity": 1,
        }],
        metadata=metadata,
    )
    return CheckoutSessionResult(url=session.url, session_id=session.id, simulated=False)


def create_billing_portal_session(*, stripe_customer_id: str) -> CheckoutSessionResult:
    settings = get_settings()
    if not settings.stripe_secret_key:
        return CheckoutSessionResult(
            url=f"https://billing.stripe.local/simulated?customer={stripe_customer_id}",
            simulated=True,
        )
    stripe.api_key = settings.stripe_secret_key
    sess = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=settings.stripe_billing_portal_return_url or settings.stripe_success_url,
    )
    return CheckoutSessionResult(url=sess.url, simulated=False)


def create_invoice_payment_session(
    *,
    invoice_number: str,
    customer_email: str,
    amount_cents: int,
    currency: str,
    description: str,
    metadata: dict,
) -> CheckoutSessionResult:
    settings = get_settings()
    if not settings.stripe_secret_key:
        fake_url = f"https://checkout.stripe.local/simulated/invoice?invoice={invoice_number}&email={customer_email}"
        return CheckoutSessionResult(url=fake_url, session_id=f"cs_sim_inv_{uuid.uuid4().hex[:8]}", simulated=True)

    stripe.api_key = settings.stripe_secret_key
    sess = stripe.checkout.Session.create(
        mode="payment",
        success_url=settings.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=settings.stripe_cancel_url,
        customer_email=customer_email,
        line_items=[{
            "price_data": {
                "currency": (currency or settings.default_currency).lower(),
                "product_data": {"name": description},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        metadata=metadata,
    )
    return CheckoutSessionResult(url=sess.url, session_id=sess.id, simulated=False)


def construct_webhook_event(payload: bytes, sig_header: str | None):
    settings = get_settings()
    is_dev = (settings.app_env or "").lower() in {"development", "dev", "local"}
    if not settings.stripe_secret_key:
        if not is_dev:
            # C3: fail-closed in produzione. Senza key non possiamo verificare la firma.
            raise ValueError("STRIPE_SECRET_KEY missing in non-dev env: webhook closed")
        data = json.loads(payload.decode("utf-8"))
        return data
    stripe.api_key = settings.stripe_secret_key
    if not settings.stripe_webhook_secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET missing")
    if not sig_header:
        raise ValueError("Missing Stripe signature header")
    return stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
