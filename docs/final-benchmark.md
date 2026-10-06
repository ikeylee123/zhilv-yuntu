# Final benchmark report — Phase 4

## Baseline and environment

- Starting branch: `feat/rag-hardening`; working tree clean.
- Phase 3.5 and starting HEAD: `dcee713c065bd75251f81fbb1784e6d5b4d1f5f8`.
- Review branch: `feat/project-benchmark-and-docs`; Phase 4 changes are uncommitted.
- Recorded execution date: 2026-10-06 UTC (local review may be 2026-10-07 Australia/Sydney).
- Windows, Python 3.13.5; existing environment: FastAPI 0.142.2, Pydantic 2.13.5, SQLAlchemy 2.1.3, MCP SDK 2.3.0, pytest 9.1.1.
- Mode: offline; providers: mock/replay; deterministic evidence clock `2026-10-06T00:00:00+00:00`.
- Temporary SQLite and cache stubs; fixed checked-in corpus. E2E uses in-process HTTP. The frozen MCP evaluator uses real loopback Streamable HTTP with replay observations.
- No dependency installation, live Amap/LLM/embedding/reranker call, persistent Chroma build, Docker build or browser test was performed.

**Environment qualification:** pytest 9.1.1 is outside the declared `pytest>=8,<9` range. The tests passed in this pre-existing environment; a clean install under declared constraints remains unverified. Python 3.11 in the Dockerfile was not exercised. Dependency files were intentionally not changed.

[Per-scenario evidence](phase4-e2e-results.json), [frozen evaluator results](phase4-frozen-results.json), [full regression/source hashes](phase4-regression-results.json). Their commit fields identify the frozen baseline, not a commit containing these new files. New evaluator/data hashes and the dirty flag identify the Phase 4 worktree being evaluated.

## Cross-layer design

24 externally labeled scenarios: 5 initial planning, 4 route validation, 7 partial replanning, 4 selector, 4 knowledge. The extra replanning case verifies exact-detail grounding conflict. The dataset is [end_to_end_scenarios.json](../backend/eval/end_to_end_scenarios.json); the runner is [evaluate_end_to_end.py](../backend/scripts/evaluate_end_to_end.py).

Initial cases traverse the real FastAPI generate endpoint, local city resolution, real planner output parser, deterministic itinerary assembly, constraint/route validation and managed persistence. The LLM boundary returns fixed JSON; the knowledge boundary uses real corpus keyword retrieval. Dynamic-city initial generation and the browser are not covered by these five scenarios.

Replan/selector cases use HTTP candidate/replan endpoints, a typed replay provider, real server-authorized candidate preparation, exact POI detail grounding, deterministic patch/fact services, full validation and actual SQLite CAS. They begin from explicit server-created multi-attraction fixtures, not model-generated plans. Route scenarios exercise collector/validator service boundaries; knowledge scenarios exercise the typed retrieval service. This is a cross-layer integration suite, not one uninterrupted live user journey.

The current initial generator generally creates one timed attraction per day. Its route-requested scenario intentionally returns unverifiable. Initial draft persistence is not valid-only and is excluded from failed-request preservation denominators. Only the replan commit boundary is valid-only.

## Metrics and denominators

| Metric | Result | Measurement |
|---|---:|---|
| Scenario checks | 24/24 passed; 0 failed | Decision, status, required finding/reason and applicable invariants |
| Decision accuracy | 24/24 | Actual decision versus external scenario label |
| Constraint outcome accuracy | 16/16 | Cases with an expected non-null validation status |
| Failure-state preservation | 11/11 | Exact database-row equality after failed replan or read-only unknown/no-data scenario |
| Failed replans only | 6/6 | Route violation, must_visit removal, timeout, grounding conflict, invented ID and concurrent version change |
| State preservation accuracy | 24/24 | Each scenario's permitted state boundary/projection check |
| Fact grounding correctness | 11/11 | Known route values match bound baseline observations or actual replay tool responses; timeout remains unknown |
| Version conflict correctness | 1/1 | Conflict decision and no overwrite of the concurrent winning row |
| Unauthorized mutation count | 0 | Actual failed row comparisons / complete patch preservation projection violation markers |
| External connection attempts in E2E | 0 | Replay execution under network guard |

Failure checks include every stored column, including itinerary JSON, version, archive and timestamps. In the concurrent case, the expected row is captured immediately after the intentional winning CAS. Restoring the old row would be wrong. Accepted replans use the frozen complete preservation projection (including independent derived transport/budget checks) plus a separate activity-ID scope comparison; this is not just a hardcoded zero counter.

The Phase 4 harness has 8 expanded regression instances. Negative controls mutate another day, activity, constraints, summary or budget and verify detection; a fabricated known-route value fails grounding measurement; an incorrect expected label gives a nonzero evaluator exit. A subprocess test compares two complete reports for repeatability within the same date/worktree. This proves measurement sensitivity to those controls, not exhaustive correctness of the harness.

