"""Grade the classifier against hidden ground truth (`event["_true_bucket"]`,
stamped by the generator at generation time -- see
generator/generate.py::_decide_true_bucket) across a full generated batch.
Run as: python -m classifier.grade

Resolution 2026-09-03: classify() and ground truth used to share the same
congestion_override lookup at grading time, which made batch accuracy a
config-agreement tautology, not a real test. Ground truth is now decided
once at generation time, with injected noise the classifier has no access
to (see generate.py's CONGESTION_FALSE_POSITIVE_RATE /
CONGESTION_FALSE_NEGATIVE_RATE) -- so this grading run measures actual
detection, not agreement with itself.

Also grades a second, hand-built set of attempt_number=2 events, since M2
never emits attempt_number > 1 and that's the one classifier path
(ambiguity-reclassification) with no coverage in the real batch otherwise.
"""

from collections import Counter, defaultdict
from datetime import datetime

from app.matrix import load_decision_matrix
from classifier.classify import classify
from generator.generate import generate_batch


def grade(events: list[dict], matrix) -> tuple[float, dict]:
    confusion: dict[str, Counter] = defaultdict(Counter)
    correct = 0
    for e in events:
        actual = e["_true_bucket"]
        predicted = classify(e, matrix)["bucket"]
        confusion[actual][predicted] += 1
        if predicted == actual:
            correct += 1
    accuracy = correct / len(events) if events else 0.0
    return accuracy, confusion


def print_report(title: str, events: list[dict], matrix) -> None:
    accuracy, confusion = grade(events, matrix)
    print(f"\n{title}")
    print("=" * len(title))
    print(f"n = {len(events)}, accuracy = {accuracy * 100:.1f}%")

    true_buckets = sorted(confusion.keys())
    predicted_buckets = sorted({p for row in confusion.values() for p in row})
    all_buckets = sorted(set(true_buckets) | set(predicted_buckets))

    if not all_buckets:
        return

    col_width = max(len(b) for b in all_buckets) + 2
    header = "true \\ predicted".ljust(22) + "".join(b.ljust(col_width) for b in all_buckets)
    print("\n" + header)
    for t in all_buckets:
        row = confusion.get(t, Counter())
        line = t.ljust(22) + "".join(str(row.get(p, 0)).ljust(col_width) for p in all_buckets)
        print(line)

    print("\nper-bucket precision / recall:")
    for b in all_buckets:
        true_positive = confusion.get(b, Counter()).get(b, 0)
        predicted_total = sum(confusion[t].get(b, 0) for t in all_buckets)
        true_total = sum(confusion.get(b, Counter()).values())
        precision = true_positive / predicted_total if predicted_total else float("nan")
        recall = true_positive / true_total if true_total else float("nan")
        print(f"  {b:<15} precision={precision*100:5.1f}%  recall={recall*100:5.1f}%  (n_true={true_total}, n_predicted={predicted_total})")

    misclassified = [
        (t, p, n) for t, row in confusion.items() for p, n in row.items() if p != t and n > 0
    ]
    if misclassified:
        print("\nsystematic misclassifications (true -> predicted: count):")
        for t, p, n in sorted(misclassified, key=lambda x: -x[2]):
            print(f"  {t} -> {p}: {n}")
    else:
        print("\nno misclassifications in this set.")


def _build_ambiguous_retry_events(matrix) -> list[dict]:
    """Hand-built events with attempt_number=2, one per ambiguous reason
    code, all placed outside the congestion window so only the
    ambiguity-reclassification path can fire. Ground truth is set by hand
    here (not via _decide_true_bucket) since these codes aren't
    congestion-eligible -- their true bucket is always just their
    YAML-declared nominal bucket, noise-free."""
    events = []
    ambiguous_reasons = [r for r, p in matrix.reason_codes.items() if p.get("ambiguous")]
    for i, reason in enumerate(ambiguous_reasons):
        play = matrix.reason_codes[reason]
        source = play["source"]
        events.append(
            {
                "event_id": f"synthetic_retry_{i}",
                "payment_id": f"pay_synthetic_{i}",
                "mandate_id": f"mandate_synthetic_{i}",
                "customer_id": f"cust_synthetic_{i}",
                "merchant_category": "OTT",
                "amount_inr": 499,
                "failed_at": datetime(2026, 8, 15, 15, 0, 0),  # 15:00 -- outside the 10-13 restricted window
                "cycle_id": f"mandate_synthetic_{i}_cycle1",
                "error": {
                    "code": "GATEWAY_ERROR" if source in ("gateway", "razorpay") else "BAD_REQUEST_ERROR",
                    "source": source,
                    "step": "payment_authorization",
                    "reason": reason,
                },
                "attempt_number": 2,  # <- the retry already failed once
                "customer_history": {
                    "prior_failures_90d": 1,
                    "prior_insufficient_funds_90d": 0,
                    "typical_credit_day": 15,
                    "mandate_age_days": 100,
                },
                "_true_bucket": play["bucket"],  # not congestion-eligible -- always the nominal bucket
            }
        )
    return events


def main() -> None:
    matrix = load_decision_matrix()

    batch = generate_batch(500, 42, matrix)
    print_report("Batch grading (M2 seed=42, n=500) -- classifier vs. hidden ground truth", batch, matrix)

    retry_events = _build_ambiguous_retry_events(matrix)
    print_report(
        "\nSecond-attempt grading (hand-built, attempt_number=2) -- the path M2's batch never exercises",
        retry_events,
        matrix,
    )
    print(
        "\nOn this second set, classifier and ground truth are EXPECTED to disagree: classify() "
        "reclassifies these to B5_DEAD (heuristic: one failed retry on an ambiguous code is treated as "
        "probably-dead), while the noise-free ground truth for these non-congestion-eligible codes stays "
        "at their nominal bucket (B3_TRANSIENT). That gap is intentional -- it's the classifier's real "
        "guess under genuine uncertainty, not an error to fix."
    )


if __name__ == "__main__":
    main()
