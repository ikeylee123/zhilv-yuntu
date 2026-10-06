# Short demonstration checklist

## Reproducible offline demonstration

From the repository root, run:

```sh
python -B backend/scripts/evaluate_end_to_end.py --output e2e-results.json
```

Open the result and inspect these scenarios:

1. `initial_basic`: HTTP generate, replay LLM, real local knowledge context, deterministic validation, persisted managed draft/version 1.
2. `initial_route_requested`: the local draft has insufficient timed route coverage, so the report remains unverifiable.
3. `replan_replace`: prepare authorized candidates, exact POI detail grounding, deterministic patch, affected route queries, validation and version 2.
4. `replan_modify_time_violation`: deterministic edit creates an insufficient gap; rejected and version 1 unchanged, with zero search/LLM calls.
5. `replan_provider_timeout`: unknown route evidence; unverifiable and the active row unchanged.
6. `selector_hallucinated` / `selector_version_race`: one replay selection cannot invent authority or overwrite the winning revision.
7. `rag_cache_provenance`: valid cache hit, stale-version miss and rejection of a fabricated body even when its self-hash is recomputed.

Provider and model responses are deliberate fixtures. No browser or live inference is demonstrated by this command. Timed multi-attraction replanning fixtures are server-created in temporary SQLite; they are not editable client-supplied authoritative payloads.

## Optional manual API walkthrough

Start the backend as described in the [README](../README.md). In `/docs`:

- `POST /trip/generate?managed_state=true`: provide destination/dates/travelers/budget and explicit constraints.
- Inspect `validation_report.status`, findings, route coverage, `trip_id`, `planning_version` and activity IDs.
- `GET /trip/{trip_id}/state`: inspect the server-owned baseline.
- `POST /trip/{trip_id}/replan/candidates`: use the current `base_plan_version`, `target_day_index`, `change_type="replace"` and target activity ID.
- `POST /trip/{trip_id}/replan`: use `selection_mode="explicit"`, the returned candidate set/ID and the same version/target, or `selection_mode="llm"` without supplying candidate IDs.
- Re-read state after accepted/rejected/unverifiable/conflict. Accepted returns a newer revision; failed requests do not commit a candidate.

A locally generated draft may lack the grounded identities/evidence needed for successful replacement. Treat that rejection as a boundary result; do not bypass it by submitting fabricated facts. A successful live demonstration depends on genuine provider credentials, candidate coverage and evidence. It has not been verified in Phase 4.

The Vue client generates managed drafts but has no current candidate/replan UI. Legacy day edit is deliberately blocked for managed state. Use the API walkthrough and offline fixtures for the reliability demonstration rather than claiming a complete browser editing flow.
