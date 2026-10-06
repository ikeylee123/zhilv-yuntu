from datetime import timedelta
from pathlib import Path
import sys
import pytest
from pydantic import ValidationError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.planning_facts import Coordinates, FactSource, FactStatus, PlanningFacts, RouteFact, RouteCoverage
from app.services.route_validation import discover_pairs, plan_fingerprint
from app.services.plan_validator import validate_plan
from route_fixtures import state, facts, NOW


@pytest.mark.parametrize("field,value", [("duration_seconds", -1), ("duration_seconds", float("nan")),
    ("duration_seconds", float("inf")), ("distance_meters", -1), ("distance_meters", float("inf")),
    ("duration_seconds", None), ("distance_meters", None), ("source", None)])
def test_known_rejects_incomplete_or_invalid_data(field, value):
    raw = facts(state()).route_facts[0].model_dump()
    raw[field] = value
    with pytest.raises(ValidationError):
        RouteFact.model_validate(raw)


def test_source_timestamp_must_be_aware_and_present_for_known():
    with pytest.raises(ValidationError):
        FactSource(fetched_at="2026-10-06T00:00:00")
    raw = facts(state()).route_facts[0].model_dump()
    raw["source"]["fetched_at"] = None
    with pytest.raises(ValidationError):
        RouteFact.model_validate(raw)


@pytest.mark.parametrize("status", list(FactStatus))
def test_status_roundtrip_and_snapshot_immutable(status):
    snapshot = facts(state(), status=status)
    assert PlanningFacts.model_validate_json(snapshot.model_dump_json()) == snapshot
    with pytest.raises(ValidationError):
        snapshot.route_facts[0].duration_seconds = 0
    assert isinstance(snapshot.route_facts, tuple)


@pytest.mark.parametrize("longitude,latitude", [(181, 0), (0, 91), (float("nan"), 0), (0, float("inf"))])
def test_coordinates_reject_invalid_values(longitude, latitude):
    with pytest.raises(ValidationError):
        Coordinates(longitude=longitude, latitude=latitude)


def test_pair_order_is_time_not_array_order():
    s = state(); s.current_itinerary.days[0].spots.reverse()
    pair = discover_pairs(s.current_itinerary)[0][0]
    assert pair.origin.entity_id == "a" and pair.destination.entity_id == "b"


def test_missing_time_does_not_bridge_unordered_activity():
    s = state(); s.current_itinerary.days[0].spots[0].start_time = None
    pairs, skipped, reasons = discover_pairs(s.current_itinerary)
    assert pairs == () and skipped == 1 and reasons == ("missing_activity_time",)


def test_pairs_never_cross_days_or_include_meals():
    s = state(); day = s.current_itinerary.days[0]
    s.current_itinerary.days.append(day.model_copy(deep=True, update={"day_index": 2}))
    pairs, _, _ = discover_pairs(s.current_itinerary)
    assert len(pairs) == 2
    assert all(p.origin.day_index == p.destination.day_index for p in pairs)
    day.spots.pop()
    assert len(discover_pairs(s.current_itinerary)[0]) == 1


@pytest.mark.parametrize("field,value", [("start_time", "11:21"), ("poi_id", "changed"), ("longitude", 120.2)])
def test_fingerprint_changes_with_route_fields(field, value):
    s = state(); before = plan_fingerprint(s.current_itinerary, s.constraints)
    setattr(s.current_itinerary.days[0].spots[1], field, value)
    assert plan_fingerprint(s.current_itinerary, s.constraints) != before


def test_fingerprint_ignores_descriptions_and_is_stable_after_json():
    s = state(); before = plan_fingerprint(s.current_itinerary, s.constraints)
    s.current_itinerary.tips.append("Different advice")
    s.current_itinerary.days[0].spots[0].description = "new prose"
    assert plan_fingerprint(s.current_itinerary, s.constraints) == before
    restored = type(s).model_validate_json(s.model_dump_json())
    assert plan_fingerprint(restored.current_itinerary, restored.constraints) == before
    s.constraints.transport_preference = "walking"
    assert plan_fingerprint(s.current_itinerary, s.constraints) != before


