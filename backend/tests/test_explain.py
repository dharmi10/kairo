"""M7 -- the explanation layer.

The tests that matter here are not "does it produce a string" but the
three properties the layer is built around: it cannot block a decision,
it cannot leak PII, and it cannot make 500 API calls.
"""

from datetime import datetime

import pytest

from app.matrix import load_decision_matrix
from app.models import Decision
from explain.explain import (
    ALSO_EXCLUDED_FROM_PROMPT,
    FORBIDDEN_IN_PROMPT,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    Explainer,
    PIILeakError,
    build_explanation_prompt,
    cache_key,
    context_from_decision,
)
from explain.templates import render_template


# --- doubles ---------------------------------------------------------------


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Message:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_Block(text)]


class RecordingClient:
    """Returns a deterministic sentence and records every prompt it was
    sent -- which is how the PII tests inspect what actually crossed the
    boundary, rather than trusting the builder."""

    def __init__(self, text="A generated explanation."):
        self.text = text
        self.prompts: list[str] = []
        outer = self

        class _Messages:
            def create(self, *, model, max_tokens, system, messages, **kwargs):
                outer.prompts.append(messages[0]["content"])
                outer.system = system
                return _Message(outer.text)

        self.messages = _Messages()


class FailingClient:
    def __init__(self, exc=None):
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls += 1
                raise exc or ConnectionError("network down")

        self.messages = _Messages()


class EmptyResponseClient:
    """The subtler failure: a 200 that contains no usable text (e.g. the
    whole token budget went to thinking). Must be treated as a failure,
    not written into the audit record as an empty explanation."""

    class _Messages:
        def create(self, **kwargs):
            return _Message("   ")

    messages = _Messages()


def make_decision(bucket="B1_CONGESTION", action="RETRY_SCHEDULED", verdict="ALLOW", **overrides):
    fields = {
        "decision_id": "dec_test",
        "event_id": "evt_test",
        "classified_bucket": bucket,
        "confidence": 0.82,
        "signals": ["fired_in_restricted_window"],
        "policy_verdict": verdict,
        "policy_reasons": ["classification_confident"],
        "action": action,
        "scheduled_for": datetime(2026, 8, 15, 13, 30),
        "window_snapped": True,
        "outcome": "PENDING",
        "amount_recovered_inr": 0,
        "engine_version": "0.1.0",
        "matrix_version": "test",
    }
    fields.update(overrides)
    return Decision(**fields)


@pytest.fixture
def matrix():
    return load_decision_matrix()


# --- PII -------------------------------------------------------------------


def test_no_forbidden_field_reaches_the_prompt(matrix):
    """Layer 1: the architecture doc's field-name filter."""
    context = context_from_decision(make_decision(), matrix)
    poisoned = {
        **context,
        "customer_id": "cust_000042",
        "payment_id": "pay_000042",
        "vpa": "someone@bank",
        "phone": "9876543210",
        "email": "someone@example.com",
    }
    prompt = build_explanation_prompt(poisoned)
    for value in ("cust_000042", "pay_000042", "someone@bank", "9876543210", "someone@example.com"):
        assert value not in prompt


def test_amount_and_mandate_id_also_stay_out_of_the_prompt(matrix):
    """Not in the doc's set, excluded on the same data-minimisation
    principle -- see ALSO_EXCLUDED_FROM_PROMPT."""
    context = context_from_decision(make_decision(), matrix)
    prompt = build_explanation_prompt({**context, "amount_inr": 4999, "mandate_id": "mandate_000042"})
    assert "4999" not in prompt
    assert "mandate_000042" not in prompt


def test_value_level_scan_catches_pii_smuggled_inside_an_allowed_field(matrix):
    """Layer 2, and the reason layer 1 alone is not enough: a customer id
    that arrives INSIDE a permitted field passes every field-name check."""
    context = context_from_decision(make_decision(), matrix)
    smuggled = {**context, "bucket_label": "Congestion / timing for cust_000042"}

    with pytest.raises(PIILeakError):
        build_explanation_prompt(smuggled, forbidden_values={"customer_id": "cust_000042"})


def test_pii_leak_is_not_swallowed_by_the_fallback(matrix):
    """A leak is our bug, not a dependency being down. It must NOT quietly
    degrade to a template -- that would hide the exact thing the check
    exists to surface."""
    explainer = Explainer(client=RecordingClient())
    context = context_from_decision(make_decision(), matrix)
    smuggled = {**context, "bucket_label": "Congestion for cust_000042"}

    with pytest.raises(PIILeakError):
        explainer.generate(smuggled, forbidden_values={"customer_id": "cust_000042"})


