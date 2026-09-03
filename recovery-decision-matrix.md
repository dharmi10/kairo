# Recovery Decision Matrix
### The decision spine for the UPI Mandate Recovery Agent
**Razorpay Buildathon — AI Revenue Recovery track**

---

## How to read this document

Every failed payment event enters the agent carrying a Razorpay error object with three fields: `source`, `step`, and `reason`. This document maps each `reason` value to:

1. **Root cause bucket** — the real underlying problem
2. **Decline class** — soft (recoverable by retry) or hard (retry is futile)
3. **Recovery play** — what the agent actually executes
4. **Stopping rule** — bounded limits so the agent never spams or loops

This table is the single source of truth. The classifier reads it, the executor acts on it, the audit log records which row fired and why.

---

## Part 1 — The Root Cause Buckets

Five buckets. Every failure resolves into exactly one.

| # | Bucket | Class | Core idea | Recoverable? |
|---|---|---|---|---|
| B1 | **Congestion / timing** | Soft | Mandate fired inside an NPCI-restricted window | Yes — high confidence |
| B2 | **Balance timing** | Soft | Money isn't there *right now*, but will be | Yes — if timed to income |
| B3 | **Transient technical** | Soft | Bank/PSP/gateway hiccup, nothing structurally wrong | Yes — short retry |
| B4 | **Structural / limit** | Soft-ish | Payment path itself is constrained (limit, method, instrument) | Yes — via channel switch |
| B5 | **Dead instrument / mandate** | **Hard** | The authorisation itself is invalid or gone | **No — stop retrying** |

---

## Part 2 — The Master Mapping Table

### Verified Razorpay reason codes

These reason codes are documented by Razorpay. Build against these first.

| Razorpay `reason` | `source` | Bucket | Class | Recovery play | Max attempts | Stop condition |
|---|---|---|---|---|---|---|
| `gateway_technical_error` | gateway | B3 | Soft | Retry in 2–4 hrs, snapped to next safe NPCI window | 3 | 3 consecutive fails in 24h → escalate to notify |
| `insufficient_funds` | customer | B2 | Soft | Hold. Schedule retry at predicted income window (month-start / salary date). Send soft pre-debit nudge 12h before. | 2 | 2 fails across 2 income windows → pause mandate, request customer action |
| `payment_timed_out` | customer | B3 | Soft | Retry same day, next safe window | 2 | 2 fails → switch to customer-initiated link |
| `authorisation_declined_by_psp` | gateway | B3 | Soft | Retry after 4–6 hrs (PSP/VPA issue often transient). If repeat → treat as B4. | 2 | 2 fails → channel fallback |
| `card_expired` | customer | B5 | **Hard** | **No retry.** Immediate re-authorisation nudge with one-click update link. | **0** | Immediate stop |
| `debit_instrument_blocked` | customer | B5 | **Hard** | **No retry.** Notify customer, offer alternate method. | **0** | Immediate stop |
| `payment_risk_check_failed` | gateway | B5 | **Hard** | **No retry** — repeat attempts can worsen risk flags. Escalate to merchant review. | **0** | Immediate stop + human queue |
| `payment_cancelled` | customer | B4 | Soft | Customer intent unclear. Single re-engagement nudge, no silent retry. | 1 | 1 nudge, then stop |
| `authentication_failed` / `invalid_otp` | customer | B4 | Soft | Customer-initiated retry link (agent cannot fix OTP autonomously) | 1 | 1 nudge, then stop |
| `incorrect_cvv` | customer | B4 | Soft | Customer-initiated retry link | 1 | 1 nudge, then stop |
| `payment_failed` (generic bank decline) | gateway | B3 → escalate | Soft | Ambiguous by design — Razorpay notes banks often don't share the real reason. Retry once in a safe window; if it fails again, reclassify as B5 and stop. | 2 | 2 fails → treat as hard, notify |

> ⚠️ **Note on `payment_failed`:** Razorpay's docs explicitly say they may not have access to the specific failure reason because customer banks often don't provide it. This is your *hardest* and most interesting case — a genuinely ambiguous code. Handling it well (probabilistic reclassification after one attempt) is a strong thing to show a judge.

---

### UPI mandate–specific cases — **VERIFY BEFORE DEMO**

⚠️ **Important honesty flag:** I have not verified the exact reason-code strings Razorpay uses for UPI Autopay mandate lifecycle failures. The *behaviours* below are real; the code names are placeholders. Confirm the exact strings from Razorpay's UPI/Subscriptions error docs before you demo, or a judge who knows their API will catch it.

| Behaviour | Bucket | Class | Recovery play | Max attempts | Stop condition |
|---|---|---|---|---|---|
| Mandate revoked by customer | B5 | **Hard** | **No retry.** Re-authorisation request only. | **0** | Immediate stop |
| Mandate expired / past validity | B5 | **Hard** | **No retry.** New mandate creation flow. | **0** | Immediate stop |
| Debit amount exceeds mandate cap | B4 | Soft | No blind retry. Route to merchant: split charge or request mandate amendment. | 0 auto | Human decision required |
| Daily transaction limit breached | B4 | Soft | Retry next day, off-peak window | 2 | 2 fails → channel fallback |
| Debit fired in NPCI restricted window | **B1** | Soft | **Same-day retry, snapped into a safe execution window** | 2 | 2 fails → reclassify to B2/B3 |

