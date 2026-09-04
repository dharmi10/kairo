"""Unit tests for app/rng.py -- the independent, deterministic per-draw
RNG stream that replaced a single shared `random.Random` consumed
sequentially across the whole batch. See DECISIONS.md, "Independent
deterministic RNG streams" -- the property under test is: an unrelated
draw happening anywhere else (before, after, for a different event, of a
different type) must never change what THIS draw produces.
"""

import random

from app.rng import deterministic_random


def test_deterministic_random_reproducible():
    a = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    b = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    assert a.random() == b.random()


def test_deterministic_random_differs_across_parts():
    a = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    b = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "safe_window")
    assert a.random() != b.random()


def test_deterministic_random_differs_across_event_ids():
    a = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    b = deterministic_random(42, "evt_2", "retry", "B1_CONGESTION", "restricted_window")
    assert a.random() != b.random()


def test_deterministic_random_differs_across_seed():
    a = deterministic_random(42, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    b = deterministic_random(43, "evt_1", "retry", "B1_CONGESTION", "restricted_window")
    assert a.random() != b.random()


def test_deterministic_random_passes_through_an_existing_random_instance():
    """Test-only escape hatch: passing a live random.Random uses it
    directly, ignoring parts, so tests can force a specific outcome
    without reverse-engineering an integer seed."""
    rng = random.Random(1)
    returned = deterministic_random(rng, "anything", "at", "all")
    assert returned is rng


def test_deterministic_random_order_independent():
    """The whole point: deriving one stream must not depend on whether
    some unrelated stream was drawn first. Simulates "a new draw type was
    added upstream" by drawing unrelated streams first, then checking the
    stream under test still matches the same call made in isolation."""
    isolated = deterministic_random(42, "evt_1", "retry", "B3_TRANSIENT", "under_2h").random()

    deterministic_random(42, "evt_1", "human_review").random()  # unrelated draw type, same event
    deterministic_random(42, "evt_2", "nudge", "B5_DEAD").random()  # unrelated draw, different event
    after_unrelated_draws = deterministic_random(42, "evt_1", "retry", "B3_TRANSIENT", "under_2h").random()

    assert isolated == after_unrelated_draws