## Scenario detail

Tool counts below are search/detail/route. Every detailed finding, changed/preserved ID set, expected outcome and revision appears in the JSON artifact.

| Scenario | Decision / validation | Version | Tools | LLM | Reuse / recollect |
|---|---|---|---|---:|---|
| `initial_basic` | draft_created / valid | none → 1 | 0/0/0 | 1 | 0/0 |
| `initial_budget` | draft_created / invalid | none → 1 | 0/0/0 | 1 | 0/0 |
| `initial_must_visit` | draft_created / invalid | none → 1 | 0/0/0 | 1 | 0/0 |
| `initial_avoid` | draft_created / invalid | none → 1 | 0/0/0 | 1 | 0/0 |
| `initial_route_requested` | draft_created / unverifiable | none → 1 | 0/0/0 | 1 | 0/0 |
| `route_enough_gap` | valid / valid | 1 → 1 | 0/2/1 | 0 | 0/1 |
| `route_insufficient_gap` | invalid / invalid | 1 → 1 | 0/2/1 | 0 | 0/1 |
| `route_missing_evidence` | unverifiable / unverifiable | 1 → 1 | 0/2/1 | 0 | 0/1 |
| `route_unsupported_mode` | unverifiable / unverifiable | 1 → 1 | 0/0/0 | 0 | 0/0 |
| `replan_replace` | accepted / valid | 1 → 2 | 1/1/2 | 0 | 2/2 |
| `replan_remove` | accepted / valid | 1 → 2 | 0/0/1 | 0 | 2/1 |
| `replan_add` | accepted / valid | 1 → 2 | 1/1/2 | 0 | 3/2 |
| `replan_modify_time_violation` | rejected / invalid | 1 → 1 | 0/0/0 | 0 | 4/0 |
| `replan_must_visit_removal` | rejected / n/a | 1 → 1 | 0/0/0 | 0 | 0/0 |
| `replan_provider_timeout` | unverifiable / unverifiable | 1 → 1 | 1/1/2 | 0 | 2/2 |
| `replan_grounding_conflict` | rejected / n/a | 1 → 1 | 1/1/0 | 0 | 0/0 |
| `selector_valid` | accepted / valid | 1 → 2 | 1/1/2 | 1 | 2/2 |
| `selector_hallucinated` | rejected / n/a | 1 → 1 | 1/0/0 | 1 | 0/0 |
| `selector_injection_text` | accepted / valid | 1 → 2 | 1/1/2 | 1 | 2/2 |
| `selector_version_race` | conflict / n/a | 1 → 2 | 1/0/0 | 1 | 0/0 |
| `rag_exact` | success / n/a | 1 → 1 | 0/0/0 | 0 | 0/0 |
| `rag_semantic_weakness` | no_data / n/a | 1 → 1 | 0/0/0 | 0 | 0/0 |
| `rag_unsupported` | no_data / n/a | 1 → 1 | 0/0/0 | 0 | 0/0 |
| `rag_cache_provenance` | success / n/a | 1 → 1 | 0/0/0 | 0 | 0/0 |

Totals: 8 POI searches, 12 POI detail calls, 14 route calls, 9 model replay calls (5 planner + 4 selector), 17 reused route queries and 14 recollection queries. Recollection counts attempts, including provider timeouts; it does not imply successful evidence. remove/modify_time make no POI searches, and explicit candidate operations make no LLM calls. These are fixture call counts, not production latency/cost savings. Initial RAG uses a forced keyword adapter and bypasses live rewrite/rerank; their quality is not included in these call counts.

## Existing evaluators remain separate

| Evaluator | Cases | Result and interpretation |
|---|---:|---|
| Validator | 67 | 0 mismatches; deterministic rule labels |
| Route-grounded | 48 | 0 mismatches; known/unknown and coverage rules |
| MCP | 28 | 0 mismatches; discovery, schema, direct/protocol equivalence, replay and dedup checks |
| Deterministic replanning | 50 | 0 mismatches; scope, mutation, fact and state checks |
| Selector contract | 36 | 0 mismatches; one-shot output enforcement, not real model preference |
| RAG v2 | 60 | Executed keyword baseline; retrieval quality reported below, not “60/60 accuracy” |

RAG v2 positive-label metrics use 54 cases: Recall@1 62.96%, Recall@3 72.22%, Recall@5 and @6 74.07%, MRR 0.7167. Semantic-preference Recall@6 is 0% across 12 cases. No-result/failure controls are 6/6; evidence groups are completely covered in 8/8 applicable cases; destination contamination and duplicate rates are 0 across 125 returned chunks. Expected retrieval outcome match is only 48/60 (80%). No quality labels were weakened to hide those misses.

