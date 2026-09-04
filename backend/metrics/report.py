"""Run BOTH arms against the identical batch and identical oracle, compute
the five metrics aggregate and by-bucket, and print the comparison table.
Run as: python -m metrics.report

Every draw (human review, retry outcome, nudge acceptance) uses an
independent, deterministic per-draw RNG stream (app/rng.py) rather than a
single shared `random.Random` consumed sequentially across the batch --
see generator/oracle.py's module docstring and DECISIONS.md for why that
distinction matters for defensibility. Each event still gets a FRESH
`context_outcomes` cache shared between its agent and baseline
simulations: if the two arms' retries land in the identical (bucket,
context-state), they see the identical simulated outcome, and repeated
attempts under unchanged context never independently compound. See
generator/oracle.py::draw_retry_outcome.

`run_comparison()` is the reusable core (also used by
metrics/multi_seed.py to run this across several seeds) -- it runs the
simulation and returns every number this module prints; `main()` is the
single-seed CLI entry point with the full detailed printout.
"""

from app.matrix import DecisionMatrix, load_decision_matrix
from baseline.baseline import simulate_baseline_cycle
from executor.simulate import (
    HUMAN_REVIEW_APPROVAL_RATE,
    HUMAN_REVIEW_DELAY_HOURS,
    NUDGE_ACCEPTANCE_DELAY_HOURS,
    simulate_agent_cycle,
)
from generator.generate import generate_batch
from metrics.metrics import compute_metrics, compute_metrics_by_bucket

BATCH_SIZE = 500
DEFAULT_BATCH_SEED = 42
DEFAULT_SIM_SEED = 20260903


def run_comparison(batch_seed: int, sim_seed: int, matrix: DecisionMatrix) -> dict:
    """Generate a fresh `BATCH_SIZE`-event batch from `batch_seed`, run
    both arms against it using `sim_seed` for every draw, and return
    everything needed to print or aggregate a report. Pure with respect
    to global state -- two calls with the same (batch_seed, sim_seed)
    always return the same result."""
    events = generate_batch(BATCH_SIZE, batch_seed, matrix)

    agent_records = []
    baseline_records = []
    for event in events:
        context_outcomes: dict = {}
        agent_result = simulate_agent_cycle(event, matrix, context_outcomes, sim_seed)
        agent_records.append(agent_result)
        baseline_records.append(simulate_baseline_cycle(event, agent_result["bucket"], context_outcomes, sim_seed))

    agent_metrics = compute_metrics(agent_records)
    baseline_metrics = compute_metrics(baseline_records)
    rs_uplift_pct = (
        100 * (agent_metrics["rupees_recovered"] - baseline_metrics["rupees_recovered"]) / baseline_metrics["rupees_recovered"]
        if baseline_metrics["rupees_recovered"] else float("inf")
    )
    rate_delta_points = agent_metrics["recovery_rate_pct"] - baseline_metrics["recovery_rate_pct"]

    return {
        "batch_seed": batch_seed,
        "sim_seed": sim_seed,
        "agent_records": agent_records,
        "baseline_records": baseline_records,
        "agent_metrics": agent_metrics,
        "baseline_metrics": baseline_metrics,
        "rs_uplift_pct": rs_uplift_pct,
        "rate_delta_points": rate_delta_points,
    }


def _fmt_hours(h: float | None) -> str:
    return "n/a" if h is None else f"{h:.1f}h"


def _print_comparison(title: str, agent: dict, baseline: dict) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    rows = [
        ("n events", agent["n"], baseline["n"]),
        ("Rs recovered", agent["rupees_recovered"], baseline["rupees_recovered"]),
        ("Recovery rate %", f"{agent['recovery_rate_pct']:.1f}%", f"{baseline['recovery_rate_pct']:.1f}%"),
        ("Attempts on hard declines", agent["attempts_on_hard_declines"], baseline["attempts_on_hard_declines"]),
        ("Median time-to-recovery", _fmt_hours(agent["median_hours_to_recovery"]), _fmt_hours(baseline["median_hours_to_recovery"])),
        ("Customer contacts sent", agent["customer_contacts_sent"], baseline["customer_contacts_sent"]),
    ]
    label_w = max(len(r[0]) for r in rows)
    print(f"  {'metric'.ljust(label_w)}  {'agent':>14}  {'baseline':>14}")
    for label, a, b in rows:
        print(f"  {label.ljust(label_w)}  {str(a):>14}  {str(b):>14}")


