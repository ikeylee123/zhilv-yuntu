# Phase 1: Constraint State and Deterministic Plan Validator

## Scope and architecture

This phase adds inspectable constraints and deterministic validation to the existing
application. It does not make the Planner autonomous and does not repair a plan.

```text
TripRequest -> reject reversed dates before tools
            -> existing local RAG or dynamic POI generation
            -> existing map enrichment / cost calculation
            -> PlanningState -> validate_plan -> Itinerary + metadata -> API

Existing edit -> existing enrichment/cost refresh -> revalidate -> version + 1
Save -> recompute report from supplied state -> existing SQLite itinerary JSON
Load -> existing Pydantic deserialization (legacy metadata may be absent)
```

`app/services/plan_validator.py` imports only standard library modules and schemas.
It does not import app.config, open a database, call a model, read guide files or
access the network. `validate_plan` does not mutate its input. The activity adapter
reads existing DayPlan items; no new frontend activity protocol is required.

Both generation service functions attach validation after their existing final
cost calculation. The API retains the original submitted request (before city
normalization) in the metadata. No second LLM call is made to fix a violation.
Existing POI ID guards, RAG, map/weather, cache, cost algorithms, frontend and
export implementations are unchanged.

## Models and compatibility

Models extend `app/models/schemas.py`:

- `TripConstraints`: nullable structured hard requirements and soft preferences.
- `PlaceReference`: optional entity_id and name; at least one nonblank identifier.
- `ScheduledActivity`: day_index, activity_ref, activity_type, entity_id/name,
  raw start/end strings, estimated_cost. It is an adapter value, not a new UI model.
- `ConstraintViolation`: type, severity, message, day_index, activity_ref, expected, actual.
- `ValidationReport`: violations, unknowns, checked_constraints, and derived
  status, valid and deterministic summary.
- `PlanningState`: trip_request (optional for old records), constraints,
  current_itinerary, validation_report, version. It is ephemeral, not a workflow engine.

TripRequest gains an optional `constraints`. Itinerary gains optional
`original_request`, `constraints`, `validation_report`, `planning_version`.
Old requests/records remain parseable. Existing frontend objects retain additional
JSON fields while editing/saving; the UI does not yet present a validation panel.

### TripConstraints

| Field | Type | Meaning |
| --- | --- | --- |
| budget_limit | Decimal or null | Upper bound on existing estimated totals |
| must_visit | list of string/PlaceReference or null | Required attractions |
| avoid | list of string/PlaceReference or null | Forbidden attractions |
| daily_start_time / daily_end_time | string or null | Hard same-day time bounds |
| transport_preference | string or null | Explicit requirement, currently unknown |
| mobility_constraints | list of strings or null | Explicit requirements, currently unknown |
| hard_constraints | list of strings or null | Required check identifiers; unsupported ones are unknown |
| soft_preferences | list of strings or null | Stored, not scored or optimized |

All fields may be omitted or null. For generation, omitted budget_limit inherits
TripRequest.budget; omitted soft_preferences inherits TripRequest.preferences.
An explicit null overrides inheritance. An empty object inherits both values.
TripConstraints serializes overrides as an object containing only explicitly set
keys: absent stays absent, while explicit null remains a present JSON null. This
also applies to default nested model_dump/model_dump_json, so deserialization
retains presence rather than guessing whether a null used to mean missing.
At workflow normalization, constraints_from_request materializes inherited budget
and preferences as explicit resolved values persisted on Itinerary. Revalidation
keeps these values; absent/partial itinerary overrides inherit from the preserved
request, while explicit null still wins. No inheritance metadata engine is added.
A zero budget is a real limit, not a missing value. Negative/nonfinite limits and
unknown constraint field names are rejected by the input model. Raw time strings
are retained so malformed windows produce structured validation errors.

