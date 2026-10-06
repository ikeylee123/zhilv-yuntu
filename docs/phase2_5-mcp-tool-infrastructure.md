# Phase 2.5: Unified Tool Contract and Minimal MCP Gateway

## Baseline and purpose

Implementation branch: `feat/mcp-tool-infrastructure`.
Frozen Phase 2 HEAD: `557b082dacb89c137128310056a2f17554c087ae`.
The working tree was clean before branch creation. No commit/push is part of this phase.
Baseline: Phase 1 156 passed, Phase 2 200 passed, full backend 444 passed / 2 known failures.

Production planning currently uses the direct typed adapter. MCP is a standardized
external tool gateway, not the decision engine. It is optional: the existing workflow
needs no local HTTP round trip, no MCP schemas in Planner prompts and no new Agent.
The concrete benefit is protocol-level reuse by evaluation and future consumers.

## Architecture and files

```text
Existing generate/edit/save -> Fact Collector -> DirectToolProvider -> MapBackend
                                                                  -> existing map_service
Evaluation/external consumer -> MCPToolProvider -> Streamable HTTP gateway
                                               -> same DirectToolProvider
ToolResult -> identity / query / freshness binding -> PlanningFacts -> pure Validator
```

- `app/models/tool_contracts.py`: immutable requests, typed results and query identities.
- `app/services/tool_adapter.py`: normalization, cache ownership, run registry, provider protocol.
- `app/services/tool_replay.py`: offline raw supplier fixtures; no HTTP fallback.
- `app/mcp/server.py`: one logical server, exactly three read-only tools.
- `app/mcp/client.py`: discovery, call, structured-result validation and failure mapping.
- `scripts/evaluate_mcp_contract.py`: real loopback protocol evaluation using fixed responses.

The SDK low-level public `Server` API declares the same Pydantic request/output schemas
used by the adapter. It passes original arguments to the adapter so unknown fields
cannot disappear inside generated function wrappers. No provider HTTP code is duplicated.
The collector retains its plan binding, pair discovery, conservative skip policy and
Phase 2 finding vocabulary. Its former cross-run cache was moved to the adapter.
The pure validators and Phase 2 fact model are unchanged.

## Dependencies and transport

Official Python SDK is pinned to `mcp==2.3.0` in backend/requirements.txt, verified against
PyPI during implementation. SDK and `mcp-types==2.3.0` were installed into the existing
`%TEMP%/zhilv-phase1-tests` virtual environment, not system Python. That environment
inherits existing site packages. Tested interpreter: Python 3.13.5. No Python downgrade.
SDK dependencies include httpx2 and Pydantic >=2.12; the project provider retains httpx.
No unrelated dependency or deployment service was introduced. The existing Docker Python
3.11 image was not rebuilt or certified during this task.

The gateway uses Streamable HTTP, stateless HTTP mode and structured JSON results.
No old SSE transport, stdio entrypoint, separate MCP container or public deployment.
The default backend does NOT mount MCP. Enable only in a controlled environment:

```powershell
# From backend/, after installing declared requirements in your chosen virtualenv:
$env:ENABLE_TRAVEL_MCP='true'
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# MCP URL: http://127.0.0.1:8000/mcp/
```

Mounting preserves FastAPI's prior lifespan. Tool definitions and the provider factory
remain reusable; each startup creates a new lifespan-scoped SDK session runtime.
Shutdown exits the SDK context and clears dispatch to that runtime. The same app supports
repeated startup/shutdown: enabled and disabled production FastAPI apps were verified over
three cycles, including health checks and real SDK discovery/calls when enabled. Existing
Compose configuration is unchanged. Do not enable this unauthenticated development
endpoint on a public listener. SDK host/origin protection and a 64 KiB request-body limit
are enabled; they do not replace deployment authentication. Production gateway use needs
access control and resource limits appropriate to the hosting environment.

## Tool contract

`ToolResult[T]` contains schema_version, status, data, source, fetched_at, reason, metadata.
Source includes provider, endpoint and API version; metadata includes request_id,
query_hash, policy_version, cache_hit, provider_attempts and deduplicated.
Success requires typed non-null data, complete source, aware fetched_at and no failure
reason. Failures require reason and null data. Models reject unknown fields and freeze
nested values. Numeric fields reject bool, negative distance/duration, NaN and infinity;
legitimate numeric strings normalize to JSON numbers. Coordinates are bounded GCJ-02.

Tools:

| Tool | Input | Success data |
| --- | --- | --- |
| search_poi | nonblank query/city_or_adcode, category spot/meal/hotel, page >=1, page_size 1..25, city_limit bool | tuple of POIData, page, page_size |
| get_poi | nonblank poi_id | exact POIData with coordinates |
| get_driving_route | origin/destination, mode driving, strategy 0 | bound endpoints, mode/strategy, distance_meters, duration_seconds |

