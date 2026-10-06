"""Pure single-day authorization; no tools, LLM or database writes."""
from app.models.replanning import ReplanningScope, StateError
from app.services.plan_validator import normalize_name
from app.services.route_validation import discover_pairs
from app.models.tool_contracts import build_tool_query_key


def matches(reference, spot):
    if not isinstance(reference, str) and reference.entity_id is not None:
        return reference.entity_id == spot.poi_id
    return normalize_name(reference if isinstance(reference, str) else reference.name or "") == normalize_name(spot.name)


def authorize_scope(state, request):
    if state.trip_id != request.trip_id:
        raise StateError("INVALID_CHANGE_REQUEST")
    if state.planning_version != request.base_plan_version:
        raise StateError("VERSION_CONFLICT")
    if not state.replan_eligible:
        raise StateError("REPLAN_NOT_ELIGIBLE")
    days = [d for d in state.itinerary.days if d.day_index == request.target_day_index]
    if len(days) != 1:
        raise StateError("ACTIVITY_DAY_MISMATCH")
    all_spots = [(d.day_index, a) for d in state.itinerary.days for a in d.spots]
    for ref in (request.target_activity_id, getattr(request, "before_activity_id", None)):
        if ref is None: continue
        found = [day for day, a in all_spots if a.activity_id == ref]
        if len(found) != 1: raise StateError("ACTIVITY_NOT_FOUND")
        if found[0] != request.target_day_index: raise StateError("ACTIVITY_DAY_MISMATCH")
    target = request.target_activity_id
    fields = ("start_time", "end_time") if request.change_type == "modify_time" else ("target_attraction",)
    return ReplanningScope(trip_id=state.trip_id, base_plan_version=state.planning_version,
        affected_day=request.target_day_index, affected_activity_ids=(target,) if target else (),
        preserved_activity_ids=tuple(a.activity_id for _, a in all_spots if a.activity_id != target),
        allowed_operation=request.change_type, allowed_fields=fields,
        old_required_route_queries=tuple(sorted(route_queries(state.itinerary))),
        scope_reason=f"explicit single {request.change_type} on day {request.target_day_index}")


def precheck_constraints(state, request, candidate=None):
    spots = [s for d in state.itinerary.days for s in d.spots]
    target = next((s for s in spots if s.activity_id == request.target_activity_id), None)
    if request.change_type == "remove" or (request.change_type == "replace" and candidate is not None):
        for ref in state.itinerary.constraints.must_visit or []:
            if matches(ref, target) and sum(matches(ref, s) for s in spots) == 1:
                if candidate is None or not matches(ref, candidate):
                    raise StateError("CONSTRAINT_CONFLICT")
    if candidate is not None:
        if any(matches(ref, candidate) for ref in state.itinerary.constraints.avoid or []):
            raise StateError("CONSTRAINT_CONFLICT")
        if any((s.poi_id and s.poi_id == candidate.poi_id) or normalize_name(s.name) == normalize_name(candidate.name)
               for s in spots if not (request.change_type == "replace" and s.activity_id == request.target_activity_id)):
            raise StateError("DUPLICATE_ATTRACTION")


def route_key(pair):
    if pair.origin.coordinates is None or pair.destination.coordinates is None:
        return None
    try:
        return build_tool_query_key("get_driving_route", {
            "origin": pair.origin.coordinates.model_dump(), "destination": pair.destination.coordinates.model_dump()})
    except ValueError:
        return None


def route_queries(plan):
    if not plan.constraints or plan.constraints.require_route_feasibility is not True:
        return {}
    result = {}
    for pair in discover_pairs(plan)[0]:
        key = route_key(pair)
        if key: result.setdefault(key, []).append(pair)
    return result


def duplicate_relations(plan):
    """Stable-ID pairs: compare the delta, not a retroactive global invariant."""
    pairs = set()
    for day in plan.days:
        for i, a in enumerate(day.spots):
            for b in day.spots[i+1:]:
                if (a.poi_id and a.poi_id == b.poi_id) or normalize_name(a.name) == normalize_name(b.name):
                    pairs.add(tuple(sorted((a.activity_id, b.activity_id))))
    return pairs
