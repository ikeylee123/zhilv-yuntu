# Phase 2: Route-grounded Feasibility

## Scope and opt-in

Phase 1 baseline: d274862f2ea1d4b32941cf828bba18938ce49b9a.
Branch: feat/tool-grounded-feasibility.

Enable the hard route contract with TripConstraints.require_route_feasibility=true
and transport_preference="driving". Missing/false/null route requirement preserves
Phase 1 behavior: no route collection or route errors, even if old metadata contains
a route violation. Existing standalone transport_preference semantics remain when
the new contract is disabled. Under the route contract, unsupported/missing modes
produce route_feasibility_unverifiable instead of substituting driving.

RouteFact is a provider estimate, not real-time or future traffic guarantee.
Current itinerary structure often yields zero route-checkable activity pairs.

No Planner changes, extra activities, autonomous calls, repair or regeneration.
No MCP, LangGraph, Multi-Agent, Jev, partial replanning, route optimization,
walking/transit/cycling/taxi feasibility, weather/opening-hours checks, live prices
or LLM judge. The root README and Phase 1 fixed dataset are unchanged.

## Architecture

```text
Existing generation/edit -> final location/time/map/cost updates
    -> attach_server_validation
    -> resolve_constraints (same Phase 1 inheritance)
    -> discover_pairs -> bind identities -> collect_route_facts
    -> immutable PlanningFacts snapshot
    -> attach_validation -> pure validate_plan -> check_routes
    -> ValidationReport.route_coverage + Itinerary.planning_facts

Save -> deep copy -> same server validation boundary -> existing itinerary JSON
Load -> historical JSON snapshot; not a new live feasibility certification
```

Collection performs I/O; neither validator nor pair/fingerprint helpers import
config, HTTP, Redis or storage. evaluation_time is an explicit pure-validator
input; the server workflow supplies UTC time after collection. Fixtures supply a
fixed time, so replay does not depend on wall-clock time.

## Fact contracts

Models live in backend/app/models/planning_facts.py. Facts and nested models are
frozen; collections are tuples rather than mutable lists/dictionaries.

- FactStatus: known, unknown, stale, conflicting.
- FactSource: amap, /direction/driving, v3, strategy=0, fetched_at (aware datetime),
  evidence_kind=provider_estimate. No API key or complete HTTP response is stored.
- ActivityReference: stable-in-snapshot activity path, day index/date, attraction
  type, POI ID/name, start/end, optional finite GCJ-02 coordinates.
- RouteFact: directional endpoints, driving mode, finite nonnegative meters and
  seconds, source, reason. Known requires both IDs/coordinates, distance, duration
  and timestamped source. A successful HTTP response alone is insufficient.
- PlanningFacts: run_id, plan_fingerprint, collected_at, schema/policy versions,
  route_facts, collection_outcomes and call metrics.

Unknown facts still use the driving route schema. An unsupported requested mode
is preserved in constraints and reported by reason; no driving query was made.
Malformed known facts are rejected at model construction. The collector converts
malformed upstream fields into unknown/invalid_response before building facts.

## Pair discovery and coverage

Within each day, sort attractions by valid start_time, then form adjacent directed
pairs. Never add reverse routes unless independently required. Meals, hotels,
transport items and cross-day movements are outside this contract. If a day's
attraction interval is missing/invalid, skip that day's potential links rather than
bridge across an activity whose ordering cannot be established. Report the schedule
gap as unknown when the route contract is required.

Temporal candidate pairs remain required even if coordinates/identity are missing;
otherwise dropping difficult pairs would falsely improve coverage. Binding and mode
checks decide which of these candidates can actually be queried.

RouteCoverage definitions:

- required_pairs: temporal pairs requiring the opted-in route check.
- verified_pairs: pairs with applicable known estimates actually compared; includes
  verified violations, not just passing pairs.
- unknown_pairs: required pairs that could not be checked.
- skipped_pairs: potential links excluded for invalid/missing times, or temporal
  pairs not checked because the contract is disabled.
- reasons: explicit coverage/unknown causes. Scope is
  same_day_adjacent_timed_attractions, not the entire itinerary.
- verified_fraction: verified/required, null (N/A) for a zero denominator. This ratio
  describes discovered temporal pairs only; skipped links and unsupported activity
  classes must not be hidden by quoting the fraction alone.

A required contract with zero pairs reports insufficient_schedule_coverage and
unverifiable. A day with one timed attraction cannot establish transport feasibility.
An unknown caused by zero pairs is reported even though unknown_pairs itself is zero.

## Identity and coordinates

Dynamic generation passes a server-only selected candidate map to collection.
Its exact POI ID, normalized name and coordinates must agree with each endpoint.
No name search or first-result substitution occurs in this path.

Local generation's name-based enrichment is not trusted binding evidence. Even
when it produces coordinates/IDs, the route collector reports identity_uncertain
rather than certifying the inferred identity.

