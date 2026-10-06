"""Server-side route collection. Client snapshots are never accepted as evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
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


def _same_place(activity, place):
    try:
        return (place is not None and place.get("poi_id") == activity.entity_id
                and " ".join(str(place.get("name") or "").split()).casefold() == " ".join(activity.name.split()).casefold()
                and Coordinates(longitude=place.get("longitude"), latitude=place.get("latitude")) == activity.coordinates)
    except (ValidationError, AttributeError):
        return False


def collect_route_facts(plan, constraints, *, trusted_pois=None, allow_identity_lookup=True, now=None):
    """trusted_pois is an internal candidate-pool argument, never an HTTP field.

    Local name-based enrichment is not binding evidence. Edit/save may rebind an
    explicit POI ID via the provider, requiring exact ID, normalized name and coordinates.
    No supplied itinerary.planning_facts or report is read here.
    """
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    if clock().tzinfo is None or clock().utcoffset() is None:
        raise ValueError("Collection time must be timezone aware")
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
                        counts["identity_provider_calls"] += 1
                        try:
                            resolved_ids[activity.entity_id] = (map_service.fetch_place_identity(activity.entity_id), None)
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
                cache_key = "planning:route:v1:" + sha256(query_id.encode()).hexdigest()
                previous = None
                try:
                    cached = get_cached_json(cache_key)
                    if cached is not None:
                        candidate = CachedRoute.model_validate(cached)
                        if candidate.query_id == query_id and candidate.source.fetched_at is not None:
                            previous = candidate
                except (ValidationError, ValueError, TypeError):
                    pass  # Old/invalid cache is not evidence, nor a substitute for a provider call.
                if previous is not None and source_freshness(previous.source, clock()) is None:
                    data = previous
                    cache_hit = True
                    counts["cache_hits"] += 1
                    fact_status = FactStatus.KNOWN
                else:
                    counts["actual_provider_calls"] += 1
                    try:
                        response = map_service.fetch_driving_route_fact(pair.origin.coordinates, pair.destination.coordinates)
                        if response is None:
                            status, reason = OutcomeStatus.NO_DATA, "no_data"
                        else:
                            data = CachedRoute(query_id=query_id, source=FactSource(fetched_at=clock()), **response)
                            set_cached_json(cache_key, data.model_dump(mode="json"),
                                            expire_seconds=ROUTE_FACT_MAX_AGE_SECONDS)
                    except Exception as exc:
                        status, reason = failure(exc)
                    fact_status = FactStatus.KNOWN if reason is None else FactStatus.UNKNOWN
                    if reason is not None and previous is not None and source_freshness(previous.source, clock()) == "stale_fact":
                        data, fact_status = previous, FactStatus.STALE
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
