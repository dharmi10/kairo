import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check against the raw request body.

    Must run against raw bytes, not re-serialised JSON -- re-serialising
    changes byte order/whitespace and breaks the signature.
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_webhook_body(raw_body: bytes, secret: str) -> str:
    """The sender's half of the same computation. Exists so the tests and
    the demo script produce signatures the way Razorpay would, instead of
    each re-implementing HMAC and both being wrong in the same way.

    This is a test/demo affordance, not a production capability -- the
    real signer is Razorpay, and this project never signs a webhook it
    then sends anywhere."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
