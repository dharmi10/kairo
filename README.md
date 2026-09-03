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

### Ground truth is decided at generation time, hidden from the classifier, and noisy on purpose

Every generated event carries a hidden `_true_bucket` field, stamped once
by the generator (`generator/generate.py::_decide_true_bucket`) — the
classifier (`classifier/classify.py`) never reads it
(`tests/test_classifier.py::test_classify_never_reads_true_bucket` proves
this directly, not just by inspection).

**Why this needed fixing:** the first version of this system derived
ground truth *on the fly*, at grading time, from the exact same
`congestion_override` condition the classifier itself uses to decide its
guess. The two could never disagree on any event with `attempt_number ==
1` (all of the generator's output) — grading measured "does the
classifier's config match the oracle's config", which is guaranteed to be
100%, not "does the classifier detect anything". Ground truth is now
decided once, independently, with deliberate noise the classifier has no
access to.

**The noise, ASSUMPTION values (`generator/generate.py`):**

| Case | Rate | Effect |
|---|---|---|
| In the restricted window, technical reason (`gateway_technical_error` / `payment_failed`), no balance-failure history — but NOT actually congestion | 15% | Classifier over-calls congestion (false positive) |
| Outside the restricted window, same reason codes, no balance-failure history — but IS actually congestion (NPCI congestion isn't perfectly bounded by the window) | 10% | Classifier misses congestion (false negative) |

A false positive flips to `B3_TRANSIENT` (60%) or `B2_BALANCE` (40%) —
another undated assumption; there's no data to weight this more precisely.
Noise is deliberately scoped to the two reason codes
`congestion_override` already recognizes as congestion-eligible, not
broadened to every B3-bucketed technical code — the more conservative
reading of "technical reason".

**Result on seed=42, n=500: 98.0% overall accuracy — higher than a
naive 15%/10% reading might suggest, and that's dilution, not a weak
test.** Noise only touches the congestion-boundary subset (~85 of 500
events are even eligible); a 15%/10% *within-subset* error rate works out
to about 10 misclassified events overall, or 2%. The metric that actually
measures detection quality is B1_CONGESTION's precision (89.7%) and
recall (95.3%) specifically — that's where the real inference difficulty
lives, and it's diluted away by the other four buckets (which have zero
injected noise) if you only look at the aggregate number. Run
`python -m classifier.grade` for the full per-bucket breakdown and
confusion matrix.

### Amount distribution and the escalation threshold have to be set together

`generator/generate.py` samples each event's `amount_inr` from a
per-category **log-normal** distribution, not a uniform band. This
replaced an earlier uniform-band version specifically because it broke
`policy.py`'s high-value escalation rule (see below) — the fix and the
reasoning are recorded together because neither number means anything on
its own.

**Targets (ASSUMPTION, ~median / ~P99 tail), `AMOUNT_DISTRIBUTION_INR`:**

| Category | Median | ~P99 tail |
|---|---|---|
| OTT | Rs.300 | Rs.1,500 |
| UTILITY | Rs.900 | Rs.5,000 |
| SIP | Rs.2,000 | Rs.25,000 |
| INSURANCE | Rs.3,000 | Rs.30,000 |
| EMI | Rs.6,000 | Rs.40,000 |

Each category's log-normal `sigma` is *derived* from its median/tail
ratio (`sigma = ln(tail / median) / 2.326`, the z-score of the 99th
percentile), not chosen independently — the median and tail above are the
only real inputs. Actual medians on seed=42 land close to target (OTT 331,
UTILITY 1066, SIP 2072, INSURANCE 3141, EMI 5879).

**Why this needed fixing:** the original version sampled *uniformly*
across wide bands (e.g. SIP: Rs.500–50,000). A uniform distribution over
a wide band puts far more mass above any reasonable threshold than real
payment amounts do — real UPI Autopay traffic is heavily right-skewed,
mostly small debits with a thin tail of large ones. Against the original
Rs.5,000 high-value escalation threshold, that uniform sampling drove
**54% of the batch to ESCALATE** — an "autonomous agent" that hands over
half its decisions to a human isn't meaningfully autonomous, which
undercuts this project's entire premise. The fix has two parts, and both
were necessary: the distribution needed to actually resemble real traffic
(log-normal, not uniform), *and* `high_value_threshold_inr` was raised
from Rs.5,000 to **Rs.10,000** (`app/config.py`) to match. **Result on
seed=42: ESCALATE = 10.0%** (ALLOW 78.4%, BLOCK 11.6%) — inside the
targeted 10–15% band on the first run with these values.

**The general rule this leaves behind:** the escalation threshold and the
amount distribution are not independent knobs. A threshold is only
"high-value" relative to what's actually below it. Changing either one
without checking the other can silently turn "escalate the rare big
payment" into "escalate over half of everything" without any single line
of code being wrong — run `python -m policy.report` after touching either
and check the aggregate ESCALATE rate before calling it done.

**Regulatory cross-check (reported, not assumed into the numbers above):**
NPCI/RBI's Additional Factor Authentication (AFA) exemption threshold for
UPI Autopay / e-mandate recurring debits. Multiple 2026 financial-news
sources describe an RBI e-mandate framework circular dated **21 April
2026** setting the general AFA-exempt limit at **Rs.15,000 per
transaction**, with an enhanced **Rs.1,00,000 exemption** specifically for
insurance premiums, mutual fund subscriptions, and credit card bill
payments — and recurring EMI auto-debits via e-mandates reportedly exempt
from the newer 2FA digital-lending rules entirely, to avoid disrupting
repayment schedules. This is secondary reporting (not fetched from RBI's
own circular text), but consistent across independent sources. All of the
table above's P99 tails sit comfortably under these limits (EMI's 40k and
SIP/INSURANCE's 25-30k vs. the 1L enhanced exemption; OTT/UTILITY
trivially under the general 15k) — worth citing in the pitch as
supporting context for why these amount assumptions are realistic, with
that sourcing caveat stated plainly if asked.

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
