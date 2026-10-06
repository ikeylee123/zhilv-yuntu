"""Bounded server-owned candidate grants. No client-supplied POI objects."""
from collections import OrderedDict
from threading import Lock
from uuid import uuid4
from app.models.replanning import AllowedCandidateSet, GroundedCandidate, StateError
from app.models.tool_contracts import validate_result
from app.services.replanning_scope import authorize_scope, precheck_constraints, matches
from app.services.plan_validator import normalize_name

CANDIDATE_TTL_SECONDS = 300


class CandidateStore:
    def __init__(self, capacity=128):
        self.capacity = capacity
        self._sets = OrderedDict()
        self._lock = Lock()

    def put(self, candidates):
        with self._lock:
            self._sets[candidates.candidate_set_id] = candidates
            while len(self._sets) > self.capacity: self._sets.popitem(last=False)

    def get(self, key):
        with self._lock: result = self._sets.get(key)
        if result is None: raise StateError("CANDIDATE_SET_NOT_FOUND")
        return result


def call_tool(provider, name, arguments):
    try:
        raw = provider.execute(name, arguments)
        if name == "get_poi" and raw.status == "success" and getattr(raw.data,"poi_id",None) != arguments["poi_id"]:
            raise StateError("GROUNDING_CONFLICT")
        return validate_result(name, arguments, raw.model_dump())
    except StateError:
        raise
    except Exception as exc:
        raise StateError("REPLAN_UNVERIFIABLE") from exc


def fresh(result, now, ttl=900):
    return result.status == "success" and result.fetched_at is not None and 0 <= (now-result.fetched_at).total_seconds() <= ttl


def default_region(destination):
    from app.services.city_resolver_service import resolve_city
    region = resolve_city(destination)
    if region.adcode:
        return region
    # Curated cities deliberately skip administrative lookup in the old resolver.
    from app.services.map_service import resolve_administrative_area
    from types import SimpleNamespace
    area = resolve_administrative_area(region.city)
    return SimpleNamespace(adcode=area.get("adcode") if area else None)


def prepare_replanning_candidates(state, request, provider, store, now, *, region_resolver=default_region):
    authorize_scope(state, request)
    precheck_constraints(state, request)
    try: region = region_resolver(state.itinerary.destination)
    except Exception as exc: raise StateError("REPLAN_UNVERIFIABLE") from exc
    adcode = region.adcode
    if not isinstance(adcode, str) or len(adcode) != 6 or not adcode.isdigit() or (adcode.endswith("0000") and adcode[:2] not in ("11", "12", "31", "50")):
        raise StateError("REPLAN_UNVERIFIABLE")
    query = {"attraction": "\u666f\u70b9", "museum": "\u535a\u7269\u9986", "park": "\u516c\u56ed"}[request.preference]
    args = dict(query=query, city_or_adcode=adcode, category="spot", page=1, page_size=10, city_limit=True)
    result = call_tool(provider, "search_poi", args)
    now = now() if callable(now) else now
    if not fresh(result, now, CANDIDATE_TTL_SECONDS): raise StateError("REPLAN_UNVERIFIABLE")
    # Conflicting duplicate IDs are excluded entirely, not resolved by list order.
    groups = {}
    for poi in result.data.items: groups.setdefault(poi.poi_id, []).append(poi)
    from app.services.trip_service import _estimate_ticket_cost
    selected = [s for d in state.itinerary.days for s in d.spots
                if not (request.change_type == "replace" and s.activity_id == request.target_activity_id)]
    candidates = []
    for items in groups.values():
        poi = items[0]
        if any(p != poi for p in items) or poi.coordinates is None: continue
        if not poi.adcode or len(poi.adcode) != 6 or not poi.adcode.isdigit(): continue
        prefix = 2 if adcode.endswith("0000") else 4 if adcode.endswith("00") else 6
        if poi.adcode[:prefix] != adcode[:prefix]: continue
        if poi.category != "spot": continue
        if poi.provider_type and any(t in poi.provider_type for t in ("\u4f4f\u5bbf", "\u9910\u996e", "\u9152\u5e97")): continue
        if any(s.poi_id == poi.poi_id or normalize_name(s.name) == normalize_name(poi.name) for s in selected): continue
        if any(matches(ref, poi) for ref in state.itinerary.constraints.avoid or []): continue
        candidates.append(GroundedCandidate(poi=poi, estimated_cost=_estimate_ticket_cost(poi.name)))
    grant = AllowedCandidateSet(candidate_set_id="candidates_"+uuid4().hex,
        trip_id=state.trip_id, base_plan_version=state.planning_version,
        target_day=request.target_day_index, target_activity_id=request.target_activity_id,
        operation=request.change_type, candidates=tuple(candidates[:10]), created_at=now, fetched_at=result.fetched_at)
    store.put(grant)
    return grant


def authorized_candidate(state, request, store, now):
    grant = store.get(request.candidate_set_id)
    if (grant.trip_id != state.trip_id or grant.base_plan_version != state.planning_version
            or grant.target_day != request.target_day_index or grant.operation != request.change_type
            or grant.target_activity_id != request.target_activity_id):
        raise StateError("CANDIDATE_NOT_AUTHORIZED")
    if any(not 0 <= (now-time).total_seconds() <= CANDIDATE_TTL_SECONDS for time in (grant.created_at, grant.fetched_at)):
        raise StateError("CANDIDATE_SET_EXPIRED")
    candidate = next((p for p in grant.candidates if p.poi.poi_id == request.candidate_id), None)
    if candidate is None: raise StateError("CANDIDATE_NOT_AUTHORIZED")
    return candidate


def ground_selected(candidate, provider, now):
    result = call_tool(provider, "get_poi", {"poi_id": candidate.poi.poi_id})
    now = now() if callable(now) else now
    if not fresh(result, now): raise StateError("REPLAN_UNVERIFIABLE")
    poi = result.data
    if (poi.coordinates != candidate.poi.coordinates or normalize_name(poi.name) != normalize_name(candidate.poi.name)):
        raise StateError("GROUNDING_CONFLICT")
    for field in ("category", "adcode"):
        value = getattr(poi, field)
        if value is not None and value != getattr(candidate.poi, field):
            raise StateError("GROUNDING_CONFLICT")
    # Missing detail context retains search evidence; explicit conflicts never win.
    return candidate
