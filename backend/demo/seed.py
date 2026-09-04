"""Demo seed script -- run as: python -m demo.seed

Produces the two things a demo needs, against a RUNNING backend
(`uvicorn app.main:app --reload` from `backend/` -- see the README's
"Running it" section):

  1. THE CANONICAL REPRODUCIBLE RUN. `POST /simulate/run` with no body --
     which means its own defaults (`app/schemas.py::SimulateRunIn`:
     count=500, batch_seed=42, sim_seed=20260903). This script doesn't
     invent a "demo batch" separate from what the API already defaults
     to; it just runs it and prints the headline numbers, so what you see
     on screen is exactly what `POST /simulate/run` with an empty body
     always produces -- reproducible by definition, not by a special path.

  2. ONE HAND-CRAFTED UNKNOWN-REASON-CODE EVENT, POSTed through the REAL
     webhook endpoint (HMAC-signed the same way Razorpay would sign it,
     using whatever WEBHOOK_SHARED_SECRET the running server is actually
     configured with) -- this is architecture-and-security.md sec. 5.1's
     own suggested demo moment: "Feed the system a reason code that isn't
     in the YAML and show it safely routing to human review instead of
     guessing. That's a 30-second demo moment that proves P3 concretely."
     POSTed AFTER the batch run, deliberately: /simulate/run's own
     `clear_all()` would otherwise wipe it, and this way the batch's
     stored metrics (what /results/summary reports) are computed and
     persisted before this one extra event exists, so injecting it never
     perturbs "the numbers" -- it only ever adds one extra, clearly
     visible row to the audit trail.

Verifies the unknown-code event actually behaves per spec (B_UNKNOWN /
ESCALATE / HUMAN_QUEUE / no schedule) before declaring success -- a demo
script that silently seeds the wrong thing is worse than one that fails
loudly.
"""

import argparse
import json
import sys
from datetime import datetime

import httpx

from app.config import settings
from app.security import sign_webhook_body

DEFAULT_API_BASE = "http://127.0.0.1:8000"

# Deliberately NOT in config/decision_matrix.yaml (checked against every
# key in `reason_codes` as of the matrix's 2026-09-03 version -- see
# DECISIONS.md if that ever needs re-verifying after a matrix edit).
# Named to read as a plausible NEXT code Razorpay/NPCI could ship, tying
# back to the PRD's own framing of NPCI's Traffic Management framework
# (PRD sec. 1) -- not a randomly-typo'd string, which would make the
# "this could really happen" argument weaker.
UNKNOWN_REASON_CODE = "npci_traffic_shaping_declined"

DEMO_EVENT_ID = "evt_demo_unknown_reason_code"


def _unknown_reason_payload() -> dict:
    """A same-shaped FailureEvent (PRD sec. 6 flat shape) carrying a
    reason code the matrix has never seen. Fixed event_id (not a fresh
    one per run) so re-running this script twice hits the documented
    idempotency path harmlessly instead of accumulating demo litter, and
    so a presenter can memorise one id to type into the audit search box.

    `failed_at` is a FIXED timestamp, not `datetime.now()` -- two reasons.
    First, reproducibility: this script's whole point is a demo run that
    looks identical every time it's seeded. Second, correctness: every
    naive datetime elsewhere in this codebase (the generator, executor
    window-snapping, app/razorpay_adapter.py's IST conversion) is
    implicitly IST wall-clock, and `datetime.now()`/`utcnow()` would be
    neither, consistently. Doesn't affect THIS event's outcome (B_UNKNOWN
    routes on the reason code alone, never on timing), but a fixed IST
    timestamp costs nothing and keeps the whole script consistent with
    that convention rather than being the one exception."""
    failed_at = datetime(2026, 8, 20, 11, 0, 0)
    return {
        "event_id": DEMO_EVENT_ID,
        "payment_id": "pay_demo_unknown_reason",
        "mandate_id": "mandate_demo_unknown_reason",
        "customer_id": "cust_demo_unknown_reason",
        "merchant_category": "OTT",
        "amount_inr": 499,
        "failed_at": failed_at.isoformat(),
        "attempt_number": 1,
        "error": {
            "code": "GATEWAY_ERROR",
            "source": "gateway",
            "step": "payment_authorization",
            "reason": UNKNOWN_REASON_CODE,
        },
        "customer_history": {
            "prior_failures_90d": 0,
            "prior_insufficient_funds_90d": 0,
            "typical_credit_day": 15,
            "mandate_age_days": 90,
        },
    }


