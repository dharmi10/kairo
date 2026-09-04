"""The agent's full-cycle simulation -- the "outcome resolver" (PRD build
order task 8, grouped with M6/metrics). Phases 3-5 each deliberately
computed exactly ONE decision against a fresh, single-attempt
mandate_history -- that was the right scope for those phases, but the
five M6 metrics need to know whether a mandate's failure cycle actually
RESOLVES, which means repeatedly deciding, scheduling, drawing an oracle
outcome, and updating state until the mandate recovers or the agent stops
taking autonomous action.

ASSUMPTION, load-bearing for this simulation: a failed retry is modelled
as a re-observation of the SAME reason code (`event["error"]["reason"]`
never changes across attempts) with `attempt_number` incremented and
`failed_at` advanced to the retry's own scheduled time. There is no model
here for what a follow-up Razorpay webhook's reason would actually say
after our own retry fails -- Razorpay doesn't publish that, and inventing
it would be a bigger, unlabelled assumption than reusing the original
reason. Advancing `failed_at` is what lets cooling-off, B3's
elapsed-hours oracle branch, and the 7-day cycle-expiry check all anchor
correctly off the LATEST attempt rather than the first one.
"""

from datetime import timedelta

from app.config import settings
from app.matrix import DecisionMatrix
from app.rng import deterministic_random
from classifier.classify import classify
from executor.executor import HUMAN_QUEUE, NUDGE_SENT, RETRY_SCHEDULED, resolve_action
from generator.oracle import draw_nudge_acceptance, draw_retry_outcome
from policy.policy import evaluate_policy

# Defense-in-depth only: the global attempt cap (3) plus "STOPPED /
# HUMAN_QUEUE / NUDGE_SENT are all terminal" already bound this well
# below 10 in practice -- this exists so a future bug can't spin forever.
MAX_LOOP_ITERATIONS = 10

# ASSUMPTION, per the user's explicit spec (2026-09-03): an escalated case
# is reviewed by a human within a business day -- 12h models "a merchant
# checks their queue same-day", not an instant automated response. ~70% of
# escalations get approved and proceed through the normal recovery play;
# ~30% are rejected and end not-recovered. Documented in the README
# ("Human review of escalated events") alongside the other oracle
# assumptions -- these two numbers are not measured, they're a modelling
# choice to avoid treating every escalation as permanent Rs.0 loss, which
# is neither realistic nor what escalation means (a human reviews it,
# they don't discard it).
HUMAN_REVIEW_DELAY_HOURS = 12
HUMAN_REVIEW_APPROVAL_RATE = 0.70

# ASSUMPTION: a customer who acts on a nudge (re-authorising a dead
# instrument, switching payment channel, etc.) doesn't do it instantly --
# 24h models "the customer notices and acts within a day", the same order
# of magnitude as the human-review delay above but for a customer's own
# pace rather than a merchant's queue. No data to calibrate this against;
# documented in the README alongside NUDGE_ACCEPTANCE_PROBABILITIES.
NUDGE_ACCEPTANCE_DELAY_HOURS = 24


def _fresh_mandate_history(event: dict) -> dict:
    return {
        "cycle_started_at": event["failed_at"],
        "total_retry_attempts": 0,
        "total_contacts_sent": 0,
        "last_attempt_at": None,
        "prior_cycle_failed": False,
        "b1_congestion_failed_attempts": 0,
    }


