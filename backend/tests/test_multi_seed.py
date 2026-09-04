"""Tests for metrics/multi_seed.py -- the seed sweep and its JSON
fixture. The fixture is what the M8 dashboard's 20-seed range panel
reads (see DECISIONS.md, "M8 dashboard: precomputed fixture, not a live
sweep endpoint") -- these tests cover the sweep's own arithmetic and the
fixture's shape, using a small 2-3 seed subset rather than the full
SEEDS list, purely for test speed (the sweep logic doesn't care how many
seeds it's given).
"""

import json

import pytest

from app.matrix import load_decision_matrix
from metrics.multi_seed import run_sweep, write_fixture


@pytest.fixture(scope="module")
def matrix():
    return load_decision_matrix()


def test_run_sweep_computes_summary_stats_matching_per_seed_rows(matrix):
    sweep = run_sweep([1, 2, 3], matrix)
    assert len(sweep["per_seed"]) == 3
    uplifts = [row["rs_uplift_pct"] for row in sweep["per_seed"]]
    assert sweep["rs_uplift_pct"]["min"] == min(uplifts)
    assert sweep["rs_uplift_pct"]["max"] == max(uplifts)
    assert sweep["rs_uplift_pct"]["mean"] == pytest.approx(sum(uplifts) / len(uplifts))


def test_run_sweep_is_reproducible(matrix):
    """Same property the rest of the simulation has (app/rng.py) --
    re-running the sweep with the same seeds must reproduce it exactly,
    since a stale-looking fixture should be safe to regenerate."""
    a = run_sweep([1, 2], matrix)
    b = run_sweep([1, 2], matrix)
    assert a["per_seed"] == b["per_seed"]


def test_run_sweep_floor_and_ceiling_counts_partition_correctly(matrix):
    sweep = run_sweep([1, 2, 3, 4, 5], matrix)
    below = sweep["seeds_below_floor"]
    above = sweep["seeds_above_ceiling"]
    assert below["count"] == len(below["seeds"])
    assert above["count"] == len(above["seeds"])
    # a seed can't be both below the floor and above the ceiling
    assert not set(below["seeds"]) & set(above["seeds"])


def test_write_fixture_produces_valid_json_with_expected_shape(matrix, tmp_path):
    sweep = run_sweep([1, 2], matrix)
    out_path = tmp_path / "fixture.json"

    write_fixture(sweep, matrix, out_path)

    with out_path.open(encoding="utf-8") as f:
        fixture = json.load(f)

    assert fixture["_generated_by"] == "python -m metrics.multi_seed"
    assert "regenerate" in fixture["_note"].lower()  # documents it's not a magic file, per the user
    assert fixture["matrix_version"] == matrix.matrix_version
    assert len(fixture["per_seed"]) == 2
    for key in ("rs_uplift_pct", "recovery_rate_delta_points", "seeds_below_floor", "seeds_above_ceiling"):
        assert key in fixture
