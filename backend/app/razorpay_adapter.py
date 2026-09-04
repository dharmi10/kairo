"""Adapter: Razorpay's REAL webhook envelope -> the same `FailureEventIn`
every other path already builds (see app/schemas.py).

Verified against Razorpay's own docs (2026-09-04, via WebFetch):
  - Payment entity fields (id, amount in paise, vpa, email, contact,
    notes, error_code/error_description/error_source/error_step/
    error_reason, created_at): razorpay.com/docs/api/payments/entity/
  - HMAC signature header `X-Razorpay-Signature` (unchanged from what
    app/security.py already implements) and the REAL idempotency
    mechanism -- an `x-razorpay-event-id` header, "unique per event" --
    not a body field: razorpay.com/docs/webhooks/validate-test/

The envelope shape (`entity`/`account_id`/`event`/`contains`/`payload`/
`created_at` at the top level, `payload.payment.entity` nested inside)
matches Razorpay's documented pattern; the full literal sample JSON
could not be pulled from a live doc page in this pass (404s on the two
specific webhook-payload URLs tried), so treat the WRAPPER field names as
VERIFIED-by-convention rather than VERIFIED-by-example, same provenance
standard config/decision_matrix.yaml already uses. The payment entity
fields nested inside it ARE confirmed against a live example.

WHAT RAZORPAY'S SCHEMA HAS NO ROOM FOR, AND THIS SYSTEM NEEDS ANYWAY:
`mandate_id`, `customer_id`, `merchant_category`, `cycle_id`, and our own
`customer_history` features are not Razorpay payment fields -- there is
no native "which UPI AutoPay mandate is this" reference on a payment
entity Razorpay documents publicly (the honesty requirement in PRD
sec. 11.2 already flags mandate-lifecycle reason codes as unconfirmed;
this is the same gap surfacing on the payment entity, not a new one).
Real merchants close exactly this kind of gap with the payment entity's
own `notes` field, which Razorpay explicitly reserves for merchant-
supplied key/value metadata -- so that's what this adapter reads from.

`notes` values are plain strings in Razorpay's real schema, not nested
objects (a documented ~256-char-per-value limit on a flat key/value
bag). `customer_history` is therefore accepted as a JSON-encoded STRING
under `notes.customer_history` -- the realistic way a merchant would
smuggle structured data through a flat-string notes field, not an
invented relaxation of Razorpay's actual constraint.

DATA MINIMISATION (P5, architecture-and-security.md sec. 3.3): a real
payment entity carries `vpa`, `email`, `contact` -- none of which the
flat `FailureEventIn` contract has ever had a field for, because
synthetic events never carried them. `customer_id` is taken from
`notes.customer_id` when the merchant supplies one (the realistic case:
merchants already keep their own customer reference), and otherwise
derived as a SHA-256 hash of vpa/email/contact -- never the plaintext
value itself, matching the architecture doc's "Full VPA: Hashed" /
"phone/email: Tokenised reference" rules exactly. `scrub_pii_for_storage`
additionally redacts those three fields out of the copy persisted as
`Event.raw_payload` -- applied AFTER HMAC verification (which runs
against Razorpay's own untouched bytes; scrubbing never touches what was
actually signed) and BEFORE anything is written to disk.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.schemas import CustomerHistoryIn, ErrorIn, FailureEventIn

PII_FIELDS_TO_SCRUB = ("vpa", "email", "contact")

# Every timestamp elsewhere in this codebase (the generator, the
# executor's window-snapping, app/config.py's npci_restricted_*_hour
# settings) is a NAIVE datetime that is implicitly IST wall-clock -- see
# app/dates.py and executor/executor.py's snap_to_safe_window. A real
# Razorpay `created_at` is Unix epoch seconds (UTC), so it must be
# converted to IST and stripped of tzinfo before it enters the rest of
# the system -- converting to naive UTC instead would silently shift
# every hour-of-day check (NPCI window snapping, the congestion
# override) by 5.5 hours. Found by test_flat_and_razorpay_shapes_reach_
# the_same_decision_logic disagreeing on bucket for what should have
# been the identical wall-clock failure time.
_IST = timezone(timedelta(hours=5, minutes=30))


class RazorpayEnvelopeError(ValueError):
    """A structurally Razorpay-shaped payload that is still missing
    something this system needs (an idempotency key, a mandate
    reference, customer history). Raised, not asserted -- the webhook
    handler turns this into a 422, the same fail-closed response an
    invalid flat payload already gets."""


def looks_like_razorpay_envelope(payload: dict) -> bool:
    """Cheap shape sniff, checked before the flat-schema path is tried.
    Real Razorpay envelopes always have `event` and a
    `payload.payment.entity`; the flat `FailureEventIn` shape has neither
    key at the top level. Not a security boundary -- HMAC verification
    already ran against the raw bytes before this is ever called."""
    return (
        isinstance(payload, dict)
        and "event" in payload
        and isinstance(payload.get("payload"), dict)
        and isinstance(payload["payload"].get("payment"), dict)
        and isinstance(payload["payload"]["payment"].get("entity"), dict)
    )


def _hashed_customer_ref(entity: dict) -> str:
    """Fallback when the merchant's own notes carry no customer_id.
    SHA-256 over whichever real identifier is present -- never the
    plaintext itself, per P5. Falls back to the payment id (not PII) only
    if Razorpay sent none of vpa/email/contact, which correlates nothing
    across payments but still yields a valid, if unlinkable, reference."""
    seed = entity.get("vpa") or entity.get("email") or entity.get("contact") or entity["id"]
    return "cust_" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def from_razorpay_envelope(payload: dict, header_event_id: str | None) -> FailureEventIn:
    """Build a `FailureEventIn` from a real Razorpay `payment.failed`
    webhook body. Raises `RazorpayEnvelopeError` (-> 422) for anything
    Razorpay's own schema doesn't and can't carry."""
    entity = payload["payload"]["payment"]["entity"]
    notes = entity.get("notes") or {}

    # Razorpay's real idempotency key is a HEADER (`x-razorpay-event-id`),
    # not a body field -- there is no `event_id` anywhere in the payment
    # entity or the envelope itself. `notes.event_id` is accepted only as
    # a testing affordance for callers exercising this path without a
    # full webhook simulator that sets headers.
    event_id = header_event_id or notes.get("event_id")
    if not event_id:
        raise RazorpayEnvelopeError(
            "missing event id: set the X-Razorpay-Event-Id header "
            "(Razorpay always sends this), or notes.event_id for local testing"
        )

    mandate_id = notes.get("mandate_id")
    if not mandate_id:
        raise RazorpayEnvelopeError(
            "missing notes.mandate_id -- Razorpay's payment entity has no native mandate reference, "
            "so the merchant must supply one via notes (see module docstring)"
        )

    raw_history = notes.get("customer_history")
    if not raw_history:
        raise RazorpayEnvelopeError("missing notes.customer_history (a JSON-encoded string) -- required for scheduling")
    try:
        history_dict = json.loads(raw_history) if isinstance(raw_history, str) else raw_history
        customer_history = CustomerHistoryIn(**history_dict)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RazorpayEnvelopeError(f"notes.customer_history is not valid JSON: {exc}") from exc

    return FailureEventIn(
        event_id=str(event_id),
        payment_id=entity["id"],
        mandate_id=mandate_id,
        customer_id=notes.get("customer_id") or _hashed_customer_ref(entity),
        merchant_category=notes.get("merchant_category", "UNKNOWN"),
        # Razorpay amounts are integer paise; FailureEvent.amount_inr is
        # whole rupees (PRD sec. 6). Rounds to the nearest rupee -- the
        # same granularity the synthetic generator already produces, not
        # a new simplification introduced here.
        amount_inr=round(entity["amount"] / 100),
        # No separate "failed_at" field exists on a payment entity -- the
        # entity's own `created_at` (when Razorpay recorded this attempt)
        # IS the failure record for a failed payment. ASSUMPTION, stated
        # once here rather than left implicit. Converted to IST and made
        # naive -- see the _IST comment above.
        failed_at=datetime.fromtimestamp(entity["created_at"], tz=_IST).replace(tzinfo=None),
        error=ErrorIn(
            code=entity.get("error_code") or "UNKNOWN_ERROR",
            source=entity.get("error_source") or "gateway",
            step=entity.get("error_step") or "payment_authorization",
            reason=entity.get("error_reason") or "unknown",
        ),
        attempt_number=int(notes.get("attempt_number", 1)),
        customer_history=customer_history,
        cycle_id=notes.get("cycle_id"),
    )


def scrub_pii_for_storage(raw_body: bytes) -> bytes:
    """Redact vpa/email/contact from the copy of the body persisted as
    `Event.raw_payload`. Called ONLY after HMAC verification has already
    run against the true, untouched `raw_body` -- this never touches what
    Razorpay actually signed, only our own durable copy of it (P5).

    Malformed JSON is not this function's problem -- the webhook handler
    already required valid JSON (and a recognisable envelope shape)
    before calling it."""
    payload = json.loads(raw_body)
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    for field in PII_FIELDS_TO_SCRUB:
        if entity.get(field):
            entity[field] = "[redacted]"
    return json.dumps(payload).encode("utf-8")
