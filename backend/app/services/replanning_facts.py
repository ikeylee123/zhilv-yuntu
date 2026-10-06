"""Incremental route observations, rebound only after validating the base snapshot."""
from uuid import uuid4
from app.models.planning_facts import (
    PlanningFacts, FactMetadata, FactSource, RouteFact, CollectionOutcome,
    ToolCallMetrics, SCHEMA_VERSION, POLICY_VERSION, FactStatus, OutcomeStatus,
)
from app.services.route_validation import discover_pairs, plan_fingerprint, source_freshness
from app.services.replanning_scope import route_queries, route_key
from app.services.replanning_candidates import call_tool
from app.models.replanning import StateError


def coordinate_identity(coords):
    return (coords.longitude,coords.latitude,coords.system) if coords else None


def identity(ref):
    return (ref.entity_id, ref.name, coordinate_identity(ref.coordinates))


def base_observations(base, now):
    facts = base.planning_facts
    if (facts is None or facts.metadata.plan_fingerprint != plan_fingerprint(base, base.constraints)
            or facts.metadata.schema_version != SCHEMA_VERSION or facts.metadata.policy_version != POLICY_VERSION
            or facts.metadata.collected_at > now):
        return {}, set()
    pairs = discover_pairs(base)[0]
    grouped, identities = {}, set()
    for fact in facts.route_facts:
        if not any(fact.origin == p.origin and fact.destination == p.destination for p in pairs):
            continue
        key = route_key(fact)
        if key: grouped.setdefault(key, []).append(fact)
    reusable = {}
    for key, entries in grouped.items():
        # Ambiguous observations never authorize identity or reuse.
        signatures = {(f.status, f.distance_meters, f.duration_seconds, f.source, f.reason) for f in entries}
        if len(signatures) != 1: continue
        fact = entries[0]
        if fact.status != "known" or fact.source.fetched_at > facts.metadata.collected_at: continue
        for f in entries:
            identities.update((identity(f.origin), identity(f.destination)))
        if source_freshness(fact.source, now) is None: reusable[key] = fact
    return reusable, identities


def collect_incremental_facts(base, candidate, scope, provider, clock, grounded=None):
    now = clock()
    observations, identities = base_observations(base, now)
    if grounded:
        identities.add((grounded.poi.poi_id, grounded.poi.name, coordinate_identity(grounded.poi.coordinates)))
    old, new = route_queries(base), route_queries(candidate)
    reusable = set(new) & set(observations)
    # Coordinate-equivalent queries may share estimates, but never manufacture POI binding.
    reusable = {k for k in reusable if all(identity(a) in identities for p in new[k] for a in (p.origin,p.destination))}
    missing = set(new)-reusable
    scope = scope.model_copy(update=dict(new_required_route_queries=tuple(sorted(new)),
        facts_to_invalidate=tuple(sorted(set(old)-set(new))), facts_to_recollect=tuple(sorted(missing)),
        eligible_reuse_queries=tuple(sorted(reusable))))
    actual_reused, recollected = set(), set()
    results, facts, outcomes = {}, [], []
    counts = dict(logical_route_queries=0, actual_provider_calls=0, cache_hits=0, deduplicated_calls=0)
    pairs = discover_pairs(candidate)[0] if candidate.constraints.require_route_feasibility is True else ()
    for pair in pairs:
        key = route_key(pair)
        observation = observations.get(key) if key in reusable else None
        status, reason, source, distance, duration = "success", None, None, None, None
        hit = dedup = False
        if candidate.constraints.transport_preference != "driving":
            status, reason = "unsupported", "unsupported_mode"
        elif key is None:
            status, reason = "no_data", "missing_coordinates"
        elif any(identity(a) not in identities for a in (pair.origin,pair.destination)):
            status, reason = "no_data", "identity_uncertain"
        elif observation:
            source, distance, duration = observation.source, observation.distance_meters, observation.duration_seconds
            reason = source_freshness(source, clock())
            if reason:
                status = "invalid_response"
            else:
                actual_reused.add(key)
        else:
            counts["logical_route_queries"] += 1
            if key in results:
                dedup = True
                counts["deduplicated_calls"] += 1
            else:
                args = dict(origin=pair.origin.coordinates.model_dump(), destination=pair.destination.coordinates.model_dump())
                try:
                    recollected.add(key)
                    result = call_tool(provider, "get_driving_route", args)
                    counts["actual_provider_calls"] += result.metadata.provider_attempts
                    counts["cache_hits"] += int(result.metadata.cache_hit)
                    results[key] = result
                except StateError:
                    results[key] = None
            result = results[key]
            if result is None:
                status, reason = "provider_error", "provider_error"
            elif result.status != "success":
                status = result.status.value if result.status.value in ("no_data","timeout","invalid_response","unsupported") else "provider_error"
                reason = status
            else:
                source = FactSource(fetched_at=result.fetched_at)
                reason = source_freshness(source, clock())
                status = "invalid_response" if reason else "success"
                distance, duration = result.data.distance_meters, result.data.duration_seconds
                hit = result.metadata.cache_hit
        facts.append(RouteFact(fact_id="replan-route-"+str(len(facts)), status="unknown" if reason else "known",
            origin=pair.origin, destination=pair.destination, source=source, distance_meters=distance,
            duration_seconds=duration, reason=reason))
        outcomes.append(CollectionOutcome(pair=pair, query_id=key, status=status, reason=reason,
            cache_hit=hit, deduplicated=dedup))
    # Recheck reused observations at snapshot completion, including slow intervening calls.
    finished = clock()
    for i, fact in enumerate(facts):
        if fact.status == "known" and source_freshness(fact.source, finished):
            reason = source_freshness(fact.source, finished)
            facts[i] = fact.model_copy(update={"status": FactStatus.UNKNOWN, "reason": reason})
            outcomes[i] = outcomes[i].model_copy(update={"status": OutcomeStatus.INVALID_RESPONSE, "reason": reason})
    unknown = {route_key(f) for f in facts if f.status != "known" and route_key(f)}
    actual_reused -= unknown
    scope = scope.model_copy(update=dict(reused_queries=tuple(sorted(actual_reused)),
        recollected_queries=tuple(sorted(recollected)), unknown_queries=tuple(sorted(unknown))))
    snapshot = PlanningFacts(metadata=FactMetadata(run_id=str(uuid4()),
        plan_fingerprint=plan_fingerprint(candidate,candidate.constraints), collected_at=finished),
        route_facts=tuple(facts), collection_outcomes=tuple(outcomes), metrics=ToolCallMetrics(**counts))
    return snapshot, scope
