"""The ingestion + decision path shared by the webhook and the batch
simulator, plus the DB-backed mandate state the policy engine's caps read.

Phases 3-6 ran every decision against a FRESH in-memory `mandate_history`
-- honest for what those phases produced (500 independent first failures),
but it meant the attempt cap, cooling-off floor and contact limit had
nothing durable to count against. This module is where those become real:
`MandateState` is loaded from the DB, handed to the policy engine, and
advanced inside the SAME transaction that writes the decision (see
executor.execute_decision's `mandate_state` parameter).
"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.matrix import DecisionMatrix
from app.models import Event, MandateState
from classifier.classify import classify
from executor.executor import execute_decision
from policy.policy import evaluate_policy


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {obj!r}")


def persist_event(db: Session, event: dict, raw_payload: bytes | None = None) -> Event:
    """Append-only raw-event write. `event_id` is the primary key, which
    is idempotency layer 1 (architecture-and-security.md sec. 3.2) --
    enforced by the DB, not by application code. Callers must check for a
    duplicate BEFORE calling this; a second call for the same event_id
    raises IntegrityError, which is the intended behaviour (loud, not
    silent).

    `raw_payload` is stored as received where we have the actual bytes
    (the webhook path). The batch simulator has no wire bytes -- the
    generator's dict IS the event -- so it re-serialises, which is fine
    for a synthetic batch and explicitly NOT fine for signature
    verification (see app/security.py)."""
    payload = (
        raw_payload.decode("utf-8", errors="replace")
        if raw_payload is not None
        else json.dumps({k: v for k, v in event.items() if not k.startswith("_")}, default=_json_default)
    )
    row = Event(
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
        raw_payload=payload,
    )
    db.add(row)
    return row


def load_or_open_mandate_state(db: Session, event: dict) -> MandateState:
    """The mandate's mutable aggregate, created on first sight.

    If a state row exists for a DIFFERENT cycle_id, this failure opens a
    NEW cycle: counters reset, and `prior_cycle_failed` is carried over
    from whether the last cycle ended without recovering -- which is
    exactly what evaluate_policy's repeat-offender rule (rule 3) escalates
    on. Added to the Session but NOT committed here; the decision
    transaction commits it."""
    state = db.get(MandateState, event["mandate_id"])
    if state is None:
        # Every counter is set explicitly rather than left to the column
        # defaults: those are applied at INSERT, so a freshly constructed
        # row still has None in them, and the policy engine reads this
        # object BEFORE it is flushed (`None >= 3` is a TypeError, found
        # the first time the webhook ran end to end).
        state = MandateState(
            mandate_id=event["mandate_id"],
            cycle_id=event["cycle_id"],
            cycle_started_at=event["failed_at"],
            total_retry_attempts=0,
            total_contacts_sent=0,
            last_attempt_at=None,
            status="active",
            prior_cycle_failed=False,
            version=1,
            updated_at=datetime.utcnow(),
        )
        db.add(state)
        return state

    if state.cycle_id != event["cycle_id"]:
        state.prior_cycle_failed = state.status != "recovered"
        state.cycle_id = event["cycle_id"]
        state.cycle_started_at = event["failed_at"]
        state.total_retry_attempts = 0
        state.total_contacts_sent = 0
        state.last_attempt_at = None
        state.status = "active"
        state.version += 1
        state.updated_at = datetime.utcnow()

    return state


def history_from_state(state: MandateState) -> dict:
    """The plain dict evaluate_policy() expects. `b1_congestion_failed_attempts`
    is not a MandateState column: it drives the executor's
    reclassify_after_max_attempts path, which needs per-cycle B1 retry
    history that only the in-process simulation loop tracks. On the
    webhook path each request is a single decision, so 0 is correct and
    not a silent shortcut -- there is no earlier B1 retry in scope."""
    return {
        "cycle_started_at": state.cycle_started_at,
        "total_retry_attempts": state.total_retry_attempts,
        "total_contacts_sent": state.total_contacts_sent,
        "last_attempt_at": state.last_attempt_at,
        "prior_cycle_failed": state.prior_cycle_failed,
        "b1_congestion_failed_attempts": 0,
    }


def decide(db: Session, event: dict, matrix: DecisionMatrix, state: MandateState):
    """classify -> policy -> execute, committing the Decision, any Attempt,
    and the advanced MandateState together in one transaction.

    Returns the committed Decision. No explanation is attached here and
    nothing in this call path can reach the LLM layer -- that is the
    structural guarantee described in explain/explain.py."""
    classification = classify(event, matrix)
    history = history_from_state(state)
    verdict, reasons = evaluate_policy(event, classification, history)
    return execute_decision(db, event, classification, verdict, reasons, history, matrix, mandate_state=state)
