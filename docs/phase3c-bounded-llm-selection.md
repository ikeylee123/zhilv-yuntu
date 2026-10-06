# Phase 3C: Bounded LLM Candidate Selection

## Baseline and scope

Implementation started from a clean working tree on
`feat/constraint-preserving-replanning`, HEAD / frozen Phase 3B commit
`b0de2b0fdaf936d060ccc763b069f01270cdb7b1`.
Phase 3A state/CAS and Phase 3B services, models, validators, patches, candidate
filtering, route facts and evaluators remain unchanged. Only the HTTP dispatch and
LLM configuration are extended; new selection modules wrap the existing core.
README, dependencies, frontend and the four existing evaluation datasets are unchanged.

## Architecture and authority

```text
managed state + structured intent
  -> scope/version checks
  -> existing server candidate preparation and filtering
  -> current bounded grant authorization (<=10)
  -> one LLM request -> strict candidate_id (+ optional rationale)
  -> reload version, check unchanged grant, TTL and membership
  -> existing Phase 3B grounding/patch/preservation/facts/full validator
  -> deadline gate -> existing valid-only CAS
```

`app/agents/candidate_selector.py` has no storage, authoritative state, ToolProvider,
MCP, validator or CAS access. It accepts a minimized DTO and returns a selection
attempt. `app/services/replanning_selection.py` owns orchestration, not another merge
implementation. It delegates to the frozen `ReplanningService`. A per-request shallow
service copy adds a deadline gate in front of the original committer; it does not
change the shared service or CAS implementation.

Selection is not validation. Only the chosen ID is authoritative as a selection,
and only within the current server grant. Rationale is untrusted display/debug text;
it never enters the plan, facts, validator or constraints. Models cannot create IDs,
change times/coordinates/budgets/constraints, produce patches or call tools.

## API

`POST /trip/{trip_id}/replan` accepts `selection_mode=explicit|llm`, default `explicit`.
`/replan/candidates` is unchanged. Explicit mode retains the original candidate_id
and candidate_set_id request and never constructs a selector. LLM mode prepares its
own candidate set; supplying either candidate field in that mode is a 422 error.
Optional `preference=attraction|museum|park` controls existing server search only.

Example replacement:

```json
{
  "trip_id": "managed-trip",
  "base_plan_version": 5,
  "selection_mode": "llm",
  "change_type": "replace",
  "target_day_index": 1,
  "target_activity_id": "existing-activity",
  "preference": "museum"
}
```

For add, omit target_activity_id and supply exactly one before_activity_id or
append=true plus explicit increasing start_time/end_time. The model chooses neither
anchor nor times. Overlap is handled by the existing full validator.
Remove and modify_time always take the deterministic path, including when requested
with selection_mode=llm. Invalid request, stale version, collection failure and
must_visit removal conflict cause zero model requests. Replace/add issue one request
once prechecks, nonempty candidates and resource admission succeed; there is no retry.
Responses add optional selection_metadata; no internal prompt is returned.

## Input and output contracts

`CandidateSelectionInput` and all nested DTOs forbid extra fields and coercion.
It contains selection_id, replace/add operation, target context, hard constraint
summary, bounded soft preferences, candidate_set_id and 1-10 unique candidates.
Only target name/theme/authorized time slot, relevant represented must_visit labels,
server-filtered avoid/category indicators and preferences are projected.
Candidate fields are ID, name, category and optional short_description,
address_or_area and provider_type. Current POIData has no description, so projection
uses available name/address/provider_type and leaves short_description null. Costs
are deliberately omitted: the selector does no budget or route arithmetic.
Text fields are bounded; context/preference summaries may be truncated for prompting.
The authoritative constraints remain complete and unchanged for final validation.

No entire itinerary/request, other days, PlanningFacts, raw ToolResult, coordinates,
cache internals, provider credentials, tool schemas or database metadata are sent.
Candidate collection's region/category/avoid/duplicate checks and grant freshness
are server responsibilities. Both before and after selection, current grant
trip/version/operation/target/day, timestamps and membership are checked.

Output is exactly candidate_id (nonblank string) and optional rationale (string/null,
at most 1000 characters). The provider strict JSON schema requires both fields with
nullable rationale. Local validation also accepts rationale omission. Parsing accepts
only a JSON object, not prose, fenced JSON, arrays, duplicate keys, extra fields or
coerced IDs. Content is capped at 8192 characters. Schema-valid invented IDs are
rejected before get_poi; there is no lookup fallback for invented IDs.

## Prompt and injection boundary

Prompt version is `CANDIDATE_SELECTOR_PROMPT_VERSION=v1`; selector policy is
`bounded-one-shot-v1`. A fixed system instruction marks all task/candidate/preference
content as untrusted data. It cannot alter IDs, schema or authorization. The user
message is a serialized minimized DTO. Malicious candidate content may affect a
model's ranking, but cannot gain plan mutation authority: strict schema, membership,
version checks and the deterministic validator remain outside the model.
Tests with malicious text and invented replay output establish enforcement boundaries,
not empirical resistance of an actual model to every injection.

