"""The API surface -- PRD sec. 8, all seven endpoints.

    POST   /webhook/payment-failed     ingest one failure event
    POST   /simulate/run               run the full batch through both arms
    GET    /results/summary            the five headline metrics, both arms
    GET    /results/by-bucket          per-bucket breakdown
    GET    /audit                      paginated decision log
    GET    /audit/{decision_id}        single decision detail
    POST   /reset                      clear DB, regenerate batch

One ordering rule holds across every endpoint that produces a decision:
the decision transaction commits FIRST, and only then is the explanation
layer called, inside a guard that cannot propagate. See
`_explain_after_commit` at the bottom of this module and the structural
argument in explain/explain.py.
"""

import json
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.ingest import decide, load_or_open_mandate_state, persist_event
from app.models import Decision, Event, SimulationRun
from app.razorpay_adapter import (
    RazorpayEnvelopeError,
    from_razorpay_envelope,
    looks_like_razorpay_envelope,
    scrub_pii_for_storage,
)
from app.runner import run_simulation, seed_batch
from app.schemas import FailureEventIn, ResetIn, SimulateRunIn
from app.security import verify_webhook_signature
from explain.explain import attach_explanations

logger = logging.getLogger(__name__)
router = APIRouter()

# Razorpay's own header names -- verified via WebFetch against
# razorpay.com/docs/webhooks/validate-test/ (2026-09-04). The signature
# header was already assumed correctly; the idempotency header
# (`x-razorpay-event-id`, "unique per event") is new information from
# that pass -- see app/razorpay_adapter.py.
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def _matrix(request: Request):
    return request.app.state.decision_matrix


def _explainer(request: Request):
    return request.app.state.explainer


def _decision_summary(decision: Decision) -> dict:
    return {
        "decision_id": decision.decision_id,
        "event_id": decision.event_id,
        "classified_bucket": decision.classified_bucket,
        "confidence": decision.confidence,
        "signals": decision.signals,
        "policy_verdict": decision.policy_verdict,
        "policy_reasons": decision.policy_reasons,
        "action": decision.action,
        "scheduled_for": decision.scheduled_for,
        "window_snapped": decision.window_snapped,
        "explanation": decision.explanation,
        "explanation_source": decision.explanation_source,
        "outcome": decision.outcome,
        "amount_recovered_inr": decision.amount_recovered_inr,
    }


# --- 1. webhook ------------------------------------------------------------


