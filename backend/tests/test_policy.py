"""Unit tests for policy.evaluate_policy -- one test per rule in PRD sec.
M4, plus the fail-closed data-quality gate, worst-wins verdict
resolution, and the three critical properties requested explicitly:
  1. A B5 hard decline is NEVER ALLOW, across >=2000 randomised inputs.
  2. Attempt caps hold across a full simulated 7-day cycle.
  3. Cooling-off is enforced even when attempts are under the cap.
"""

import random
from datetime import datetime, timedelta

import pytest

from app.config import settings
from policy.policy import ALLOW, BLOCK, ESCALATE, evaluate_policy


def make_event(reason: str = "gateway_technical_error", *, failed_at: datetime = datetime(2026, 8, 15, 15, 0, 0), amount_inr: int = 499) -> dict:
    return {
        "event_id": "evt_test",
        "payment_id": "pay_test",
        "mandate_id": "mandate_test",
        "customer_id": "cust_test",
        "merchant_category": "OTT",
        "amount_inr": amount_inr,
        "failed_at": failed_at,
        "cycle_id": "mandate_test_cycle1",
        "error": {"code": "GATEWAY_ERROR", "source": "gateway", "step": "payment_authorization", "reason": reason},
        "attempt_number": 1,
        "customer_history": {
            "prior_failures_90d": 0,
            "prior_insufficient_funds_90d": 0,
            "typical_credit_day": 15,
            "mandate_age_days": 100,
        },
    }


def make_classification(bucket: str = "B3_TRANSIENT", confidence: float = 0.9) -> dict:
    return {"bucket": bucket, "confidence": confidence, "signals": ["base_lookup"]}


def make_mandate_history(
    *,
    cycle_started_at: datetime = datetime(2026, 8, 15, 9, 0, 0),
    total_retry_attempts: int = 0,
    total_contacts_sent: int = 0,
    last_attempt_at: datetime | None = None,
    prior_cycle_failed: bool = False,
) -> dict:
    return {
        "cycle_started_at": cycle_started_at,
        "total_retry_attempts": total_retry_attempts,
        "total_contacts_sent": total_contacts_sent,
        "last_attempt_at": last_attempt_at,
        "prior_cycle_failed": prior_cycle_failed,
    }


# --- baseline ----------------------------------------------------------

def test_allow_when_every_rule_passes():
    verdict, reasons = evaluate_policy(make_event(), make_classification(), make_mandate_history())
    assert verdict == ALLOW
    assert reasons == [
        "classification_confident",
        "not_risk_flagged",
        "not_repeat_offender",
        "within_value_threshold",
        "not_hard_decline",
        "within_recovery_cycle",
        "within_attempt_cap",
        "cooling_off_satisfied",
        "within_contact_limit",
    ]


# --- one test per rule ---------------------------------------------------

def test_unknown_bucket_escalates():
    verdict, reasons = evaluate_policy(make_event(), make_classification(bucket="B_UNKNOWN", confidence=0.0), make_mandate_history())
    assert verdict == ESCALATE
    assert "unclassified_or_low_confidence" in reasons


def test_low_confidence_escalates_even_with_a_known_bucket():
    verdict, reasons = evaluate_policy(make_event(), make_classification(bucket="B3_TRANSIENT", confidence=0.3), make_mandate_history())
    assert verdict == ESCALATE
    assert "unclassified_or_low_confidence" in reasons


def test_confidence_exactly_at_threshold_does_not_escalate():
    verdict, _ = evaluate_policy(make_event(), make_classification(bucket="B3_TRANSIENT", confidence=settings.unknown_bucket_confidence_threshold), make_mandate_history())
    assert verdict == ALLOW


def test_hard_decline_blocks_not_escalates_on_its_own():
    verdict, reasons = evaluate_policy(make_event("card_expired"), make_classification(bucket="B5_DEAD"), make_mandate_history())
    assert verdict == BLOCK
    assert "hard_decline_never_retried" in reasons


def test_risk_flagged_escalates_even_though_its_own_bucket_is_b5():
    # payment_risk_check_failed is itself bucketed B5_DEAD -- both rule 2
    # (risk) and rule 5 (hard decline) fire. Worst-wins must produce
    # ESCALATE, not BLOCK, and both reasons must be present.
    verdict, reasons = evaluate_policy(make_event("payment_risk_check_failed"), make_classification(bucket="B5_DEAD"), make_mandate_history())
    assert verdict == ESCALATE
    assert "risk_flagged" in reasons
    assert "hard_decline_never_retried" in reasons


def test_repeat_offender_escalates():
    verdict, reasons = evaluate_policy(make_event(), make_classification(), make_mandate_history(prior_cycle_failed=True))
    assert verdict == ESCALATE
    assert "repeat_offender" in reasons


