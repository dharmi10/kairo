# Kairo

**An autonomous recovery agent for failed UPI AutoPay mandates.**

Over 20 million UPI AutoPay mandates are revoked every month. Razorpay
diagnoses every failed debit precisely — `source`, `step`, `reason` — but
retries them all identically: three times, at T+1/T+2/T+3 days, each at
the same time of day as the original failure. Since May 2026, NPCI's
traffic-management framework deprioritises automated mandate traffic
between 10AM and 1PM — so a debit that failed at 10:30 gets retried at
10:30, three days running, straight back into the window that killed it.
Razorpay's own documentation says building the remedial logic on top of
these diagnostics is the merchant's job, not theirs. Kairo is that
missing layer.

## What Kairo does

| | |
|---|---|
| **DETECT** | Ingest a failed mandate event — a real Razorpay webhook or the synthetic equivalent — HMAC-verified, deduped. |
| **DIAGNOSE** | Classify the failure into one of five root-cause buckets (congestion, balance timing, transient, structural, dead) with a confidence score and signals. |
| **DECIDE & EXECUTE** | A governance layer enforces attempt caps, cooling-off, contact limits, and NPCI-window snapping — before anything is scheduled. |
| **GOVERN & REPORT** | Every decision is an immutable, plain-language-explained audit record, measured live against Razorpay's own baseline retry behaviour. |

## The result

**+12 to +18.6 recovery-rate points** over Razorpay's baseline retry,
agent vs. baseline, across 20 independently-generated 500-event
populations. Rupee uplift is a **range, not a single figure** — +2.3% to
+68.5% (mean +24.3%) across the same 20 seeds, because a skewed amount
distribution swings a rupee-weighted number far more than the underlying
recovery-rate advantage moves.

Every simulation assumption and its reasoning is documented in
[`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md).

## Quickstart (Windows, from a clean clone)

Two halves that talk over plain HTTP: a Python/FastAPI backend and a
Vite/React frontend. Start the backend first.

**Backend** — PowerShell, from the repo root (Python 3.10; see
`DECISIONS.md` for why not the PRD's stated 3.11):

```powershell
cd backend
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m pytest                          # 182 tests, ~20s
.venv\Scripts\python.exe -m uvicorn app.main:app --reload    # leave running
```

`.env`'s defaults work as-is — a dummy webhook secret, a local SQLite
file, no Anthropic key. No key means every explanation in the demo comes
from a deterministic template, not Claude; the system's decisions and
audit trail are unaffected either way (see `docs/ASSUMPTIONS.md`).
`http://127.0.0.1:8000/health` should return `{"status": "ok", ...}`.

> **`.venv\Scripts\Activate.ps1` blocked?** PowerShell's default
> execution policy refuses to run it on a fresh machine —
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or
> use `.venv\Scripts\activate.bat` from `cmd.exe` instead. Every command
> above uses the `.venv\Scripts\python.exe ...` form specifically so it
> works with no activation step at all.

**Frontend** — second terminal, backend still running:

```powershell
cd frontend
npm install
npm run dev      # http://localhost:5173
```

You'll land on an empty state until a simulation has run — see below.

## Demoing fail-closed live

An unrecognised reason code must never be guessed at — it routes to a
human, on every input, no exceptions. Prove it against the real running
API:

```powershell
cd backend
.venv\Scripts\python.exe -m demo.seed
```

This runs the canonical, reproducible 500-event comparison (seed 42),
then POSTs one more event — HMAC-signed like any other — carrying a
reason code that doesn't exist in `config/decision_matrix.yaml`. Reload
the dashboard and search `evt_demo_unknown_reason_code` in the audit
trail (or filter Bucket = `B_UNKNOWN`):

| Field | Value | What it proves |
|---|---|---|
| `classified_bucket` | `B_UNKNOWN` | No bucket was guessed for a code the system doesn't recognise |
| `confidence` | `0.0` | Zero, not a hedge — "no information," distinct from "not confident enough" |
| `policy_verdict` | `ESCALATE` | The data-quality gate fires before any bucket-based rule is trusted |
| `action` | `HUMAN_QUEUE` | Routed to a person, never auto-retried |
| `scheduled_for` | `null` | No money-moving action was scheduled |

`demo/seed.py` asserts this table itself and exits non-zero if it's ever
wrong — a live regression check, not just a script.

## Repo map

```
backend/classifier/    reason code -> bucket, confidence, signals
backend/policy/        governance: attempt caps, cooling-off, escalation
backend/executor/      NPCI-window snapping, scheduling, full-cycle simulation
backend/tests/         182 tests, one file per module above
frontend/              React + Vite + Recharts dashboard
docs/                  original planning docs + docs/ASSUMPTIONS.md
```

## Built vs. designed

Everything above is built and tested. **Designed, not built in 48
hours** (`docs/architecture-and-security.md` sec. 3.5/4.1): per-merchant
rate limiting, webhook replay protection, and the async queue between a
webhook ACK and its decision — straightforward extensions of what's
here, not attempted in this window.

---

See [`DECISIONS.md`](DECISIONS.md) for every design decision, every
divergence from the planning docs in `docs/`, and every bug found during
the build, in chronological order.