All named constraint fields other than soft_preferences are hard requirements.
For a soft transport preference, use soft_preferences instead. Existing pace,
dietary_preferences and hotel_level remain in original_request and are not scored.
`special_notes` and edit instructions are not parsed into constraints. Natural
language edits do not replace the stored budget limit or must_visit/avoid lists.

Example additional request field:

```json
{
  "constraints": {
    "budget_limit": "2500.00",
    "must_visit": [{"entity_id": "POI-123"}, "Museum A"],
    "avoid": ["Park B"],
    "daily_start_time": "09:00",
    "daily_end_time": "18:00",
    "hard_constraints": ["opening_hours"],
    "soft_preferences": ["photography"]
  }
}
```

IDs compare exactly and take precedence over the name in the same reference.
Bare strings mean names, not ambiguous ID-or-name values. Name normalization only
collapses whitespace and applies casefold. No fuzzy, substring, translation,
alias or semantic match is performed. ID-based requirements cannot be certified
by a matching name when the ID is absent or different. Different known IDs are
confirmed non-matches, even with equal names. If an ID-based reference encounters
an activity without an ID and with the same normalized name, identity is unknown:
avoid reports place_identity_unverifiable; must_visit does likewise unless another
activity provides a confirmed match. Clearly different names rule out that
candidate. With no usable name for comparison, a missing activity ID also remains
unknown. References without IDs retain normalized exact-name matching.

## Report semantics (not a real-world safety guarantee)

| Hard errors | Required unknowns | status | valid |
| --- | --- | --- | --- |
| none | none | valid | true |
| present | any | invalid | false |
| none | present | unverifiable | false |

Warnings alone do not invalidate. status, valid and summary are computed from the
findings, not trusted from client-supplied report fields. Unknowns are retained
when errors are also present. `checked_constraints` lists attempted checks, not
only passed checks. A successful report certifies only the Phase 1 scope and the
supplied estimates/identities; it never certifies real-world feasibility.

Generation still returns the itinerary with its report for invalid/unverifiable
results. Reversed request dates are the early fail-closed exception: HTTP 422
`invalid_date_range`, before city lookup or LLM. Direct generation services raise
ValueError. The standalone validator also reports reversed request ranges, so
synthetic inputs and supplied old state can be inspected.

## Rules and finding types

| Rule | Finding types | Behavior |
| --- | --- | --- |
| Dates | date_range, day_count, day_date, day_index | Ordered request dates; inclusive day count; dates within range and corresponding to unique consecutive day indexes |
| Clock values | time_format, time_order | Strict ASCII HH:MM, 00:00..23:59; start < end; no overnight intervals |
| Overlap | activity_overlap | Every overlapping pair of complete valid intervals within a day; adjacency is allowed |
| Cross-day duplicate | duplicate_attraction | ID when both are present, otherwise normalized exact name; only attractions |
| Required attractions | must_visit_missing | Required ID/name absent |
| Forbidden attractions | avoided_attraction | Forbidden ID/name present |
| Place identity | place_identity_unverifiable (unknown) | Required ID cannot be confirmed or ruled out from available identity evidence |
| Malformed place text | invalid_place_reference | Empty text is not silently treated as satisfied |
| Estimated budget | budget_exceeded | Decimal comparison of explicit summaries against limit; conservative max when both exist |
| Estimate disagreement | budget_estimate_mismatch (warning) | Report mismatch; use the larger supplied estimate without altering costs |
| Daily window | daily_time_window, constraint_time_format, constraint_time_order | Optional one-sided bounds; two-sided window must be ordered |
| Missing data | activity_time_missing, day_date, missing_trip_request, budget_unverifiable (unknown) | Incomplete interval/date/original request or missing/nonfinite budget summaries |
| Unsupported hard fact | unverifiable_hard_constraint (unknown) | Requested rule lacks supported facts, parameters or implementation |

Attractions without complete times are unknown. Legacy meals/hotels/transport
have no start/end schema; they are not assigned invented times. They are skipped
for ordinary interval checks, but become unknown when an explicit daily window
requires their schedule to be verified. Complete intervals are checked even if
another activity has unknown times.

