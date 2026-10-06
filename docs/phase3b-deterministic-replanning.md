# Phase 3B: Deterministic Replanning Core

## Baseline and scope

Branch: `feat/constraint-preserving-replanning`. Phase 3A frozen baseline:
`58a133c12aa4e7932da3ce7c82d626892ad39057` (initial working tree clean).

One request changes one attraction in one day of a managed authoritative trip.
Supported operations: `replace`, `remove`, `add`, `modify_time`. No LLM, natural
language parsing, repair loop, global enrichment, cross-day move, constraint
editing, meal/hotel replanning, Weather, Opening Hours or new agent framework.
Candidate selection is an explicit client/test choice from server-authorized IDs.

## Contracts and API

Models: `backend/app/models/replanning.py`. All public request models forbid extra
fields. Versions and day indexes require positive integers, excluding booleans and
strings. Times use HH:MM with an increasing same-day interval. A request cannot
supply coordinates, names, new activity IDs, costs, facts, reports, constraints or
new state versions. The body trip ID must equal the path trip ID.

`POST /trip/{trip_id}/replan/candidates` accepts:

```json
{
  "trip_id": "server-trip-id",
  "base_plan_version": 1,
  "target_day_index": 1,
  "change_type": "replace",
  "target_activity_id": "server-activity-id",
  "preference": "attraction"
}
```

For add, omit target_activity_id. Preference is attraction, museum or park.
The response is an AllowedCandidateSet with candidate_set_id and up to ten
GroundedCandidate objects (poi and explicitly estimated cost).

`POST /trip/{trip_id}/replan` accepts:

```json
{
  "trip_id": "server-trip-id",
  "base_plan_version": 1,
  "target_day_index": 1,
  "change_type": "replace",
  "target_activity_id": "server-activity-id",
  "candidate_set_id": "server-grant-id",
  "candidate_id": "provider-poi-id-from-grant"
}
```

Remove requires target_activity_id only. Modify_time adds start_time/end_time and
forbids candidates. Add requires candidate IDs, start_time/end_time, and exactly
one of before_activity_id or append=true; it forbids target_activity_id. Replace
preserves the old time slot and forbids time overrides.

Replan decisions return HTTP 200 with decision/reason/base_plan_version,
optional committed candidate_plan_version, validation_report, scope,
change_summary, and committed itinerary only on success. Malformed bodies/path
mismatch return 422. Candidate preparation uses existing state error mapping
(404 for missing state, otherwise 409). Earlier failures can have no report because
merge/full validation never ran. Legacy managed /edit stays forbidden; legacy save
still cannot overwrite the managed core. No frontend UI or dependency change.

## Authorization and candidates

The loader uses the frozen Phase 3A managed payload parser in a read-only session.
It refuses legacy rows without invoking lazy migration. Scope is generated before
merge and binds trip, version, day, operation, target and preserved activity IDs.
Cross-day anchors and targets, unknown IDs, and stale versions are rejected.
Unique must_visit removal is rejected before tools. Replacement waits for the
authorized selected candidate before evaluating whether must_visit remains satisfied.
Search filtering and duplicate prechecks exclude the target being replaced; preserved
activities still participate. Avoid and exact ID/name conflicts are checked before detail lookup.

Preparation resolves the server destination to a city/district adcode through the
existing city resolver, then calls ToolProvider.search_poi once with page_size=10.
Uncertain regions fail closed. Municipalities use their province-level prefix;
ordinary cities use four digits, districts six. Conflicting duplicate IDs, already
selected POIs, avoid matches, missing coordinates, wrong region/category and
obvious accommodation/restaurant provider types are excluded. No fuzzy ranking.

Grants are random server IDs in a locked, bounded 128-entry in-process cache.
They bind trip, base version, day, operation and replacement target. Both grant age
and provider search age must be within 300 seconds, never in the future. Restart,
eviction or another worker can make a grant unavailable: prepare again. Production
multi-worker deployment needs sticky routing or a shared grant store; no distributed
cache is introduced here. Grants are authorization capabilities within existing
application access control, not a newly introduced user authentication system.

