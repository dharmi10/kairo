"""M6 -- the baseline simulator.

Replicates Razorpay's ACTUAL documented retry behaviour (verified against
razorpay.com/docs/payments/subscriptions/payment-retries/ -- see
DECISIONS.md, "M6 baseline correction"): 3 automatic retries at T+1/T+2/T+3
days, each at the SAME time of day as the original failure. No
reason-awareness (never calls classify()), no window-snapping (the whole
point -- a retry CAN land straight back in the restricted window that
killed the payment in the first place), hard declines retried too (no
bucket check of any kind). Stops early on the first success; after 3
straight fails, the subscription moves to `halted`.
"""

from datetime import timedelta

from generator.oracle import draw_retry_outcome

BASELINE_RETRY_DAY_OFFSETS = (1, 2, 3)


def simulate_baseline_cycle(event: dict, bucket_for_grouping: str, context_outcomes: dict, seed) -> dict:
    """`bucket_for_grouping` is NOT baseline logic -- baseline has no
    concept of buckets at all, that's the entire point of it being blind.
    It's passed in purely so the by-bucket comparison table (metrics/report.py)
    can show "of the events the AGENT classified as B2, how did each arm
    do" -- the only bucket label that exists for either arm comes from the
    agent's own classification of the identical event.

    `context_outcomes` -- a dict the caller creates FRESH per event and
    shares with the agent's simulation of the SAME event -- is what
    prevents baseline's 3 same-time-of-day retries (which very often land
    in the identical context bucket, e.g. all three inside the restricted
    window) from compounding toward near-certain success via independent
    draws. `seed` (normally an int -- see app/rng.py) derives an
    independent stream per (event, context) the first time it's drawn,
    rather than consuming a shared sequential RNG -- see
    generator/oracle.py::draw_retry_outcome."""
    outcome = "FAILED"
    attempts_made = 0
    recovered_at = None

    for day_offset in BASELINE_RETRY_DAY_OFFSETS:
        retry_time = event["failed_at"] + timedelta(days=day_offset)
        attempts_made += 1
        if draw_retry_outcome(event, retry_time, context_outcomes, seed):
            outcome = "RECOVERED"
            recovered_at = retry_time
            break

    hours_to_recovery = (recovered_at - event["failed_at"]).total_seconds() / 3600 if recovered_at else None

    return {
        "event_id": event["event_id"],
        "amount_inr": event["amount_inr"],
        "bucket": bucket_for_grouping,
        "outcome": outcome,
        "attempts_made": attempts_made,
        "contacts_sent": 0,  # baseline never nudges -- Razorpay's documented behaviour is retry-only
        "hours_to_recovery": hours_to_recovery,
    }
