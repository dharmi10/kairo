"""M7 demonstration: `python -m explain.demo`

Proves the four properties the explanation layer claims, against real
decisions produced by the real pipeline (no hand-built fixtures):

  1. What actually goes in the prompt -- printed verbatim, so the
     PII-free claim can be checked by reading it rather than believing it.
  2. The LLM path, end to end.
  3. The FALLBACK path, by simulating an API failure -- the demo's whole
     point: the network is the one dependency a live demo cannot control.
  4. The cache: N decisions, one API call per distinct
     (bucket, action, policy_verdict).
  5. The PII guard firing on a deliberately poisoned prompt context.

ON THE LLM PATH AND HONESTY. If ANTHROPIC_API_KEY is set, section 2 makes
a real API call and prints what the model actually returned. If it is not
set -- the default state of this repo, which ships no key -- section 2
runs against a STUB client whose text is hardcoded in this file. The stub
exercises the real code path (cache, response parsing, provenance flag)
but the SENTENCE IS NOT MODEL-GENERATED, and the output says so in
capital letters every time. Do not screenshot stub output and call it a
model sample.
"""

import logging
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.ingest import load_or_open_mandate_state, persist_event
from app.matrix import load_decision_matrix
from app.models import Decision
from classifier.classify import classify
from executor.executor import execute_decision
from explain.explain import (
    SYSTEM_PROMPT,
    Explainer,
    PIILeakError,
    build_explanation_prompt,
    cache_key,
    context_from_decision,
)
from generator.generate import generate_batch
from policy.policy import evaluate_policy

RULE = "=" * 78

STUB_SENTENCES = {
    ("B1_CONGESTION", "RETRY_SCHEDULED", "ALLOW"): (
        "This debit was turned away while the UPI network was throttling automated "
        "mandates, not because anything is wrong with the customer's account, so we "
        "have queued another attempt for a quieter part of the day."
    ),
    ("B5_DEAD", "NUDGE_SENT", "BLOCK"): (
        "The mandate behind this payment is no longer live, so retrying it would fail "
        "every time; instead we have asked the customer to re-authorise it."
    ),
}
STUB_DEFAULT = (
    "The failure was placed in this recovery category and the governance rules "
    "returned this verdict, so the system took the action recorded above."
)


class _StubBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _StubMessage:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_StubBlock(text)]


class _StubMessages:
    def create(self, *, model, max_tokens, system, messages, **kwargs):
        prompt = messages[0]["content"]
        for key, sentence in STUB_SENTENCES.items():
            if all(part in prompt for part in key):
                return _StubMessage(sentence)
        return _StubMessage(STUB_DEFAULT)


class StubClient:
    """Shaped like `anthropic.Anthropic` in the one way this code touches
    it. Used only when no API key is configured."""

    messages = _StubMessages()


class FailingClient:
    """Simulates the API being unreachable. Raises the same class the real
    SDK raises for a connection failure, so the fallback is exercised by
    the exact exception type production would see -- not a bare
    `Exception` that only this demo could ever produce."""

    class _Messages:
        def create(self, **kwargs):
            import anthropic

            raise anthropic.APIConnectionError(request=None)

    messages = _Messages()


def build_sample_decisions(db, matrix, count=60) -> list[Decision]:
    """Run a real batch through generate -> classify -> policy -> execute.
    These are committed rows: exactly what M7 is allowed to operate on."""
    decisions = []
    for event in generate_batch(count, 42, matrix):
        persist_event(db, event)
        state = load_or_open_mandate_state(db, event)
        classification = classify(event, matrix)
        history = {
            "cycle_started_at": event["failed_at"],
            "total_retry_attempts": 0,
            "total_contacts_sent": 0,
            "last_attempt_at": None,
            "prior_cycle_failed": False,
            "b1_congestion_failed_attempts": 0,
        }
        verdict, reasons = evaluate_policy(event, classification, history)
        decisions.append(
            execute_decision(db, event, classification, verdict, reasons, history, matrix, mandate_state=state)
        )
    return decisions


def section(number: int, title: str) -> None:
    print(f"\n{RULE}\n{number}. {title}\n{RULE}")


