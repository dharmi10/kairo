"""M2 -- the hidden ground-truth oracle.

Answers exactly one question: would a retry AT A GIVEN TIME succeed?
`oracle()` is a pure function of (event, retry_time) -- no attempt-number
term, no hidden mutable state, no matrix lookup (ground truth is read from
`event["_true_bucket"]`, stamped by the generator -- see generate.py).
See DECISIONS.md ("Oracle model: conditional, not independent-per-attempt")
and the README for why this must be conditional on context, not drawn
independently per attempt.

ALL probabilities below are documented ASSUMPTIONS calibrated to keep the
simulated uplift in a defensible range (PRD sec. 11) -- not measurements.
Razorpay does not publish retry-success-by-context data.

Phase 6 addition -- `draw_retry_outcome()`: being conditional on context
is necessary but NOT sufficient to avoid compounding. If a simulation
draws an INDEPENDENT random outcome at every attempt, two attempts under
IDENTICAL context (e.g. baseline's 3 same-time-of-day retries, all
landing in the same NPCI-restricted hour) still compound toward near-
certain success via 1-(1-p)^3, even though p correctly reflects the
context each time and nothing about that context ever changed. That's
the same artifact the conditional-oracle redesign was meant to prevent,
just reintroduced one layer up, in how a simulation consumes the oracle
rather than in the oracle's probabilities. The fix: attempts sharing the
same (bucket, context-state) key must share the same drawn outcome --
"we already tried under these exact conditions and it didn't work" is
what "the underlying cause persists" (see DECISIONS.md) actually implies.
A NEW context (the window changed, elapsed time crossed 2h, payday
arrived) gets a genuinely new, independent draw.

Later addition -- draws are keyed off an independent, deterministic
per-draw RNG stream (`app/rng.py::deterministic_random`), not a single
shared `random.Random` consumed sequentially across the whole batch. A
shared sequential stream means adding any new draw upstream (anywhere in
the batch, for either arm) shifts every draw that comes after it, which
makes "the baseline recovers Rs.X" not a stable fact -- it would move
when unrelated code changed. See DECISIONS.md.
"""

from datetime import datetime

from app.config import settings
from app.dates import next_payday_on_or_after
from app.rng import deterministic_random

# --- THE assumption block -------------------------------------------------
# Every number the oracle can return lives here, in one place, so it can be
# audited and cited as a single, clearly-labelled block of assumptions.
ORACLE_PROBABILITIES = {
    "B2_BALANCE": {"before_payday": 0.25, "after_payday": 0.55},
    "B1_CONGESTION": {"restricted_window": 0.35, "safe_window": 0.70},
    "B3_TRANSIENT": {"under_2h": 0.35, "over_2h": 0.70},
    "B4_STRUCTURAL": {"always": 0.20},  # unchanged from the PRD's original spec
    "B5_DEAD": {"always": 0.0},
}

# Wired in via draw_nudge_acceptance() below (2026-09-03) -- modelling
# whether a customer *accepts* a nudge is a different question from
# "would a retry succeed" (a different recovery mechanism: customer
# behaviour, not retry timing), so it's kept as its own function/table
# rather than folded into ORACLE_PROBABILITIES, but it's no longer
# unused: an agent that correctly routes a dead/structural failure to a
# nudge instead of a blind retry needs to get credit for the customer
# sometimes acting on it, or the $-recovered comparison structurally
# can't reward that choice (every nudge would be worth Rs.0, while every
# baseline blind retry can still win) -- see DECISIONS.md.
NUDGE_ACCEPTANCE_PROBABILITIES = {
    "B5_DEAD_reauth": 0.30,        # unchanged from the PRD's original spec
    "B4_STRUCTURAL_channel_switch": 0.45,  # unchanged from the PRD's original spec
}

# Which NUDGE_ACCEPTANCE_PROBABILITIES key applies to a nudge sent for a
# given bucket. Both of B5_DEAD's and B4_STRUCTURAL's matrix-prescribed
# nudge actions (NUDGE_REAUTH; NUDGE_CUSTOMER_LINK / NUDGE_REENGAGE /
# NUDGE_ALT_METHOD respectively -- see executor.py's
# _NUDGE_MATRIX_ACTIONS) share ONE acceptance probability per bucket, not
# one per specific action -- the PRD gave exactly two numbers, at bucket
# granularity ("B5 reauth", "B4 channel switch"), not per matrix action.
NUDGE_ACCEPTANCE_KEY_BY_BUCKET = {
    "B5_DEAD": "B5_DEAD_reauth",
    "B4_STRUCTURAL": "B4_STRUCTURAL_channel_switch",
}

B3_TIME_THRESHOLD_HOURS = 2.0


