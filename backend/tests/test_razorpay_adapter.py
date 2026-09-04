"""app/razorpay_adapter.py -- mapping Razorpay's real webhook envelope
onto the internal FailureEventIn contract, and the PII scrub applied to
what gets persisted."""

import json

import pytest

from app.razorpay_adapter import (
    RazorpayEnvelopeError,
    from_razorpay_envelope,
    looks_like_razorpay_envelope,
    scrub_pii_for_storage,
)


def make_envelope(**entity_overrides) -> dict:
    entity = {
        "id": "pay_L0nSsccovt6zyp",
        "entity": "payment",
        "amount": 49900,  # paise
        "currency": "INR",
        "status": "failed",
        "method": "upi",
        "vpa": "gauravkumar@exampleupi",
        "email": "gaurav.kumar@example.com",
        "contact": "+919000090000",
        "notes": {
            "mandate_id": "mandate_rzp_1",
            "customer_id": "cust_rzp_1",
            "merchant_category": "OTT",
            "customer_history": json.dumps(
                {
                    "prior_failures_90d": 1,
                    "prior_insufficient_funds_90d": 0,
                    "typical_credit_day": 20,
                    "mandate_age_days": 200,
                }
            ),
        },
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Insufficient balance in the customer's account.",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "error_reason": "insufficient_funds",
        "created_at": 1755250800,  # arbitrary epoch -- these tests assert on mapping, not on bucket/timing
    }
    entity.update(entity_overrides)
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": 1755250800,
    }


# --- shape detection ---------------------------------------------------


def test_recognises_a_real_razorpay_envelope():
    assert looks_like_razorpay_envelope(make_envelope())


def test_does_not_recognise_the_flat_shape():
    flat = {
        "event_id": "evt_1",
        "payment_id": "pay_1",
        "mandate_id": "mandate_1",
        "customer_id": "cust_1",
        "merchant_category": "OTT",
        "amount_inr": 499,
        "failed_at": "2026-08-15T11:00:00",
        "error": {"code": "X", "source": "gateway", "step": "payment_authorization", "reason": "gateway_technical_error"},
        "customer_history": {"typical_credit_day": 20, "mandate_age_days": 200},
    }
    assert not looks_like_razorpay_envelope(flat)


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"event": "payment.failed"},
        {"event": "payment.failed", "payload": {}},
        {"event": "payment.failed", "payload": {"payment": {}}},
        {"event": "payment.failed", "payload": {"payment": "not-a-dict"}},
        "not-even-a-dict",
        None,
    ],
)
def test_rejects_malformed_or_partial_envelopes(broken):
    assert not looks_like_razorpay_envelope(broken)


# --- field mapping -------------------------------------------------------


def test_maps_every_field_correctly():
    parsed = from_razorpay_envelope(make_envelope(), header_event_id="evt_from_header")

    assert parsed.event_id == "evt_from_header"
    assert parsed.payment_id == "pay_L0nSsccovt6zyp"
    assert parsed.mandate_id == "mandate_rzp_1"
    assert parsed.customer_id == "cust_rzp_1"
    assert parsed.merchant_category == "OTT"
    assert parsed.amount_inr == 499  # 49900 paise -> 499 rupees
    assert parsed.error.code == "BAD_REQUEST_ERROR"
    assert parsed.error.source == "customer"
    assert parsed.error.step == "payment_authorization"
    assert parsed.error.reason == "insufficient_funds"
    assert parsed.customer_history.typical_credit_day == 20
    assert parsed.customer_history.mandate_age_days == 200


def test_event_id_comes_from_the_header_not_the_body():
    """The real Razorpay idempotency key is a header. notes.event_id is a
    testing-only fallback and must not shadow it."""
    envelope = make_envelope()
    envelope["payload"]["payment"]["entity"]["notes"]["event_id"] = "evt_from_notes"
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_from_header")
    assert parsed.event_id == "evt_from_header"


def test_falls_back_to_notes_event_id_when_no_header_is_present():
    envelope = make_envelope()
    envelope["payload"]["payment"]["entity"]["notes"]["event_id"] = "evt_from_notes"
    parsed = from_razorpay_envelope(envelope, header_event_id=None)
    assert parsed.event_id == "evt_from_notes"


def test_missing_event_id_anywhere_is_rejected():
    with pytest.raises(RazorpayEnvelopeError, match="event id"):
        from_razorpay_envelope(make_envelope(), header_event_id=None)


def test_missing_mandate_id_is_rejected():
    envelope = make_envelope()
    del envelope["payload"]["payment"]["entity"]["notes"]["mandate_id"]
    with pytest.raises(RazorpayEnvelopeError, match="mandate_id"):
        from_razorpay_envelope(envelope, header_event_id="evt_1")


