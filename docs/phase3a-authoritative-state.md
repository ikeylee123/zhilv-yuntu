# Phase 3A ? Authoritative State Foundation

## Baseline and scope

Branch: `feat/constraint-preserving-replanning`.
Frozen baseline: `7102211c0044f826e3250c70d1c283fce4305e77`.
No commit or push is part of this implementation.

This stage adds state ownership, activity IDs, versioned persistence, migration and
legacy write protection. It adds no patch model, partial replanner, candidate selection,
fact diff, incremental collection, repair loop, framework or new external dependency.
Phase 1/2 validators and Phase 2.5 tools remain unchanged.

## Ownership and generation

Previously edit/save could use a complete client itinerary as their base. Managed state
is instead loaded by trip ID from the database. `AuthoritativePlanState` contains the
itinerary, DB revision, state schema, archive flag and baseline provenance. Constraints
and original request are nested in the itinerary. The returned object is a detached
snapshot; mutating it does not mutate storage.

`POST /trip/generate?managed_state=true` persists a server-produced authoritative draft
and returns the existing itinerary response shape with additional metadata. The frontend
now opts into this path. Trip IDs are random server UUID identifiers, avoiding collisions
between repeated same-destination/date generations. Drafts may carry invalid/unverifiable
validation reports: storing a generated baseline does not certify feasibility.

For staged compatibility, `/generate` without the option and direct legacy generation
helpers retain their old behavior and are NOT authoritative managed state. They do not
promise materialized activity IDs. This is an explicit compatibility boundary, not a
second way to create trusted state from arbitrary HTTP JSON.

## Activity IDs

Only SpotItem gains optional `activity_id` for backward-compatible parsing. At managed
materialization every spot must have a trimmed nonempty ID, unique across the itinerary.
Production uses `activity_` plus UUID; tests inject a deterministic factory. New generation
discards supplied IDs. Legacy migration retains valid existing IDs and fills only missing
(null/absent) IDs; blank and duplicate IDs reject the migration. Managed loads reject
missing, noncanonical or duplicate IDs instead of silently repairing them.

Save/load and unrelated serialization preserve IDs. Future time-only edits must retain
IDs; future replace/add must allocate new IDs. Those operations are not implemented here.

## Version and database

`planning_version` is the managed authoritative committed revision, starting at 1.
There is no new `plan_version` field. Future PlanningState.version must reflect that
revision; legacy preview counters outside managed state retain their old semantics.

TripRecord adds nullable integer planning_version/state_schema_version, baseline_trusted
(default false) and archived (default true for legacy rows). State schema 1 is independent
of tool/fact schema versions. DB and itinerary JSON versions must match on managed load.
Mismatch is an error, never silently reconciled.

The current repository uses SQLite. init_db uses a short BEGIN IMMEDIATE transaction to
serialize additive column upgrades and leaves historical rows otherwise intact. This is
not a generic MySQL migration framework. No production data migration was executed as
part of development tests; database regressions ran against temporary SQLite files.

## CAS and validation

`compare_and_swap_plan(trip_id, base_version, candidate, evaluation_time=...)` is an
internal persistence primitive, not an HTTP mutation endpoint or a replan implementation.
Only server-produced candidates may reach it. It checks managed baseline eligibility,
IDs, original request/constraints/destination preservation and full deterministic validation.
Candidate validation_report is recomputed. Existing route facts require explicit applicable
evidence and evaluation time under the unchanged validator.

The candidate carries the base version, not an arbitrary proposed next version. The server
computes base+1. The short UPDATE includes trip_id, planning_version=base and state schema
predicates. Exactly one affected row is success; zero is VERSION_CONFLICT. JSON and column
revision are updated together. Rejected/unknown candidates and losing writers do not change
active JSON or increment the revision. No LLM, MCP or provider I/O occurs inside transactions.
This primitive does not perform future patch scope authorization or fact collection.

## Legacy migration and reading

`load_authoritative_plan` / `migrate_authoritative_plan_if_needed` lazily upgrades a legacy
row, atomically assigning missing IDs, state schema 1 and committed revision 1. Historical
JSON counters are deliberately reset because they were not authoritative revisions.
The conditional update checks the old JSON and null managed columns to prevent overwriting
concurrent changes. Failure rolls back. Repeat loads do not regenerate IDs or rewrite JSON.

Historical facts and reports are cleared. Existing original_request/constraints may remain
for display, but all migrated historical baselines have baseline_trusted=false: their
provenance cannot be inferred from field presence. They are readable and archivable but
return replan_eligible=false / BASELINE_UNVERIFIABLE. No constraints are invented from
place names or existing budgets. Promotion of historical constraint provenance is deferred.

`GET /trip/{trip_id}/state` exposes this authoritative load path and can trigger the
one-time migration. Ordinary detail reads preserve legacy display compatibility; managed
detail reads enforce consistency. Migration is a one-way upgrade; client payloads cannot
remove the managed marker from the database.

## Draft versus archive and legacy entry points

Managed drafts are omitted from saved-history lists and saved token statistics until
archived. Old `/save` on a managed record changes only archived metadata; it ignores the
supplied core JSON, constraints, facts, IDs and version. Archiving does not increment the
plan revision. Thus a forged client plan can never overwrite the authoritative record.
Export after save uses the stored authoritative content, not frontend display-only tip edits.

Old `/edit` rejects managed records with LEGACY_WRITE_FORBIDDEN before invoking the legacy
LLM helper. The frontend explains that managed partial editing is not yet available. No
new editing UI or pretend Phase 3B operation is included.

