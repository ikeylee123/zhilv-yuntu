"""Deterministic, local merge and independent preservation checks."""
from decimal import Decimal
from app.models.schemas import SpotItem
from app.models.replanning import PlanPatch, ReplacePatch, RemovePatch, AddPatch, ModifyTimePatch, StateError
from app.services.authoritative_state import new_activity_id, materialize_activity_ids
from app.services.replanning_transport import allowed_derived_changes, cost_deltas


def build_patch(state, request, candidate=None, *, id_factory=new_activity_id):
    day = next(d for d in state.itinerary.days if d.day_index == request.target_day_index)
    target = next((s for s in day.spots if s.activity_id == request.target_activity_id), None)
    if request.change_type in ("replace", "add"):
        poi = candidate.poi
        activity = SpotItem(activity_id=id_factory(), poi_id=poi.poi_id, name=poi.name,
            latitude=poi.coordinates.latitude, longitude=poi.coordinates.longitude,
            address=poi.address, location=state.itinerary.destination, estimated_cost=candidate.estimated_cost,
            start_time=target.start_time if target else request.start_time,
            end_time=target.end_time if target else request.end_time)
        if activity.activity_id in {a.activity_id for d in state.itinerary.days for a in d.spots}:
            raise StateError("DUPLICATE_ACTIVITY_ID")
        op = ReplacePatch(target_activity_id=target.activity_id, activity=activity) if target else AddPatch(
            activity=activity, before_activity_id=request.before_activity_id, append=request.append)
    elif request.change_type == "remove": op = RemovePatch(target_activity_id=target.activity_id)
    else: op = ModifyTimePatch(target_activity_id=target.activity_id, start_time=request.start_time, end_time=request.end_time)
    return PlanPatch(base_plan_version=state.planning_version, target_day=day.day_index, operation=op)


def _derived(base, candidate, patch, *, finalized=False):
    policy = allowed_derived_changes(base, candidate, patch, finalized=finalized)
    old = next(d for d in base.days if d.day_index == patch.target_day)
    new = next(d for d in candidate.days if d.day_index == patch.target_day)
    transport = []
    for i, original in enumerate(old.transport):
        if i in policy.removed_indices: continue
        item = original.model_copy(deep=True)
        if i in policy.cleared_indices:
            # Time edits never erase endpoint identity or an existing cost estimate.
            item.distance_km = item.estimated_minutes = item.duration = None
        transport.append(item)
    new.transport = transport + [a.model_copy(deep=True) for a in policy.added_transports]
    ticket, travel = cost_deltas(base, candidate, patch, policy)
    if ticket or travel:
        if "estimated_budget" not in base.model_fields_set or "total" not in base.budget_breakdown.model_fields_set:
            raise StateError("REPLAN_UNVERIFIABLE")
        for field, delta in (("tickets",ticket),("transport",travel),("total",ticket+travel)):
            value = Decimal(str(getattr(base.budget_breakdown,field)))+delta
            if not value.is_finite() or value < 0: raise StateError("REPLAN_UNVERIFIABLE")
            setattr(candidate.budget_breakdown,field,float(value))
        value = Decimal(str(base.estimated_budget))+ticket+travel
        if not value.is_finite() or value < 0: raise StateError("REPLAN_UNVERIFIABLE")
        candidate.estimated_budget=float(value)
    return policy


def finalize_derived(base, candidate, patch):
    _derived(base, candidate, patch, finalized=True)
    assert_preserved_projection(base, candidate, patch, finalized=True)
    return allowed_derived_changes(base, candidate, patch, finalized=True)


