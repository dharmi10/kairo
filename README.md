# Kairo

UPI Mandate Recovery Agent — Razorpay Buildathon, AI Revenue Recovery track.

Razorpay's own failure diagnostics (`source`, `step`, `reason`) already tell
you why every mandate debit failed. Their recovery doesn't use that
information — every failure gets the same blind retry, regardless of cause.
Kairo reads those codes, routes each failure to a reason-appropriate
recovery play, stops when retrying is futile, and measures the difference
against Razorpay's actual current retry behaviour.

This README documents every simulation assumption and its source, per the
project's honesty requirements — if a number here isn't backed by a cited
source, treat it as a documented guess, not a measurement. See
[`DECISIONS.md`](DECISIONS.md) for the full chronological log of build
decisions and their reasoning.

## Modelling choices

### The ground-truth oracle is conditional, not independent-per-attempt

The synthetic generator's oracle (`generator/`, not yet built) computes
retry-success probability as a function of the **context at the specific
retry datetime** — never as a fixed per-reason probability drawn
independently at each attempt.

**Why this matters:** the baseline simulator (M6) replicates Razorpay's
real retry behaviour — 3 automatic retries at T+1/T+2/T+3 days, same time
of day as the original failure, every time. If each attempt drew success
independently from a fixed probability `p`, the cumulative chance of
recovery across 3 tries is `1 - (1-p)^3` — geometric compounding that
rewards the baseline purely for trying more times, even when nothing about
the underlying cause changed between attempts (e.g. all 3 retries landing
in the same NPCI-restricted hour, or all 3 landing before the customer's
payday). Real payment failures are correlated across nearby attempts
because the underlying cause — no money in the account, a congested
execution window, a down gateway — persists across them. They're repeated
observations of one latent state, not independent coin flips. Modelling
them as independent would manufacture an uplift number that doesn't
reflect anything real, and would specifically undermine this project's
own thesis (context-aware timing beats blind persistence) by making blind
persistence look artificially competitive.

**The oracle's probabilities** (`oracle(event, retry_datetime) -> p`, no
attempt-number term):

| Bucket | Condition | p |
|---|---|---|
| B2_BALANCE | retry before customer's `typical_credit_day` | 0.25 |
| B2_BALANCE | retry after `typical_credit_day` | 0.55 |
| B1_CONGESTION | retry lands in the NPCI restricted window (10:00–13:00 IST) | 0.35 |
| B1_CONGESTION | retry lands in a safe window | 0.70 |
| B3_TRANSIENT | retry under 2h after the original failure | 0.35 |
| B3_TRANSIENT | retry 2h+ after the original failure | 0.70 |
| B5_DEAD | any retry, any timing | 0.0 |

These are documented assumptions calibrated to keep the simulated uplift
in a defensible range, not measured real-world figures — Razorpay does not
publish retry-success-by-context data. B3 is a deliberate exception where
the baseline is expected to do reasonably well (time genuinely resolves a
transient technical hiccup); Kairo's advantage there is claimed to be
**time-to-recovery**, not eventual recovery rate.

### The Razorpay baseline is 3 retries, not 1

Razorpay's own [Payment Retries docs](https://razorpay.com/docs/payments/subscriptions/payment-retries/)
describe 3 automatic retries on T+1/T+2/T+3 days (same schedule for cards
and UPI) before a subscription moves to `halted` — not a single next-day
retry. The baseline simulator (M6) implements this real behaviour: 3
retries, same time of day as the original failure each time, no
reason-awareness, hard declines retried too.

### Reason codes

`backend/config/decision_matrix.yaml` cites its own sources inline —
`VERIFIED` entries link to the Razorpay docs page they came from,
`PLACEHOLDER` entries are explicitly marked as unconfirmed synthetic
assumptions for the UPI-mandate-lifecycle events Razorpay doesn't publicly
document a reason-code string for. See `DECISIONS.md` for the full
verification history.
