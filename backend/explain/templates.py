"""Deterministic template explanations -- the fallback path for M7.

These are NOT a degraded placeholder to apologise for. They are the
system's guarantee that every audit record carries a readable rationale
whether or not the Anthropic API is reachable (PRD M7 acceptance: "every
decision has a non-empty explanation, with or without network access"),
and they are what the demo runs on when the venue wifi dies.

Two deliberate differences from the LLM path, both consequences of where
each one runs:

  - Templates are rendered PER DECISION, so they can safely name that
    decision's own specifics (the exact snapped retry time, the reason
    code, the governance rules that fired). The LLM sentence is CACHED
    across every decision sharing a (bucket, action, policy_verdict) key,
    so it must not name anything that varies inside that key -- see
    explain/explain.py's module docstring.
  - Templates are pure string formatting: same decision in, same sentence
    out, forever. No non-determinism anywhere near the audit record.

Every phrase below restates something already present in the structured
decision record. Nothing here introduces a fact the record doesn't have.
"""

# One phrase per bucket, stating the diagnosis in a merchant's language.
_BUCKET_PHRASE = {
    "B1_CONGESTION": (
        "the debit failed inside NPCI's restricted 10:00-13:00 window and this customer "
        "has no history of balance failures, so network congestion is the likely cause"
    ),
    "B2_BALANCE": "the customer's account did not have the balance to cover this debit",
    "B3_TRANSIENT": "the failure looks like a temporary technical fault rather than anything wrong with the mandate",
    "B4_STRUCTURAL": "the debit ran into a limit or configuration on the customer's side rather than a one-off fault",
    "B5_DEAD": "the mandate or instrument behind this debit is no longer usable, so retrying it cannot succeed",
    "B_UNKNOWN": "the failure reason could not be classified confidently enough to act on automatically",
}
_BUCKET_FALLBACK_PHRASE = "the failure was classified into a recovery category"

_ACTION_PHRASE = {
    "RETRY_SCHEDULED": "a retry has been scheduled for {scheduled_for}, outside the restricted window",
    "NUDGE_SENT": "the customer has been asked to act rather than the debit being retried",
    "STOPPED": "no further automated attempt will be made on this cycle",
    "HUMAN_QUEUE": "the case has been queued for a person to review before anything else happens",
}
_ACTION_FALLBACK_PHRASE = "the recorded action was taken"

# The governance rules worth naming in prose. Rules that merely PASSED are
# in the audit record's policy_reasons list already and add nothing to a
# one-line rationale, so only the ones that actually fired are rendered.
_REASON_PHRASE = {
    "unclassified_or_low_confidence": "the classification was not confident enough to act on",
    "risk_flagged": "the payment was flagged by risk checks",
    "repeat_offender": "this mandate already failed a previous recovery cycle",
    "high_value_amount": "the amount is above the high-value review threshold",
    "hard_decline_never_retried": "hard declines are never auto-retried",
    "recovery_cycle_expired": "the 7-day recovery cycle has expired",
    "attempt_cap_exceeded": "the retry attempt cap for this cycle is already used up",
    "cooling_off_not_satisfied": "the minimum gap between retries had not elapsed",
    "max_contacts_reached": "the contact limit for this cycle is already used up",
}


def _fmt_time(scheduled_for) -> str:
    if scheduled_for is None:
        return "the scheduled time"
    return scheduled_for.strftime("%d %b %Y at %H:%M IST")


def render_template(context: dict) -> str:
    """`context` is the same PII-free dict the LLM prompt is built from,
    plus `scheduled_for` (safe here -- see the module docstring)."""
    bucket_phrase = _BUCKET_PHRASE.get(context["bucket"], _BUCKET_FALLBACK_PHRASE)
    action_phrase = _ACTION_PHRASE.get(context["action"], _ACTION_FALLBACK_PHRASE).format(
        scheduled_for=_fmt_time(context.get("scheduled_for"))
    )

    fired = [_REASON_PHRASE[r] for r in context.get("policy_reasons", []) if r in _REASON_PHRASE]
    if fired:
        governance = f" The governance check returned {context['policy_verdict']} because {_join(fired)}."
    else:
        governance = f" The governance check returned {context['policy_verdict']} with every rule satisfied."

    return f"We think {bucket_phrase}, so {action_phrase}.{governance}"


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"
