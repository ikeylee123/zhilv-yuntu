"""Deterministic snapshot identities, independent of deployment time."""
import hashlib
import json
from app.config import BACKEND_DIR, EMBEDDING_MODEL, LLM_BASE_URL, RERANK_MODEL, LLM_MODEL
from app.rag.guide_catalog import destination_for_guide

RAG_SCHEMA_VERSION = "knowledge-v2"
CHUNKING_POLICY = {"version": "heading-path-v2", "max_chars": 1600, "overlap": 0}
EMBEDDING_CONFIG = {"model": EMBEDDING_MODEL, "endpoint": LLM_BASE_URL or "https://api.openai.com/v1", "document": "title-newline-text-v1", "distance": "cosine"}

def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def logical_source_hash(text, source):
    from app.rag.vector_db import _split_markdown_into_chunks
    # Hash exactly the logical indexed content, ignoring newline/space noise.
    chunks = _split_markdown_into_chunks(text, source)
    return stable_hash([{k: c[k] for k in ("title", "heading_path", "text")} for c in chunks])


def corpus_manifest(data_dir=None):
    from app.rag.vector_db import DATA_DIR
    folder = data_dir or DATA_DIR
    result = []
    for path in sorted(folder.glob("*.md*")):
        destination = destination_for_guide(path.name)
        if destination is None:
            raise ValueError("Unregistered corpus source: " + path.name)
        result.append({"source_path": "data/" + path.name, "destination": destination,
                       "content_hash": logical_source_hash(path.read_text(encoding="utf-8"), path.name)})
    return result

def index_version(manifest=None, embedding=None, chunking=None):
    manifest = corpus_manifest() if manifest is None else manifest
    if not isinstance(manifest, list) or any(not isinstance(x, dict) or
            any(not isinstance(x.get(k), str) or not x[k] for k in ("source_path", "destination", "content_hash")) for x in manifest):
        raise ValueError("Malformed corpus manifest")
    if len({x["source_path"] for x in manifest}) != len(manifest):
        raise ValueError("Duplicate manifest sources")
    return stable_hash({"manifest": sorted(manifest, key=lambda x: x["source_path"]),
                        "embedding": EMBEDDING_CONFIG if embedding is None else embedding,
                        "chunking": CHUNKING_POLICY if chunking is None else chunking})

def retrieval_config_version():
    rules = (BACKEND_DIR / "data/retrieval_rules.json").read_bytes()
    parsed = json.loads(rules)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("global_rules"), list) or not isinstance(parsed.get("destinations"), dict):
        raise ValueError("Malformed retrieval rules")
    return stable_hash({"rewrite": "bounded-poi-entities-v2", "rewrite_model": LLM_MODEL,
        "rules_hash": hashlib.sha256(rules).hexdigest(), "candidate": "max(top_k*2,6)",
        "canonicalization": "corpus-alias-v1", "rerank_provider": "dashscope-compatible-v1",
        "rerank_model": RERANK_MODEL, "rerank_config": "full-list-identity-v2-timeout30",
        "fallback": "existing-rule-v1", "formatter": "static-untrusted-bounded-v1"})

def __getattr__(name):
    # Resolve inside the caller's typed boundary, never perform corpus IO on import.
    if name == "INDEX_VERSION":
        return index_version()
    if name == "RETRIEVAL_CONFIG_VERSION":
        return retrieval_config_version()
    raise AttributeError(name)

