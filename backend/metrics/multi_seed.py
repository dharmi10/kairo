"""Run the agent-vs-baseline comparison across several independent
seeds and report the uplift RANGE, not a single number. Run as:
python -m metrics.multi_seed

A single seed=42 result invites "did you pick the seed that looked
good?" -- a fair question, since seed 42 is also literally this
project's other default (M2's generator batch, unit tests, etc). This
runs `metrics.report.run_comparison` across `SEEDS` (n=20, extended from
an original n=5 -- 5 gives a noisy mean estimate, and 20 runs cheaply) --
a DIFFERENT `batch_seed` (so each run generates a genuinely different
500-event population, not just different dice on the same events) paired
with a matching `sim_seed` per run -- and reports min/mean/max/stdev
uplift and recovery-rate delta across them, plus how many seeds fall
below the "advantage may not be materialising" floor, so a wide spread is
visible AND quantified, not averaged away or eyeballed.

Each individual run is still fully reproducible (see app/rng.py --
every draw is an independent, deterministic stream, so this is not
"rerun and hope for the same answer").

Also writes `metrics/output/multi_seed_range.json` -- the JSON fixture
the M8 dashboard reads for its "20-seed range" context panel (see
DECISIONS.md, "M8 dashboard: precomputed fixture, not a live sweep
endpoint"). The fixture is a CACHED SNAPSHOT of this command's output,
not a hand-maintained or magic file -- regenerate it any time by
re-running `python -m metrics.multi_seed`; it will be byte-for-byte
identical unless SEEDS, the matrix, or the simulation code changed.
"""

import json
import statistics
from pathlib import Path

from app.config import settings
from app.matrix import DecisionMatrix, load_decision_matrix
from metrics.report import BATCH_SIZE, run_comparison

# 20 fixed seeds -- not cherry-picked after seeing results (chosen once,
# before that run, and never edited afterward to move the range). n=5
# gave a noisy estimate of the mean (per the user, correctly); extended
# to n=20 -- cheap to run, and enough to say something quantitative about
# whether a low outcome (seed 2024's original +2.3%) is a common result
# or a genuine tail case, rather than eyeballing 5 points. The original 5
# ([42, 7, 123, 2024, 55555]) are kept as the first 5 for continuity with
# the prior report -- 15 more were added, small ascending integers with
# no special relationship to anything in this codebase, so there is no
# room to argue any of them was picked for its outcome. 42 stays in the
# set so the single-seed report in metrics/report.py remains a member of
# this distribution, not a separate, incomparable number.
SEEDS = [42, 7, 123, 2024, 55555, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 15, 17, 19, 21]

UPLIFT_FLOOR_PCT = 10  # below this: "advantage may not be materialising" (metrics/report.py's own threshold)
UPLIFT_CEILING_PCT = 50  # above this: "may be too generous" (same threshold)

FIXTURE_PATH = Path(__file__).resolve().parent / "output" / "multi_seed_range.json"


def run_sweep(seeds: list[int], matrix: DecisionMatrix) -> dict:
    """Run `run_comparison` for every seed and reduce to summary
    statistics. Returns per-seed rows (small: aggregate numbers only, NOT
    the full agent_records/baseline_records -- those are batch_size*2
    per-event dicts per seed, not needed for a range summary) plus
    min/mean/max/stdev for both headline numbers."""
    per_seed = []
    for seed in seeds:
        r = run_comparison(seed, seed, matrix)
        per_seed.append({
            "batch_seed": r["batch_seed"],
            "sim_seed": r["sim_seed"],
            "agent_rs_recovered": r["agent_metrics"]["rupees_recovered"],
            "baseline_rs_recovered": r["baseline_metrics"]["rupees_recovered"],
            "agent_recovery_rate_pct": r["agent_metrics"]["recovery_rate_pct"],
            "baseline_recovery_rate_pct": r["baseline_metrics"]["recovery_rate_pct"],
            "rs_uplift_pct": r["rs_uplift_pct"],
            "recovery_rate_delta_points": r["rate_delta_points"],
        })

    uplifts = [row["rs_uplift_pct"] for row in per_seed]
    rate_deltas = [row["recovery_rate_delta_points"] for row in per_seed]
    below = [row for row in per_seed if row["rs_uplift_pct"] < UPLIFT_FLOOR_PCT]
    above = [row for row in per_seed if row["rs_uplift_pct"] > UPLIFT_CEILING_PCT]

    return {
        "per_seed": per_seed,
        "rs_uplift_pct": {
            "min": min(uplifts), "mean": statistics.mean(uplifts), "max": max(uplifts),
            "stdev": statistics.stdev(uplifts),
        },
        "recovery_rate_delta_points": {
            "min": min(rate_deltas), "mean": statistics.mean(rate_deltas), "max": max(rate_deltas),
        },
        "seeds_below_floor": {
            "count": len(below), "pct": 100 * len(below) / len(per_seed),
            "seeds": [row["batch_seed"] for row in below],
        },
        "seeds_above_ceiling": {
            "count": len(above), "pct": 100 * len(above) / len(per_seed),
            "seeds": [row["batch_seed"] for row in above],
        },
    }