def simulate_agent_cycle(event: dict, matrix: DecisionMatrix, context_outcomes: dict, seed) -> dict:
    """`context_outcomes` -- created fresh per event by the caller and
    shared with that event's baseline simulation too -- makes repeated
    attempts under unchanged context correlated instead of independently
    compounding. See generator/oracle.py::draw_retry_outcome.

    `seed` (normally an int -- see app/rng.py) is NOT a shared, mutable
    RNG consumed sequentially across the batch. Every draw in this
    function (human review approval, retry outcome, nudge acceptance)
    derives its own independent stream from `seed` plus everything that
    should make that specific draw distinct (event id, draw type, the
    relevant context key) -- so adding a new draw anywhere else in the
    codebase can never shift this event's outcomes."""
    mandate_history = _fresh_mandate_history(event)
    current = event
    first_bucket = None
    outcome = "FAILED"
    attempts_made = 0
    contacts_sent = 0
    recovered_at = None
    human_approved = False
    human_review = None  # None | "approved" | "rejected" -- for diagnostics only, not used by compute_metrics

    for _ in range(MAX_LOOP_ITERATIONS):
        classification = classify(current, matrix)
        if first_bucket is None:
            first_bucket = classification["bucket"]  # what the agent believed at the moment it first saw this failure -- used for by-bucket grouping

        verdict, reasons = evaluate_policy(current, classification, mandate_history)
        plan = resolve_action(current, classification, verdict, reasons, mandate_history, matrix, human_approved=human_approved)

        if plan["action"] == HUMAN_QUEUE:
            # Escalation is a delay-and-filter, not a black hole: a human
            # reviews the queue within a business day (12h ASSUMPTION) and
            # approves ~70% of the time (ASSUMPTION) -- see
            # HUMAN_REVIEW_DELAY_HOURS / HUMAN_REVIEW_APPROVAL_RATE above
            # and the README. Rejected cases end here, not-recovered.
            # Approved cases advance the clock past the review delay and
            # loop back through the SAME classify/policy/resolve_action
            # path as everything else, with `human_approved=True` from
            # here on so this event's own review isn't re-litigated on a
            # later iteration (e.g. a subsequent retry failing and the
            # loop coming back around) -- the human signed off once, on
            # the whole cycle, not per-attempt.
            review_time = current["failed_at"] + timedelta(hours=HUMAN_REVIEW_DELAY_HOURS)
            review_rng = deterministic_random(seed, event["event_id"], "human_review")
            if review_rng.random() < HUMAN_REVIEW_APPROVAL_RATE:
                human_approved = True
                human_review = "approved"
                current = {**current, "failed_at": review_time}
                continue
            human_review = "rejected"
            break

        if plan["action"] == RETRY_SCHEDULED:
            attempts_made += 1
            if draw_retry_outcome(event, plan["scheduled_for"], context_outcomes, seed):
                outcome = "RECOVERED"
                recovered_at = plan["scheduled_for"]
                break

            mandate_history["total_retry_attempts"] += 1
            mandate_history["last_attempt_at"] = plan["scheduled_for"]
            if plan["effective_bucket"] == "B1_CONGESTION":
                mandate_history["b1_congestion_failed_attempts"] += 1
            # BUG FOUND AND FIXED (2026-09-03, Phase 6): advancing `current`
            # to EXACTLY the attempt's own firing time made the next loop's
            # cooling-off check compare that attempt against itself --
            # `failed_at - last_attempt_at` is always 0, which always
            # violates the 2h floor regardless of the bucket's own
            # delay_hours (which can be well over 2h). That silently capped
            # every mandate at exactly 1 real retry attempt, no matter what
            # the matrix said, and was the root cause of the agent losing
            # to baseline on $ recovered. Fix: the next decision point is
            # modelled as no sooner than the cooling-off floor after the
            # last attempt -- resolve_action's own bucket-specific
            # delay_hours is then computed FROM that point, so slow buckets
            # (delay_hours > cooling-off) are unaffected and fast ones
            # (B1's 0-1h, payment_timed_out's 0h) get floored up to 2h.
            current = {
                **current,
                "attempt_number": current["attempt_number"] + 1,
                "failed_at": plan["scheduled_for"] + timedelta(hours=settings.min_cooling_off_hours),
            }
            continue

        if plan["action"] == NUDGE_SENT:
            contacts_sent += 1
            mandate_history["total_contacts_sent"] += 1
            # Same correlated-draw pattern as a retry: does the customer
            # act on this nudge? (generator/oracle.py::draw_nudge_acceptance,
            # NUDGE_ACCEPTANCE_PROBABILITIES -- 0.30 for a B5 reauth nudge,
            # 0.45 for a B4 channel-switch nudge). If accepted, the full
            # amount recovers after NUDGE_ACCEPTANCE_DELAY_HOURS (24h,
            # ASSUMPTION -- customers don't act instantly). Terminal
            # either way -- no follow-up retry after a nudge.
            if draw_nudge_acceptance(event, plan["effective_bucket"], context_outcomes, seed):
                outcome = "RECOVERED"
                recovered_at = current["failed_at"] + timedelta(hours=NUDGE_ACCEPTANCE_DELAY_HOURS)
            break

        break  # STOPPED -- terminal, no more autonomous action (HUMAN_QUEUE is handled above)

    hours_to_recovery = (recovered_at - event["failed_at"]).total_seconds() / 3600 if recovered_at else None

    return {
        "event_id": event["event_id"],
        "amount_inr": event["amount_inr"],
        "bucket": first_bucket,
        "outcome": outcome,
        "attempts_made": attempts_made,
        "contacts_sent": contacts_sent,
        "hours_to_recovery": hours_to_recovery,
        "human_review": human_review,  # None | "approved" | "rejected" -- diagnostic only
    }