def print_full_report(result: dict) -> None:
    agent_records = result["agent_records"]
    baseline_records = result["baseline_records"]
    agent_metrics = result["agent_metrics"]
    baseline_metrics = result["baseline_metrics"]
    rs_uplift_pct = result["rs_uplift_pct"]
    rate_delta_points = result["rate_delta_points"]

    _print_comparison(
        f"AGGREGATE -- agent vs baseline (n={BATCH_SIZE}, batch_seed={result['batch_seed']}, sim_seed={result['sim_seed']})",
        agent_metrics, baseline_metrics,
    )

    wasted_avoided = baseline_metrics["attempts_on_hard_declines"] - agent_metrics["attempts_on_hard_declines"]
    print(f"\nWasted attempts avoided (baseline's B5 attempts - agent's): {wasted_avoided}")

    reviewed = [r for r in agent_records if r["human_review"] is not None]
    approved = [r for r in reviewed if r["human_review"] == "approved"]
    print(
        f"\nHuman review of escalated events: {len(reviewed)} escalated, "
        f"{len(approved)} approved ({100 * len(approved) / len(reviewed):.1f}%), "
        f"{len(reviewed) - len(approved)} rejected "
        f"-- {HUMAN_REVIEW_DELAY_HOURS}h review delay, {100 * HUMAN_REVIEW_APPROVAL_RATE:.0f}% approval rate (ASSUMPTIONS, see README)"
        if reviewed else "\nHuman review of escalated events: 0 escalated in this batch"
    )

    nudged = [r for r in agent_records if r["contacts_sent"] > 0]
    nudge_recovered = [r for r in nudged if r["outcome"] == "RECOVERED"]
    nudge_rs = sum(r["amount_inr"] for r in nudge_recovered)
    print(
        f"\nNudge acceptance (agent only): {len(nudged)} nudges sent, "
        f"{len(nudge_recovered)} accepted ({100 * len(nudge_recovered) / len(nudged):.1f}%), "
        f"Rs.{nudge_rs:,} recovered via nudge acceptance "
        f"-- {NUDGE_ACCEPTANCE_DELAY_HOURS}h acceptance delay (ASSUMPTION, see README)"
        if nudged else "\nNudge acceptance (agent only): 0 nudges sent in this batch"
    )
    print(
        "Baseline sends 0 nudges -- it is retry-only, blind to bucket, and has no "
        "customer-facing action at all (see baseline/baseline.py). This is a real, "
        "structural asymmetry between the two arms, not a metrics gap: the agent's "
        "advantage on B4/B5 comes partly from taking an action baseline cannot take, "
        "not only from better retry timing."
    )

    agent_by_bucket = compute_metrics_by_bucket(agent_records)
    baseline_by_bucket = compute_metrics_by_bucket(baseline_records)
    for bucket in sorted(agent_by_bucket):
        _print_comparison(f"BY BUCKET: {bucket}", agent_by_bucket[bucket], baseline_by_bucket[bucket])

    print("\nUplift sanity check")
    print("-" * 20)
    print(f"  Rs recovered uplift: {rs_uplift_pct:+.1f}%  (agent Rs.{agent_metrics['rupees_recovered']:,} vs baseline Rs.{baseline_metrics['rupees_recovered']:,})")
    print(f"  Recovery rate delta: {rate_delta_points:+.1f} points  ({agent_metrics['recovery_rate_pct']:.1f}% vs {baseline_metrics['recovery_rate_pct']:.1f}%)")
    if rs_uplift_pct > 50:
        print("  FLAG: uplift is above ~50% -- the oracle may be too generous; consider tuning before presenting.")
    elif rs_uplift_pct < 10:
        print("  FLAG: uplift is below ~10% -- the agent's advantage may not be materialising; investigate before building a dashboard around it.")
    else:
        print("  Uplift sits inside the ~10-50% defensible band.")

    # --- supplementary: isolate retry-timing intelligence from governance
    # routing. Events where the agent takes ZERO autonomous action
    # (ESCALATE-to-human, or a hard STOP with no nudge) are, by policy
    # design, deliberately excluded from autonomous retry -- but baseline
    # has no such concept and blindly attempts everything, including
    # things a real system would also flag for human review. Counting
    # those as "Rs.0 recovered by the agent" in a 2-arm comparison
    # penalizes the agent for a deliberate safety property baseline
    # doesn't have, not for worse retry timing. This does NOT change the
    # headline metric above (which follows the PRD's literal definition
    # over the full population) -- it's a diagnostic to show what's
    # actually driving the gap.
    zero_action_ids = {
        r["event_id"] for r in agent_records if r["attempts_made"] == 0 and r["contacts_sent"] == 0 and r["outcome"] == "FAILED"
    }
    agent_sub = [r for r in agent_records if r["event_id"] not in zero_action_ids]
    baseline_sub = [r for r in baseline_records if r["event_id"] not in zero_action_ids]
    agent_sub_metrics = compute_metrics(agent_sub)
    baseline_sub_metrics = compute_metrics(baseline_sub)
    sub_uplift_pct = (
        100 * (agent_sub_metrics["rupees_recovered"] - baseline_sub_metrics["rupees_recovered"]) / baseline_sub_metrics["rupees_recovered"]
        if baseline_sub_metrics["rupees_recovered"] else float("inf")
    )
    print(f"\nDiagnostic: excluding the {len(zero_action_ids)} events where the agent took zero autonomous action")
    print(f"(escalated to human or hard-stopped, Rs.{sum(r['amount_inr'] for r in agent_records if r['event_id'] in zero_action_ids):,} of volume) --")
    print(f"  Rs recovered uplift on the autonomously-actioned subset: {sub_uplift_pct:+.1f}%")
    print(f"  Recovery rate: agent {agent_sub_metrics['recovery_rate_pct']:.1f}% vs baseline {baseline_sub_metrics['recovery_rate_pct']:.1f}%")
    print("  This isolates retry-timing/classification intelligence from the governance-routing decision.")


def main() -> None:
    matrix = load_decision_matrix()
    result = run_comparison(DEFAULT_BATCH_SEED, DEFAULT_SIM_SEED, matrix)
    print_full_report(result)


if __name__ == "__main__":
    main()