Unknown/unmanaged legacy HTTP saves strip client IDs, original_request, constraints,
facts, reports, version and state marker. They remain untrusted legacy imports; a forged
managed marker for an unknown trip is rejected. Internal legacy save also refuses managed
rows, including a conditional update guard against migration races. Request trip IDs must
agree with nested itinerary IDs. Existing direct internal legacy save behavior remains
for frozen regression compatibility; it cannot overwrite a managed record.

## Errors and security boundary

Stable codes include VERSION_CONFLICT, MANAGED_STATE_REQUIRED, LEGACY_WRITE_FORBIDDEN,
BASELINE_UNVERIFIABLE, STATE_VERSION_MISMATCH, DUPLICATE_ACTIVITY_ID, INVALID_ACTIVITY_ID,
STATE_ID_MISMATCH, STATE_NOT_FOUND, BASELINE_CHANGE_FORBIDDEN and VALIDATION_REJECTED.
State API errors use detail.code with 404 for missing state and 409 otherwise.

Trip ID and version are not user authentication. There is no multi-tenant account access
control, signed evidence system or public CAS endpoint. Trusted internal call sites remain
a boundary: do not expose create_authoritative_draft/CAS as arbitrary itinerary upload APIs.

## Verification

47 Phase 3A tests cover IDs, migration, rollback, corrupted managed state, version checks,
real two-thread/file-backed SQLite CAS (exactly one winner), HTTP and internal legacy write
bypass, unknown-client metadata, archive semantics and opt-in generation.
No separate state evaluator was created: real database and HTTP integration tests directly
exercise these invariants; no replanning/LLM quality metric would be meaningful yet.

| Suite | Passed | Failed | Skipped | Collection errors |
| --- | ---: | ---: | ---: | ---: |
| Phase 3A including boundary hardening | 83 | 0 | 0 | 0 |
| Phase 2.5 | 167 | 0 | 0 | 0 |
| Phase 2 | 200 | 0 | 0 | 0 |
| Phase 1 | 156 | 0 | 0 | 0 |
| Full backend | 694 | 2 | 0 | 0 |

All 28 MCP, 48 Phase 2 and 67 Phase 1 evaluator cases pass without mismatches.
Inputs/expected labels and existing tests are unchanged. Full failures remain the known
RAG top_k 6 versus 8 assertion and day-edit mock missing json import. Existing utcnow
warnings remain. Full backend is not all green.

Use the existing Python environment with PYTHONDONTWRITEBYTECODE=1, blank provider keys,
REDIS_ENABLED=false and ENABLE_AMAP_ENRICHMENT=false. Run pytest with -p no:cacheprovider
and a unique temporary --basetemp. For full legacy tests, inject a temporary SQLite engine
and SessionLocal into app.config before importing application modules; targeted state tests
already provide isolated database fixtures.

Frontend types and object-spread preservation were inspected. vue-tsc was unavailable
because frontend/node_modules is absent; no dependencies were installed. No frontend
build or production deployment is claimed.


## State boundary hardening

Managed schema versions require exact Python/JSON integers, currently only 1.
Managed planning versions require exact positive integers. Floats, booleans, numeric
strings and null are rejected before CAS; model_copy/update and assignment do not bypass
this boundary. The candidate must carry the base revision; the server writes base+1.

validate_managed_itinerary_payload is shared by authoritative loading, migration,
draft creation and CAS. It parses the exact JSON, checks DB/envelope metadata against
payload metadata, validates structure and canonical activity IDs, and rejects JSON field
type coercions. Existing nested models' JSON-array/tuple handling is preserved without
changing frozen fact/validator models. AuthoritativePlanState additionally rejects outer
trip/version/schema mismatches with its itinerary. Eligibility remains derived from
trusted provenance plus the presence of original_request and constraints; it is not an
assignable client field.

CAS validates the input representation before business validation, then sets server-owned
metadata, serializes the final representation and validates those exact bytes again before
UPDATE. The validated bytes are the bytes written. Successful writes therefore satisfy
the same loader boundary and are immediately readable at the committed revision, absent
an intervening independent write/delete or database corruption. A later legitimate CAS
may of course make a subsequent current-state read return a newer revision.

Malformed managed JSON, non-object roots and structural/schema parsing failures become
StateError(MANAGED_STATE_CORRUPT). Specific version, identity and duplicate-ID errors keep
their existing codes. Managed corruption never falls back to migration or automatic repair.
Direct Pydantic model construction retains normal ValidationError reporting; persistence
and authoritative loading expose stable StateError codes instead of raw parser failures.

36 new parameterized test instances cover schema/planning type errors (including loops
through assignment and model_copy variants), cross-field conflicts, corruption, final
serialization tampering and three successive write/immediate-read cycles. Rejected writes
assert the entire database row unchanged, including archived and updated_at, and version
rejections assert no UPDATE was issued. Original schema=1.0 and schema=True counterexamples
now fail with STATE_VERSION_MISMATCH while the previous state remains readable.

After hardening: Phase 3A 83 passed; Phase 2.5 167; Phase 2 200; Phase 1 156.
All 28/48/67 evaluator cases still pass. Full backend: 694 passed / the same 2 baseline
failures. No frozen labels or tests were edited. No commit/push or Phase 3B work.

Remaining issues intentionally unchanged: denormalized destination/summary drift,
case-sensitive activity-ID semantics and lack of length limit, legacy edit race HTTP
mapping, frontend build verification, account authentication and multi-database migration.