@pytest.mark.parametrize("duration,status", [(1199.9, "valid"), (1200, "valid"), (1200.01, "invalid"), (2100, "invalid"), (0, "valid")])
def test_seconds_arithmetic_and_purity(duration, status):
    s = state(); s.planning_facts = facts(s, duration)
    before = s.model_dump_json(); report = validate_plan(s)
    assert report.status == status and report.route_coverage.verified_pairs == 1
    assert s.model_dump_json() == before
    if status == "invalid":
        violation = next(v for v in report.violations if v.type == "route_time_violation")
        assert violation.actual["available_seconds"] == 1200
        assert violation.actual["required_seconds"] == duration


@pytest.mark.parametrize("age,reason", [(900, None), (901, "stale_fact"), (-1, "invalid_response")])
def test_freshness_is_explicit_and_deterministic(age, reason):
    s = state(); s.planning_facts = facts(s, source=FactSource(fetched_at=NOW-timedelta(seconds=age)))
    result = validate_plan(s)
    assert result.status == ("unverifiable" if reason else "valid")
    if reason:
        assert result.unknowns[0].actual["reason"] == reason


@pytest.mark.parametrize("mutation,reason", [("missing", "missing_route"), ("wrong", "unbound_activity_pair"),
    ("stale", "stale_fact"), ("conflicting", "conflicting_fact"), ("duplicate", None),
    ("fingerprint", "unbound_activity_pair"), ("clock", "missing_evaluation_time")])
def test_fact_applicability(mutation, reason):
    s = state(); snapshot = facts(s); fact = snapshot.route_facts[0]
    if mutation == "missing": snapshot = None
    elif mutation == "wrong": snapshot = snapshot.model_copy(update={"route_facts": (fact.model_copy(update={"origin": fact.destination}),)})
    elif mutation in ("stale", "conflicting"): snapshot = facts(s, status=mutation)
    elif mutation == "duplicate": snapshot = snapshot.model_copy(update={"route_facts": (fact, fact)})
    elif mutation == "fingerprint": s.current_itinerary.days[0].spots[1].end_time = "12:10"
    elif mutation == "clock": s.evaluation_time = None
    s.planning_facts = snapshot
    result = validate_plan(s)
    assert result.status == ("unverifiable" if reason else "valid")
    if reason: assert result.unknowns[0].actual["reason"] == reason


def test_incompatible_duplicate_observations_conflict():
    s = state(); f = facts(s); one = f.route_facts[0]
    s.planning_facts = f.model_copy(update={"route_facts": (one, one.model_copy(update={"duration_seconds": 1201}))})
    assert validate_plan(s).unknowns[0].actual["reason"] == "conflicting_fact"


@pytest.mark.parametrize("required", [None, False, True])
def test_zero_pairs_is_not_full_coverage(required):
    s = state(); s.current_itinerary.days[0].spots.pop()
    s.constraints.require_route_feasibility = required
    s.constraints.transport_preference = None
    result = validate_plan(s)
    assert result.route_coverage.required_pairs == 0
    assert result.route_coverage.verified_fraction is None
    assert result.status == ("unverifiable" if required else "valid")
    assert RouteCoverage.model_validate_json(result.route_coverage.model_dump_json()) == result.route_coverage


def test_route_facts_do_not_change_status_without_opt_in():
    s = state(); s.planning_facts = facts(s, 99999)
    s.constraints.require_route_feasibility = False; s.constraints.transport_preference = None
    result = validate_plan(s)
    assert result.valid and result.route_coverage.skipped_pairs == 1


def test_route_unknown_keeps_hard_error_precedence():
    s = state(); s.constraints.budget_limit = 0
    result = validate_plan(s)
    assert result.status == "invalid"
    assert {v.type for v in result.violations} == {"budget_exceeded"}
    assert {v.type for v in result.unknowns} == {"route_feasibility_unverifiable"}
