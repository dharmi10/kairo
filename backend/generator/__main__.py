"""CLI: python -m generator --count 500 --seed 42

Generates a reproducible synthetic batch of failed-mandate events, writes
it to a JSON file, and prints summary tables (reason code, merchant
category, ground-truth bucket, % in the NPCI restricted window).
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from app.config import settings
from app.matrix import load_decision_matrix
from generator.generate import generate_batch
from generator.oracle import true_bucket

DEFAULT_OUT = Path(__file__).resolve().parent / "output" / "events_seed{seed}.json"


def _print_table(title: str, rows: list[tuple[str, int, float]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    label_width = max((len(r[0]) for r in rows), default=10)
    for label, count, pct in rows:
        print(f"  {label:<{label_width}}  {count:>5}   {pct:5.1f}%")


def print_summary(events: list[dict], matrix) -> None:
    total = len(events)

    by_reason = Counter(e["error"]["reason"] for e in events)
    _print_table(
        "Distribution by reason code",
        sorted(
            [(reason, n, 100 * n / total) for reason, n in by_reason.items()],
            key=lambda r: -r[1],
        ),
    )

    by_category = Counter(e["merchant_category"] for e in events)
    _print_table(
        "Distribution by merchant category",
        sorted(
            [(cat, n, 100 * n / total) for cat, n in by_category.items()],
            key=lambda r: -r[1],
        ),
    )

    by_bucket = Counter(true_bucket(e, matrix) for e in events)
    _print_table(
        "Ground-truth bucket distribution (ORACLE ONLY -- not the classifier)",
        sorted(
            [(bucket, n, 100 * n / total) for bucket, n in by_bucket.items()],
            key=lambda r: -r[1],
        ),
    )

    restricted = sum(
        1
        for e in events
        if settings.npci_restricted_start_hour <= e["failed_at"].hour < settings.npci_restricted_end_hour
    )
    print(f"\n% of debits in the NPCI restricted window (10:00-13:00 IST): {100 * restricted / total:.1f}%  (target: 35.0%)")


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {obj!r}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m generator")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    matrix = load_decision_matrix()
    events = generate_batch(args.count, args.seed, matrix)

    out_path = args.out or Path(str(DEFAULT_OUT).format(seed=args.seed))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, default=_json_default)

    print(f"Generated {len(events)} events (seed={args.seed}) -> {out_path}")
    print_summary(events, matrix)


if __name__ == "__main__":
    main()
