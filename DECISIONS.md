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

## 2026-09-03 — Second reason-code verification pass (decision_matrix.yaml)

Scanned https://razorpay.com/docs/errors/payments/list/ in full to reduce
the matrix's assumption surface — grew from 12 verified codes to **40
reason codes covering 5 of 6 buckets**, with only 3 remaining
`PLACEHOLDER` entries (all in the UPI-mandate-lifecycle area, still
unconfirmed as of this date).

**OTP string discrepancy, resolved:** the reason-code table on
`/docs/errors/payments/list/` uses `incorrect_otp` (matches what's already
in the YAML — no change needed). A different example error object
elsewhere in Razorpay's own docs uses `invalid_otp`. This is Razorpay's
documentation being internally inconsistent, not new information that
should change our config. We keep `incorrect_otp` because it's the string
that appears in the actual reason-code *table*, not a narrative example.
Noting the inconsistency here in case a judge raises it — the honest
answer is "Razorpay's own docs disagree with themselves; we went with the
authoritative table."

**Notable find — `payment_declined_due_to_high_traffic` (VERIFIED, source:
gateway).** This is a real Razorpay reason code that directly names
congestion ("Payment declined due to high traffic at the gateway").
Mapped straight to `B1_CONGESTION` in the YAML. This *strengthens* the
pitch: our #1 USP no longer rests solely on the timestamp+history
inference heuristic (`congestion_override`) — when this exact code shows
up, classification is a direct, verified lookup, not an inference. The
inference heuristic still stands for `gateway_technical_error` /
`payment_failed`, where no explicit congestion code exists.

**Added, VERIFIED, source-cited inline in the YAML:**
- B3_TRANSIENT: `bank_not_available`, `bank_technical_error`,
  `bank_cutoff_in_progress` (all requested explicitly), plus
  `issuer_technical_error`, `psp_app_not_available`, `psp_not_available`,
  `request_timed_out`, `invalid_response_from_gateway`, `server_error`
  (source is actually `razorpay`, not `gateway` — noted in the YAML
  comment). All inherit `gateway_technical_error`'s play (3h delay, 3 max
  attempts) since Razorpay's own "next steps" text for each is equally
  generic ("retry after some time").
- B4_STRUCTURAL: `transaction_limit_exceeded`, `transaction_daily_count_exceeded`,
  `transaction_frequency_limit_exceeded`, `credit_limit_exceeded`,
  `otp_attempts_exceeded`, `otp_expired`, `pin_attempts_exceeded`,
  `transaction_on_vpa_restricted`, `funds_blocked_by_mandate`.
- B5_DEAD: `debit_instrument_inactive`, `reqauth_mandate_not_acknowledged`
  (a genuine mandate-lifecycle code — distinct from, and does not replace,
  the `mandate_revoked_by_customer`/`mandate_expired` placeholders), and
  `card_declined` / `debit_declined` / `payment_declined`.

**One judgment call, flagged inline in the YAML, not silently asserted:**
`card_declined`, `debit_declined`, `payment_declined` are bucketed as
B5_DEAD by textual pattern-matching (an unqualified "X has been declined,
retry with a different method" reads the same way `card_expired` and
`debit_instrument_blocked` do — issuer said no, same-instrument retry is
futile) rather than a verbatim Razorpay bucket assignment, since Razorpay
doesn't publish bucket labels at all. This is a reasonable, consistent
reading but an inference, not a lookup — say so if asked.

**Explicitly considered and excluded**, with reasoning recorded in the
YAML itself: `mandate_creation_declined/_expired/_failed/_timeout` (fire
during mandate *creation*, not against a live mandate's recurring debit —
out of scope, same logic the PRD uses to exclude checkout abandonment) and
`mcc_amount_limit_exceeded` / `collect_on_mcc_blocked` (merchant/MCC-config
errors affecting all of a merchant's transactions uniformly, not a
per-mandate condition — no per-event recovery play to encode).

## 2026-09-03 — Oracle model: conditional, not independent-per-attempt

The generator's ground-truth oracle (M2, not yet built) will compute
retry-success probability as a function of **context at the retry
datetime**, not attempt number. No i.i.d. per-attempt draw anywhere.