Input text is limited to 256 characters. Integer/bool request fields are strict.
Unsupported route mode/strategy is rejected without substituting driving/default strategy.
Search may return null coordinates, but these cannot become known route evidence.
Exact lookup never uses a name-search fallback and requires coordinates for success.
The existing exact-ID service does not return category/address/adcode/provider_type;
those fields remain null rather than being invented. Search category is supplied by the
validated request and mapped to the existing provider category vocabulary; provider_type
remains separate. If exact lookup returns no exact result it is no_data; an adapter-level
returned ID mismatch is invalid_response / ID_MISMATCH.

No tool returns feasible=true, plan decisions, secrets or raw provider payloads.

## ToolResult versus FactStatus

Tool status describes execution/normalization, not applicability to a plan.
A successful route observation only becomes Known after the collector has bound server
POI identity, the requested endpoints, source, timestamp and current plan fingerprint.
The validator independently checks applicability and freshness using its explicit clock.

Failure vocabulary: INVALID_ARGUMENT, UNSUPPORTED, NO_DATA, TIMEOUT, PROVIDER_ERROR,
INVALID_RESPONSE and ID_MISMATCH. RATE_LIMITED is reserved but never guessed from text.
Transport unavailability: provider_error / TOOL_UNAVAILABLE (not an AMAP failure).
Client deadline expiry: timeout / TOOL_TIMEOUT. Malformed structured output, SDK output
schema rejection or binding mismatch: invalid_response / MALFORMED_TOOL_RESULT.
MCP execution errors have isError=true; normal empty results have isError=false.
Unrecognized tools and malformed protocol requests are rejected. Exception strings are
not exposed as machine reasons.

Collector findings preserve Phase 2 lower-case reasons. A stale cached route whose refresh
fails stays stale; MCP/replay transports do not invent historical observations on failure.
A successful replay with an old timestamp is still rejected as stale by the collector.
Client-supplied itinerary facts and validation reports remain untrusted.

## Dedup, cache and provenance

ToolQueryKey hashes stable JSON of tool_name, schema_version, provider, api_version,
policy_version and all normalized arguments. Request ID, fetched_at and latency are absent.
Route endpoints retain direction and exact coordinate precision/system. Search includes
city, category, pagination and city_limit. Exact lookup includes POI ID.

A DirectToolProvider or MCPToolProvider instance represents ONE planning/evaluation run.
Its RunRegistry shares one Future per query, merging concurrent requests and reusing
success/no_data/timeout/provider_error/invalid_response results. New runs need new instances;
do not retain one provider forever. Reused calls report zero provider_attempts and
`deduplicated=true`. No distributed or cross-client global dedup is claimed. Each gateway
request gets a fresh adapter by default; MCPToolProvider provides run-scoped dedup before
sending requests. A trusted server factory can inject a run registry for controlled tests.

Cross-run cache ownership belongs only to DirectToolProvider. Keys use `tools:v1:` and
complete ToolResult envelopes. Route and exact POI freshness are 900 seconds; search is
86400 seconds. Redis outage is a cache miss, not a successful fabricated result.
Missing explicit schema_version/policy_version, missing fetched_at/source, mismatched query,
invalid numeric values or future timestamp cannot be reused as fresh evidence.
Cache hits preserve original fetched_at. Expired valid observations may be retained for
collector stale reporting if the refresh fails, never placed in failed ToolResult.data.

Old `planning:route:v1:` and unversioned map cache entries are not migrated or re-stamped;
they miss the new cache. `search_places(use_cache=False)` prevents stacking the old cache
under the new adapter. Other legacy map consumers retain their existing cache behavior.
Legacy candidate-pool generation still uses its existing service; it is not a new MCP Agent.

The old collector cache tests were migrated to ToolResult fixtures and the new namespace;
the original fresh/stale/missing-time/wrong-query/future/failure assertions remain. The
48-case and 67-case evaluation inputs and labels remain byte-for-byte unchanged.

## Retry, timeout and observation

Zero automatic provider/adapter/collector retry. MCPToolProvider uses the SDK public
session call API once, avoiding autonomous rounds and high-level header-recovery retries.
The existing supplier HTTP timeout is unchanged (default AMAP_TIMEOUT_SECONDS=20).
MCP client default deadline is 25 seconds; consumers must provide a larger orchestration
budget when using this optional transport. These are not whole-itinerary latency promises.
Client timeouts cannot turn into success; a blocked synchronous provider thread may finish
later, but no tool can commit state, update plan versions or modify an itinerary.

Adapter logs contain request_id, tool_name, query_hash, provider, status/reason,
latency_ms, cache_hit, provider_attempts, fetched_at, schema_version and policy_version.
They do not log raw responses, keys, headers or user query text. The new uncached POI path
also avoids legacy query logging. Provider HTTPX/httpcore log emitters redact the configured
AMAP credential, including URL-encoded forms, from rendered messages/arguments and exception
text without suppressing business errors or unrelated log levels. AmapServiceError messages
and reasons are sanitized and do not chain raw HTTP exceptions into caller tracebacks.
The API key remains server-side and is sent unchanged to the provider. Offline fake-key
tests cover actual HTTPX INFO logs, injected httpcore INFO traces, success, HTTP/provider
errors, connection errors, timeout and invalid responses.
Provider attempts count actual backend invocations, not retries or mere logical calls.
Normalized ReplayToolProvider calls report zero provider attempts.

