"""M2 -- reason-code sampling distribution.

ASSUMPTION, not measurement. Built from the PRD's original 8-code
illustrative distribution (sec. M2), extended to all 40 codes now in
decision_matrix.yaml. See DECISIONS.md for the full reasoning.
"""

from app.matrix import DecisionMatrix

# Bucket-level shares -- explicit instruction (2026-09-03), overriding the
# bucket totals the PRD's original 8-code table implied (which had B1=0%
# and B4=0%, since neither bucket had a real reason code yet at that time).
BUCKET_SHARE = {
    "B2_BALANCE": 0.40,
    "B3_TRANSIENT": 0.30,
    "B5_DEAD": 0.15,
    "B1_CONGESTION": 0.10,
    "B4_STRUCTURAL": 0.05,
}

# Codes present in the PRD's original 8-code illustrative distribution keep
# their original *relative* weight within their bucket (rescaled to fit
# the bucket share above, which shrank B3 and B5 relative to the PRD's
# implied totals). The PRD's single lumped "mandate revoked/expired 10%"
# category is split evenly across the two placeholder codes that now
# represent it.
HEADLINE_RELATIVE_WEIGHT = {
    "gateway_technical_error": 20,
    "payment_failed": 12,
    "authorisation_declined_by_psp": 8,
    "payment_timed_out": 5,
    "mandate_revoked_by_customer": 5,
    "mandate_expired": 5,
    "card_expired": 3,
    "payment_risk_check_failed": 2,
}

# Within a bucket, headline codes (above) take this fraction of the
# bucket's total share; every other code in that bucket splits whatever
# remains evenly, since there's no PRD precedent or real distributional
# data to differentiate them further. A bucket with no headline codes at
# all (B1_CONGESTION, B4_STRUCTURAL) puts its entire share into the even
# split automatically -- no special-casing needed.
HEADLINE_SHARE_OF_BUCKET = 0.8


def build_distribution(matrix: DecisionMatrix) -> dict[str, float]:
    """reason_code -> sampling probability. Sums to 1.0 by construction."""
    by_bucket: dict[str, list[str]] = {}
    for reason, play in matrix.reason_codes.items():
        by_bucket.setdefault(play["bucket"], []).append(reason)

    weights: dict[str, float] = {}
    for bucket, codes in by_bucket.items():
        bucket_share = BUCKET_SHARE.get(bucket)
        if bucket_share is None:
            continue  # no target for this bucket (e.g. B_UNKNOWN has no reason_codes entries)

        headline = sorted(c for c in codes if c in HEADLINE_RELATIVE_WEIGHT)
        tail = sorted(c for c in codes if c not in HEADLINE_RELATIVE_WEIGHT)

        if headline:
            headline_total = bucket_share * HEADLINE_SHARE_OF_BUCKET
            tail_total = bucket_share - headline_total
            if not tail:
                # nothing to give the reserved slice to -- keep it all headline
                headline_total, tail_total = bucket_share, 0.0
            weight_sum = sum(HEADLINE_RELATIVE_WEIGHT[c] for c in headline)
            for c in headline:
                weights[c] = headline_total * (HEADLINE_RELATIVE_WEIGHT[c] / weight_sum)
        else:
            tail_total = bucket_share

        if tail:
            each = tail_total / len(tail)
            for c in tail:
                weights[c] = each

    total = sum(weights.values())
    assert abs(total - 1.0) < 1e-6, f"distribution weights sum to {total}, not 1.0"
    return weights
