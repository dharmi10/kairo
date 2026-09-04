# Architecture & Security Design
### UPI Mandate Recovery Agent (RetryIQ)

> Original planning document, written before the build. Superseded in places by [`DECISIONS.md`](../DECISIONS.md), which logs every divergence and why.

> **How to read this:** Every section is tagged **[BUILD]** (implement in the 2-day window) or **[DESIGN]** (architected and documented, not implemented — you defend it verbally). This split is deliberate and honest. A judge respects "we designed for this, here's the path" far more than a vague claim that it's handled.

---

## 1. Design Principles

These five constraints drive every decision below. State them on your architecture slide.

| # | Principle | Why it matters in payments |
|---|---|---|
| P1 | **Deterministic decisions, AI-assisted explanation** | A fintech system must produce the same output for the same input, every time. LLMs are non-deterministic. Rules decide; the LLM only narrates. |
| P2 | **Idempotent by default** | Webhooks are delivered at-least-once. A duplicate `payment.failed` must never cause a duplicate debit attempt. In payments, a double-charge is worse than a missed charge. |
| P3 | **Fail closed, never open** | If the classifier is uncertain or a dependency is down, the system takes *no* money-moving action. Doing nothing is always safe; retrying wrongly is not. |
| P4 | **Append-only audit** | Every decision is immutable and reconstructible. Regulatory posture and merchant trust both require it. |
| P5 | **Data minimisation** | The system never needs card numbers, VPAs, or bank credentials to do its job. It works on failure metadata alone. Don't store what you don't need. |

**P5 is a genuine architectural strength worth calling out loud:** this system operates entirely on *failure metadata* — reason codes, timestamps, amounts, mandate IDs. It touches zero payment credentials. That radically shrinks the security surface compared to anything sitting in the payment path.

---

## 2. System Architecture

```
                        ┌──────────────────────────┐
                        │  Razorpay Webhook        │
                        │  payment.failed          │
                        └────────────┬─────────────┘
                                     │ HTTPS + HMAC signature
                                     ▼
        ╔════════════════════════════════════════════════════════╗
        ║                  INGESTION TIER                        ║
        ║  ┌──────────────────────────────────────────────────┐  ║
        ║  │ 1. Verify HMAC-SHA256 signature                   │  ║
        ║  │ 2. Check idempotency key (event_id) → dedupe      │  ║
        ║  │ 3. Persist raw event (append-only)                │  ║
        ║  │ 4. ACK 200 immediately  ← latency budget < 150ms  │  ║
        ║  │ 5. Enqueue for async processing                   │  ║
        ║  └──────────────────────────────────────────────────┘  ║
        ╚════════════════════════════════════════════════════════╝
                                     │
                          ─── async boundary ───
                                     │
        ╔════════════════════════════▼═══════════════════════════╗
        ║                  DECISION TIER  (stateless)            ║
        ║                                                        ║
        ║   ┌────────────┐   ┌────────────┐   ┌──────────────┐  ║
        ║   │ CLASSIFIER │──▶│   POLICY   │──▶│   EXECUTOR   │  ║
        ║   │  (pure fn) │   │   ENGINE   │   │  (scheduler) │  ║
        ║   └────────────┘   └────────────┘   └──────────────┘  ║
        ║         │                │                  │          ║
        ║    bucket +        ALLOW/BLOCK/        schedule or     ║
        ║    confidence       ESCALATE           suppress        ║
        ╚═════════════════════════╪══════════════════════════════╝
                                  │
        ╔═════════════════════════▼══════════════════════════════╗
        ║              PERSISTENCE TIER                          ║
        ║   events (append-only) │ decisions (append-only)       ║
        ║   attempts │ mandate_state (mutable, versioned)        ║
        ╚═════════════════════════╪══════════════════════════════╝
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
      ┌──────────────┐   ┌────────────────┐  ┌────────────────┐
      │ EXPLANATION  │   │  HUMAN REVIEW  │  │   DASHBOARD    │
      │ (LLM, async, │   │  QUEUE         │  │   (read-only)  │
      │  cached)     │   │  (escalations) │  │                │
      └──────────────┘   └────────────────┘  └────────────────┘
```

