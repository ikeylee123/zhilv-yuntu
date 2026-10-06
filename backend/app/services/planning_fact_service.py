"""Server-side route collection. Client snapshots are never accepted as evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import httpx
from pydantic import Field, ValidationError, field_validator

from app.models.planning_facts import (
    CollectionOutcome, Coordinates, FactMetadata, FactSource, FactStatus,
    FrozenModel, OutcomeStatus, PlanningFacts, RouteFact, ToolCallMetrics,
    ROUTE_FACT_MAX_AGE_SECONDS, reject_bool_route_number,
)
from app.services import map_service
from app.services.cache_service import get_cached_json, set_cached_json
from app.services.route_validation import discover_pairs, plan_fingerprint, query_identity, source_freshness
from app.services.tool_adapter import DirectToolProvider
from app.models.tool_contracts import ToolStatus, validate_result


class CachedRoute(FrozenModel):
    query_id: str
    source: FactSource
    distance_meters: float = Field(ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)


    @field_validator("distance_meters", "duration_seconds", mode="before")
    @classmethod
    def reject_boolean_estimates(cls, value):
        return reject_bool_route_number(value)


def failure(exc):
    reason = getattr(exc, "reason", None)
    if isinstance(exc, httpx.TimeoutException) or reason == "request_timeout":
        return OutcomeStatus.TIMEOUT, "timeout"
    if isinstance(exc, (ValidationError, ValueError, TypeError, KeyError)) or reason == "invalid_response":
        return OutcomeStatus.INVALID_RESPONSE, "invalid_response"
    return OutcomeStatus.PROVIDER_ERROR, "provider_error"


def tool_failure(result):
    # Preserve Phase 2 finding vocabulary; richer machine reasons remain in ToolResult.
    mapping = {"timeout": OutcomeStatus.TIMEOUT, "no_data": OutcomeStatus.NO_DATA,
               "invalid_response": OutcomeStatus.INVALID_RESPONSE, "unsupported": OutcomeStatus.UNSUPPORTED}
    status = mapping.get(result.status.value, OutcomeStatus.PROVIDER_ERROR)
    return status, status.value


def _same_place(activity, place):
    try:
        return (place is not None and place.get("poi_id") == activity.entity_id
                and " ".join(str(place.get("name") or "").split()).casefold() == " ".join(activity.name.split()).casefold()
                and Coordinates(longitude=place.get("longitude"), latitude=place.get("latitude")) == activity.coordinates)
    except (ValidationError, AttributeError):
        return False


def collect_route_facts(plan, constraints, *, trusted_pois=None, allow_identity_lookup=True, now=None, tool_provider=None):
    """trusted_pois is an internal candidate-pool argument, never an HTTP field.

    Local name-based enrichment is not binding evidence. Edit/save may rebind an
    explicit POI ID via the provider, requiring exact ID, normalized name and coordinates.
    No supplied itinerary.planning_facts or report is read here.
    """
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    if clock().tzinfo is None or clock().utcoffset() is None:
        raise ValueError("Collection time must be timezone aware")
    tool_provider = tool_provider or DirectToolProvider(cache_get=get_cached_json, cache_set=set_cached_json, clock=clock)
    route_facts, outcomes = [], []
    counts = dict(logical_route_queries=0, cache_hits=0, actual_provider_calls=0,
                  deduplicated_calls=0, unnecessary_calls=0, identity_provider_calls=0)
    pairs, _, _ = discover_pairs(plan)
    resolved_ids, queries = {}, {}
    if constraints.require_route_feasibility is not True:
        pairs = ()
    for pair in pairs:
        status, reason, data, query_id = OutcomeStatus.SUCCESS, None, None, None
        cache_hit = deduplicated = False
        if constraints.transport_preference != "driving":
            status, reason = OutcomeStatus.UNSUPPORTED, "unsupported_mode"
        elif any(a.coordinates is None for a in (pair.origin, pair.destination)):
            status, reason = OutcomeStatus.NO_DATA, "missing_coordinates"
        elif any(not a.entity_id or not a.name.strip() for a in (pair.origin, pair.destination)):
            status, reason = OutcomeStatus.NO_DATA, "identity_uncertain"
        else:
            for activity in (pair.origin, pair.destination):
                place = (trusted_pois or {}).get(activity.entity_id)
                if place is None and allow_identity_lookup:
                    if activity.entity_id not in resolved_ids:
                        try:
                            lookup_args = {"poi_id": activity.entity_id}
                            result = tool_provider.execute("get_poi", lookup_args)
                            result = validate_result("get_poi", lookup_args, result.model_dump())
                            counts["identity_provider_calls"] += result.metadata.provider_attempts
                            if result.status == ToolStatus.SUCCESS:
                                age = (clock() - result.fetched_at).total_seconds()
                                if age < 0 or age > ROUTE_FACT_MAX_AGE_SECONDS:
                                    resolved_ids[activity.entity_id] = (None, (OutcomeStatus.INVALID_RESPONSE, "identity_uncertain"))
                                else:
                                    poi = result.data
                                    resolved_ids[activity.entity_id] = (dict(poi_id=poi.poi_id, name=poi.name,
                                        latitude=poi.coordinates.latitude, longitude=poi.coordinates.longitude), None)
                            elif result.status == ToolStatus.NO_DATA:
                                resolved_ids[activity.entity_id] = (None, None)
                            else:
                                resolved_ids[activity.entity_id] = (None, tool_failure(result))
                        except Exception as exc:
                            resolved_ids[activity.entity_id] = (None, failure(exc))
                    place, identity_failure = resolved_ids[activity.entity_id]
                    if identity_failure:
                        status, reason = identity_failure
                        break
                if not _same_place(activity, place):
                    status, reason = OutcomeStatus.NO_DATA, "identity_uncertain"
                    break
        if reason is None:
            counts["logical_route_queries"] += 1
            query_id = query_identity(pair.origin.coordinates, pair.destination.coordinates)
            if query_id in queries:
                counts["deduplicated_calls"] += 1
                deduplicated = True
                status, reason, data, fact_status = queries[query_id]
            else:
                arguments = {"origin": pair.origin.coordinates.model_dump(),
                             "destination": pair.destination.coordinates.model_dump()}
                try:
                    result = tool_provider.execute("get_driving_route", arguments)
                    result = validate_result("get_driving_route", arguments, result.model_dump())
                    counts["actual_provider_calls"] += result.metadata.provider_attempts
                    cache_hit = result.metadata.cache_hit
                    counts["cache_hits"] += int(cache_hit)
                    if result.status == ToolStatus.SUCCESS:
                        source = FactSource(fetched_at=result.fetched_at)
                        reason = source_freshness(source, clock())
                        data = CachedRoute(query_id=query_id, source=source,
                            distance_meters=result.data.distance_meters, duration_seconds=result.data.duration_seconds)
                        fact_status = FactStatus.KNOWN if reason is None else FactStatus.STALE if reason == "stale_fact" else FactStatus.UNKNOWN
                        if reason:
                            status = OutcomeStatus.INVALID_RESPONSE
                    else:
                        status, reason = tool_failure(result)
                        fact_status = FactStatus.UNKNOWN
                        previous = tool_provider.previous("get_driving_route", arguments)
                        if previous is not None:
                            previous = validate_result("get_driving_route", arguments, previous.model_dump())
                            source = FactSource(fetched_at=previous.fetched_at)
                            if previous.status == ToolStatus.SUCCESS and source_freshness(source, clock()) == "stale_fact":
                                data = CachedRoute(query_id=query_id, source=source,
                                    distance_meters=previous.data.distance_meters, duration_seconds=previous.data.duration_seconds)
                                fact_status = FactStatus.STALE
                except Exception as exc:
                    status, reason = failure(exc)
                    fact_status = FactStatus.UNKNOWN
                queries[query_id] = (status, reason, data, fact_status)
        else:
            fact_status = FactStatus.UNKNOWN
        fact_reason = "stale_fact" if fact_status == FactStatus.STALE else reason
        route_facts.append(RouteFact(fact_id=f"route-{len(route_facts)}", status=fact_status,
                           origin=pair.origin, destination=pair.destination,
                           distance_meters=data.distance_meters if data else None,
                           duration_seconds=data.duration_seconds if data else None,
                           source=data.source if data else None, reason=fact_reason))
        outcomes.append(CollectionOutcome(pair=pair, status=status, reason=reason, query_id=query_id,
                                          cache_hit=cache_hit, deduplicated=deduplicated))
    counts["unnecessary_calls"] = max(
        0, counts["actual_provider_calls"] - (len(queries) - counts["cache_hits"]))
    return PlanningFacts(metadata=FactMetadata(run_id=str(uuid4()),
                         plan_fingerprint=plan_fingerprint(plan, constraints), collected_at=clock()),
                         route_facts=tuple(route_facts), collection_outcomes=tuple(outcomes),
                         metrics=ToolCallMetrics(**counts))


def attach_server_validation(itinerary, request=None, *, increment_version=False,
                             trusted_pois=None, allow_identity_lookup=True):
    """Workflow boundary: discard client facts and obtain fresh server-owned evidence."""
    from app.services.plan_validator import attach_validation, resolve_constraints
    constraints = resolve_constraints(itinerary, request)
    facts = None
    if constraints.require_route_feasibility is True:
        facts = collect_route_facts(itinerary, constraints, trusted_pois=trusted_pois,
                                    allow_identity_lookup=allow_identity_lookup)
    return attach_validation(itinerary, request, increment_version=increment_version,
                             facts=facts, evaluation_time=datetime.now(timezone.utc))
