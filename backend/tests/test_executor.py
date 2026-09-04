"""Unit tests for executor.resolve_action / execute_decision -- window
snapping, income-window scheduling, the reclassify_after_max_attempts
path (deferred from M3), transactional DB writes, and the critical
whole-batch invariant: zero scheduled retries in the restricted window.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.matrix import load_decision_matrix
from app.models import Attempt, Decision
from classifier.classify import classify
from executor.executor import (
    HUMAN_QUEUE,
    NUDGE_SENT,
    RETRY_SCHEDULED,
    STOPPED,
    execute_decision,
    resolve_action,
    snap_to_safe_window,
)
from generator.generate import generate_batch
from policy.policy import ALLOW, BLOCK, ESCALATE, evaluate_policy


@pytest.fixture(scope="module")
def matrix():
    return load_decision_matrix()


def make_event(
    reason: str = "gateway_technical_error",
    *,
    failed_at: datetime = datetime(2026, 8, 15, 15, 0, 0),
    amount_inr: int = 499,
    typical_credit_day: int = 15,
    prior_insufficient_funds_90d: int = 0,
) -> dict:
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
            "prior_insufficient_funds_90d": prior_insufficient_funds_90d,
            "typical_credit_day": typical_credit_day,
            "mandate_age_days": 100,
        },
    }


def make_mandate_history(
    *,
    cycle_started_at: datetime = datetime(2026, 8, 15, 9, 0, 0),
    total_retry_attempts: int = 0,
    total_contacts_sent: int = 0,
    last_attempt_at: datetime | None = None,
    prior_cycle_failed: bool = False,
    b1_congestion_failed_attempts: int = 0,
) -> dict:
    return {
        "cycle_started_at": cycle_started_at,
        "total_retry_attempts": total_retry_attempts,
        "total_contacts_sent": total_contacts_sent,
        "last_attempt_at": last_attempt_at,
        "prior_cycle_failed": prior_cycle_failed,
        "b1_congestion_failed_attempts": b1_congestion_failed_attempts,
    }


# --- window snapping ---------------------------------------------------

def test_snap_leaves_safe_times_untouched():
    t = datetime(2026, 8, 15, 15, 0, 0)
    snapped, was_snapped = snap_to_safe_window(t)
    assert snapped == t
    assert was_snapped is False


@pytest.mark.parametrize("hour", [10, 11, 12])
def test_snap_pushes_restricted_times_to_1330(hour):
    t = datetime(2026, 8, 15, hour, 45, 30)
    snapped, was_snapped = snap_to_safe_window(t)
    assert snapped == datetime(2026, 8, 15, 13, 30, 0)
    assert was_snapped is True


def test_snap_boundary_13_is_already_safe():
    t = datetime(2026, 8, 15, 13, 0, 0)  # 13:00 is the end of the restricted band, not inside it
    snapped, was_snapped = snap_to_safe_window(t)
    assert snapped == t
    assert was_snapped is False


# --- income-window scheduling (B2) --------------------------------------

def test_income_window_schedules_one_day_after_typical_credit_day():
    event = make_event("insufficient_funds", failed_at=datetime(2026, 8, 5, 18, 0, 0), typical_credit_day=15)
    classification = classify(event, load_decision_matrix())
    plan = resolve_action(event, classification, ALLOW, ["within_value_threshold"], make_mandate_history(), load_decision_matrix())
    assert plan["action"] == RETRY_SCHEDULED
    # payday = 15th, scheduled 1 day after = 16th, at the original time-of-day (18:00, safe)
    assert plan["scheduled_for"] == datetime(2026, 8, 16, 18, 0, 0)
    assert plan["window_snapped"] is False


def test_income_window_rolls_to_next_month_when_payday_already_passed():
    event = make_event("insufficient_funds", failed_at=datetime(2026, 8, 20, 9, 0, 0), typical_credit_day=5)
    classification = classify(event, load_decision_matrix())
    plan = resolve_action(event, classification, ALLOW, [], make_mandate_history(), load_decision_matrix())
    # payday already passed this month -> rolls to Sept 5th, +1 day = Sept 6th
    assert plan["scheduled_for"].date() == datetime(2026, 9, 6).date()


def test_income_window_snaps_when_the_computed_time_lands_in_restricted_window():
    event = make_event("insufficient_funds", failed_at=datetime(2026, 8, 5, 11, 0, 0), typical_credit_day=15)
    classification = classify(event, load_decision_matrix())
    plan = resolve_action(event, classification, ALLOW, [], make_mandate_history(), load_decision_matrix())
    assert plan["scheduled_for"] == datetime(2026, 8, 16, 13, 30, 0)
    assert plan["window_snapped"] is True


# --- congestion override scheduling (B1), and why snapping matters here --

def test_b1_via_override_schedules_soon_and_snaps_out_of_the_window():
    # Restricted-window failure, gateway_technical_error, no balance history
    # -> classify() applies the congestion override (delay_hours=0), so the
    # naive proposed time is the failure time ITSELF -- still inside the
    # restricted window. This is exactly why snapping matters: the executor
    # must not schedule a retry at t+0h and call it done.
    event = make_event("gateway_technical_error", failed_at=datetime(2026, 8, 15, 11, 0, 0), prior_insufficient_funds_90d=0)
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    assert classification["bucket"] == "B1_CONGESTION"
    plan = resolve_action(event, classification, ALLOW, [], make_mandate_history(), matrix)
    assert plan["action"] == RETRY_SCHEDULED
    assert plan["window_snapped"] is True
    assert plan["scheduled_for"] == datetime(2026, 8, 15, 13, 30, 0)


def test_b1_via_direct_code_uses_its_own_play_delay():
    event = make_event("payment_declined_due_to_high_traffic", failed_at=datetime(2026, 8, 15, 15, 0, 0))
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    assert classification["bucket"] == "B1_CONGESTION"
    plan = resolve_action(event, classification, ALLOW, [], make_mandate_history(), matrix)
    # this code's own play: delay_hours=1
    assert plan["scheduled_for"] == datetime(2026, 8, 15, 16, 0, 0)


# --- reclassify_after_max_attempts (deferred from M3) ----------------------

def test_reclassifies_to_b3_after_2_failed_b1_retries_with_no_balance_history():
    event = make_event("gateway_technical_error", failed_at=datetime(2026, 8, 15, 11, 0, 0), prior_insufficient_funds_90d=0)
    matrix = load_decision_matrix()
    classification = classify(event, matrix)  # fresh classify() always says B1_CONGESTION here -- it has no attempt history
    assert classification["bucket"] == "B1_CONGESTION"

    history = make_mandate_history(b1_congestion_failed_attempts=2)
    plan = resolve_action(event, classification, ALLOW, [], history, matrix)

    assert plan["effective_bucket"] == "B3_TRANSIENT"
    assert plan["effective_confidence"] < classification["confidence"]  # less certain than the original B1 guess
    assert "reclassified_after_2_failed_congestion_retries" in plan["effective_signals"]
    assert plan["action"] == RETRY_SCHEDULED  # B3 is still soft/retryable


def test_reclassifies_to_b2_after_2_failed_b1_retries_with_balance_history():
    event = make_event("gateway_technical_error", failed_at=datetime(2026, 8, 15, 11, 0, 0), prior_insufficient_funds_90d=2, typical_credit_day=20)
    matrix = load_decision_matrix()
    # classify() itself won't call this B1 (balance history disqualifies the override) -- construct
    # the "was B1 last time, has since shown balance-failure history" scenario directly.
    classification = {"bucket": "B1_CONGESTION", "confidence": 0.82, "signals": ["fired_in_restricted_window", "no_balance_failure_history"]}
    history = make_mandate_history(b1_congestion_failed_attempts=2)

    plan = resolve_action(event, classification, ALLOW, [], history, matrix)

    assert plan["effective_bucket"] == "B2_BALANCE"
    assert plan["action"] == RETRY_SCHEDULED
    # income-window scheduling kicked in: 1 day after the 20th
    assert plan["scheduled_for"].date() == datetime(2026, 8, 21).date()


def test_no_reclassification_before_2_failed_b1_attempts():
    event = make_event("payment_declined_due_to_high_traffic", failed_at=datetime(2026, 8, 15, 15, 0, 0))
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    history = make_mandate_history(b1_congestion_failed_attempts=1)  # only 1 so far
    plan = resolve_action(event, classification, ALLOW, [], history, matrix)
    assert plan["effective_bucket"] == "B1_CONGESTION"


# --- verdict -> action mapping, including the B5 nudge exception --------

def test_escalate_verdict_always_yields_human_queue_no_schedule():
    event = make_event("payment_risk_check_failed")
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, ESCALATE, ["risk_flagged", "hard_decline_never_retried"], make_mandate_history(), matrix)
    assert plan["action"] == HUMAN_QUEUE
    assert plan["scheduled_for"] is None


def test_hard_decline_block_still_sends_the_prescribed_nudge():
    event = make_event("card_expired")
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, BLOCK, ["hard_decline_never_retried"], make_mandate_history(), matrix)
    assert plan["action"] == NUDGE_SENT
    assert plan["scheduled_for"] is None


def test_other_block_reasons_stop_entirely_no_nudge():
    event = make_event("gateway_technical_error", failed_at=datetime(2026, 8, 15, 15, 0, 0))
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, BLOCK, ["attempt_cap_exceeded"], make_mandate_history(total_retry_attempts=3), matrix)
    assert plan["action"] == STOPPED


def test_matrix_escalate_human_wins_even_under_an_allow_verdict():
    event = make_event("mandate_amount_exceeded", amount_inr=499)  # low amount -- M4 wouldn't independently escalate this
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, ALLOW, ["within_value_threshold"], make_mandate_history(), matrix)
    assert plan["action"] == HUMAN_QUEUE


def test_human_approved_escalation_with_no_block_reason_proceeds_as_allow():
    """A pure ESCALATE (nothing else independently blocks it) becomes a
    normal RETRY once a human has approved it -- the "normal recovery
    play", no special treatment."""
    event = make_event("gateway_technical_error", amount_inr=20_000)
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, ESCALATE, ["high_value_amount"], make_mandate_history(), matrix, human_approved=True)
    assert plan["action"] == RETRY_SCHEDULED


