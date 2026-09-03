"""M3 -- the classifier.

Pure function: (FailureEvent, DecisionMatrix) -> {bucket, confidence,
signals}. No side effects, no DB writes, no network calls, no mutable
state carried between calls -- everything it needs is either in the event
itself or in the (already-loaded) decision matrix.

Path priority, matching PRD sec. M3's own numbered order:
  1. Unknown code            -- reason not in the matrix at all -> B_UNKNOWN, confidence 0.0. Never guess.
  2. Congestion override      -- checked BEFORE ambiguity handling (see note below)
  3. Ambiguity handling       -- driven by the YAML's `ambiguous: true` flag, not a hardcoded reason-code name
  4. Balance-pattern boost    -- applied last, only affects confidence, never the bucket
  (0. Confidence floor        -- applied to every path's output: below threshold -> B_UNKNOWN, fail closed)

Why congestion override is checked before ambiguity: `payment_failed` is
both ambiguous AND congestion-eligible (it's in
congestion_override.applies_to_reasons). If we have real timing+history
evidence that this specific failure was congestion, that's stronger
information than "we have no evidence at all, so hedge" -- so timing
evidence wins and the ambiguity path never even runs for that event.
"""

from app.config import settings
from app.matrix import DecisionMatrix

# ASSUMPTION values not given an exact number by the PRD/matrix, documented
# here as the single place they're defined:
BASE_CONFIDENCE = 0.9  # PRD sec. M3 step 1: "base confidence 0.9"
AMBIGUOUS_FIRST_ATTEMPT_CONFIDENCE = 0.55  # PRD sec. M3 step 3
AMBIGUOUS_RECLASSIFIED_CONFIDENCE = 0.75  # PRD sec. M3 step 3
BALANCE_PATTERN_THRESHOLD = 2  # PRD sec. M3 step 4: "prior_insufficient_funds_90d >= 2"
BALANCE_PATTERN_CONFIDENCE_BOOST = 0.05  # ASSUMPTION -- PRD says "confidence boost", no magnitude given


def _apply_confidence_floor(bucket: str, confidence: float, signals: list[str]) -> dict:
    """Resolution 2026-09-03: any classification below the configured
    threshold fails closed to B_UNKNOWN, regardless of which path produced
    it. The original confidence is kept (not zeroed) -- B_UNKNOWN here
    means "not confident enough to act", which is a different, more
    informative statement than "we have zero information" (that's the
    reason-not-in-matrix case, which really is 0.0)."""
    if confidence < settings.unknown_bucket_confidence_threshold:
        return {
            "bucket": "B_UNKNOWN",
            "confidence": confidence,
            "signals": [*signals, "confidence_below_threshold"],
        }
    return {"bucket": bucket, "confidence": confidence, "signals": signals}


def classify(event: dict, matrix: DecisionMatrix) -> dict:
    reason = event["error"]["reason"]
    play = matrix.reason_codes.get(reason)

    # Path 1: unknown code. Never guess.
    if play is None:
        return {
            "bucket": "B_UNKNOWN",
            "confidence": 0.0,
            "signals": ["reason_code_not_in_matrix"],
        }

    # Path 2: congestion override -- the USP. Same condition the oracle's
    # ground-truth resolution uses (generator/oracle.py:true_bucket), read
    # directly from the YAML rather than re-hardcoded here.
    override = matrix.congestion_override
    if reason in override["applies_to_reasons"]:
        lo, hi = override["condition"]["failed_at_hour_between"]
        in_restricted_window = lo <= event["failed_at"].hour < hi
        no_balance_history = (
            event["customer_history"]["prior_insufficient_funds_90d"]
            == override["condition"]["prior_insufficient_funds_90d"]
        )
        if in_restricted_window and no_balance_history:
            return _apply_confidence_floor(
                override["reclassify_to"],
                override["confidence"],
                [override["signal"], "no_balance_failure_history"],
            )

    # Path 3: ambiguity handling. Keyed off the `ambiguous` flag generically
    # -- payment_failed is not special-cased by name, so card_declined /
    # debit_declined / payment_declined (reclassified to ambiguous
    # B3_TRANSIENT earlier this build) get identical treatment for free.
    if play.get("ambiguous"):
        if event["attempt_number"] <= 1:
            return _apply_confidence_floor(
                play["bucket"],
                AMBIGUOUS_FIRST_ATTEMPT_CONFIDENCE,
                ["ambiguous_first_attempt"],
            )
        return _apply_confidence_floor(
            "B5_DEAD",
            AMBIGUOUS_RECLASSIFIED_CONFIDENCE,
            ["reclassified_after_failed_retry"],
        )

    # Base lookup (path 1 in the PRD's own numbering), with path 4 (balance
    # pattern) layered on top -- confidence only, never changes the bucket.
    bucket = play["bucket"]
    confidence = BASE_CONFIDENCE
    signals = ["base_lookup"]

    if bucket == "B2_BALANCE" and event["customer_history"]["prior_insufficient_funds_90d"] >= BALANCE_PATTERN_THRESHOLD:
        confidence = min(1.0, confidence + BALANCE_PATTERN_CONFIDENCE_BOOST)
        signals.append("recurring_balance_pattern")

    return _apply_confidence_floor(bucket, confidence, signals)
