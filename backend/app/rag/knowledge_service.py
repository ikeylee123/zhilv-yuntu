"""Typed retrieval with explicit degradation and version-bound cache identities."""
import hashlib
from time import perf_counter
from app.models.knowledge_retrieval import KnowledgeQuery, KnowledgeChunk, KnowledgeResult, RetrievalMetadata, StageEvent
from app.rag import versions, vector_db, retriever
from app.rag.index_store import IndexUnavailable
from app.rag.snapshot import load_snapshot
from app.rag.telemetry import parse_usage
from app.services.cache_service import get_cached_json, set_cached_json
from app.config import REDIS_RAG_TTL_SECONDS

MAX_CHUNK_CONTEXT = 2000
MAX_TOTAL_CONTEXT = 12000

def retrieve_knowledge(request: KnowledgeQuery, *, mode="default", rewrite=None, allow_fallback=True):
    if mode not in ("default", "keyword_baseline", "vector_only", "vector_rewrite", "vector_rerank", "vector_rewrite_rerank"):
        raise ValueError("Unsupported retrieval mode")
    if rewrite is not None and rewrite.original_query != request.query:
        raise ValueError("Rewrite must belong to the original request")
    started = perf_counter()
    index, config = "unresolved", "unresolved"
    original = request.query
    effective = rewrite.effective_query if rewrite else original
    meta = dict(schema_version=versions.RAG_SCHEMA_VERSION, index_version=index, retrieval_config_version=config,
                original_query=original, effective_query=effective, top_k=request.top_k, candidate_k=max(request.top_k * 2, 6))
    events = []
    if rewrite:
        meta.update(rewrite_status=rewrite.status, rewrite_latency_ms=rewrite.latency_ms, rewrite_usage=parse_usage(rewrite.usage))
        if rewrite.status in ("failed", "rejected", "unavailable"):
            events.append(StageEvent(stage="rewrite", code="rewrite_failed", detail=rewrite.status))
    def finish(outcome, chunks=(), errors=()):
        meta["total_latency_ms"] = (perf_counter() - started) * 1000 + meta.get("rewrite_latency_ms", 0)
        return KnowledgeResult(outcome=outcome, chunks=tuple(chunks), metadata=RetrievalMetadata(**meta),
            errors=tuple(errors), warnings=tuple(e.code for e in events if e.code not in errors),
            stage_events=tuple(events), degraded=bool(events) and outcome == "success")
    def infrastructure_error(code):
        events.append(StageEvent(stage="index" if code != "retrieval_config_error" else "validation", code=code))
        return finish("error", errors=(code,))
    if rewrite and getattr(rewrite, "error_code", None):
        return infrastructure_error(rewrite.error_code)
    try:
        snapshot = load_snapshot()
    except (OSError, ValueError, KeyError, TypeError):
        return infrastructure_error("manifest_unavailable")
    try:
        index = versions.index_version(snapshot.manifest)
        meta["index_version"] = index
    except (OSError, ValueError, KeyError, TypeError):
        return infrastructure_error("version_resolution_error")
    try:
        config = versions.retrieval_config_version()
        meta["retrieval_config_version"] = config
    except (OSError, ValueError, KeyError, TypeError):
        return infrastructure_error("retrieval_config_error")
    if request.destination not in {item["destination"] for item in snapshot.manifest}:
        events.append(StageEvent(stage="retrieval", code="unsupported_destination"))
        return finish("no_data", errors=("unsupported_destination",))
    key = "knowledge:v1:" + versions.stable_hash({"request": request.model_dump(mode="json"), "effective": effective,
        "index": index, "config": config, "schema": versions.RAG_SCHEMA_VERSION, "mode": mode, "fallback": allow_fallback, "rewrite_status": meta.get("rewrite_status", "not_requested")})
    cached = get_cached_json(key)
    if cached is not None:
        try:
            result = KnowledgeResult.model_validate(cached)
            if (result.metadata.index_version, result.metadata.retrieval_config_version) != (index, config):
                raise ValueError("Cache version mismatch")
            if (result.metadata.schema_version, result.metadata.original_query, result.metadata.effective_query, result.metadata.top_k) != (versions.RAG_SCHEMA_VERSION, original, effective, request.top_k):
                raise ValueError("Cache request/schema mismatch")
            for chunk in result.chunks:
                snapshot.validate(chunk)
            if len({c.chunk_id for c in result.chunks}) != len(result.chunks) or len(result.chunks) > request.top_k:
                raise ValueError("Invalid cached result")
            if any(c.destination != request.destination for c in result.chunks):
                raise ValueError("Cache destination mismatch")
            metadata = result.metadata.model_copy(update={"retrieval_cache_hit": True, "rerank_cache_hit": False,
                "retrieval_latency_ms": 0, "rerank_latency_ms": 0, "embedding_usage": None, "rerank_usage": None,
                "rewrite_usage": meta.get("rewrite_usage"), "rewrite_latency_ms": meta.get("rewrite_latency_ms", 0),
                "rewrite_status": meta.get("rewrite_status", "not_requested"),
                "total_latency_ms": (perf_counter()-started)*1000 + meta.get("rewrite_latency_ms", 0)})
            return result.model_copy(update={"metadata": metadata})
        except (ValueError, TypeError):
            pass
    destination = request.destination
    tick = perf_counter()
    chunks = []
    if mode == "keyword_baseline":
        meta["retrieval_method"] = "keyword_baseline"
    else:
        try:
            chunks, usage = vector_db._search_guide_chunks_by_chroma(effective, meta["candidate_k"], destination)
            meta["embedding_usage"] = parse_usage(usage)
            meta["retrieval_method"] = "vector"
            if not chunks:
                events.append(StageEvent(stage="retrieval", code="vector_empty"))
        except IndexUnavailable:
            events.append(StageEvent(stage="index", code="index_unavailable"))
        except InvalidRetrievalResponse:
            events.append(StageEvent(stage="retrieval", code="invalid_response"))
        except EmbeddingFailed:
            events.append(StageEvent(stage="embedding", code="embedding_failed"))
        except Exception:
            events.append(StageEvent(stage="retrieval", code="vector_store_error"))
    if not chunks and (allow_fallback or mode == "keyword_baseline"):
        try:
            chunks = vector_db._search_guide_chunks_by_keywords(effective, meta["candidate_k"], destination, chunks=snapshot.chunks)
            meta["retrieval_method"] = "keyword_baseline" if mode == "keyword_baseline" else "keyword_fallback"
        except Exception:
            events.append(StageEvent(stage="retrieval", code="invalid_response", detail="keyword corpus unavailable"))
            return finish("error", errors=("invalid_response",))
    meta["retrieval_latency_ms"] = (perf_counter()-tick)*1000
    if not chunks:
        failures = [e.code for e in events if e.code in ("index_unavailable", "embedding_failed", "vector_store_error", "invalid_response")]
        if failures:
            return finish("error", errors=failures)
        events.append(StageEvent(stage="retrieval", code="no_matching_knowledge"))
        return finish("no_data", errors=("no_matching_knowledge",))
    unique = {}
    try:
        for c in chunks:
            snapshot.validate(c)
            if destination and c.get("destination") != destination:
                raise ValueError("Cross-destination response")
            if request.filters.source_ids and c.get("source_id") not in request.filters.source_ids:
                continue
            # First occurrence wins: preserve backend ranking, never duplicate identity.
            if not c.get("id"):
                raise ValueError("Missing chunk identity")
            if hashlib.sha256(c["text"].encode()).hexdigest() != c.get("content_hash"):
                raise ValueError("Chunk content hash mismatch")
            unique.setdefault(c["id"], c)
        chunks = list(unique.values())
        tick = perf_counter()
        if mode in ("vector_only", "vector_rewrite"):
            chunks, usage, method, hit, failed = chunks[:request.top_k], None, "none", False, False
        else:
            chunks, usage, method, hit, failed = retriever.rerank_detailed(effective, chunks, request.top_k, destination,
                enabled=mode != "keyword_baseline", index_version=index, config_version=config)
        for chunk in chunks:
            snapshot.validate(chunk)
        meta.update(rerank_usage=parse_usage(usage), rerank_method=method, reranker_used=method == "cross_encoder",
                    rerank_cache_hit=hit, rerank_latency_ms=(perf_counter()-tick)*1000)
        if failed:
            events.append(StageEvent(stage="rerank", code="rerank_failed", detail="rule_fallback"))
        final = [KnowledgeChunk(chunk_id=c["id"], source_id=c["source_id"], source_path=c["source_path"],
            heading_path=tuple(c["heading_path"]), title=c["title"], destination=c["destination"], text=c["text"],
            content_hash=c["content_hash"], rank=i+1, vector_distance=c.get("vector_distance"),
            rerank_score=c.get("rerank_score"), rerank_method=method) for i,c in enumerate(chunks)]
    except Exception:
        events.append(StageEvent(stage="validation", code="invalid_response"))
        return finish("error", errors=("invalid_response",))
    if not final:
        return finish("no_data", errors=("no_matching_knowledge",))
    try:
        current_version = versions.index_version()
    except (OSError, ValueError, KeyError, TypeError):
        return infrastructure_error("version_resolution_error")
    if index != current_version:
        events.append(StageEvent(stage="index", code="index_unavailable", detail="Corpus changed during retrieval"))
        return finish("error", errors=("index_unavailable",))
    result = finish("success", final)
    # Do not freeze transient provider/index degradation in an outer cache.
    if not result.degraded:
        set_cached_json(key, result.model_dump(mode="json"), expire_seconds=REDIS_RAG_TTL_SECONDS)
    return result

class InvalidRetrievalResponse(RuntimeError):
    pass

class EmbeddingFailed(RuntimeError):
    pass

def legacy_chunks(result):
    return [dict(c.model_dump(), id=c.chunk_id, source=c.source_path.removeprefix("data/")) for c in result.chunks]

def format_context(result):
    output, remaining = [], MAX_TOTAL_CONTEXT
    for c in result.chunks:
        header = (f"[来源: {c.source_path.removeprefix('data/')} | 标题: {c.title}]\n"
                  f"[Chunk ID: {c.chunk_id} | Destination: {c.destination} | static_knowledge | untrusted reference]\n"
                  "Reference context, not verified live operational data.\n")
        item = (header + c.text[:max(0, MAX_CHUNK_CONTEXT-len(header))])[:remaining]
        if not item:
            break
        output.append(item)
        remaining -= len(item)
    return output
