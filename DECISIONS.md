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

## 2026-09-03 — Phase 3: classifier built and graded against the oracle

`backend/classifier/classify.py` — pure function `classify(event, matrix)`,
no side effects. Path priority: unknown-code check, then congestion
override, then ambiguity handling (keyed off the YAML's `ambiguous: true`
flag generically, not a hardcoded reason-code name), then base
lookup + balance-pattern boost, with a confidence floor applied to every
path's output (below `settings.unknown_bucket_confidence_threshold` ->
B_UNKNOWN, original confidence preserved rather than zeroed).

**Grading result, and why it's ~100% and that's not suspicious:**
`classifier/grade.py` grades `classify()` against `oracle.true_bucket()`
across the M2 seed=42 batch (500 events) — 100.0% accuracy, zero
misclassifications. This is expected, not impressive: both functions
share the exact same `congestion_override` lookup from the YAML, so they
cannot disagree on bucket for any event with `attempt_number == 1` — which
is every event M2 generates (see Phase 2 entry above). The one place
`classify()` and the oracle are *designed* to diverge — the
ambiguity-reclassification path, active only when `attempt_number >= 2` —
never fires in M2's data. `grade.py` also grades a second, hand-built set
of `attempt_number=2` events specifically to exercise that path: 0%
"accuracy" there, entirely `B3_TRANSIENT -> B5_DEAD`, which is the
intended divergence (classify()'s heuristic guess under real uncertainty
vs. the oracle's attempt-number-blind ground truth), not a bug.

**Unit tests:** `backend/tests/test_classifier.py`, 19 tests, all passing
— one per path (including priority ordering between congestion override
and ambiguity handling), the unknown-code case, the confidence floor
(tested directly against the helper since no live path currently produces
a sub-threshold confidence), purity/no-mutation checks, and a
parametrized check that ambiguity handling generalizes to all three
reclassified decline codes without being special-cased by name. Added
`pytest==8.3.4` to requirements.txt and a `pytest.ini` (`pythonpath = .`)
so `app`/`generator`/`classifier` import cleanly regardless of invocation
directory.

## 2026-09-03 — Ground truth decoupled from classifier config, with noise

Fixed the tautology from Phase 3's grading (100% accuracy because
`classify()` and ground-truth resolution shared the same
`congestion_override` lookup). Ground truth is now decided once, at
generation time, and stamped on the event as `_true_bucket`
(`generator/generate.py::_decide_true_bucket`) — `classifier/classify.py`
never reads it (`tests/test_classifier.py::test_classify_never_reads_true_bucket`,
parametrized across 4 paths, uses a dict subclass that raises on ANY
access to that key — subscript, `.get()`, or `in` — so a read via any
normal dict-access idiom fails the test, not just a literal
`event["_true_bucket"]`). `oracle()`'s signature dropped back to exactly
`(event, retry_time)` — it now reads `_true_bucket` instead of
recomputing it, so `matrix` fell out of the signature entirely, matching
the originally-specified pure-function shape.

**Noise, as specified:** scoped to `congestion_override.applies_to_reasons`
(`gateway_technical_error`, `payment_failed`) only — not broadened to
every B3-bucketed technical code. `CONGESTION_FALSE_POSITIVE_RATE = 0.15`
(in-window, technical reason, no balance history, but NOT actually
congestion — flips to `B3_TRANSIENT` 60% / `B2_BALANCE` 40%, ASSUMPTION,
no data to weight this precisely) and `CONGESTION_FALSE_NEGATIVE_RATE =
0.10` (outside window, technical reason, no balance history, but IS
actually congestion). Documented in `generator/generate.py` and the
README.

**Result on seed=42, n=500: 98.0% overall accuracy, not "the 80s".**
Confusion is concentrated exactly where expected — `B2_BALANCE ->
B1_CONGESTION: 4`, `B3_TRANSIENT -> B1_CONGESTION: 3` (over-calling
congestion, the false-positive case) and `B1_CONGESTION -> B3_TRANSIENT: 3`
(missing it, the false-negative case) — with B1_CONGESTION precision
89.7% / recall 95.3%, and every other bucket at or near 100%. The
overall number lands far above "the 80s" purely because of **dilution**:
noise is scoped to the congestion-boundary subset (~85 of 500 events are
even eligible), so a 15%/10% *within-subset* error rate works out to
~10 misclassified events out of 500 overall — 2%, not 20%. This wasn't
adjusted to hit a target range; it's what the specified rates, scoped as
specified, produce. Flagged to the user rather than silently widening
the noise scope to manufacture a lower number — that's a real modelling
decision (how broadly "technical reason" should mean for noise purposes),
not something to change without asking. **Confirmed with the user
2026-09-03: keep as-is.** 98.0% aggregate accuracy stands; B1_CONGESTION
precision (89.7%) / recall (95.3%) is the number to cite in the pitch as
"what the classifier actually detects" — the aggregate is diluted by the
4 buckets that carry zero injected noise, so it isn't the right headline
metric for detection quality.