def _post_webhook(client: httpx.Client, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    signature = sign_webhook_body(body, settings.webhook_shared_secret)
    return client.post(
        "/webhook/payment-failed",
        content=body,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
    )


def _check_backend_up(client: httpx.Client) -> None:
    try:
        response = client.get("/health", timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Backend not reachable at {client.base_url} ({exc}).", file=sys.stderr)
        print("Start it first:  cd backend && uvicorn app.main:app --reload", file=sys.stderr)
        sys.exit(1)


def _print_run_summary(run: dict) -> None:
    agent, baseline = run["agent"], run["baseline"]
    print(f"\n1. Canonical run -- batch_seed={run['batch_seed']}, sim_seed={run['sim_seed']}, n={agent['n']}")
    print("   " + "-" * 70)
    print(f"   Recovery rate      agent {agent['recovery_rate_pct']:.1f}%   vs baseline {baseline['recovery_rate_pct']:.1f}%"
          f"   ({run['rate_delta_points']:+.1f} pts)")
    print(f"   Rs recovered       agent Rs.{agent['rupees_recovered']:,}   vs baseline Rs.{baseline['rupees_recovered']:,}"
          f"   ({run['rs_uplift_pct']:+.1f}%)")
    print(f"   Wasted attempts avoided (baseline retried hard declines, agent never does): "
          f"{baseline['attempts_on_hard_declines'] - agent['attempts_on_hard_declines']}")
    print(f"   Decisions written: {run['decisions_written']}   "
          f"(explanations: {run['explanations']['llm']} llm / {run['explanations']['template']} template, "
          f"{run['explanations']['api_calls']} API calls)")


def _print_unknown_code_result(decision: dict) -> None:
    print(f"\n2. Unknown reason code -- '{UNKNOWN_REASON_CODE}' (not in config/decision_matrix.yaml)")
    print("   " + "-" * 70)
    print(f"   classified_bucket : {decision['classified_bucket']}")
    print(f"   confidence        : {decision['confidence']}")
    print(f"   signals           : {decision['signals']}")
    print(f"   policy_verdict    : {decision['policy_verdict']}")
    print(f"   action            : {decision['action']}")
    print(f"   scheduled_for     : {decision['scheduled_for']}")
    print(f"   explanation       : {decision['explanation']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"default: {DEFAULT_API_BASE}")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api_base, timeout=60) as client:
        _check_backend_up(client)

        print(f"Seeding demo against {args.api_base} ...")

        run_response = client.post("/simulate/run")
        run_response.raise_for_status()
        run = run_response.json()
        _print_run_summary(run)

        webhook_response = _post_webhook(client, _unknown_reason_payload())
        if webhook_response.status_code != 200:
            print(f"\nUnknown-reason-code webhook FAILED: {webhook_response.status_code} {webhook_response.text}",
                  file=sys.stderr)
            sys.exit(1)
        body = webhook_response.json()
        decision = body["decisions"][0] if body["duplicate"] else body["decision"]
        _print_unknown_code_result(decision)

        # Fail loudly, not just print-and-hope: a demo script whose own
        # "proof" event doesn't actually prove fail-closed behaviour is
        # worse than one that errors out and tells you why.
        expected = {
            "classified_bucket": "B_UNKNOWN",
            "policy_verdict": "ESCALATE",
            "action": "HUMAN_QUEUE",
        }
        mismatches = {k: (v, decision[k]) for k, v in expected.items() if decision[k] != v}
        if mismatches or decision["scheduled_for"] is not None:
            print(f"\nUNEXPECTED: fail-closed invariant violated: {mismatches}", file=sys.stderr)
            sys.exit(1)

        print(f"\n{'=' * 74}")
        print("Demo seeded. Two things to show live:")
        print(f"  - The dashboard: run/reload it, headline + chart + governance panel + audit trail")
        print(f"    all reflect the run above (npm run dev in frontend/, see README).")
        print(f"  - Fail-closed on an unknown code: search '{DEMO_EVENT_ID}' in the audit trail search box,")
        print(f"    or:  curl {args.api_base}/audit/{decision['decision_id']}")
        print(f"{'=' * 74}")


if __name__ == "__main__":
    main()