def test_prompt_carries_nothing_beyond_the_cache_key(matrix):
    """The correctness property behind the narrowed prompt: two decisions
    sharing a cache key must produce a BYTE-IDENTICAL prompt. If they
    didn't, the cached sentence would state something false about one of
    them -- fiction in an append-only audit record."""
    same_key_different_everything_else = (
        context_from_decision(
            make_decision(
                scheduled_for=datetime(2026, 8, 15, 13, 30),
                confidence=0.82,
                signals=["fired_in_restricted_window"],
                policy_reasons=["classification_confident"],
            ),
            matrix,
        ),
        context_from_decision(
            make_decision(
                scheduled_for=datetime(2027, 1, 2, 21, 5),
                confidence=0.55,
                signals=["something_else_entirely"],
                policy_reasons=["within_attempt_cap", "cooling_off_satisfied"],
            ),
            matrix,
        ),
    )
    first, second = same_key_different_everything_else
    assert cache_key(first) == cache_key(second)
    assert build_explanation_prompt(first) == build_explanation_prompt(second)


def test_forbidden_sets_are_disjoint():
    """A field in both sets would be excluded twice and removed from one
    list without effect -- a trap for whoever edits these next."""
    assert not (FORBIDDEN_IN_PROMPT & ALSO_EXCLUDED_FROM_PROMPT)


# --- caching ---------------------------------------------------------------


def test_one_api_call_per_distinct_cache_key(matrix):
    client = RecordingClient()
    explainer = Explainer(client=client)

    decisions = [
        make_decision("B1_CONGESTION", "RETRY_SCHEDULED", "ALLOW"),
        make_decision("B1_CONGESTION", "RETRY_SCHEDULED", "ALLOW", decision_id="dec_2"),
        make_decision("B1_CONGESTION", "RETRY_SCHEDULED", "ALLOW", decision_id="dec_3"),
        make_decision("B5_DEAD", "NUDGE_SENT", "BLOCK", decision_id="dec_4"),
        make_decision("B5_DEAD", "NUDGE_SENT", "BLOCK", decision_id="dec_5"),
        make_decision("B2_BALANCE", "HUMAN_QUEUE", "ESCALATE", decision_id="dec_6"),
    ]
    for decision in decisions:
        explainer.generate(context_from_decision(decision, matrix))

    assert len(decisions) == 6
    assert explainer.api_calls == 3  # three distinct keys, not six decisions
    assert explainer.cache_size == 3


def test_cache_is_not_polluted_by_a_failed_call(matrix):
    """A failure must not cache the template under the LLM key -- once the
    API recovers, the next decision of that class should try again."""
    context = context_from_decision(make_decision(), matrix)

    failing = Explainer(client=FailingClient())
    text, source = failing.generate(context)
    assert source == SOURCE_TEMPLATE
    assert failing.cache_size == 0


# --- fallback --------------------------------------------------------------


@pytest.mark.parametrize(
    "client",
    [FailingClient(), FailingClient(exc=TimeoutError("read timeout")), EmptyResponseClient(), None],
    ids=["connection_error", "timeout", "empty_response", "no_client_configured"],
)
def test_every_failure_mode_falls_back_to_a_non_empty_template(client, matrix):
    """PRD M7 acceptance: every decision has a non-empty explanation, with
    or without network access. `None` is the no-API-key case, which is the
    default state of this repo."""
    explainer = Explainer(client=client) if client is not None else Explainer()
    text, source = explainer.generate(context_from_decision(make_decision(), matrix))

    assert source == SOURCE_TEMPLATE
    assert text.strip()


def test_every_bucket_and_action_has_a_template(matrix):
    """No (bucket, action) combination may render an empty or placeholder
    sentence -- the fallback is only a guarantee if it covers everything
    the executor can emit."""
    actions = ["RETRY_SCHEDULED", "NUDGE_SENT", "STOPPED", "HUMAN_QUEUE"]
    for bucket in matrix.buckets:
        for action in actions:
            for verdict in ("ALLOW", "BLOCK", "ESCALATE"):
                context = context_from_decision(make_decision(bucket, action, verdict), matrix)
                text = render_template(context)
                assert text.strip()
                assert "{" not in text  # an unfilled format placeholder


def test_template_never_leaks_pii(matrix):
    """The fallback path is subject to the same rule as the LLM path --
    it is stored in the same audit column and shown in the same UI."""
    context = context_from_decision(make_decision(), matrix)
    text = render_template(context)
    for value in ("cust_", "pay_", "@", "evt_"):
        assert value not in text


# --- structure -------------------------------------------------------------


def test_decision_tier_does_not_import_the_explanation_layer():
    """The structural guarantee, asserted rather than asserted-in-prose:
    if `executor` ever imports `explain`, the "cannot block a decision"
    claim stops being structural and becomes a matter of discipline."""
    import ast
    from pathlib import Path

    decision_tier = ["executor/executor.py", "policy/policy.py", "classifier/classify.py"]
    for relative in decision_tier:
        source = Path(relative).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.startswith("explain") for name in names), (
                f"{relative} imports the explanation layer -- the decision tier must not depend on it"
            )


def test_explainer_records_the_source_it_used(matrix):
    context = context_from_decision(make_decision(), matrix)
    assert Explainer(client=RecordingClient()).generate(context)[1] == SOURCE_LLM
    assert Explainer(client=FailingClient()).generate(context)[1] == SOURCE_TEMPLATE
