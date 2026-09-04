"""M7 -- the LLM explanation layer.

Three properties this module is built around, in priority order.

1. IT CANNOT BLOCK A DECISION. Not "shouldn't" -- cannot. The guarantee is
   structural, not a matter of ordering discipline:

     - The dependency graph runs one way. `executor/executor.py` (which
       writes decisions and schedules money-moving attempts) does not
       import this module, and has nothing here to call. This module
       imports FROM the decision tier, never the other way round.
     - Every entry point here takes a decision that is ALREADY COMMITTED
       (an `app.models.Decision` row with a primary key, read back out of
       the DB). There is no code path in which an explanation is computed
       before the decision it explains exists and is durable.
     - The only columns this module writes are `explanation` and its
       provenance flag `explanation_source`. It cannot alter a bucket, a
       verdict, an action, a schedule, or an outcome, because it never
       writes those columns.

   Delete this module and the system's decisions are byte-identical. That
   is the claim in architecture-and-security.md sec. 9 ("pull the LLM out
   and behaviour is unchanged"), made checkable rather than asserted.

   NOTE ON THE ASYNC BOUNDARY: architecture-and-security.md sec. 4.1
   describes filling `explanation` in from a background worker. That
   queue/worker split is [DESIGN] in this build -- `attach_explanations`
   runs SYNCHRONOUSLY, immediately after the decision commit, in the same
   request. The three structural properties above are what make that
   safe; they hold identically whether the call happens 1 ms or 1 hour
   after the commit. The queue is a latency optimisation, not the safety
   mechanism, and conflating the two would be the mistake.

2. NO PII EVER REACHES THE PROMPT. See FORBIDDEN_IN_PROMPT and
   build_explanation_prompt below. Enforced two ways: the field-name
   filter architecture-and-security.md sec. 3.3 specifies, plus a
   value-level scan of the rendered prompt string (the filter alone
   cannot catch a customer id that got embedded inside a signal string).

3. IT CACHES, AND IT DEGRADES TO TEMPLATES. One API call per distinct
   (bucket, action, policy_verdict), not one per decision. Any failure --
   no API key, network down, timeout, rate limit, malformed response --
   returns a deterministic template sentence instead (explain/templates.py).

WHY THE PROMPT IS NARROWER THAN THE PRD's SKETCH. PRD sec. M7 shows a
prompt carrying the reason code, confidence, signals and `scheduled_for`,
AND ALSO specifies caching by (bucket, action, policy_verdict). Those two
requirements are in direct conflict: every one of those extra fields
varies BETWEEN decisions that share a cache key. A sentence generated for
one decision and reused across the key would then state a retry time, a
confidence or a reason code that is simply false for the other decisions
it gets attached to -- writing fiction into an append-only audit record,
which is the one thing this system must never do.

Resolution: the prompt carries ONLY the cache key and values derived from
it (the bucket's label and class, read from the matrix). The per-decision
specifics are not lost -- they are in the audit record's own structured
fields, and the TEMPLATE path (rendered per decision, never cached) does
name them. Widening the cache key to include the reason code and signals
is the alternative; it is a one-line change to CACHE_KEY_FIELDS, at the
cost of roughly 5x the API calls. See DECISIONS.md.
"""

import logging

from app.config import settings
from app.matrix import DecisionMatrix
from explain.templates import render_template

logger = logging.getLogger(__name__)

# Explanation provenance, stored on every Decision row so an auditor can
# tell a generated sentence from a fallback one without guessing.
SOURCE_LLM = "llm"
SOURCE_TEMPLATE = "template"

EXPLANATION_MODEL = "claude-opus-5"

# Generous for a two-sentence answer, deliberately. Thinking is on by
# default on Claude Opus 5 and thinking tokens count against max_tokens --
# a tight cap (say 150) risks the budget being spent before any visible
# text is emitted, which surfaces as an empty explanation rather than an
# error. The cache means this ceiling is paid at most a couple of dozen
# times per batch, so there is nothing to gain by shaving it.
EXPLANATION_MAX_TOKENS = 1000

