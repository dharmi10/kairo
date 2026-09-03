from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Event(Base):
    """Raw ingested failure event. Append-only. event_id PK is idempotency layer 1."""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    payment_id: Mapped[str] = mapped_column(String, index=True)
    mandate_id: Mapped[str] = mapped_column(String, index=True)
    customer_id: Mapped[str] = mapped_column(String)
    merchant_category: Mapped[str] = mapped_column(String)
    amount_inr: Mapped[int] = mapped_column(Integer)
    failed_at: Mapped[datetime] = mapped_column(DateTime)
    cycle_id: Mapped[str] = mapped_column(String, index=True)

    error_code: Mapped[str] = mapped_column(String)
    error_source: Mapped[str] = mapped_column(String)
    error_step: Mapped[str] = mapped_column(String)
    error_reason: Mapped[str] = mapped_column(String, index=True)

    attempt_number: Mapped[int] = mapped_column(Integer)  # Razorpay's own count, not ours

    prior_failures_90d: Mapped[int] = mapped_column(Integer, default=0)
    prior_insufficient_funds_90d: Mapped[int] = mapped_column(Integer, default=0)
    typical_credit_day: Mapped[int] = mapped_column(Integer)
    mandate_age_days: Mapped[int] = mapped_column(Integer)

    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    raw_payload: Mapped[str] = mapped_column(String)  # full JSON body, as received


class Decision(Base):
    """One per agent action. Append-only -- this IS the audit record."""

    __tablename__ = "decisions"

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, ForeignKey("events.event_id"), index=True)

    classified_bucket: Mapped[str] = mapped_column(String, index=True)
    confidence: Mapped[float] = mapped_column(Float)
    signals: Mapped[list] = mapped_column(JSON, default=list)

    policy_verdict: Mapped[str] = mapped_column(String)  # ALLOW | BLOCK | ESCALATE
    policy_reasons: Mapped[list] = mapped_column(JSON, default=list)

    action: Mapped[str] = mapped_column(String)  # RETRY_SCHEDULED | NUDGE_SENT | STOPPED | HUMAN_QUEUE
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_snapped: Mapped[bool] = mapped_column(Boolean, default=False)

    explanation: Mapped[str | None] = mapped_column(String, nullable=True)  # LLM-generated

    outcome: Mapped[str] = mapped_column(String, default="NOT_ATTEMPTED")
    amount_recovered_inr: Mapped[int] = mapped_column(Integer, default=0)

    engine_version: Mapped[str] = mapped_column(String)
    matrix_version: Mapped[str] = mapped_column(String)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Attempt(Base):
    """A scheduled retry attempt. Idempotency layer 2 -- the UNIQUE constraint
    makes a duplicate attempt structurally impossible even if upstream dedupe
    is bypassed."""

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("mandate_id", "cycle_id", "retry_attempt_number", name="uq_attempt_identity"),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    decision_id: Mapped[str] = mapped_column(String, ForeignKey("decisions.decision_id"), index=True)

    mandate_id: Mapped[str] = mapped_column(String, index=True)
    cycle_id: Mapped[str] = mapped_column(String, index=True)
    retry_attempt_number: Mapped[int] = mapped_column(Integer)  # our own counter, distinct from Event.attempt_number

    scheduled_for: Mapped[datetime] = mapped_column(DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)  # RECOVERED | FAILED | PENDING


class MandateState(Base):
    """Mutable, versioned aggregate per mandate -- what the policy engine
    checks attempt caps and contact limits against."""

    __tablename__ = "mandate_state"

    mandate_id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String)
    cycle_started_at: Mapped[datetime] = mapped_column(DateTime)

    total_retry_attempts: Mapped[int] = mapped_column(Integer, default=0)
    total_contacts_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String, default="active")  # active | stopped | escalated | recovered
    prior_cycle_failed: Mapped[bool] = mapped_column(Boolean, default=False)

    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
