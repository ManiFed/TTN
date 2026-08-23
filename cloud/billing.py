"""Stripe: turning money into credits, exactly once.

Two things here are load-bearing and easy to get wrong.

Signature verification: the webhook endpoint is a public URL that grants
credit. Without verifying Stripe's signature, anyone who finds it can mint
themselves an unlimited balance, so an unverifiable payload is rejected before
it is parsed rather than after.

Idempotency: Stripe delivers each event at least once and frequently more than
once -- retries, replays, and manual re-sends all happen. Every grant is keyed
on the Stripe event id, so a repeated delivery is a no-op rather than free
money.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from . import credits

logger = logging.getLogger("cloud.billing")

#: What a member can buy. Deliberately small: this is a charity, the amounts
#: are pocket change, and a big minimum would read as a subscription.
PACKS = {
    "small":  {"dollars": 5,  "label": "$5 of telescope credit"},
    "medium": {"dollars": 15, "label": "$15 of telescope credit"},
    "large":  {"dollars": 40, "label": "$40 of telescope credit"},
}


def _key(config: dict, name: str) -> str:
    return (str((config.get("stripe") or {}).get(name) or "")
            or os.environ.get(f"STRIPE_{name.upper()}", ""))


def configured(config: dict) -> bool:
    return bool(_key(config, "secret_key"))


def checkout_url(user_id: str, email: str, pack: str, config: dict,
                 success_url: str, cancel_url: str) -> str:
    """A Stripe Checkout link for one credit pack."""
    if pack not in PACKS:
        raise ValueError(f"Unknown pack '{pack}'.")
    secret = _key(config, "secret_key")
    if not secret:
        raise RuntimeError("Top-ups are not configured on this server.")

    import stripe
    stripe.api_key = secret
    chosen = PACKS[pack]
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email or None,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": chosen["dollars"] * 100,
                "product_data": {"name": chosen["label"]},
            },
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        # Carried back on the webhook: the payment has to find its way to the
        # right ledger, and the email on the card is not a reliable key.
        metadata={"user_id": user_id, "pack": pack},
    )
    return str(session.url)


def apply_webhook(payload: bytes, signature: str, config: dict) -> dict:
    """Verify a Stripe event and credit the member. Safe to call repeatedly."""
    secret = _key(config, "webhook_secret")
    if not secret:
        raise RuntimeError("Stripe webhooks are not configured.")

    import stripe
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as exc:                      # signature or parse failure
        # Deliberately terse: an attacker probing this endpoint learns nothing
        # about why their forgery was rejected.
        logger.warning("Rejected a Stripe webhook: %s", type(exc).__name__)
        raise PermissionError("Invalid webhook signature.")

    if event["type"] != "checkout.session.completed":
        return {"ignored": event["type"]}

    session = event["data"]["object"]
    user_id = str((session.get("metadata") or {}).get("user_id") or "")
    if not user_id:
        logger.error("Stripe session %s carried no user_id", session.get("id"))
        return {"ignored": "no user_id"}

    paid_cents = int(session.get("amount_total") or 0)
    result = credits.grant_purchase(
        user_id,
        paid_cents * 10_000,                       # cents -> micro-dollars
        reference=str(event["id"]),
        detail={"pack": (session.get("metadata") or {}).get("pack"),
                "session": session.get("id"), "cents": paid_cents},
    )
    if result is None:
        return {"duplicate": event["id"]}
    logger.info("Credited %s with %d cents (event %s)",
                user_id, paid_cents, event["id"])
    return {"credited": credits.dollars(paid_cents * 10_000), "balance": result}
