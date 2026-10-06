import hashlib
import json
import logging
import re

import httpx

from app.config import LLM_API_KEY, REDIS_RAG_TTL_SECONDS, REDIS_RERANK_TTL_SECONDS, RERANK_MODEL

from app.rag.telemetry import legacy_usage, observe_result
from app.services.cache_service import get_cached_json, set_cached_json


DASHSCOPE_RERANK_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"


logger = logging.getLogger(__name__)


def _normalize_cache_text(value: str) -> str:
    """把检索 query 做简单标准化，避免大小写和空格造成重复 key。"""
    return " ".join(value.strip().lower().split())


def _extract_query_keywords(query: str) -> list[str]:
    """从 query 中切出用于轻量重排序的关键词。"""
    raw_parts = re.split(r"[\s,，。；;、]+", query)
    return [part.strip() for part in raw_parts if part.strip()]


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _score_chunk_for_rerank(
    query: str,
    chunk: dict[str, str],
    destination: str | None = None,
) -> int:
    """根据 query 关键词对召回片段做轻量打分。"""
    title = chunk.get("title", "")
    text = chunk.get("text", "")
    source = chunk.get("source", "")
    combined_text = f"{title}\n{text}"
    reasons: list[str] = []

    score = 0
    for keyword in _extract_query_keywords(query):
        if keyword in title:
            score += 3
            reasons.append(f"title+3:{keyword}")
        if keyword in text:
            score += 1
            reasons.append(f"text+1:{keyword}")

    # 文档开头通常是低信息量噪声片段。
    if title == "文档开头":
        score -= 8
        reasons.append("noise-8:文档开头")

    # 行程类片段更适合承接"景点 / 行程 / 推荐"类请求。
    if "行程" in title and "行程参考" not in title:
        score += 4
        reasons.append("domain+4:行程标题")

    # "经典行程参考"类片段内容过于全面，会霸占 Top1，对非行程查询做降权。
    if "行程参考" in title:
        score -= 4
        reasons.append("domain-4:行程参考降权")

    # "目的地简介"内容过于泛化，对具体查询（美食、亲子等）不是最优候选。
    if "目的地简介" in title:
        score -= 2
        reasons.append("domain-2:目的地简介降权")

    # 餐饮/预算类片段在"日落/拍照/轻松"这类主目标下通常不是最优候选。
    if _contains_any(title, ["餐饮", "预算"]) and not _contains_any(
        combined_text,
        ["日落", "傍晚", "拍照", "摄影", "出片", "洱海", "双廊", "慢节奏"],
    ):
        score -= 3
        reasons.append("domain-3:餐饮预算弱相关")

    # 目的地不匹配降权：优先使用 Chunk 元数据，兼容旧 Chunk 时才退回文本判断。
    if destination:
        chunk_destination = chunk.get("destination", "")
        if chunk_destination and chunk_destination != destination:
            score -= 5
            reasons.append(f"dest-5:metadata={chunk_destination}")
        elif not chunk_destination:
            chunk_lower = f"{source} {title} {text}".lower()
            if destination.lower() not in chunk_lower:
                score -= 5
                reasons.append(f"dest-5:缺失元数据且非{destination}片段")

    chunk["rerank_reasons"] = reasons
    return score


_NOISE_TITLES = {"文档开头"}


def _extract_rerank_token_usage(response_data: dict) -> tuple[dict[str, int], bool]:
    """只读取接口返回的官方 usage；没有 usage 时不做本地估算。"""
    usage = response_data.get("usage") or response_data.get("output", {}).get("usage") or {}
    prompt_tokens = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("input_token_count")
        or 0
    )
    completion_tokens = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("output_token_count")
        or 0
    )
    total_tokens = usage.get("total_tokens") or usage.get("total_token_count") or 0

    if not prompt_tokens and not completion_tokens and total_tokens:
        prompt_tokens = total_tokens

    if prompt_tokens or completion_tokens:
        return {
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
        }, True

    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }, False


def validate_rerank_results(results, count):
    import math
    if not isinstance(results, list) or not results:
        raise ValueError("Empty/malformed reranker response")
    seen, scored = set(), []
    for item in results:
        index = item.get("index")
        score = item.get("relevance_score", 0.0)
        if type(index) is not int or not 0 <= index < count or index in seen:
            raise ValueError("Invalid or duplicate reranker index")
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("Invalid reranker score")
        seen.add(index)
        scored.append((float(score), index))
    return sorted(scored, key=lambda pair: pair[0], reverse=True)


