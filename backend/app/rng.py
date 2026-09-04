"""Deterministic, independent per-draw RNG streams.

A single shared `random.Random` consumed sequentially across many draws
(the pattern this replaces -- see DECISIONS.md, "Nudge acceptance wired
into $-recovery", where adding one new draw type shifted baseline's own
Rs-recovered figure even though baseline itself didn't change) has a
defensibility problem: inserting a NEW draw anywhere upstream in that
sequence shifts the RNG state for every draw that comes after it, for
BOTH arms of the comparison, even when nothing about the thing being
drawn changed. That makes "the baseline recovers Rs.X" not a stable fact
-- it moves when unrelated agent-side code changes.

`deterministic_random(seed, *parts)` derives a fresh, independent
`random.Random` from a stable hash of `seed` and `parts` -- same inputs
always produce the same stream (reproducible), different inputs produce
statistically independent streams regardless of what else runs before or
after (there's no shared mutable state left to perturb). Callers key each
draw by everything that should make it distinct, e.g.
`deterministic_random(seed, event_id, "retry", bucket, context_state)` --
so adding a brand-new draw type elsewhere in the codebase literally
cannot change what this call returns; it depends on nothing but its own
inputs.

Uses SHA-256, not Python's builtin `hash()` -- `hash()` on a `str` is
randomized per-process (`PYTHONHASHSEED`) unless explicitly disabled, so
it is NOT reproducible across runs/processes/machines. SHA-256 is.
"""

import hashlib
import random


def deterministic_random(seed: "int | random.Random", *parts: str) -> random.Random:
    """A fresh `random.Random`, deterministically seeded from `seed` and
    `parts`.

    `seed` is normally an int -- the production path, where every
    distinct `parts` tuple gets its own independent stream. It may also
    be an existing `random.Random` instance, used as-is (ignoring
    `parts`) -- a test-only escape hatch: tests that need to force a
    specific outcome (e.g. "this draw always succeeds") can inject a
    rigged `random.Random` subclass directly rather than needing to
    reverse-engineer an integer seed that happens to produce the desired
    float. Production code always passes an int.
    """
    if isinstance(seed, random.Random):
        return seed
    digest = hashlib.sha256(":".join(str(p) for p in (seed, *parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))
