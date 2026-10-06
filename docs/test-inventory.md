# Final test inventory

Collected by [run_offline_regression.py](../backend/scripts/run_offline_regression.py), with temporary SQLite and provider credentials disabled. Source hashes and full per-file counts are in [regression results](phase4-regression-results.json). Distinct functions/methods are counted by collected node ID before parameter suffixes; expanded instances count pytest items. Neither count is a number of independent travel scenarios.

| Suite | Functions/methods | Expanded instances | Purpose | External network |
|---|---:|---:|---|---|
| Phase 3.5 | 88 | 221 | Typed retrieval, corpus/index/cache identity, rewrite and evaluation | None |
| Phase 3C | 42 | 200 | Bounded selection contracts, failures and version races | None |
| Phase 3B | 59 | 195 | Scope, patches, evidence reuse, preservation and CAS | None |
| Phase 3A | 31 | 83 | Authoritative state loading, serialization and write boundaries | None |
| Phase 2.5 | 31 | 167 | Typed tools, MCP protocol, lifecycle and secret redaction | None; local loopback HTTP for MCP |
| Phase 2 | 50 | 200 | Route collection, binding, freshness and three-state validation | None |
| Phase 1 | 40 | 156 | Constraint rules, hardening and request/plan integration | None |
| Phase 4 | 4 | 8 | Measurement counterexamples, repeatability and incorrect-label exit | None; subprocess replay |
| Full backend | 420 | 1308 | All backend tests, including legacy app features | None; local MCP HTTP |

All listed targeted suites passed. The full run contains 1308 passes, zero failures, skips or collection errors. The former day-edit failure was a test mock missing `import json`; only that import changed. The regression log includes existing warnings (mostly Pydantic deprecated model-copy and malformed-boundary serialization cases); warnings are not hidden as skips.

The Phase 3.5 targeted list contains 221 instances. The full suite also includes two older `test_knowledge_validation.py` cases not included in that frozen targeted group. Phase 2.5 includes the secret-logging regression file. Suite selection is explicit in the runner's `SUITES`; do not infer it from broad filename globs or sum tables into an accuracy score.

## Per-file collection

