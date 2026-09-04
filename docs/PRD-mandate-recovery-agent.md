# PRD — UPI Mandate Recovery Agent

> Original planning document, written before the build. Superseded in places by [`DECISIONS.md`](../DECISIONS.md), which logs every divergence and why.

**Project codename:** RetryIQ
**Track:** Razorpay Buildathon — AI Revenue Recovery
**Deadline:** 5 September 2026
**Build window:** ~2 days
**Author:** Dharmi Chauhan

> **Instruction to Claude Code:** Build strictly to this spec. Do not add features outside "In Scope". Prefer working end-to-end over polished-but-partial. Every module below has explicit acceptance criteria — treat those as the definition of done.

---

## 1. Problem Statement

India runs on UPI AutoPay. Over 120 million mandates are created monthly, and total mandates crossed 1.27 billion by late 2025. But over **20 million UPI AutoPay mandates are revoked every month**, largely due to insufficient balance at debit time. UPI AutoPay failure rates run structurally higher than cards (~8–15% vs ~2–3%) because each debit needs real-time bank approval.

Razorpay provides **excellent failure diagnostics** — every error carries `source`, `step`, and `reason`, telling you the who, where, and why. Their docs explicitly state these codes exist so merchants can *build their own logic and take remedial action*.

But Razorpay's own recovery is **flat**: payment fails → subscription goes `pending` → one automatic retry the next day. Same treatment regardless of reason. A transient bank glitch waits 24 hours unnecessarily. A dead mandate gets retried pointlessly.

**Compounding this (May 2026):** NPCI's Traffic Management framework deprioritises automated mandates during the 10 AM–1 PM peak, pushing them to off-peak windows. Blind next-day retries can land straight back in the congested window that killed the payment.

**The gap in one line:**
> The diagnosis exists. The intelligent response doesn't. We build the missing layer.

---

## 2. Solution Summary

An autonomous agent that ingests failed mandate events, classifies root cause from Razorpay's own error codes, executes a bounded recovery workflow, and reports measured money recovered against a baseline — with stopping rules, compliant escalation, and a full audit trail.

**Four-stage loop:** `DETECT → DIAGNOSE → DECIDE & EXECUTE → GOVERN & REPORT`

---

## 3. Scope

