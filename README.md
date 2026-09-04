# Kairo

**An autonomous recovery agent for failed UPI AutoPay mandates.**

Over 20 million UPI AutoPay mandates get revoked every month. Razorpay tells you exactly why each debit failed (`source`, `step`, `reason`), but then retries every failure the same way: three attempts, on T+1, T+2 and T+3, each at the same time of day as the original try. Since May 2026, NPCI has also started deprioritising automated mandate traffic between 10AM and 1PM, so a payment that failed at 10:30 just gets retried at 10:30 again, three days running, straight back into the window that killed it the first time. Razorpay's own docs even say building the actual recovery logic on top of these diagnostics is the merchant's job, not theirs. Kairo is that missing piece.

## What Kairo does

| | |
|---|---|
| **DETECT** | Ingest a failed mandate event (a real Razorpay webhook or the synthetic equivalent), HMAC-verified and deduped. |
| **DIAGNOSE** | Classify the failure into one of five root-cause buckets (congestion, balance timing, transient, structural, dead), with a confidence score and signals. |
| **DECIDE & EXECUTE** | A governance layer checks attempt caps, cooling-off, contact limits and NPCI window timing before anything actually gets scheduled. |
| **GOVERN & REPORT** | Every decision becomes an immutable, plain-language audit record, measured live against Razorpay's own baseline retry behaviour. |

## The result

The agent beats Razorpay's baseline retry by **12 to 18.6 recovery-rate points**, agent vs. baseline, tested across 20 independently generated 500-event runs. The rupee uplift is less stable, and we report it as a range rather than one number: +2.3% to +68.5% (mean around +24%) across those same 20 seeds. That's because a skewed amount distribution means a handful of large payments can swing a rupee-weighted number a lot more than the underlying recovery-rate advantage actually moves.

Every simulation assumption and its reasoning is documented in [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md).

## Quickstart (Windows, from a clean clone)

Two halves that talk over plain HTTP: a Python/FastAPI backend and a Vite/React frontend. Start the backend first.

**Backend.** PowerShell, from the repo root. This uses Python 3.10 (see `DECISIONS.md` for why, not the 3.11 the PRD originally asked for):

```powershell
cd backend
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m pytest                          # 182 tests, ~20s
.venv\Scripts\python.exe -m uvicorn app.main:app --reload    # leave running
```

The defaults in `.env` work as-is: a dummy webhook secret, a local SQLite file, no Anthropic key. Without a key, every explanation in the demo comes from a deterministic template instead of Claude, but the decisions and the audit trail are unaffected either way (see `docs/ASSUMPTIONS.md`). `http://127.0.0.1:8000/health` should return `{"status": "ok", ...}`.

> **If `.venv\Scripts\Activate.ps1` won't run:** PowerShell's default execution policy blocks it on a fresh machine. Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or just use `.venv\Scripts\activate.bat` from `cmd.exe` instead. Every command above uses the `.venv\Scripts\python.exe ...` form so it works without needing to activate anything at all.

**Frontend.** Open a second terminal, keep the backend running:

```powershell
cd frontend
npm install
npm run dev      # http://localhost:5173
```

You'll land on an empty state until a simulation has run, which is what the next section is for.

## Demoing fail-closed live

An unrecognised reason code should never get guessed at. It should always route to a human, no matter what. Here's how to prove that against the real running API:

```powershell
cd backend
.venv\Scripts\python.exe -m demo.seed
```

This runs the reproducible 500-event comparison (seed 42), then posts one more event, signed with HMAC just like a real one, carrying a reason code that isn't in `config/decision_matrix.yaml`. Reload the dashboard and search for `evt_demo_unknown_reason_code` in the audit trail, or filter by Bucket = `B_UNKNOWN`:

| Field | Value | What it proves |
|---|---|---|
| `classified_bucket` | `B_UNKNOWN` | No bucket was guessed for a code the system doesn't recognise |
| `confidence` | `0.0` | Zero, not a hedge. It means "no information," which is different from "not confident enough" |
| `policy_verdict` | `ESCALATE` | The data-quality gate fires before any bucket-based rule is trusted |
| `action` | `HUMAN_QUEUE` | Routed to a person, never auto-retried |
| `scheduled_for` | `null` | No money-moving action was scheduled |

`demo/seed.py` checks this table itself and exits with an error if it's ever wrong, so it works as a live regression check, not just a demo script.

## Repo map

```
backend/classifier/    reason code -> bucket, confidence, signals
backend/policy/        governance: attempt caps, cooling-off, escalation
backend/executor/      NPCI-window snapping, scheduling, full-cycle simulation
backend/tests/         182 tests, one file per module above
frontend/              React + Vite + Recharts dashboard
docs/                  original planning docs + docs/ASSUMPTIONS.md
```
