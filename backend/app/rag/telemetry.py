"""Telemetry is optional data, never rewrite or retrieval authority."""
from collections.abc import Mapping
import logging
logger = logging.getLogger(__name__)

def parse_usage(value):
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value or any(
        not isinstance(k, str) or type(v) is not int or v < 0 for k,v in value.items()
    ):
        logger.warning("usage_invalid")
        return None
    return dict(value)

def legacy_usage(value):
    parsed = parse_usage(value)
    # Preserve dict signature. Missing token fields mean unknown, never measured zero.
    return {"usage_known": False} if parsed is None else {**parsed, "usage_known": True}

def observe_result(result):
    if result.outcome == "error" or result.degraded:
        logger.warning("retrieval_error" if result.outcome == "error" else "retrieval_degraded",
            extra={"retrieval_outcome": result.outcome, "retrieval_codes": list(result.errors),
                   "retrieval_stages": [e.model_dump() for e in result.stage_events],
                   "retrieval_index_version": result.metadata.index_version})
