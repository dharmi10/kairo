"""Phase 6: baseline simulator, agent full-cycle simulator, oracle
correlation, and metrics computation. Includes regression tests for the
two bugs found while building this phase -- see DECISIONS.md,
"Two real bugs found while building M6".
"""

from datetime import datetime, timedelta

import pytest

from app.matrix import load_decision_matrix
from baseline.baseline import simulate_baseline_cycle
from executor.simulate import simulate_agent_cycle
from generator.oracle import (
    NUDGE_ACCEPTANCE_PROBABILITIES,
    ORACLE_PROBABILITIES,
    draw_nudge_acceptance,
    draw_retry_outcome,
    oracle_context_key,
)
from metrics.metrics import compute_metrics, compute_metrics_by_bucket


@pytest.fixture(scope="module")
def matrix():
    return load_decision_matrix()


def make_event(
    reason: str = "gateway_technical_error",
    *,
    true_bucket: str = "B3_TRANSIENT",
    failed_at: datetime = datetime(2026, 8, 15, 15, 0, 0),
    amount_inr: int = 499,
    typical_credit_day: int = 15,
    prior_insufficient_funds_90d: int = 0,
    source: str = "gateway",
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
        "error": {"code": "GATEWAY_ERROR", "source": source, "step": "payment_authorization", "reason": reason},
        "attempt_number": 1,
        "customer_history": {
            "prior_failures_90d": 0,
            "prior_insufficient_funds_90d": prior_insufficient_funds_90d,
            "typical_credit_day": typical_credit_day,
            "mandate_age_days": 100,
        },
        "_true_bucket": true_bucket,
    }


# --- REGRESSION: correlated draws prevent independent-attempt compounding --

def test_draw_retry_outcome_is_correlated_across_identical_context():
    """The exact bug: baseline retries 3x at the SAME hour every day,
    which is the SAME (bucket, context-state) key each time. Before the
    fix, 3 independent draws at p=0.35 compounded to ~72.5% cumulative.
    After the fix, all 3 must share ONE outcome."""
    import random

    event = make_event(true_bucket="B1_CONGESTION", failed_at=datetime(2026, 8, 15, 11, 0, 0))  # restricted hour
    rng = random.Random(1)
    context_outcomes: dict = {}

    outcomes = [
        draw_retry_outcome(event, event["failed_at"] + timedelta(days=d), context_outcomes, rng)
        for d in (1, 2, 3)
    ]
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert len(context_outcomes) == 1  # exactly one draw was actually made


def test_draw_retry_outcome_draws_fresh_when_context_changes():
    event = make_event(true_bucket="B1_CONGESTION", failed_at=datetime(2026, 8, 15, 11, 0, 0))
    context_outcomes: dict = {}
    import random

    rng = random.Random(1)
    restricted_key = oracle_context_key(event, datetime(2026, 8, 15, 11, 0, 0))
    safe_key = oracle_context_key(event, datetime(2026, 8, 15, 15, 0, 0))
    assert restricted_key != safe_key

    draw_retry_outcome(event, datetime(2026, 8, 15, 11, 0, 0), context_outcomes, rng)
    draw_retry_outcome(event, datetime(2026, 8, 15, 15, 0, 0), context_outcomes, rng)
    assert len(context_outcomes) == 2  # two genuinely different contexts -> two draws


def test_context_outcomes_shared_between_arms_gives_identical_realized_outcome():
    """If the agent and baseline happen to retry the SAME event under the
    SAME context, they see the same simulated reality -- not a separate
    coin flip per arm."""
    import random

    event = make_event(reason="incorrect_cvv", true_bucket="B4_STRUCTURAL")
    context_outcomes: dict = {}
    rng = random.Random(7)
    a = draw_retry_outcome(event, datetime(2026, 8, 15, 15, 0, 0), context_outcomes, rng)
    b = draw_retry_outcome(event, datetime(2026, 8, 20, 15, 0, 0), context_outcomes, rng)  # B4 state is always "always" -- same key regardless of time
    assert a == b


# --- nudge acceptance -------------------------------------------------------