### Why this shape

**The async boundary is the most important line in the diagram.** The webhook handler does the minimum possible work and returns 200 fast. All decision logic happens after the ACK. This means:
- Razorpay never sees a slow endpoint or times out
- A crash in the decision tier never causes a lost webhook — the raw event is already persisted
- The decision tier can be scaled, restarted, or replayed independently

**The decision tier is stateless and pure.** Classifier and policy engine are pure functions: same input → same output, no side effects. This makes them trivially testable, replayable against historical events, and safe to run concurrently.

---

## 3. Security Design

### 3.1 Webhook authentication **[BUILD]**

Razorpay signs webhooks with HMAC-SHA256 using a shared secret. Verify before doing anything else.

```python
import hmac, hashlib

def verify_webhook(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)   # constant-time
```

**Critical details:**
- Use `hmac.compare_digest`, never `==` — string comparison leaks timing information
- Verify against the **raw request body**, not the parsed JSON. Re-serialising changes bytes and breaks the signature.
- Reject before parsing. An unsigned payload should never reach your parser.

### 3.2 Idempotency **[BUILD]**

The single most important safety property in this system.

```python
# UNIQUE constraint on event_id enforces this at the DB level,
# not just in application code.
CREATE TABLE events (
    event_id      TEXT PRIMARY KEY,      -- idempotency key
    received_at   TIMESTAMP NOT NULL,
    raw_payload   TEXT NOT NULL,
    ...
);
```

On duplicate `event_id`: return `200 OK` with the *original* result. Do not error, do not reprocess. Razorpay retries webhooks it thinks failed — a 500 causes a redelivery storm.

**Second idempotency layer — the attempt guard:**
```
UNIQUE (mandate_id, cycle_id, attempt_number)
```
This makes a duplicate retry attempt structurally impossible even if the logic above is bypassed. Defense in depth: two independent layers must both fail for a double-attempt to occur.

### 3.3 Data minimisation & PII **[BUILD]**

| Data | Stored? | Rationale |
|---|---|---|
| Card number / PAN | **Never** | Not needed. Never enters the system. |
| CVV | **Never** | Never touched by anyone but the gateway. |
| Full VPA (`user@bank`) | **Hashed** | Store SHA-256 hash for correlation; never the plaintext. |
| Bank account number | **Never** | Not needed for recovery decisions. |
| Customer phone/email | **Tokenised reference** | Store a notification token; the notification service resolves it. |
| `payment_id`, `mandate_id` | Yes | Razorpay-issued opaque IDs, not sensitive alone. |
| Amount, timestamp, reason code | Yes | The actual working data. |

**Explicit rule: no PII ever reaches the LLM.** The explanation prompt receives only bucket, reason code, confidence, signals, and action — no customer identifiers, no amounts tied to individuals. Assert this in code:

```python
FORBIDDEN_IN_PROMPT = {"customer_id", "vpa", "phone", "email", "payment_id"}

def build_explanation_prompt(decision: dict) -> str:
    safe = {k: v for k, v in decision.items() if k not in FORBIDDEN_IN_PROMPT}
    assert not (FORBIDDEN_IN_PROMPT & safe.keys())
    ...
```

This is a small piece of code with a large signalling value — it shows a judge you thought about AI data governance, which most teams won't.

### 3.4 Secrets handling **[BUILD]**
- All secrets via environment variables, never in source
- `.env` in `.gitignore`, ship a `.env.example` with dummy values
- Anthropic API key server-side only, never exposed to the frontend
- No secrets in logs — scrub before writing