## 2026-09-03 — Deliberate deferral: congestion_override.reclassify_after_max_attempts

Confirmed with the user: the "after 2 failed B1_CONGESTION retries,
reclassify to B3_TRANSIENT/B2_BALANCE" rule stays unimplemented in
`classify()`. It needs multi-attempt retry history that doesn't exist on
a single fresh `FailureEvent` — implementing it in the classifier would
either break purity (reading external mandate state) or require inventing
fields M2 doesn't produce. Belongs to M4 (policy engine) / M5 (executor),
where retry history is actually tracked. Recorded here so it isn't lost —
the YAML's `reclassify_after_max_attempts` block is written and waiting,
just not wired to anything yet.

## 2026-09-03 — Phase 4: policy engine built

`backend/policy/policy.py` — pure function `evaluate_policy(event,
classification, mandate_history) -> (verdict, reasons)`, tuple return
matching the user's literal spec. All 8 PRD M4 rules plus the fail-closed
data-quality gate implemented as 9 independent checks, each contributing
a reason string (pass or fail) — more exhaustive than the PRD's own
abbreviated audit example on purpose (see module docstring: "the audit
record is the product"). Verdict resolution is worst-wins (ESCALATE >
BLOCK > ALLOW), so e.g. `payment_risk_check_failed` (both risk-flagged
AND B5_DEAD) correctly resolves to ESCALATE, not BLOCK, without rule 5
needing to know about rule 2.

**Design choices worth remembering:**
- "Now" for cooling-off/cycle-age purposes is `event["failed_at"]`, not a
  separate injected clock parameter — keeps the signature exactly
  `(event, classification, mandate_history)` as specified.
- Max-contacts cap (rule 9) is NOT conditioned on whether the eventual
  action is a customer-facing nudge vs. a silent retry — policy() doesn't
  know the executor's action choice yet (M5). Treated as a flat cap,
  matching the PRD's table literally. Flagged as a modelling
  simplification, not a verified distinction — M5 may need to revisit.
- Reused `app/config.py` settings (`global_max_retry_attempts`,
  `min_cooling_off_hours`, `max_contacts_per_cycle`, `recovery_cycle_days`,
  `high_value_threshold_inr`, `unknown_bucket_confidence_threshold`) —
  all six were already defined there since Phase 1, unused until now.

**Tests:** `tests/test_policy.py`, 23 tests. Includes the three
explicitly-requested critical properties: a fixed-seed 2000-combination
fuzz test proving B5_DEAD never resolves to ALLOW (varies confidence,
amount, reason code, and all mandate_history fields, including values
that also trigger other ESCALATE/BLOCK rules simultaneously); a simulated
7-day/12h-spaced retry cycle proving the attempt cap holds for the rest
of the window once hit; and a dedicated cooling-off test at 1/30/60/90/119
minutes (all under the 2h threshold, all with attempts=1, nowhere near
the cap of 3) proving cooling-off blocks independently of the attempt cap.

**Flagged, not fixed (Phase 2 concern, out of Phase 4 scope): the
high-value escalation rule interacts badly with Phase 2's amount bands.**
`policy/report.py` (`python -m policy.report`) shows 53.6% of the M2
batch trips `high_value_amount` and the overall verdict split is 54.0%
ESCALATE / 39.2% ALLOW / 6.8% BLOCK — escalating over half of everything
to a human undercuts the "autonomous agent" pitch. Cause: Phase 2's
`AMOUNT_BANDS_INR` (SIP up to ₹50,000, EMI up to ₹25,000, INSURANCE up to
₹50,000, sampled uniformly) were picked for category-plausibility, not
against the ₹5,000 threshold M4 introduced later — the two were never
checked against each other. Needs a decision: tighten the amount bands
(Phase 2), or accept that real SIP/EMI/insurance amounts legitimately
often exceed ₹5,000 and a majority-escalate outcome is honest. Not
changed without the user's say-so.