| Test file | Functions/methods | Expanded instances |
|---|---:|---:|
| [test_amap_secret_logging.py](../backend/tests/test_amap_secret_logging.py) | 2 | 15 |
| [test_api_trip.py](../backend/tests/test_api_trip.py) | 20 | 20 |
| [test_authoritative_state.py](../backend/tests/test_authoritative_state.py) | 23 | 47 |
| [test_candidate_selection_evaluation.py](../backend/tests/test_candidate_selection_evaluation.py) | 2 | 37 |
| [test_candidate_selector.py](../backend/tests/test_candidate_selector.py) | 6 | 33 |
| [test_candidate_selector_hardening.py](../backend/tests/test_candidate_selector_hardening.py) | 14 | 73 |
| [test_city_registry_service.py](../backend/tests/test_city_registry_service.py) | 5 | 8 |
| [test_city_resolver_service.py](../backend/tests/test_city_resolver_service.py) | 8 | 8 |
| [test_dynamic_trip_service.py](../backend/tests/test_dynamic_trip_service.py) | 5 | 5 |
| [test_end_to_end_evaluation.py](../backend/tests/test_end_to_end_evaluation.py) | 4 | 8 |
| [test_knowledge_validation.py](../backend/tests/test_knowledge_validation.py) | 2 | 2 |
| [test_mcp_gateway.py](../backend/tests/test_mcp_gateway.py) | 11 | 46 |
| [test_models_schemas.py](../backend/tests/test_models_schemas.py) | 7 | 7 |
| [test_place_candidate_service.py](../backend/tests/test_place_candidate_service.py) | 9 | 9 |
| [test_plan_patch_service.py](../backend/tests/test_plan_patch_service.py) | 2 | 15 |
| [test_plan_validator.py](../backend/tests/test_plan_validator.py) | 21 | 41 |
| [test_planning_evaluation.py](../backend/tests/test_planning_evaluation.py) | 4 | 70 |
| [test_planning_fact_service.py](../backend/tests/test_planning_fact_service.py) | 13 | 41 |
| [test_planning_hardening.py](../backend/tests/test_planning_hardening.py) | 9 | 38 |
| [test_planning_workflow.py](../backend/tests/test_planning_workflow.py) | 6 | 7 |
| [test_rag_destination_metadata.py](../backend/tests/test_rag_destination_metadata.py) | 5 | 5 |
| [test_rag_eval_cases.py](../backend/tests/test_rag_eval_cases.py) | 2 | 2 |
| [test_rag_evaluation_v2.py](../backend/tests/test_rag_evaluation_v2.py) | 12 | 13 |
| [test_rag_hardening.py](../backend/tests/test_rag_hardening.py) | 32 | 85 |
| [test_rag_index.py](../backend/tests/test_rag_index.py) | 8 | 15 |
| [test_rag_integrity_hardening.py](../backend/tests/test_rag_integrity_hardening.py) | 24 | 96 |
| [test_rag_retriever.py](../backend/tests/test_rag_retriever.py) | 2 | 2 |
| [test_rag_tool.py](../backend/tests/test_rag_tool.py) | 3 | 3 |
| [test_replanning_candidates.py](../backend/tests/test_replanning_candidates.py) | 7 | 15 |
| [test_replanning_commit.py](../backend/tests/test_replanning_commit.py) | 5 | 11 |
| [test_replanning_correctness_hardening.py](../backend/tests/test_replanning_correctness_hardening.py) | 31 | 50 |
| [test_replanning_evaluation.py](../backend/tests/test_replanning_evaluation.py) | 1 | 50 |
| [test_replanning_facts.py](../backend/tests/test_replanning_facts.py) | 7 | 11 |
| [test_replanning_llm_selection.py](../backend/tests/test_replanning_llm_selection.py) | 20 | 57 |
| [test_replanning_preservation.py](../backend/tests/test_replanning_preservation.py) | 1 | 11 |
| [test_replanning_scope.py](../backend/tests/test_replanning_scope.py) | 4 | 28 |
| [test_replanning_tool_efficiency.py](../backend/tests/test_replanning_tool_efficiency.py) | 1 | 4 |
| [test_route_facts.py](../backend/tests/test_route_facts.py) | 16 | 45 |
| [test_route_hardening.py](../backend/tests/test_route_hardening.py) | 8 | 52 |
| [test_route_workflow.py](../backend/tests/test_route_workflow.py) | 8 | 10 |
| [test_services_trip.py](../backend/tests/test_services_trip.py) | 8 | 8 |
| [test_state_boundary_hardening.py](../backend/tests/test_state_boundary_hardening.py) | 8 | 36 |
| [test_storage_service.py](../backend/tests/test_storage_service.py) | 3 | 3 |
| [test_tool_adapter.py](../backend/tests/test_tool_adapter.py) | 10 | 56 |
| [test_tool_contracts.py](../backend/tests/test_tool_contracts.py) | 8 | 50 |
| [test_tool_grounded_evaluation.py](../backend/tests/test_tool_grounded_evaluation.py) | 5 | 52 |
| [test_trip_planner_agent.py](../backend/tests/test_trip_planner_agent.py) | 8 | 8 |

## Reproduce

```sh
python -B backend/scripts/run_offline_regression.py --suite 3.5 --output rag-tests.json
python -B backend/scripts/run_offline_regression.py --suite 3C --output selector-tests.json
python -B backend/scripts/run_offline_regression.py --suite 3B --output replanning-tests.json
python -B backend/scripts/run_offline_regression.py --suite 3A --output state-tests.json
python -B backend/scripts/run_offline_regression.py --suite 2.5 --output tool-tests.json
python -B backend/scripts/run_offline_regression.py --suite 2 --output route-tests.json
python -B backend/scripts/run_offline_regression.py --suite 1 --output constraint-tests.json
python -B backend/scripts/run_offline_regression.py --suite 4 --output e2e-harness-tests.json
python -B backend/scripts/run_offline_regression.py --suite full --output full-tests.json
```

Use an output path outside the repository to avoid leaving generated local files. The runner disables pytest's cache and uses a temporary basetemp. Application config may ensure ignored runtime directories exist; SQLite data and evaluation artifacts are isolated from the production database. Credentials are cleared before application imports. Windows stdlib socketpair is allowed for the event loop; MCP suites allow localhost protocol traffic with replay providers.