### 3.5 Additional hardening **[DESIGN]**
| Control | Approach |
|---|---|
| Rate limiting | Per-merchant token bucket on the webhook endpoint |
| Replay protection | Reject events with a timestamp skew > 5 minutes |
| Encryption at rest | DB-level encryption; hashed VPAs mean even a dump reveals little |
| TLS | 1.2+ enforced, HSTS on |
| Least privilege | Decision tier gets read-only access to mandate state; only the executor writes attempts |
| Audit immutability | Append-only table; in production, WORM storage or hash-chained records |

---

## 4. Performance & Latency

### 4.1 Latency budget **[BUILD]**

| Stage | Target | Why |
|---|---|---|
| Signature verify | < 5 ms | Pure CPU |
| Idempotency check | < 10 ms | Indexed PK lookup |
| Persist raw event | < 30 ms | Single insert |
| **Webhook ACK total** | **< 150 ms** | Razorpay must never see a slow endpoint |
| Classification | < 20 ms | Pure function, dict lookups only |
| Policy evaluation | < 20 ms | Bounded rule set, indexed history queries |
| Decision total (async) | < 200 ms | Not on the critical path |
| LLM explanation | 1–3 s | **Fully async and cached** — never blocks a decision |

**Enforce the boundary in code:** the LLM call must be structurally incapable of blocking a money-moving decision. The decision is written to the DB first, with `explanation = null`; a background worker fills it in later.

### 4.2 Why it's fast by design

- **No ML inference at decision time.** Classification is dict lookups and integer comparisons on a bounded rule set. There's no model to load, no GPU, no cold start. This is a feature, not a shortcut — it's why the system can be both fast *and* auditable.
- **No external calls in the decision path.** Everything needed is already persisted.
- **Bounded work per event.** No unbounded loops, no N+1 history queries — customer history is a single indexed read.

### 4.3 Scale path **[DESIGN]**

The 2-day build handles a 500-event batch in-process. Here's how the same architecture scales, unchanged in shape:

| Layer | Prototype | Production |
|---|---|---|
| Queue | In-process list | Kafka / SQS, partitioned by `mandate_id` |
| DB | SQLite | Postgres, partitioned by month, read replicas for dashboard |
| Decision tier | Single process | Horizontally scaled stateless workers |
| Scheduler | Simulated clock | Durable timer service / delayed queue |
| Cache | Python dict | Redis for explanation cache + rate-limit counters |

**Partitioning by `mandate_id` is the key insight** — it guarantees all events for one mandate are processed in order by one worker, which preserves attempt-count correctness without distributed locking.

---

## 5. Reliability Patterns

### 5.1 Failure modes and responses **[BUILD]**

| Failure | Response | Principle |
|---|---|---|
| Duplicate webhook | Dedupe on `event_id`, return original result | P2 |
| Classifier confidence below threshold | Route to human queue, no auto-action | P3 fail closed |
| LLM API down | Template fallback, decision unaffected | P1 |
| DB write fails mid-decision | Transaction rollback; event stays unprocessed and is retried | Atomicity |
| Unknown reason code | Default to `B_UNKNOWN` → human queue, **never** auto-retry | P3 fail closed |
| Scheduler fires twice | Attempt-number unique constraint blocks the second | P2 |

**The unknown-reason-code case is worth demoing.** Feed the system a reason code that isn't in the YAML and show it safely routing to human review instead of guessing. That's a 30-second demo moment that proves P3 concretely.

### 5.2 Transactional boundaries **[BUILD]**

Decision write and attempt scheduling must be atomic:

```python
with db.transaction():
    decision_id = write_decision(...)
    if verdict == "ALLOW":
        schedule_attempt(decision_id, scheduled_for)
    # both commit, or neither does
```

Never schedule an attempt that has no corresponding audit record. An unexplainable money-moving action is the worst possible outcome in this system.

### 5.3 Circuit breaker **[DESIGN]**

If retry success rate across a merchant drops below a threshold within a window, halt autonomous retries for that merchant and alert. This protects against acting confidently on a systemic upstream problem (a bank outage) — during which every retry is wasted and may trigger fraud heuristics.