### In Scope (build these)
- Synthetic failed-mandate event generator with configurable, documented assumptions
- Rule-based classifier: Razorpay `reason` → root cause bucket + confidence + signals
- NPCI execution-window awareness and retry snapping
- Recovery executor with simulated clock
- Governance layer: stopping rules, escalation triggers, audit log
- Baseline simulator (Razorpay's current next-day blind retry)
- Comparison dashboard: agent vs baseline, five metrics
- LLM-assisted explanation layer (natural-language reason for each decision)

### Out of Scope (do NOT build)
- Real Razorpay API integration or live credentials
- Checkout abandonment, B2B receivables, invoice chasing
- Actual SMS/email/voice sending — simulate notifications only
- User authentication, multi-tenancy, deployment infra
- Model training. The classifier is rule-based by design; ML is not required and would be a time sink.

---

## 4. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | **Python 3.11 + FastAPI** | Familiar; webhook-shaped API fits the Razorpay integration story |
| Data | **SQLite via SQLAlchemy** | Zero setup, file-based, survives restarts |
| Simulation | Plain Python + `datetime` | No external scheduler needed |
| LLM layer | **Anthropic API (Claude)** | For explanation generation only, not classification |
| Frontend | **Single-page React (Vite) + Recharts** | Fast to build, renders the comparison charts |
| Config | **YAML** for the decision matrix | Judges notice data-driven logic over hardcoded ifs |

---

## 5. Architecture

```
                    ┌─────────────────────────┐
                    │  Synthetic Event Gen    │
                    │  (500 failed mandates)  │
                    └───────────┬─────────────┘
                                │
                   POST /webhook/payment-failed
                                │
        ┌───────────────────────▼───────────────────────┐
        │              INGESTION LAYER                  │
        │   validates + persists raw failure event      │
        └───────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────▼───────────────────────┐
        │              CLASSIFIER                       │
        │   reason code + context → bucket B1–B5        │
        │   emits: bucket, confidence, signals[]        │
        └───────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────▼───────────────────────┐
        │           POLICY ENGINE (governance)          │
        │   stopping rules │ attempt caps │ escalation  │
        │   → ALLOW / BLOCK / ESCALATE_TO_HUMAN         │
        └───────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────▼───────────────────────┐
        │              EXECUTOR                         │
        │   schedules retry (NPCI-window snapped)       │
        │   or sends nudge / stops / queues for human   │
        └───────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────▼───────────────────────┐
        │        AUDIT LOG  +  OUTCOME RESOLVER         │
        └───────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────▼───────────────────────┐
        │      DASHBOARD: agent vs baseline metrics     │
        └───────────────────────────────────────────────┘
```

**Design principle:** the LLM never makes the decision. Rules decide; the LLM explains. This is deliberate and should be stated in the pitch — it makes the system auditable and deterministic, which matters in fintech.

---

## 6. Data Models

### `FailureEvent`
```python
{
  "event_id": str,              # evt_xxxxx
  "payment_id": str,            # pay_xxxxx
  "mandate_id": str,            # mandate reference
  "customer_id": str,
  "merchant_category": str,     # OTT | SIP | EMI | UTILITY | INSURANCE
  "amount_inr": int,
  "failed_at": datetime,
  "error": {
      "code": str,              # BAD_REQUEST_ERROR | GATEWAY_ERROR
      "source": str,            # customer | business | gateway | razorpay
      "step": str,              # payment_authentication | payment_authorization
      "reason": str             # gateway_technical_error | insufficient_funds | ...
  },
  "attempt_number": int,
  "customer_history": {
      "prior_failures_90d": int,
      "prior_insufficient_funds_90d": int,
      "typical_credit_day": int,       # 1-28, simulated income date
      "mandate_age_days": int
  }
}
```

### `Decision` (one per agent action — this IS the audit record)
```python
{
  "decision_id": str,
  "event_id": str,
  "classified_bucket": str,          # B1_CONGESTION | B2_BALANCE | B3_TRANSIENT | B4_STRUCTURAL | B5_DEAD
  "confidence": float,               # 0.0–1.0
  "signals": list[str],              # ["fired_in_restricted_window", "no_balance_failure_history"]
  "policy_verdict": str,             # ALLOW | BLOCK | ESCALATE
  "policy_reasons": list[str],       # ["within_attempt_cap", "cooling_off_satisfied"]
  "action": str,                     # RETRY_SCHEDULED | NUDGE_SENT | STOPPED | HUMAN_QUEUE
  "scheduled_for": datetime | None,
  "window_snapped": bool,
  "explanation": str,                # LLM-generated, human-readable
  "outcome": str,                    # RECOVERED | FAILED | PENDING | NOT_ATTEMPTED
  "amount_recovered_inr": int
}
```

---

## 7. Modules & Acceptance Criteria

### M1 — Decision Matrix Config (`config/decision_matrix.yaml`)
Encode the full reason-code → bucket → play → limits mapping as YAML.

```yaml
buckets:
  B3_TRANSIENT:
    class: soft
    label: "Transient technical failure"
reason_codes:
  gateway_technical_error:
    bucket: B3_TRANSIENT
    action: RETRY
    delay_hours: 3
    max_attempts: 3
    snap_to_window: true
  insufficient_funds:
    bucket: B2_BALANCE
    action: RETRY_AT_INCOME_WINDOW
    max_attempts: 2
    snap_to_window: true
    pre_nudge_hours: 12
  card_expired:
    bucket: B5_DEAD
    action: NUDGE_REAUTH
    max_attempts: 0
  payment_risk_check_failed:
    bucket: B5_DEAD
    action: ESCALATE_HUMAN
    max_attempts: 0
```

**Acceptance:** all reason codes from the decision matrix doc are present; the app loads this file at startup and no decision logic is hardcoded elsewhere.

---

### M2 — Synthetic Event Generator (`generator/`)

Produces N failed mandate events plus a hidden ground-truth oracle that determines whether a retry at a given time would succeed.

**Reason-code distribution (documented assumption — put this on a slide):**
| Reason | Share |
|---|---|
| `insufficient_funds` | 40% |
| `gateway_technical_error` | 20% |
| `payment_failed` (generic) | 12% |
| mandate revoked/expired | 10% |
| `authorisation_declined_by_psp` | 8% |
| `payment_timed_out` | 5% |
| `card_expired` | 3% |
| `payment_risk_check_failed` | 2% |

**Ground-truth oracle rules (tunable, must be documented):**
- **Insufficient funds:** retry succeeds with p=0.75 if attempted within 3 days *after* `typical_credit_day`; p=0.15 otherwise.
- **Congestion:** if original failure was in 10AM–1PM with a technical reason, a retry in a safe window succeeds with p=0.80; a retry inside the restricted window succeeds with p=0.25.
- **Transient:** succeeds with p=0.70 after ≥2 hours; p=0.35 if retried sooner.
- **Dead mandate / expired card:** retry **always** fails (p=0.0). Only re-authorisation resolves it, modelled as p=0.30 customer response.
- **Structural/limit:** retry p=0.20; channel-switch nudge p=0.45.

**Timestamp distribution:** 35% of original debits fall inside the 10AM–1PM restricted window (this is what makes the congestion USP measurable).

**Acceptance:** `python -m generator --count 500 --seed 42` produces a reproducible batch; oracle probabilities live in one config block, clearly labelled as assumptions.

---

### M3 — Classifier (`agent/classifier.py`)

Maps a `FailureEvent` to a bucket with confidence and signals.

**Core logic:**
1. Look up `reason` in the decision matrix → base bucket, base confidence 0.9
2. **Congestion override (the USP):** if reason ∈ {`gateway_technical_error`, `payment_failed`} AND `failed_at` is in 10AM–1PM AND `customer_history.prior_insufficient_funds_90d == 0` → reclassify to `B1_CONGESTION`, confidence 0.82, signal `fired_in_restricted_window`
3. **Ambiguity handling for `payment_failed`:** first attempt → treat as B3 soft, confidence 0.55. If it fails again → reclassify to B5, confidence 0.75, signal `reclassified_after_failed_retry`
4. **Balance-pattern signal:** if `prior_insufficient_funds_90d >= 2` → confidence boost on B2, signal `recurring_balance_pattern`

**Acceptance:** every event returns exactly one bucket, a confidence in [0,1], and ≥1 signal string. Unit tests cover each override path.

---

### M4 — Policy Engine (`agent/policy.py`)

Runs **before** any action executes. Returns ALLOW / BLOCK / ESCALATE with reasons.

**Rules (all must be enforced):**
| Rule | Threshold |
|---|---|
| Global max attempts per mandate cycle | 3 |
| Min cooling-off between retries | 2 hours |
| Hard declines (B5) auto-retried | **never** |
| Max customer contacts per cycle | 2 |
| Recovery cycle duration | 7 days, then stop |
| High-value escalation threshold | amount > ₹5,000 → ESCALATE |
| Risk-flagged (`payment_risk_check_failed`) | always ESCALATE |
| Repeat offender (failed prior cycle) | ESCALATE |

**Acceptance:** a dedicated test proves that a B5 event is *never* retried under any input, and that attempt caps hold across a full simulated cycle.

---

### M5 — Executor (`agent/executor.py`)

Executes the allowed action against a simulated clock.

**Window snapping — mandatory on every scheduled retry:**
```python
RESTRICTED_START, RESTRICTED_END = 10, 13   # 10:00–13:00 IST

def snap_to_safe_window(t: datetime) -> tuple[datetime, bool]:
    if RESTRICTED_START <= t.hour < RESTRICTED_END:
        return t.replace(hour=13, minute=30, second=0), True
    return t, False
```

**Income-window scheduling for B2:**
```python
def next_income_window(t, typical_credit_day):
    """Schedule retry 1 day after the customer's typical credit date."""
```

**Acceptance:** no scheduled retry timestamp ever falls inside 10:00–13:00. Assert this across the whole batch in a test.

---

### M6 — Baseline Simulator (`baseline/`)

Replicates Razorpay's current behaviour exactly:
- Single retry, exactly 24 hours after original failure
- Same time of day as the original attempt (so it can land back in the restricted window — this is the point)
- No reason-awareness; hard declines retried too
- One attempt only, then the subscription is treated as churned

**Acceptance:** runs on the identical batch with the identical oracle, producing a directly comparable result set.

---

### M7 — LLM Explanation Layer (`agent/explain.py`)

For each decision, generate a one-to-two sentence human-readable rationale via the Anthropic API.

**Prompt shape:**
```
You are explaining an automated payment-recovery decision to a merchant.
Failure reason: {reason}
Classified as: {bucket} (confidence {confidence})
Signals: {signals}
Action taken: {action}, scheduled for {scheduled_for}
Policy verdict: {policy_verdict} because {policy_reasons}

Write 1-2 plain sentences explaining why this decision was made.
No jargon. No preamble. Do not invent facts not given above.
```

**Requirements:** batch these calls, cache by `(bucket, action, policy_verdict)` to avoid 500 API calls, and **degrade gracefully** — if the API fails, fall back to a template string. The demo must never break because of a network error.

**Acceptance:** every decision has a non-empty explanation, with or without network access.

---

### M8 — Dashboard (`frontend/`)

Single page, four sections:

1. **Headline strip:** ₹ recovered by agent vs baseline, and the delta — largest text on the page
2. **Comparison chart:** recovery rate by bucket, agent vs baseline (grouped bar)
3. **Governance panel:** wasted attempts avoided, hard declines correctly suppressed, contacts sent, items escalated to human
4. **Audit trail table:** searchable, one row per decision, expandable to show signals + explanation

**Metrics to compute:**
| Metric | Definition |
|---|---|
| ₹ recovered | Sum of `amount_recovered_inr` where outcome = RECOVERED |
| Recovery rate % | recovered events / total events |
| Wasted attempts avoided | baseline retries on B5 that the agent suppressed |
| Median time-to-recovery | hours from failure to successful retry |
| Customer contacts sent | count of nudges, proving anti-spam |

**Acceptance:** loads from the API, renders with real generated data, and the headline number is visible without scrolling.

---

## 8. API Surface

```
POST   /webhook/payment-failed     # ingest a failure event (Razorpay-shaped payload)
POST   /simulate/run               # run full batch through agent + baseline
GET    /results/summary            # the five headline metrics, both arms
GET    /results/by-bucket          # per-bucket breakdown
GET    /audit                      # paginated decision log
GET    /audit/{decision_id}        # single decision detail
POST   /reset                      # clear DB, regenerate batch
```

---

## 9. Build Order (do it in this sequence)

| # | Task | Est. | Blocks |
|---|---|---|---|
| 1 | Project skeleton, FastAPI app, SQLite models | 1h | everything |
| 2 | M1 decision matrix YAML + loader | 1h | 3,4,5 |
| 3 | M2 generator + oracle | 2h | 6,7 |
| 4 | M3 classifier + unit tests | 2h | 5 |
| 5 | M4 policy engine + unit tests | 1.5h | 6 |
| 6 | M5 executor + window snapping | 2h | 8 |
| 7 | M6 baseline simulator | 1h | 9 |
| 8 | Outcome resolver + metrics computation | 1.5h | 10 |
| 9 | M7 LLM explanation layer | 1h | — |
| 10 | M8 dashboard | 3h | — |
| 11 | Seed data, README, demo script | 1.5h | — |

**Critical path:** 1 → 2 → 3 → 4 → 5 → 6 → 8 → 10. If time runs short, cut M7 (fall back to templates) before cutting anything else. Never cut M4 — governance is explicitly in the brief's bar.

---

## 10. Definition of Done

- [ ] `POST /simulate/run` processes 500 events end to end without error
- [ ] Dashboard shows a clear ₹ recovered delta over baseline
- [ ] Zero scheduled retries fall inside the NPCI restricted window (asserted in tests)
- [ ] Zero B5 hard declines are auto-retried (asserted in tests)
- [ ] Every decision has a complete audit record with signals and explanation
- [ ] All attempt caps, cooling-off periods, and contact limits hold across the batch
- [ ] README documents every simulation assumption and its source
- [ ] Reproducible via a fixed seed

---

## 11. Honesty Requirements (do not skip)

These protect you in Q&A. A judge who catches an unsupported claim discounts everything else.

1. **Label the simulation clearly.** The README and the demo must state that data is synthetic and oracle probabilities are assumptions, not measurements.
2. **Verify UPI mandate reason-code strings.** The exact code names Razorpay uses for mandate revocation/expiry are **unconfirmed** — check their Subscriptions/UPI error docs and correct the YAML before demoing.
3. **Verify the NPCI window timings** are still current as of the demo date.
4. **Keep the uplift believable.** Reason-aware retries are commonly cited as recovering meaningfully more than static retries — often quoted in a 20–50% range. If your simulation produces a 300% uplift, your oracle is rigged. Tune it to land in a defensible range and say so.
5. **Don't claim Razorpay has no diagnostics.** They have excellent ones. Your claim is that the diagnostics aren't wired to differentiated action — that's the accurate and stronger version.

---

## 12. The Pitch Line

> Razorpay already tells you exactly why every payment failed — source, step, and reason, on every failure. Then it retries them all the same way, next day, often straight back into the NPCI window that killed the payment. We built the agent that reads those codes, routes each failure to the right recovery play, stops when retrying is futile, and logs every decision. Across 500 failed mandates, it recovered ₹X that the current logic writes off.