def test_human_approved_escalation_still_respects_an_independent_block_reason():
    """Approval covers the escalation reason, not an unrelated governance
    stop riding along on the same verdict (worst-wins ESCALATE masked a
    hard-decline BLOCK reason too, here) -- still resolves BLOCK, with the
    existing B5-nudge exception since the matrix action isn't a nudge for
    this code so it falls through to STOPPED."""
    event = make_event("payment_risk_check_failed")
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(
        event, classification, ESCALATE, ["risk_flagged", "hard_decline_never_retried"], make_mandate_history(), matrix, human_approved=True,
    )
    assert plan["action"] == STOPPED


def test_human_approved_skips_matrix_escalate_human_no_infinite_requeue():
    """Once approved, the matrix's own ESCALATE_HUMAN preference (which
    would otherwise route back to HUMAN_QUEUE every time, see
    test_matrix_escalate_human_wins_even_under_an_allow_verdict) is
    skipped -- there's no RETRY/NUDGE play behind this code either, so it
    falls through to STOPPED rather than looping."""
    event = make_event("mandate_amount_exceeded", amount_inr=499)
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, ALLOW, ["within_value_threshold"], make_mandate_history(), matrix, human_approved=True)
    assert plan["action"] == STOPPED


def test_new_mandate_flow_maps_to_stopped():
    event = make_event("mandate_expired")
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    plan = resolve_action(event, classification, BLOCK, ["hard_decline_never_retried"], make_mandate_history(), matrix)
    assert plan["action"] == STOPPED  # NEW_MANDATE_FLOW is not a nudge action, so the B5-nudge exception doesn't apply


