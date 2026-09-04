"""demo/seed.py -- just the one thing worth regression-testing: that its
UNKNOWN_REASON_CODE constant stays genuinely absent from the decision
matrix. Everything else the script does (webhook signing, dedupe,
fail-closed routing) is already covered end-to-end by tests/test_api.py;
this guards against someone later adding a reason code to
config/decision_matrix.yaml that happens to collide with the demo's
constant, which would silently turn "prove fail-closed live" into "prove
a normal retry live" without any test failing to say so."""

from app.matrix import load_decision_matrix
from demo.seed import UNKNOWN_REASON_CODE


def test_demo_unknown_reason_code_is_not_actually_in_the_matrix():
    matrix = load_decision_matrix()
    assert UNKNOWN_REASON_CODE not in matrix.reason_codes
