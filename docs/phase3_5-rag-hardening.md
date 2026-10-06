# Phase 3.5 — RAG Hardening

## Baseline and scope

Started from clean `feat/constraint-preserving-replanning` at
`b3062877409a58886e0fe8e8a5d233b971be6217`; implementation branch is
`feat/rag-hardening`. No dependency installation, commit, push, live provider calls,
or production index rebuild was performed. Frozen Phase 1–3C implementation and
evaluation labels remain unchanged. README is unchanged.

## Retrieval architecture

Before: destination request -> unconstrained LLM/rule rewrite -> unversioned text
cache -> `max(top_k*2,6)` vector candidates (or keyword fallback) -> optional
DashScope rerank (or deterministic rule ranking) -> formatted strings -> Planner.
The read path could create an empty collection, and repeated upserts retained
removed chunks. Rerank cache identity used source/title and stored a truncated list.

Now: `KnowledgeQuery` -> `retrieve_knowledge` -> explicit stage outcomes ->
`KnowledgeResult` -> bounded static/untrusted formatter -> existing Planner API.
`get_destination_guide_context`, `retrieve_travel_guide`, and
`retrieve_travel_guide_chunks` remain compatibility adapters. The Planner still
requests main, lodging, and restaurant context. Supplement chunks are deduplicated
by chunk ID and the combined context is capped. Phase 3C selector is not connected.

## Typed contract

`backend/app/models/knowledge_retrieval.py` defines frozen, extra-forbid models:

- `KnowledgeQuery`: nonblank query up to 2048 characters, canonical destination,
  strict integer `top_k` in 1..10, optional bounded purpose and source-ID allowlist.
  No arbitrary Chroma filter expression is accepted.
- `KnowledgeChunk`: ID, source ID/path, heading path, title, destination, text,
  content hash, contiguous rank, optional vector distance/rerank score/method,
  and `knowledge_kind=static_knowledge`.
- `RetrievalMetadata`: original/effective query, rewrite status, schema/index/config
  versions, candidate/output limits, retrieval/rerank method, separate cache hits,
  stage/total latencies, nullable provider usage.
- `KnowledgeResult`: success/no_data/error, chunks, warnings/errors, stage events,
  and degradation. Success requires nonempty unique chunks within the output limit.

Unknown usage is `null` in typed results and evaluators. Legacy Planner adapters
retain their dict signature: unknown usage is `{ "usage_known": false }` without
numeric token fields; measured counts include `usage_known=true`. Existing callers
using `.get(key, 0)` remain compatible; that arithmetic default is not measured usage.
Typed error/degraded outcomes also produce structured `retrieval_error` /
`retrieval_degraded` logs with codes, stages and index identity, never raw queries.

## Destination and limits

Minimal corpus aliases cover the six existing cities, including 北京 / 北京市 /
Beijing / surrounding whitespace. Unsupported or unrepresented corpus destinations
return `no_data/unsupported_destination`. There is no wildcard/all-cities fallback.
An old caller omitting destination can infer a single Chinese corpus city from its
query; ambiguous/unspecified requests return no data. Public output top_k rejects
0, negative, bool, string, and >10. Lower-level candidate helpers separately accept
strict 1..20 so internal recall can exceed the output limit.

Candidate policy remains exactly `max(top_k * 2, 6)`. The stale test expecting 8
for top_k=2 was corrected to 6; production policy was not changed. Its mock seam and
format assertions were updated for the typed adapter and required provenance header.
The Planner prompt test now asserts the new reference wording and trust boundary.
The unrelated day-edit mock failure was not repaired.

## Provenance and versions

`versions.py` defines `RAG_SCHEMA_VERSION=knowledge-v2`, `INDEX_VERSION`, and
`RETRIEVAL_CONFIG_VERSION`; request-time functions recompute snapshot identities.
No deployment timestamp participates in an identity.