def merge_patch(state, scope, patch):
    if (patch.base_plan_version != scope.base_plan_version or patch.target_day != scope.affected_day
            or patch.operation.kind != scope.allowed_operation): raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    candidate = state.itinerary.model_copy(deep=True)
    day = next(d for d in candidate.days if d.day_index == patch.target_day)
    op = patch.operation
    if op.kind == "add":
        index = len(day.spots) if op.append else next(i for i,a in enumerate(day.spots) if a.activity_id == op.before_activity_id)
        day.spots.insert(index,op.activity.model_copy(deep=True))
    else:
        if (op.target_activity_id,) != scope.affected_activity_ids: raise StateError("UNAUTHORIZED_PLAN_MUTATION")
        index = next(i for i,a in enumerate(day.spots) if a.activity_id == op.target_activity_id)
        if op.kind == "remove": day.spots.pop(index)
        elif op.kind == "replace": day.spots[index]=op.activity.model_copy(deep=True)
        else:
            day.spots[index].start_time=op.start_time
            day.spots[index].end_time=op.end_time
    materialize_activity_ids(candidate,fill_missing=False)
    _derived(state.itinerary,candidate,patch)
    assert_preserved_projection(state.itinerary,candidate,patch)
    return candidate


def assert_preserved_projection(base, candidate, patch, *, finalized=False):
    """Compare complete expected content, not just a list of preserved names."""
    expected = base.model_copy(deep=True)
    day = next(d for d in expected.days if d.day_index == patch.target_day)
    op = patch.operation
    original = next((a for a in day.spots if a.activity_id == getattr(op,"target_activity_id",None)),None)
    if op.kind == "modify_time":
        original.start_time, original.end_time = op.start_time, op.end_time
    elif op.kind == "remove": day.spots = [a for a in day.spots if a.activity_id != op.target_activity_id]
    elif op.kind == "replace":
        if (op.activity.start_time,op.activity.end_time) != (original.start_time,original.end_time):
            raise StateError("UNAUTHORIZED_PLAN_MUTATION")
        day.spots = [op.activity.model_copy(deep=True) if a.activity_id == op.target_activity_id else a for a in day.spots]
    else:
        i=len(day.spots) if op.append else next(i for i,a in enumerate(day.spots) if a.activity_id==op.before_activity_id)
        day.spots.insert(i,op.activity.model_copy(deep=True))
    # The assertion never calls the mutation helper. Authorization is declarative;
    # inspect actual transports and component deltas independently.
    expected.planning_facts = candidate.planning_facts
    policy = allowed_derived_changes(base, expected, patch, finalized=finalized)
    old_day = next(d for d in base.days if d.day_index == patch.target_day)
    actual_day = next(d for d in candidate.days if d.day_index == patch.target_day)
    retained = [(i,t) for i,t in enumerate(old_day.transport) if i not in policy.removed_indices]
    if len(actual_day.transport) != len(retained)+len(policy.added_transports):
        raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    for (index, original), actual in zip(retained,actual_day.transport):
        allowed = {"distance_km","estimated_minutes","duration"} if index in policy.cleared_indices else set()
        if original.model_dump(exclude=allowed) != actual.model_dump(exclude=allowed):
            raise StateError("UNAUTHORIZED_PLAN_MUTATION")
        if any(getattr(actual,f) is not None for f in allowed): raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    if actual_day.transport[len(retained):] != list(policy.added_transports):
        raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    ticket, travel = cost_deltas(base,expected,patch,policy)
    for field,delta in (("tickets",ticket),("transport",travel),("total",ticket+travel)):
        if Decimal(str(getattr(candidate.budget_breakdown,field))) != Decimal(str(float(Decimal(str(getattr(base.budget_breakdown,field)))+delta))):
            raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    if Decimal(str(candidate.estimated_budget)) != Decimal(str(float(Decimal(str(base.estimated_budget))+ticket+travel))):
        raise StateError("UNAUTHORIZED_PLAN_MUTATION")
    # Copy only independently verified derived fields into the business projection.
    day.transport = actual_day.transport
    expected.budget_breakdown.tickets = candidate.budget_breakdown.tickets
    expected.budget_breakdown.transport = candidate.budget_breakdown.transport
    if "total" in candidate.budget_breakdown.model_fields_set:
        expected.budget_breakdown.total = candidate.budget_breakdown.total
    if "estimated_budget" in candidate.model_fields_set:
        expected.estimated_budget = candidate.estimated_budget
    def projection(plan):
        return plan.model_dump(mode="json",exclude={"planning_facts","validation_report"})
    if projection(expected) != projection(candidate): raise StateError("UNAUTHORIZED_PLAN_MUTATION")