def _describe(rows: list[dict]) -> str:
    return ", ".join(f"seed {row['batch_seed']} ({row['rs_uplift_pct']:+.1f}%)" for row in rows)


def print_sweep_report(sweep: dict) -> None:
    per_seed = sweep["per_seed"]
    n = len(per_seed)

    print(f"Multi-seed comparison -- {n} independent batches, seeds {[row['batch_seed'] for row in per_seed]}")
    print("=" * 70)
    print(f"\n{'seed':>8}  {'agent Rs':>12}  {'baseline Rs':>12}  {'uplift %':>10}  {'rate delta':>11}")
    for row in per_seed:
        print(
            f"{row['batch_seed']:>8}  {row['agent_rs_recovered']:>12,}  "
            f"{row['baseline_rs_recovered']:>12,}  {row['rs_uplift_pct']:>+9.1f}%  "
            f"{row['recovery_rate_delta_points']:>+10.1f}pt"
        )

    u = sweep["rs_uplift_pct"]
    print("\nUplift range across seeds")
    print("-" * 25)
    print(f"  min:    {u['min']:+.1f}%")
    print(f"  mean:   {u['mean']:+.1f}%")
    print(f"  max:    {u['max']:+.1f}%")
    print(f"  stdev:  {u['stdev']:.1f} points (n={n})")

    rd = sweep["recovery_rate_delta_points"]
    print("\nRecovery-rate delta range across seeds")
    print("-" * 25)
    print(f"  min:    {rd['min']:+.1f} points")
    print(f"  mean:   {rd['mean']:+.1f} points")
    print(f"  max:    {rd['max']:+.1f} points")

    below = sweep["seeds_below_floor"]
    above = sweep["seeds_above_ceiling"]
    below_rows = [row for row in per_seed if row["batch_seed"] in below["seeds"]]
    above_rows = [row for row in per_seed if row["batch_seed"] in above["seeds"]]

    print(f"\nSeeds below the +{UPLIFT_FLOOR_PCT}% floor: {below['count']} of {n} ({below['pct']:.0f}%)")
    if below_rows:
        print(f"  {_describe(below_rows)}")
        tail_verdict = "a genuine tail case" if below["pct"] <= 10 else "NOT a rare tail case -- a common outcome in this sample"
        print(f"  -- at {below['pct']:.0f}% of seeds, this is {tail_verdict}")
    print(f"Seeds above the +{UPLIFT_CEILING_PCT}% ceiling: {above['count']} of {n} ({above['pct']:.0f}%)")
    if above_rows:
        print(f"  {_describe(above_rows)}")
    if below["count"] == 0 and above["count"] == 0:
        print(f"Every seed's uplift lands inside the ~{UPLIFT_FLOOR_PCT}-{UPLIFT_CEILING_PCT}% defensible band.")


def write_fixture(sweep: dict, matrix: DecisionMatrix, out_path: Path = FIXTURE_PATH) -> None:
    fixture = {
        "_generated_by": "python -m metrics.multi_seed",
        "_note": (
            "Cached snapshot, not a hand-maintained or magic file -- "
            "regenerate any time with `python -m metrics.multi_seed` from "
            "backend/. Consumed by the M8 dashboard's 20-seed range panel "
            "(see DECISIONS.md, 'M8 dashboard: precomputed fixture, not a "
            "live sweep endpoint')."
        ),
        "batch_size": BATCH_SIZE,
        "seeds": SEEDS,
        "uplift_floor_pct": UPLIFT_FLOOR_PCT,
        "uplift_ceiling_pct": UPLIFT_CEILING_PCT,
        "engine_version": settings.engine_version,
        "matrix_version": matrix.matrix_version,
        **sweep,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2)
        f.write("\n")


def main() -> None:
    matrix = load_decision_matrix()
    sweep = run_sweep(SEEDS, matrix)
    print_sweep_report(sweep)
    write_fixture(sweep, matrix)
    print(f"\nFixture written -> {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
