# Claim safety matrix

Evidence baseline: Phase 3.5 `dcee713`, with the uncommitted Phase 4 evaluation additions. See [measured results](final-benchmark.md) and [source boundaries](final-architecture.md). Claims below concern implemented behavior and fixed offline evidence, not external validity.

## Safe to claim

| Claim | Evidence | Precise wording |
|---|---|---|
| Deterministic constraint validation | `plan_validator.py`; 67-case evaluator | Pure, repeatable validation for supported constraints with explicit unknown findings. |
| Server-grounded POI/route evidence | `tool_adapter.py`, `planning_fact_service.py`; 48 route cases | Typed observations with identity, units, freshness and failure metadata. |
| Optimistic concurrency | `authoritative_state.py`; authoritative-state tests and E2E race | Conditional versioned writes prevent a stale request from overwriting the current revision. |
| Bounded selection | `candidate_selector.py`, `replanning_selection.py`; 36 selector cases | An LLM output can select only an authorized ID; deterministic code retains mutation authority. |
| Provenance-aware RAG | `knowledge_service.py`, `snapshot.py`, `index_store.py`; RAG tests | Returned/cached chunks must match the current logical corpus identity and content. |
| Optional MCP gateway | `app/mcp/server.py`; 28 protocol cases | Route/POI tools are reusable through a controlled optional MCP interface. |

## Claim with qualifier

| Claim | Required qualifier |
|---|---|
| Route feasibility | Driving estimate for discoverable timed pairs; no future traffic, whole-trip, weather or opening-hours guarantee. |
| Constraint-preserving partial replanning | One attraction operation on one day, with allowed derived budget/transport changes; not arbitrary natural-language whole-trip editing. |
| Valid-only commits | Applies to replanning. Initial version-1 drafts may be invalid or unverifiable. |
| Failure preservation 11/11 | Fixed E2E controls: 6 failed replans, 3 route validations and 2 knowledge no-data scenarios. Concurrent winner is the preservation reference. |
| No unauthorized mutations observed | Zero in 24 fixed scenarios, using full row/projection checks; not a proof over all inputs or all deployments. |
| LLM safety | Enforced output authority, one-shot call and checks under replay; not measured live model quality or universal prompt-injection resistance. |
| Cache reuse | Version-bound RAG caches and eligible route evidence; candidate store is single-process. No demonstrated production speedup. |
| Concurrency | SQLite/version-CAS contract and controlled races; no distributed or multi-database reliability claim. |
| RAG evaluation | 60 fixed source-labeled cases; positive-label metric denominator 54; keyword semantic-preference Recall@6 is 0/12. No demonstrated vector superiority. |
| All backend tests pass | 1308 expanded instances in the recorded existing Python environment; pytest version differs from declared requirement. Not a clean-install guarantee. |
| End-to-end | In-process HTTP generation/replanning plus route/RAG service scenarios. No browser or live provider run. |

## Do not claim

Production accuracy; real-world itinerary guarantees; live pricing or inventory; real-time traffic prediction/optimization; autonomous multi-agent planning; full production authentication/security; distributed production readiness; semantic retrieval superiority; BM25/RRF hybrid search; BGE-M3 embeddings; a measured planning improvement percentage without a defined baseline.

Do not sum unrelated evaluator datasets into one accuracy number. Do not call 1308 parameterized instances independent travel scenarios. Do not reuse historical README latency/quality improvements as current comparable experiments.
