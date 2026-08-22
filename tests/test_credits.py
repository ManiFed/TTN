#!/usr/bin/env python3
"""Credits: the ledger, the nightly ceiling, and paying exactly once.

This is the part where mistakes cost somebody money, so the tests are about the
ways that happens rather than the happy path: a webhook delivered twice minting
free credit, a runaway loop draining a balance overnight, rounding that
quietly favours the spender, and cache reads billed at the full input rate --
which would overcharge by roughly ten times, since the cached tool block is
most of every request.

Run with:  python3 -m pytest tests/test_credits.py
"""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from cloud import agent_chat, billing, credits


class _FakeDb:
    def __init__(self):
        self.ledger: list = []
        self.settings: dict = {}
        self._id = 0

    @staticmethod
    def loads(text, default=None):
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return default

    def execute(self, sql, params=(), returning_id=False):
        if "INSERT INTO credit_ledger" in sql:
            row = dict(zip(
                ("user_id", "kind", "amount_micros", "day", "created_at",
                 "detail_json", "reference"), params))
            if any(r.get("reference") and r["reference"] == row.get("reference")
                   for r in self.ledger):
                raise RuntimeError("duplicate key value violates unique constraint")
            self._id += 1
            row["id"] = self._id
            self.ledger.append(row)
        elif "INSERT INTO credit_settings" in sql:
            user_id, cap, _ = params
            self.settings[user_id] = cap
        return 1

    def query_one(self, sql, params=()):
        if "COALESCE(SUM(amount_micros)" in sql:
            return {"total": sum(r["amount_micros"] for r in self.ledger
                                 if r["user_id"] == params[0])}
        if "COALESCE(SUM(-amount_micros)" in sql:
            user_id, kind, day = params
            return {"total": sum(-r["amount_micros"] for r in self.ledger
                                 if r["user_id"] == user_id
                                 and r["kind"] == kind and r["day"] == day)}
        if "FROM credit_settings" in sql:
            cap = self.settings.get(params[0])
            return {"nightly_cap_micros": cap} if cap is not None else None
        # These are existence checks: the caller does `if row:`, so a match must
        # be truthy. Returning {} made every guard read as "not found", which
        # is how a double-delivered webhook would have minted free credit.
        if "kind = %s" in sql and "credit_ledger" in sql:
            user_id, kind = params
            return next(({"found": 1} for r in self.ledger
                         if r["user_id"] == user_id and r["kind"] == kind), None)
        if "WHERE reference = %s" in sql:
            return next(({"found": 1} for r in self.ledger
                         if r.get("reference") == params[0]), None)
        return None

    def query(self, sql, params=()):
        return [r for r in reversed(self.ledger) if r["user_id"] == params[0]]


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDb()
        p = patch.object(credits, "db", self.db)
        p.start()
        self.addCleanup(p.stop)


class LedgerTest(_Base):

    def test_a_new_member_starts_with_credit_not_a_paywall(self):
        self.assertTrue(credits.grant_welcome_once("u1"))
        self.assertEqual(credits.balance_micros("u1"), credits.WELCOME_MICROS)

    def test_the_welcome_grant_cannot_be_claimed_twice(self):
        """A retried signup must not mint credit."""
        credits.grant_welcome_once("u1")
        self.assertFalse(credits.grant_welcome_once("u1"))
        self.assertEqual(credits.balance_micros("u1"), credits.WELCOME_MICROS)

    def test_spending_reduces_the_balance(self):
        credits.grant("u1", 1_000_000)
        credits.record_charge("u1", 100_000, {})
        self.assertLess(credits.balance_micros("u1"), 1_000_000)

    def test_the_markup_is_applied_to_charges(self):
        credits.grant("u1", 10 * credits.MICROS)
        credits.record_charge("u1", credits.MICROS, {})
        spent = 10 * credits.MICROS - credits.balance_micros("u1")
        self.assertEqual(spent, credits.with_markup(credits.MICROS))
        self.assertGreater(spent, credits.MICROS)

    def test_rounding_never_favours_the_spender(self):
        """Fractions of a micro-dollar round up, or they accumulate as losses."""
        self.assertEqual(credits.to_micros(0.0000001), 1)
        self.assertGreaterEqual(credits.with_markup(3), 4)