Replace/add looks up only the selected POI through ToolProvider.get_poi. Exact ID,
normalized name, coordinates and fresh typed response must agree with the grant.
Successful lookup alone never authorizes an arbitrary provider ID. Missing detail
category/adcode retains search evidence; explicit disagreement rejects grounding
with GROUNDING_CONFLICT. ID/name/coordinate conflicts also reject, while timeout or
missing evidence stays unverifiable. Neither source silently wins an explicit conflict. Remove/time perform zero POI searches and
zero POI detail calls. City resolution is separate preparation overhead and can
itself use the existing administrative-area provider.

## Merge, identities and preservation

PlanPatch is an internal discriminated union, never an HTTP input. Merge starts
from a deep copy of authoritative state. Replace generates a new server activity ID
and retires the old ID; add generates a new ID; remove deletes its ID; modify_time
retains identity and all fields except start/end. No neighbor times are shifted.
The insertion anchor controls list position; chronological route discovery still
sorts actual times, so insertion position cannot hide overlap or route violations.

assert_preserved_projection reconstructs the authorized activity edit and compares
all preserved business fields. It never invokes the transport/budget mutation helper.
A separate pure AllowedDerivedChanges policy authorizes exact transport indices,
cleared estimate fields, added estimated segments and direct cost deltas. The assertion
checks retained transports, additions, costs and the full business projection against
that policy independently; a broken mutator cannot authorize its own output.

Transport endpoints are classified as attraction, hotel, meal, ambiguous or unbound.
Fallback binding uses the existing case/whitespace normalization (no new punctuation
or fuzzy matching). Same-name attractions/hotels/meals remain ambiguous. An affected
ambiguous endpoint or a target segment whose ownership cannot be proven fails with
TRANSPORT_BINDING_UNVERIFIABLE; the authoritative row stays unchanged. Unrelated
hotel/airport transfers are preserved exactly, including their endpoint strings.

Replace/remove/add can remove only uniquely attraction-bound obsolete adjacent
segments. Time reordering retains existing transport endpoint identity and estimated
cost, clearing only obsolete distance/minutes/duration display estimates. It may add
newly required segments with explicit estimated costs. Unrelated fields never change.
A target-to-unbound/hotel/nonadjacent segment fails closed rather than being guessed.

For each newly required chronological relationship, the service first seeks an exact,
uniquely bound existing transport estimate with compatible mode. An explicit numeric
zero is valid evidence under the existing estimate model. Otherwise, a Known driving
observation can supply distance to the existing _apply_route_based_transport_costs
heuristic, called on an isolated new segment, never the whole itinerary. The existing
default driving formula is 10 + 2.2 * distance_km, rounded to two decimals. This is an
explicit heuristic estimate, never a provider fare or real-price claim. Candidate
response change_summary identifies this policy. Each logical new relationship gets
its own segment estimate even when provider queries are deduplicated.

Without applicable cost evidence, no zero-cost segment is created. The internal
policy records unknown_cost_pairs, and the decision reports transport_cost_status
and unknown_transport_cost_pairs for affected new relationships. With a hard budget,
budget_unverifiable is appended after full validation, so other hard errors still
win (invalid > unverifiable > valid). Without a hard budget the operation may commit,
but its response explicitly retains the unknown cost state. Disabling route validation
does not disable the separate requirement for new cost evidence under a hard budget.

Budgets change only by the target ticket delta, removed authorized transport estimates
and newly evidenced segment estimates. No whole-plan budget allocation is performed.
Time edits without adjacency changes keep costs unchanged; reordering retains old
segment costs and accounts for newly required segments conservatively. Missing or
nonfinite necessary aggregate evidence fails closed.