## 2026-09-03 — Fixed the 54% escalation collision: threshold and amount distribution set together

**Root cause confirmed as the user diagnosed it: the amount *distribution*,
not the threshold.** Phase 2's `AMOUNT_BANDS_INR` sampled uniformly across
wide bands (SIP up to Rs.50,000, EMI up to Rs.25,000, INSURANCE up to
Rs.50,000) — a uniform distribution over a wide band puts far too much
mass above any reasonable escalation threshold, because real payment
amounts are right-skewed (most debits small, a thin tail of large ones),
not flat.

**Fix 1 — replaced uniform bands with per-category log-normal sampling**
(`generator/generate.py::_sample_amount_inr`, `AMOUNT_DISTRIBUTION_INR`).
Targets given by the user: median + approximate P99 tail per category;
`sigma` is *derived* from that ratio (`sigma = ln(tail/median) / z_p99`,
`z_p99 ≈ 2.326`), not independently chosen — so the only real inputs are
the two numbers the user specified per category. Clipped to
`[floor, cap]` since a log-normal's tail is technically unbounded and an
occasional 10x-the-target-tail draw isn't a realistic mandate amount.
Actual medians on seed=42: OTT 331 (target 300), UTILITY 1066 (target
900), SIP 2072 (target 2000), INSURANCE 3141 (target 3000), EMI 5879
(target 6000) — close enough to the targets to trust the derivation.

**Fix 2 — raised `high_value_threshold_inr` from Rs.5,000 to Rs.10,000**
(`app/config.py`). Updated `tests/test_policy.py`'s three
threshold-dependent tests to reference `settings.high_value_threshold_inr`
instead of the old hardcoded `5000`/`5001` literals — they would have
silently passed-for-the-wrong-reason otherwise (both values sit under the
new 10,000 threshold).

**Result on seed=42, n=500: ESCALATE = 10.0%** (ALLOW 78.4%, BLOCK
11.6%) — right at the bottom edge of the requested 10-15% band. Not
further tuned; landed there from the specified targets on the first run.

**The reasoning to keep, going forward: the escalation threshold and the
amount distribution are not independent parameters — either one alone is
meaningless.** A threshold is only "high-value" relative to what the
distribution actually produces below it; changing one without checking
the other (which is exactly what happened the first time) can silently
turn "escalate the rare big one" into "escalate over half of everything,"
undercutting the entire autonomous-agent premise without any single line
of code being wrong on its own. Any future change to either the amount
distribution or the threshold should re-run `python -m policy.report` and
check the aggregate ESCALATE rate before considering it done.

**Regulatory cross-check (per user request — reported, not assumed):**
searched for NPCI/RBI's Additional Factor Authentication (AFA) threshold
for UPI Autopay / e-mandate recurring debits. Multiple 2026 sources
(BusinessToday, Upstox, NewsBytesApp, Wiretel, IndiaAIPulse, OfficeNewz —
secondary financial-news reporting, not fetched directly from an RBI
primary document) consistently describe an RBI e-mandate framework
circular dated **21 April 2026** that raised/consolidated the AFA-exempt
limit to **Rs.15,000 per transaction generally**, with an enhanced
**Rs.1,00,000 exemption for insurance premiums, mutual fund
subscriptions, and credit card bill payments** specifically. One source
additionally notes recurring EMI auto-debits via e-mandates are exempted
from the newer 2FA digital-lending requirements entirely, to avoid
disrupting repayment schedules. All of `AMOUNT_DISTRIBUTION_INR`'s P99
tails sit under these limits (EMI 40k, SIP/INSURANCE 25-30k vs. a 1L
enhanced exemption; OTT/UTILITY trivially under the general 15k) — no
band needed adjusting to comply. Worth citing in the pitch as supporting
context for why the amount assumptions are realistic, with the caveat
that this is secondary-source reporting on the regulation, not a citation
of RBI's own circular text.

## 2026-09-03 — Tooling

No Python 3.11 installed on this machine. `py -0p` listed a 3.13, but its
registered path (`OneDrive\Desktop\python.exe`) is broken — not a real
interpreter, venv creation fails against it. Using **3.10** for the backend
venv; nothing in the spec depends on a 3.11-only language feature.
