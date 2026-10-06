"""Pure derived-change authorization, independent of the patch mutation helper."""
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from app.models.replanning import StateError
from app.models.schemas import TransportItem
from app.services.plan_validator import normalize_name
from app.services.route_validation import discover_pairs, source_freshness


@dataclass(frozen=True)
class EndpointBinding:
    kind: str
    activity_id: str | None = None


@dataclass(frozen=True)
class AllowedDerivedChanges:
    removed_indices: frozenset[int]
    cleared_indices: frozenset[int]
    added_transports: tuple[TransportItem, ...]
    unknown_cost_pairs: tuple[str, ...]


def bind_endpoint(day, name):
    key = normalize_name(name or "")
    entities = [("attraction", s.activity_id) for s in day.spots if key and normalize_name(s.name) == key]
    entities += [("hotel", None)] if day.hotel and key == normalize_name(day.hotel.name) else []
    entities += [("meal", None) for m in day.meals if key and normalize_name(m.name) == key]
    if not entities: return EndpointBinding("unbound")
    if len(entities) != 1: return EndpointBinding("ambiguous")
    return EndpointBinding(*entities[0])


def endpoint_key(ref):
    c = ref.coordinates
    return (ref.entity_id, normalize_name(ref.name), (c.longitude, c.latitude, c.system) if c else None)


def pair_key(pair):
    return endpoint_key(pair.origin), endpoint_key(pair.destination)


def day_pairs(plan, day_index):
    return [p for p in discover_pairs(plan)[0] if p.origin.day_index == day_index]


def estimate_transport(pair, facts):
    """Existing distance policy on a tiny isolated fragment, never whole-plan enrichment.

    An explicit heuristic estimate is not a provider fare or real-price evidence.
    Only exact Known candidate-bound driving evidence can supply distance.
    """
    matches = [f for f in facts.route_facts if f.status == "known"
               and f.origin == pair.origin and f.destination == pair.destination] if facts else []
    if len(matches) != 1: return None
    fact = matches[0]
    if source_freshness(fact.source, facts.metadata.collected_at) is not None: return None
    item = TransportItem(mode="driving", from_place=pair.origin.name, to_place=pair.destination.name,
        distance_km=fact.distance_meters/1000, estimated_minutes=round(fact.duration_seconds/60))
    from app.services.trip_service import _apply_route_based_transport_costs
    _apply_route_based_transport_costs(SimpleNamespace(days=[SimpleNamespace(transport=[item])]))
    return item


def allowed_derived_changes(base, authorized, patch, *, finalized=False):
    old = next(d for d in base.days if d.day_index == patch.target_day)
    new = next(d for d in authorized.days if d.day_index == patch.target_day)
    old_pairs, new_pairs = day_pairs(base, old.day_index), day_pairs(authorized, old.day_index)
    old_keys, new_keys = {pair_key(p) for p in old_pairs}, {pair_key(p) for p in new_pairs}
    obsolete = old_keys-new_keys
    target_id = getattr(patch.operation, "target_activity_id", None)
    target = next((s for s in old.spots if s.activity_id == target_id), None)
    target_changed = target is not None and patch.operation.kind in ("replace", "remove")
    affected_names = {normalize_name(a.name) for p in old_pairs if pair_key(p) in obsolete for a in (p.origin,p.destination)}
    if target_changed: affected_names.add(normalize_name(target.name))
    refs = {normalize_name(a.name): a for p in old_pairs for a in (p.origin,p.destination)}
    removed, cleared = set(), set()
    for i, item in enumerate(old.transport):
        names = (normalize_name(item.from_place or ""), normalize_name(item.to_place or ""))
        bindings = [bind_endpoint(old, name) for name in names]
        if any(b.kind == "ambiguous" and n in affected_names for b,n in zip(bindings,names)):
            raise StateError("TRANSPORT_BINDING_UNVERIFIABLE")
        targets_endpoint = target_changed and any(b.activity_id == target_id for b in bindings)
        key = (endpoint_key(refs[names[0]]), endpoint_key(refs[names[1]])) if all(n in refs for n in names) else None
        is_obsolete = all(b.kind == "attraction" for b in bindings) and key in obsolete
        # A target-to-airport/hotel/nonadjacent segment cannot be silently reassigned.
        if targets_endpoint and not is_obsolete:
            if patch.operation.kind == "replace" and key in new_keys:
                continue  # Same identity/context replacement: route remains applicable.
            raise StateError("TRANSPORT_BINDING_UNVERIFIABLE")
        if is_obsolete:
            if patch.operation.kind == "modify_time": cleared.add(i)
            else: removed.add(i)
    additions, unknown = [], []
    if finalized:
        for pair in new_pairs:
            if pair_key(pair) in old_keys: continue
            # Existing non-adjacent explicit cost may now become applicable.
            existing = []
            for i, item in enumerate(old.transport):
                if i in removed: continue
                bindings = [bind_endpoint(old,n) for n in (item.from_place,item.to_place)]
                names = (normalize_name(item.from_place or ""), normalize_name(item.to_place or ""))
                if (all(b.kind == "attraction" for b in bindings) and all(n in refs for n in names)
                        and (endpoint_key(refs[names[0]]),endpoint_key(refs[names[1]])) == pair_key(pair)
                        and normalize_name(item.mode) == (base.constraints.transport_preference or "driving")
                        and "estimated_cost" in item.model_fields_set):
                    existing.append(item.estimated_cost)
            if existing and len(set(existing)) == 1: continue
            item = None
            if not existing and base.constraints.transport_preference == "driving":
                item = estimate_transport(pair, authorized.planning_facts)
            if item is None:
                unknown.append(pair.origin.activity_ref+"->"+pair.destination.activity_ref)
                continue
            if any(bind_endpoint(new,a.name).kind != "attraction" for a in (pair.origin,pair.destination)):
                raise StateError("TRANSPORT_BINDING_UNVERIFIABLE")
            additions.append(item)
    return AllowedDerivedChanges(frozenset(removed), frozenset(cleared), tuple(additions), tuple(unknown))


def cost_deltas(base, authorized, patch, policy):
    """Direct target delta and authorized segment accounting, not global rebudgeting."""
    day = next(d for d in base.days if d.day_index == patch.target_day)
    target = next((a for a in day.spots if a.activity_id == getattr(patch.operation,"target_activity_id",None)),None)
    ticket = Decimal(0)
    if patch.operation.kind in ("remove","replace"):
        if "estimated_cost" not in target.model_fields_set: raise StateError("REPLAN_UNVERIFIABLE")
        ticket -= Decimal(str(target.estimated_cost))
    if patch.operation.kind in ("add","replace"):
        ticket += Decimal(str(patch.operation.activity.estimated_cost))
    travel = sum((Decimal(str(a.estimated_cost)) for a in policy.added_transports),Decimal(0))
    travel -= sum((Decimal(str(day.transport[i].estimated_cost)) for i in policy.removed_indices),Decimal(0))
    if not ticket.is_finite() or not travel.is_finite(): raise StateError("REPLAN_UNVERIFIABLE")
    return ticket, travel
