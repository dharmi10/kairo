"""`POST /simulate/run`: the whole batch, both arms, persisted.

The metrics and the audit records come from ONE execution of each event's
recovery cycle, not two. `simulate_agent_cycle` now returns the decisions
it took (Phase 7) alongside the outcome record it always returned, so the
Decision rows written here and the numbers in `metrics.compute_metrics`
are by construction the same run -- there is no second replay that could
drift from the first.

Explanations are attached at the very end, after every decision is
committed, and the whole call is guarded: if M7 fails outright, the run
still returns and every decision is intact with a NULL explanation.
"""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.ingest import load_or_open_mandate_state, persist_event
from app.matrix import DecisionMatrix
from app.models import Attempt, Decision, Event, MandateState, SimulationRun
from baseline.baseline import simulate_baseline_cycle
from executor.executor import NUDGE_SENT, RETRY_SCHEDULED
from executor.simulate import simulate_agent_cycle
from generator.generate import generate_batch
from metrics.metrics import compute_metrics, compute_metrics_by_bucket

SIMULATION_TABLES = (Attempt, Decision, MandateState, Event, SimulationRun)


def clear_all(db: Session) -> dict[str, int]:
    """Wipe every table, in FK-safe order (attempts -> decisions -> events).

    The audit log is append-only WITHIN a run; a run is the unit of
    reset. Anything else would mean `/results/summary` mixing two batches'
    numbers, which is a worse property than a resettable demo DB."""
    counts = {}
    for model in SIMULATION_TABLES:
        counts[model.__tablename__] = db.query(model).delete()
    db.commit()
    return counts


def _persist_cycle(db: Session, event: dict, agent_result: dict, matrix: DecisionMatrix) -> list[Decision]:
    """Write the full audit trail for one event's recovery cycle: one
    Decision per action the agent took, plus an Attempt row for each
    scheduled retry. Ordering is the cycle's own order, so
    `dec_<event>_0` is always the first decision taken."""
    rows: list[Decision] = []
    for index, record in enumerate(agent_result["decisions"]):
        plan = record["plan"]
        decision = Decision(
            decision_id=f"dec_{event['event_id']}_{index}",
            event_id=event["event_id"],
            classified_bucket=plan["effective_bucket"],
            confidence=plan["effective_confidence"],
            signals=plan["effective_signals"],
            policy_verdict=record["policy_verdict"],
            policy_reasons=record["policy_reasons"],
            action=plan["action"],
            scheduled_for=plan["scheduled_for"],
            window_snapped=plan["window_snapped"],
            explanation=None,  # M7 runs after every decision here is committed
            explanation_source=None,
            outcome=record["outcome"],
            amount_recovered_inr=record["amount_recovered_inr"],
            engine_version=settings.engine_version,
            matrix_version=matrix.matrix_version,
            decided_at=record["decided_at"],
        )
        db.add(decision)
        rows.append(decision)

        if plan["action"] == RETRY_SCHEDULED:
            db.add(
                Attempt(
                    attempt_id=f"att_{event['event_id']}_{index}",
                    decision_id=decision.decision_id,
                    mandate_id=event["mandate_id"],
                    cycle_id=event["cycle_id"],
                    retry_attempt_number=record["retry_attempt_number"],
                    scheduled_for=plan["scheduled_for"],
                    executed_at=plan["scheduled_for"],  # simulated clock -- nothing sleeps
                    result=record["outcome"],
                )
            )
    return rows


def _final_state(state: MandateState, agent_result: dict) -> None:
    state.total_retry_attempts = agent_result["attempts_made"]
    state.total_contacts_sent = agent_result["contacts_sent"]
    state.status = "recovered" if agent_result["outcome"] == "RECOVERED" else "stopped"
    for record in reversed(agent_result["decisions"]):
        if record["plan"]["action"] in (RETRY_SCHEDULED, NUDGE_SENT):
            state.last_attempt_at = record["plan"]["scheduled_for"] or record["decided_at"]
            break
    state.version += 1
    state.updated_at = datetime.utcnow()


def run_simulation(
    db: Session,
    matrix: DecisionMatrix,
    count: int,
    batch_seed: int,
    sim_seed: int,
) -> tuple[SimulationRun, list[Decision]]:
    """Returns the committed run row and every Decision it wrote. The
    caller attaches explanations afterwards -- deliberately not done here,
    so that the function that produces decisions has no dependency on the
    one that narrates them."""
    clear_all(db)

    events = generate_batch(count, batch_seed, matrix)
    agent_records: list[dict] = []
    baseline_records: list[dict] = []
    decisions: list[Decision] = []

    for event in events:
        persist_event(db, event)
        state = load_or_open_mandate_state(db, event)

        # One fresh `context_outcomes` per event, shared between the two
        # arms -- identical (bucket, context) means identical simulated
        # outcome for both. See generator/oracle.py::draw_retry_outcome.
        context_outcomes: dict = {}
        agent_result = simulate_agent_cycle(event, matrix, context_outcomes, sim_seed)
        baseline_result = simulate_baseline_cycle(event, agent_result["bucket"], context_outcomes, sim_seed)

        agent_records.append(agent_result)
        baseline_records.append(baseline_result)
        decisions.extend(_persist_cycle(db, event, agent_result, matrix))
        _final_state(state, agent_result)

    db.commit()

    agent_metrics = compute_metrics(agent_records)
    baseline_metrics = compute_metrics(baseline_records)
    rs_uplift_pct = (
        100
        * (agent_metrics["rupees_recovered"] - baseline_metrics["rupees_recovered"])
        / baseline_metrics["rupees_recovered"]
        if baseline_metrics["rupees_recovered"]
        else float("inf")
    )

    run = SimulationRun(
        run_id=f"run_{uuid.uuid4().hex[:12]}",
        batch_size=count,
        batch_seed=batch_seed,
        sim_seed=sim_seed,
        agent_metrics=agent_metrics,
        baseline_metrics=baseline_metrics,
        agent_by_bucket=compute_metrics_by_bucket(agent_records),
        baseline_by_bucket=compute_metrics_by_bucket(baseline_records),
        rs_uplift_pct=rs_uplift_pct,
        rate_delta_points=agent_metrics["recovery_rate_pct"] - baseline_metrics["recovery_rate_pct"],
        engine_version=settings.engine_version,
        matrix_version=matrix.matrix_version,
    )
    db.add(run)
    db.commit()
    return run, decisions


def seed_batch(db: Session, matrix: DecisionMatrix, count: int, batch_seed: int) -> int:
    """`POST /reset`: clear everything and regenerate the batch as RAW
    EVENTS ONLY. No decisions, no metrics -- a clean slate the demo can
    then run `/simulate/run` (or individual webhooks) against."""
    clear_all(db)
    events = generate_batch(count, batch_seed, matrix)
    for event in events:
        persist_event(db, event)
    db.commit()
    return len(events)
