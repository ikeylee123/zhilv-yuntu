"""Small independent fixtures for Phase 2 tests (not evaluation labels)."""
from datetime import datetime, timezone
from app.models.schemas import Itinerary, TripRequest, TripConstraints, PlanningState
from app.models.planning_facts import FactMetadata, FactSource, PlanningFacts, RouteFact
from app.services.route_validation import discover_pairs, plan_fingerprint

NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)


def state():
    request = TripRequest(destination="Test City", start_date="2026-10-06", end_date="2026-10-06",
                          travelers=1, budget=1000)
    constraints = TripConstraints(require_route_feasibility=True, transport_preference="driving")
    plan = Itinerary.model_validate(dict(trip_id="route-test", destination="Test City", summary="Fixture",
        estimated_budget=100, budget_breakdown={"total": 100}, days=[dict(day_index=1, date="2026-10-06", spots=[
        dict(name="Museum", poi_id="a", longitude=120, latitude=30, start_time="09:00", end_time="11:00"),
        dict(name="Garden", poi_id="b", longitude=120.1, latitude=30.1, start_time="11:20", end_time="12:00")])]))
    return PlanningState(trip_request=request, constraints=constraints, current_itinerary=plan, evaluation_time=NOW)


def facts(s, duration=1200, **extra):
    pair = discover_pairs(s.current_itinerary)[0][0]
    data = dict(fact_id="test-fact", status="known", origin=pair.origin, destination=pair.destination,
                source=FactSource(fetched_at=NOW), duration_seconds=duration, distance_meters=5000)
    data.update(extra)
    return PlanningFacts(metadata=FactMetadata(run_id="test-run", collected_at=NOW,
                         plan_fingerprint=plan_fingerprint(s.current_itinerary, s.constraints)),
                         route_facts=(RouteFact(**data),))


def trusted(s):
    return {spot.poi_id: dict(poi_id=spot.poi_id, name=spot.name,
                             latitude=spot.latitude, longitude=spot.longitude)
            for day in s.current_itinerary.days for spot in day.spots}
