"""One immutable-by-convention corpus snapshot for identity and retrieval validation."""
import hashlib
from dataclasses import dataclass
from app.rag import versions, vector_db

PROVENANCE_FIELDS = ("source_id", "source_path", "destination", "title", "heading_path", "content_hash")

@dataclass(frozen=True)
class Snapshot:
    index_version: str
    manifest: list
    chunks: tuple
    members: dict

    def validate(self, chunk):
        value = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
        identity = value.get("chunk_id", value.get("id"))
        expected = self.members.get(identity)
        if expected is None:
            raise ValueError("provenance_mismatch: unknown chunk")
        for key in PROVENANCE_FIELDS:
            actual = value.get(key)
            wanted = expected[key]
            if key == "heading_path":
                actual, wanted = tuple(actual or ()), tuple(wanted)
            if actual != wanted:
                raise ValueError("provenance_mismatch: " + key)
        if value.get("text") != expected["text"] or hashlib.sha256(value["text"].encode()).hexdigest() != expected["content_hash"]:
            raise ValueError("provenance_mismatch: body")


def load_snapshot():
    manifest = versions.corpus_manifest(vector_db.DATA_DIR)
    version = versions.index_version(manifest)
    chunks = vector_db.load_guide_chunks()
    if manifest != versions.corpus_manifest(vector_db.DATA_DIR):
        raise ValueError("Corpus changed while loading snapshot")
    sources = {m["source_path"]: m for m in manifest}
    grouped, members = {}, {}
    for c in chunks:
        source = sources.get(c["source_path"])
        if source is None or source["destination"] != c["destination"]:
            raise ValueError("Invalid snapshot source")
        if c["source_id"] != "source_" + versions.stable_hash(c["source_path"]):
            raise ValueError("Invalid snapshot source identity")
        if c["id"] != vector_db._build_chunk_id(c["source_path"], "/".join(c["heading_path"]), c["text"]):
            raise ValueError("Invalid snapshot chunk identity")
        if hashlib.sha256(c["text"].encode()).hexdigest() != c["content_hash"] or c["id"] in members:
            raise ValueError("Invalid or duplicate snapshot chunk")
        members[c["id"]] = c
        grouped.setdefault(c["source_path"], []).append({k: c[k] for k in ("title", "heading_path", "text")})
    for path, source in sources.items():
        if versions.stable_hash(grouped.get(path, [])) != source["content_hash"]:
            raise ValueError("Snapshot differs from manifest")
    return Snapshot(version, manifest, tuple(chunks), members)