def _rerank_with_dashscope(query, chunks, top_k):
    if not LLM_API_KEY or not chunks:
        return None, None
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(DASHSCOPE_RERANK_URL,
                json={"model": RERANK_MODEL, "documents": [f"{c['title']}\n{c['text']}" for c in chunks],
                      "query": query, "top_n": min(top_k, len(chunks)),
                      "instruct": "优先选择具体、详细、直接回答旅行查询的片段，避免泛化简介。"},
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"})
        response.raise_for_status()
        data = response.json()
        usage, official = _extract_rerank_token_usage(data)
        results = data.get("output", {}).get("results") or data.get("results")
        return validate_rerank_results(results, len(chunks)), usage if official else None
    except Exception as exc:
        logger.warning("rerank_failed type=%s", type(exc).__name__)
        return None, None


def _build_rerank_cache_key(query, chunks, index_version=None, config_version=None):
    from app.rag import versions
    return "rerank:v1:" + versions.stable_hash({"query": query,
        "chunks": [(c.get("id"), c.get("content_hash"), versions.stable_hash(c.get("text", ""))) for c in chunks],
        "model": RERANK_MODEL, "index": index_version or versions.index_version(),
        "config": config_version or versions.retrieval_config_version()})


def rerank_detailed(query, chunks, top_k, destination=None, *, enabled=True, index_version=None, config_version=None):
    key = _build_rerank_cache_key(query, chunks, index_version, config_version)
    scored, usage, hit = None, None, False
    if enabled:
        cached = get_cached_json(key)
        if cached is not None:
            try:
                if not isinstance(cached, dict) or cached.get("identity") != [(c["id"], c["content_hash"]) for c in chunks]:
                    # Redis JSON round-trips tuples as lists; compare normalized structures.
                    if not isinstance(cached, dict) or cached.get("identity") != [[c["id"], c["content_hash"]] for c in chunks]:
                        raise ValueError("Cached ranking identity mismatch")
                scored = validate_rerank_results(cached["ranking"], len(chunks))
                if len(scored) != len(chunks):
                    raise ValueError("Incomplete cached ranking")
                hit = True
            except (ValueError, TypeError, AttributeError, KeyError):
                scored = None
        if scored is None:
            scored, usage = _rerank_with_dashscope(query, chunks, len(chunks))
            if scored is not None:
                try:
                    scored = validate_rerank_results([{"index": i, "relevance_score": s} for s, i in scored], len(chunks))
                    if len(scored) != len(chunks):
                        raise ValueError("Incomplete provider ranking")
                except (ValueError, TypeError):
                    scored = None
            if scored:
                set_cached_json(key, {"identity": [[c["id"], c["content_hash"]] for c in chunks], "ranking": [{"index": i, "relevance_score": s} for s, i in scored]}, expire_seconds=REDIS_RERANK_TTL_SECONDS)
    method = "cross_encoder" if scored else "rule_fallback"
    if scored is None:
        scored = sorted([(_score_chunk_for_rerank(query, dict(c), destination), i) for i,c in enumerate(chunks)], key=lambda x: (-x[0], x[1]))
    output = [dict(chunks[i], rerank_score=score, rerank_method=method) for score,i in scored[:top_k]]
    return output, usage, method, hit, enabled and method == "rule_fallback"


def rerank_guide_chunks(query, matched_chunks, top_k, destination=None):
    chunks, usage, _, _, _ = rerank_detailed(query, matched_chunks, top_k, destination)
    return chunks, usage or {"prompt_tokens": 0, "completion_tokens": 0}


def _legacy_destination(query, destination):
    from app.rag.guide_catalog import known_destinations, canonical_destination
    if destination:
        return canonical_destination(destination)
    matches = [city for city in known_destinations() if city in query]
    return matches[0] if len(matches) == 1 else "unspecified_destination"


def retrieve_travel_guide_chunks(query, top_k=3, destination=None):
    from app.rag.knowledge_service import retrieve_knowledge, legacy_chunks
    from app.models.knowledge_retrieval import KnowledgeQuery
    result = retrieve_knowledge(KnowledgeQuery(query=query, destination=_legacy_destination(query, destination), top_k=top_k))
    zero = {"prompt_tokens": 0, "completion_tokens": 0}
    observe_result(result)
    return legacy_chunks(result), legacy_usage(result.metadata.rerank_usage), legacy_usage(result.metadata.embedding_usage)


def retrieve_travel_guide(query, top_k=3, destination=None):
    from app.rag.knowledge_service import retrieve_knowledge, format_context
    from app.models.knowledge_retrieval import KnowledgeQuery
    result = retrieve_knowledge(KnowledgeQuery(query=query, destination=_legacy_destination(query, destination), top_k=top_k))
    zero = {"prompt_tokens": 0, "completion_tokens": 0}
    observe_result(result)
    return format_context(result), legacy_usage(result.metadata.rerank_usage), legacy_usage(result.metadata.embedding_usage)
