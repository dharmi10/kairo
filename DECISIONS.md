# Kairo — Build Decisions Log

Running record of decisions made during the build that resolve ambiguities
in, or diverge from, the three source documents (PRD, decision matrix,
architecture-and-security). Dated, chronological.

## Standing instructions (apply for the rest of the build, not dated entries)

- **Never run `git push`, never create a remote, never run any `gh` command,
  never publish anything to GitHub.** Local `git add` / `git commit` are
  fine. The user handles all pushes themselves. This holds even if it isn't
  repeated in a later prompt.

## 2026-09-03 — Scope & naming resolutions

1. Project name is **Kairo** everywhere. The PRD's "RetryIQ" codename is retired.
2. Added a sixth bucket, **B_UNKNOWN**: any reason code not present in
   `decision_matrix.yaml`, or any classification with confidence < 0.5,
   routes there and straight to the human queue. Never auto-retried. This
   resolves a direct contradiction between the decision matrix ("five
   buckets, every failure resolves into exactly one") and the architecture
   doc's fail-closed requirement for unrecognised codes. Treated as a demo
   requirement, not optional.
3. The architecture doc's async ingestion/queue/background-worker design is
   **[DESIGN] only** — not built. The actual pipeline is a synchronous batch
   job (`POST /simulate/run` over the generated batch). No `FastAPI
   BackgroundTasks`, no queue.
4. Added `cycle_id` to `FailureEvent` / the `events` table. A cycle is the
   7-day recovery window for one failed mandate debit.
5. Renamed the executor's retry counter to `retry_attempt_number` to
   disambiguate it from `Event.attempt_number` (Razorpay's own count of
   which attempt this is). Idempotency constraint:
   `UNIQUE(mandate_id, cycle_id, retry_attempt_number)` on the `attempts`
   table.
6. Added the B1 reclassification path: after 2 failed `B1_CONGESTION`
   retries, reclassify to `B3_TRANSIENT` if the customer has no
   balance-failure history, else `B2_BALANCE`. Encoded in
   `decision_matrix.yaml` under `congestion_override.reclassify_after_max_attempts`.
7. HMAC: the synthetic event generator (M2, not yet built) will sign each
   payload with a fake shared secret from `.env`; the webhook handler
   verifies against the same secret (`app/security.py`). This demonstrates
   the pattern against self-generated traffic, not real Razorpay
   integration — state that explicitly in the pitch.
8. PII: keep the `FORBIDDEN_IN_PROMPT` assertion in the explanation layer
   (M7, not yet built). Not building VPA hashing/tokenisation — the data
   model never carries VPA/phone/email, so there's nothing to protect.
9. Bucket names are uppercase everywhere: `B1_CONGESTION`, `B2_BALANCE`,
   `B3_TRANSIENT`, `B4_STRUCTURAL`, `B5_DEAD`, `B_UNKNOWN`.
10. Added `engine_version` and `matrix_version` to the `Decision` schema and
    audit record.
11. Dashboard (M8) budgeted at 5h, not the PRD's original 3h. If time is
    short, drop search/expand on the audit table — a plain paginated table
    still meets the acceptance criteria.

## 2026-09-03 — Reason-code verification (against Razorpay's official docs)

Confirmed as real, currently-documented Razorpay `reason` strings:
`insufficient_funds`, `gateway_technical_error`, `payment_failed`,
`card_expired`, `payment_risk_check_failed`, `authorisation_declined_by_psp`,
`payment_timed_out`, `debit_instrument_blocked`, `payment_cancelled`,
`authentication_failed`, `incorrect_cvv`.

Two corrections applied to `decision_matrix.yaml`:
- `invalid_otp` → **`incorrect_otp`** (the actual documented string).
- Placeholder "daily transaction limit breached" → **`transaction_daily_limit_exceeded`**
  (confirmed, gateway source).

Left as explicitly labelled **PLACEHOLDER** entries in the YAML (no public
Razorpay reason-code string found as of 2026-09-03): `mandate_revoked_by_customer`,
`mandate_expired`, `mandate_amount_exceeded`. Razorpay's subscription docs
describe mandate cancellation only narratively ("customer has cancelled the
mandate → subscription moves to pending"), with no machine-readable code
alongside it. Do not present these as verified in the pitch — the YAML
comments flag them inline.

NPCI restricted window (10:00–13:00 IST; safe windows before 10:00, 13:00–17:00,
after 21:30) confirmed against current Razorpay/NPCI reporting as of
2026-09-03. No changes needed.

**M6 baseline correction:** Razorpay's actual current retry behaviour is 3
automatic retries (T+1, T+2, T+3 days), not a single next-day retry as
originally drafted in the PRD. The baseline simulator will implement 3
retries, same time-of-day each attempt, no reason-awareness, hard declines
retried too, then the subscription moves to `halted`. Confirmed with the
user 2026-09-03 — this makes the uplift claim more defensible in Q&A.

## 2026-09-03 — Tooling

No Python 3.11 installed on this machine. `py -0p` listed a 3.13, but its
registered path (`OneDrive\Desktop\python.exe`) is broken — not a real
interpreter, venv creation fails against it. Using **3.10** for the backend
venv; nothing in the spec depends on a 3.11-only language feature.
