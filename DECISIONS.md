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

## 2026-09-03 — Phase 5: executor built

`backend/executor/executor.py` -- two layers, deliberately separated:
`resolve_action()` is pure (event, classification, policy_verdict,
policy_reasons, mandate_history, matrix) -> a plan; `execute_decision()`
is the only impure function, writing Decision + Attempt to the same
SQLAlchemy Session and committing once (both persist or neither does --
architecture-and-security.md sec. 5.2's atomicity requirement).

**Real bug caught by the whole-batch test, not by review:**
`test_zero_scheduled_retries_fall_in_restricted_window_across_full_batch`
crashed with `KeyError: 'delay_hours'` on its first run. Cause: the four
ambiguous B3 codes (`payment_failed`, `card_declined`, `debit_declined`,
`payment_declined`) never got a `delay_hours` field in
`decision_matrix.yaml` -- Phases 2-3 only ever needed their
bucket/action/confidence, so the gap was invisible until the executor
actually tried to schedule a retry for one. Fixed both ends: added
`delay_hours: 3` to all four (matching `gateway_technical_error`'s delay,
the closest sibling), and made the executor's delay lookup defensive
(`.get("delay_hours", ...)` instead of `[...]`) so a future missing field
degrades to the documented fallback instead of crashing the batch.

**Shared `app/dates.py`:** extracted `next_payday_on_or_after` out of
`generator/oracle.py` (private there since Phase 2) into a shared module,
since the executor's B2 income-window scheduling needs the exact same
date-rollover logic (payday nearest-or-after a date, clamped to month
length, rolling to next month). `oracle.py` updated to import it instead
of keeping its own copy.

**Design choices worth remembering:**
- B1_CONGESTION reached via the timing+history override (signalled by
  `"fired_in_restricted_window"` in classify()'s signals) uses
  `congestion_override["play"]`'s delay; reached via the direct code
  (`payment_declined_due_to_high_traffic`) uses that code's own play.
  Disambiguated via the signal, not by inspecting the reason string, so
  it stays correct if more direct-mapped congestion codes are ever added.
- `reclassify_after_max_attempts` detection (`_effective_classification`)
  is explicit, via a dedicated marker signal
  (`reclassified_after_2_failed_congestion_retries`), NOT inferred from
  bucket-equality comparisons. An earlier draft compared
  `play["bucket"] == classification["bucket"]` to detect "was this
  reclassified away from its own code's bucket" -- that coincidentally
  "worked" for `gateway_technical_error`/`payment_failed` only because
  their own declared bucket already IS `B3_TRANSIENT`, which would have
  silently broken (or silently "worked for the wrong reason") the moment
  the matrix changed. Caught in review before it shipped, not by a test.
- **One deliberate, narrow exception to Phase 4's policy verdict, not a
  general override of it:** `BLOCK` due specifically to
  `hard_decline_never_retried` still allows the matrix's prescribed
  NUDGE action to fire (`NUDGE_SENT`) -- PRD/decision-matrix.md are
  explicit that every B5_DEAD code gets "no retry, immediate
  re-authorisation nudge", and `hard_decline_never_retried` is
  specifically about blocking the *retry*, not blanket silence. Every
  OTHER `BLOCK` reason (cycle expired, attempt cap, cooling-off, max
  contacts) still means "no autonomous action of any kind" -- that
  blanket reading is left exactly as Phase 4 built and tested it, not
  relitigated here.
- The matrix's own per-code `ESCALATE_HUMAN` action wins even under an
  `ALLOW` policy verdict (e.g. `mandate_amount_exceeded` at a low amount,
  which no M4 rule independently catches) -- same "more conservative
  wins" principle as M4's worst-wins, applied one layer up.
- `NEW_MANDATE_FLOW` (the `mandate_expired` placeholder's action) maps to
  `STOPPED` -- genuinely out of this system's scope (PRD sec. 3 excludes
  mandate creation), so there's no better fit in the action vocabulary.

**Tests:** `tests/test_executor.py`, 22 tests, all passing (68 total
across the project). Includes the two explicitly-requested checks: the
whole-batch zero-restricted-window assertion, and dedicated
reclassify_after_max_attempts tests for both branches (no balance
history -> B3, balance history -> B2) plus a negative case (1 failed
attempt, not yet 2 -> no reclassification).

**Outcome counts, seed=42, n=500 (fresh mandate_history per event, same
convention as `policy/report.py`):** RETRY_SCHEDULED 372 (74.4%),
NUDGE_SENT 61 (12.2%), HUMAN_QUEUE 50 (10.0%), STOPPED 17 (3.4%). 111 of
372 scheduled retries needed window-snapping; zero landed in the
restricted window after snapping. `reclassify_after_max_attempts` cannot
fire on this fresh-history batch (needs 2 prior failed B1 retries, which
no event in a first-failure-only batch has) -- demonstrated separately
via a hand-built scenario in `executor/pipeline.py`, same pattern used for
Phase 3's ambiguity-reclassification path and Phase 4's attempt-cap rule.

## 2026-09-03 — Phase 6: baseline simulator + metrics — two real bugs found and fixed

`backend/baseline/baseline.py` (M6), `backend/executor/simulate.py` (the
agent's full-cycle "outcome resolver" -- new orchestration; Phases 3-5
each deliberately computed ONE decision against fresh state, but the five
metrics need to know whether a mandate's cycle actually RESOLVES),
`backend/metrics/metrics.py` + `metrics/report.py`. `python -m
metrics.report` runs both arms against the identical batch and prints the
aggregate + by-bucket comparison. 11 new tests
(`tests/test_simulation.py`, 79 total project-wide), two of which are
regression tests for the bugs below.

**Bug 1 — independent draws under identical context still compound, even
with a "conditional" oracle.** First run of the comparison: agent LOST to
baseline by -49.4% on Rs recovered. Root cause: baseline retries at the
SAME hour, 3 days running -- for B1_CONGESTION that's the same
restricted-window context (p=0.35) three times. Being "conditional on
context, not attempt number" (the whole point of the September oracle
redesign) stops probability from artificially escalating with attempt
count, but says nothing about whether REPEATED attempts under UNCHANGED
context should be independent drawer -- and my simulation was drawing
independently at every attempt regardless. Three independent draws at
p=0.35 still compound to 1-(1-0.35)^3 = 72.5%, which is exactly the same
class of artifact the conditional redesign was built to prevent, just
reintroduced one layer up (in how the simulation consumes the oracle,
not in the oracle's probabilities). **Fix:** `generator/oracle.py` gained
`oracle_context_key()` (bucket + context-state, e.g. `("B1_CONGESTION",
"restricted_window")`) and `draw_retry_outcome()`, which caches ONE drawn
outcome per (event, context-key) and reuses it for every subsequent
attempt sharing that key -- "we already tried under these exact
conditions and it didn't work" is what "the underlying cause persists"
(the original oracle-design reasoning) actually implies. The cache is
created FRESH per event and shared between that event's agent AND
baseline simulation, so if both arms happen to retry under the identical
context, they see the identical simulated reality rather than a separate
coin flip per arm.

**Bug 2 — cooling-off self-collision silently capped every mandate at
exactly 1 retry.** After fixing Bug 1, agent still lost (-21.5%). Traced
via a direct comparison of mismatched B3 outcomes: several showed the
agent making 0 or 1 attempts where baseline (correctly, per Bug 1's fix,
using the SAME cached draw) recovered on attempt 1 -- meaning the agent
was failing to retry at all in cases it should have. Root cause: after a
failed retry, `simulate_agent_cycle` advanced `current["failed_at"]` to
exactly `plan["scheduled_for"]` (the retry's own firing time) AND set
`mandate_history["last_attempt_at"]` to that identical value. The next
loop iteration's cooling-off check (`current["failed_at"] -
last_attempt_at`) then compared that attempt against itself -- always a
0h gap, always < the 2h floor, regardless of the bucket's actual
`delay_hours` (3h for most of B3, well clear of cooling-off). This
silently capped every mandate at exactly 1 real retry attempt no matter
what the matrix said, for the entire batch, invisibly (no error, no test
failure -- just a systematically wrong number). **Fix:** advance
`current["failed_at"]` to `plan["scheduled_for"] + cooling_off_hours`,
not `plan["scheduled_for"]` itself -- the next decision point is modelled
as occurring no sooner than the cooling-off floor after the last attempt,
so resolve_action's own bucket-specific delay is computed from a point
that can't collide with itself. Slow buckets (delay > cooling-off)
barely notice; the handful of fast ones (B1's 0-1h delay,
`payment_timed_out`'s 0h) get floored up to 2h, which is arguably more
correct anyway. Regression test:
`test_agent_can_make_more_than_one_retry_attempt_when_delay_exceeds_cooling_off`
forces every draw to fail and asserts the loop doesn't get stuck at
exactly 1 attempt.

**After both fixes: aggregate uplift is still -17.4% Rs recovered
(though recovery RATE now favours the agent, +8.8 points, 46.8% vs
38.0%) -- investigated further per the user's explicit instruction to
check anomalous uplift, not just fix bugs and stop.** Root cause of the
REMAINING gap is not a bug: 67 events (Rs.865,773 of volume) get ZERO
autonomous action from the agent -- correctly escalated to a human
(high-value, risk-flagged, repeat-offender) or hard-stopped (cycle
expired, attempt cap, cooling-off) per M4's governance rules. Baseline
has no governance concept at all and blindly attempts every one of those
same events, sometimes recovering money the agent's policy deliberately
routed elsewhere. Counting those as "Rs.0 recovered by the agent" in a
2-arm $ comparison penalizes the agent for a genuine safety property
baseline doesn't have -- it does not model what a human reviewer would
recover after escalation (not zero, in reality). **Excluding those 67
zero-action events from both arms: uplift is +30.5%, recovery rate 54.0%
vs 40.2%** -- squarely inside the 10-50% defensible band, and this
isolates the actual retry-timing/classification intelligence from the
governance-routing decision. `metrics/report.py` prints BOTH numbers: the
literal full-population metric (matching the PRD's metric definition
exactly, for the headline) and this diagnostic breakdown (for
understanding what's driving it) -- not silently substituting one for
the other. The headline "Rs recovered" metric was NOT redefined to
exclude escalations; that's a framing decision for the pitch, left to the
user rather than decided unilaterally here.

**By-bucket note:** B4_STRUCTURAL (n=23, the smallest bucket) shows
baseline recovering 43.5% against an oracle probability of 20% per
context -- a ~2.8-sigma outcome for this specific seed on a small sample,
not a bug (verified: B4's context state is always `"always"`, i.e. one
shared draw per event since context never varies with retry timing;
23 independent per-event draws at p=0.20 have real sampling variance).
Worth knowing before citing per-bucket numbers from small buckets as if
they were precise.

**Kept out of scope, flagged for the record:** nudge-driven recovery
(`NUDGE_ACCEPTANCE_PROBABILITIES` in oracle.py, e.g. p=0.30 for a B5
reauth nudge being accepted) is still not wired into either simulation --
a `NUDGE_SENT` action is terminal for $-recovery purposes in both this
phase's agent simulator. This is a deliberate scoping choice, not an
oversight: modelling nudge acceptance would inject a second, different
recovery mechanism (customer behaviour, not retry timing) into a
comparison meant to isolate the timing/classification thesis specifically.
Available as a documented, ready-to-wire extension if the pitch wants to
show it separately.

## 2026-09-03 — Escalation modelled as human review, not permanent loss; full-population uplift re-run

**The problem with the -17.4% headline, per the user:** the prior phase's
$-recovered comparison modelled every `ESCALATE`d event as Rs.0 recovered
forever. That's not what escalation means — a human reviews the case and
approves or rejects it, usually within a business day. Modelling it as a
black hole made governance look like pure cost, which is neither
realistic nor the point of building governance in the first place.
Excluding escalated events from the comparison (the prior phase's
diagnostic +30.5%) wasn't accepted as the fix either — that's a different
kind of dodge, just hiding the population instead of mis-modelling it.

**Fix: human review as delay-and-filter** (`executor/simulate.py`,
`executor/executor.py`). When `resolve_action` returns `HUMAN_QUEUE`, the
simulation now waits 12h (ASSUMPTION) then draws approval at 70%
(ASSUMPTION) — both cited in the README ("Human review of escalated
events") next to the oracle's own assumption block, not asserted as
measured. Approved cases advance the clock past the review delay and
re-enter the SAME classify → policy → resolve_action → oracle path as
every other event, with no special-cased recovery logic — the oracle call
(`draw_retry_outcome`) and its `context_outcomes` cache are unchanged.
Rejected cases (30%) end the cycle not-recovered.

`resolve_action` gained a `human_approved: bool = False` parameter
(default preserves all existing test behaviour — 79/79 tests still pass
unmodified) rather than duplicating its matrix-lookup logic in
`simulate.py`, per M1's "no decision logic hardcoded elsewhere"
principle. Approval doesn't blanket-override the policy verdict: if the
same decision also carries one of rule 5-9's independent BLOCK reasons
(hard decline, cycle expired, attempt cap, cooling-off, max contacts —
`_BLOCK_ONLY_REASONS` in `executor.py`), it still resolves to BLOCK after
approval, same worst-wins principle used throughout M4/M5. A pure
ESCALATE (no BLOCK reason riding along) resolves to ALLOW, and the
matrix's own per-code `ESCALATE_HUMAN` preference is skipped once
approved (the human already reviewed this case; it doesn't route back to
the queue a second time — verified no infinite loop for
`mandate_amount_exceeded`, whose matrix action is `ESCALATE_HUMAN` with
no underlying retry/nudge play, so an approved instance now resolves to
`STOPPED`, i.e. "human handled it outside the automated system").

**Result, seed=42/20260903, full population, no exclusions:**

- Rs recovered uplift: **-17.4% → +50.1%** (agent Rs.892,622 vs baseline
  Rs.594,520). Recovery rate: 51.0% vs 37.2% (+13.8 points — a smaller,
  more plausible move than the $ swing, see below).
- 50 events escalated, 35 approved (70.0%, matches the target rate — not
  tuned, just what a 500-event batch produces), 15 rejected.
- The $ swing is much larger than the recovery-rate swing because
  escalation correlates heavily with `high_value_amount`
  (`policy.py` rule 4) — the events previously locked out of the agent's
  $-recovered total at Rs.0 were disproportionately the *largest*
  payments in the batch. Once ~70% of them proceed through the normal
  recovery play (at ordinary per-bucket recovery odds), they add a
  disproportionate share of rupees relative to their share of event
  count. This is not a bug — it is exactly the dollar-weighted effect
  you'd expect once big-ticket escalations stop being modelled as
  automatic write-offs.
- **Honest caveat, not smoothed over:** `metrics/report.py`'s own sanity
  check flags anything above ~50% as "may be too generous, consider
  tuning" — +50.1% sits right on that line, not comfortably inside the
  10-50% band the prior phase called defensible. Not retuned to force it
  under the flag; that would be gaming the number the same way excluding
  escalated events would have been. Worth re-checking after any future
  change to the amount distribution, escalation threshold, or oracle
  probabilities, per the existing "re-run `python -m metrics.report`
  before calling it done" discipline.

## 2026-09-03 — B4_STRUCTURAL 0% recovery: confirmed genuine decision-matrix gap, not sampling noise

The user asked for this to be checked specifically, since the prior
phase's 0% agent / 43.5% baseline B4 comparison had been attributed to
small-n sampling variance (n=23). Re-investigated by instrumenting the
seed=42 batch's 23 `B4_STRUCTURAL` events directly
(`resolve_action`'s first decision per event, before any retry loop):

**1 of 23 events gets a scheduled retry. 20 of 23 get `NUDGE_SENT` as
their first and only action. The remaining 2 escalate.** This is not a
scheduling bug — it's `decision_matrix.yaml` working exactly as
specified: 8 of B4's 11 reason codes (`incorrect_otp`,
`otp_attempts_exceeded`, `otp_expired`, `pin_attempts_exceeded`,
`transaction_on_vpa_restricted`, `authentication_failed`,
`incorrect_cvv`, `credit_limit_exceeded`, `transaction_limit_exceeded`,
`transaction_frequency_limit_exceeded`) carry `action:
NUDGE_CUSTOMER_LINK` / `NUDGE_REENGAGE` with `max_attempts: 0` — the
matrix's own design, matching `recovery-decision-matrix.md`'s original
table verbatim ("Customer-initiated retry link (agent cannot fix OTP
autonomously)"). Only `transaction_daily_count_exceeded` has an
autonomous `RETRY` play.

**Why that reads as 0% $-recovered:** per the existing, previously-flagged
scoping decision (see the Phase 6 entry above, "nudge-driven recovery...
kept out of scope"), `NUDGE_SENT` is terminal for $-recovery purposes in
this simulation — `NUDGE_ACCEPTANCE_PROBABILITIES["B4_STRUCTURAL_channel_switch"]
= 0.45` exists in `generator/oracle.py` but is not wired into
`executor/simulate.py`. So B4's simulated $-recovery is effectively
gated entirely on that single retryable code (which, on this seed, didn't
recover — one Bernoulli(0.20) draw, 80% chance of exactly this outcome,
so *that* part genuinely is small-sample noise) while every nudge-routed
event contributes Rs.0 by construction, not by chance.

**This compounds with the baseline's blindness in the wrong direction for
a bucket comparison:** baseline has no concept of "this needs a customer
action, not a blind retry" — it retries all 23 B4 events regardless of
code, including the ones the matrix deliberately routes to a
customer-facing nudge because the agent structurally cannot fix them
(wrong OTP, expired OTP, limit exceeded) by retrying the SAME failed
attempt. So baseline's ~20-22% recovery on B4 isn't "baseline handling
structural failures better" — it's the oracle's flat B4_STRUCTURAL
p=0.20 applying to blind retries Razorpay's real system also would not
expect to work for an OTP/limit failure (retrying an expired OTP without
a fresh customer action doesn't make sense against a real bank/PSP
either — the oracle's flat 0.20 for B4 is itself the least-scrutinized
number in `ORACLE_PROBABILITIES`, carried over unchanged from the PRD's
original spec with no bucket-specific reasoning recorded anywhere).

**Verdict: genuine gap, not noise — flagged, not fixed, per the user's
question (investigate, don't just patch).** Two independent things drive
B4's number, and both are real, not artifacts of this seed:
1. The decision matrix correctly withholds autonomous retry for
   8 of 11 B4 codes (matches source doc; not a bug to fix).
2. The simulation doesn't model nudge acceptance as recovered revenue
   for ANY bucket (B4 or B5), which was an explicit, previously-recorded
   scope decision, not an oversight — but it lands hardest on B4
   specifically because B4 is overwhelmingly nudge-routed, unlike B5
   where nudge-only was always going to mean "recovers via a different,
   unmodelled mechanism" that nobody claimed was in scope.

**Left as a decision for the user, not resolved unilaterally:** wiring
`NUDGE_ACCEPTANCE_PROBABILITIES` into the $-recovery simulation (for both
B4 and B5) would make the by-bucket comparison for those two buckets
meaningful again — right now B4's row in `metrics/report.py`'s by-bucket
table should be read as "agent routes to nudge, doesn't blindly retry",
not "agent recovers less money than baseline on structural failures". If
B4's by-bucket row is going in front of a judge, caveat it explicitly
rather than presenting it next to B1/B2/B3's rows as if they measure the
same thing.

## 2026-09-03 — Nudge acceptance wired into $-recovery; honest uplift is +29.3%

**The problem, per the user:** `NUDGE_ACCEPTANCE_PROBABILITIES` (0.30 B5
reauth, 0.45 B4 channel-switch) had sat in `generator/oracle.py`, unused,
since Phase 2 — every `NUDGE_SENT` action scored Rs.0 in the $-recovered
comparison, while every baseline blind retry could still win. That's not
conservatism, it's a hole in the metric: the agent got zero credit for
correctly choosing "this needs a customer action, not a blind retry"
instead of just not modelling that choice's payoff. Landed hardest on
B5_DEAD, which sends nothing but reauth nudges by construction (retrying
a dead instrument is never attempted) and was scoring a flat, structural
0% recovery regardless of how many customers would realistically reauth
in reality.

**Fix** (`generator/oracle.py::draw_nudge_acceptance`,
`executor/simulate.py`'s `NUDGE_SENT` branch): a nudge now draws
acceptance from `NUDGE_ACCEPTANCE_PROBABILITIES`, keyed off the
decision's `effective_bucket` via the new `NUDGE_ACCEPTANCE_KEY_BY_BUCKET`
map (B5_DEAD → 0.30, B4_STRUCTURAL → 0.45 — the only two buckets the
matrix ever routes to `NUDGE_SENT`, so this covers every real case, with
a fail-closed p=0.0 default for anything else). Uses the SAME
`context_outcomes` correlated-draw cache pattern as `draw_retry_outcome`
— per the user's explicit instruction, no special treatment — keyed as
`(bucket, "nudge_acceptance")`, a marker string that cannot collide with
a real `(bucket, context-state)` retry key (verified with a dedicated
test: `test_draw_nudge_acceptance_does_not_collide_with_a_retry_outcome_key`).
An accepted nudge recovers the full amount **24h** after being sent
(ASSUMPTION, `NUDGE_ACCEPTANCE_DELAY_HOURS` — "customers don't act
instantly," no data to calibrate against, documented in the README next
to the human-review delay it parallels). A rejected nudge ends the cycle
not-recovered, same shape as a rejected human review. 8 new tests
(`tests/test_simulation.py`), 91 total project-wide.

**Baseline: explicitly NOT given an equivalent, and said so out loud, per
the user's instruction.** `baseline/baseline.py` is retry-only — it has
no customer-facing action at all, so there's nothing to wire in. Rather
than let that asymmetry sit silently in the code,
`metrics/report.py` now prints it directly: "Baseline sends 0 nudges...
this is a real, structural asymmetry between the two arms, not a metrics
gap." The agent's B4/B5 advantage is now visibly coming partly from
taking an action baseline structurally cannot take, not only from better
retry timing — worth saying explicitly if asked in Q&A, not something to
let a judge assume without the caveat.

**Result, seed=42/20260903, full population, no exclusions: uplift
+50.1% → +29.3%** (agent Rs.914,099 vs baseline Rs.706,980; recovery rate
51.8% vs 40.6%). This is now **inside** `metrics/report.py`'s own 10-50%
"defensible band" check, not sitting at its edge — the user's stated
preference ("a defensible metric over a comfortable one") was satisfied
by wiring the fix honestly, not by retuning anything; the number moved
because a real gap in the model closed, and it happened to land inside
the band rather than needing to be pulled there.

- 65 nudges sent (43 B5 reauth, 22 B4 channel-switch), 19 accepted
  (29.2% — close to the blended expectation given the B5/B4 mix at their
  respective 30%/45% probabilities), Rs.83,446 recovered via nudge
  acceptance alone.
- B5_DEAD by-bucket: agent recovery **0% → 12.7%** (Rs.53,609) — no
  longer a flat zero; still well below B4/baseline-attempt-based buckets,
  which is expected (0.30 is the lowest acceptance rate in the table and
  most B5 codes are genuinely dead instruments).
- B4_STRUCTURAL by-bucket: agent recovery **0% → 43.5%** (Rs.29,837),
  now clearly ahead of baseline's 26.1% (Rs.25,789) on this bucket
  instead of reading as a loss — directly answers the prior session's B4
  investigation (see the entry above): the agent wasn't failing on B4, it
  was correctly routing to nudges the simulation didn't yet credit.
- Human review counts shifted slightly from the prior run (50 escalated,
  36 approved this time vs 35 before) purely because nudge-acceptance
  draws are new calls against the same shared, sequentially-consumed
  seeded RNG (`metrics/report.py`'s `SIMULATION_SEED`) — inserting new
  draws upstream shifts what every later draw in the sequence produces,
  for both arms (baseline's own Rs recovered moved too, 594,520 → 706,980,
  for the identical reason, not because baseline changed at all).
  Expected and still fully reproducible given the code as it now stands,
  not nondeterminism.

## 2026-09-03 — Independent deterministic RNG streams; multi-seed robustness check

**Problem 1, per the user:** the whole comparison shared ONE seeded
`random.Random`, consumed sequentially across the batch (agent's draws
for event 1, baseline's draws for event 1, event 2's, ...). That's a
defensibility problem, not just a code-cleanliness one: inserting a NEW
draw anywhere upstream shifts the RNG state for every draw after it, for
BOTH arms, even when nothing about the thing being drawn changed. Already
observed in practice at the end of the last session: wiring nudge
acceptance shifted baseline's own Rs-recovered figure (594,520 →
706,980) even though baseline's code hadn't changed at all. That means
"the baseline recovers Rs.X" was never a stable, citable fact — it moved
whenever unrelated agent-side code changed.

**Fix** (`app/rng.py`, new module): `deterministic_random(seed, *parts)`
derives a fresh `random.Random` from a SHA-256 hash of `(seed, *parts)`
(SHA-256, not Python's builtin `hash()` — `hash()` on a `str` is
randomized per-process via `PYTHONHASHSEED` and is NOT reproducible
across runs). Every draw site now derives its own independent stream:

- `generator/oracle.py::draw_retry_outcome` — keyed
  `(seed, event_id, "retry", bucket, context_state)`.
- `generator/oracle.py::draw_nudge_acceptance` — keyed
  `(seed, event_id, "nudge", bucket)`; gained an `event` parameter (was
  bucket-only) since the event id is now part of the key.
- `executor/simulate.py`'s human-review approval draw — keyed
  `(seed, event_id, "human_review")`.

All three functions that used to take a shared `rng: random.Random`
(`draw_retry_outcome`, `draw_nudge_acceptance`, `simulate_agent_cycle`,
`simulate_baseline_cycle`) now take `seed`, normally an int. The
`context_outcomes` correlation cache (same event + same context ⇒ same
outcome, shared between arms) is UNCHANGED — this only changes where the
entropy for a brand-new key comes from, not the correlation mechanism
itself. `deterministic_random` also accepts an existing `random.Random`
directly (used as-is, ignoring `parts`) — a documented test-only escape
hatch so existing tests that force a specific outcome (`AlwaysSucceedRng`
etc.) keep working unchanged rather than needing to reverse-engineer an
integer seed that happens to produce the desired float; production code
always passes a plain int.

**Verified, not just asserted:** 6 new tests in `tests/test_rng.py`
(reproducibility, independence across parts/event-id/seed, the
test-escape-hatch pass-through) plus
`tests/test_simulation.py::test_agent_cycle_outcome_is_order_independent_across_events_with_same_int_seed`,
which simulates one event before and after an unrelated event with the
same seed and asserts identical outcome/attempts/recovery-time either
way — the exact property a shared sequential stream could not guarantee.
99 tests total, all passing.

**Consequence, stated plainly rather than smoothed over: the headline
number moved again on the same nominal seed, because the actual draws
are now genuinely different, not because anything is broken.**
seed=42/20260903 (batch_seed/sim_seed) now reads **+60.1%** (agent
Rs.1,020,617 vs baseline Rs.637,405), vs the prior session's +29.3%. This
is not a bug and not a "which one is correct" question — the old number
was produced by a sequential-consumption architecture that is no longer
in the code; the new number is a different, independently-drawn sample
under the fixed architecture. Neither number, on its own, was ever a
sound thing to present as *the* uplift — which is exactly what problem 2
was about.

**Problem 2, per the user: report the uplift across several seeds, not
one.** `metrics/report.py` was refactored to expose
`run_comparison(batch_seed, sim_seed, matrix) -> dict` (pure, returns
every number instead of printing) with `main()` now a thin wrapper that
calls it once (seed 42/20260903) and prints the full detailed report
(`print_full_report`). New `metrics/multi_seed.py`
(`python -m metrics.multi_seed`) runs `run_comparison` across 5 fixed
seeds — `SEEDS = [42, 7, 123, 2024, 55555]`, chosen once before first
run and not adjusted afterward — pairing each seed as BOTH `batch_seed`
and `sim_seed`, so each run generates a genuinely different 500-event
population AND a genuinely different set of draws, not just different
dice on the same fixed batch.

**Result:**

| seed | agent Rs | baseline Rs | uplift | rate delta |
|---|---|---|---|---|
| 42 | 828,296 | 640,159 | +29.4% | +14.6pt |
| 7 | 845,095 | 602,314 | +40.3% | +17.0pt |
| 123 | 996,061 | 781,612 | +27.4% | +14.0pt |
| 2024 | 780,333 | 762,468 | **+2.3%** | +14.6pt |
| 55555 | 988,532 | 745,698 | +32.6% | +17.2pt |

Uplift: min +2.3%, mean +26.4%, max +40.3%, stdev 14.3 points (n=5).
Recovery-rate delta: min +14.0, mean +15.5, max +17.2 points.

**Honest read, per the user's explicit preference for defensible over
comfortable:**
- **Recovery rate is the stable, presentable number** — a tight
  14.0-17.2 point band across 5 independently-generated populations. The
  agent reliably recovers more OF THE FAILURES, seed after seed.
- **Rs-recovered uplift is NOT stable** — it ranges from +2.3% to
  +40.3%, a 38-point spread on n=5, and one seed (2024) falls below
  `metrics/report.py`'s own 10% "may not be materialising" flag. A
  single-seed Rs-uplift number (whichever seed) should never be
  presented alone; it should be given as the range with the mean, and
  ideally alongside the recovery-rate delta, which doesn't have this
  problem.
- **Root cause of the spread, checked, not just observed:** seed 2024's
  detailed report (`run_comparison(2024, 2024, matrix)`) shows
  B3_TRANSIENT's recovery RATE is close between arms (70.8% agent vs
  72.3% baseline) but its Rs-recovered is not (Rs.293k vs Rs.417k) — a
  small number of high-value events (log-normal amount distribution,
  real tail — see the amount-distribution entry above) landing on
  different sides of an otherwise-close race is enough to swing a
  dollar-weighted metric substantially without the underlying
  recovery-rate advantage moving much at all. This is a genuine property
  of measuring a skewed-amount population with n≈500, not a modelling
  bug, and not something to fix by tuning — it's the reason a range
  matters more than any single point estimate here.
- **Not tuned to narrow the spread.** The spread is the finding, per the
  user's stated preference: "if the spread is wide that's something I
  need to know now." It is wide. Recorded here rather than smoothed into
  a single reassuring number.

## 2026-09-03 — 20-seed robustness check; M8 dashboard spec updated (not yet built)

**Extended the multi-seed check from n=5 to n=20** per the user
("n=5 gives a very noisy estimate of the mean, and 20 runs cheaply").
`metrics/multi_seed.py`'s `SEEDS` grew from `[42, 7, 123, 2024, 55555]`
to those 5 plus 15 more small ascending integers
(`1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 15, 17, 19, 21`) — kept the
original 5 for continuity with the prior report, chosen once and not
adjusted after seeing results, same discipline as before. Also added an
explicit "how many seeds fall below the +10% floor" count, since the
user specifically wanted to be able to say with a number whether seed
2024's original +2.3% was a tail case or a common outcome.

**Result, n=20:** uplift min +2.3%, mean +24.3%, max +68.5%, stdev 15.2
points. Recovery-rate delta: min +12.0, mean +15.1, max +18.6 points.
**5 of 20 seeds (25%) fall below the +10% floor** (2024, 4, 10, 19, 21 —
+2.3% to +8.6%), 1 of 20 (5%) falls above the +50% ceiling (seed 3,
+68.5%).

**Answer to the user's actual question: seed 2024 was NOT a tail case.**
At 25%, a sub-10% Rs-uplift outcome happens roughly 1 run in 4 — common,
not rare. The n=5 sample last session (1 of 5 below +10%, 20%) had
already pointed the same direction; n=20 confirms it with a number
instead of a vibe. Recovery-rate delta stayed tight (12.0–18.6 points)
across all 20 seeds, same conclusion as before, now on 4x the sample:
**recovery rate is stable, Rs-recovered uplift is not, and roughly a
quarter of the time the Rs-uplift figure alone would read as "the agent's
advantage isn't materialising" even though the recovery-rate advantage is
holding steady underneath it.** Not tuned to narrow this — per the user,
the variance is a real property of a skewed-amount population (log-normal
`AMOUNT_DISTRIBUTION_INR`, see the amount-distribution entry above) and
should be reported, not hidden.

**M8 dashboard spec (`PRD-mandate-recovery-agent.md` sec. "M8 —
Dashboard", not yet built) is superseded here, per the user, so it isn't
built the old way:**

The PRD's headline strip spec — "₹ recovered by agent vs baseline, and
the delta — largest text on the page" — is now WRONG given the finding
above: a single-run ₹ figure is the least stable number this project
produces (a ~25% chance of landing under the informal "materialising"
floor on any given run), while recovery-rate delta is the stable one. Do
not build M8 against the PRD's literal headline spec. Updated spec:

- **Headline strip (largest text on the page):** the **recovery-rate
  delta**, in percentage points (agent recovery % vs baseline recovery
  %), NOT the ₹ figure. This is the number that has held up across 20
  independently-generated populations (12.0–18.6 points) and is what
  should anchor the page.
- **₹ recovered:** still shown, prominently, but as a **range** (min /
  mean / max across seeds — the exact numbers `metrics/multi_seed.py`
  already computes), never a single figure presented as THE uplift. A
  single-run ₹ number is fine to show as "this run's result" alongside
  the range, but must not stand alone as the page's primary claim.
- Sections 2-4 (by-bucket comparison chart, governance panel, audit
  trail table) are unaffected by this change.
- **Acceptance criterion, updated from the PRD's "Dashboard shows a clear
  ₹ recovered delta over baseline":** dashboard shows a clear
  recovery-rate delta over baseline as the headline, AND shows the ₹
  recovered uplift as a range (not a single figure), both visible without
  scrolling.
- **The precomputed-fixture vs. live-sweep-endpoint question is
  RESOLVED — see the dated entry below ("M8 dashboard: precomputed
  fixture, not a live sweep endpoint").** Precomputed fixture, not a live
  endpoint.

## 2026-09-03 — M8 dashboard: precomputed fixture, not a live sweep endpoint

Resolved the open question left in the previous entry (fixture vs. a
new server-side sweep endpoint), per the user's explicit call and
reasoning: **precomputed fixture. A 20-seed sweep is too slow to run
live in a demo, and anything that can hang on stage will.** (Sub-second
in this session's own timing, yes — but "sub-second on a dev machine
mid-build" and "reliably fast on the specific laptop and network used
for a live judged demo" are different claims, and the failure mode of
guessing wrong on stage — a spinner, a judge's attention drifting — is
worse than the cost of a slightly-stale precomputed number.)

**Implementation** (`backend/metrics/multi_seed.py`, refactored;
`backend/metrics/output/multi_seed_range.json`, new, committed):

- `run_sweep(seeds, matrix) -> dict` — pulled out of `main()` — runs
  `metrics.report.run_comparison` per seed and reduces to summary stats
  (min/mean/max/stdev uplift, min/mean/max rate delta, below-floor and
  above-ceiling seed lists) plus small per-seed rows (aggregate numbers
  only — NOT the full `agent_records`/`baseline_records`, which are
  `batch_size * 2` per-event dicts per seed and irrelevant to a range
  summary).
- `write_fixture(sweep, matrix, out_path)` writes
  `metrics/output/multi_seed_range.json` — same "committed generated
  artifact" pattern this project already uses for
  `generator/output/events_seed42.json` (`generator/__main__.py`), same
  2-space-indent JSON style. Contains: the sweep summary, every per-seed
  row, `SEEDS`, the floor/ceiling thresholds, and `engine_version` /
  `matrix_version` for audit traceability (same two fields every
  `Decision` record stamps — see M5).
- **`python -m metrics.multi_seed` now BOTH prints the human-readable
  report AND writes the fixture** — one command, one source of truth, no
  separate "generate the fixture" step to forget.
- **The fixture is explicitly NOT a magic file, per the user's
  instruction** — its own `_note` field says so in plain language
  ("Cached snapshot, not a hand-maintained or magic file — regenerate any
  time with `python -m metrics.multi_seed`"), and `DECISIONS.md` (this
  entry) says the same. Anyone touching `SEEDS`, the simulation, or the
  matrix should re-run the command and re-commit the regenerated file —
  it is fully reproducible (`app/rng.py`'s independent deterministic
  streams; verified by `test_run_sweep_is_reproducible`), so a stale
  fixture is a "forgot to regenerate" bug, not a "someone hand-edited a
  number" risk.
- 4 new tests (`tests/test_multi_seed.py`, 103 total project-wide):
  summary-stats arithmetic, reproducibility, floor/ceiling partition
  correctness, and the fixture's JSON shape.

**M8 dashboard spec, finalized (supersedes both the PRD's original
headline spec AND the "open question" left in the prior entry):**

- **"Run simulation" button still does ONE live run** (`POST
  /simulate/run` against a freshly generated batch) — the judge sees the
  system actually execute, not a static page. This is unchanged from the
  PRD.
- **Live run's numbers are displayed** — this run's ₹ recovered, recovery
  rate, by-bucket breakdown, audit trail — exactly as the PRD's sections
  2-4 already specify.
- **The 20-seed range is displayed alongside it, as context, sourced from
  the committed fixture** — NOT recomputed live, NOT a second live sweep
  triggered by the button. The dashboard reads
  `metrics/output/multi_seed_range.json` (bundled at build time or served
  as a static asset — an M8 build detail, not decided further here) and
  renders its `rs_uplift_pct` / `recovery_rate_delta_points`
  min/mean/max alongside the live run's own figures — e.g. "This run:
  +29.4% ₹ uplift, +14.6pt recovery-rate delta — across 20 seeds:
  +2.3–68.5% ₹ uplift (mean +24.3%), +12.0–18.6pt recovery-rate delta
  (mean +15.1pt)."
- **Headline strip (largest text on the page): recovery-rate delta**,
  from the LIVE run (not the fixture) — per the prior entry's finding
  that recovery rate is this project's stable, presentable number. The ₹
  figure is shown too, but paired with the fixture's range immediately
  beside/below it, never presented alone as though it were a fixed,
  precise claim.

## 2026-09-03 — Tooling

No Python 3.11 installed on this machine. `py -0p` listed a 3.13, but its
registered path (`OneDrive\Desktop\python.exe`) is broken — not a real
interpreter, venv creation fails against it. Using **3.10** for the backend
venv; nothing in the spec depends on a 3.11-only language feature.

## 2026-09-04 — Phase 7: API surface + M7 explanation layer

All seven PRD sec. 8 endpoints are built (`app/api.py`), plus M7
(`explain/`). 44 new tests, 147 total, all green. `python -m
executor.pipeline`, `python -m metrics.report` and `python -m
metrics.multi_seed` are unchanged in output — the multi-seed fixture
regenerates byte-identically, which is the check that Phase 7 didn't
perturb the simulation.

### The explanation prompt is narrower than the PRD's sketch — deliberately

PRD sec. M7 specifies two things that cannot both be true:

- a prompt carrying `reason`, `confidence`, `signals[]` and
  `scheduled_for`;
- caching by `(bucket, action, policy_verdict)`.

Every one of those four fields **varies between decisions that share that
cache key**. A sentence generated for one decision and then reused across
the key would state a retry time, a confidence, or a reason code that is
simply false for the other decisions it gets attached to. That is
fabricated content in an append-only audit record — the one failure mode
this system cannot have.

**Resolution: the prompt carries only the cache key and things derived
from it** (the bucket's `label` and `class`, read from the matrix).
Enforced by a test, not just by care —
`test_prompt_carries_nothing_beyond_the_cache_key` builds two contexts
that share a key but differ in every other field and asserts the rendered
prompts are byte-identical.

Nothing is lost. The per-decision specifics are in the audit record's own
structured fields, and the **template** path — rendered per decision,
never cached — does name the exact snapped retry time and the governance
rules that fired. On this project the fallback sentence is the more
specific of the two, which is an odd but honest outcome worth stating out
loud rather than hiding.

Alternative considered and rejected: widen the cache key to
`(reason, bucket, action, policy_verdict, tuple(signals))`. Measured on
the seed-42 batch this takes 23 distinct keys to 91 — still not 801,
so it does satisfy "don't make 500 API calls" — and it would let the
sentence name the reason code. Rejected because the user's spec named the
three-field key explicitly, and because the marginal value of the model
restating a reason code that is already displayed verbatim next to it in
the audit table is close to zero. It is a one-line change to
`CACHE_KEY_FIELDS` if that judgement changes.

### The async boundary is [DESIGN]; the non-blocking guarantee is [BUILD]

Per the user's instruction, `attach_explanations` runs **synchronously,
immediately after the decision commit** — no queue, no background worker.
architecture-and-security.md sec. 4.1 describes a worker filling
`explanation` in later; that split is the latency optimisation and is
explicitly not built.

What IS built is the property the worker was supposed to provide, and it
does not depend on the worker existing:

1. **Dependency direction.** `executor/executor.py`, `policy/policy.py`
   and `classifier/classify.py` do not import `explain`. Asserted by
   `test_decision_tier_does_not_import_the_explanation_layer`, which
   walks the AST of those three files — so it stays true when someone
   adds a convenient import in six months.
2. **It only ever sees committed rows.** Every entry point takes an
   `app.models.Decision` that already has a primary key and is durable.
3. **It writes two columns.** `explanation` and `explanation_source`.
   It cannot alter a bucket, verdict, action, schedule or outcome,
   because it never writes those columns.
4. **The one call site is guarded.** `app.api._explain_after_commit`
   catches `Exception` (not `BaseException` — KeyboardInterrupt and
   SystemExit must still stop the process) and rolls back only the
   explanation transaction.

`test_a_broken_explanation_layer_cannot_break_a_decision` makes the whole
layer raise and asserts the decision is committed, correct, scheduled,
and carries a NULL explanation.
`test_decision_is_identical_with_and_without_the_explanation_layer` is
the direct check of the claim in architecture-and-security.md sec. 9.

### FORBIDDEN_IN_PROMPT: the doc's assertion, plus the one that can fire

The architecture doc's sketch is:

```python
safe = {k: v for k, v in decision.items() if k not in FORBIDDEN_IN_PROMPT}
assert not (FORBIDDEN_IN_PROMPT & safe.keys())
```

That assertion is **tautological** — the comprehension on the line above
guarantees it. It documents intent; it cannot fail. It is kept verbatim
(good signalling value, and it is what the doc says), and two things were
added:

- **A value-level scan.** `build_explanation_prompt(context,
  forbidden_values=...)` searches the *rendered prompt string* for each
  excluded value and raises `PIILeakError`. This is the layer that
  catches a `customer_id` that reached the prompt *inside an allowed
  field* — a signal string, a badly templated bucket label — which the
  field-name filter structurally cannot see.
- **An exception, not an `assert`.** `assert` is compiled out under
  `python -O`. A data-governance control that disappears under an
  optimisation flag is not a control.

`PIILeakError` is also the **one** exception `Explainer.generate` does not
swallow into the template fallback: a leak is our bug, not a dependency
being down, and templating past it would hide exactly what the check
exists to surface. Tested by
`test_pii_leak_is_not_swallowed_by_the_fallback`.

Two fields were added to the exclusion set beyond the doc's five
(`amount_inr`, `mandate_id`, `event_id`, `customer_history`) on the same
P5 data-minimisation principle: they are what remains that ties a decision
to one individual's transaction, and the model does not need any of them
to explain a decision *class*.

### Measured: 801 decisions, 23 API calls

Seed-42, 500 events, the full `/simulate/run`:

| | |
|---|---|
| decisions written | 801 |
| distinct `(bucket, action, policy_verdict)` | 23 |
| API calls | 23 |
| calls avoided by the cache | 778 (97%) |

The repo ships no API key, so in the default state all 801 explanations
come from templates and the API-call count is 0. The 23 above was
measured with a stub client injected (`explain/demo.py::StubClient`).

### Deviations and known gaps, stated plainly

- **The webhook accepts the PRD sec. 6 flat `FailureEvent`, not
  Razorpay's `{"event": ..., "payload": {"payment": {"entity": ...}}}`
  envelope.** The PRD calls it "Razorpay-shaped" but then defines the
  payload as the flat model, and that flat model is the project's single
  contract across every module. An envelope adapter is ~20 lines and no
  insight; every interesting property of the endpoint (constant-time HMAC
  over raw bytes, dedupe, fail-closed) is identical either way. Gap, not
  done.
- **No sample explanation from the real model has been produced.** No
  `ANTHROPIC_API_KEY` is configured on this machine. `python -m
  explain.demo` runs the LLM code path against a stub whose sentences are
  hardcoded in `explain/demo.py`, and prints a four-line capitalised
  warning saying so. Run `ANTHROPIC_API_KEY=sk-... python -m explain.demo`
  for a genuine one. Do not screenshot stub output as model output.
- **Replay protection (timestamp skew > 5 min) is not implemented.** It
  is [DESIGN] in architecture-and-security.md sec. 3.5 and stays there;
  the webhook payload carries `failed_at`, not a delivery timestamp, so
  implementing it properly needs a header Razorpay sends that we do not
  model.
- **Rate limiting is not implemented.** Same section, same reason.

### Smaller decisions

- **`Decision.explanation_source`** (`llm` | `template` | NULL) is a new
  column, not in the PRD's Decision model. "The audit record is the
  product" (sec. 6) argues an auditor must be able to tell a generated
  sentence from a fallback one without inferring it from prose style.
- **`SimulationRun`** is a new table, also not in the PRD's four.
  `/results/summary` has to answer "what did the last run measure?"
  across a restart, and the *baseline* arm's per-event records exist
  nowhere else — baseline writes no decisions because it makes none.
  Storing the computed metrics rather than 500 baseline rows keeps this
  to one small row per run.
- **`simulate_agent_cycle` now returns the decisions it took.** The
  alternative was for `/simulate/run` to replay classify/policy/execute a
  second time to produce audit rows. Two independent replays that could
  silently drift is a worse property than one extra key on a return
  value; `compute_metrics` ignores the key entirely.
- **`execute_decision` gained an optional `mandate_state`.** The counters
  the policy engine's attempt-cap / cooling-off / contact-limit rules read
  are now advanced inside the *same transaction* as the decision write. A
  caller committing them separately could crash in between and leave a
  scheduled attempt the cap doesn't know about — precisely the atomicity
  sec. 5.2 asks for. Callers that don't track state (the in-memory batch
  runners, the unit tests) pass nothing and are unaffected.
- **Column defaults bit us once.** SQLAlchemy's `default=0` applies at
  INSERT, so a freshly constructed `MandateState` still has `None` in its
  counters — and the policy engine reads the object before it is flushed
  (`None >= 3` is a TypeError). Found on the first end-to-end webhook run.
  `load_or_open_mandate_state` now sets every counter explicitly.
- **`/reset` and `/simulate/run` are deliberately distinct.** `/reset`
  clears the DB and regenerates the batch as *raw events only*;
  `/simulate/run` clears, decides, and measures. Blending two batches'
  numbers in `/results/summary` would be a worse property than a
  resettable demo DB.
- **`claude-opus-5`, `max_tokens=1000`.** Generous for two sentences on
  purpose: thinking is on by default on Opus 5 and counts against
  `max_tokens`, so a tight cap risks the budget being spent before any
  visible text is emitted — which surfaces as an empty explanation, not
  an error. An empty response is treated as a failure and falls back
  (`test_every_failure_mode_falls_back_to_a_non_empty_template`,
  `empty_response` case). The cache means this ceiling is paid ~23 times
  per batch.