Edit/save discard client facts. If pairs exist, they rebind explicit itinerary
POI IDs using the same provider's /place/detail endpoint, requiring the returned ID,
normalized exact name and coordinates to agree. No name search is used. This proves
binding to the provider entity, not that a same-name entity was the user's intended
choice, nor that the place is open. Missing, changed or ambiguous identity returns
unknown. Coordinates must be finite and within latitude/longitude bounds.

The new ID lookup is separate from route-call metrics; identity_provider_calls
records its calls. Repeated IDs are resolved once per collection run. Disabling the
legacy map-enrichment switch does not disable explicitly requested route collection.

## Freshness and cache

ROUTE_FACT_MAX_AGE_SECONDS=900 and ROUTE_BUFFER_SECONDS=0 are versioned policy
constants. Fifteen minutes is an engineering reuse policy, not a traffic accuracy
promise. No future departure timestamp is sent or invented.

The new map adapter bypasses the legacy 24h route cache. Phase 2 uses the namespace
planning:route:v1 with a hash of provider, API version, driving mode, strategy=0,
ordered exact coordinates and coordinate system. The cache stores this query
identity, finite values and the original source timestamp. It expires after 900s.

Never reset fetched_at on a cache hit. Missing timestamps, wrong query identities,
malformed cache payloads and future timestamps are not reusable; refresh upstream.
An expired usable previous estimate can be retained as stale if refresh fails;
collection_outcomes retains the refresh failure separately. Ordinary Redis expiry
may remove it entirely, in which case failure produces unknown. No previous estimate
is relabeled known. Identical run-local queries, including failures, are deduplicated.

A snapshot fingerprint binds day/date, attraction identity/name, coordinates,
start/end, mode and opt-in. Descriptions/tips do not affect it. Array positions are
part of activity references; reordering may conservatively invalidate old snapshots.
Fingerprint, schema or policy mismatches prevent old facts from certifying a new plan.

## Validation and failure semantics

Only the explicit route contract adds hard route findings:

- route_time_violation (error): available_seconds = B.start - A.end;
  required_seconds = duration_seconds + 0. Compare exact stored second estimates
  via Decimal(str(value)), never rounded display minutes. Equality passes.
- route_feasibility_unverifiable (unknown): absent/stale/conflicting evidence,
  wrong pair/fingerprint, missing coordinates/time, identity uncertainty,
  insufficient coverage, unsupported mode or collection failure.

Reasons include missing_route, no_data, timeout, provider_error, invalid_response,
stale_fact, conflicting_fact, identity_uncertain, missing_coordinates,
unbound_activity_pair, unsupported_mode, outdated_policy,
missing_activity_time, missing_evaluation_time and insufficient_schedule_coverage.

Collection outcomes are success, no_data, timeout, provider_error,
invalid_response or unsupported. HTTP success plus valid numeric fields and binding
can produce known. Identical duplicate observations count once per pair; incompatible
observations for the exact pair cannot become last-write-wins and produce conflicting
unknown. The sequential deduplicating collector normally emits one observation per
query; the pure validator also handles externally assembled trusted offline snapshots.

Preserve invalid > unverifiable > valid. Warnings alone do not invalidate. Known
route estimates prove only an interval comparison, not actual travel completion.

## Persistence and trust boundary

PlanningFacts is optional itinerary JSON metadata: no table/schema migration or
separate fact database. Existing rows remain readable. Loaded facts/report describe
a historical run and may now be stale; load does not call tools or recertify them.

Client facts and validation_report never supply trusted evidence to edit/save.
Those paths collect new server evidence (a fresh server cache may be reused) and
replace the snapshot/report. The public JSON cannot set trusted_pois. Direct pure
attachment ignores itinerary.planning_facts unless a caller explicitly supplies
facts; production callers use the server boundary. This is not server-authoritative
constraint anti-tampering or optimistic concurrency control.

## Offline evaluation and tests

backend/eval/tool_grounded_eval_cases.json contains 48 fixed synthetic cases:
16 route arithmetic/mode, 8 identity/binding, 8 freshness/conflict, 8 normalized
failures, 8 combined/scope. Expected labels were declared without calling the
validator; IDs/categories/expected never enter predict().

Several raw tool failures normalize to the same unknown shape. These are not
independent validator algorithms: dedicated collector tests separately exercise
empty paths, missing/negative/nonfinite/partial responses and provider exceptions.
Fixtures reuse templates, so a perfect score is regression conformance, not a
holdout estimate or production planning accuracy.

Metrics: exact three-state and binary validity accuracy (positive=not valid),
per-type precision/recall, route violation and unknown detection, unknown precision,
known_fact_utilization_rate against independently declared eligible known pair
counts, unexpected_verified_pairs, and raw coverage counts. Utilization is a
pair-count metric, not a causal trace of each individual observation. Zero metric
denominators produce JSON null (N/A). Coverage and mismatch diagnostics accompany
accuracy so unknown-heavy data cannot be advertised as fully feasible planning.