@router.post("/webhook/payment-failed")
async def webhook_payment_failed(
    request: Request,
    db: Session = Depends(get_db),
    x_razorpay_signature: str | None = Header(default=None, alias=SIGNATURE_HEADER),
    x_razorpay_event_id: str | None = Header(default=None, alias=EVENT_ID_HEADER),
):
    """Ingest one failure event.

    Accepts EITHER of two body shapes, both signed and dispatched the same
    way -- the envelope choice only affects step 3.5 below:

      - Razorpay's REAL webhook envelope (`{"event": "payment.failed",
        "payload": {"payment": {"entity": {...}}}, ...}`) -- what a live
        Razorpay integration actually sends. See app/razorpay_adapter.py.
      - The flat `FailureEventIn` shape from PRD sec. 6 -- what the
        synthetic generator emits and every internal module already
        consumes. Kept working because it is simpler to drive from tests
        and the demo script, and because "flat internal shape" is still
        this project's single contract everywhere past this endpoint.

    Order is load-bearing and matches architecture-and-security.md
    sec. 3.1-3.2 exactly:

      1. Read the RAW body. Not the parsed JSON -- re-serialising changes
         the bytes (key order, whitespace, unicode escaping) and the
         signature would never match.
      2. Verify HMAC-SHA256 with `hmac.compare_digest`, and REJECT BEFORE
         PARSING. An unsigned payload never reaches the parser, so a
         malformed-JSON attack surface simply isn't reachable without the
         shared secret.
      3. Parse, then detect which of the two shapes this is and map it
         onto the one internal `FailureEventIn` contract.
      4. Dedupe on `event_id`. A duplicate returns 200 with the ORIGINAL
         decision -- never an error. Razorpay retries anything it thinks
         failed, so a 500 here causes a redelivery storm.
      5. Persist, decide, and commit in one transaction. For a real
         Razorpay envelope, the copy persisted as `raw_payload` has
         vpa/email/contact redacted first (P5) -- verification in step 2
         already ran against the true, unredacted bytes.
      6. THEN explain, in a guard that cannot affect any of the above.
    """
    started = time.perf_counter()

    raw_body = await request.body()

    if not x_razorpay_signature or not verify_webhook_signature(
        raw_body, x_razorpay_signature, settings.webhook_shared_secret
    ):
        # Same response whether the header is missing or wrong: an
        # attacker learns nothing about which half failed.
        raise HTTPException(status_code=401, detail="invalid_signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed_json")

    stored_raw_body = raw_body
    if looks_like_razorpay_envelope(payload):
        try:
            parsed = from_razorpay_envelope(payload, x_razorpay_event_id)
        except RazorpayEnvelopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False))
        stored_raw_body = scrub_pii_for_storage(raw_body)
    else:
        try:
            parsed = FailureEventIn.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False))

    event = parsed.to_event_dict()

    # Idempotency layer 1. The events PK enforces this at the DB level too
    # -- this check is what turns the constraint into the documented
    # behaviour (200 + original result) instead of an IntegrityError.
    if db.get(Event, event["event_id"]) is not None:
        existing = (
            db.query(Decision)
            .filter(Decision.event_id == event["event_id"])
            .order_by(Decision.decision_id)
            .all()
        )
        return {
            "status": "duplicate",
            "duplicate": True,
            "event_id": event["event_id"],
            "decisions": [_decision_summary(d) for d in existing],
            "ack_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    persist_event(db, event, stored_raw_body)
    state = load_or_open_mandate_state(db, event)
    decision = decide(db, event, _matrix(request), state)

    # Everything above is durable from here on. The latency budget
    # (architecture-and-security.md sec. 4.1, < 150 ms) applies to exactly
    # this span -- signature verify, dedupe check, persist, decide, commit.
    ack_latency_ms = round((time.perf_counter() - started) * 1000, 2)

    explain_started = time.perf_counter()
    _explain_after_commit(db, [decision], _matrix(request), _explainer(request))
    explanation_latency_ms = round((time.perf_counter() - explain_started) * 1000, 2)

    return {
        "status": "accepted",
        "duplicate": False,
        "event_id": event["event_id"],
        "decision": _decision_summary(decision),
        "ack_latency_ms": ack_latency_ms,
        # Reported separately, and honestly: in this build the explanation
        # runs inside the request, so the client waits for it. It is NOT
        # inside the ack budget above because the decision is already
        # committed by then -- moving this behind a queue changes the
        # response time and nothing else. See explain/explain.py.
        "explanation_latency_ms": explanation_latency_ms,
    }


# --- 2. simulate -----------------------------------------------------------


@router.post("/simulate/run")
def simulate_run(request: Request, body: SimulateRunIn | None = None, db: Session = Depends(get_db)):
    """Run a fresh batch through the agent and the baseline, persist the
    full audit trail, and store the run's metrics.

    Clears prior simulation data first: `/results/summary` reports THE
    last run, and silently blending two batches' numbers would be a worse
    property than a resettable demo DB."""
    params = body or SimulateRunIn()
    matrix = _matrix(request)
    explainer = _explainer(request)

    started = time.perf_counter()
    run, decisions = run_simulation(db, matrix, params.count, params.batch_seed, params.sim_seed)
    decision_ms = round((time.perf_counter() - started) * 1000, 2)

    counts = _explain_after_commit(db, decisions, matrix, explainer)
    run.explanations_llm = counts.get("llm", 0)
    run.explanations_template = counts.get("template", 0)
    run.explanation_api_calls = explainer.api_calls
    db.commit()

    return {
        "run_id": run.run_id,
        "batch_size": run.batch_size,
        "batch_seed": run.batch_seed,
        "sim_seed": run.sim_seed,
        "decisions_written": len(decisions),
        "decision_phase_ms": decision_ms,
        "explanations": {
            "llm": run.explanations_llm,
            "template": run.explanations_template,
            "api_calls": run.explanation_api_calls,
            "distinct_cache_keys": explainer.cache_size,
        },
        "agent": run.agent_metrics,
        "baseline": run.baseline_metrics,
        "rs_uplift_pct": run.rs_uplift_pct,
        "rate_delta_points": run.rate_delta_points,
    }


# --- 3/4. results ----------------------------------------------------------


def _latest_run(db: Session) -> SimulationRun:
    run = db.query(SimulationRun).order_by(SimulationRun.created_at.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="no_simulation_run_yet")
    return run


@router.get("/results/summary")
def results_summary(db: Session = Depends(get_db)):
    """The five headline metrics for both arms, plus the two comparison
    figures that only exist as a difference between them."""
    run = _latest_run(db)
    agent, baseline = run.agent_metrics, run.baseline_metrics
    return {
        "run_id": run.run_id,
        "batch_seed": run.batch_seed,
        "sim_seed": run.sim_seed,
        "engine_version": run.engine_version,
        "matrix_version": run.matrix_version,
        "agent": agent,
        "baseline": baseline,
        "delta": {
            "rupees_recovered": agent["rupees_recovered"] - baseline["rupees_recovered"],
            "rs_uplift_pct": run.rs_uplift_pct,
            "recovery_rate_points": run.rate_delta_points,
            # The PRD's "wasted attempts avoided" metric: baseline retries
            # on hard declines that the agent suppressed. Only meaningful
            # as a difference, which is why compute_metrics reports each
            # arm's own figure and the subtraction happens here.
            "wasted_attempts_avoided": baseline["attempts_on_hard_declines"] - agent["attempts_on_hard_declines"],
        },
    }


@router.get("/results/by-bucket")
def results_by_bucket(db: Session = Depends(get_db)):
    """Per-bucket breakdown for both arms. The bucket label on a BASELINE
    row is still the AGENT's classification of that same event -- baseline
    has no bucketing of its own, that is the point of it being blind."""
    run = _latest_run(db)
    buckets = sorted(set(run.agent_by_bucket) | set(run.baseline_by_bucket))
    return {
        "run_id": run.run_id,
        "buckets": [
            {
                "bucket": bucket,
                "agent": run.agent_by_bucket.get(bucket),
                "baseline": run.baseline_by_bucket.get(bucket),
            }
            for bucket in buckets
        ],
    }


# --- 5/6. audit ------------------------------------------------------------


@router.get("/audit")
def audit_log(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    bucket: str | None = None,
    action: str | None = None,
    policy_verdict: str | None = None,
    outcome: str | None = None,
    q: str | None = Query(default=None, description="substring match on decision_id / event_id"),
):
    """Paginated decision log, newest decision first within a stable
    ordering. The filters are what M8's searchable audit table needs;
    they are plain indexed column matches, no free-text engine."""
    query = db.query(Decision)
    if bucket:
        query = query.filter(Decision.classified_bucket == bucket)
    if action:
        query = query.filter(Decision.action == action)
    if policy_verdict:
        query = query.filter(Decision.policy_verdict == policy_verdict)
    if outcome:
        query = query.filter(Decision.outcome == outcome)
    if q:
        pattern = f"%{q}%"
        query = query.filter(Decision.decision_id.like(pattern) | Decision.event_id.like(pattern))

    total = query.count()
    rows = query.order_by(Decision.decided_at.desc(), Decision.decision_id).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "decisions": [_decision_summary(row) for row in rows],
    }