def main() -> None:
    # Every fallback logs a WARNING in normal operation (that is how an
    # operator notices the API is down). This demo deliberately triggers
    # hundreds of them, so the log is quietened here and the failures are
    # reported as counts instead -- the noise is real, it is just not what
    # this script is trying to show.
    logging.basicConfig(level=logging.ERROR)

    matrix = load_decision_matrix()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    decisions = build_sample_decisions(db, matrix)
    print(f"Built {len(decisions)} real committed decisions from a seed-42 batch.")

    # Pick one interesting decision to show in full.
    sample = next(
        (d for d in decisions if d.classified_bucket == "B1_CONGESTION" and d.action == "RETRY_SCHEDULED"),
        decisions[0],
    )
    context = context_from_decision(sample, matrix)

    # --- 1. what goes in the prompt ---------------------------------------
    section(1, "THE PROMPT -- read it and check the PII claim yourself")
    print(f"Decision under explanation: {sample.decision_id}")
    print(f"  event_id     {sample.event_id}   <- NOT in the prompt")
    print(f"  amount_inr   (in the audit record) <- NOT in the prompt")
    print(f"  scheduled_for {sample.scheduled_for}  <- NOT in the prompt (varies within the cache key)")
    print(f"\nSystem prompt and user prompt actually sent:\n")
    print("--- system ---")
    print(SYSTEM_PROMPT)
    print("\n--- user ---")
    print(build_explanation_prompt(context))
    print(f"\nCache key for this decision: {cache_key(context)}")

    # --- 2. the LLM path ---------------------------------------------------
    section(2, "THE LLM PATH")
    have_key = bool(settings.anthropic_api_key)
    if have_key:
        print("ANTHROPIC_API_KEY is set -- calling the real Anthropic API.")
        live = Explainer()
    else:
        print("*** NO ANTHROPIC_API_KEY CONFIGURED. ***")
        print("*** The sentence below is a HARDCODED STUB from explain/demo.py, not model output. ***")
        print("*** It exercises the real code path (cache, parsing, provenance flag) and nothing more. ***")
        print("*** For a genuine sample: ANTHROPIC_API_KEY=sk-... python -m explain.demo ***")
        live = Explainer(client=StubClient())

    text, source = live.generate(context)
    print(f"\nsource            : {source}")
    print(f"api_calls so far  : {live.api_calls}")
    print(f"explanation       : {text}")

    # --- 3. the fallback ---------------------------------------------------
    section(3, "SIMULATED API FAILURE -> TEMPLATE FALLBACK")
    print("Injecting a client that raises anthropic.APIConnectionError on every call.\n")
    broken = Explainer(client=FailingClient())
    text_fb, source_fb = broken.generate(context)
    print(f"source            : {source_fb}")
    print(f"api_calls         : {broken.api_calls}  (attempted)")
    print(f"api_failures      : {broken.api_failures}")
    print(f"explanation       : {text_fb}")

    print("\nSame failing client, across every decision in the batch:")
    counts = {"llm": 0, "template": 0}
    for decision in decisions:
        _, src = broken.generate(context_from_decision(decision, matrix))
        counts[src] += 1
    print(f"  decisions explained : {sum(counts.values())}")
    print(f"  from the API        : {counts['llm']}")
    print(f"  from templates      : {counts['template']}")
    empty = [d for d in decisions if not broken.generate(context_from_decision(d, matrix))[0]]
    print(f"  empty explanations  : {len(empty)}   <- PRD M7 acceptance: must be 0, with or without network")

    # --- 4. the cache ------------------------------------------------------
    section(4, "THE CACHE -- 'do not make 500 API calls'")
    counting = Explainer(client=StubClient())
    for decision in decisions:
        counting.generate(context_from_decision(decision, matrix))
    distinct = {cache_key(context_from_decision(d, matrix)) for d in decisions}
    print(f"decisions explained          : {len(decisions)}")
    print(f"distinct (bucket, action, verdict) : {len(distinct)}")
    print(f"API calls made               : {counting.api_calls}")
    print(f"calls saved by the cache     : {len(decisions) - counting.api_calls}")
    for key in sorted(distinct):
        print(f"    {key}")

    # --- 5. the PII guard --------------------------------------------------
    section(5, "FORBIDDEN_IN_PROMPT -- the guard firing")
    print("Layer 1 (field-name filter): a context carrying customer_id / payment_id")
    poisoned_fields = {**context, "customer_id": "cust_000123", "payment_id": "pay_000123"}
    rendered = build_explanation_prompt(poisoned_fields)
    print(f"  customer_id in rendered prompt? {'cust_000123' in rendered}")
    print(f"  payment_id  in rendered prompt? {'pay_000123' in rendered}")

    print("\nLayer 2 (value scan): a customer_id smuggled INSIDE an allowed field")
    print("  -- the case the field-name filter cannot catch --")
    smuggled = {**context, "bucket_label": "Congestion / timing for cust_000123"}
    try:
        build_explanation_prompt(smuggled, forbidden_values={"customer_id": "cust_000123"})
        print("  NOT CAUGHT -- this would be a bug")
        db.close()
        sys.exit(1)
    except PIILeakError as exc:
        print(f"  PIILeakError raised: {exc}")

    print(f"\n{RULE}")
    print("Every decision above is unchanged by any of this: the explanation layer")
    print("only ever wrote the `explanation` / `explanation_source` columns.")
    print(RULE)

    db.close()


if __name__ == "__main__":
    main()