Collector metrics record eligible logical_route_queries (before run-local dedup),
unique cache_hits, actual_provider_calls for driving, deduplicated_calls and separate
identity_provider_calls. unnecessary_calls counts route calls exceeding unique
uncached eligible queries; tests additionally forbid calls for unsupported modes,
missing coordinates/identity and zero pairs. No network latency KPI is claimed.

Reproduction from repository root, with existing declared dependencies:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:LLM_API_KEY=''
$env:AMAP_API_KEY=''
$env:REDIS_ENABLED='false'
$env:ENABLE_AMAP_ENRICHMENT='false'
python -B -m pytest backend/tests/test_route_facts.py backend/tests/test_planning_fact_service.py backend/tests/test_route_workflow.py backend/tests/test_tool_grounded_evaluation.py backend/tests/test_route_hardening.py -q -p no:cacheprovider
python -B backend/scripts/evaluate_planning.py
python -B backend/scripts/evaluate_tool_grounded.py
python -B -m pytest backend/tests -q -p no:cacheprovider
```

Use a fresh task-specific --basetemp if required by Windows permissions. The existing
upstream full suite may write ignored backend/db artifacts; new route storage tests
use an in-memory database. No live supplier, model or Redis connection is required
for Phase 2 tests. No new dependencies were installed.

## Verification record (2026-10-06)

Environment: existing temporary Python 3.13 venv with the Phase 1 test dependencies;
no package installation or dependency changes. This is offline regression evidence,
not live-provider validation, a performance SLA or container/Python 3.11 certification.

| Check | Passed | Failed | Skipped | Collection errors |
| --- | ---: | ---: | ---: | ---: |
| Phase 1 targeted | 156 | 0 | 0 | 0 |
| Phase 2 targeted | 148 | 0 | 0 | 0 |
| Full backend/tests | 392 | 2 | 0 | 0 |

The 148 new instances expand 42 test functions, including 48 fixed-case instances.
Full-suite failures remain exactly the pre-existing candidate_k expectation (8 vs 6)
and the nested-day-edit test mock missing its json import. Those tests and their
unrelated implementations were not changed. Existing utcnow warnings remain.

Phase 1 fixed evaluation: 67/67, no mismatches, original file byte-for-byte unchanged.
Phase 2 fixed evaluation: 48/48, no status/type/reason/coverage mismatches. Binary
confusion TP=35, TN=13, FP=0, FN=0. Three-state/binary accuracy, violation precision/
recall, both route detection rates, unknown detection/precision, and eligible-known
pair utilization are 1.0 on this fixture set. Unexpected verified pairs: zero.
Coverage: required=48, verified=19, unknown=29, skipped=0; verified fraction=19/48
(39.58%). Verified includes pairs with confirmed violations. Zero-pair case ratios
remain null. These numbers do not measure production planning accuracy.

The 48-case file was frozen before evaluation and retains SHA-256:
5c754f64b77a63dff046fdcdfc994d4be22016945c9b097ba5b6fe6a09379ffb.

## Limitations and permitted claim

Current itinerary structure often yields zero route-checkable activity pairs.
No forced multi-attraction generation or invented activity times improve this number.
All itinerary activities, entire journeys, real-time traffic, future traffic,
parking/entry delays, walking/transit/taxi availability and actual prices remain
outside this validation scope. Legacy rendering/enrichment is unchanged and may
still show heuristic transport text; it is never treated as route fact evidence.

Permitted claim: Added server-grounded driving route facts and deterministic
inter-activity travel-time validation with explicit unknown handling and coverage
reporting. Do not claim full itinerary real-world feasibility, full transport
feasibility, route optimization or production planning accuracy.


## Minimal route fact hardening

Boolean distance/duration values are explicitly rejected before numeric coercion
at provider/cache normalization (CachedRoute) and direct RouteFact validation.
Finite nonnegative numeric strings and numeric zero remain supported. Invalid raw
provider values produce unknown / invalid_response and do not count as verified;
other hard errors retain precedence over unverifiable.

Known endpoints require nonnegative day_index and nonblank entity_id, activity_ref
and name. This does not authorize local name-only grounding; unknown endpoints may
remain incomplete. RouteCoverage requires verified_pairs + unknown_pairs <=
required_pairs; skipped_pairs stays independent and zero required yields null fraction.

Independent raw-provider, cache, model and server-validation regressions are in
backend/tests/test_route_hardening.py. The existing 48-case and 67-case datasets
and their expected labels are unchanged.

Hardening verification (2026-10-06): the 52 new regression instances produced
25 failures / 27 passes before the fix; all 52 pass after it. Phase 2 targeted:
200 passed. Phase 1 targeted: 156 passed (a fresh --basetemp resolved an initial
Windows temporary-directory permission error). Fixed evaluations: 48/48 and 67/67,
with both dataset hashes unchanged. Full backend: 444 passed, the same two baseline
failures described above, no new failures. Existing utcnow warnings remain.