def oracle_context_key(event: dict, retry_time: datetime) -> tuple[str, str]:
    """(bucket, context-state) for a retry at `retry_time`. Two attempts
    with the same key are, by this model's own definition, identical
    conditions. `oracle()` is a thin wrapper around this plus a table
    lookup -- kept as one function each does one job, but sharing this
    exact branching so they can never drift apart."""
    bucket = event["_true_bucket"]

    if bucket == "B2_BALANCE":
        typical_credit_day = event["customer_history"]["typical_credit_day"]
        next_payday = next_payday_on_or_after(event["failed_at"].date(), typical_credit_day)
        after = retry_time.date() >= next_payday
        return bucket, ("after_payday" if after else "before_payday")

    if bucket == "B1_CONGESTION":
        restricted = settings.npci_restricted_start_hour <= retry_time.hour < settings.npci_restricted_end_hour
        return bucket, ("restricted_window" if restricted else "safe_window")

    if bucket == "B3_TRANSIENT":
        elapsed_hours = (retry_time - event["failed_at"]).total_seconds() / 3600
        return bucket, ("over_2h" if elapsed_hours >= B3_TIME_THRESHOLD_HOURS else "under_2h")

    if bucket in ("B4_STRUCTURAL", "B5_DEAD"):
        return bucket, "always"

    return bucket, "always"  # B_UNKNOWN -- not reachable by construction; ORACLE_PROBABILITIES has no entry, oracle() returns 0.0


def oracle(event: dict, retry_time: datetime) -> float:
    """Would a retry at `retry_time` succeed? Returns a probability in
    [0, 1]. Callers draw against it with their own RNG -- this function
    only computes the probability, it never samples. Pure function of
    exactly (event, retry_time), per the original spec.

    Reads ground truth from `event["_true_bucket"]`, stamped by the
    generator at generation time (generator/generate.py::_decide_true_bucket)
    -- NOT recomputed here. See DECISIONS.md, "Ground truth decoupled from
    classifier config".
    """
    bucket, state = oracle_context_key(event, retry_time)
    return ORACLE_PROBABILITIES.get(bucket, {}).get(state, 0.0)


def draw_retry_outcome(event: dict, retry_time: datetime, context_outcomes: dict, seed) -> bool:
    """Would THIS attempt succeed -- correlated with any prior attempt (in
    either arm) that shared the same (bucket, context-state) for this
    same event. `context_outcomes` is a plain dict the caller creates
    FRESH per event and passes to every attempt across BOTH arms'
    simulation of that event -- so if baseline and the agent happen to
    retry under the identical context, they see the identical simulated
    outcome (that context either clears or it doesn't; it isn't a
    separate coin flip per arm), and repeated attempts under unchanged
    context never compound.

    `seed` (normally an int -- see app/rng.py) seeds an INDEPENDENT
    stream for this exact (event, bucket, context-state) the first time
    it's drawn -- not a shared, sequentially-consumed RNG. Adding an
    unrelated draw anywhere else (a different event, a different draw
    type, a different context) can never perturb this one.
    """
    key = oracle_context_key(event, retry_time)
    if key not in context_outcomes:
        p = ORACLE_PROBABILITIES.get(key[0], {}).get(key[1], 0.0)
        rng = deterministic_random(seed, event["event_id"], "retry", key[0], key[1])
        context_outcomes[key] = rng.random() < p
    return context_outcomes[key]


def draw_nudge_acceptance(event: dict, bucket: str, context_outcomes: dict, seed) -> bool:
    """Would the customer act on a nudge sent for THIS event? Same
    correlated-draw-per-event pattern as draw_retry_outcome (see its
    docstring, including the independent-stream `seed` semantics) -- if a
    nudge for this event were ever considered more than once in one
    simulated cycle, it must resolve to the SAME outcome, not a fresh
    coin flip, for the same "the underlying state persists" reasoning.
    Keyed as `(bucket, "nudge_acceptance")` -- a marker string that can
    never collide with a real (bucket, context-state) retry key from
    oracle_context_key(), since none of those states are literally
    "nudge_acceptance". A bucket with no entry in
    NUDGE_ACCEPTANCE_KEY_BY_BUCKET (i.e. anything other than
    B4_STRUCTURAL / B5_DEAD -- the only buckets the matrix ever sends a
    NUDGE_SENT action for today) defaults to p=0.0, fail-closed, rather
    than crashing on an unrecognised bucket.
    """
    key = (bucket, "nudge_acceptance")
    if key not in context_outcomes:
        p = NUDGE_ACCEPTANCE_PROBABILITIES.get(NUDGE_ACCEPTANCE_KEY_BY_BUCKET.get(bucket, ""), 0.0)
        rng = deterministic_random(seed, event["event_id"], "nudge", bucket)
        context_outcomes[key] = rng.random() < p
    return context_outcomes[key]