def test_high_value_escalates_above_threshold():
    verdict, reasons = evaluate_policy(make_event(amount_inr=settings.high_value_threshold_inr + 1), make_classification(), make_mandate_history())
    assert verdict == ESCALATE
    assert "high_value_amount" in reasons


def test_exactly_at_value_threshold_does_not_escalate():
    # PRD: "amount > threshold" -- strictly greater than.
    verdict, reasons = evaluate_policy(make_event(amount_inr=settings.high_value_threshold_inr), make_classification(), make_mandate_history())
    assert verdict == ALLOW
    assert "within_value_threshold" in reasons


def test_recovery_cycle_expired_blocks():
    cycle_started_at = datetime(2026, 8, 1, 9, 0, 0)
    failed_at = cycle_started_at + timedelta(days=8)
    verdict, reasons = evaluate_policy(make_event(failed_at=failed_at), make_classification(), make_mandate_history(cycle_started_at=cycle_started_at))
    assert verdict == BLOCK
    assert "recovery_cycle_expired" in reasons


def test_recovery_cycle_exactly_at_boundary_does_not_expire():
    cycle_started_at = datetime(2026, 8, 1, 9, 0, 0)
    failed_at = cycle_started_at + timedelta(days=7)  # exactly 7 days -- not > 7
    verdict, _ = evaluate_policy(make_event(failed_at=failed_at), make_classification(), make_mandate_history(cycle_started_at=cycle_started_at))
    assert verdict == ALLOW


def test_attempt_cap_blocks_at_threshold():
    verdict, reasons = evaluate_policy(make_event(), make_classification(), make_mandate_history(total_retry_attempts=3))
    assert verdict == BLOCK
    assert "attempt_cap_exceeded" in reasons


def test_attempt_cap_allows_just_under_threshold():
    verdict, _ = evaluate_policy(make_event(), make_classification(), make_mandate_history(total_retry_attempts=2))
    assert verdict == ALLOW


def test_cooling_off_blocks_when_violated():
    failed_at = datetime(2026, 8, 15, 15, 0, 0)
    last_attempt_at = failed_at - timedelta(minutes=30)  # < 2h
    verdict, reasons = evaluate_policy(make_event(failed_at=failed_at), make_classification(), make_mandate_history(total_retry_attempts=1, last_attempt_at=last_attempt_at))
    assert verdict == BLOCK
    assert "cooling_off_not_satisfied" in reasons


def test_cooling_off_satisfied_when_no_prior_attempt():
    verdict, reasons = evaluate_policy(make_event(), make_classification(), make_mandate_history(last_attempt_at=None))
    assert verdict == ALLOW
    assert "cooling_off_satisfied" in reasons


def test_max_contacts_blocks_at_threshold():
    verdict, reasons = evaluate_policy(make_event(), make_classification(), make_mandate_history(total_contacts_sent=2))
    assert verdict == BLOCK
    assert "max_contacts_reached" in reasons


def test_max_contacts_allows_just_under_threshold():
    verdict, _ = evaluate_policy(make_event(), make_classification(), make_mandate_history(total_contacts_sent=1))
    assert verdict == ALLOW


# --- worst-wins verdict resolution ----------------------------------------

def test_worst_wins_when_block_and_escalate_both_fire():
    # B5_DEAD fires BLOCK (rule 5); amount > threshold fires ESCALATE (rule 4).
    # Final verdict must be the worse of the two, ESCALATE.
    verdict, reasons = evaluate_policy(make_event(amount_inr=settings.high_value_threshold_inr + 1000), make_classification(bucket="B5_DEAD"), make_mandate_history())
    assert verdict == ESCALATE
    assert "high_value_amount" in reasons
    assert "hard_decline_never_retried" in reasons


# --- purity ----------------------------------------------------------------

def test_evaluate_policy_is_pure():
    event, classification, history = make_event(), make_classification(), make_mandate_history()
    assert evaluate_policy(event, classification, history) == evaluate_policy(event, classification, history)


def test_evaluate_policy_does_not_mutate_inputs():
    event, classification, history = make_event(), make_classification(), make_mandate_history()
    event_before, classification_before, history_before = (
        {**event, "error": dict(event["error"]), "customer_history": dict(event["customer_history"])},
        dict(classification),
        dict(history),
    )
    evaluate_policy(event, classification, history)
    assert event == event_before
    assert classification == classification_before
    assert history == history_before


# --- CRITICAL TEST 1: a B5 hard decline is NEVER ALLOW ---------------------
# Property-based: fix bucket=B5_DEAD, randomise everything else broadly
# (including values that ALSO trigger other ESCALATE/BLOCK rules, and
# extreme edge values), across >=2000 combinations. Seeded for
# reproducibility -- the property should hold for any input, but a fixed
# seed keeps a failure reproducible rather than flaky.