**Why:** with the M6 baseline now doing 3 retries (T+1/T+2/T+3, see the
earlier baseline-correction entry above), independent per-attempt success
probabilities compound geometrically (`1 - (1-p)^3`) regardless of whether
anything about the underlying cause actually changed between attempts.
Concretely, the baseline retries at the *same time of day* every attempt
(per M6's spec) — so if the original failure landed in the NPCI restricted
window, all 3 baseline retries land in that same restricted window, with
literally nothing different about the conditions each time. Modelling that
as 3 independent draws would hand the baseline a rising cumulative
recovery rate purely from trial count, which is a modelling artifact, not
a real result — and it directly undermines the pitch's thesis ("blind
retries ignore context and underperform"), since it would make blind
persistence look competitive with context-aware timing. Real payment
failures are correlated across attempts because the underlying cause
(no money, congested window, gateway down) persists across nearby retries
— they are repeated observations of one latent state, not fresh coin
flips.

**Confirmed values (supersede the illustrative 0.15/0.25 figures used
while explaining the reasoning — those were the old independent-draw
example numbers, not the new conditional ones):**
- **B2_BALANCE:** `p = 0.25` if retry is before the customer's
  `typical_credit_day`, `p = 0.55` if after. Three retries before payday
  must NOT compound toward success — the customer still has no money.
- **B1_CONGESTION:** `p = 0.35` if the retry lands in the NPCI restricted
  window, `p = 0.70` if in a safe window. Repeated retries into the
  restricted window do not compound.
- **B3_TRANSIENT:** `p = 0.35` if retried under 2h after failure,
  `p = 0.70` if over 2h. This one genuinely resolves with elapsed time —
  the baseline will legitimately do well here over 3 days, and that's
  honest, not a bug. Our edge on B3 is time-to-recovery, not eventual
  recovery rate.
- **B5_DEAD:** `p = 0.0` always, regardless of attempts or context.

Oracle signature: `oracle(event, retry_datetime) -> probability`. No
attempt-number term anywhere in the calculation. Must also be documented
in the README (M2 section) as a deliberate modelling choice, not asserted
as measured fact.

## 2026-09-03 — card_declined / debit_declined / payment_declined reclassified

Moved from `B5_DEAD` to ambiguous `B3_TRANSIENT` in `decision_matrix.yaml`
(same treatment as `payment_failed`: soft on attempt 1 at confidence 0.55,
retry once, reclassify to `B5_DEAD` if that retry also fails).

**Reasoning:** these are generic bank-decline codes — Razorpay's
description doesn't distinguish insufficient funds, velocity/risk-rule
declines, or a genuinely dead instrument. `B5_DEAD` means never retried.
The two possible misclassifications aren't symmetric: filing a recoverable
failure as B5 loses that money permanently and silently (no second
chance, by design). Filing a dead one as B3 costs exactly one bounded,
capped retry attempt before the classifier reclassifies to B5 anyway.
Given genuine ambiguity, the asymmetric cost of the two errors means
"assume recoverable first" is the correct default, not "assume dead
first" — the downside of being wrong is capped in one direction and
unbounded in the other.

Design implication for M3 (not yet built): the classifier should key the
two-stage soft→reclassify confidence behaviour off the YAML's
`ambiguous: true` flag generically, not off a hardcoded check for
`payment_failed` by name — so `card_declined` / `debit_declined` /
`payment_declined` get the identical treatment automatically.

## 2026-09-03 — Phase 2: generator + oracle built

`backend/generator/` (distribution.py, oracle.py, generate.py, __main__.py).
`python -m generator --count 500 --seed 42` verified reproducible
(byte-identical output across two runs) and fast.

**decision_matrix.yaml gained a structured `source` field** on all 40
reason codes (previously only in comments) — the generator needs it as
data to populate `error.source`, and duplicating it in generator-side code
would violate M1's "no decision logic hardcoded elsewhere" principle.
`app/matrix.py` validation now checks every code has a valid source.

**Bucket-to-code distribution** (`distribution.py`): codes present in the
PRD's original 8-code table keep their relative weight within their
bucket (rescaled to the new bucket-share targets); every other code
splits the remainder of its bucket evenly. Generalizes automatically to
B1/B4, which had no PRD precedent at all.

**Ground-truth bucket resolution reuses `congestion_override` from the
YAML directly** (`oracle.true_bucket()`), rather than just reading each
event's nominally-declared bucket. This means congestion is a genuine
latent pattern in the generated data — discoverable via timing + balance
history — not just an apparent correlation with nothing real behind it.
Consequence worth remembering: the *sampled* B1 share (10%, via
`payment_declined_due_to_high_traffic`) and the *ground-truth* B1 share
end up different — on the seed=42 run, ground truth came out to ~15.4%
B1 / ~26.0% B3 (vs. 30% B3 sampled), because some nominally-B3-coded
events (gateway_technical_error / payment_failed landing in the
restricted window with no balance-failure history) are secretly
congestion too. This is intended, not a bug — it's what makes the
classifier's future job (M3) a real inference problem.

Ambiguous codes (`payment_failed`, `card_declined`, `debit_declined`,
`payment_declined`) resolve to their YAML-declared bucket (B3_TRANSIENT)
for ground-truth purposes, always — `ambiguous: true` is purely a
classifier-confidence concept, not a ground-truth one.

Restricted-window timestamp assignment (35% target) is independent of
reason-code assignment — applied uniformly to every event regardless of
code, which is what creates the raw material for M3's later
congestion-override inference on B3-coded events. On the seed=42 run it
landed at 32.0% (expected sampling noise on a Bernoulli(0.35) draw over
500 events, not a bug).

Generator emits FailureEvent payloads matching the PRD's nested §6 JSON
shape (webhook-shaped), not the flattened DB row shape — those are
different layers by design; the ingestion tier (not yet built) is what
flattens+enriches into DB columns later.

## 2026-09-03 — Tooling

No Python 3.11 installed on this machine. `py -0p` listed a 3.13, but its
registered path (`OneDrive\Desktop\python.exe`) is broken — not a real
interpreter, venv creation fails against it. Using **3.10** for the backend
venv; nothing in the spec depends on a 3.11-only language feature.
