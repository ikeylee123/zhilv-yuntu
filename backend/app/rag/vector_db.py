from __future__ import annotations

import re
import math
import json

import httpx

from app.config import (
    BACKEND_DIR,
    CHROMA_DB_DIR,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
)
from app.rag.guide_catalog import destination_for_guide


DATA_DIR = BACKEND_DIR / "data"


def _split_markdown_into_chunks(markdown_text: str, source_name: str) -> list[dict]:
    from app.rag.versions import CHUNKING_POLICY
    chunks, lines, headings = [], [], []
    parent = None
    def flush():
        text = "\n".join(lines).strip()
        title = headings[-1] if headings else "文档开头"
        for offset in range(0, len(text), CHUNKING_POLICY["max_chars"]):
            body = text[offset:offset + CHUNKING_POLICY["max_chars"]].strip()
            if body:
                chunks.append({"title": title, "text": body, "source": source_name,
                               "heading_path": list(headings) or [title]})
        lines.clear()
    for line in markdown_text.splitlines():
        stripped = line.strip()
        match = re.match(r"^(#{2,3})\s+(.+)$", stripped)
        if match:
            flush()
            if len(match[1]) == 2:
                parent = match[2]
                headings[:] = [parent]
            else:
                headings[:] = ([parent] if parent else []) + [match[2]]
        elif stripped:
            lines.append(stripped)
    flush()
    return chunks


def _build_chunk_id(source: str, title: str, text: str) -> str:
    from app.rag.versions import stable_hash
    return "chunk_" + stable_hash([source, title, text.strip()])


def _build_document_text(chunk: dict) -> str:
    return f"{chunk['title']}\n{chunk['text']}"


def load_guide_chunks() -> list[dict]:
    from app.rag.versions import stable_hash
    import hashlib
    chunks = []
    for guide_file in sorted(DATA_DIR.glob("*.md*")):
        destination = destination_for_guide(guide_file.name)
        if destination is None:
            raise ValueError("Unregistered corpus source: " + guide_file.name)
        for chunk in _split_markdown_into_chunks(guide_file.read_text(encoding="utf-8"), guide_file.name):
            path = "data/" + guide_file.name
            chunk.update(source_path=path, source_id="source_" + stable_hash(path),
                         destination=destination, knowledge_kind="static_knowledge",
                         content_hash=hashlib.sha256(chunk["text"].encode()).hexdigest())
            chunk["id"] = _build_chunk_id(path, "/".join(chunk["heading_path"]), chunk["text"])
            chunks.append(chunk)
    return chunks


def _extract_keywords(query: str) -> list[str]:
    """把查询语句切成简单关键词，用于回退匹配。"""
    raw_keywords = re.split(r"[\s,，。；;、]+", query)
    return [keyword.strip() for keyword in raw_keywords if keyword.strip()]


def _score_chunk(query: str, chunk_text: str) -> int:
    """按关键词出现次数给片段打分。"""
    keywords = _extract_keywords(query)
    return sum(1 for keyword in keywords if keyword in chunk_text)


def _validate_candidate_query(top_k, destination):
    from app.rag.guide_catalog import canonical_destination
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("Candidate count must be a strict integer in 1..20")
    return canonical_destination(destination) if destination else None


def _search_guide_chunks_by_keywords(
    query: str, top_k: int = 3, destination: str | None = None, *, chunks=None
) -> list[dict[str, str]]:
    """回退方案：使用关键词匹配本地攻略片段。"""
    destination = _validate_candidate_query(top_k, destination)
    scored_chunks: list[tuple[int, dict[str, str]]] = []
    for chunk in (load_guide_chunks() if chunks is None else chunks):
        if destination and chunk.get("destination") != destination:
            continue
        score = _score_chunk(query, _build_document_text(chunk))
        if score > 0:
            scored_chunks.append((score, chunk))

    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored_chunks[:top_k]]


def _build_embeddings():
    """创建 embedding 模型实例。"""
    if not LLM_API_KEY:
        return None

    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError:
        return None

    try:
        return OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL or None,
            chunk_size=EMBEDDING_BATCH_SIZE,
            check_embedding_ctx_length=False,
        )
    except TypeError:
        return OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            openai_api_key=LLM_API_KEY,
            openai_api_base=LLM_BASE_URL or None,
            chunk_size=EMBEDDING_BATCH_SIZE,
            check_embedding_ctx_length=False,
        )


def _extract_embedding_token_usage(response_data: dict) -> dict[str, int] | None:
    """Only official counts are known; absent/malformed telemetry stays null."""
    from app.rag.telemetry import parse_usage
    usage = parse_usage(response_data.get("usage"))
    if usage is None:
        return None
    for key in ("prompt_tokens", "input_tokens", "input_token_count", "total_tokens", "total_token_count"):
        if key in usage:
            return {"prompt_tokens": usage[key], "completion_tokens": 0}
    return None