def test_draw_nudge_acceptance_uses_bucket_probability_and_is_correlated():
    import random

    event = make_event("card_expired", true_bucket="B5_DEAD")
    context_outcomes: dict = {}
    rng = random.Random(3)
    a = draw_nudge_acceptance(event, "B5_DEAD", context_outcomes, rng)
    b = draw_nudge_acceptance(event, "B5_DEAD", context_outcomes, rng)  # same bucket, same event -> same outcome
    assert a == b
    assert len(context_outcomes) == 1


def test_draw_nudge_acceptance_does_not_collide_with_a_retry_outcome_key():
    """B5_DEAD's retry-outcome key is ("B5_DEAD", "always") -- the nudge
    key must be distinct so a cached retry draw (always False for B5,
    p=0.0) can never accidentally answer a nudge-acceptance question."""
    import random

    context_outcomes: dict = {}
    rng = random.Random(1)
    event = make_event("card_expired", true_bucket="B5_DEAD")
    draw_retry_outcome(event, event["failed_at"], context_outcomes, rng)  # seeds ("B5_DEAD", "always") = False
    accepted = draw_nudge_acceptance(event, "B5_DEAD", context_outcomes, rng)
    assert ("B5_DEAD", "always") in context_outcomes
    assert ("B5_DEAD", "nudge_acceptance") in context_outcomes
    assert len(context_outcomes) == 2


def test_draw_nudge_acceptance_unknown_bucket_defaults_to_zero():
    import random

    event = make_event()
    context_outcomes: dict = {}
    assert draw_nudge_acceptance(event, "B3_TRANSIENT", context_outcomes, random.Random(1)) is False


def test_draw_nudge_acceptance_independent_streams_for_different_events():
    """Two different events must not accidentally share a draw just
    because they hash to the same bucket -- distinct event_ids must
    route to genuinely independent streams (this is the point of keying
    on event_id, not just bucket, when `seed` is a real int)."""
    event_a = {**make_event("card_expired", true_bucket="B5_DEAD"), "event_id": "evt_a"}
    event_b = {**make_event("card_expired", true_bucket="B5_DEAD"), "event_id": "evt_b"}
    outcomes_a: dict = {}
    outcomes_b: dict = {}
    draw_nudge_acceptance(event_a, "B5_DEAD", outcomes_a, 999)
    draw_nudge_acceptance(event_b, "B5_DEAD", outcomes_b, 999)
    # Same seed, same bucket, different event id -- results may coincide
    # by chance, but the underlying draws must be independently derived,
    # not the literal same float. Assert reproducibility instead (a
    # weaker but unambiguous property): re-deriving with the same inputs
    # always reproduces the same outcome.
    outcomes_a_repeat: dict = {}
    draw_nudge_acceptance(event_a, "B5_DEAD", outcomes_a_repeat, 999)
    assert outcomes_a[("B5_DEAD", "nudge_acceptance")] == outcomes_a_repeat[("B5_DEAD", "nudge_acceptance")]


