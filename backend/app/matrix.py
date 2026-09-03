from functools import lru_cache
from pathlib import Path

import yaml

from app.config import settings

REQUIRED_TOP_LEVEL_KEYS = {
    "matrix_version",
    "buckets",
    "reason_codes",
    "congestion_override",
    "unknown_bucket",
}


class DecisionMatrix:
    """Typed view over the loaded decision_matrix.yaml. No decision logic
    lives here -- this only loads and validates the config shape."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.matrix_version: str = raw["matrix_version"]
        self.buckets: dict = raw["buckets"]
        self.reason_codes: dict = raw["reason_codes"]
        self.congestion_override: dict = raw["congestion_override"]
        self.unknown_bucket: dict = raw["unknown_bucket"]

    def play_for(self, reason: str) -> dict | None:
        """The play for a known reason code, or None if it isn't in the
        matrix -- callers must treat None as a B_UNKNOWN trigger."""
        return self.reason_codes.get(reason)


VALID_SOURCES = {"customer", "business", "gateway", "razorpay"}


def _validate(raw: dict) -> None:
    missing = REQUIRED_TOP_LEVEL_KEYS - raw.keys()
    if missing:
        raise ValueError(f"decision_matrix.yaml missing required keys: {sorted(missing)}")

    for reason, play in raw["reason_codes"].items():
        bucket = play.get("bucket")
        if bucket not in raw["buckets"]:
            raise ValueError(f"reason_code '{reason}' references undefined bucket '{bucket}'")
        source = play.get("source")
        if source not in VALID_SOURCES:
            raise ValueError(f"reason_code '{reason}' has missing/invalid source '{source}'")

    override_bucket = raw["congestion_override"].get("reclassify_to")
    if override_bucket not in raw["buckets"]:
        raise ValueError(f"congestion_override.reclassify_to references undefined bucket '{override_bucket}'")

    unknown_bucket = raw["unknown_bucket"].get("bucket")
    if unknown_bucket not in raw["buckets"]:
        raise ValueError(f"unknown_bucket.bucket references undefined bucket '{unknown_bucket}'")


@lru_cache
def load_decision_matrix(path: str | None = None) -> DecisionMatrix:
    matrix_path = Path(path or settings.decision_matrix_path)
    if not matrix_path.exists():
        raise FileNotFoundError(f"Decision matrix not found at {matrix_path.resolve()}")

    with matrix_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    _validate(raw)
    return DecisionMatrix(raw)