## Replay and evaluation

ToolProvider is a common execute/previous interface. DirectToolProvider, MCPToolProvider
and ReplayToolProvider can be injected into the same collector. Async MCP users call acall;
synchronous workflow/evaluator users call execute. No use_mcp business branches.

ReplayToolProvider accepts normalized observations keyed by ToolQueryKey; missing fixture
is explicit REPLAY_MISS, malformed fixture INVALID_REPLAY, never real network fallback.
ReplayBackend supplies fixed raw responses through the real adapter. The 28 contract cases
cover 10/45 minute routes, numeric zero/strings, bool/nonfinite/missing data, provider errors,
timeout, stale-like timestamps, unsupported mode/strategy, reverse routes, POI searches,
empty results, exact lookup, ID mismatch and invalid/missing coordinates.

Evaluation runs real SDK clients against a loopback ASGI server with an OS-assigned port;
it does not invoke Python MCP handlers directly. It covers discovery, schema validation,
call/structured errors and disconnect. Tests additionally cover malformed peer output,
timeout, extra arguments, unknown tools, in-flight dedup and FastAPI lifespan mounting.

Metrics are fixture regression evidence, not external generalization, planning accuracy,
token savings or speedup. Direct/MCP share normalization intentionally; expected status and
reason assertions, raw malformed payload tests and independent model tests supplement
transport equivalence. No holdout or live supplier claims. Fresh timestamps are frozen for
comparisons. Only request_id is excluded from the direct/MCP result comparison.

## Reproduction and results

Use the existing environment or install backend/requirements.txt in a dedicated virtualenv.
From the repository root:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
$env:LLM_API_KEY=''
$env:AMAP_API_KEY=''
$env:REDIS_ENABLED='false'
$env:ENABLE_AMAP_ENRICHMENT='false'
python -B -m pytest backend/tests/test_tool_contracts.py backend/tests/test_tool_adapter.py backend/tests/test_mcp_gateway.py backend/tests/test_amap_secret_logging.py -q -p no:cacheprovider
python -B backend/scripts/evaluate_mcp_contract.py
python -B backend/scripts/evaluate_tool_grounded.py
python -B backend/scripts/evaluate_planning.py
python -B -m pytest backend/tests -q -p no:cacheprovider --basetemp "$env:TEMP/phase25-$([guid]::NewGuid().ToString('N'))"
```

Verified on 2026-10-06:

| Suite | Passed | Failed | Skipped | Collection errors |
| --- | ---: | ---: | ---: | ---: |
| Phase 2.5 targeted including hardening | 167 | 0 | 0 | 0 |
| Phase 2 targeted | 200 | 0 | 0 | 0 |
| Phase 1 targeted | 156 | 0 | 0 | 0 |
| Full backend | 611 | 2 | 0 | 0 |

Full failures are unchanged: test_retrieve_travel_guide_formats_chunks_as_text (8 vs 6)
and test_generate_day_edit_draft_accepts_nested_day_shape (mock missing json import).
Existing utcnow deprecation warnings remain. This is NOT full-suite passing.
Hardening adds 2 lifecycle cases (enabled/disabled, 3 cycles each) and 15 logging cases.
All three evaluators were rerun after these fixes; evaluation inputs and labels are unchanged.

Contract evaluation: 28/28 equivalent, expected status/reason 28/28, input/output schema
checks 28/28, fresh replay 28/28 deterministic, repeated same-run calls suppressed 28/28,
protocol responses 28/28. Discovery exposes exactly 3 tools. Controlled backend call-count
tests separately verify concurrent success and failure execution once.

Phase 2 evaluation: 48/48; Phase 1: 67/67, no mismatches.
SHA-256 (unchanged):
- planning_eval_cases.json: 6c3e73cccfff18688143b5c5d1ead7009baa1d4ce0cd17c854ddec299f37e0d1
- tool_grounded_eval_cases.json: 5c754f64b77a63dff046fdcdfc994d4be22016945c9b097ba5b6fe6a09379ffb

## Scope and claims

No Planner modifications, autonomous tool selection, new Agents, LangGraph, Jev,
Partial Replanning, Weather/RAG MCP, booking/purchase or state mutation tools.
No claimed planning-accuracy, token-cost or performance improvement.
No distributed dedup, production authentication, public deployment, real supplier test,
container build or production-scale concurrency certification.

Permitted claim: Unified POI and driving-route capabilities behind typed tool contracts
and exposed them through a minimal MCP gateway for reusable evaluation and external consumers.
