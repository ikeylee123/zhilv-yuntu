"""One-shot authoritative replanning. Tools run outside the final CAS transaction."""
from datetime import datetime, timezone
from app.models.replanning import (PlanChangeRequest, CandidatePreparationRequest, ReplanDecision, StateError)
from app.models.schemas import PlanningState, ConstraintViolation
from app.services.authoritative_state import validate_managed_itinerary_payload, compare_and_swap_plan, new_activity_id
from app.services.replanning_scope import authorize_scope, precheck_constraints, duplicate_relations
from app.services.replanning_candidates import (CandidateStore, prepare_replanning_candidates,
    authorized_candidate, ground_selected, default_region)
from app.services.plan_patch_service import build_patch, merge_patch, assert_preserved_projection, finalize_derived
from app.services.replanning_facts import collect_incremental_facts
from app.services.plan_validator import validate_plan, normalize_name
from app.services.tool_adapter import DirectToolProvider

CANDIDATES = CandidateStore()


def load_replanning_base(trip_id):
    """Read managed state without triggering the legacy migration side effect."""
    from app.services import storage_service as store
    from app.models.db_models import TripRecord
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=trip_id).first()
        if row is None:
            raise StateError("STATE_NOT_FOUND")
        if row.planning_version is None or row.state_schema_version is None:
            raise StateError("REPLAN_NOT_ELIGIBLE")
        return validate_managed_itinerary_payload(row.itinerary_json, trip_id=row.trip_id,
            planning_version=row.planning_version, state_schema_version=row.state_schema_version,
            baseline_trusted=row.baseline_trusted, archived=row.archived)


class ReplanningService:
    def __init__(self, *, provider=None, store=None, clock=None, loader=load_replanning_base,
                 committer=compare_and_swap_plan, id_factory=new_activity_id, region_resolver=default_region):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider = provider or DirectToolProvider(clock=self.clock)
        self.store = store if store is not None else CANDIDATES
        self.loader, self.committer, self.id_factory = loader, committer, id_factory
        self.region_resolver = region_resolver

    def prepare(self, request):
        request = CandidatePreparationRequest.model_validate(request.model_dump())
        state = self.loader(request.trip_id)
        result = prepare_replanning_candidates(state, request, self.provider, self.store, self.clock,
            region_resolver=self.region_resolver)
        # Calls can outlive their grant or the base version.
        if self.loader(request.trip_id).planning_version != request.base_plan_version:
            raise StateError("VERSION_CONFLICT")
        return result

    def replan(self, request):
        request = PlanChangeRequest.model_validate(request.model_dump())
        scope = report = None
        summary = {}
        try:
            state = self.loader(request.trip_id)
            scope = authorize_scope(state, request)
            precheck_constraints(state, request)
            grounded = None
            if request.change_type in ("replace", "add"):
                grounded = authorized_candidate(state, request, self.store, self.clock())
                precheck_constraints(state, request, grounded.poi)
                grounded = ground_selected(grounded, self.provider, self.clock)
                authorized_candidate(state, request, self.store, self.clock())
            patch = build_patch(state, request, grounded, id_factory=self.id_factory)
            candidate = merge_patch(state, scope, patch)
            if duplicate_relations(candidate)-duplicate_relations(state.itinerary):
                raise StateError("DUPLICATE_ATTRACTION")
            candidate.planning_facts, scope = collect_incremental_facts(
                state.itinerary, candidate, scope, self.provider, self.clock, grounded)
            assert_preserved_projection(state.itinerary, candidate, patch)
            derived = finalize_derived(state.itinerary, candidate, patch)
            report = validate_plan(PlanningState(trip_request=state.itinerary.original_request,
                constraints=state.itinerary.constraints, current_itinerary=candidate,
                planning_facts=candidate.planning_facts, evaluation_time=self.clock(), version=state.planning_version))
            if derived.unknown_cost_pairs and state.itinerary.constraints.budget_limit is not None:
                report.unknowns.append(ConstraintViolation(type="budget_unverifiable",severity="unknown",
                    message="New transport relationship lacks applicable cost evidence.",
                    actual={"unknown_cost_pairs":list(derived.unknown_cost_pairs)}))
            candidate.validation_report = report
            old_ids = {s.activity_id for d in state.itinerary.days for s in d.spots}
            new_ids = {s.activity_id for d in candidate.days for s in d.spots}
            summary = dict(change_type=request.change_type.value,
                removed_activity_ids=sorted(old_ids-new_ids), added_activity_ids=sorted(new_ids-old_ids),
                modified_activity_ids=[request.target_activity_id] if request.change_type == "modify_time" else [],
                preserved_activity_ids=list(scope.preserved_activity_ids),
                transport_cost_status="unknown" if derived.unknown_cost_pairs else "estimated",
                unknown_transport_cost_pairs=list(derived.unknown_cost_pairs),
                new_transport_cost_policy="existing-route-distance-heuristic" if derived.added_transports else None)
            if report.status != "valid":
                return ReplanDecision(decision="rejected" if report.status == "invalid" else "unverifiable",
                    reason="REPLAN_VALIDATION_FAILED" if report.status == "invalid" else "REPLAN_UNVERIFIABLE",
                    base_plan_version=request.base_plan_version, validation_report=report, scope=scope, change_summary=summary)
            assert_preserved_projection(state.itinerary, candidate, patch, finalized=True)
            committed = self.committer(request.trip_id, request.base_plan_version, candidate, evaluation_time=self.clock())
            return ReplanDecision(decision="accepted", reason="ACCEPTED", base_plan_version=request.base_plan_version,
                candidate_plan_version=committed.planning_version, validation_report=committed.validation_report,
                itinerary=committed, scope=scope, change_summary=summary)
        except StateError as exc:
            decision = "conflict" if exc.code == "VERSION_CONFLICT" else "unverifiable" if exc.code in (
                "REPLAN_UNVERIFIABLE", "GROUNDING_FAILED", "BASELINE_UNVERIFIABLE", "TRANSPORT_BINDING_UNVERIFIABLE") else "rejected"
            return ReplanDecision(decision=decision, reason=exc.code, base_plan_version=request.base_plan_version,
                validation_report=report, scope=scope, change_summary=summary)
