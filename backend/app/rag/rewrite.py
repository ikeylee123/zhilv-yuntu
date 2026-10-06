"""Bounded rewrite policy; corpus-recognizable entities survive every fallback."""
from dataclasses import dataclass
import re
from time import perf_counter
from app.rag.guide_catalog import canonical_destination
from app.rag.vector_db import load_guide_chunks
from app.rag.telemetry import parse_usage

MAX_QUERY = 2048
MAX_REWRITE = 1024

@dataclass(frozen=True)
class RewriteResult:
    original_query: str
    effective_query: str
    status: str
    usage: dict | None
    latency_ms: float
    error_code: str | None = None

def recognized_entities(original, destination):
    terms = set()
    for chunk in load_guide_chunks():
        if chunk["destination"] != destination:
            continue
        title = re.sub(r"^\d+(?:\.\d+)*[.、\s]*", "", chunk["title"]).strip()
        attraction = bool(re.match(r"^2\.\d+\s", chunk["title"]))
        candidates = ([title] if attraction else []) + re.findall(r"【([^】]+)】", chunk["text"])
        # A recognized prefix supports common short names, e.g. 故宫 / 故宫博物院.
        for term in candidates:
            for length in range(2, len(term)+1):
                if term[:length] in original:
                    terms.add(term[:length])
    return sorted((t for t in terms if not any(t != u and t in u for u in terms)), key=lambda t: (-len(t), t))

def rewrite_query(original, destination, *, preferences=None, pace=None, provider=None):
    started = perf_counter()
    destination = canonical_destination(destination)
    original = original.strip()
    if not original or len(original) > MAX_QUERY:
        raise ValueError("Query must contain 1..2048 characters")
    try:
        entities = recognized_entities(original, destination)
    except (OSError, ValueError, KeyError, TypeError):
        return RewriteResult(original, original, "failed", None, (perf_counter()-started)*1000, "manifest_unavailable")
    # Original free text takes priority over optional structured expansions.
    fallback = " ".join([destination, original] + list(preferences or []) + ([pace] if pace else []))[:4096]
    # Full bounded original (<=2048) survives; structured expansions use the remaining budget.
    status, usage, effective = "unavailable", None, fallback
    if provider is not None:
        try:
            candidate, usage = provider()
            if not isinstance(candidate, str) or not candidate.strip():
                status = "failed"
            elif len(candidate) > MAX_REWRITE or destination not in candidate or any(e not in candidate for e in entities):
                status = "rejected"
            else:
                status, effective = "accepted", candidate.strip()
        except Exception:
            status = "failed"
    return RewriteResult(original, effective, status, parse_usage(usage),
                         (perf_counter()-started)*1000)
