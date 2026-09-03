"""M4 -- the policy engine.

Pure function: (event, classification, mandate_history) -> (verdict, reasons).
Runs BEFORE any action executes -- classify() has already produced a
bucket/confidence, this decides whether the system is allowed to act on
it. No DB reads, no side effects: `mandate_history` is passed in fully
formed by the caller (shaped like the `mandate_state` table -- see
app/models.py -- but this module has no dependency on SQLAlchemy or
anything else stateful).

Verdict resolution is worst-wins: ESCALATE > BLOCK > ALLOW. If multiple
rules fire at once (e.g. a hard decline that's ALSO risk-flagged), the
more conservative verdict wins without any rule needing to know about the
others -- consistent with P3 (fail closed) in architecture-and-security.md.

Every rule contributes a reason string regardless of whether it fired --
this is more exhaustive than the PRD's own abbreviated audit example
(`policy_reasons: ["within_attempt_cap", "cooling_off_satisfied"]`, which
shows 2 of the ~9 checks actually run). Deliberate: "the audit record is
the product" (architecture-and-security.md sec. 6) argues for completeness
here, not brevity.
"""

from datetime import timedelta

from app.config import settings

ALLOW = "ALLOW"
BLOCK = "BLOCK"
ESCALATE = "ESCALATE"

_SEVERITY = {ALLOW: 0, BLOCK: 1, ESCALATE: 2}


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def evaluate_policy(event: dict, classification: dict, mandate_history: dict) -> tuple[str, list[str]]:
    verdict = ALLOW
    reasons: list[str] = []

    def rule(fired: bool, fail_verdict: str, fail_reason: str, pass_reason: str) -> None:
        nonlocal verdict
        if fired:
            verdict = _worse(verdict, fail_verdict)
            reasons.append(fail_reason)
        else:
            reasons.append(pass_reason)

    # 1. Data-quality gate. Fail closed, always -- checked first, since no
    # bucket-based rule below can be trusted without a confident
    # classification. Belt-and-suspenders with classify()'s own
    # confidence floor (which already routes low confidence to
    # B_UNKNOWN): this check stands on its own regardless of whether the
    # classification it was handed went through that floor.
    rule(
        classification["bucket"] == "B_UNKNOWN"
        or classification["confidence"] < settings.unknown_bucket_confidence_threshold,
        ESCALATE, "unclassified_or_low_confidence", "classification_confident",
    )

    # 2. Risk-flagged -- always escalate, independent of bucket.
    rule(
        event["error"]["reason"] == "payment_risk_check_failed",
        ESCALATE, "risk_flagged", "not_risk_flagged",
    )

    # 3. Repeat offender: this mandate already failed a prior cycle.
    rule(
        mandate_history["prior_cycle_failed"],
        ESCALATE, "repeat_offender", "not_repeat_offender",
    )

    # 4. High value. Strictly greater than the threshold, per PRD wording.
    rule(
        event["amount_inr"] > settings.high_value_threshold_inr,
        ESCALATE, "high_value_amount", "within_value_threshold",
    )

    # 5. Hard decline -- never auto-retried. BLOCK on its own, not
    # ESCALATE: a routine dead mandate doesn't need a human, it just
    # stops. payment_risk_check_failed is ALSO B5_DEAD, but rule 2 above
    # already escalates it -- worst-wins means the final verdict there is
    # ESCALATE without this rule needing to special-case that reason.
    rule(
        classification["bucket"] == "B5_DEAD",
        BLOCK, "hard_decline_never_retried", "not_hard_decline",
    )

    # 6. Recovery cycle expired -- 7 days from cycle start, then stop
    # (not escalate: this is a routine timeout, not a case needing a human).
    cycle_age = event["failed_at"] - mandate_history["cycle_started_at"]
    rule(
        cycle_age > timedelta(days=settings.recovery_cycle_days),
        BLOCK, "recovery_cycle_expired", "within_recovery_cycle",
    )

    # 7. Global attempt cap per cycle.
    rule(
        mandate_history["total_retry_attempts"] >= settings.global_max_retry_attempts,
        BLOCK, "attempt_cap_exceeded", "within_attempt_cap",
    )

    # 8. Cooling-off between retries. No prior attempt at all trivially
    # satisfies this -- there's nothing to cool off from yet.
    last_attempt_at = mandate_history.get("last_attempt_at")
    cooling_off_violated = last_attempt_at is not None and (
        event["failed_at"] - last_attempt_at
    ) < timedelta(hours=settings.min_cooling_off_hours)
    rule(cooling_off_violated, BLOCK, "cooling_off_not_satisfied", "cooling_off_satisfied")

    # 9. Max customer contacts per cycle. Deliberately NOT conditioned on
    # whether the eventual action is a nudge vs. a silent retry -- policy()
    # doesn't know the executor's action choice, and the PRD's own
    # governance table lists this as a flat cap, not one scoped to
    # contact-triggering actions specifically. Simpler and matches the
    # spec literally; flagged as a modelling simplification, not a
    # verified distinction.
    rule(
        mandate_history["total_contacts_sent"] >= settings.max_contacts_per_cycle,
        BLOCK, "max_contacts_reached", "within_contact_limit",
    )

    return verdict, reasons