REASON_CODES_FOR_FUZZING = [
    "gateway_technical_error", "payment_failed", "card_expired", "debit_instrument_blocked",
    "payment_risk_check_failed", "insufficient_funds", "incorrect_otp", "card_declined",
]


def test_hard_decline_never_allow_across_2000_random_combinations():
    rng = random.Random(20260903)
    n = 2000
    violations = []

    for i in range(n):
        failed_at = datetime(2026, 8, 1, 0, 0, 0) + timedelta(hours=rng.randint(0, 24 * 60))
        event = make_event(
            reason=rng.choice(REASON_CODES_FOR_FUZZING),
            failed_at=failed_at,
            amount_inr=rng.randint(1, 200_000),
        )
        classification = make_classification(bucket="B5_DEAD", confidence=rng.uniform(0.0, 1.0))

        has_prior_attempt = rng.random() < 0.7
        last_attempt_at = failed_at - timedelta(hours=rng.uniform(0, 500)) if has_prior_attempt else None
        mandate_history = make_mandate_history(
            cycle_started_at=failed_at - timedelta(hours=rng.uniform(0, 400)),
            total_retry_attempts=rng.randint(0, 10),
            total_contacts_sent=rng.randint(0, 10),
            last_attempt_at=last_attempt_at,
            prior_cycle_failed=rng.random() < 0.5,
        )

        verdict, reasons = evaluate_policy(event, classification, mandate_history)
        if verdict == ALLOW:
            violations.append((i, event, classification, mandate_history, verdict, reasons))

    assert not violations, f"{len(violations)} / {n} B5_DEAD cases were ALLOWed -- e.g. {violations[0]}"


# --- CRITICAL TEST 2: attempt caps hold across a full 7-day cycle ---------

def test_attempt_cap_holds_across_full_simulated_7_day_cycle():
    cycle_started_at = datetime(2026, 8, 1, 9, 0, 0)
    classification = make_classification(bucket="B3_TRANSIENT")
    mandate_history = make_mandate_history(cycle_started_at=cycle_started_at)

    # Attempts spaced 12h apart -- well clear of the 2h cooling-off, so
    # this isolates the attempt-cap check specifically, across the full
    # 7-day recovery window.
    allowed_count = 0
    for hour_offset in range(0, 7 * 24, 12):
        failed_at = cycle_started_at + timedelta(hours=hour_offset)
        event = make_event(failed_at=failed_at)
        verdict, reasons = evaluate_policy(event, classification, mandate_history)

        if mandate_history["total_retry_attempts"] < settings.global_max_retry_attempts:
            assert verdict == ALLOW, f"expected ALLOW at attempt {mandate_history['total_retry_attempts']} (hour {hour_offset}), got {verdict} {reasons}"
            allowed_count += 1
            mandate_history = {
                **mandate_history,
                "total_retry_attempts": mandate_history["total_retry_attempts"] + 1,
                "last_attempt_at": failed_at,
            }
        else:
            assert verdict != ALLOW, f"attempt cap breached at hour {hour_offset}: {verdict} {reasons}"
            assert "attempt_cap_exceeded" in reasons

    assert allowed_count == settings.global_max_retry_attempts


# --- CRITICAL TEST 3: cooling-off enforced even under the attempt cap ----

def test_cooling_off_enforced_even_when_well_under_attempt_cap():
    cycle_started_at = datetime(2026, 8, 15, 9, 0, 0)
    failed_at = datetime(2026, 8, 15, 15, 0, 0)
    classification = make_classification(bucket="B3_TRANSIENT")

    for minutes_since_last_attempt in [1, 30, 60, 90, 119]:
        mandate_history = make_mandate_history(
            cycle_started_at=cycle_started_at,
            total_retry_attempts=1,  # nowhere near the cap of 3
            last_attempt_at=failed_at - timedelta(minutes=minutes_since_last_attempt),
        )
        verdict, reasons = evaluate_policy(make_event(failed_at=failed_at), classification, mandate_history)
        assert verdict != ALLOW, f"cooling-off not enforced at {minutes_since_last_attempt}min under cap"
        assert "cooling_off_not_satisfied" in reasons

    # and confirm it releases right at the 2h boundary
    mandate_history = make_mandate_history(cycle_started_at=cycle_started_at, total_retry_attempts=1, last_attempt_at=failed_at - timedelta(hours=2))
    verdict, reasons = evaluate_policy(make_event(failed_at=failed_at), classification, mandate_history)
    assert verdict == ALLOW
    assert "cooling_off_satisfied" in reasons
