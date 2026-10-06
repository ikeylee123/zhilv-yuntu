"""Pure pair discovery, applicability and route arithmetic. Never reads a clock or a tool."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from decimal import Decimal
from pydantic import ValidationError

from app.models.planning_facts import (
    ActivityPair, ActivityReference, Coordinates, FactStatus, PlanningFacts, RouteCoverage,
    POLICY_VERSION, SCHEMA_VERSION, ROUTE_FACT_MAX_AGE_SECONDS, ROUTE_BUFFER_SECONDS,
)


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour * 3600 + minute * 60 if hour < 24 and minute < 60 else None


def reference(day, spot, day_position, spot_position):
    try:
        coordinates = Coordinates(longitude=spot.longitude, latitude=spot.latitude)
    except ValidationError:
        coordinates = None
    return ActivityReference(activity_ref=f"days[{day_position}].spots[{spot_position}]",
                             day_index=day.day_index, date=day.date, entity_id=spot.poi_id,
                             name=spot.name, start_time=spot.start_time, end_time=spot.end_time,
                             coordinates=coordinates)


def discover_pairs(plan):
    """A day with an unorderable attraction is skipped, never bridged across a missing time."""
    pairs, skipped, reasons = [], 0, []
    for day_position, day in enumerate(plan.days):
        activities = [reference(day, spot, day_position, i) for i, spot in enumerate(day.spots)]
        if any(seconds(a.start_time) is None or seconds(a.end_time) is None
               or seconds(a.start_time) >= seconds(a.end_time) for a in activities):
            skipped += max(0, len(activities) - 1)
            reasons.append("missing_activity_time")
            continue
        activities.sort(key=lambda a: (seconds(a.start_time), a.activity_ref))
        pairs.extend(ActivityPair(origin=a, destination=b) for a, b in zip(activities, activities[1:]))
    return tuple(pairs), skipped, tuple(dict.fromkeys(reasons))


def plan_fingerprint(plan, constraints):
    payload = {
        "days": [{"day_index": day.day_index, "date": str(day.date), "spots": [
            {"id": spot.poi_id, "name": spot.name, "longitude": spot.longitude,
             "latitude": spot.latitude, "start": spot.start_time, "end": spot.end_time}
            for spot in day.spots]} for day in plan.days],
        "mode": constraints.transport_preference,
        "required": constraints.require_route_feasibility,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def query_identity(origin, destination):
    # Exact coordinates used for the provider query; no cache-only rounding alias.
    return json.dumps({"provider": "amap", "api_version": "v3", "mode": "driving", "strategy": 0,
                       "origin": origin.model_dump(), "destination": destination.model_dump()},
                      sort_keys=True, separators=(",", ":"))


def source_freshness(source, evaluated_at):
    if source is None or source.fetched_at is None:
        return "stale_fact"
    if evaluated_at is None or evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        return "missing_evaluation_time"
    age = (evaluated_at - source.fetched_at).total_seconds()
    if age < 0:
        return "invalid_response"
    return "stale_fact" if age > ROUTE_FACT_MAX_AGE_SECONDS else None


def check_routes(plan, constraints, facts: PlanningFacts | None, evaluated_at: datetime | None):
    """Return coverage and finding dictionaries without mutating any input."""
    pairs, skipped, discovery_reasons = discover_pairs(plan)
    if constraints.require_route_feasibility is not True:
        return RouteCoverage(skipped_pairs=len(pairs) + skipped), []
    findings, reasons = [], list(discovery_reasons)
    verified = unknown = 0

    def unverifiable(reason, pair=None):
        reasons.append(reason)
        findings.append(dict(type="route_feasibility_unverifiable", severity="unknown",
                             message="Required driving estimate check could not be certified.",
                             day_index=pair.origin.day_index if pair else None,
                             expected="applicable server-collected driving estimate",
                             actual={"reason": reason, "pair": pair.model_dump(mode="json") if pair else None}))

    for reason in discovery_reasons:
        unverifiable(reason)
    if not pairs:
        unverifiable("insufficient_schedule_coverage")
    global_reason = None
    if constraints.transport_preference != "driving":
        global_reason = "unsupported_mode"
    elif facts is None:
        global_reason = "missing_route"
    elif facts.metadata.schema_version != SCHEMA_VERSION or facts.metadata.policy_version != POLICY_VERSION:
        global_reason = "outdated_policy"
    elif facts.metadata.plan_fingerprint != plan_fingerprint(plan, constraints):
        global_reason = "unbound_activity_pair"
    elif evaluated_at is None or evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        global_reason = "missing_evaluation_time"
    elif facts.metadata.collected_at > evaluated_at:
        global_reason = "invalid_response"
    if not pairs and global_reason == "unsupported_mode":
        unverifiable(global_reason)

    for pair in pairs:
        reason = global_reason
        if not reason and (not pair.origin.entity_id or not pair.destination.entity_id):
            reason = "identity_uncertain"
        if not reason and (pair.origin.coordinates is None or pair.destination.coordinates is None):
            reason = "missing_coordinates"
        matching = [] if facts is None else [f for f in facts.route_facts
                    if f.origin == pair.origin and f.destination == pair.destination]
        if not reason and not matching:
            reason = "unbound_activity_pair" if facts.route_facts else "missing_route"
        if not reason:
            # Multiple incompatible observations must not become last-write-wins.
            signatures = {(f.status, f.duration_seconds, f.distance_meters, f.reason) for f in matching}
            if len(signatures) > 1 or any(f.status == FactStatus.CONFLICTING for f in matching):
                reason = "conflicting_fact"
            elif any(f.status == FactStatus.STALE for f in matching):
                reason = "stale_fact"
            elif matching[0].status != FactStatus.KNOWN:
                reason = matching[0].reason or "missing_route"
            else:
                reason = next((r for f in matching if (r := source_freshness(f.source, evaluated_at))), None)
        if reason:
            unknown += 1
            unverifiable(reason, pair)
            continue
        verified += 1  # Verified includes a verified violation, not only passing pairs.
        fact = matching[0]
        available = seconds(pair.destination.start_time) - seconds(pair.origin.end_time)
        required = Decimal(str(fact.duration_seconds)) + ROUTE_BUFFER_SECONDS
        if Decimal(available) < required:
            findings.append(dict(type="route_time_violation", severity="error",
                                 message="Activity gap is shorter than the provider driving estimate.",
                                 day_index=pair.origin.day_index,
                                 expected={"required_seconds": float(required)},
                                 actual={"available_seconds": available, "required_seconds": float(required),
                                         "route_duration_seconds": fact.duration_seconds,
                                         "origin": pair.origin.model_dump(mode="json"),
                                         "destination": pair.destination.model_dump(mode="json")}))
    return RouteCoverage(requested=True, required_pairs=len(pairs), verified_pairs=verified,
                         unknown_pairs=unknown, skipped_pairs=skipped,
                         reasons=tuple(dict.fromkeys(reasons))), findings