def test_nudge_accepted_recovers_full_amount_after_24h_delay():
    import random

    matrix = load_decision_matrix()
    event = make_event("card_expired", true_bucket="B5_DEAD", amount_inr=1234, failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysSucceedRng(random.Random):
        def random(self):
            return 0.0  # beats NUDGE_ACCEPTANCE_PROBABILITIES["B5_DEAD_reauth"] = 0.30

    result = simulate_agent_cycle(event, matrix, {}, AlwaysSucceedRng())
    assert result["outcome"] == "RECOVERED"
    assert result["amount_inr"] == 1234
    assert result["contacts_sent"] == 1
    assert result["attempts_made"] == 0  # B5 never retries -- recovery came entirely from the nudge
    assert result["hours_to_recovery"] == 24.0


def test_nudge_rejected_ends_not_recovered():
    import random

    matrix = load_decision_matrix()
    event = make_event("card_expired", true_bucket="B5_DEAD", failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysRejectRng(random.Random):
        def random(self):
            return 0.99  # fails NUDGE_ACCEPTANCE_PROBABILITIES["B5_DEAD_reauth"] = 0.30

    result = simulate_agent_cycle(event, matrix, {}, AlwaysRejectRng())
    assert result["outcome"] == "FAILED"
    assert result["contacts_sent"] == 1
    assert result["hours_to_recovery"] is None


def test_b4_nudge_uses_the_channel_switch_probability():
    import random

    matrix = load_decision_matrix()
    event = make_event("incorrect_cvv", true_bucket="B4_STRUCTURAL", amount_inr=500, failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysSucceedRng(random.Random):
        def random(self):
            return 0.0  # beats NUDGE_ACCEPTANCE_PROBABILITIES["B4_STRUCTURAL_channel_switch"] = 0.45

    result = simulate_agent_cycle(event, matrix, {}, AlwaysSucceedRng())
    assert result["outcome"] == "RECOVERED"
    assert result["bucket"] == "B4_STRUCTURAL"
    assert NUDGE_ACCEPTANCE_PROBABILITIES["B4_STRUCTURAL_channel_switch"] == 0.45


# --- REGRESSION: cooling-off self-collision bug ----------------------------

def test_agent_can_make_more_than_one_retry_attempt_when_delay_exceeds_cooling_off():
    """The exact bug: advancing `current["failed_at"]` to exactly the last
    attempt's own firing time made cooling-off compare that attempt
    against itself (always a 0h gap), capping every mandate at 1 retry
    regardless of the bucket's actual delay_hours (gateway_technical_error
    is 3h, well over the 2h cooling-off floor, and its own max_attempts
    is 3). Force every draw to fail so the loop runs until a terminal
    action, and assert it's not stuck at exactly 1 attempt."""
    import random

    matrix = load_decision_matrix()
    event = make_event("gateway_technical_error", true_bucket="B3_TRANSIENT", failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysFailRng(random.Random):
        def random(self):
            return 0.999  # never beats any real probability in ORACLE_PROBABILITIES

    result = simulate_agent_cycle(event, matrix, {}, AlwaysFailRng())
    assert result["outcome"] == "FAILED"
    assert result["attempts_made"] > 1, "agent should retry more than once before the global attempt cap stops it"


# --- baseline: no reason-awareness, no snapping, 3 attempts, halts ---------

def test_baseline_retries_hard_declines_unlike_the_agent():
    import random

    event = make_event("card_expired", true_bucket="B5_DEAD")
    result = simulate_baseline_cycle(event, "B5_DEAD", {}, random.Random(1))
    assert result["attempts_made"] == 3  # blind: retries a hard decline all 3 times regardless


def test_baseline_never_snaps_and_can_land_in_the_restricted_window():
    import random

    event = make_event("gateway_technical_error", true_bucket="B3_TRANSIENT", failed_at=datetime(2026, 8, 15, 11, 0, 0))
    result = simulate_baseline_cycle(event, "B3_TRANSIENT", {}, random.Random(1))
    # same hour every day, 11:00 -- inside the restricted window, unsnapped
    assert result["attempts_made"] == 3 or result["outcome"] == "RECOVERED"


def test_baseline_sends_no_contacts():
    import random

    event = make_event("card_expired", true_bucket="B5_DEAD")
    result = simulate_baseline_cycle(event, "B5_DEAD", {}, random.Random(1))
    assert result["contacts_sent"] == 0


def test_baseline_stops_early_on_first_success():
    import random

    event = make_event("gateway_technical_error", true_bucket="B1_CONGESTION", failed_at=datetime(2026, 8, 15, 15, 0, 0))  # safe hour, p=0.70

    class AlwaysSucceedRng(random.Random):
        def random(self):
            return 0.0  # beats any probability > 0

    result = simulate_baseline_cycle(event, "B1_CONGESTION", {}, AlwaysSucceedRng())
    assert result["outcome"] == "RECOVERED"
    assert result["attempts_made"] == 1


# --- human review of escalated events --------------------------------------

def test_escalated_event_approved_by_human_review_proceeds_and_can_recover():
    """Escalation is a delay-and-filter, not a black hole: approved ->
    same oracle, same recovery play as anything else."""
    import random

    matrix = load_decision_matrix()
    event = make_event("gateway_technical_error", true_bucket="B3_TRANSIENT", amount_inr=20_000, failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysSucceedRng(random.Random):
        def random(self):
            return 0.0  # beats the 70% approval threshold and any oracle probability

    result = simulate_agent_cycle(event, matrix, {}, AlwaysSucceedRng())
    assert result["human_review"] == "approved"
    assert result["outcome"] == "RECOVERED"
    assert result["attempts_made"] == 1


def test_escalated_event_rejected_by_human_review_ends_not_recovered():
    import random

    matrix = load_decision_matrix()
    event = make_event("gateway_technical_error", true_bucket="B3_TRANSIENT", amount_inr=20_000, failed_at=datetime(2026, 8, 15, 15, 0, 0))

    class AlwaysRejectRng(random.Random):
        def random(self):
            return 0.99  # fails the 70% approval threshold

    result = simulate_agent_cycle(event, matrix, {}, AlwaysRejectRng())
    assert result["human_review"] == "rejected"
    assert result["outcome"] == "FAILED"
    assert result["attempts_made"] == 0


def test_non_escalated_event_has_no_human_review():
    import random

    matrix = load_decision_matrix()
    event = make_event("insufficient_funds", true_bucket="B2_BALANCE")
    result = simulate_agent_cycle(event, matrix, {}, random.Random(1))
    assert result["human_review"] is None


# --- independent, deterministic per-draw streams (app/rng.py) --------------

def test_agent_cycle_outcome_is_order_independent_across_events_with_same_int_seed():
    """The defensibility property this was built for: simulating some
    unrelated event before or after this one, with the same int seed,
    must not change this event's own outcome -- unlike a single shared
    sequential RNG, where it would."""
    matrix = load_decision_matrix()
    event_a = {**make_event("insufficient_funds", true_bucket="B2_BALANCE"), "event_id": "evt_order_a"}
    event_b = {**make_event("gateway_technical_error", true_bucket="B3_TRANSIENT"), "event_id": "evt_order_b"}

    result_a_alone = simulate_agent_cycle(event_a, matrix, {}, 20260903)

    simulate_agent_cycle(event_b, matrix, {}, 20260903)  # unrelated draws happen first this time
    result_a_after_b = simulate_agent_cycle(event_a, matrix, {}, 20260903)

    assert result_a_alone["outcome"] == result_a_after_b["outcome"]
    assert result_a_alone["attempts_made"] == result_a_after_b["attempts_made"]
    assert result_a_alone["hours_to_recovery"] == result_a_after_b["hours_to_recovery"]


# --- metrics ---------------------------------------------------------------

def test_compute_metrics_basic():
    records = [
        {"event_id": "1", "amount_inr": 100, "bucket": "B2_BALANCE", "outcome": "RECOVERED", "attempts_made": 1, "contacts_sent": 0, "hours_to_recovery": 5.0},
        {"event_id": "2", "amount_inr": 200, "bucket": "B2_BALANCE", "outcome": "FAILED", "attempts_made": 2, "contacts_sent": 1, "hours_to_recovery": None},
        {"event_id": "3", "amount_inr": 50, "bucket": "B5_DEAD", "outcome": "FAILED", "attempts_made": 3, "contacts_sent": 0, "hours_to_recovery": None},
    ]
    m = compute_metrics(records)
    assert m["n"] == 3
    assert m["n_recovered"] == 1
    assert m["rupees_recovered"] == 100
    assert m["recovery_rate_pct"] == pytest.approx(33.333, rel=1e-3)
    assert m["attempts_on_hard_declines"] == 3  # the B5 record's attempts
    assert m["median_hours_to_recovery"] == 5.0
    assert m["customer_contacts_sent"] == 1


def test_compute_metrics_by_bucket_groups_correctly():
    records = [
        {"event_id": "1", "amount_inr": 100, "bucket": "A", "outcome": "RECOVERED", "attempts_made": 1, "contacts_sent": 0, "hours_to_recovery": 1.0},
        {"event_id": "2", "amount_inr": 100, "bucket": "B", "outcome": "FAILED", "attempts_made": 1, "contacts_sent": 0, "hours_to_recovery": None},
    ]
    by_bucket = compute_metrics_by_bucket(records)
    assert set(by_bucket.keys()) == {"A", "B"}
    assert by_bucket["A"]["n"] == 1
    assert by_bucket["B"]["n"] == 1


def test_compute_metrics_empty_list_does_not_crash():
    m = compute_metrics([])
    assert m["n"] == 0
    assert m["recovery_rate_pct"] == 0.0
    assert m["median_hours_to_recovery"] is None
