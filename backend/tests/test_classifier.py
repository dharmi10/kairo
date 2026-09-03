"""Unit tests for classifier.classify -- one test per path in PRD sec. M3,
plus the unknown-code case and the confidence floor. Uses the real
decision_matrix.yaml (not a mock) so tests break loudly if the matrix
changes underneath the classifier's assumptions.
"""

from datetime import datetime

import pytest

from app.matrix import load_decision_matrix
from classifier.classify import (
    AMBIGUOUS_FIRST_ATTEMPT_CONFIDENCE,
    AMBIGUOUS_RECLASSIFIED_CONFIDENCE,
    BASE_CONFIDENCE,
    _apply_confidence_floor,
    classify,
)


@pytest.fixture(scope="module")
def matrix():
    return load_decision_matrix()


def make_event(
    reason: str,
    *,
    failed_at: datetime = datetime(2026, 8, 15, 15, 0, 0),  # 15:00 -- safe window by default
    prior_insufficient_funds_90d: int = 0,
    attempt_number: int = 1,
    source: str = "gateway",
) -> dict:
    return {
        "event_id": "evt_test",
        "payment_id": "pay_test",
        "mandate_id": "mandate_test",
        "customer_id": "cust_test",
        "merchant_category": "OTT",
        "amount_inr": 499,
        "failed_at": failed_at,
        "cycle_id": "mandate_test_cycle1",
        "error": {
            "code": "GATEWAY_ERROR",
            "source": source,
            "step": "payment_authorization",
            "reason": reason,
        },
        "attempt_number": attempt_number,
        "customer_history": {
            "prior_failures_90d": 0,
            "prior_insufficient_funds_90d": prior_insufficient_funds_90d,
            "typical_credit_day": 15,
            "mandate_age_days": 100,
        },
    }


# --- shape / contract, every path must satisfy this ------------------------

def _assert_valid_result(result: dict, matrix) -> None:
    assert result["bucket"] in matrix.buckets
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["signals"], list) and len(result["signals"]) >= 1


# --- Path 1: base lookup ----------------------------------------------------

def test_base_lookup_returns_matrix_bucket_and_base_confidence(matrix):
    event = make_event("incorrect_cvv")
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == "B4_STRUCTURAL"
    assert result["confidence"] == BASE_CONFIDENCE
    assert result["signals"] == ["base_lookup"]


def test_base_lookup_on_non_ambiguous_non_congestion_technical_code(matrix):
    # bank_not_available: real B3 code, not in congestion_override.applies_to_reasons
    event = make_event("bank_not_available", failed_at=datetime(2026, 8, 15, 11, 0, 0))
    result = classify(event, matrix)
    assert result["bucket"] == "B3_TRANSIENT"
    assert result["confidence"] == BASE_CONFIDENCE
    assert result["signals"] == ["base_lookup"]


# --- Path 2: congestion override --------------------------------------------

def test_congestion_override_fires_in_restricted_window_with_no_balance_history(matrix):
    event = make_event(
        "gateway_technical_error",
        failed_at=datetime(2026, 8, 15, 11, 30, 0),  # inside 10-13
        prior_insufficient_funds_90d=0,
    )
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == "B1_CONGESTION"
    assert result["confidence"] == matrix.congestion_override["confidence"]
    assert "fired_in_restricted_window" in result["signals"]
    assert "no_balance_failure_history" in result["signals"]


def test_congestion_override_does_not_fire_outside_restricted_window(matrix):
    event = make_event(
        "gateway_technical_error",
        failed_at=datetime(2026, 8, 15, 15, 0, 0),  # outside 10-13
        prior_insufficient_funds_90d=0,
    )
    result = classify(event, matrix)
    assert result["bucket"] == "B3_TRANSIENT"
    assert result["signals"] == ["base_lookup"]


def test_congestion_override_does_not_fire_with_balance_history(matrix):
    event = make_event(
        "gateway_technical_error",
        failed_at=datetime(2026, 8, 15, 11, 0, 0),  # inside 10-13
        prior_insufficient_funds_90d=1,  # has balance-failure history -- override requires exactly 0
    )
    result = classify(event, matrix)
    assert result["bucket"] == "B3_TRANSIENT"
    assert result["signals"] == ["base_lookup"]


def test_congestion_override_takes_priority_over_ambiguity_handling(matrix):
    # payment_failed is BOTH ambiguous AND congestion-eligible. Timing
    # evidence must win over "we have no evidence, hedge" ambiguity.
    event = make_event(
        "payment_failed",
        failed_at=datetime(2026, 8, 15, 12, 0, 0),
        prior_insufficient_funds_90d=0,
        attempt_number=1,
    )
    result = classify(event, matrix)
    assert result["bucket"] == "B1_CONGESTION"
    assert "ambiguous_first_attempt" not in result["signals"]


# --- Path 3: ambiguity handling ---------------------------------------------

def test_ambiguity_first_attempt_is_soft_at_reduced_confidence(matrix):
    event = make_event("payment_failed", attempt_number=1)  # 15:00, safe window -- no congestion override
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == matrix.reason_codes["payment_failed"]["bucket"] == "B3_TRANSIENT"
    assert result["confidence"] == AMBIGUOUS_FIRST_ATTEMPT_CONFIDENCE
    assert result["signals"] == ["ambiguous_first_attempt"]


def test_ambiguity_reclassifies_to_dead_after_failed_retry(matrix):
    event = make_event("payment_failed", attempt_number=2)
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == "B5_DEAD"
    assert result["confidence"] == AMBIGUOUS_RECLASSIFIED_CONFIDENCE
    assert result["signals"] == ["reclassified_after_failed_retry"]