def _embed_query_with_usage(query: str) -> tuple[list[float] | None, dict[str, int]]:
    """在线 query embedding：优先直接调接口拿官方 usage，失败时回退 LangChain；未知 usage 为 null。"""
    empty_usage = None
    if not LLM_API_KEY:
        return None, empty_usage

    base_url = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{base_url}/embeddings"
    payload = {
        "model": EMBEDDING_MODEL,
        "input": query,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(endpoint, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data") or []
            if items and "embedding" in items[0]:
                usage = _extract_embedding_token_usage(data)
                print(
                    "[embedding] query embedding token: "
                    f"usage_known={usage is not None}, source=api"
                )
                return items[0]["embedding"], usage
            print(f"[embedding] embeddings response missing vector: [redacted]")
        else:
            print(
                "[embedding] embeddings API failed: "
                f"status_code={response.status_code}, response=[redacted]"
            )
    except Exception as exc:
        print(f"[embedding] embeddings API failed: {type(exc).__name__}")

    embeddings = _build_embeddings()
    if embeddings is None:
        return None, empty_usage
    print("[embedding] fallback to LangChain embed_query; official token usage unavailable")
    return embeddings.embed_query(query), empty_usage


def _get_chroma_collection():
    """Only open a verified, published index. Never create a collection on reads."""
    from app.rag.index_store import open_active_index
    return open_active_index()


def ingest_guide_chunks_to_chroma() -> int:
    from app.rag.index_store import rebuild_index
    return rebuild_index()


def _search_guide_chunks_by_chroma(
    query: str, top_k: int = 3, destination: str | None = None
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """优先使用 Chroma 做向量检索，并返回在线 query embedding token。"""
    destination = _validate_candidate_query(top_k, destination)
    collection = _get_chroma_collection()
    empty_usage = None

    if collection is None:
        return [], empty_usage
    if collection.count() == 0:
        return [], empty_usage

    try:
        query_embedding, embedding_usage = _embed_query_with_usage(query)
    except Exception as exc:
        from app.rag.knowledge_service import EmbeddingFailed
        raise EmbeddingFailed("Query embedding failed") from exc
    if query_embedding is None:
        from app.rag.knowledge_service import EmbeddingFailed
        raise EmbeddingFailed("Query embedding unavailable")
    if not isinstance(query_embedding, list) or not query_embedding or any(type(v) not in (int, float) or not math.isfinite(v) for v in query_embedding):
        from app.rag.knowledge_service import EmbeddingFailed
        raise EmbeddingFailed("Invalid embedding vector")
    query_args = {
        "query_embeddings": [query_embedding],
        "n_results": min(top_k, collection.count()),
        "include": ["documents", "metadatas", "distances"],
    }
    if destination:
        query_args["where"] = {"destination": destination}
    result = collection.query(**query_args)

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    ids = result.get("ids", [[]])[0]
    distances = result.get("distances", [[]])[0]
    if not len(documents) == len(metadatas) == len(ids) == len(distances):
        from app.rag.knowledge_service import InvalidRetrievalResponse
        raise InvalidRetrievalResponse("Inconsistent vector response")
    matched_chunks: list[dict[str, str]] = []
    for position, (document, metadata) in enumerate(zip(documents, metadatas)):
        title = metadata.get("title", "未命名片段") if metadata else "未命名片段"
        source = metadata.get("source", "未知来源") if metadata else "未知来源"
        chunk_destination = metadata.get("destination", "") if metadata else ""
        text = document.split("\n", 1)[1] if "\n" in document else document
        matched_chunks.append(
            {
                "id": ids[position],
                "source_path": metadata.get("source_path", "data/" + source),
                "source_id": metadata.get("source_id", ""),
                "heading_path": json.loads(metadata.get("heading_path", "null")) or [title],
                "content_hash": metadata.get("content_hash", ""),
                "vector_distance": result["distances"][0][position] if result.get("distances") else None,
                "title": title,
                "text": text,
                "source": source,
                "destination": chunk_destination,
            }
        )

    return matched_chunks, embedding_usage


def search_guide_chunks_with_usage(
    query: str, top_k: int = 3, destination: str | None = None
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """
    从本地攻略片段里找最相关的 top_k 条结果。

    优先走 Chroma 向量检索；如果当前环境还没准备好，再回退到关键词检索。
    """
    destination = _validate_candidate_query(top_k, destination)
    embedding_usage = None
    try:
        chroma_results, embedding_usage = _search_guide_chunks_by_chroma(query, top_k, destination)
        if chroma_results:
            return chroma_results, embedding_usage
    except Exception:
        import logging
        logging.getLogger(__name__).warning("legacy vector retrieval degraded to keyword fallback")
    return _search_guide_chunks_by_keywords(query, top_k, destination), embedding_usage


def search_guide_chunks(
    query: str, top_k: int = 3, destination: str | None = None
) -> list[dict[str, str]]:
    """兼容旧调用：只返回检索片段，不返回 token usage。"""
    chunks, _ = search_guide_chunks_with_usage(
        query=query, top_k=top_k, destination=destination
    )
    return chunks