def test_missing_customer_history_is_rejected():
    envelope = make_envelope()
    del envelope["payload"]["payment"]["entity"]["notes"]["customer_history"]
    with pytest.raises(RazorpayEnvelopeError, match="customer_history"):
        from_razorpay_envelope(envelope, header_event_id="evt_1")


def test_malformed_customer_history_json_is_rejected():
    envelope = make_envelope(notes={
        "mandate_id": "mandate_1",
        "customer_history": "{not valid json",
    })
    with pytest.raises(RazorpayEnvelopeError, match="not valid JSON"):
        from_razorpay_envelope(envelope, header_event_id="evt_1")


def test_missing_customer_id_falls_back_to_a_hash_never_the_plaintext():
    """P5: never store vpa/email/contact plaintext, even as a derived id."""
    envelope = make_envelope()
    del envelope["payload"]["payment"]["entity"]["notes"]["customer_id"]
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_1")

    assert parsed.customer_id.startswith("cust_")
    assert "gauravkumar" not in parsed.customer_id
    assert "example" not in parsed.customer_id


def test_hash_fallback_is_deterministic_for_the_same_vpa():
    envelope = make_envelope()
    del envelope["payload"]["payment"]["entity"]["notes"]["customer_id"]
    first = from_razorpay_envelope(envelope, header_event_id="evt_1")
    second = from_razorpay_envelope(envelope, header_event_id="evt_2")
    assert first.customer_id == second.customer_id


def test_missing_merchant_category_defaults_rather_than_rejecting():
    """Not load-bearing for any decision -- classify()/evaluate_policy()
    never read it -- so a merchant who doesn't tag this note shouldn't get
    a 422 for it."""
    envelope = make_envelope()
    del envelope["payload"]["payment"]["entity"]["notes"]["merchant_category"]
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_1")
    assert parsed.merchant_category == "UNKNOWN"


def test_missing_error_fields_fail_closed_to_unknown_not_a_guess():
    envelope = make_envelope(error_code=None, error_source=None, error_step=None, error_reason=None)
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_1")
    assert parsed.error.reason == "unknown"  # classify() routes this to B_UNKNOWN -- never guessed


def test_created_at_is_interpreted_as_ist_not_utc():
    """Every naive datetime elsewhere in this codebase (the generator,
    executor/executor.py's window snapping, app/config.py's restricted-
    hour settings) is implicitly IST wall-clock. A UTC-naive conversion
    of Razorpay's epoch `created_at` would silently shift every hour-of-
    day decision (NPCI window snapping, the congestion override) by 5.5
    hours relative to everything else in the system."""
    envelope = make_envelope(created_at=1786771800)  # 2026-08-15T11:00:00 IST
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_1")
    assert parsed.failed_at.isoformat() == "2026-08-15T11:00:00"


def test_cycle_id_defaults_the_same_way_the_flat_path_does():
    parsed = from_razorpay_envelope(make_envelope(), header_event_id="evt_1")
    assert parsed.cycle_id is None  # to_event_dict() fills in f"{mandate_id}_cycle1"
    assert parsed.to_event_dict()["cycle_id"] == "mandate_rzp_1_cycle1"


def test_explicit_cycle_id_in_notes_is_respected():
    envelope = make_envelope()
    envelope["payload"]["payment"]["entity"]["notes"]["cycle_id"] = "mandate_rzp_1_cycle2"
    parsed = from_razorpay_envelope(envelope, header_event_id="evt_1")
    assert parsed.to_event_dict()["cycle_id"] == "mandate_rzp_1_cycle2"


# --- PII scrub on the persisted copy -------------------------------------


def test_scrub_redacts_vpa_email_contact():
    envelope = make_envelope()
    body = json.dumps(envelope).encode()

    scrubbed = json.loads(scrub_pii_for_storage(body))
    entity = scrubbed["payload"]["payment"]["entity"]

    assert entity["vpa"] == "[redacted]"
    assert entity["email"] == "[redacted]"
    assert entity["contact"] == "[redacted]"


def test_scrub_leaves_everything_else_untouched():
    envelope = make_envelope()
    body = json.dumps(envelope).encode()

    scrubbed = json.loads(scrub_pii_for_storage(body))
    entity = scrubbed["payload"]["payment"]["entity"]

    assert entity["id"] == "pay_L0nSsccovt6zyp"
    assert entity["amount"] == 49900
    assert entity["error_reason"] == "insufficient_funds"
    assert entity["notes"]["mandate_id"] == "mandate_rzp_1"


def test_scrub_never_runs_on_the_bytes_that_were_actually_signed():
    """Documents the ordering constraint rather than testing scrub_pii_
    for_storage in isolation: the function receives raw_body and returns
    a DIFFERENT bytes object -- callers must verify the signature against
    the original before ever calling this. Covered end-to-end in
    tests/test_api.py's Razorpay-envelope webhook tests."""
    envelope = make_envelope()
    original = json.dumps(envelope).encode()
    redacted = scrub_pii_for_storage(original)
    assert redacted != original