For legacy persisted items where missing and explicit zero have already collapsed to
0.0, the system cannot reconstruct the original evidence state. This round adds no
migration or fabricated provenance. New route cost handling does not repeat that
collapse: it creates an explicitly estimated segment or records unknown.

## Fact differences, reuse and collection

Old/new required queries come from the frozen chronological adjacent-attraction
pair discovery and typed driving query keys (direction, coordinates and policy).
No fixed two-edge assumption or route matrix. Time reorder may change multiple
queries. ReplanningScope reports old/new keys, invalidated keys, reused keys and
keys needing recollection. eligible_reuse_queries is the initially compatible fresh
set. reused_queries now means actual reuse: observations really inserted as Known
without recollection and still fresh at snapshot completion. recollected_queries
records unique attempted query keys, including failed attempts; unknown_queries
records keys left unknown. Counts are unique query keys, not logical pair counts.
Unsupported modes, unusable identity and observations expiring during collection
cannot count as actual reuse.

Reuse first checks base snapshot fingerprint, schema/policy and collection time.
Only facts bound to actual complete base endpoint references can supply observations.
Conflicting duplicate observations are excluded. Known historical evidence can
establish endpoint identity even when its route estimate is stale; only fresh
observations (age <= 900 seconds) can be reused as route estimates. Fresh reused
observations keep fetched_at, source, duration and distance; endpoint references
are rebound into a new snapshot with a new run ID and candidate fingerprint.

New required queries minus compatible fresh observations are candidates for
recollection, including unrelated stale routes. Each query is sent at most once in
the orchestration, through DirectToolProvider in production. Missing coordinates,
unsupported mode or unproven identity produces unknown evidence without fabricating
facts or making unnecessary POI calls. Thus facts_to_recollect is the evidence
shortfall, not a promise that every key resulted in a provider call. Tool metadata
records actual reported attempts/cache hits/deduplication separately.

An isolated POI with no trustworthy old identity evidence can prevent certification
of a newly required edge. The operation then remains unverifiable; there is no
silent identity refresh for remove/time. A foreign or corrupt snapshot is never
made valid by replacing only its fingerprint. Full validation uses the current
clock after collection, so an observation expiring during tool calls cannot commit.

## Validation, decision and persistence

The frozen full deterministic validator checks dates, times, overlap, cross-day
duplicates, must_visit, avoid, budget, windows, supported route evidence and unknown
hard requirements. An additional exact ID/normalized-name guard rejects same-day
duplicates introduced by the patch. The guard compares stable-ID duplicate
relations before and after the patch; unchanged pre-existing same-day duplicates do
not retroactively block unrelated edits. Candidate selection compares against the
post-replacement preserved set. The frozen cross-day validator is unchanged. Disabling route validation
while retaining a transport-mode hard requirement can still be unverifiable under
the frozen validator; no inherited or explicit-null constraint is silently changed.

Only a valid candidate enters the frozen compare_and_swap_plan primitive, which
validates again and conditionally updates the matching base version. All network
work happens after read sessions close and before the short CAS transaction.
Initial version checking never replaces final CAS. There is no automatic rebase.
CAS is an authoritative validated storage primitive, not a replanning scope gate.
ReplanningService owns scope, preservation, duplicate delta and new-cost evidence
authority. Arbitrary candidate CAS remains internal and is not exposed by an API.
Invalid returns rejected; missing evidence returns unverifiable; CAS/version loss
returns conflict. Failed requests preserve the complete original DB row, or preserve
a competing writer's winning row. Accepted responses include minimal change_summary;
no optional new persistent metadata schema is introduced.

Stable reasons include VERSION_CONFLICT, REPLAN_NOT_ELIGIBLE,
INVALID_CHANGE_REQUEST, ACTIVITY_NOT_FOUND, ACTIVITY_DAY_MISMATCH,
CANDIDATE_SET_NOT_FOUND, CANDIDATE_SET_EXPIRED, CANDIDATE_NOT_AUTHORIZED,
CONSTRAINT_CONFLICT, DUPLICATE_ATTRACTION, UNAUTHORIZED_PLAN_MUTATION,
GROUNDING_FAILED, GROUNDING_CONFLICT, TRANSPORT_BINDING_UNVERIFIABLE,
REPLAN_UNVERIFIABLE and REPLAN_VALIDATION_FAILED. Corrupt managed
state retains the existing Phase 3A machine codes.