Index identity hashes the sorted corpus manifest (relative data path, canonical
city, hash of each source's normalized indexed title/heading/text sequence),
chunking policy, and embedding model/endpoint/document
format/distance configuration. Corpus additions, deletion, text changes, or index
configuration changes require rebuilding. Chunk IDs separately hash source path,
heading hierarchy and normalized chunk text. Source IDs hash relative paths; chunk
content hashes use SHA-256 of text. Current corpus has 79 chunks across six cities.
Heading hierarchy is retained; long sections split at 1600 characters without
semantic overlap. Changing this policy changes the index version. CRLF/CR/LF and
trailing spaces yielding identical chunks share a version. Explicit manifest inputs
are sorted before hashing. Orphan consecutive `###` headings are siblings; only a
current `##` supplies a parent. No import-time corpus/config IO is required.

Vector results retain backend IDs and distances. Inconsistent result arrays fail
closed. `snapshot.py` builds a chunk-ID membership map from one verified snapshot.
Vector, keyword and cache results must match source ID/path, destination, title,
heading path, content hash and exact text in that map. Invented identities and
cross-bound ID/metadata pairs fail as invalid_response; bad cached identities miss.
Keyword fallback uses the same corpus chunks and provenance semantics. Duplicate
backend IDs keep the first occurrence, preserving backend rank; reranker duplicate
indices are rejected entirely rather than silently interpreted as valid ranking.

Retrieval configuration identity covers rules-file hash, rewrite/model policy,
candidate formula, canonicalization, reranker provider/model/policy, deterministic
ranking and formatter. Rules parsing is cached by file content, not forever by a
zero-argument loader. Future policy changes must bump their explicit policy version.

## Index rebuild and publication

`index_store.rebuild_index` reads a corpus manifest, chunks, verifies the manifest
has not changed, embeds this snapshot and creates a fresh collection named
`rag_<24-character-index-prefix>_<8-character-build-suffix>` (37 characters).
This respects declared Chroma 0.5/0.6 constraints (3..63), verified by an enforcing
fake client without installation. The unique build suffix permits concurrent
build attempts; it is not the deterministic snapshot identity.

Before publication it verifies nonempty/unique IDs, expected sources and destination
metadata, nonblank provenance/body, embedding count/dimension/finite values, exact
read-back IDs/documents/metadata, embedding config, and unchanged corpus manifest.
A temporary pointer is flushed/fsynced and atomically replaced as
`CHROMA_DB_DIR/active_index.json`. Any pre-publication failure leaves the old pointer
untouched. Old collections are not mutated. Removed chunks are absent from the new
collection. Retired/orphan collections are retained; automated garbage collection
is deliberately not implemented.

Both pointer and collection metadata contain full index_version, retrieval_config_version,
rag_schema_version, manifest_hash, embedding_config_id and chunking_policy_version.
Reads compare all six fields against the expected snapshot and against each other;
same-count old collections are rejected with INDEX_VERSION_MISMATCH inside
index_unavailable. Config/schema changes conservatively require republishing.

Reads require the published pointer, current snapshot/config and expected count,
then call `get_collection`, never `get_or_create_collection`. Missing/stale pointer
is `index_unavailable`. Existing fixed `travel_guides` collections are not adopted
silently. Run `python backend/scripts/ingest_data.py` explicitly with configured
embedding/Chroma capability to build/publish the new index; this command performs
real embedding calls. It was not run in this implementation session.

## Caches

Retrieval cache key includes canonical destination, original and effective query,
top_k, purpose, source filters, schema/index/retrieval config, ablation mode, rewrite
status, and fallback policy. Cached typed results are validated against request,
versions, limits, city, rank uniqueness, and text hashes. Transient degraded results
are not written to the outer cache. This avoids persisting a failed provider's
fallback response across recovery.

Rerank cache key includes effective query, ordered chunk IDs/content hashes/text
fingerprints, reranker model, retrieval config, and index version. The provider is
asked to rank the full candidate list; only a validated complete ranking is cached.
Each request then slices to top_k. Thus 2 -> 3 cannot reuse a two-item partial list.
Cache payloads include the ordered candidate ID/hash identities and undergo the
same strict index/score validation as provider output. Final reranked chunks undergo
snapshot validation again.
Cache hits report nullable usage for calls not made in that request.

Rewrite still runs before the retrieval cache. Metadata includes the current
rewrite latency/usage even on a retrieval cache hit; no avoided-rewrite-cost claim
is made. Latency fields are per request; total includes rewrite plus retrieval path.

## Rewrite and rerank boundaries

Rewrite output is bounded at 1024 characters and must retain canonical destination
and deterministically identifiable corpus entities (numbered attraction headings, bracketed names,
and matching name prefixes). This is a small matcher, not NER. Empty, timeout,
provider error, oversized, destination-omitting or entity-dropping output fails or
is rejected. Fallback contains canonical city, the complete bounded original free
text, and bounded structured/rule expansions (effective fallback cap 4096).
Original/effective query and fallback status stay visible. This does not guarantee
recognition of every synonym or named entity. Generic section headings such as
经济实惠/住宿/推荐 are excluded. 西安城墙 has no reliable entity entry in the current
corpus and remains an explicit coverage limitation; no city dictionary was added.
Malformed usage strings/lists/negative/bool/non-finite counts become null with a
usage_invalid log and do not invalidate an otherwise valid rewrite.

Ranks and counts are strict integers. Latencies are finite/nonnegative; usage counts
are nonnegative strict integers. Chunk models verify text hashes. Successful results
cannot contain terminal errors or hide fallback events behind degraded=false.
Rule ranking scores remain signed finite numbers, preserving the existing algorithm.

DashScope response index must be a genuine integer (not bool/string), unique and
within candidate bounds. Scores, if present, must be finite numbers; missing scores
use neutral 0. Invalid/partial provider rankings fall back to deterministic rule
ranking. Provider timeout/error/malformed output yields `rerank_failed` with
`rerank_method=rule_fallback`. Optional provider unavailability has the same explicit
degradation. Keyword baseline explicitly disables the provider and uses rule ranking.
Provider logs omit full queries, user notes, response bodies and exception messages.

## Failure policy

Stable codes include unsupported_destination, rewrite_failed, embedding_failed,
vector_store_error, rerank_failed, invalid_response, no_matching_knowledge and
index_unavailable; vector_empty separately identifies successful empty recall.
Manifest/entity-map, version-resolution and rules/config read failures are represented
as manifest_unavailable, version_resolution_error or retrieval_config_error, rather
than raw OSError or no_data. Unresolved version fields are explicit `unresolved`.

- Unknown corpus city: no_data/unsupported_destination; no other-city fallback.
- Supported corpus, ordinary empty keyword/vector results: no_data/no_matching_knowledge.
- Default mode allows keyword fallback after empty vector recall or vector/index/
  embedding failure. Successful fallback is success + degraded + explicit events.
- If an infrastructure failure remains with no usable fallback result, return error
  with its original code. This is not claimed as proof of corpus irrelevance.
- With fallback disabled (vector ablations), infrastructure failure is an error.
- Reranker failure after usable recall can remain success with rule ranking/events.
- Invalid final provenance fails closed as invalid_response.

## Static-knowledge trust boundary

Formatter includes chunk ID, source, title, destination, static_knowledge and
untrusted-reference notice. Single formatted chunk <=2000 characters; combined
Planner context <=12000 characters. Retrieved content remains human-readable data,
including an adversarial `Ignore previous instructions...` fixture.

Planner system prompt says retrieved content cannot override instructions, alter
constraints or grant tool permissions. Merchant names are reference names, not
verified real-time claims. Ticket prices, hours, closures, traffic, availability
and weather are not promoted to PlanningFacts. No new facts conversion or tool
permission path was added. This is an architectural trust boundary, not proof that
an LLM can never follow a prompt injection.

## Evaluation v2

`backend/eval/rag_eval_cases_v2.json` contains 60 source-read annotations frozen
before retrieval: exact lexical 12, semantic preference 12, city disambiguation 10,
multi-chunk evidence 8, rewrite 6, failure/unsupported 6, noisy/adversarial 6.
Labels reference stable chunk IDs, with independent evidence groups for multi-chunk
requests and explicit outcomes/error codes for no-result cases. Detailed entity
chunks are required; broad itinerary mentions do not satisfy that annotation.
The evaluator also supports source IDs and acceptable alternative chunk sets.

Dataset binds `corpus_manifest_version`; evaluator refuses mismatch rather than
silently regenerating labels. The full dataset is preflighted before any retrieval: unique
case IDs, known chunk/source IDs, nonempty valid acceptable sets/evidence groups,
explicit no-result semantics and positive labels for quality categories. Missing IDs
or emptied semantic labels abort with zero retrieval calls, so denominators cannot
silently shrink. Replay functions capture only the replay response payload, not case
objects. Per-case output retains typed metadata, errors, stage events, degradation,
cache hits, versions, latencies and nullable usage; explicit live output adds provider
and model identifiers without keys/endpoints. Evaluation never derives relevance from retrieved
results. These are authored, source-informed tests, not a blind production sample.
Original 18 labels and all frozen earlier-phase datasets remain unchanged.

Run `python backend/scripts/evaluate_rag_v2.py` for deterministic offline
`keyword_baseline`. It disables retrieval/rerank caches and blocks embedding,
rewrite-client and reranker calls even when a key is configured. Six rewrite cases
use explicit fixed replay outputs to measure boundary enforcement, not LLM quality.
No replay vector ranking is invented.

Modes `vector_only`, `vector_rewrite`, `vector_rerank`, and
`vector_rewrite_rerank` require explicit `--live`; they never silently fall back to
keyword results. Example: `python backend/scripts/evaluate_rag_v2.py --live --mode
vector_rewrite_rerank` (write the command on one line). These require a valid
published index and working providers. No live ablation was run.

Metrics use per-case relevant-ID recall and first relevant raw rank. Duplicate
results occupy rank but cannot earn duplicate recall credit. Alternative sets use
the best covered acceptable set. No-result cases are excluded from relevance metric
denominators; zero denominators return null. Contamination/duplicate rates use actual
returned chunk counts. Evidence coverage measures satisfied groups and complete
coverage separately. Metric formulas have independent unit tests.

### Measured offline results

| Metric | Result |
|---|---:|
| Recall@1 (54 positive-label cases) | 62.96% |
| Recall@3 | 72.22% |
| Recall@5 / @6 | 74.07% / 74.07% |
| Hit@1 / @3 / @5 / @6 | 70.37% / 72.22% / 74.07% / 74.07% |
| MRR | 0.7167 |
| Destination contamination / duplicate rate | 0 / 0 (125 returned chunks) |
| No-result correctness / failure classification | 6/6 / 6/6 |
| Multi-evidence group / complete coverage | 100% / 8/8 |
| Rewrite replay status correctness | 6/6 |

| Category | Cases | Recall@6 | MRR |
|---|---:|---:|---:|
| Exact lexical | 12 | 100% | 1.0000 |
| Semantic preference | 12 | 0% | 0.0000 |
| Destination disambiguation | 10 | 100% | 1.0000 |
| Multi-chunk evidence | 8 | 100% | 1.0000 |
| Query rewrite | 6 | 66.67% | 0.4500 |
| Failure/unsupported | 6 | N/A | N/A |
| Adversarial/noisy | 6 | 100% | 1.0000 |

Semantic queries expose the current substring keyword fallback's limitations.
Preserved natural-language entities alone do not guarantee strong lexical recall.
These numbers do not measure actual vector or live reranker performance, improvement
in travel quality, or production retrieval accuracy.

### Legacy regression set

`python backend/scripts/evaluate_rag_retrieval.py` now defaults to offline
keyword/rule evaluation; real providers require `--live`. Timing includes rewrite,
noise denominator uses actual returned chunks, zero-case division is guarded, and
unknown usage is null. Results: Top1 9/18, Top5 18/18, required keywords 52/58,
MRR 0.6991, noise 0, cross-destination pollution 0. Broad title/keyword labels make
this a legacy regression set, not the primary quality benchmark.

## Deliberately deferred

No BM25 dependency or hybrid retrieval was introduced: lexical baseline succeeds
on this exact-entity slice, and live vector comparison is absent. RRF remains
unjustified without complementary retrieval measurements. BGE-M3/embedding changes
would confound the snapshot and evaluation work. The existing optional reranker
model remains unchanged. RAG MCP is optional future work for a real external consumer;
current consumers can reuse the typed Python adapter. No Phase 3C selector RAG
integration, autonomous agent, framework migration, or dynamic-fact system was added.

## Verification

RAG targeted tests after this hardening: 221 passed. The new Phase 3.5.1 file adds
96 expanded test instances; the previous Phase 3.5 suite contains 125. These are
parameterized instances, not 96 independently sampled travel scenarios.
Coverage includes index identity/build/publication failure, current-only snapshots,
query validation/aliases, both caches, rewrite rejection/fallback, reranker malformed
responses, vector IDs/distances/body hashes, duplicates, context bounds, untrusted
data, metrics, offline enforcement and corpus-version mismatch.

Frozen targeted suites: 3C 200, 3B 195, 3A 83, 2.5 167, 2 200, 1 156 — all passed.
Selector 36, replanning 50, MCP 28, route 48 and planning 67 evaluators all completed
with no mismatches. Full backend after hardening: 1299 passed, 1 known failure.
Remaining known failure is
`test_trip_planner_agent.py::test_generate_day_edit_draft_accepts_nested_day_shape`:
its mock uses `json.dumps` without importing json. This pre-existing unrelated issue
was explicitly left unchanged.

No real Chroma/provider SDK compatibility or index deployment was exercised. Index
tests use isolated fake clients and temporary directories. Existing SDK availability
and deployment credentials must be verified before explicit live evaluation.

Recommended commit message (not committed):
`feat(rag): harden retrieval provenance and evaluation`.

## Phase 3.5.1 counterexample re-check

| Audit counterexample | Hardened behavior |
|---|---|
| 81-character collection name | 37 characters; enforcing fake client accepts |
| Current pointer -> same-count old collection | Identity mismatch rejected |
| Real body/hash + invented ID/source | invalid_response |
| Corrupt retrieval/rerank cache identities | Cache miss/rule fallback; never accepted identity |
| Missing relevant ID | Preflight abort, retrieval calls=0 |
| Empty semantic labels | Preflight abort, denominator cannot silently shrink |
| Config/version/manifest OSError | Typed infrastructure error |
| Malformed rewrite usage | Accepted valid rewrite with null usage; no crash |
| LF vs CRLF/CR, same logical chunks | Same index version |
| Consecutive orphan `###` | Sibling heading paths |
| 经济实惠 as preference | No mandatory entity; semantic rewording accepted |

Only the v2 dataset's corpus_manifest_version binding was migrated from raw-source
identity to logical-source identity. All 60 case objects, relevance IDs, evidence
groups and replay payloads remain unchanged; legacy labels are unchanged. The offline
metrics above remained identical after migration. No real persistent index was rebuilt.

Snapshot identifiers for the hardened run:

```text
index: 93ec1ea2d9a1a9f4c1b68b70a099d7d245ca392ddcde8123b7d0f8df089a5734
retrieval_config: 72f9910a28686ca4f4f57e981f2b2969bfd24d012821dad65ae5d7e295c3d5ce
corpus_manifest: 92f4df063ba3d9150da3bf96a173388ccc9e6f1f15cc20740a25b4de84d2dc6d
```

Hardening commit suggestion (not committed):
`fix(rag): harden index provenance and evaluation integrity`.
