import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check against the raw request body.

    Must run against raw bytes, not re-serialised JSON -- re-serialising
    changes byte order/whitespace and breaks the signature.
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