## Offline verification

New tests are split across test_replanning_scope, test_plan_patch_service,
test_replanning_preservation, test_replanning_candidates, test_replanning_facts,
test_replanning_commit, test_replanning_tool_efficiency and test_replanning_evaluation.
They use typed fake tools and temporary SQLite databases, including complete row
comparisons and a concurrent writer during route collection. Required five service
scenarios plus strict HTTP body/path checks are covered.

Run `python -B backend/scripts/evaluate_replanning.py`. It owns a temporary database
and makes no external provider/LLM calls. Fifty explicit JSON cases in
backend/eval/replanning_eval_cases.json contain base inputs and independently written
expected labels: 10 replace, 8 remove, 8 add, 8 modify_time, 8 version/security,
8 fact/adjacency. The evaluator reads labels; it cannot generate or overwrite them.
Its server-side candidate grants are fixture setup, while separate tests exercise
real candidate preparation and filtering. Metrics report numerator/denominator;
early prechecks have null status instead of a manufactured validator report.
Unauthorized detection and version-conflict metrics have smaller applicable
populations; their 100% values are fixture regression results, not intelligence or
production coverage claims.

Frozen Phase 1/2/2.5 labels, validators and Phase 3A persistence code are unchanged.
Full backend has two pre-existing failures: RAG retrieval top-k expectation (8 versus
6) and a day-edit mock missing json import. Neither is changed by this phase.
No commit or push is performed. Review this working tree before committing.

## Correctness hardening verification (2026-10-06, prior to semantic reconciliation)

New test_replanning_correctness_hardening.py covers transport ambiguity, normalized
endpoints, independent preservation, explicit/unknown costs, search/detail conflict,
must_visit timing, duplicate deltas and actual reuse. No adversarial addendum dataset
was added: targeted regression tests cover the audit counterexamples.

The original 50-case inputs and planning outcome labels remain unchanged. One metric
label was corrected: modify_time-07 expected_reused_queries changed from A>B, B>C,
C>D, E>F to [] because walking produces Unknown evidence, not actual reuse. Dataset
SHA256 before: 6d0e77805abc22b6840ba4d0785b600a1e1b986f521280e62c5340d683d4b2b4.
After: e4d31474f0dfbb3370e77b2e1dba45c06ffe5fb65916e88376130d15e3d192bb.
No other case field was changed. The evaluator implementation was not changed.

At that point, the remaining benchmark disagreement was facts-08 disables route validation and mode,
but retains budget_limit=1000. Removing B creates A>C with no applicable cost evidence.
The corrected service returns unverifiable and preserves the row; the old label still
expects accepted/valid/active change. This is reported as a failure, not hidden with
an xfail, rewritten planning outcome label, fabricated zero cost or extra provider call.

Pre-reconciliation re-runs:

| Check | Result |
| --- | --- |
| New correctness hardening tests | 50 passed |
| All Phase 3B targeted | 194 passed, 1 failed (facts-08) |
| Replanning evaluator | 49/50 cases match; facts-08 remains a reported outcome mismatch |
| Scope / preservation / merge / fact metrics | 50/50 each |
| Failure-row preservation | 27/27 |
| Phase 3A targeted | 83 passed |
| Phase 2.5 targeted / MCP evaluator | 167 passed / 28 cases, no mismatches |
| Phase 2 targeted / route evaluator | 200 passed / 48 cases, no mismatches |
| Phase 1 targeted / planning evaluator | 156 passed / 67 cases, no mismatches |
| Full backend | 888 passed, 3 failed |

