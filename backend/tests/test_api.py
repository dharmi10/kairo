"""The API surface (PRD sec. 8) -- all seven endpoints.

Weighted towards the two properties architecture-and-security.md calls
the most important in the system: the webhook cannot be forged, and a
duplicate delivery cannot cause a duplicate debit attempt.
"""

import json
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import Attempt, Decision, Event, MandateState
from app.security import sign_webhook_body

SIGNATURE_HEADER = "X-Razorpay-Signature"


def make_payload(**overrides) -> dict:
    payload = {
        "event_id": "evt_api_1",
        "payment_id": "pay_api_1",
        "mandate_id": "mandate_api_1",
        "customer_id": "cust_api_1",
        "merchant_category": "OTT",
        "amount_inr": 499,
        "failed_at": "2026-08-15T11:00:00",
        "attempt_number": 1,
        "error": {
            "code": "GATEWAY_ERROR",
            "source": "gateway",
            "step": "payment_authorization",
            "reason": "gateway_technical_error",
        },
        "customer_history": {
            "prior_failures_90d": 1,
            "prior_insufficient_funds_90d": 0,
            "typical_credit_day": 20,
            "mandate_age_days": 200,
        },
    }
    payload.update(overrides)
    return payload


def post_webhook(client, payload: dict, secret: str | None = None, signature: str | None = None):
    """Signs the EXACT bytes that are sent. Building the body once and
    signing that same object is the whole point -- a helper that
    re-serialised between signing and sending would silently test nothing
    (and is the mistake the raw-body rule exists to prevent)."""
    body = json.dumps(payload).encode()
    if signature is None:
        signature = sign_webhook_body(body, secret or settings.webhook_shared_secret)
    headers = {SIGNATURE_HEADER: signature, "Content-Type": "application/json"} if signature else {}
    return client.post("/webhook/payment-failed", content=body, headers=headers)


# --- webhook: authentication ----------------------------------------------