# --- transactional DB write ----------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_execute_decision_writes_decision_and_attempt_together(db_session):
    event = make_event("insufficient_funds", failed_at=datetime(2026, 8, 5, 18, 0, 0), typical_credit_day=15)
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    verdict, reasons = evaluate_policy(event, classification, make_mandate_history())

    decision = execute_decision(db_session, event, classification, verdict, reasons, make_mandate_history(), matrix)

    assert db_session.query(Decision).count() == 1
    assert db_session.query(Attempt).count() == 1
    attempt = db_session.query(Attempt).one()
    assert attempt.decision_id == decision.decision_id
    assert attempt.retry_attempt_number == 1
    assert attempt.scheduled_for == decision.scheduled_for


def test_execute_decision_writes_no_attempt_when_not_a_retry(db_session):
    event = make_event("card_expired")
    matrix = load_decision_matrix()
    classification = classify(event, matrix)
    verdict, reasons = evaluate_policy(event, classification, make_mandate_history())

    execute_decision(db_session, event, classification, verdict, reasons, make_mandate_history(), matrix)

    assert db_session.query(Decision).count() == 1
    assert db_session.query(Attempt).count() == 0


# --- purity of resolve_action ----------------------------------------------

def test_resolve_action_is_pure(matrix):
    event = make_event("insufficient_funds")
    classification = classify(event, matrix)
    history = make_mandate_history()
    a = resolve_action(event, classification, ALLOW, [], history, matrix)
    b = resolve_action(event, classification, ALLOW, [], history, matrix)
    assert a == b


# --- CRITICAL TEST: zero scheduled retries in the restricted window, ------
# across the entire generated + classified + policy-evaluated batch.

def test_zero_scheduled_retries_fall_in_restricted_window_across_full_batch(matrix):
    events = generate_batch(500, 42, matrix)
    violations = []

    for event in events:
        classification = classify(event, matrix)
        fresh_history = {
            "cycle_started_at": event["failed_at"],
            "total_retry_attempts": 0,
            "total_contacts_sent": 0,
            "last_attempt_at": None,
            "prior_cycle_failed": False,
            "b1_congestion_failed_attempts": 0,
        }
        verdict, reasons = evaluate_policy(event, classification, fresh_history)
        plan = resolve_action(event, classification, verdict, reasons, fresh_history, matrix)

        if plan["action"] == RETRY_SCHEDULED:
            hour = plan["scheduled_for"].hour
            if 10 <= hour < 13:
                violations.append((event["event_id"], plan["scheduled_for"]))

    assert not violations, f"{len(violations)} scheduled retries fell inside 10:00-13:00: {violations[:5]}"
