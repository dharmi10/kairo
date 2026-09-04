"""Request/response shapes for the API surface (PRD sec. 8).

`FailureEventIn` is the FLAT `FailureEvent` shape defined in PRD sec. 6 --
the same dict the generator emits and every module downstream already
consumes. It is also the TARGET shape everything else gets mapped onto:
`app/razorpay_adapter.py` builds a `FailureEventIn` from Razorpay's real
nested webhook envelope (`{"event": ..., "payload": {"payment": {"entity":
{...}}}}`), and the webhook handler accepts either shape on the wire --
see app/api.py's `webhook_payment_failed` and DECISIONS.md, "Razorpay
envelope adapter".
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ErrorIn(BaseModel):
    code: str
    source: str
    step: str
    reason: str


class CustomerHistoryIn(BaseModel):
    prior_failures_90d: int = 0
    prior_insufficient_funds_90d: int = 0
    typical_credit_day: int = Field(ge=1, le=28)
    mandate_age_days: int = Field(ge=0)


class FailureEventIn(BaseModel):
    event_id: str
    payment_id: str
    mandate_id: str
    customer_id: str
    merchant_category: str
    amount_inr: int = Field(ge=0)
    failed_at: datetime
    error: ErrorIn
    attempt_number: int = 1
    customer_history: CustomerHistoryIn
    # A cycle is the 7-day recovery window for one failed debit. Razorpay
    # doesn't send one; if the caller omits it we open the mandate's first
    # cycle, matching what the generator emits.
    cycle_id: str | None = None

    def to_event_dict(self) -> dict:
        """The plain dict shape classify()/evaluate_policy()/resolve_action()
        all expect. Those are pure functions over dicts by design (M3-M5) --
        this is the one place Pydantic meets them."""
        return {
            "event_id": self.event_id,
            "payment_id": self.payment_id,
            "mandate_id": self.mandate_id,
            "customer_id": self.customer_id,
            "merchant_category": self.merchant_category,
            "amount_inr": self.amount_inr,
            "failed_at": self.failed_at.replace(tzinfo=None),
            "cycle_id": self.cycle_id or f"{self.mandate_id}_cycle1",
            "error": self.error.model_dump(),
            "attempt_number": self.attempt_number,
            "customer_history": self.customer_history.model_dump(),
        }


class SimulateRunIn(BaseModel):
    count: int = Field(default=500, ge=1, le=5000)
    batch_seed: int = 42
    sim_seed: int = 20260903


class ResetIn(BaseModel):
    count: int = Field(default=500, ge=1, le=5000)
    batch_seed: int = 42
