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

import math
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

# ASSUMPTION (revised 2026-09-03): per-category amount distributions,
# right-skewed (log-normal), not uniform bands. Real UPI Autopay traffic
# is heavily right-skewed -- most debits small, a thin tail of large ones
# -- and uniform sampling over a wide band (the original version of this
# table) put far too much mass in the tail, which collided with M4's
# high-value escalation rule and drove escalation to 54% of the batch
# (see DECISIONS.md, "escalation threshold and amount distribution have
# to be set together"). `median` and `p99_tail` are the user-specified
# targets; `sigma` is derived from them (see _sample_amount_inr below),
# not independently chosen. `floor`/`cap` clip the unbounded log-normal
# tail to a sane range. Not sourced from any Razorpay data -- these
# medians are checked against the RBI April-2026 e-mandate AFA-exempt
# thresholds in the README, not derived from them.
AMOUNT_DISTRIBUTION_INR = {
    "OTT": {"median": 300, "p99_tail": 1_500, "floor": 49, "cap": 3_000},
    "UTILITY": {"median": 900, "p99_tail": 5_000, "floor": 99, "cap": 10_000},
    "SIP": {"median": 2_000, "p99_tail": 25_000, "floor": 199, "cap": 50_000},
    "INSURANCE": {"median": 3_000, "p99_tail": 30_000, "floor": 299, "cap": 60_000},
    "EMI": {"median": 6_000, "p99_tail": 40_000, "floor": 499, "cap": 80_000},
}

# z-score of the 99th percentile of a standard normal distribution --
# used to derive each category's log-normal sigma from its target
# median/tail ratio (see _sample_amount_inr).
_Z_P99 = 2.326

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

# --- Ground-truth noise around the congestion boundary (2026-09-03) -------
# Resolution: ground truth must be decided HERE, at generation time, and
# stamped on the event as `_true_bucket` -- not derived later from the same
# congestion_override config the classifier itself reads. Before this
# change, oracle.py's true_bucket() recomputed ground truth from that same
# YAML condition classify() uses, so the two could never disagree on any
# event with attempt_number == 1 (all of M2's output) -- grading measured
# "does the classifier's config match the oracle's config", not "does the
# classifier detect anything". These two rates make disagreement possible:
# `classify()` has no access to them or to `_true_bucket` (see
# tests/test_classifier.py::test_classify_never_reads_true_bucket).
#
# ASSUMPTION, not measurement -- documented here and in the README.
CONGESTION_FALSE_POSITIVE_RATE = 0.15  # in-window, technical reason, no balance history -- but NOT actually congestion
CONGESTION_FALSE_NEGATIVE_RATE = 0.10  # outside window, technical reason, no balance history -- but IS actually congestion (NPCI congestion isn't perfectly bounded by the window)

# When a false positive flips away from B1, it becomes one of these two
# plausible real causes. ASSUMPTION: no data to weight this precisely: a
# technical-looking failure that wasn't really congestion is modelled as
# somewhat more likely to be a genuine transient hiccup than a first-time
# (unflagged) balance issue.
CONGESTION_FALSE_POSITIVE_FLIP_TARGETS = ["B3_TRANSIENT", "B2_BALANCE"]
CONGESTION_FALSE_POSITIVE_FLIP_WEIGHTS = [60, 40]

# Noise is scoped to the same reason codes the classifier's congestion
# override itself considers (matrix.congestion_override.applies_to_reasons)
# -- we're modelling error in detecting congestion among failures that
# already look technical, not inventing congestion on codes that have
# nothing to do with it (e.g. incorrect_cvv).


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


def _sample_amount_inr(rng: random.Random, merchant_category: str) -> int:
    """Log-normal per category: median = target median, and sigma chosen
    so the target p99_tail lands at roughly the 99th percentile
    (sigma = ln(tail / median) / z_p99). Clipped to [floor, cap] since a
    log-normal's tail is technically unbounded and an occasional
    draw-of-10x-the-target-tail is not a realistic mandate amount."""
    params = AMOUNT_DISTRIBUTION_INR[merchant_category]
    mu = math.log(params["median"])
    sigma = math.log(params["p99_tail"] / params["median"]) / _Z_P99
    amount = rng.lognormvariate(mu, sigma)
    return round(min(max(amount, params["floor"]), params["cap"]))


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


def _decide_true_bucket(event: dict, rng: random.Random, matrix: DecisionMatrix) -> str:
    """The event's REAL underlying bucket -- ground truth, hidden from the
    classifier. Consults the same congestion_override condition the
    classifier reads (so there's a real, coherent rule to be noisy around,
    not noise for its own sake), then deliberately disagrees with it at
    the rates above. This is what makes classification a genuine inference
    problem instead of a config-agreement tautology."""
    reason = event["error"]["reason"]
    nominal_bucket = matrix.reason_codes[reason]["bucket"]

    override = matrix.congestion_override
    if reason in override["applies_to_reasons"]:
        lo, hi = override["condition"]["failed_at_hour_between"]
        in_window = lo <= event["failed_at"].hour < hi
        no_balance_history = (
            event["customer_history"]["prior_insufficient_funds_90d"]
            == override["condition"]["prior_insufficient_funds_90d"]
        )
        if in_window and no_balance_history:
            if rng.random() < CONGESTION_FALSE_POSITIVE_RATE:
                return rng.choices(CONGESTION_FALSE_POSITIVE_FLIP_TARGETS, weights=CONGESTION_FALSE_POSITIVE_FLIP_WEIGHTS)[0]
            return override["reclassify_to"]
        if (not in_window) and no_balance_history:
            if rng.random() < CONGESTION_FALSE_NEGATIVE_RATE:
                return override["reclassify_to"]

    return nominal_bucket


def generate_event(i: int, rng: random.Random, matrix: DecisionMatrix, distribution: dict[str, float]) -> dict:
    reasons = list(distribution.keys())
    weights = list(distribution.values())
    reason = rng.choices(reasons, weights=weights)[0]
    play = matrix.reason_codes[reason]
    source = play["source"]

    merchant_category = rng.choice(MERCHANT_CATEGORIES)
    amount_inr = _sample_amount_inr(rng, merchant_category)

    failed_at = _sample_failed_at(rng)

    mandate_id = f"mandate_{i:06d}"

    event = {
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

    # Hidden ground truth -- stamped last (needs the fields above), never
    # read by classify(). Leading underscore: not part of the real
    # Razorpay webhook shape, internal/test-only.
    event["_true_bucket"] = _decide_true_bucket(event, rng, matrix)
    return event


def generate_batch(count: int, seed: int, matrix: DecisionMatrix) -> list[dict]:
    rng = random.Random(seed)
    distribution = build_distribution(matrix)
    return [generate_event(i, rng, matrix, distribution) for i in range(count)]