## Provider, retries and deadlines

Reuse the existing langchain_openai ChatOpenAI and LLM_API_KEY/LLM_BASE_URL settings.
No second LLM framework or dependencies are introduced. Configuration:

| Setting | Default |
| --- | --- |
| CANDIDATE_SELECTOR_MODEL | existing LLM_MODEL |
| CANDIDATE_SELECTOR_TIMEOUT_SECONDS | 15 seconds |
| REPLAN_SELECTION_DEADLINE_SECONDS | 60 seconds |
| selector temperature | 0 |
| selector SDK max_retries | 0 |
| selector/orchestration semantic retries | 0 |

The pre-existing planner uses LLM_MAX_RETRIES (default 1); selector explicitly
overrides it to 0. There are no stacked retries, repair prompts or model escalation.
Temperature zero reduces sampling variability; it does not guarantee determinism.
Provider must support strict response_format=json_schema; unsupported providers
fail closed rather than falling back to prose or tool calls.

Positive finite selector timeout must be strictly less than the overall deadline.
A bounded daemon worker performs only the provider read; the caller stops waiting
at the selector timeout. Late responses have no continuation into orchestration.
At most eight provider workers can be outstanding per process; saturation returns
provider error without another request. The SDK also receives the timeout.
Version and grant TTL are rechecked after the call. A competing writer produces
VERSION_CONFLICT, preserving that writer's state; there is no automatic rebase.

The overall deadline gates candidate preparation completion, selection, core entry
and entry to CAS. Existing synchronous tool calls/DB transactions are not forcibly
cancelled; this is a commit-admission deadline, not a guaranteed HTTP response SLA.
An already-entered CAS can finish after the deadline. An uncooperative HTTP provider
can occupy a bounded detached worker until it finishes; it cannot commit a late result.

## Failure semantics

| Condition | Decision / reason |
| --- | --- |
| Malformed/extra-field/schema-invalid content | rejected / LLM_SELECTION_INVALID |
| Empty content or empty candidate set | rejected / LLM_SELECTION_EMPTY |
| Invented ID, wrong/changed/expired/evicted grant | rejected / LLM_SELECTION_NOT_AUTHORIZED |
| Timeout or deadline exhausted | unverifiable / LLM_SELECTION_TIMEOUT |
| Provider failure, unavailable client, invalid timeout policy, saturation | unverifiable / LLM_SELECTION_PROVIDER_ERROR |
| Concurrent authoritative version | conflict / VERSION_CONFLICT |
| Candidate collection failure | existing REPLAN_UNVERIFIABLE |
| Selected detail conflicts with search evidence | existing GROUNDING_CONFLICT |
| Deterministic full validation fails | existing REPLAN_VALIDATION_FAILED / REPLAN_UNVERIFIABLE |

All selector failures preserve the entire authoritative row. Grounding/validator/CAS
failures remain deterministic-core outcomes, never reclassified as provider failure.
For a concurrent writer, preservation means not overwriting the winning version.

## Observability

Response metadata and structured logs record selection_id, model, candidate_set_id,
selected ID, timestamp, prompt/schema and policy versions, candidate count, prompt
character count, latency and input/output token counts when supplied by the client.
Logs additionally include hashed trip reference, base version and final decision/reason.
Rationale is response-only and excluded from logs. Metadata is not persisted inside
the authoritative itinerary or used as fact evidence. No new database schema is added.
No full prompt, private preferences, API key or raw exception messages are logged by
these modules. Existing host logging/tracing configuration remains an operational
responsibility. Tokens are observation only; no token optimization claim is made.

## Separate evaluation

Run offline (default, no real model/network calls):

```text
python -B backend/scripts/evaluate_candidate_selection.py
```

`backend/eval/candidate_selection_eval_cases.json` contains 36 fixed cases:
10 simple preference, 6 hard-constraint context, 6 similar candidates, 6 malicious
text, 4 invalid outputs and 4 provider failures. Subjective cases use acceptable ID
sets rather than asserting a universally correct travel choice. Replay outputs and
expected labels are static data; the evaluator does not write either.

Offline result: contract 36/36; one-call count 36/36; unauthorized-ID enforcement
2/2; invalid-schema enforcement 3/3; failure mapping 10/10; no mismatches.
Raw injected schema compliance is 28/36 and authorized/acceptable output is 26/36:
the deliberate invalid and failure cases are in those denominators. These are not
actual model quality rates. Offline token median is null; replay latency is not
model latency. State preservation and end-to-end call counts are integration tests,
not claims derived from the isolated selector evaluator.

Optional `--live` explicitly enables configured model calls and evaluates the 28
content tasks (excluding controlled invalid/provider injections). It reports schema
compliance, authorized selection, acceptable-choice count and median usage/latency.
Live mode was NOT run for this implementation. No actual model preference match,
real token usage, injection resistance or replanning accuracy is claimed.
The existing 67/48/28/50-case evaluators and labels remain unchanged.