The three full-suite failures are facts-08 plus the two pre-existing failures below.
No result is suppressed or relabeled to claim a green suite. In the exact 160-total
bridge counterexample, absent cost evidence now yields unverifiable/no commit. When
the existing heuristic applies to a Known 1km driving observation, the new bridge
has an explicit estimated cost of 12.20 and total 62.20, rather than silently zero.
That estimate may be valid under a 100 budget, but is not real fare validation.

Frozen Phase
1/2/2.5 data and Phase 3A CAS remain unchanged. The two pre-existing full-backend
failures remain the RAG top-k expectation and day-edit mock missing json import.

## Benchmark semantic revision (2026-10-06)

Independent correctness hardening was rerun first: 50 passed before changing any
label in this reconciliation. Only then was the benchmark expectation reconciled.

facts-08 previously expected valid/accepted for a newly required A>C transport leg
without cost evidence, effectively treating the missing cost as zero. Its effective
budget_limit remains 1000 even though route feasibility is disabled. Missing usable
transport-cost evidence under that hard budget is budget_unverifiable: the result
is unverifiable and the active authoritative plan remains unchanged.
Only expected_decision (accepted -> unverifiable), expected_status (valid ->
unverifiable), and expected_active_plan_changed (true -> false) changed in this case.
Its empty query expectations remain unchanged; the schema has no finding label.

This is a correctness semantic label revision. Separately, the retained
modify_time-07 expected_reused_queries correction (A>B, B>C, C>D, E>F -> []) fixes
metric definition: eligible reuse is not actual reuse of Known supported evidence.
Cumulatively, exactly facts-08 and modify_time-07 changed. All inputs, category
counts (10 replace; 8 each remove/add/modify_time/version_security/facts), and other
expected labels are unchanged. Production logic and evaluator code were not changed
to recover benchmark accuracy. A legitimate deterministic estimate is estimated
cost, not a real fare; it can enter budget validation without inventing zero cost.

Dataset SHA-256:
- Original before both corrections: 6d0e77805abc22b6840ba4d0785b600a1e1b986f521280e62c5340d683d4b2b4
- Before this reconciliation: e4d31474f0dfbb3370e77b2e1dba45c06ffe5fb65916e88376130d15e3d192bb
- Final: d4eb8714a101e84a8e12c719f5711682d2cb3c40f107b16e2aeaf7b835bf6c6f

Post-reconciliation verification:

| Check | Result |
| --- | --- |
| Independent hardening, before label edit | 50 passed |
| Phase 3B targeted (includes hardening) | 195 passed |
| 50-case deterministic replanning regression | 50/50; no mismatches |
| Phase 3A targeted | 83 passed |
| Phase 2.5 targeted / MCP evaluator | 167 passed / 28 cases, no mismatches |
| Phase 2 targeted / route evaluator | 200 passed / 48 cases, no mismatches |
| Phase 1 targeted / planning evaluator | 156 passed / 67 cases, no mismatches |
| Full backend | 889 passed, 2 pre-existing failures |

The full-backend failures remain test_rag_retriever.py::
test_retrieve_travel_guide_formats_chunks_as_text (top_k actual 6, expected 8) and
test_trip_planner_agent.py::test_generate_day_edit_draft_accepts_nested_day_shape
(mock missing json import). The earlier facts-08 disagreement is resolved.

Independent passing tests explicitly verify ambiguous hotel transfers preserve the
row, unknown bridge costs under hard budgets are unverifiable, applicable estimated
costs enter validation, explicit category/region grounding conflicts are rejected,
valid must_visit-preserving replacements are allowed, unrelated legacy duplicates
do not add a blocker, and eligible reuse differs from actual reuse.

50-case deterministic replanning regression: 50/50. This is a fixed offline
regression set after two documented semantic expectation corrections discovered
through independent adversarial testing; it does not establish general replanning
accuracy. Phase 3B is READY TO COMMIT with the two disclosed baseline failures.
No commit or push was performed.