class CeilingTest(_Base):

    def test_a_healthy_member_may_spend(self):
        credits.grant("u1", 10 * credits.MICROS)
        credits.check_can_spend("u1")

    def test_an_empty_balance_refuses_before_the_call(self):
        """Charging for a request they could not afford would be taking money
        for something they did not get."""
        with self.assertRaises(credits.OutOfCredit) as ctx:
            credits.check_can_spend("u1")
        self.assertIn("out of credit", str(ctx.exception).lower())

    def test_the_nightly_ceiling_stops_a_runaway_before_the_balance_does(self):
        """The point of the cap: a bad loop costs one night, not a year."""
        credits.grant("u1", 500 * credits.MICROS)
        credits.record_charge("u1", credits.DEFAULT_NIGHTLY_CAP_MICROS, {})
        with self.assertRaises(credits.OutOfCredit) as ctx:
            credits.check_can_spend("u1")
        self.assertIn("tonight", str(ctx.exception).lower())
        self.assertGreater(credits.balance_micros("u1"), 0, "balance survived")

    def test_the_refusal_says_the_telescope_keeps_observing(self):
        """Running out of chat credit must not read as the telescope stopping."""
        credits.grant("u1", 500 * credits.MICROS)
        credits.record_charge("u1", credits.DEFAULT_NIGHTLY_CAP_MICROS, {})
        with self.assertRaises(credits.OutOfCredit) as ctx:
            credits.check_can_spend("u1")
        self.assertIn("observing", str(ctx.exception).lower())

    def test_the_ceiling_can_be_raised(self):
        credits.grant("u1", 500 * credits.MICROS)
        credits.record_charge("u1", credits.DEFAULT_NIGHTLY_CAP_MICROS, {})
        credits.set_nightly_cap("u1", 20 * credits.MICROS)
        credits.check_can_spend("u1")

    def test_the_summary_reports_what_is_left_tonight(self):
        credits.grant("u1", 50 * credits.MICROS)
        s = credits.summary("u1")
        self.assertEqual(s["remaining_tonight"],
                         credits.dollars(credits.DEFAULT_NIGHTLY_CAP_MICROS))
        self.assertEqual(s["markup_percent"], 10)


class StripeTest(_Base):

    def _event(self, event_id="evt_1", cents=500, user_id="u1"):
        return {"id": event_id, "type": "checkout.session.completed",
                "data": {"object": {"id": "cs_1", "amount_total": cents,
                                    "metadata": {"user_id": user_id,
                                                 "pack": "small"}}}}

    def _apply(self, event):
        with patch.object(billing, "credits", credits), \
             patch("stripe.Webhook.construct_event", return_value=event):
            return billing.apply_webhook(b"{}", "sig",
                                         {"stripe": {"webhook_secret": "whsec"}})

    def test_a_payment_becomes_credit(self):
        self._apply(self._event(cents=500))
        self.assertEqual(credits.balance_micros("u1"), 5 * credits.MICROS)

    def test_the_same_event_delivered_twice_credits_once(self):
        """Stripe redelivers. Without this, a retry is free money."""
        self._apply(self._event())
        result = self._apply(self._event())
        self.assertIn("duplicate", result)
        self.assertEqual(credits.balance_micros("u1"), 5 * credits.MICROS)

    def test_a_forged_webhook_is_rejected(self):
        """This endpoint is public and grants credit; the signature is all
        that stands between it and anyone minting a balance."""
        with patch("stripe.Webhook.construct_event",
                   side_effect=ValueError("bad signature")):
            with self.assertRaises(PermissionError):
                billing.apply_webhook(b"{}", "forged",
                                      {"stripe": {"webhook_secret": "whsec"}})
        self.assertEqual(credits.balance_micros("u1"), 0)

    def test_an_unrelated_event_type_is_ignored(self):
        event = self._event()
        event["type"] = "customer.created"
        self.assertIn("ignored", self._apply(event))

    def test_a_session_without_a_user_credits_nobody(self):
        event = self._event()
        event["data"]["object"]["metadata"] = {}
        self.assertIn("ignored", self._apply(event))
        self.assertEqual(len(self.db.ledger), 0)

    def test_an_unknown_pack_is_refused(self):
        with self.assertRaises(ValueError):
            billing.checkout_url("u1", "a@b.c", "enormous", {}, "s", "c")


class PricingTest(unittest.TestCase):
    """Cache reads are a tenth of the input rate. Billing them at full rate
    would overcharge by about ten times, because the cached tool block is most
    of every request."""

    class _Usage:
        def __init__(self, plain=0, read=0, write=0, out=0):
            self.input_tokens = plain
            self.cache_read_input_tokens = read
            self.cache_creation_input_tokens = write
            self.output_tokens = out

    def test_cache_reads_are_cheaper_than_fresh_input(self):
        cached = agent_chat.cost_micros("claude-opus-5", self._Usage(read=10_000))
        fresh = agent_chat.cost_micros("claude-opus-5", self._Usage(plain=10_000))
        self.assertAlmostEqual(fresh / cached, 10.0, delta=0.5)

    def test_cache_writes_cost_more_than_fresh_input(self):
        write = agent_chat.cost_micros("claude-opus-5", self._Usage(write=10_000))
        fresh = agent_chat.cost_micros("claude-opus-5", self._Usage(plain=10_000))
        self.assertGreater(write, fresh)

    def test_output_costs_more_than_input(self):
        out = agent_chat.cost_micros("claude-opus-5", self._Usage(out=1_000))
        inp = agent_chat.cost_micros("claude-opus-5", self._Usage(plain=1_000))
        self.assertGreater(out, inp)

    def test_a_realistic_cached_turn_is_pennies(self):
        cost = agent_chat.cost_micros(
            "claude-opus-5", self._Usage(plain=1_000, read=11_857, out=350))
        self.assertLess(credits.dollars(credits.with_markup(cost)), 0.05)

    def test_an_unknown_model_falls_back_rather_than_charging_nothing(self):
        cost = agent_chat.cost_micros("some-future-model", self._Usage(plain=10_000))
        self.assertGreater(cost, 0)


if __name__ == "__main__":
    unittest.main()