## Verification (2026-10-06)

Tests use isolated temporary SQLite and fake providers/model responses. Added files:
`test_candidate_selector.py`, `test_replanning_llm_selection.py`, and
`test_candidate_selection_evaluation.py`. They cover strict schemas, minimal prompts,
SDK configuration, failures, late responses, pre/post grant races, real DB + CAS,
explicit/remove/time zero calls, same-core route/budget/overlap/grounding failures,
must_visit choices, server filtering, malicious metadata/rationale and log minimization.

| Check | Result |
| --- | --- |
| Phase 3C targeted (after boundary hardening) | 200 passed |
| Phase 3B targeted | 195 passed |
| Phase 3A targeted | 83 passed |
| Phase 2.5 targeted | 167 passed |
| Phase 2 targeted | 200 passed |
| Phase 1 targeted | 156 passed |
| Selector evaluator | 36/36 contract match |
| Phase 3B / MCP / Phase 2 / Phase 1 evaluators | 50/50, 28/28, 48/48, 67/67; no mismatches |
| Full backend (after boundary hardening) | 1089 passed, 2 pre-existing failures |

The two full-backend failures are test_rag_retriever.py::
test_retrieve_travel_guide_formats_chunks_as_text (actual top_k=6, expected 8) and
test_trip_planner_agent.py::test_generate_day_edit_draft_accepts_nested_day_shape
(mock missing json import). They are neither suppressed nor changed.
Final evaluator reporting assertions were rerun after adding enforcement counters:
37 evaluator tests passed, with all metrics above unchanged.

## Limits and non-goals

Candidate quality depends on existing bounded provider metadata; current metadata
may be too sparse for strong preference ranking. No live quality claim is available.
Existing in-memory candidate-store TTL, single-process cache and Phase 3B estimated
cost/provenance limitations remain. Existing route/tool collection behavior remains.
There is no new durable selection audit database or frontend UI.

No autonomous tools, RAG call, RAG hardening, reranker/embedding model, weather/open
hours, full itinerary generation, multi-operation/day planning or repair loop was
added. LangGraph and Multi-Agent are not needed for one bounded selection followed
by the existing deterministic core. Jev is not introduced.

No commit or push. Suggested message after review:
`feat(planning): add bounded LLM candidate selection`.

## Selector boundary hardening (2026-10-06)

The existing parse_selection boundary retains the 8192-character cap and duplicate
key rejection, explicitly requires an object root, and maps parsing/structure failures
(ValueError including JSONDecodeError, TypeError, RecursionError, UnicodeError and
Pydantic ValidationError) to LLM_SELECTION_INVALID. No broad Exception catch or new
JSON dependency was added. The minimal depth policy is to catch parser recursion
failure; there is no independent fixed nesting-depth limit. Provider/network errors
and timeout remain separate. Empty output retains LLM_SELECTION_EMPTY and a
schema-valid invented ID retains LLM_SELECTION_NOT_AUTHORIZED.

TargetContext reuses Phase 3B ClockTime: strict zero-padded 24-hour HH:MM. Both times
must be present or both absent, and a supplied slot must increase within one day.
Add requires both; replace permits no slot or a valid read-only existing slot.
9:00, 25:90, 09:60, 9am, yesterday, blank strings, partial pairs, equal/reversed times
and 23:00 -> 01:00 are rejected. The selector never guesses a missing time. These
fields remain input-only: model output still contains only candidate_id/rationale.

Invalid selector input raises validation failure before invoking the model. Invalid
projected server context maps to INVALID_CHANGE_REQUEST, not LLM_SELECTION_INVALID.
Malformed public add requests retain HTTP 422. The exact deep-array counterexample
('[' * 3000 + '0' + ']' * 3000) now returns HTTP 200 with rejected /
LLM_SELECTION_INVALID: one selector/client call, zero grounding/route/CAS calls,
and the complete authoritative database row and planning_version unchanged.
The same checks cover a deep object. Missing/invalid add times produce zero model,
tool and CAS calls and preserve the row.

Independent test_candidate_selector_hardening.py adds 73 expanded regression cases
for parsing, time contracts, error classification and HTTP/DB preservation. It passes
independently; all Phase 3C tests now total 200 passed. Phase 3B/3A/2.5/2/1 targeted
results remain 195/83/167/200/156 passed. Selector/3B/MCP/2/1 evaluators remain
36/50/28/48/67 cases with no mismatches. Full backend: 1089 passed, the same two
pre-existing failures documented above. No actual model call was made.

The 36-case data, inputs and expected labels are byte-for-byte unchanged:
SHA-256 3a9e4a53bc36a7186ffd1cb5cf95e37c93b8e5aa368c4aa2b0ab72fb93333449.
Phase 3B core, other datasets/evaluators, README and dependencies are unchanged.
