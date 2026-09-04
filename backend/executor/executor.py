"""M5 -- the executor.

Two layers, deliberately separated:
  - `resolve_action()` is pure: (event, classification, policy_verdict,
    policy_reasons, mandate_history, matrix) -> a plan (action, schedule,
    effective bucket/confidence/signals). No DB, no side effects, fully
    testable and exactly what the "zero retries in the restricted window"
    batch test runs against.
  - `execute_decision()` is the only impure part: it calls resolve_action()
    and then writes the Decision (+ Attempt, if one was scheduled) to the
    DB in ONE transaction -- both commit together or neither does
    (architecture-and-security.md sec. 5.2).

Simulated clock: every timestamp here is computed via datetime arithmetic
on `event["failed_at"]`. Nothing sleeps, nothing reads the real wall clock.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.dates import next_payday_on_or_after
from app.matrix import DecisionMatrix
from app.models import Attempt, Decision
from classifier.classify import AMBIGUOUS_RECLASSIFIED_CONFIDENCE
from policy.policy import ALLOW, BLOCK, ESCALATE

RETRY_SCHEDULED = "RETRY_SCHEDULED"
NUDGE_SENT = "NUDGE_SENT"
STOPPED = "STOPPED"
HUMAN_QUEUE = "HUMAN_QUEUE"

_RETRY_MATRIX_ACTIONS = {"RETRY", "RETRY_AT_INCOME_WINDOW"}
_NUDGE_MATRIX_ACTIONS = {"NUDGE_REAUTH", "NUDGE_CUSTOMER_LINK", "NUDGE_REENGAGE", "NUDGE_ALT_METHOD"}

# Reasons from policy.py's rules 5-9 that are independent governance stops,
# not "needs a human to look at it" -- a human approving an escalation
# doesn't waive these (see resolve_action's `human_approved` param).
_BLOCK_ONLY_REASONS = {
    "hard_decline_never_retried",
    "recovery_cycle_expired",
    "attempt_cap_exceeded",
    "cooling_off_not_satisfied",
    "max_contacts_reached",
}

# ASSUMPTION: reclassified-to-B3 retries (see _effective_classification)
# have no single reason code to take a delay from -- there's no one
# "the B3 play" once we've fallen back from B1, so this uses the same
# delay gateway_technical_error's own play uses, as the closest
# representative B3 timing.
RECLASSIFIED_B3_FALLBACK_DELAY_HOURS = 3

# Explicit marker for "this classification came from
# reclassify_after_max_attempts, not a fresh classify() call" -- used to
# detect the reclassified case without relying on bucket-equality
# comparisons that would coincidentally match for the wrong reason (see
# _play_for's docstring).
_RECLASSIFIED_SIGNAL = "reclassified_after_2_failed_congestion_retries"


def snap_to_safe_window(t: datetime) -> tuple[datetime, bool]:
    """Never let a retry land in the NPCI restricted window. Unconditional
    -- applied to every RETRY_SCHEDULED timestamp regardless of the YAML's
    per-code `snap_to_window` flag (which is true almost everywhere anyway).
    This is a systemic NPCI constraint, not something a future matrix edit
    should be able to opt out of per reason code."""
    if settings.npci_restricted_start_hour <= t.hour < settings.npci_restricted_end_hour:
        return t.replace(hour=settings.npci_snap_hour, minute=settings.npci_snap_minute, second=0, microsecond=0), True
    return t, False


def _income_window_schedule(event: dict) -> datetime:
    """B2_BALANCE: 1 day after the customer's typical_credit_day (PRD sec.
    M5). Time-of-day is ASSUMPTION: kept the same as the original failure's
    time-of-day (matching how M6's baseline is described -- "same time of
    day" -- as the natural default), then snapped like anything else."""
    typical_credit_day = event["customer_history"]["typical_credit_day"]
    payday = next_payday_on_or_after(event["failed_at"].date(), typical_credit_day)
    retry_date = payday + timedelta(days=1)
    return datetime.combine(retry_date, event["failed_at"].time())


def _effective_classification(event: dict, classification: dict, mandate_history: dict, matrix: DecisionMatrix) -> dict:
    """Applies congestion_override.reclassify_after_max_attempts (deferred
    from M3 -- needs retry history, which only exists here). If this
    mandate has already had 2 failed B1_CONGESTION retries, stop guessing
    "congestion" and fall back to B3_TRANSIENT (no balance-failure
    history) or B2_BALANCE (otherwise), per the YAML rule -- read from
    the matrix, not re-hardcoded.

    Returns a (possibly adjusted) {bucket, confidence, signals} -- never
    mutates the `classification` argument."""
    if classification["bucket"] != "B1_CONGESTION":
        return classification

    failed_b1_attempts = mandate_history.get("b1_congestion_failed_attempts", 0)
    if failed_b1_attempts < 2:
        return classification

    reclass = matrix.congestion_override["reclassify_after_max_attempts"]
    no_balance_history = (
        event["customer_history"]["prior_insufficient_funds_90d"]
        == matrix.congestion_override["condition"]["prior_insufficient_funds_90d"]
    )
    new_bucket = reclass["if_no_balance_failure_history"] if no_balance_history else reclass["else"]

    return {
        "bucket": new_bucket,
        "confidence": AMBIGUOUS_RECLASSIFIED_CONFIDENCE,
        "signals": [*classification["signals"], _RECLASSIFIED_SIGNAL],
    }


def _play_for(event: dict, classification: dict, matrix: DecisionMatrix) -> dict | None:
    """Which matrix play governs THIS decision's action/timing.

    B1_CONGESTION reached via the timing+history inference (signalled by
    "fired_in_restricted_window" in classify()'s output) has no reason
    code of its own -- it uses congestion_override's play. B1_CONGESTION
    reached via a direct code (payment_declined_due_to_high_traffic) uses
    that code's own play, same as everything else. Returns None when
    `classification` came from reclassify_after_max_attempts (detected
    explicitly via _RECLASSIFIED_SIGNAL, not inferred from bucket
    matching -- gateway_technical_error's and payment_failed's own
    declared bucket already IS B3_TRANSIENT, so a bucket-equality check
    would silently "succeed" for the wrong reason on exactly the two
    codes that matter here). Callers handle None with fallback timing.
    """
    if _RECLASSIFIED_SIGNAL in classification["signals"]:
        return None

    if classification["bucket"] == "B1_CONGESTION":
        if "fired_in_restricted_window" in classification["signals"]:
            return matrix.congestion_override["play"]
        return matrix.reason_codes[event["error"]["reason"]]

    return matrix.reason_codes.get(event["error"]["reason"])


def resolve_action(
    event: dict,
    classification: dict,
    policy_verdict: str,
    policy_reasons: list[str],
    mandate_history: dict,
    matrix: DecisionMatrix,
    human_approved: bool = False,
) -> dict:
    """Pure. Returns {action, scheduled_for, window_snapped,
    effective_bucket, effective_confidence, effective_signals}.

    `human_approved`: set by a caller that has already run this decision
    through human review (see executor/simulate.py's escalation-review
    loop) and had it approved. It does NOT blanket-override policy_verdict
    -- an ESCALATE verdict that also carries one of policy.py's rule 5-9
    BLOCK-only reasons (hard decline, cycle expired, attempt cap,
    cooling-off, max contacts) still resolves to BLOCK, since those are
    independent governance stops a human sign-off on "should a person
    look at this" doesn't waive. A pure ESCALATE (no BLOCK reason also
    present) resolves to ALLOW, and the matrix's own per-code
    ESCALATE_HUMAN preference (see below) is skipped for the same reason
    -- the human already reviewed this case, so it doesn't route back to
    the queue a second time."""
    effective = _effective_classification(event, classification, mandate_history, matrix)
    play = _play_for(event, effective, matrix)
    matrix_action = play["action"] if play is not None else None

    scheduled_for: datetime | None = None
    window_snapped = False

    if human_approved and policy_verdict == ESCALATE:
        effective_verdict = BLOCK if any(r in policy_reasons for r in _BLOCK_ONLY_REASONS) else ALLOW
    else:
        effective_verdict = policy_verdict

    if effective_verdict == ESCALATE:
        action = HUMAN_QUEUE

    elif effective_verdict == BLOCK:
        # Exception, not a general override of M4: PRD/decision-matrix.md
        # explicitly prescribe "no retry, immediate re-authorisation
        # nudge" for every B5_DEAD code. hard_decline_never_retried blocks
        # the RETRY specifically -- it doesn't mean "do nothing at all".
        # Every OTHER BLOCK reason (cycle expired, attempt cap, cooling-off,
        # max contacts) still means "no autonomous action of any kind" --
        # that blanket reading of M4's verdict is left exactly as Phase 4
        # built and tested it, not relitigated here.
        if "hard_decline_never_retried" in policy_reasons and matrix_action in _NUDGE_MATRIX_ACTIONS:
            action = NUDGE_SENT
        else:
            action = STOPPED

    else:  # ALLOW
        if matrix_action == "ESCALATE_HUMAN" and not human_approved:
            # The matrix's own per-code judgement can call for a human
            # even when no M4 rule independently caught it (e.g.
            # mandate_amount_exceeded at a low amount) -- more
            # conservative wins, same principle as M4's worst-wins. Once a
            # human has already approved this case (human_approved=True),
            # this preference is skipped rather than re-queuing it -- see
            # the docstring above. There's usually no RETRY/NUDGE play
            # behind ESCALATE_HUMAN either (e.g. mandate_amount_exceeded
            # has none), so this typically falls through to STOPPED below,
            # same as "human handled it outside the automated system".
            action = HUMAN_QUEUE
        elif matrix_action in _RETRY_MATRIX_ACTIONS or (matrix_action is None and effective["bucket"] in ("B2_BALANCE", "B3_TRANSIENT")):
            action = RETRY_SCHEDULED
            if effective["bucket"] == "B2_BALANCE":
                proposed = _income_window_schedule(event)
            elif effective["bucket"] == "B1_CONGESTION":
                delay_hours = play.get("delay_hours", 0) if play else 0
                proposed = event["failed_at"] + timedelta(hours=delay_hours)
            else:
                # .get(), not [] -- defensive: a matrix entry missing
                # delay_hours (this happened once, for the ambiguous B3
                # codes, caught by the whole-batch test below) falls back
                # to the same default a reclassified/play-less bucket
                # uses, rather than crashing the whole batch.
                delay_hours = play.get("delay_hours", RECLASSIFIED_B3_FALLBACK_DELAY_HOURS) if play else RECLASSIFIED_B3_FALLBACK_DELAY_HOURS
                proposed = event["failed_at"] + timedelta(hours=delay_hours)
            scheduled_for, window_snapped = snap_to_safe_window(proposed)
        elif matrix_action in _NUDGE_MATRIX_ACTIONS:
            action = NUDGE_SENT
        elif matrix_action == "NEW_MANDATE_FLOW":
            # Genuinely out of this system's scope (PRD sec. 3, out of
            # scope: mandate creation) -- closest fit in the action
            # vocabulary is STOPPED: this agent's job on this mandate ends
            # here.
            action = STOPPED
        else:
            action = STOPPED

    return {
        "action": action,
        "scheduled_for": scheduled_for,
        "window_snapped": window_snapped,
        "effective_bucket": effective["bucket"],
        "effective_confidence": effective["confidence"],
        "effective_signals": effective["signals"],
    }


def execute_decision(
    db: Session,
    event: dict,
    classification: dict,
    policy_verdict: str,
    policy_reasons: list[str],
    mandate_history: dict,
    matrix: DecisionMatrix,
    mandate_state=None,
) -> Decision:
    """The only impure function here. Decision write + Attempt scheduling
    happen against the same Session and commit together -- if the commit
    fails, neither row persists. There is never a scheduled Attempt
    without its Decision audit record (architecture-and-security.md
    sec. 5.2).

    `mandate_state` (Phase 7, optional): the `MandateState` row this
    decision's counters should be advanced on. Applied HERE, before the
    single commit, rather than by the caller afterwards -- because a
    caller that committed the counters separately could crash in between
    and leave a scheduled attempt that the attempt cap doesn't know about,
    which is precisely the atomicity property sec. 5.2 asks for. Callers
    that don't track mandate state (the in-memory batch runners in
    executor/pipeline.py, and the unit tests) pass nothing and behave
    exactly as before.

    `explanation` is left NULL on purpose: M7 fills it in only after this
    transaction has committed. See explain/explain.py."""
    plan = resolve_action(event, classification, policy_verdict, policy_reasons, mandate_history, matrix)

    decision = Decision(
        decision_id=f"dec_{event['event_id']}",
        event_id=event["event_id"],
        classified_bucket=plan["effective_bucket"],
        confidence=plan["effective_confidence"],
        signals=plan["effective_signals"],
        policy_verdict=policy_verdict,
        policy_reasons=policy_reasons,
        action=plan["action"],
        scheduled_for=plan["scheduled_for"],
        window_snapped=plan["window_snapped"],
        explanation=None,  # M7 fills this in AFTER this transaction commits
        explanation_source=None,
        outcome="PENDING" if plan["action"] == RETRY_SCHEDULED else "NOT_ATTEMPTED",
        amount_recovered_inr=0,
        engine_version=settings.engine_version,
        matrix_version=matrix.matrix_version,
    )
    db.add(decision)

    if plan["action"] == RETRY_SCHEDULED:
        attempt = Attempt(
            attempt_id=f"att_{event['event_id']}",
            decision_id=decision.decision_id,
            mandate_id=event["mandate_id"],
            cycle_id=event["cycle_id"],
            retry_attempt_number=mandate_history["total_retry_attempts"] + 1,
            scheduled_for=plan["scheduled_for"],
        )
        db.add(attempt)

    if mandate_state is not None:
        _advance_mandate_state(mandate_state, plan, event)

    db.commit()
    return decision


def _advance_mandate_state(state, plan: dict, event: dict) -> None:
    """Mirror this decision's effect onto the mandate's mutable aggregate,
    which is what evaluate_policy's attempt-cap / cooling-off / contact-cap
    rules read on the NEXT event for this mandate. Mutates `state` in the
    caller's Session; the caller commits."""
    if plan["action"] == RETRY_SCHEDULED:
        state.total_retry_attempts += 1
        state.last_attempt_at = plan["scheduled_for"]
    elif plan["action"] == NUDGE_SENT:
        state.total_contacts_sent += 1
        state.status = "stopped"  # a nudge is terminal for the automated cycle -- see executor/simulate.py
    elif plan["action"] == HUMAN_QUEUE:
        state.status = "escalated"
    elif plan["action"] == STOPPED:
        state.status = "stopped"

    state.version += 1
    state.updated_at = datetime.utcnow()
