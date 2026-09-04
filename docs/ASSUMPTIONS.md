# Assumptions & implementation reference

This file holds everything the top-level [`README.md`](../README.md) is
too tight to carry: every simulation assumption with its reasoning (per
the project's honesty requirements — [`docs/PRD-mandate-recovery-agent.md`](PRD-mandate-recovery-agent.md)
sec. 11), the RNG and multi-seed methodology behind the headline numbers,
and implementation-level detail for the API, the explanation layer, and
the dashboard that a two-minute read has no room for. If a number below
isn't backed by a cited source, treat it as a documented modelling
choice, not a measurement — [`config/decision_matrix.yaml`](../backend/config/decision_matrix.yaml)
and [`DECISIONS.md`](../DECISIONS.md) carry the same distinction for the
reason-code matrix and the full build history respectively.

- [Simulation assumptions](#simulation-assumptions)
- [The API — implementation notes](#the-api--implementation-notes)
- [The explanation layer (M7) — implementation notes](#the-explanation-layer-m7--implementation-notes)
- [The dashboard — implementation notes](#the-dashboard--implementation-notes)

---

## Simulation assumptions

### Timestamp distribution: 35% of debits fall in the NPCI restricted window

`generator/generate.py::_sample_failed_at` draws each event's failure
hour from a per-event coin flip: with probability `RESTRICTED_WINDOW_SHARE
= 0.35`, the hour is uniform within `10:00–13:00`; otherwise it's uniform
over every other hour. PRD sec. M2, verbatim: *"35% of original debits
fall inside the 10AM–1PM restricted window (this is what makes the
congestion USP measurable)."* Not derived from any real Razorpay traffic
data — Razorpay doesn't publish an hour-of-day distribution — chosen
specifically to make B1_CONGESTION a large enough slice of the batch
(65/500 ≈ 13% on seed 42, after the classifier's own false-positive/
false-negative noise) that the congestion story is demonstrable without
dominating every other bucket.

### NPCI window timings — independently checked, not just assumed

PRD sec. 11.3 requires verifying the NPCI restricted-window timing is
still current as of the demo date, not carried over unchecked from
whenever the PRD was written. Checked via web search during this build
(2026-09-04, one day before the PRD's stated deadline): NPCI's traffic
management rules for UPI AutoPay, which took effect **May 2026**,
restrict automated recurring-mandate execution during **10:00–13:00
IST**, directing banks to schedule background debits before 10:00,
between 13:00–17:00, or after 21:30 instead — corroborated independently
of Razorpay's own docs (Republic World, *"UPI AutoPay Failure: Why Your
Morning EMIs and SIPs are Failing in May 2026"*,
<https://www.republicworld.com/business/upi-autopay-failure-morning-peak-hours-npci-new-rules-2026>).
This matches `app/config.py`'s `npci_restricted_start_hour=10` /
`npci_restricted_end_hour=13` exactly — no code change was needed, but
the check itself hadn't been done until now, and "we didn't check" is a
different, weaker claim than "we checked and it's still right." A
single web search is not the same rigour as reading NPCI's own circular
directly; treat this as corroborated, not primary-source-verified.

### The ground-truth oracle is conditional, not independent-per-attempt

The synthetic generator's oracle (`generator/oracle.py`) computes
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
| B4_STRUCTURAL | any retry, any timing | 0.20 |
| B5_DEAD | any retry, any timing | 0.0 |

These are documented assumptions calibrated to keep the simulated uplift
in a defensible range, not measured real-world figures — Razorpay does not
publish retry-success-by-context data. B3 is a deliberate exception where
the baseline is expected to do reasonably well (time genuinely resolves a
transient technical hiccup); Kairo's advantage there is claimed to be
**time-to-recovery**, not eventual recovery rate.

**B4_STRUCTURAL's `p=0.20` is, by this project's own admission, the
least-scrutinised number in this table** — carried over unchanged from
the PRD's original spec with no bucket-specific reasoning ever recorded.
Every other row has at least a stated intuition (balance timing
correlates with payday, congestion correlates with the restricted
window); B4's does not. Flagged, not fixed.

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

### Human review of escalated events

Earlier builds of the M6 comparison modelled an `ESCALATE` verdict as a
dead end — Rs.0 recovered, forever. That's not what escalation means in
reality: a human reviews the queue and approves or rejects the case,
usually within a business day. Modelling it as permanent loss made
governance look like pure cost in the $-recovered comparison, and
penalized the agent for a safety property (routing genuinely risky/
high-value/ambiguous cases to a person) that the blind baseline doesn't
have at all.

The simulation (`executor/simulate.py`) now models human review as a
**delay-and-filter**, not a black hole:

- An escalated event is reviewed **12 hours** after it fires (ASSUMPTION
  — "a merchant checks the queue within a business day", not an instant
  automated response).
- **~70%** of reviewed cases are **approved** (ASSUMPTION — no data to
  calibrate this against; picked to be a plausible human-in-the-loop
  approval rate, not a measurement) and then proceed through the **same**
  recovery play as any other event — same classification, same matrix
  lookup, same oracle, same `context_outcomes` cache. No special
  treatment once approved.
- The remaining **~30%** are **rejected** and end the cycle
  not-recovered.

Both numbers live in `executor/simulate.py` as
`HUMAN_REVIEW_DELAY_HOURS` / `HUMAN_REVIEW_APPROVAL_RATE`, next to the
oracle's own assumption block, for the same reason: so they're auditable
in one place rather than buried in control flow. `python -m
metrics.report` prints how many events were escalated and what fraction
were approved, so the 70% target can be checked against the actual run.
A genuine governance stop that isn't about "does a human need to look at
this" — the attempt cap, cooling-off, recovery-cycle expiry, or a hard
decline — still applies after an approval; the human's approval covers
the escalation reason (high value, risk flag, repeat offender,
low-confidence classification), not those independent rules. See
`executor/executor.py::resolve_action`'s `human_approved` parameter.

**Effect on the headline uplift:** this changed the number quite a lot,
because escalation correlates heavily with `high_value_amount` — the
events that were previously locked out of the agent's $-recovered total
entirely were disproportionately the *largest* payments in the batch.
With human review modelled (and, at the time, nudge acceptance still
unwired — see below), full-population Rs-recovered uplift moved from
**-17.4%** (the literal old number, escalation as black hole) to
**+50.1%**. That number was superseded the same day once nudge
acceptance was wired in too — see the next section and `DECISIONS.md`
for the current figure and the full history of both changes.

### Nudge acceptance is wired into recovery

A `NUDGE_SENT` action (re-authorisation for a dead instrument, a
customer-initiated link for an OTP/limit failure, re-engagement after a
cancelled payment) used to be worth Rs.0 in the $-recovered comparison,
even though `generator/oracle.py` already carried
`NUDGE_ACCEPTANCE_PROBABILITIES` (0.30 for a B5 reauth nudge, 0.45 for a
B4 channel-switch nudge) as an unused, documented extension point. That
meant the agent got **no credit for choosing the right action** — every
nudge scored zero while every baseline blind retry could still win,
which isn't conservatism, it's a hole in the metric. B5_DEAD in
particular sends nothing but reauth nudges (retrying a dead instrument
is never attempted, by design) and was scoring a flat 0% recovery as a
result, regardless of how many customers would realistically reauth.

**Now wired in** (`executor/simulate.py`, `generator/oracle.py`):

- A `NUDGE_SENT` action draws acceptance from
  `NUDGE_ACCEPTANCE_PROBABILITIES`, keyed by the decision's
  `effective_bucket` (`NUDGE_ACCEPTANCE_KEY_BY_BUCKET`: B5_DEAD → the
  0.30 reauth figure, B4_STRUCTURAL → the 0.45 channel-switch figure —
  these are the only two buckets the matrix ever routes to a nudge
  today).
- The draw uses the **same correlated `context_outcomes` cache pattern**
  as `draw_retry_outcome` (`generator/oracle.py::draw_nudge_acceptance`),
  keyed as `(bucket, "nudge_acceptance")` — a marker that can't collide
  with a real `(bucket, context-state)` retry key. No special treatment
  versus how a retry outcome is drawn.
- An accepted nudge recovers the **full amount**, **24 hours** after it's
  sent (ASSUMPTION, `NUDGE_ACCEPTANCE_DELAY_HOURS` in
  `executor/simulate.py` — customers don't act on a nudge instantly; no
  data to calibrate this against, same honesty as every other timing
  assumption in this file). A rejected nudge ends the cycle
  not-recovered, same as a rejected human review.
- **Baseline gets no equivalent change, and that's stated explicitly, not
  silently absent:** baseline is retry-only (`baseline/baseline.py`) — it
  has no customer-facing action at all, so there is nothing to wire.
  `python -m metrics.report` prints this asymmetry directly ("Baseline
  sends 0 nudges... this is a real, structural asymmetry, not a metrics
  gap") so it's visible in the output, not just in this doc.

**Result, seed=42/20260903, full population, no exclusions: uplift moved
from +50.1% to +29.3%** (agent Rs.914,099 vs baseline Rs.706,980;
recovery rate 51.8% vs 40.6%) — inside `metrics/report.py`'s own 10-50%
"defensible band" check, not at its edge. B5_DEAD's agent recovery moved
from a flat 0% to 12.7% (43 reauth nudges sent, several accepted);
B4_STRUCTURAL's agent recovery moved from 0% to 43.5%, now clearly ahead
of baseline's 26.1% on that bucket instead of looking like a loss. At the
time, baseline's own Rs-recovered figure also moved for a reason that
turned out to be a defensibility problem in its own right — see the next
section, which replaced the mechanism responsible.

### Every draw is an independent, deterministic stream, not a shared sequential RNG

Every simulated run before this point shared ONE seeded `random.Random`,
consumed sequentially — the agent's draws for event 1, then baseline's
draws for event 1, then event 2's, and so on through the batch. That has
a real defensibility problem: inserting a NEW draw anywhere upstream in
that sequence (adding nudge acceptance, for instance) shifts the RNG
state for every draw after it — including **baseline's own retry
outcomes**, even though baseline's code hadn't changed at all. That
happened in practice: wiring nudge acceptance moved baseline's
Rs-recovered figure from 594,520 to 706,980 in the same run, for a reason
that had nothing to do with baseline. That makes "the baseline recovers
Rs.X" not a stable, citable fact — it moves when unrelated agent-side
code changes, which is exactly the kind of thing that invites "did you
just get lucky with the RNG" in a room full of judges.

**Fix (`app/rng.py::deterministic_random`):** every draw — human review
approval, a retry outcome, nudge acceptance — now seeds its own
independent `random.Random` from a SHA-256 hash of `(seed, event_id,
draw_type, ...context_key)`, computed only the first time that exact
draw is needed. Two calls with the same inputs always reproduce the same
result (reproducibility is unchanged); two different draws — a different
event, a different draw type, a different context — are statistically
independent regardless of what else ran before or after them, because
there is no shared mutable stream left to perturb. Adding a sixth draw
type next month cannot change a single existing number this document
cites. Verified directly:
`tests/test_simulation.py::test_agent_cycle_outcome_is_order_independent_across_events_with_same_int_seed`
simulates one event before and after an unrelated event, with the same
seed, and asserts the outcome, attempt count, and recovery time are all
identical either way — the property a shared sequential RNG could not
have guaranteed. `python -m metrics.report`'s aggregate `context_outcomes`
correlation mechanism (same event, same context ⇒ same outcome, both
arms) is unchanged; only where the entropy for a *new* key comes from
changed.

Because this changes what every draw actually produces (not just how
it's produced), the headline number moved again on the same seed:
**seed=42/20260903 now reads +60.1%** (agent Rs.1,020,617 vs baseline
Rs.637,405), a different sample than the +29.3% above, not a correction
of it — see `DECISIONS.md`. That's exactly why a single seed shouldn't
be the headline; see the next section.

### Multi-seed robustness: the uplift is a range, not a point estimate

A single seed's result invites "did you pick the seed that looked good?"
— a fair question, especially since seed 42 was already this project's
default everywhere else (the generator, the unit tests). `python -m
metrics.multi_seed` runs the full comparison across **20** independent
seeds (`[42, 7, 123, 2024, 55555, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 15,
17, 19, 21]` — the original 5 kept for continuity, extended to 20 because
n=5 gives a noisy estimate of the mean; chosen once and never adjusted
after seeing results) — each seed drives BOTH a fresh 500-event generated
batch AND the matching simulation draws, so each run is a genuinely
different synthetic population, not just different dice on the same 500
events.

**Result (n=20):**

| | min | mean | max | stdev |
|---|---|---|---|---|
| Rs recovered uplift | +2.3% | +24.3% | +68.5% | 15.2 points |
| Recovery rate delta | +12.0pt | +15.1pt | +18.6pt | — |

**5 of 20 seeds (25%) land below `metrics/report.py`'s own +10% "advantage
may not be materialising" floor** (seeds 2024, 4, 10, 19, 21 — +2.3% to
+8.6%), and 1 of 20 (5%) lands above the +50% "may be too generous"
ceiling (seed 3, +68.5%). At 25%, a sub-10% Rs-uplift outcome is **not a
rare tail case — it's a genuinely common result at this sample size**,
roughly 1 run in 4. That's the honest answer to "was seed 2024 unlucky":
no, seeds like it are common, not exceptional.

**Recovery rate is the stable, presentable number** — a comparatively
tight 12.0–18.6 point band across 20 independently-generated populations;
the agent reliably recovers more OF THE FAILURES, seed after seed. Rs
recovered uplift is not stable — the root cause, checked on seed 2024's
detailed report, is that a small number of high-value events (this
project's amount distribution is log-normal with a real tail, see
"Amount distribution and the escalation threshold" above) landing on
different sides of an otherwise-close race swings a dollar-weighted
metric substantially even when the underlying recovery-rate advantage
barely moves — B3_TRANSIENT's recovery RATE was close between arms on
that seed (70.8% agent vs 72.3% baseline) while its Rs-recovered was not
(Rs.293k vs Rs.417k). **Recovery rate is the number to lead with;
Rs-recovered uplift should always be cited as a range (+2% to +69%, mean
~+24%, ~1-in-4 chance of landing under +10%) with the recovery-rate delta
alongside it, never as a single seed's headline figure.** Not tuned to
narrow this spread — the spread itself is the honest finding, and it got
clearer, not smaller, going from n=5 to n=20.

### Reason codes

`backend/config/decision_matrix.yaml` cites its own sources inline —
`VERIFIED` entries link to the Razorpay docs page they came from (37 of
40 reason codes); `PLACEHOLDER` entries are explicitly marked as
unconfirmed synthetic assumptions for the UPI-mandate-lifecycle events
Razorpay doesn't publicly document a reason-code string for. Per PRD
sec. 11.2's honesty requirement, named explicitly here rather than left
to a grep of the YAML: the three PLACEHOLDER codes are
**`mandate_revoked_by_customer`**, **`mandate_expired`**, and
**`mandate_amount_exceeded`** — all three are UPI-mandate-lifecycle
events (revocation, expiry, amount-limit breach), not payment-gateway
failures, which is exactly the category Razorpay's public API/webhook
docs don't name a reason string for. Their bucket/play assignments
(B5_DEAD / B5_DEAD / ESCALATE_HUMAN respectively) are this project's own
defensible-by-construction judgement calls, not confirmed Razorpay
behaviour — correct the strings against Razorpay's docs before citing
them as real in a pitch. See `DECISIONS.md` for the full two-pass
verification history.

---

## The API — implementation notes

Seven endpoints (`backend/app/api.py`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/payment-failed` | Ingest one failure event |
| POST | `/simulate/run` | Run a full batch through agent + baseline |
| GET | `/results/summary` | The five headline metrics, both arms, plus the deltas |
| GET | `/results/by-bucket` | Per-bucket breakdown, both arms |
| GET | `/audit` | Paginated, filterable decision log |
| GET | `/audit/{decision_id}` | One decision, fully reconstructible |
| POST | `/reset` | Clear the DB, regenerate the batch |

### The webhook

Order of operations, which is the whole security story:

1. **Read the raw body.** Not the parsed JSON — re-serialising changes the
   bytes (key order, whitespace, unicode escaping) and the signature would
   never match again.
2. **Verify HMAC-SHA256 with `hmac.compare_digest`**, and **reject before
   parsing**. A plain `==` leaks timing information; parsing an unsigned
   payload hands an attacker a parser they haven't authenticated to.
3. **Dedupe on `event_id`.** A duplicate returns `200` with the
   *original* decision, never an error — Razorpay retries anything it
   thinks failed, so a 500 here causes a redelivery storm. This is
   idempotency layer 1; layer 2 is the `UNIQUE (mandate_id, cycle_id,
   retry_attempt_number)` constraint on `attempts`, which makes a
   duplicate debit attempt structurally impossible even if layer 1 is
   bypassed.
4. **Persist, decide, and commit in one transaction** — the raw event,
   the `Decision` audit record, any scheduled `Attempt`, and the advanced
   `MandateState` counters. All of it, or none of it. There is never a
   scheduled attempt without its audit record.
5. **Then explain** (see below), in a guard that cannot affect any of the
   above.

The response reports `ack_latency_ms` — the span from request arrival to
the durable decision, which is what
[`docs/architecture-and-security.md`](architecture-and-security.md)'s < 150 ms
budget applies to — separately from `explanation_latency_ms`. In this
build the explanation runs inside the request, so the client waits for
both; moving it behind a queue changes the response time and nothing else.

The mandate state is **durable**, so the policy engine's attempt cap,
cooling-off floor and contact limit hold across separate HTTP requests,
not just within one in-process simulation.

`POST /reset` regenerates the batch as **raw events only** — no decisions,
no metrics. `POST /simulate/run` clears, decides, and measures. They are
deliberately distinct: `/results/summary` reports *the* last run, and
blending two batches' numbers would be worse than a resettable demo DB.

### Two wire shapes, one internal contract

The webhook accepts either body shape, signed and dispatched identically:

- **The real Razorpay envelope** — `{"event": "payment.failed", "payload":
  {"payment": {"entity": {...}}}, ...}` — what a live integration actually
  sends. `backend/app/razorpay_adapter.py` maps it onto the same internal
  event dict everything downstream already consumes. Verified against
  Razorpay's own docs (payment entity fields, the `X-Razorpay-Signature`
  header, and the *real* idempotency mechanism — an `X-Razorpay-Event-Id`
  **header**, not a body field, which corrected an earlier assumption in
  this project that `event_id` lived in the payload).
- **The flat `FailureEvent` shape** from PRD sec. 6 — what the synthetic
  generator emits and every test drives directly. Kept working because it
  remains the project's one internal contract past this endpoint.

Razorpay's payment entity has no field for `mandate_id`, `customer_id`,
`merchant_category`, `cycle_id`, or this project's own `customer_history`
signals — there's no public "which UPI AutoPay mandate is this" reference
on a payment. The adapter reads them from the entity's own `notes` field,
which Razorpay reserves for merchant-supplied key/value metadata; since
real `notes` values are flat strings, `customer_history` travels as a
JSON-encoded string under `notes.customer_history`.

**Data minimisation on the real envelope (P5):** a genuine payment entity
carries `vpa`, `email` and `contact` — real customer PII the flat shape
never had a field for. `customer_id` prefers `notes.customer_id`, falling
back to a SHA-256 hash of whichever of those three is present — never the
plaintext. Separately, the copy of the body persisted as `Event.raw_payload`
has all three redacted before it's written to disk; this happens *after*
HMAC verification, which always runs against Razorpay's true, untouched
bytes, so scrubbing never affects what was actually authenticated.

### What isn't built

Two controls from `docs/architecture-and-security.md` sec. 3.5 remain
[DESIGN]-only and are not implemented: **replay protection** (rejecting a
delivery timestamp skewed more than 5 minutes — the payload carries
`failed_at`, not a delivery time, so this needs a header we don't model)
and **per-merchant rate limiting**.

---

## The explanation layer (M7) — implementation notes

`python -m explain.demo` runs the whole thing and prints the evidence for
each claim below.

**The LLM never makes a decision. It writes a sentence about a decision
that has already been committed.** That is not a convention this codebase
follows carefully; it is a property of how the code is arranged:

- `executor/`, `policy/` and `classifier/` **do not import `explain`** —
  asserted by a test that walks their ASTs, so it stays true;
- every entry point in `explain/` takes a `Decision` row that is already
  durable;
- the layer writes exactly two columns, `explanation` and
  `explanation_source`. It cannot change a bucket, a verdict, an action, a
  schedule or an outcome, because it never writes those columns;
- the single call site catches every exception and rolls back only the
  explanation transaction.

Delete the entire `explain/` package and every decision the system makes
is byte-identical. There is a test for that too.

> **The async boundary is designed, not built.**
> `docs/architecture-and-security.md` sec. 4.1 describes a background worker
> filling `explanation` in after the fact. That queue is *not*
> implemented — the call runs synchronously right after the decision
> commit. The four properties above are what make it safe, and they hold
> identically whether the call happens 1 ms or 1 hour later. The queue is
> a latency optimisation, not the safety mechanism.

### No PII reaches the prompt

`FORBIDDEN_IN_PROMPT = {"customer_id", "vpa", "phone", "email",
"payment_id"}`, plus `amount_inr`, `mandate_id`, `event_id` and
`customer_history` on the same data-minimisation principle. Enforced in
two layers:

- the field-name filter from the architecture doc, kept verbatim;
- a **value-level scan of the rendered prompt string**, which is the layer
  that can actually fire — it catches a customer id that reached the
  prompt *inside an allowed field* (a signal string, a badly templated
  label), which a field-name filter structurally cannot see. It raises
  `PIILeakError`, an exception rather than an `assert`, because `assert`
  is compiled out under `python -O` and a control that vanishes under an
  optimisation flag is not a control.

`PIILeakError` is the one failure the layer does **not** swallow into the
template fallback. A leak is our bug, not a dependency being down.

The demo prints the exact prompt so the claim can be checked by reading it
rather than believing it.

### The prompt is narrower than the PRD's sketch — on purpose

The PRD asks for a prompt carrying the reason code, confidence, signals
and scheduled time, *and* for caching by `(bucket, action,
policy_verdict)`. Those are in conflict: all four of those fields vary
between decisions that share a cache key, so a cached sentence naming any
of them would state something **false** about the other decisions it gets
attached to — fabricated content in an append-only audit record.

So the prompt carries only the cache key and things derived from it. A
test asserts that two contexts sharing a key render byte-identical
prompts. The specifics aren't lost: they're in the audit record's own
structured fields, and the *template* path — rendered per decision, never
cached — does name the exact snapped retry time and the rules that fired.

### The cache

Measured on the seed-42, 500-event run:

| | |
|---|---|
| decisions written | 801 |
| distinct `(bucket, action, policy_verdict)` | 23 |
| API calls | 23 |
| calls avoided by the cache | 778 (97%) |

Widening the key to include the reason code and signals would take 23
distinct keys to 91 — still far from 801. It is a one-line change to
`CACHE_KEY_FIELDS` if the extra specificity is ever wanted.

### The fallback

Any failure — no API key, network down, timeout, rate limit, or a 200 that
came back with no usable text — returns a deterministic template sentence
instead. `python -m explain.demo` proves it by injecting a client that
raises `anthropic.APIConnectionError` on every call: 60 decisions, 60
templates, **zero empty explanations**. The demo must never break because
of a network error, and the audit record must never carry a blank
rationale.

Every explanation stores its provenance in `Decision.explanation_source`
(`llm` | `template`), so an auditor never has to infer it from prose
style.

> **The repo ships no API key**, so out of the box every explanation comes
> from the template path and zero API calls are made. `python -m
> explain.demo` exercises the LLM code path against a **stub whose
> sentences are hardcoded in `explain/demo.py`**, and says so in capitals
> every time it runs. For a genuine model-written sample, set
> `ANTHROPIC_API_KEY` and re-run. Stub output is not model output.

---

## The dashboard — implementation notes

React + Vite + Recharts, per PRD tech stack table.

```powershell
cd frontend
npm install
npm run dev      # http://localhost:5173
```

`npm run dev` and `npm run build` both run `scripts/sync-fixture.mjs`
first (see `predev`/`prebuild` in `package.json`) — see "The one
non-live number" below. `npm install` pulls exact pinned versions
(`package-lock.json` is committed): React 18.3.1, Recharts 2.15.4,
Vite 5.4.21 — one major behind the newest available at build time
(React 19 / Recharts 3 / Vite 8), a deliberate reliability-over-recency
choice. Confidence in Recharts 3's exact API surface is materially lower
than in 2.x's (extensively represented in training data), and a
buildathon demo has zero tolerance for a chart silently failing to
render on a prop the newer major renamed.

### Four sections, PRD M8, with the DECISIONS.md revisions applied

1. **Headline strip.** Largest text on the page — visible without
   scrolling — is the **live run's recovery-rate delta**, not ₹
   recovered. Per the multi-seed finding above: recovery rate is stable
   across 20 independently-generated populations (a 12.0–18.6pt band)
   while ₹ uplift swings widely on the same runs. The ₹ figures are
   still shown — agent, baseline, and the delta — but a fourth tile puts
   the fixture's 20-seed ₹-uplift *range* directly beside them, so the
   single-run ₹ number is never presented alone as though it were a
   fixed, precise claim.
2. **Comparison chart.** Recharts grouped bar, recovery rate % by bucket,
   agent vs. baseline. Two named series → categorical color (agent =
   palette slot 1 blue, baseline = slot 2 orange), the same pair used
   everywhere else in the dashboard. Validated against the dataviz
   skill's CVD/contrast gates before use (`node
   scripts/validate_palette.js "#2a78d6,#eb6834" --mode light`, and the
   dark pair — both pass every check).
3. **Governance panel.** Wasted attempts avoided, hard declines correctly
   suppressed (of N B5 events, 0 auto-retried — the number M4's tests
   already guarantee is always 0), customer contacts sent, and items
   escalated to a human. Every number is read straight off
   `/results/summary`, `/results/by-bucket`, or one filtered `/audit`
   count (`?action=HUMAN_QUEUE&limit=1`, whose `total` is the escalation
   count without fetching every row) — nothing computed client-side
   beyond the subtraction the API already did for
   `delta.wasted_attempts_avoided`.
4. **Audit trail table.** Paginated (20/page), filterable (bucket,
   action, verdict, outcome, and a decision/event-id substring search),
   and expandable per row to show signals, policy reasons, confidence,
   and the explanation with its provenance (`llm` / `template`). Kept
   both search and expand rather than cutting them — `GET /audit`
   already returns every field a row needs (see
   `app/api.py::_decision_summary`), so expand costs zero extra requests
   and the filters are query params the endpoint already accepted.
   Neither one was the expensive part of M8. What actually got
   simplified instead: no sort-by-column (fixed newest-first, matching
   the API's own ordering) and no CSV export.

### "Run simulation" does one live run; the 20-seed range never does

The button calls `POST /simulate/run` exactly once per click — a fresh
500-event batch, decided, measured, persisted. The 20-seed range shown
beside the headline is **never** recomputed by this button or by
anything else in the dashboard; it is read from a static asset.

### The one non-live number

Per `DECISIONS.md` ("M8 dashboard: precomputed fixture, not a live sweep
endpoint"), the 20-seed range is committed context, not something a
button should ever trigger — running it live would mean 20 full
500-event simulations (all of `backend/metrics/multi_seed.py`) on every
click, and it would make the "stable" claim itself re-computed on
demand, undermining the point of citing it as a fixed, already-audited
number.

`frontend/scripts/sync-fixture.mjs` copies
`backend/metrics/output/multi_seed_range.json` into `frontend/public/`
before every `dev`/`build`, so the copy can't go silently stale — there
is no separate manual step to forget, and regenerating the backend
fixture (`python -m metrics.multi_seed`) is picked up on the next
`npm run dev`. Every other number in the dashboard is fetched live from
the API on load and after each run; nothing else is hardcoded.

A page reload during a demo does not lose the current run: `SimulationRun`
rows are durable, so the dashboard reloads whatever the backend last
computed rather than requiring a fresh click every time. The "Run
simulation" button's contract — one live run, on click, never implicit —
is unaffected: nothing on mount ever calls `POST /simulate/run` itself.