Repeated meals/hotels are not errors. Distinct stable POI IDs with the same name
are not merged. The duplicate rule is across days, not within one day.

Budget checking is only a bound on the existing estimates, not real-price
validation. It does not change cost algorithms or infer room counts, nights,
actual tickets, fares or per-person totals. Decimal comparison uses the stored
numbers' string values; it does not silently round an excess down.

Budget evidence policy: use explicitly supplied estimated_budget, or explicitly
supplied budget_breakdown.total when it is the only summary. Explicit zero is
known zero. If both are supplied, retain mismatch warning plus conservative max;
neither source silently overrides the other. Missing both summaries with an active
limit yields budget_unverifiable (unknown), never a zero-cost success. A nonfinite
supplied summary retains the existing unknown policy. Numeric defaults remain for
legacy callers, but serializers omit absent summary keys so JSON/storage cannot
turn those defaults into evidence. Partial breakdown components are not summed:
the model does not guarantee a complete cost partition.

Recognized identifiers in hard_constraints are date_range, day_count, day_date,
day_index, time_format, time_order, activity_overlap, duplicate_attraction,
must_visit, avoid, budget_limit, daily_time_window. Parameterized requirements
without their required values remain unknown rather than vacuously passing.
Any other identifier (including opening_hours, route_feasibility,
real_travel_time, real_ticket_price, mobility_suitability) is unknown. No generic
natural-language hard constraint parser is implemented.

## State, edits and storage

Initial generation has planning_version=1. Existing edits retain original_request
and resolved constraints, then increment the version once and recompute the report.
Saving recomputes the report but does not increment the version or mutate the
caller object. Itinerary contains metadata directly: PlanningState is not embedded
recursively inside Itinerary.

The .gitignore exception includes only this phase document; other local notes remain ignored.

No SQL schema migration or engine change is required: the same itinerary_json
column stores all optional fields. Old rows load with null metadata. Saving or
editing an old plan with no original request reports missing_trip_request as
unknown; dates/constraints are not reconstructed from guesses.

Version is a local edit counter, not optimistic locking, event sourcing, history
or a trusted server-side session. Clients still supply the itinerary and its
metadata. Revalidation prevents stale success reports, not malicious alteration
of the constraints themselves. Existing trip_id collision/overwrite behavior
is unchanged. Export formatting is unchanged and does not newly display reports.

## Fixed synthetic evaluation

Dataset: `backend/eval/planning_eval_cases.json` (67 fixed cases).
Evaluator: `backend/scripts/evaluate_planning.py`.

| Category | Count |
| --- | ---: |
| valid | 15 |
| date | 5 |
| time format/order | 5 |
| overlap | 10 |
| duplicate | 5 |
| must_visit | 5 |
| avoid | 5 |
| budget | 5 |
| daily window | 5 |
| unknown | 6 |
| mixed errors + unknown | 1 |

These are synthetic rule fixtures, not a production benchmark, holdout quality
estimate or measure of travel planning accuracy. IDs, categories and expected
labels never enter predict(). Prediction completes from input alone before the
scorer reads labels. No LLM, external API, cache or database is used. Labels were
fixed with the scenarios before the first evaluation and were not revised to
obtain a perfect score. Every case remains in the dataset.

Metric semantics:

- Overall Validation Accuracy: exact three-way status accuracy across all cases.
- Binary validity: positive = cannot certify valid (invalid OR unverifiable).
  Precision=TP/(TP+FP), recall=TP/(TP+FN), FPR=FP/(FP+TN), FNR=FN/(FN+TP).
- Per-violation-type detection: presence/absence per case, independent of number
  of instances. Report TP/FP/FN, recall (detection_rate) and precision per type.
- Unknown Detection Rate: matched required unknown keys / expected unknown keys,
  including unknowns in mixed invalid cases; unknown precision also reported.
  Unsupported requirements use their exact identifier as the unknown key.
