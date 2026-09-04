"""Run the full pipeline -- generate -> classify -> policy -> execute --
over a batch and report outcome counts. Run as: python -m executor.pipeline

Each event gets a FRESH mandate_history (matching policy/report.py's
convention): 500 independent first failures, not a retry sequence, since
that's the honest state of what M2 actually produces. This means
reclassify_after_max_attempts (which needs 2 PRIOR failed B1 retries)
cannot fire in this pass -- same situation Phase 3's ambiguity path and
Phase 4's attempt-cap rule were in, addressed the same way: a dedicated
demonstration alongside the batch run, not silently absent.

Uses a fresh in-memory SQLite DB for each run (not the shared dev
kairo.db) so the reported counts are always exactly what this run
produced, not accumulated across runs.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.matrix import load_decision_matrix
from app.models import Attempt, Decision, Event
from classifier.classify import classify
from executor.executor import RETRY_SCHEDULED, execute_decision, resolve_action
from generator.generate import generate_batch
from policy.policy import evaluate_policy


def fresh_mandate_history(event: dict) -> dict:
    return {
        "cycle_started_at": event["failed_at"],
        "total_retry_attempts": 0,
        "total_contacts_sent": 0,
        "last_attempt_at": None,
        "prior_cycle_failed": False,
        "b1_congestion_failed_attempts": 0,
    }


def persist_event(db, event: dict) -> None:
    db.add(
        Event(
            event_id=event["event_id"],
            payment_id=event["payment_id"],
            mandate_id=event["mandate_id"],
            customer_id=event["customer_id"],
            merchant_category=event["merchant_category"],
            amount_inr=event["amount_inr"],
            failed_at=event["failed_at"],
            cycle_id=event["cycle_id"],
            error_code=event["error"]["code"],
            error_source=event["error"]["source"],
            error_step=event["error"]["step"],
            error_reason=event["error"]["reason"],
            attempt_number=event["attempt_number"],
            prior_failures_90d=event["customer_history"]["prior_failures_90d"],
            prior_insufficient_funds_90d=event["customer_history"]["prior_insufficient_funds_90d"],
            typical_credit_day=event["customer_history"]["typical_credit_day"],
            mandate_age_days=event["customer_history"]["mandate_age_days"],
            raw_payload="{}",  # the generator's dict IS the payload; not re-serialised here for this demo run
        )
    )
    db.commit()


def demonstrate_reclassification(matrix) -> None:
    """The batch run above can't exercise reclassify_after_max_attempts
    (needs 2 prior failed B1 retries, which fresh mandate_history never
    has). Prove it fires, the same way Phase 3/4 proved their
    history-dependent paths fired: a small hand-built scenario."""
    event = {
        "event_id": "evt_demo_reclass",
        "payment_id": "pay_demo",
        "mandate_id": "mandate_demo",
        "customer_id": "cust_demo",
        "merchant_category": "OTT",
        "amount_inr": 499,
        "failed_at": datetime(2026, 8, 15, 11, 0, 0),
        "cycle_id": "mandate_demo_cycle1",
        "error": {"code": "GATEWAY_ERROR", "source": "gateway", "step": "payment_authorization", "reason": "gateway_technical_error"},
        "attempt_number": 1,
        "customer_history": {"prior_failures_90d": 2, "prior_insufficient_funds_90d": 0, "typical_credit_day": 20, "mandate_age_days": 200},
    }
    classification = classify(event, matrix)
    history_after_2_failed_b1_retries = {
        "cycle_started_at": datetime(2026, 8, 10, 9, 0, 0),
        "total_retry_attempts": 2,
        "total_contacts_sent": 0,
        "last_attempt_at": datetime(2026, 8, 14, 15, 30, 0),  # well over 2h before this event's failed_at
        "prior_cycle_failed": False,
        "b1_congestion_failed_attempts": 2,
    }
    verdict, reasons = evaluate_policy(event, classification, history_after_2_failed_b1_retries)
    plan = resolve_action(event, classification, verdict, reasons, history_after_2_failed_b1_retries, matrix)

    print("\nreclassify_after_max_attempts demonstration (not exercised by the fresh-history batch above):")
    print(f"  fresh classify() says:  bucket={classification['bucket']}  confidence={classification['confidence']}")
    print(f"  after 2 failed B1 retries, executor's effective bucket: {plan['effective_bucket']}  confidence={plan['effective_confidence']}")
    print(f"  action: {plan['action']}, scheduled_for: {plan['scheduled_for']}")


def main() -> None:
    matrix = load_decision_matrix()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    events = generate_batch(500, 42, matrix)

    action_counts = Counter()
    verdict_counts = Counter()
    window_snapped_count = 0
    scheduled_hours_in_restricted_window = 0

    for event in events:
        persist_event(db, event)
        classification = classify(event, matrix)
        history = fresh_mandate_history(event)
        verdict, reasons = evaluate_policy(event, classification, history)
        decision = execute_decision(db, event, classification, verdict, reasons, history, matrix)

        action_counts[decision.action] += 1
        verdict_counts[verdict] += 1
        if decision.window_snapped:
            window_snapped_count += 1
        if decision.scheduled_for is not None and 10 <= decision.scheduled_for.hour < 13:
            scheduled_hours_in_restricted_window += 1

    total = len(events)
    n_decisions = db.query(Decision).count()
    n_attempts = db.query(Attempt).count()
    n_events = db.query(Event).count()

    print(f"Pipeline run: generate -> classify -> policy -> execute (n={total}, seed=42)")
    print("=" * 70)
    print(f"DB rows written: events={n_events}  decisions={n_decisions}  attempts={n_attempts}")

    print("\nOutcome (action) counts:")
    for action, n in action_counts.most_common():
        print(f"  {action:<16} {n:>4}   {100*n/total:5.1f}%")

    print("\nPolicy verdict counts:")
    for verdict, n in verdict_counts.most_common():
        print(f"  {verdict:<10} {n:>4}   {100*n/total:5.1f}%")

    print(f"\nWindow-snapped retries: {window_snapped_count} / {n_attempts} scheduled attempts")
    print(f"Scheduled retries still landing in the restricted window: {scheduled_hours_in_restricted_window}  (must be 0)")

    demonstrate_reclassification(matrix)

    db.close()


if __name__ == "__main__":
    main()
