"""Build immutable snapshots, verify read-back, atomically publish a small pointer."""
import json
import os
import uuid
from pathlib import Path
from app.config import CHROMA_DB_DIR
from app.rag import versions

class IndexUnavailable(RuntimeError):
    pass

IDENTITY_FIELDS = ("index_version", "retrieval_config_version", "rag_schema_version", "manifest_hash", "embedding_config_id", "chunking_policy_version")

def snapshot_identity(manifest):
    return dict(index_version=versions.index_version(manifest),
                retrieval_config_version=versions.retrieval_config_version(),
                rag_schema_version=versions.RAG_SCHEMA_VERSION,
                manifest_hash=versions.stable_hash(sorted(manifest, key=lambda x: x["source_path"])),
                embedding_config_id=versions.stable_hash(versions.EMBEDDING_CONFIG),
                chunking_policy_version=versions.CHUNKING_POLICY["version"])


def collection_name(version):
    name = "rag_" + version[:24] + "_" + uuid.uuid4().hex[:8]
    if not 3 <= len(name) <= 63 or not all(c.isalnum() or c in "_-" for c in name):
        raise ValueError("Invalid Chroma collection name")
    return name


def open_active_index(directory=None, client=None):
    directory = Path(directory or CHROMA_DB_DIR)
    pointer = directory / "active_index.json"
    if not pointer.is_file():
        raise IndexUnavailable("No published index")
    try:
        active = json.loads(pointer.read_text(encoding="utf-8"))
        expected = snapshot_identity(versions.corpus_manifest())
        if any(active.get(key) != expected[key] for key in IDENTITY_FIELDS):
            raise IndexUnavailable("INDEX_VERSION_MISMATCH: pointer snapshot")
        if client is None:
            import chromadb
            client = chromadb.PersistentClient(path=str(directory))
        collection = client.get_collection(name=active["collection"])
        metadata = collection.metadata or {}
        if any(metadata.get(key) != active[key] for key in IDENTITY_FIELDS):
            raise IndexUnavailable("INDEX_VERSION_MISMATCH: collection binding")
        if type(active.get("count")) is not int or active["count"] <= 0 or collection.count() != active["count"]:
            raise IndexUnavailable("Published index count mismatch")
        return collection
    except IndexUnavailable:
        raise
    except Exception as exc:
        raise IndexUnavailable("Cannot open published index") from exc

def validate_chunks(chunks, manifest):
    ids = [c["id"] for c in chunks]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Empty or duplicate chunk IDs")
    sources = {item["source_path"]: item["destination"] for item in manifest}
    if {c["source_path"] for c in chunks} != set(sources):
        raise ValueError("Corpus sources not fully represented")
    for chunk in chunks:
        if any(not chunk.get(k) or not str(chunk[k]).strip() for k in
               ("source_path", "source_id", "title", "text", "content_hash", "destination", "heading_path")):
            raise ValueError("Blank provenance or text")
        if sources.get(chunk["source_path"]) != chunk["destination"]:
            raise ValueError("Invalid destination")

def rebuild_index(directory=None, client=None, embeddings=None):
    from app.rag import vector_db
    directory = Path(directory or CHROMA_DB_DIR)
    from app.rag.snapshot import load_snapshot
    snapshot = load_snapshot()
    manifest, chunks = snapshot.manifest, list(snapshot.chunks)
    if manifest != versions.corpus_manifest(vector_db.DATA_DIR):
        raise ValueError("Corpus changed while reading snapshot")
    validate_chunks(chunks, manifest)
    identity = snapshot_identity(manifest)
    version = identity["index_version"]
    if embeddings is None:
        embeddings = vector_db._build_embeddings()
    if embeddings is None:
        raise RuntimeError("Embedding capability unavailable")
    documents = [vector_db._build_document_text(c) for c in chunks]
    vectors = embeddings.embed_documents(documents)
    import math
    if len(vectors) != len(chunks) or not vectors or not vectors[0]:
        raise ValueError("Embedding count/dimension mismatch")
    dimension = len(vectors[0])
    if any(len(v) != dimension or any(type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("Invalid embeddings")
    directory.mkdir(parents=True, exist_ok=True)
    if client is None:
        import chromadb
        client = chromadb.PersistentClient(path=str(directory))
    # Unique build suffix permits concurrent builders without mutating an active collection.
    name = collection_name(version)
    collection = client.create_collection(name=name, metadata={**identity, "hnsw:space": "cosine",
                            "embedding_config": json.dumps(versions.EMBEDDING_CONFIG, sort_keys=True)})
    metadata = [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                 for k, v in c.items() if k not in ("id", "text")} for c in chunks]
    ids = [c["id"] for c in chunks]
    collection.add(ids=ids, documents=documents, metadatas=metadata, embeddings=vectors)
    saved = collection.get(include=["documents", "metadatas"])
    expected = {i: (d, m) for i, d, m in zip(ids, documents, metadata)}
    if collection.count() != len(chunks) or len(saved["ids"]) != len(set(saved["ids"])):
        raise ValueError("Index count/identity validation failed")
    actual = {i: (d, m) for i, d, m in zip(saved["ids"], saved["documents"], saved["metadatas"])}
    if actual != expected or any(collection.metadata.get(k) != identity[k] for k in IDENTITY_FIELDS) or collection.metadata["embedding_config"] != json.dumps(versions.EMBEDDING_CONFIG, sort_keys=True):
        raise ValueError("Index read-back validation failed")
    if manifest != versions.corpus_manifest(vector_db.DATA_DIR):
        raise ValueError("Corpus changed before publication")
    if snapshot_identity(manifest) != identity:
        raise ValueError("Configuration changed before publication")
    active = {**identity, "collection": name, "count": len(chunks)}
    temporary = directory / (".active-" + uuid.uuid4().hex + ".json")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(active, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "active_index.json")
    finally:
        temporary.unlink(missing_ok=True)
    return len(chunks)