- A zero denominator produces null, not an invented perfect score.
- Mismatch output includes missing/extra finding types, not only status errors.
  The command exits nonzero on any mismatch.

First evaluation: 67/67 correct statuses, TP=52, TN=15, FP=0, FN=0. Precision,
recall, all represented violation detection rates and unknown detection rate
are 1.0; FPR/FNR are 0. No mismatches. This measures fixture conformance only.

## Reproduction and test evidence

From repository root, in a Python environment with the declared dependencies:

```powershell
$env:LLM_API_KEY = ''
$env:AMAP_API_KEY = ''
$env:REDIS_ENABLED = 'false'
$env:ENABLE_AMAP_ENRICHMENT = 'false'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest backend/tests/test_plan_validator.py backend/tests/test_planning_workflow.py backend/tests/test_planning_evaluation.py backend/tests/test_planning_hardening.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
python backend/scripts/evaluate_planning.py
```

On Windows, if an existing pytest temporary directory is inaccessible, add
`--basetemp` with a fresh task-specific temporary path. Do not point this option
at a directory containing user files: pytest manages its contents.

Implementation environment: Python 3.13, pytest from the existing interpreter,
Pydantic 2.13.5. A temporary system-site-packages venv supplied FastAPI 0.142.2,
SQLAlchemy 2.1.3, python-dotenv 1.2.4 and HTTPX 0.28.1. No system packages,
requirements file or lockfile were changed. LangChain/Chroma were not installed;
existing tests therefore exercise mocks and existing fallback paths, not live
models/vector infrastructure. This is not Python 3.11/container certification.

Before changes, the original environment had 11 collection errors from missing
FastAPI/HTTPX/dotenv. In the isolated environment, baseline was 88 passed,
2 failed, 0 skipped, 0 collection errors. The two pre-existing failures are:

1. test_retrieve_travel_guide_formats_chunks_as_text: mock expects candidate_k=8,
   implementation supplies 6 for top_k=2.
2. test_generate_day_edit_draft_accepts_nested_day_shape: mock invokes json.dumps
   without importing json; the Planner catches the NameError and returns None.

Neither old test nor unrelated implementation was modified to hide these failures.
Phase 1 targeted tests: 118 passed. Full suite: 206 passed, the same 2 failed,
0 skipped, 0 collection errors. Existing datetime.utcnow deprecation warnings
remain. Added storage tests use an isolated temporary SQLite database. Existing
upstream tests use the configured backend/db and can create ignored DB artifacts.

Hardening re-run (2026-10-06): 38 dedicated adversarial regression instances
in test_planning_hardening.py cover override presence, budget evidence, identity
uncertainty and JSON/SQLite/edit/save round-trips. Targeted: 156 passed. Full
backend/tests: 244 passed, the same two baseline failures above; no new failures.
The fixed 67-case dataset is byte-for-byte unchanged and still has 67/67 correct
statuses with no finding-type mismatches. This is a fixed synthetic regression
set, not production accuracy. No important-issue fixes outside those three
critical corrections were included.

## Explicit limitations

- No opening-hours, closure-event, route feasibility, real travel-time, weather,
  real ticket-price or mobility-suitability validation. Required facts yield unknown.
- No soft preference satisfaction score or optimization.
- No partial replanning, autonomous tool loop, automatic repair or regeneration.
- No LangGraph, Multi-Agent, MCP, new retrieval stack, MySQL, memory, context
  compression or LLM judge.
- No NLP extraction from special_notes or edit instructions.
- No time zones or overnight activity/window support.
- Candidate provenance and ID validity retain their existing generation guards;
  this validator does not independently confirm an arbitrary supplied POI exists.
- Unknowns can be common for daily windows because old non-attraction items do
  not contain scheduling fields; this is intentional and not a successful check.
- No new UI validation panel or validation details in exported documents.
