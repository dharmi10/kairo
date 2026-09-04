"""The five headline metrics (PRD sec. 8 / decision-matrix.md Part 5),
computed identically for either arm's list of per-event outcome records
-- same function, same code path, so the two arms can't be measured
differently by accident.

Each record: {event_id, amount_inr, bucket, outcome, attempts_made,
contacts_sent, hours_to_recovery}. `bucket` is always the AGENT's
classification of that event, even for baseline records -- baseline has
no bucketing of its own; see baseline/baseline.py.
"""

import statistics


def compute_metrics(records: list[dict]) -> dict:
    total = len(records)
    recovered = [r for r in records if r["outcome"] == "RECOVERED"]
    hours = sorted(r["hours_to_recovery"] for r in recovered if r["hours_to_recovery"] is not None)

    return {
        "n": total,
        "n_recovered": len(recovered),
        "rupees_recovered": sum(r["amount_inr"] for r in recovered),
        "recovery_rate_pct": 100 * len(recovered) / total if total else 0.0,
        # "Wasted attempts avoided" (the comparison metric) is
        # baseline_wasted - agent_wasted, computed by the caller -- this
        # function reports each arm's OWN attempts spent on B5-classified
        # events, which is 0 for the agent by construction (M4's
        # hard-decline-never-retried rule, proven never-ALLOW across 2000
        # cases in Phase 4) and >0 for baseline (it retries everything
        # blindly, hard declines included).
        "attempts_on_hard_declines": sum(r["attempts_made"] for r in records if r["bucket"] == "B5_DEAD"),
        "median_hours_to_recovery": statistics.median(hours) if hours else None,
        "customer_contacts_sent": sum(r["contacts_sent"] for r in records),
    }


def compute_metrics_by_bucket(records: list[dict]) -> dict[str, dict]:
    buckets = sorted({r["bucket"] for r in records})
    return {b: compute_metrics([r for r in records if r["bucket"] == b]) for b in buckets}