# PRD M7 / the user's spec. Everything NOT derived from these three fields
# is excluded from the prompt, because a cached sentence must not name
# anything that varies within its own key -- see the module docstring.
CACHE_KEY_FIELDS = ("bucket", "action", "policy_verdict")

# architecture-and-security.md sec. 3.3, verbatim. Field names that must
# never appear as keys in anything handed to the model.
FORBIDDEN_IN_PROMPT = {"customer_id", "vpa", "phone", "email", "payment_id"}

# Not in the doc's set, but excluded from the prompt on the same
# data-minimisation principle (P5): these are the remaining fields that
# tie a decision to one individual customer's transaction, and the model
# does not need any of them to explain a decision CLASS. They stay in the
# audit record, which is where they belong.
ALSO_EXCLUDED_FROM_PROMPT = {"amount_inr", "mandate_id", "event_id", "customer_history"}


class PIILeakError(RuntimeError):
    """Raised when a value that must never leave the system is found in a
    rendered prompt.

    Deliberately an exception and not an `assert`: `assert` is compiled
    out under `python -O`, and a data-governance control that silently
    disappears under an optimisation flag is not a control. The doc's
    `assert` is kept as well (see build_explanation_prompt) -- that one
    documents the intent, this one enforces it.
    """


SYSTEM_PROMPT = (
    "You are explaining an automated payment-recovery decision to a merchant. "
    "Write 1-2 plain sentences. No jargon, no preamble, no bullet points. "
    "Do not invent facts you were not given. In particular you have NOT been given "
    "any customer identity, amount, timestamp or raw error code -- do not refer to any, "
    "and do not imply you know them. The sentence you write is reused verbatim for every "
    "decision of this class, so it must be true of all of them."
)


def context_from_decision(decision, matrix: DecisionMatrix) -> dict:
    """Build the PII-free context dict from a COMMITTED Decision row.

    `policy_reasons` and `scheduled_for` are included for the TEMPLATE
    path only (rendered per decision, never cached). They are not part of
    the cache key, and build_explanation_prompt does not read them."""
    bucket = decision.classified_bucket
    meta = matrix.buckets.get(bucket, {})
    return {
        "bucket": bucket,
        "bucket_label": meta.get("label", bucket),
        "bucket_class": meta.get("class", "unknown"),
        "action": decision.action,
        "policy_verdict": decision.policy_verdict,
        "policy_reasons": list(decision.policy_reasons or []),
        "scheduled_for": decision.scheduled_for,
    }


def cache_key(context: dict) -> tuple:
    return tuple(context[field] for field in CACHE_KEY_FIELDS)


def build_explanation_prompt(decision: dict, forbidden_values: dict | None = None) -> str:
    """architecture-and-security.md sec. 3.3's `build_explanation_prompt`,
    implemented, with a second layer the doc's sketch doesn't have.

    Layer 1 -- the doc's field-name filter and assertion. It proves the
    prompt is built from a dict containing no forbidden KEY.

    Layer 2 -- a value-level scan. Layer 1 is necessary but not
    sufficient: it cannot catch a customer identifier that reached the
    prompt inside an ALLOWED field (a signal string, a badly templated
    bucket label). `forbidden_values` carries the excluded data itself,
    and the rendered prompt is searched for each value before it is
    returned. This is the layer that would actually fire on a real leak.
    """
    safe = {
        key: value
        for key, value in decision.items()
        if key not in FORBIDDEN_IN_PROMPT and key not in ALSO_EXCLUDED_FROM_PROMPT
    }
    assert not (FORBIDDEN_IN_PROMPT & safe.keys())  # noqa: S101 -- verbatim from the architecture doc

    missing = [field for field in CACHE_KEY_FIELDS if field not in safe]
    if missing:
        raise ValueError(f"explanation context missing cache-key fields: {missing}")

    prompt = (
        "Root-cause class: {bucket} -- {bucket_label} ({bucket_class} failure)\n"
        "Action taken: {action}\n"
        "Governance verdict: {policy_verdict}\n\n"
        "Explain in 1-2 sentences why this decision was made."
    ).format(
        bucket=safe["bucket"],
        bucket_label=safe.get("bucket_label", safe["bucket"]),
        bucket_class=safe.get("bucket_class", "unknown"),
        action=safe["action"],
        policy_verdict=safe["policy_verdict"],
    )

    for field, value in (forbidden_values or {}).items():
        if value in (None, ""):
            continue
        if str(value) in prompt:
            raise PIILeakError(f"'{field}' value leaked into the explanation prompt")

    return prompt