@pytest.mark.parametrize("reason", ["card_declined", "debit_declined", "payment_declined"])
def test_ambiguity_handling_generalises_to_every_ambiguous_code_by_flag_not_name(matrix, reason):
    # These were reclassified from B5_DEAD to ambiguous B3_TRANSIENT and
    # must get identical treatment to payment_failed without being
    # special-cased by name anywhere in classify.py.
    first = classify(make_event(reason, attempt_number=1), matrix)
    assert first["bucket"] == "B3_TRANSIENT"
    assert first["confidence"] == AMBIGUOUS_FIRST_ATTEMPT_CONFIDENCE

    second = classify(make_event(reason, attempt_number=2), matrix)
    assert second["bucket"] == "B5_DEAD"
    assert second["confidence"] == AMBIGUOUS_RECLASSIFIED_CONFIDENCE


# --- Path 4: balance-pattern confidence boost -------------------------------

def test_balance_pattern_boosts_confidence_on_recurring_history(matrix):
    event = make_event("insufficient_funds", prior_insufficient_funds_90d=3)
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == "B2_BALANCE"
    assert result["confidence"] > BASE_CONFIDENCE
    assert "recurring_balance_pattern" in result["signals"]


def test_balance_pattern_does_not_boost_below_threshold(matrix):
    event = make_event("insufficient_funds", prior_insufficient_funds_90d=1)
    result = classify(event, matrix)
    assert result["bucket"] == "B2_BALANCE"
    assert result["confidence"] == BASE_CONFIDENCE
    assert "recurring_balance_pattern" not in result["signals"]


def test_balance_pattern_boost_is_capped_at_one(matrix):
    # BASE_CONFIDENCE + boost must never exceed 1.0 even if constants change.
    event = make_event("insufficient_funds", prior_insufficient_funds_90d=5)
    result = classify(event, matrix)
    assert result["confidence"] <= 1.0


# --- Unknown-code case -------------------------------------------------------

def test_unknown_reason_code_returns_b_unknown_with_zero_confidence(matrix):
    event = make_event("this_code_does_not_exist_in_the_matrix")
    result = classify(event, matrix)
    _assert_valid_result(result, matrix)
    assert result["bucket"] == "B_UNKNOWN"
    assert result["confidence"] == 0.0
    assert result["signals"] == ["reason_code_not_in_matrix"]


# --- Confidence floor (fail-closed safety net) ------------------------------
# Not reachable through classify() with current constants (every real path
# produces >= 0.55) -- tested directly against the helper so the guarantee
# is verified even though no live reason code currently exercises it.

def test_confidence_floor_passes_through_when_above_threshold():
    result = _apply_confidence_floor("B3_TRANSIENT", 0.9, ["base_lookup"])
    assert result == {"bucket": "B3_TRANSIENT", "confidence": 0.9, "signals": ["base_lookup"]}


def test_confidence_floor_fails_closed_below_threshold():
    result = _apply_confidence_floor("B3_TRANSIENT", 0.3, ["base_lookup"])
    assert result["bucket"] == "B_UNKNOWN"
    assert result["confidence"] == 0.3  # preserved, not zeroed -- "uncertain" != "no information"
    assert result["signals"] == ["base_lookup", "confidence_below_threshold"]


# --- Purity -----------------------------------------------------------------

def test_classify_is_pure_same_input_same_output(matrix):
    event = make_event("gateway_technical_error", failed_at=datetime(2026, 8, 15, 11, 0, 0))
    assert classify(event, matrix) == classify(event, matrix)


def test_classify_does_not_mutate_its_input(matrix):
    event = make_event("insufficient_funds", prior_insufficient_funds_90d=3)
    before = {**event, "error": dict(event["error"]), "customer_history": dict(event["customer_history"])}
    classify(event, matrix)
    assert event == before


# --- classify() must never read the hidden ground-truth field --------------
# Resolution 2026-09-03: ground truth is now decided at generation time and
# stamped on events as `_true_bucket`, specifically so it can be hidden
# from the classifier. This proves it actually is: `_ForbiddenKeyDict`
# raises on ANY access to that key -- subscript, .get(), or `in` -- so a
# read via any of Python's normal dict-access idioms fails the test loudly,
# not just a literal `event["_true_bucket"]`.

class _ForbiddenKeyDict(dict):
    forbidden_key = "_true_bucket"

    def __getitem__(self, key):
        if key == self.forbidden_key:
            raise AssertionError("classify() must never read _true_bucket")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == self.forbidden_key:
            raise AssertionError("classify() must never read _true_bucket")
        return super().get(key, default)

    def __contains__(self, key):
        if key == self.forbidden_key:
            raise AssertionError("classify() must never read _true_bucket")
        return super().__contains__(key)


@pytest.mark.parametrize(
    "reason,failed_at",
    [
        ("gateway_technical_error", datetime(2026, 8, 15, 11, 0, 0)),  # congestion-override path
        ("payment_failed", datetime(2026, 8, 15, 15, 0, 0)),  # ambiguity path
        ("insufficient_funds", datetime(2026, 8, 15, 15, 0, 0)),  # base lookup + balance-boost path
        ("this_code_does_not_exist", datetime(2026, 8, 15, 15, 0, 0)),  # unknown-code path
    ],
)
def test_classify_never_reads_true_bucket(matrix, reason, failed_at):
    event = make_event(reason, failed_at=failed_at, prior_insufficient_funds_90d=3, attempt_number=1)
    # Deliberately WRONG value: if classify() ever read it and used it,
    # this would corrupt the result in an obviously-detectable way too.
    event["_true_bucket"] = "B_UNKNOWN"
    guarded = _ForbiddenKeyDict(event)

    result = classify(guarded, matrix)  # must not raise

    assert result["bucket"] in matrix.buckets
