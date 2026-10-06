"""Bounded static-knowledge contracts; no operational planning facts."""
import hashlib
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from app.rag.guide_catalog import canonical_destination

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class KnowledgeFilters(Contract):
    source_ids: tuple[Text, ...] = Field(default=(), max_length=10)

class KnowledgeQuery(Contract):
    query: Text = Field(max_length=2048)
    destination: Text
    top_k: int = Field(default=5, strict=True, ge=1, le=10)
    purpose: Text | None = Field(default=None, max_length=128)
    filters: KnowledgeFilters = Field(default_factory=KnowledgeFilters)

    @field_validator("destination")
    @classmethod
    def canonicalize(cls, value):
        return canonical_destination(value)

class KnowledgeChunk(Contract):
    chunk_id: Text
    source_id: Text
    source_path: Text
    title: Text
    heading_path: tuple[Text, ...] = Field(min_length=1)
    destination: Text
    text: Text
    content_hash: Text
    knowledge_kind: Literal["static_knowledge"] = "static_knowledge"
    rank: int = Field(ge=1, strict=True)
    vector_distance: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    rerank_score: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    rerank_method: str | None = None

    @model_validator(mode="after")
    def valid_body_hash(self):
        if hashlib.sha256(self.text.encode()).hexdigest() != self.content_hash:
            raise ValueError("Chunk content hash mismatch")
        return self

class StageEvent(Contract):
    stage: Literal["rewrite", "index", "embedding", "retrieval", "rerank", "validation"]
    code: Literal["unsupported_destination", "rewrite_failed", "embedding_failed", "vector_store_error", "rerank_failed", "invalid_response", "no_matching_knowledge", "index_unavailable", "vector_empty", "manifest_unavailable", "retrieval_config_error", "version_resolution_error", "usage_invalid"]
    detail: str = ""

class RetrievalMetadata(Contract):
    schema_version: str
    index_version: str
    retrieval_config_version: str
    original_query: str
    effective_query: str
    rewrite_status: str = "not_requested"
    retrieval_method: str = "none"
    reranker_used: bool = False
    rerank_method: str = "none"
    retrieval_cache_hit: bool = False
    rerank_cache_hit: bool = False
    candidate_k: int = Field(strict=True, ge=1, le=20)
    top_k: int = Field(strict=True, ge=1, le=10)
    rewrite_latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    retrieval_latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    rerank_latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    total_latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    rewrite_usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] | None = None
    embedding_usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] | None = None
    rerank_usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] | None = None

    @model_validator(mode="after")
    def candidate_policy(self):
        if self.candidate_k != max(self.top_k * 2, 6):
            raise ValueError("Candidate/output policy mismatch")
        return self

class KnowledgeResult(Contract):
    outcome: Literal["success", "no_data", "error"]
    chunks: tuple[KnowledgeChunk, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: RetrievalMetadata
    stage_events: tuple[StageEvent, ...] = ()
    degraded: bool = False

    @model_validator(mode="after")
    def coherent_outcome(self):
        if self.outcome == "success" and self.errors:
            raise ValueError("Success cannot contain terminal errors")
        if self.outcome == "success" and self.stage_events and not self.degraded:
            raise ValueError("Successful fallback must disclose degradation")
        if self.outcome != "success" and not self.errors:
            raise ValueError("Non-success requires a reason")
        if (self.outcome == "success") != bool(self.chunks):
            raise ValueError("Only successful results contain chunks")
        if len(self.chunks) > self.metadata.top_k or len({c.chunk_id for c in self.chunks}) != len(self.chunks):
            raise ValueError("Duplicate or excessive chunks")
        if [c.rank for c in self.chunks] != list(range(1, len(self.chunks)+1)):
            raise ValueError("Ranks must be contiguous")
        return self