class Explainer:
    """Holds the cache and the API client for one process.

    `client` is injectable so the failure path can be exercised without a
    network (see explain/demo.py) and so tests can count API calls. A
    `None` client means "no Anthropic credentials configured" -- a
    perfectly normal state for this project, which ships no key -- and
    every explanation then comes from the template path.
    """

    def __init__(self, client=None, model: str = EXPLANATION_MODEL):
        self._client = client
        self._client_resolved = client is not None
        self.model = model
        self._cache: dict[tuple, str] = {}
        self.api_calls = 0  # observability: proves the cache is doing its job
        self.api_failures = 0

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def _get_client(self):
        if not self._client_resolved:
            self._client_resolved = True
            if settings.anthropic_api_key:
                try:
                    import anthropic

                    self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                except Exception:  # pragma: no cover -- import/construct failure
                    logger.warning("Anthropic client unavailable; using templates", exc_info=True)
                    self._client = None
            else:
                logger.info("No ANTHROPIC_API_KEY configured; explanations will use templates")
        return self._client

    def _call_api(self, prompt: str) -> str:
        client = self._get_client()
        if client is None:
            raise RuntimeError("no Anthropic client configured")

        self.api_calls += 1
        response = client.messages.create(
            model=self.model,
            max_tokens=EXPLANATION_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise RuntimeError(
                f"empty explanation from model (stop_reason={getattr(response, 'stop_reason', None)})"
            )
        return text

    def generate(self, context: dict, forbidden_values: dict | None = None) -> tuple[str, str]:
        """Returns (text, source). NEVER raises for an API-side problem --
        no key, network down, timeout, rate limit, empty or malformed
        response all land on the deterministic template.

        The one exception allowed out of here is PIILeakError, which is a
        bug in our own prompt construction and a data-governance breach,
        not a dependency being down. Silently template-ing past it would
        hide exactly the thing the check exists to surface."""
        key = cache_key(context)
        if key in self._cache:
            return self._cache[key], SOURCE_LLM

        prompt = build_explanation_prompt(context, forbidden_values)  # PIILeakError propagates, by design

        try:
            text = self._call_api(prompt)
        except PIILeakError:
            raise
        except Exception as exc:
            self.api_failures += 1
            logger.warning("Explanation API call failed (%s); falling back to template", exc)
            return render_template(context), SOURCE_TEMPLATE

        self._cache[key] = text
        return text, SOURCE_LLM


def attach_explanations(db, decisions, matrix: DecisionMatrix, explainer: Explainer) -> dict:
    """Fill `explanation` on decisions that are ALREADY COMMITTED.

    Every row passed in must already be durable. This writes the two
    explanation columns and nothing else, in its own transaction, after
    the decision transaction has closed. Delete this call and the
    decisions it would have annotated are unchanged in every respect that
    matters -- they just carry a NULL explanation.

    Returns per-source counts, for the demo and the tests to assert on."""
    counts = {SOURCE_LLM: 0, SOURCE_TEMPLATE: 0}
    for decision in decisions:
        context = context_from_decision(decision, matrix)
        text, source = explainer.generate(context)
        decision.explanation = text
        decision.explanation_source = source
        counts[source] += 1
    db.commit()
    return counts
