"""M2 -- the hidden ground-truth oracle.

Answers exactly one question: would a retry AT A GIVEN TIME succeed?
`oracle()` is a pure function of (event, retry_time, matrix) -- no
attempt-number term, no hidden mutable state. See DECISIONS.md
("Oracle model: conditional, not independent-per-attempt") and the
README for why this must be conditional on context, not drawn
independently per attempt.

ALL probabilities below are documented ASSUMPTIONS calibrated to keep the
simulated uplift in a defensible range (PRD sec. 11) -- not measurements.
Razorpay does not publish retry-success-by-context data.
"""

import calendar
from datetime import date, datetime

from app.config import settings
from app.matrix import DecisionMatrix

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

# Not used by oracle() -- retained here (same "one config block" the PRD
# asks for) for M5 (executor), which will need it to model whether a
# customer *accepts* a nudge. That's a different question from "would a
# retry succeed", so it's deliberately outside oracle()'s pure function.
NUDGE_ACCEPTANCE_PROBABILITIES = {
    "B5_DEAD_reauth": 0.30,        # unchanged from the PRD's original spec
    "B4_STRUCTURAL_channel_switch": 0.45,  # unchanged from the PRD's original spec
}

B3_TIME_THRESHOLD_HOURS = 2.0


def _next_payday_on_or_after(start: date, typical_credit_day: int) -> date:
    day = min(typical_credit_day, calendar.monthrange(start.year, start.month)[1])
    candidate = date(start.year, start.month, day)
    if candidate >= start:
        return candidate
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    day = min(typical_credit_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def true_bucket(event: dict, matrix: DecisionMatrix) -> str:
    """The event's REAL underlying bucket, for ground-truth purposes only.

    This is not the classifier (M3, not yet built) -- there's no confidence
    score, no signal list, no Decision record. It's a direct reuse of the
    same declarative condition already sitting in decision_matrix.yaml's
    congestion_override block, so that congestion is a genuine latent
    pattern in the data (discoverable via timing + history) rather than an
    apparent correlation with nothing real behind it. The classifier's
    later job is to rediscover this same condition from the event alone --
    this function is what makes that a real inference problem instead of a
    tautology.
    """
    reason = event["error"]["reason"]
    play = matrix.reason_codes.get(reason)
    if play is None:
        return "B_UNKNOWN"  # not reachable by construction -- the generator only emits matrix-known codes

    override = matrix.congestion_override
    if reason in override["applies_to_reasons"]:
        lo, hi = override["condition"]["failed_at_hour_between"]
        failed_at: datetime = event["failed_at"]
        in_window = lo <= failed_at.hour < hi
        no_balance_history = (
            event["customer_history"]["prior_insufficient_funds_90d"]
            == override["condition"]["prior_insufficient_funds_90d"]
        )
        if in_window and no_balance_history:
            return override["reclassify_to"]

    return play["bucket"]


def oracle(event: dict, retry_time: datetime, matrix: DecisionMatrix) -> float:
    """Would a retry at `retry_time` succeed? Returns a probability in
    [0, 1]. Callers draw against it with their own RNG -- this function
    only computes the probability, it never samples."""
    bucket = true_bucket(event, matrix)

    if bucket == "B2_BALANCE":
        typical_credit_day = event["customer_history"]["typical_credit_day"]
        next_payday = _next_payday_on_or_after(event["failed_at"].date(), typical_credit_day)
        after = retry_time.date() >= next_payday
        return ORACLE_PROBABILITIES["B2_BALANCE"]["after_payday" if after else "before_payday"]

    if bucket == "B1_CONGESTION":
        restricted = settings.npci_restricted_start_hour <= retry_time.hour < settings.npci_restricted_end_hour
        return ORACLE_PROBABILITIES["B1_CONGESTION"]["restricted_window" if restricted else "safe_window"]

    if bucket == "B3_TRANSIENT":
        elapsed_hours = (retry_time - event["failed_at"]).total_seconds() / 3600
        return ORACLE_PROBABILITIES["B3_TRANSIENT"]["over_2h" if elapsed_hours >= B3_TIME_THRESHOLD_HOURS else "under_2h"]

    if bucket == "B4_STRUCTURAL":
        return ORACLE_PROBABILITIES["B4_STRUCTURAL"]["always"]

    if bucket == "B5_DEAD":
        return ORACLE_PROBABILITIES["B5_DEAD"]["always"]

    return 0.0  # B_UNKNOWN -- never retried, so this is never actually queried