The E2E semantic case explicitly expects the known keyword miss. Passing that boundary scenario is not a semantic-quality pass. No live vector/embedding/rerank ablation was run. Real Chroma/provider compatibility is not established by fake index-client tests.

## Final regression

Full backend: **1308 passed, 0 failed, 0 skipped, 0 collection errors**, 420 distinct collected functions/methods. Targeted Phase 3.5/3C/3B/3A/2.5/2/1 counts: **221 / 200 / 195 / 83 / 167 / 200 / 156**, all passing. The [inventory](test-inventory.md) names the files and differentiates functions from expanded instances.

The former `test_generate_day_edit_draft_accepts_nested_day_shape` failure was audited: its mock called `json.dumps` without importing `json`. Phase 4 adds exactly that import; production planner behavior is unchanged. Initial runs of the new isolation wrapper incorrectly blocked two existing local MCP health checks; allowing localhost for the regression/MCP suite fixed the harness issue, and the final full suite was rerun. E2E retains stricter connection blocking.

## Dataset versions and hashes

All older evaluator labels and production files are unchanged from the baseline. Dataset hashes below are exact bytes at execution, not generated expected labels.

| Dataset | SHA-256 |
|---|---|
| validator | `6c3e73cccfff18688143b5c5d1ead7009baa1d4ce0cd17c854ddec299f37e0d1` |
| route | `5c754f64b77a63dff046fdcdfc994d4be22016945c9b097ba5b6fe6a09379ffb` |
| mcp | `88c03e8fdbbf1f48a73cb4f998680bb7852000abe3c9238994f9ee5d3963ff4e` |
| replanning | `d4eb8714a101e84a8e12c719f5711682d2cb3c40f107b16e2aeaf7b835bf6c6f` |
| selector | `3a9e4a53bc36a7186ffd1cb5cf95e37c93b8e5aa368c4aa2b0ab72fb93333449` |
| rag_v2 | `22fc0f90a525edb909cab190ae060d950fedda6d549e830d64deaec6e6edd58a` |
| E2E (`e2e-v1`) | `1084dd46de24d7cebe46cf7ec1ca8d205341e7a257d63ab89bc2c124a37f5009` |

The RAG schema is `knowledge-v2`, chunking policy `heading-path-v2`; logical corpus manifest, index and configuration identity are preserved in the frozen RAG report / Phase 3.5 record. Reports include UTC date, Python version, mode, provider type and baseline commit. E2E additionally records the fixture/harness/runtime hashes; regression records every collected test source hash.

## Reproduction

Run from the repository root after installing declared dependencies into your own environment:

```sh
python -B backend/scripts/evaluate_end_to_end.py --output e2e-results.json
python -B backend/scripts/evaluate_frozen_suites.py --output frozen-results.json
python -B backend/scripts/run_offline_regression.py --suite full --output regression-results.json
```

The [test inventory](test-inventory.md) gives every targeted command. Choose output paths outside the repository for local reruns. No environment file or provider key is required for these offline commands. Application runtime directories may be ensured by existing config imports, but database content is temporary. Do not run `ingest_data.py` for this benchmark.

## Repository hygiene and residual issues

The tracked and Phase 4 added text was checked for obvious credential shapes, private-key blocks, accidental environment files, local database/index artifacts, dependency trees, absolute user/workspace paths and oversized generated files. No matching obvious secret, tracked `.env`, database, Chroma index, venv or node_modules was found. The existing ignored `backend/db` directory remains local. There are no new files above 5 MB; largest frozen dataset is about 273 KB. This is a scoped static check, not proof that no secret has ever existed in repository history.

No obvious shipping-blocking TODO/dead experimental module was identified in the scoped review; this is not an exhaustive reachability analysis. Production Planner code still prints model/base URL, raw failure exceptions and response previews. It needs a separate deployment privacy/security review. No logging or production refactor was performed.

Historical phase documents and CHANGELOG contain old measurements/test failures; README labels them historical and points to this report. The new public README uses relative repository links and no local absolute paths. Frontend currently generates managed drafts, blocks legacy edits for them, and has no candidate/replan UI; API/fixture demonstrations must state that limitation.

Remaining limits: driving estimates only; limited timed initial coverage; estimated prices; no opening-hours/weather hard validation/booking; single-day/single-operation replans; single-process candidate cache; six-city static corpus; unverified live providers/model quality; no production authentication/distributed guarantees; unverified clean install, Docker and browser build. No core blocker was silently repaired to improve benchmark scores.

## Maturity and review state

**Reliability-oriented applied AI system, verified within fixed offline boundaries.** This is an engineering evidence classification, not production readiness. Phase 4 leaves a reviewable dirty worktree and creates no commit or push. Suggested message after review: `feat(project): add end-to-end benchmark and final documentation`.
