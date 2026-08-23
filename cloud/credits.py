"""Credits: what a member has bought, what they have spent, and tonight's ceiling.

Members buy credits through Stripe and spend them on model usage when they talk
to their telescope. Two numbers govern every request, and they are different
protections:

  balance     what they have left. Runs out, they buy more.
  nightly cap the most that can be spent in one day, whatever the balance.

The cap is the one that matters. A balance alone protects the charity but not
the member: an agent loop that misbehaves at 2am can drain a year of credit
before anyone wakes up, and "you have no credit and we cannot tell you why" is
a far worse morning than "tonight stopped early". The cap makes the blast
radius one night.

Everything is recorded in whole micro-dollars (millionths). Float arithmetic on
money accumulates error, and this ledger has to reconcile against Stripe.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from . import db

logger = logging.getLogger("cloud.credits")

#: Ledger units. A micro-dollar is 1/1_000_000 of a dollar.
MICROS = 1_000_000

#: What TTN adds on top of the model's own cost.
MARKUP = 0.10

#: Most that may be spent on one member in one day, regardless of balance.
DEFAULT_NIGHTLY_CAP_MICROS = 2 * MICROS      # $2.00

#: Credit given to a new member so setup never hits a paywall. Setup costs
#: well under this; the point is that nobody's first experience is a bill.
WELCOME_MICROS = 3 * MICROS                  # $3.00

CHARGE, PURCHASE, WELCOME, REFUND = "charge", "purchase", "welcome", "refund"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def dollars(micros: int) -> float:
    return round(micros / MICROS, 4)


def to_micros(dollars_value: float) -> int:
    """Round *up*, so rounding never quietly favours the spender."""
    return int(-(-float(dollars_value) * MICROS // 1))


def with_markup(cost_micros: int) -> int:
    return int(-(-cost_micros * (1.0 + MARKUP) // 1))


# ── balance ──────────────────────────────────────────────────────────────────

def balance_micros(user_id: str) -> int:
    row = db.query_one(
        "SELECT COALESCE(SUM(amount_micros), 0) AS total "
        "FROM credit_ledger WHERE user_id = %s", (user_id,))
    return int((row or {}).get("total") or 0)


def spent_today_micros(user_id: str) -> int:
    row = db.query_one(
        """SELECT COALESCE(SUM(-amount_micros), 0) AS total
           FROM credit_ledger
           WHERE user_id = %s AND kind = %s AND day = %s""",
        (user_id, CHARGE, _today()))
    return int((row or {}).get("total") or 0)


def nightly_cap_micros(user_id: str) -> int:
    row = db.query_one(
        "SELECT nightly_cap_micros FROM credit_settings WHERE user_id = %s",
        (user_id,))
    if row and row.get("nightly_cap_micros") is not None:
        return int(row["nightly_cap_micros"])
    return DEFAULT_NIGHTLY_CAP_MICROS


def set_nightly_cap(user_id: str, cap_micros: int) -> None:
    db.execute(
        """INSERT INTO credit_settings (user_id, nightly_cap_micros, updated_at)
           VALUES (%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE
             SET nightly_cap_micros = EXCLUDED.nightly_cap_micros,
                 updated_at = EXCLUDED.updated_at""",
        (user_id, max(0, int(cap_micros)), _now()))


def summary(user_id: str) -> dict:
    bal = balance_micros(user_id)
    spent = spent_today_micros(user_id)
    cap = nightly_cap_micros(user_id)
    return {
        "balance": dollars(bal),
        "spent_today": dollars(spent),
        "nightly_cap": dollars(cap),
        "remaining_tonight": dollars(max(0, min(bal, cap - spent))),
        "markup_percent": int(MARKUP * 100),
    }


# ── spending ─────────────────────────────────────────────────────────────────

class OutOfCredit(RuntimeError):
    """Raised when a request cannot be paid for. The message is shown to the member."""


def check_can_spend(user_id: str) -> None:
    """Refuse before calling the model, not after.

    Charging for a request the member could not afford would mean taking money
    for something they did not get.
    """
    bal = balance_micros(user_id)
    if bal <= 0:
        raise OutOfCredit(
            "You are out of credit. Top up to keep talking to your telescope — "
            "your telescope keeps observing either way; only this conversation "
            "needs credit.")

    cap, spent = nightly_cap_micros(user_id), spent_today_micros(user_id)
    if spent >= cap:
        raise OutOfCredit(
            f"That is tonight's spending limit (${dollars(cap):.2f}) reached. "
            f"It resets at midnight UTC, and your telescope carries on "
            f"observing regardless. Raise the limit if you want more headroom.")


def record_charge(user_id: str, cost_micros: int, detail: dict) -> dict:
    """Charge for one model call. Returns the member's position afterwards."""
    charged = with_markup(max(0, int(cost_micros)))
    db.execute(
        """INSERT INTO credit_ledger
               (user_id, kind, amount_micros, day, created_at, detail_json)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (user_id, CHARGE, -charged, _today(), _now(), json.dumps(detail)))
    return summary(user_id)


def grant(user_id: str, amount_micros: int, kind: str = PURCHASE,
          detail: Optional[dict] = None) -> dict:
    """Add credit: a purchase, the welcome grant, or a refund."""
    db.execute(
        """INSERT INTO credit_ledger
               (user_id, kind, amount_micros, day, created_at, detail_json)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (user_id, kind, max(0, int(amount_micros)), _today(), _now(),
         json.dumps(detail or {})))
    return summary(user_id)


def grant_welcome_once(user_id: str) -> bool:
    """Give a new member their starting credit, exactly once.

    Guarded rather than trusted to the caller: a retried signup or a
    double-fired webhook must not mint credit.
    """
    row = db.query_one(
        "SELECT 1 FROM credit_ledger WHERE user_id = %s AND kind = %s",
        (user_id, WELCOME))
    if row:
        return False
    grant(user_id, WELCOME_MICROS, WELCOME, {"reason": "new member"})
    return True


def already_recorded(reference: str) -> bool:
    """Whether a Stripe event has been applied. Webhooks are delivered more
    than once, and each delivery must not add credit again."""
    row = db.query_one(
        "SELECT 1 FROM credit_ledger WHERE reference = %s", (reference,))
    return bool(row)


def grant_purchase(user_id: str, amount_micros: int, reference: str,
                   detail: Optional[dict] = None) -> Optional[dict]:
    """Apply a completed Stripe payment. Returns None if already applied."""
    if already_recorded(reference):
        logger.info("Stripe event %s already applied; ignoring", reference)
        return None
    db.execute(
        """INSERT INTO credit_ledger
               (user_id, kind, amount_micros, day, created_at, detail_json,
                reference)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, PURCHASE, max(0, int(amount_micros)), _today(), _now(),
         json.dumps(detail or {}), reference))
    return summary(user_id)


def history(user_id: str, limit: int = 50) -> list[dict]:
    rows = db.query(
        """SELECT kind, amount_micros, created_at, detail_json
           FROM credit_ledger WHERE user_id = %s
           ORDER BY id DESC LIMIT %s""", (user_id, int(limit)))
    return [{"kind": r["kind"], "amount": dollars(int(r["amount_micros"])),
             "at": r["created_at"], "detail": db.loads(r.get("detail_json"), {})}
            for r in rows]
