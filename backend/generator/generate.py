"""M2 -- synthetic failed-mandate event generator.

Produces N FailureEvent payloads (Razorpay webhook-shaped, nested error
object -- matching PRD sec. 6, not the flattened DB row shape the
ingestion tier writes later). All randomness comes from one seeded
`random.Random` instance, never the stdlib global `random` module, so a
given --seed is fully reproducible regardless of run order or environment.

Every distributional choice below (timestamp window, merchant-category
mix, amount bands, prior-failure history) is a documented ASSUMPTION, not
a measurement -- flagged inline where it isn't already covered by
distribution.py or oracle.py.
"""

import random
from datetime import date, datetime, timedelta

from app.config import settings
from app.matrix import DecisionMatrix
from generator.distribution import build_distribution

# ASSUMPTION: fixed synthetic simulation window, independent of wall-clock
# time, so --seed 42 reproduces identically no matter what day this is run.
REFERENCE_START_DATE = date(2026, 8, 1)
GENERATION_WINDOW_DAYS = 30

# PRD sec. M2: 35% of original debits fall inside the NPCI restricted
# window -- this is what makes the congestion USP measurable.
RESTRICTED_WINDOW_SHARE = 0.35

# ASSUMPTION: uniform across categories -- the PRD does not specify a
# merchant-category mix.
MERCHANT_CATEGORIES = ["OTT", "SIP", "EMI", "UTILITY", "INSURANCE"]

# ASSUMPTION: per-category amount bands (INR), loosely realistic (OTT
# subscriptions are cheap, SIPs/insurance premiums are large). Not sourced
# from any Razorpay data.
AMOUNT_BANDS_INR = {
    "OTT": (99, 999),
    "SIP": (500, 50_000),
    "EMI": (999, 25_000),
    "UTILITY": (199, 5_000),
    "INSURANCE": (999, 50_000),
}

# ASSUMPTION: reason codes involving OTP/CVV/auth happen at the
# authentication step; everything else at authorization. Not verified
# against Razorpay's actual `step` values (that verification pass covered
# `reason` and `source`, not `step`) -- flagged as an assumption in the
# README.
AUTHENTICATION_STEP_REASONS = {
    "authentication_failed",
    "incorrect_otp",
    "otp_attempts_exceeded",
    "otp_expired",
    "incorrect_cvv",
    "reqauth_mandate_not_acknowledged",
}


def _error_code_for_source(source: str) -> str:
    """Razorpay's own top-level code is BAD_REQUEST_ERROR for
    customer/business-sourced errors, GATEWAY_ERROR for gateway/razorpay-
    sourced ones -- derived directly from how razorpay.com/docs/errors/
    payments/list/ groups its two tables ("Bad Request Errors" section vs
    "Gateway Errors" section), not an independent guess."""
    return "BAD_REQUEST_ERROR" if source in ("customer", "business") else "GATEWAY_ERROR"


def _sample_failed_at(rng: random.Random) -> datetime:
    day_offset = rng.randint(0, GENERATION_WINDOW_DAYS - 1)
    day = REFERENCE_START_DATE + timedelta(days=day_offset)

    if rng.random() < RESTRICTED_WINDOW_SHARE:
        hour = rng.randint(settings.npci_restricted_start_hour, settings.npci_restricted_end_hour - 1)
    else:
        safe_hours = [h for h in range(24) if not (settings.npci_restricted_start_hour <= h < settings.npci_restricted_end_hour)]
        hour = rng.choice(safe_hours)

    return datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59))


def _sample_prior_insufficient_funds(rng: random.Random, reason: str) -> int:
    # ASSUMPTION: insufficient_funds failures more often carry a recurring
    # balance-history pattern; everything else mostly doesn't. This is
    # deliberately shaped to give the future classifier's "recurring
    # balance pattern" signal (M3) real data to key off, and to leave a
    # meaningful share of restricted-window B3-coded events eligible for
    # the congestion override (which requires prior_insufficient_funds_90d
    # == 0).
    if reason == "insufficient_funds":
        return rng.choices([0, 1, 2, 3], weights=[20, 30, 30, 20])[0]
    return rng.choices([0, 1, 2, 3], weights=[70, 20, 7, 3])[0]


def generate_event(i: int, rng: random.Random, matrix: DecisionMatrix, distribution: dict[str, float]) -> dict:
    reasons = list(distribution.keys())
    weights = list(distribution.values())
    reason = rng.choices(reasons, weights=weights)[0]
    play = matrix.reason_codes[reason]
    source = play["source"]

    merchant_category = rng.choice(MERCHANT_CATEGORIES)
    lo, hi = AMOUNT_BANDS_INR[merchant_category]
    amount_inr = rng.randint(lo, hi)

    failed_at = _sample_failed_at(rng)

    mandate_id = f"mandate_{i:06d}"

    return {
        "event_id": f"evt_{i:06d}",
        "payment_id": f"pay_{i:06d}",
        "mandate_id": mandate_id,
        "customer_id": f"cust_{i:06d}",
        "merchant_category": merchant_category,
        "amount_inr": amount_inr,
        "failed_at": failed_at,
        # cycle_id: a cycle is the 7-day recovery window for one failed
        # mandate debit (Resolution, 2026-09-03) -- the generator emits one
        # fresh failure per mandate, so this is always that mandate's first
        # cycle. Subsequent cycles (repeat-offender mandates) are an
        # executor-phase concern, not M2's.
        "cycle_id": f"{mandate_id}_cycle1",
        "error": {
            "code": _error_code_for_source(source),
            "source": source,
            "step": "payment_authentication" if reason in AUTHENTICATION_STEP_REASONS else "payment_authorization",
            "reason": reason,
        },
        # ASSUMPTION: the generator only ever emits FIRST-attempt failures.
        # Subsequent retry attempts are produced later by the executor
        # (M5), not by M2.
        "attempt_number": 1,
        "customer_history": {
            "prior_failures_90d": rng.choices([0, 1, 2, 3, 4, 5], weights=[40, 25, 15, 10, 6, 4])[0],
            "prior_insufficient_funds_90d": _sample_prior_insufficient_funds(rng, reason),
            "typical_credit_day": rng.randint(1, 28),
            "mandate_age_days": rng.randint(7, 730),
        },
    }


def generate_batch(count: int, seed: int, matrix: DecisionMatrix) -> list[dict]:
    rng = random.Random(seed)
    distribution = build_distribution(matrix)
    return [generate_event(i, rng, matrix, distribution) for i in range(count)]