---

## 6. Auditability

### The audit record is the product **[BUILD]**

Every decision is reconstructible from its record alone. A merchant should be able to answer "why did you retry my customer at 2 PM on Tuesday?" without reading code.

```json
{
  "decision_id": "dec_01H...",
  "event_id": "evt_01H...",
  "decided_at": "2026-09-02T10:34:12+05:30",
  "engine_version": "1.0.0",
  "matrix_version": "2026-09-02",
  "input_snapshot": { "reason": "gateway_technical_error", "failed_at_hour": 10 },
  "classification": {
    "bucket": "B1_CONGESTION",
    "confidence": 0.82,
    "signals": ["fired_in_restricted_window", "no_balance_failure_history"]
  },
  "policy": {
    "verdict": "ALLOW",
    "rules_evaluated": ["attempt_cap", "cooling_off", "hard_decline_guard", "value_threshold"],
    "rules_passed": ["attempt_cap", "cooling_off", "hard_decline_guard", "value_threshold"]
  },
  "action": { "type": "RETRY_SCHEDULED", "scheduled_for": "...", "window_snapped": true },
  "explanation": "The debit failed during NPCI's restricted morning window...",
  "outcome": "RECOVERED"
}
```

**`engine_version` and `matrix_version` matter more than they look.** They let you explain a historical decision using the rules that were actually in force at the time — not today's rules. That's real regulatory-grade thinking and a strong answer if a judge asks about compliance.

---

## 7. What's Built vs Designed

Be upfront about this. It reads as maturity, not as a gap.

### [BUILD] — in the prototype
- HMAC signature verification (constant-time)
- Two-layer idempotency (event dedupe + attempt uniqueness)
- Data minimisation: no card data, hashed VPAs, PII-free LLM prompts
- Async boundary with fast webhook ACK
- Stateless pure-function decision tier
- Fail-closed on unknown codes and low confidence
- Transactional decision + scheduling
- Append-only versioned audit log
- LLM fallback to templates

### [DESIGN] — architected, defended verbally
- Kafka/SQS partitioned queue and horizontal scaling
- Postgres partitioning and read replicas
- Redis caching and distributed rate limiting
- Durable scheduler
- Circuit breaker per merchant
- WORM / hash-chained audit storage
- Full RBAC and per-merchant tenancy isolation

---

## 8. The Architecture Slide

If you have one slide for architecture, put these five lines on it:

1. **Fast ACK, async decisions** — the webhook path is under 150 ms; nothing that moves money runs on the critical path
2. **Deterministic core, AI at the edges** — rules decide, the LLM only explains; same input always yields the same decision
3. **Idempotent in two independent layers** — a duplicate webhook cannot cause a duplicate debit attempt
4. **Zero payment credentials** — the system runs on failure metadata alone; no card data, no PII to the LLM
5. **Fail closed** — unknown code, low confidence, or dependency down means no action taken, routed to a human

---

## 9. Likely Judge Questions

**"What if the webhook is delivered twice?"**
Two independent layers: a unique constraint on `event_id` at ingestion, and a unique constraint on `(mandate_id, cycle_id, attempt_number)` at execution. Both must fail for a double-attempt.

**"How do you know the LLM won't hallucinate a bad retry?"**
It can't. The LLM never makes decisions — it only writes the explanation after the decision is already committed. Pull the LLM out entirely and the system's behaviour is byte-identical.

**"What happens if you get a reason code you've never seen?"**
It routes to human review. The system never guesses on money-moving actions. We can demo that live.

**"Is this fast enough for production volume?"**
The decision path is dict lookups and integer comparisons — no model inference, no external calls. It's the LLM explanation that's slow, and that's deliberately off the critical path and cached.

**"How would you scale this?"**
Partition the queue by `mandate_id` so all events for one mandate hit one worker in order. That preserves attempt-count correctness without distributed locks, and the decision tier is already stateless, so it scales horizontally as-is.