@router.get("/audit/{decision_id}")
def audit_detail(decision_id: str, db: Session = Depends(get_db)):
    """One decision, in the reconstructible shape
    architecture-and-security.md sec. 6 specifies -- including
    `engine_version` / `matrix_version`, which are what let a historical
    decision be explained with the rules that were in force at the time
    rather than today's."""
    decision = db.get(Decision, decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="decision_not_found")

    event = db.get(Event, decision.event_id)
    return {
        "decision_id": decision.decision_id,
        "event_id": decision.event_id,
        "decided_at": decision.decided_at,
        "engine_version": decision.engine_version,
        "matrix_version": decision.matrix_version,
        "input_snapshot": (
            {
                "reason": event.error_reason,
                "source": event.error_source,
                "step": event.error_step,
                "code": event.error_code,
                "failed_at": event.failed_at,
                "failed_at_hour": event.failed_at.hour,
                "amount_inr": event.amount_inr,
                "merchant_category": event.merchant_category,
                "attempt_number": event.attempt_number,
                "prior_failures_90d": event.prior_failures_90d,
                "prior_insufficient_funds_90d": event.prior_insufficient_funds_90d,
                "typical_credit_day": event.typical_credit_day,
                "mandate_age_days": event.mandate_age_days,
            }
            if event is not None
            else None
        ),
        "classification": {
            "bucket": decision.classified_bucket,
            "confidence": decision.confidence,
            "signals": decision.signals,
        },
        "policy": {
            "verdict": decision.policy_verdict,
            "rules_evaluated": decision.policy_reasons,
        },
        "action": {
            "type": decision.action,
            "scheduled_for": decision.scheduled_for,
            "window_snapped": decision.window_snapped,
        },
        "explanation": decision.explanation,
        "explanation_source": decision.explanation_source,
        "outcome": decision.outcome,
        "amount_recovered_inr": decision.amount_recovered_inr,
    }


# --- 7. reset --------------------------------------------------------------


@router.post("/reset")
def reset(request: Request, body: ResetIn | None = None, db: Session = Depends(get_db)):
    """Clear the DB and regenerate the batch as raw events only. Distinct
    from `/simulate/run`, which also decides and measures."""
    params = body or ResetIn()
    count = seed_batch(db, _matrix(request), params.count, params.batch_seed)
    # A new batch means a new explanation cache generation. Not strictly
    # required (the cache is keyed on decision CLASS, which is stable
    # across batches) but it keeps `api_calls` an honest per-demo counter.
    request.app.state.explainer = type(request.app.state.explainer)()
    return {"status": "reset", "events_generated": count, "batch_seed": params.batch_seed}


# --- the guard -------------------------------------------------------------


def _explain_after_commit(db: Session, decisions, matrix, explainer) -> dict:
    """The ONLY place the API calls M7, and it is always after a commit.

    The broad `except Exception` is deliberate and is the point of the
    function: an explanation is narration attached to an already-durable
    decision, so no failure in producing it may change what the caller
    returns or what is in the database. `attach_explanations` already
    degrades a failed API call to a template internally; this catches the
    outer cases it cannot -- a DB error on the explanation write, a bug in
    the template renderer, a PIILeakError (which is loud in the log
    precisely because it means our own prompt construction is wrong).

    `BaseException` is NOT caught: KeyboardInterrupt and SystemExit must
    still stop the process."""
    try:
        return attach_explanations(db, decisions, matrix, explainer)
    except Exception:
        logger.exception("Explanation layer failed; decisions are unaffected and keep a NULL explanation")
        db.rollback()
        return {}
