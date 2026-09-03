"""Run classify() + evaluate_policy() over the M2 batch and report the
verdict distribution. Run as: python -m policy.report

Each event gets a FRESH mandate_history (0 attempts, 0 contacts, no prior
attempt, no prior-cycle failure, cycle just started at this event's
failed_at) -- that's the honest state of the data M2 actually produces:
500 independent first failures, not a retry sequence. Multi-attempt
history is exercised in tests/test_policy.py's simulated-cycle tests, not
here.
"""

from collections import Counter, defaultdict

from app.matrix import load_decision_matrix
from classifier.classify import classify
from generator.generate import generate_batch
from policy.policy import evaluate_policy


def fresh_mandate_history(event: dict) -> dict:
    return {
        "cycle_started_at": event["failed_at"],
        "total_retry_attempts": 0,
        "total_contacts_sent": 0,
        "last_attempt_at": None,
        "prior_cycle_failed": False,
    }


def main() -> None:
    matrix = load_decision_matrix()
    events = generate_batch(500, 42, matrix)

    verdict_counts = Counter()
    reason_counts = Counter()
    by_bucket_verdict = defaultdict(Counter)

    for event in events:
        classification = classify(event, matrix)
        verdict, reasons = evaluate_policy(event, classification, fresh_mandate_history(event))
        verdict_counts[verdict] += 1
        by_bucket_verdict[classification["bucket"]][verdict] += 1
        for r in reasons:
            reason_counts[r] += 1

    total = len(events)
    print(f"Verdict distribution (n={total}, fresh-cycle mandate_history for every event)")
    print("=" * 70)
    for verdict, n in verdict_counts.most_common():
        print(f"  {verdict:<10} {n:>4}   {100*n/total:5.1f}%")

    print("\nVerdict by classified bucket:")
    print("-" * 70)
    buckets = sorted(by_bucket_verdict.keys())
    verdicts = ["ALLOW", "BLOCK", "ESCALATE"]
    header = "bucket".ljust(16) + "".join(v.ljust(12) for v in verdicts) + "total"
    print(header)
    for b in buckets:
        row = by_bucket_verdict[b]
        counts = "".join(str(row.get(v, 0)).ljust(12) for v in verdicts)
        print(f"{b:<16}{counts}{sum(row.values())}")

    print("\nFiring reasons (why -- across all 500 decisions, pass + fail markers):")
    print("-" * 70)
    for reason, n in reason_counts.most_common():
        print(f"  {reason:<32} {n:>4}   {100*n/total:5.1f}%")


if __name__ == "__main__":
    main()