def test_valid_signature_is_accepted(client):
    response = post_webhook(client, make_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_wrong_signature_is_rejected(client):
    response = post_webhook(client, make_payload(), signature="0" * 64)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_signature"


def test_missing_signature_header_is_rejected(client):
    body = json.dumps(make_payload()).encode()
    response = client.post("/webhook/payment-failed", content=body)
    assert response.status_code == 401


def test_signature_from_the_wrong_secret_is_rejected(client):
    response = post_webhook(client, make_payload(), secret="not_the_shared_secret")
    assert response.status_code == 401


def test_signature_is_verified_against_the_raw_body_not_the_parsed_json(client):
    """The rule from architecture-and-security.md sec. 3.1. Two byte
    sequences that parse to the SAME object: a signature computed over one
    must not validate the other. If the endpoint re-serialised before
    verifying, this would pass with either signature and the test would be
    worthless."""
    payload = make_payload()
    compact = json.dumps(payload, separators=(",", ":")).encode()
    spaced = json.dumps(payload, indent=2).encode()
    assert json.loads(compact) == json.loads(spaced)
    assert compact != spaced

    signature_for_compact = sign_webhook_body(compact, settings.webhook_shared_secret)
    response = client.post(
        "/webhook/payment-failed",
        content=spaced,
        headers={SIGNATURE_HEADER: signature_for_compact},
    )
    assert response.status_code == 401


def test_unsigned_malformed_json_is_rejected_before_parsing(client):
    """Reject before parse: an unsigned payload never reaches the parser,
    so it gets 401 (not the 400 a signed-but-broken body would get)."""
    response = client.post("/webhook/payment-failed", content=b"{not json", headers={SIGNATURE_HEADER: "0" * 64})
    assert response.status_code == 401


def test_signed_malformed_json_gets_400(client):
    body = b"{not json"
    signature = sign_webhook_body(body, settings.webhook_shared_secret)
    response = client.post("/webhook/payment-failed", content=body, headers={SIGNATURE_HEADER: signature})
    assert response.status_code == 400
    assert response.json()["detail"] == "malformed_json"


def test_signed_but_schema_invalid_payload_gets_422(client):
    response = post_webhook(client, {"event_id": "evt_x"})
    assert response.status_code == 422


# --- webhook: idempotency --------------------------------------------------


def test_duplicate_event_id_returns_200_with_the_original_result(client):
    """Razorpay retries anything it thinks failed. A duplicate must be a
    200 carrying the original decision -- never an error, which would
    trigger a redelivery storm."""
    first = post_webhook(client, make_payload())
    assert first.json()["duplicate"] is False
    original = first.json()["decision"]

    second = post_webhook(client, make_payload())
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert [d["decision_id"] for d in second.json()["decisions"]] == [original["decision_id"]]


def test_duplicate_event_id_creates_no_second_attempt(client):
    """The property that actually matters: a double delivery must not
    cause a double debit attempt."""
    for _ in range(5):
        post_webhook(client, make_payload())

    db = SessionLocal()
    try:
        assert db.query(Event).count() == 1
        assert db.query(Decision).count() == 1
        assert db.query(Attempt).count() == 1
    finally:
        db.close()


def test_second_failure_on_the_same_mandate_counts_against_the_attempt_cap(client):
    """MandateState is durable, so the policy engine's caps hold ACROSS
    requests -- not just within one in-process simulation."""
    base = make_payload()
    for index in range(5):
        post_webhook(
            client,
            {
                **base,
                "event_id": f"evt_cap_{index}",
                "payment_id": f"pay_cap_{index}",
                "failed_at": (datetime(2026, 8, 15, 11, 0) + timedelta(days=index)).isoformat(),
            },
        )

    db = SessionLocal()
    try:
        state = db.get(MandateState, base["mandate_id"])
        assert state.total_retry_attempts <= settings.global_max_retry_attempts
        assert db.query(Attempt).count() <= settings.global_max_retry_attempts
        blocked = db.query(Decision).filter(Decision.policy_verdict == "BLOCK").all()
        assert any("attempt_cap_exceeded" in d.policy_reasons for d in blocked)
    finally:
        db.close()


# --- webhook: fail-closed --------------------------------------------------


def test_unknown_reason_code_routes_to_a_human_and_never_schedules_a_retry(client):
    """architecture-and-security.md sec. 5.1's demo moment: a reason code
    that isn't in the YAML must route to human review, not be guessed at."""
    payload = make_payload(event_id="evt_unknown", error={
        "code": "BAD_REQUEST_ERROR",
        "source": "customer",
        "step": "payment_authorization",
        "reason": "some_code_razorpay_invented_last_tuesday",
    })
    decision = post_webhook(client, payload).json()["decision"]

    assert decision["classified_bucket"] == "B_UNKNOWN"
    assert decision["policy_verdict"] == "ESCALATE"
    assert decision["action"] == "HUMAN_QUEUE"
    assert decision["scheduled_for"] is None


def test_hard_decline_is_never_auto_retried_via_the_api(client):
    payload = make_payload(event_id="evt_dead", error={
        "code": "BAD_REQUEST_ERROR",
        "source": "customer",
        "step": "payment_authorization",
        "reason": "card_expired",
    })
    decision = post_webhook(client, payload).json()["decision"]

    assert decision["classified_bucket"] == "B5_DEAD"
    assert decision["action"] != "RETRY_SCHEDULED"
    assert decision["scheduled_for"] is None


def test_scheduled_retry_never_lands_in_the_restricted_window(client):
    """Every hour of the day, through the real endpoint."""
    for hour in range(24):
        post_webhook(
            client,
            make_payload(
                event_id=f"evt_hour_{hour}",
                payment_id=f"pay_hour_{hour}",
                mandate_id=f"mandate_hour_{hour}",
                failed_at=f"2026-08-15T{hour:02d}:20:00",
            ),
        )

    db = SessionLocal()
    try:
        scheduled = [a.scheduled_for for a in db.query(Attempt).all()]
        assert scheduled
        assert not [
            t
            for t in scheduled
            if settings.npci_restricted_start_hour <= t.hour < settings.npci_restricted_end_hour
        ]
    finally:
        db.close()


# --- the explanation boundary ----------------------------------------------


def test_every_decision_gets_a_non_empty_explanation(client):
    decision = post_webhook(client, make_payload()).json()["decision"]
    assert decision["explanation"].strip()
    # No API key is configured in the test environment (see conftest), so
    # this is the fallback path -- which is exactly the state the demo
    # runs in.
    assert decision["explanation_source"] == "template"


def test_a_broken_explanation_layer_cannot_break_a_decision(client, monkeypatch):
    """The structural claim, tested at the boundary that matters: make the
    whole explanation layer explode, and the decision must still be
    committed, correct, and unchanged -- only its explanation is missing."""
    import app.api

    def exploding(*args, **kwargs):
        raise RuntimeError("the explanation layer is on fire")

    monkeypatch.setattr(app.api, "attach_explanations", exploding)

    response = post_webhook(client, make_payload(event_id="evt_boom"))
    assert response.status_code == 200

    decision = response.json()["decision"]
    assert decision["action"] == "RETRY_SCHEDULED"
    assert decision["classified_bucket"] == "B1_CONGESTION"
    assert decision["scheduled_for"] is not None

    db = SessionLocal()
    try:
        row = db.query(Decision).filter(Decision.event_id == "evt_boom").one()
        assert row.action == "RETRY_SCHEDULED"
        assert row.explanation is None  # the only casualty
        assert db.query(Attempt).filter(Attempt.decision_id == row.decision_id).count() == 1
    finally:
        db.close()


def test_decision_is_identical_with_and_without_the_explanation_layer(client, monkeypatch):
    """"Pull the LLM out entirely and the system's behaviour is
    byte-identical" (architecture-and-security.md sec. 9), checked."""
    import app.api

    with_explanations = post_webhook(client, make_payload(event_id="evt_a")).json()["decision"]

    monkeypatch.setattr(app.api, "attach_explanations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    without = post_webhook(client, make_payload(event_id="evt_b", payment_id="pay_b", mandate_id="mandate_b")).json()[
        "decision"
    ]

    compared = ("classified_bucket", "confidence", "signals", "policy_verdict", "policy_reasons", "action",
                "scheduled_for", "window_snapped", "outcome")
    assert {k: with_explanations[k] for k in compared} == {k: without[k] for k in compared}


# --- simulate / results / audit / reset ------------------------------------


@pytest.fixture
def run(client):
    """One small simulation run, shared by the read-side tests. 60 events
    rather than 500: these assert on shape and consistency, and the
    500-event numbers are already covered by metrics/report.py."""
    response = client.post("/simulate/run", json={"count": 60, "batch_seed": 42, "sim_seed": 20260903})
    assert response.status_code == 200
    return response.json()


def test_simulate_run_persists_events_decisions_and_attempts(run, client):
    db = SessionLocal()
    try:
        assert db.query(Event).count() == 60
        assert db.query(Decision).count() == run["decisions_written"]
        assert db.query(Decision).count() >= 60  # a cycle can take several decisions
        assert db.query(Attempt).count() == db.query(Decision).filter(
            Decision.action == "RETRY_SCHEDULED"
        ).count()
    finally:
        db.close()


def test_simulate_run_is_reproducible(client):
    first = client.post("/simulate/run", json={"count": 60, "batch_seed": 42, "sim_seed": 20260903}).json()
    second = client.post("/simulate/run", json={"count": 60, "batch_seed": 42, "sim_seed": 20260903}).json()
    assert first["agent"] == second["agent"]
    assert first["baseline"] == second["baseline"]


def test_simulate_run_explains_every_decision_without_exhausting_the_api(run, client):
    """PRD M7: cache by (bucket, action, policy_verdict), do not make one
    call per decision. With no key configured every explanation is a
    template -- the count that matters is that none are missing."""
    assert run["explanations"]["llm"] + run["explanations"]["template"] == run["decisions_written"]

    db = SessionLocal()
    try:
        assert db.query(Decision).filter(Decision.explanation.is_(None)).count() == 0
    finally:
        db.close()


def test_results_summary_reports_both_arms_and_the_delta(run, client):
    summary = client.get("/results/summary").json()
    assert summary["agent"]["n"] == 60
    assert summary["baseline"]["n"] == 60
    assert summary["delta"]["rupees_recovered"] == (
        summary["agent"]["rupees_recovered"] - summary["baseline"]["rupees_recovered"]
    )
    # The agent never retries a hard decline, so every one of baseline's
    # is an attempt avoided.
    assert summary["agent"]["attempts_on_hard_declines"] == 0
    assert summary["delta"]["wasted_attempts_avoided"] == summary["baseline"]["attempts_on_hard_declines"]


def test_results_endpoints_404_before_any_run(client):
    assert client.get("/results/summary").status_code == 404
    assert client.get("/results/by-bucket").status_code == 404


def test_results_by_bucket_covers_both_arms(run, client):
    payload = client.get("/results/by-bucket").json()
    assert payload["buckets"]
    for entry in payload["buckets"]:
        assert entry["bucket"].startswith("B")
        assert entry["agent"]["n"] == entry["baseline"]["n"]  # same events, both arms


def test_audit_paginates(run, client):
    total = client.get("/audit", params={"limit": 1}).json()["total"]
    assert total == run["decisions_written"]

    first_page = client.get("/audit", params={"limit": 10, "offset": 0}).json()["decisions"]
    second_page = client.get("/audit", params={"limit": 10, "offset": 10}).json()["decisions"]
    assert len(first_page) == 10
    assert {d["decision_id"] for d in first_page}.isdisjoint({d["decision_id"] for d in second_page})


def test_audit_filters(run, client):
    escalations = client.get("/audit", params={"policy_verdict": "ESCALATE", "limit": 500}).json()
    assert all(d["policy_verdict"] == "ESCALATE" for d in escalations["decisions"])

    dead = client.get("/audit", params={"bucket": "B5_DEAD", "limit": 500}).json()
    assert all(d["classified_bucket"] == "B5_DEAD" for d in dead["decisions"])
    assert all(d["action"] != "RETRY_SCHEDULED" for d in dead["decisions"])


def test_audit_detail_is_reconstructible(run, client):
    decision_id = client.get("/audit", params={"limit": 1}).json()["decisions"][0]["decision_id"]
    detail = client.get(f"/audit/{decision_id}").json()

    for key in ("decision_id", "event_id", "decided_at", "engine_version", "matrix_version",
                "input_snapshot", "classification", "policy", "action", "explanation", "outcome"):
        assert key in detail
    assert detail["input_snapshot"]["reason"]
    assert detail["classification"]["signals"]
    assert detail["policy"]["rules_evaluated"]


def test_audit_detail_404s_for_an_unknown_id(client):
    assert client.get("/audit/dec_does_not_exist").status_code == 404


def test_reset_clears_everything_and_regenerates_raw_events(run, client):
    response = client.post("/reset", json={"count": 25, "batch_seed": 7})
    assert response.json() == {"status": "reset", "events_generated": 25, "batch_seed": 7}

    db = SessionLocal()
    try:
        assert db.query(Event).count() == 25
        assert db.query(Decision).count() == 0  # reset regenerates the batch, it does not decide
        assert db.query(Attempt).count() == 0
    finally:
        db.close()
    assert client.get("/results/summary").status_code == 404