---

## Part 3 — The NPCI Execution Window Logic (your #1 USP)

As of May 2026, NPCI's Traffic Management framework deprioritises automated mandates during the morning peak. Debits scheduled in the restricted band frequently take technical declines.

```
RESTRICTED  →  10:00 AM – 1:00 PM   (P2P priority — avoid mandates)

SAFE WINDOWS →  before 10:00 AM
                1:00 PM – 5:00 PM
                after 9:30 PM
```

### Window-snapping rule

Any retry the agent schedules must be snapped into a safe window before execution:

```python
def snap_to_safe_window(proposed_time):
    """Never let a retry land in the NPCI restricted band."""
    if 10 <= proposed_time.hour < 13:
        return proposed_time.replace(hour=13, minute=30)   # push to afternoon
    return proposed_time
```

### The congestion-detection heuristic

You won't get a "congestion" reason code from Razorpay — NPCI congestion surfaces as a generic technical decline. So infer it:

**Flag as B1 (congestion) if ALL of:**
- Original debit timestamp fell inside 10:00 AM – 1:00 PM
- Reason code is `gateway_technical_error` or generic `payment_failed`
- Customer has no recent history of insufficient-balance failures

This inference is the heart of your differentiation. Log it explicitly in the audit trail with a confidence score — it's what makes your decisions *explainable* rather than a black box.

---

## Part 4 — Global Governance Rules

These apply on top of every row above. This is the "bar" the brief explicitly asks for — most teams will skip it.

### Stopping rules
| Rule | Value | Rationale |
|---|---|---|
| Global max retry attempts per mandate cycle | **3** | Prevents attempt-burning and fraud-flag escalation |
| Minimum cooling-off between retries | **2 hours** | Avoids hammering the bank/PSP |
| Hard declines auto-retried | **Never** | Retrying a dead instrument can trigger fraud alerts |
| Max customer contacts per cycle | **2** | Anti-spam; protects merchant reputation |
| Recovery cycle window | **7 days** | After this, escalate to merchant, stop autonomous action |

### Compliant escalation — human-in-the-loop triggers
Route to merchant approval instead of auto-executing when **any** of:
- Transaction value exceeds a configurable high-value threshold
- Reason code is `payment_risk_check_failed` (risk-flagged)
- Mandate amendment or amount change is required
- The mandate has already failed in a prior cycle (repeat offender)

### Audit trail — log for every single decision
```json
{
  "event_id": "evt_xxx",
  "payment_id": "pay_xxx",
  "timestamp_failed": "2026-09-02T10:34:00+05:30",
  "raw_error": { "source": "gateway", "step": "...", "reason": "gateway_technical_error" },
  "classified_bucket": "B1_congestion",
  "classification_confidence": 0.82,
  "classification_signals": ["fired_in_restricted_window", "no_balance_failure_history"],
  "action_taken": "retry_scheduled",
  "action_detail": { "scheduled_for": "2026-09-02T13:30:00+05:30", "window": "safe_afternoon" },
  "attempt_number": 1,
  "stopping_rule_status": "within_limits",
  "human_approval_required": false,
  "outcome": "recovered",
  "amount_recovered_inr": 499
}
```

---

## Part 5 — The Baseline You're Beating

For your measured-recovery demo, implement Razorpay's current behaviour as the control arm:

> **Baseline:** payment fails → subscription moves to `pending` → single automatic retry the following day, same time, regardless of reason.

Run the identical batch through both arms.

### Metrics to report
| Metric | Why it matters |
|---|---|
| **₹ recovered** (agent vs baseline) | The headline number the brief demands |
| **Recovery rate %** (agent vs baseline) | Normalised comparison |
| **Wasted attempts avoided** | Hard declines the baseline retried pointlessly |
| **Time-to-recovery** (median hours) | Cash-flow benefit, not just totals |
| **Customer contacts sent** | Proves you're not spamming to win |

> **Benchmark for your pitch:** industry evidence suggests reason-aware smart retries recover meaningfully more revenue than static retries on identical failure volume — commonly cited in the 20–50% uplift range. Use this to sanity-check your simulated results. If your agent shows a 400% uplift, your simulation is unrealistic and a judge will spot it. Aim for a defensible, believable delta.

---

## Part 6 — Build Order

1. **This matrix → code.** Encode as a config/dict, not hardcoded if-statements. Judges notice when logic is data-driven and editable.
2. **Synthetic batch generator.** ~500 failed mandate events with realistic reason-code distribution, timestamps spread across restricted and safe windows, and customer balance histories.
3. **Classifier.** Maps raw error → bucket + confidence + signals.
4. **Executor.** Simulated clock; runs recovery plays, enforces stopping rules.
5. **Audit logger.** Every decision, as per the JSON schema above.
6. **Results dashboard.** Agent vs baseline, the five metrics.

---

## Part 7 — Before You Demo: verification checklist

- [ ] Confirm exact UPI mandate failure reason-code strings from Razorpay's Subscriptions/UPI error docs
- [ ] Confirm current NPCI execution-window timings are still in force
- [ ] Sanity-check that your synthetic reason-code distribution resembles plausible real-world proportions
- [ ] Make sure every claim on your slides is one you can point to a source for

**The rule:** if a judge asks "where did this number come from?" you must have an answer. One unsupported claim undoes three good ones.
