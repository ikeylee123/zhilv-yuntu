"""Phase 1 unit tests use only schemas and the pure validator."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import (
    BudgetBreakdown, ConstraintViolation, DayPlan, Itinerary, MealItem,
    PlaceReference, PlanningState, SpotItem, TripConstraints, TripRequest, ValidationReport,
)
from app.services.plan_validator import (
    attach_validation, constraints_from_request, ensure_date_range,
    scheduled_activities, validate_plan,
)


def make_state(**constraints):
    request = TripRequest(destination="Test City", start_date="2026-08-01", end_date="2026-08-02",
                          travelers=2, budget=100, preferences=["quiet"], special_notes="avoid all crowds")
    plan = Itinerary(trip_id="test", destination="Test City", summary="Synthetic",
                     estimated_budget=100, budget_breakdown=BudgetBreakdown(total=100), days=[
        DayPlan(day_index=1, date="2026-08-01", spots=[SpotItem(name="Museum A", poi_id="a", start_time="09:00", end_time="10:00")]),
        DayPlan(day_index=2, date="2026-08-02", spots=[SpotItem(name="Park B", poi_id="b", start_time="10:00", end_time="11:00")]),
    ])
    return PlanningState(trip_request=request, constraints=TripConstraints(**constraints), current_itinerary=plan)


def types(report):
    return {item.type for item in report.violations if item.severity == "error"}


def test_legacy_models_and_nullable_constraints():
    state = make_state()
    assert state.trip_request.constraints is None
    assert state.current_itinerary.validation_report is None
    empty = TripConstraints.model_validate({key: None for key in TripConstraints.model_fields})
    assert empty.budget_limit is None
    resolved = constraints_from_request(state.trip_request)
    assert resolved.budget_limit == Decimal("100")
    assert resolved.soft_preferences == ["quiet"]
    assert resolved.avoid is None  # No NLP inference from special_notes.
    state.trip_request.constraints = TripConstraints(budget_limit=None)
    assert constraints_from_request(state.trip_request).budget_limit is None
    state.trip_request.constraints = TripConstraints(must_visit=["Museum A"])
    assert constraints_from_request(state.trip_request).budget_limit == 100


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity"])
def test_invalid_constraint_budget_rejected(value):
    with pytest.raises(ValidationError):
        TripConstraints(budget_limit=value)


def test_unknown_constraint_fields_are_not_silently_ignored():
    with pytest.raises(ValidationError):
        TripConstraints(budegt_limit=100)
    with pytest.raises(ValidationError):
        PlaceReference()


def test_report_semantics_and_roundtrip():
    warning = ConstraintViolation(type="example", severity="warning", message="notice")
    report = ValidationReport(violations=[warning])
    assert report.valid and report.status == "valid"
    report.unknowns.append(ConstraintViolation(type="opening_hours", severity="unknown", message="missing facts"))
    assert not report.valid and report.status == "unverifiable"
    report.violations.append(ConstraintViolation(type="overlap", severity="error", message="conflict"))
    assert report.status == "invalid"
    payload = report.model_dump(mode="json")
    payload["valid"] = True
    payload["status"] = "valid"
    assert ValidationReport.model_validate(payload).status == "invalid"
    with pytest.raises(ValidationError):
        ValidationReport(unknowns=[warning])


def test_validator_is_pure_and_state_roundtrips():
    state = make_state(budget_limit=100)
    before = state.model_dump_json()
    assert validate_plan(state).valid
    assert state.model_dump_json() == before
    assert PlanningState.model_validate_json(before) == state


def test_reverse_request_dates_rejected_before_generation_and_reported():
    state = make_state()
    state.trip_request.end_date = state.trip_request.start_date.replace(day=1, month=7)
    with pytest.raises(ValueError):
        ensure_date_range(state.trip_request)
    assert "date_range" in types(validate_plan(state))


@pytest.mark.parametrize("mutation,expected", [
    ("count", "day_count"), ("outside", "day_date"), ("date_index", "day_date"), ("index", "day_index"),
])
def test_date_consistency(mutation, expected):
    state = make_state()
    if mutation == "count":
        state.current_itinerary.days.pop()
    elif mutation == "outside":
        state.current_itinerary.days[0].date = state.trip_request.start_date.replace(day=3)
    elif mutation == "date_index":
        state.current_itinerary.days[0].date = state.trip_request.end_date
    else:
        state.current_itinerary.days[1].day_index = 1
    assert expected in types(validate_plan(state))


@pytest.mark.parametrize("start,end,expected", [
    ("24:00", "25:00", "time_format"), ("09:60", "10:00", "time_format"),
    ("9:00", "10:00", "time_format"), ("09:00:00", "10:00", "time_format"),
    (" 09:00", "10:00", "time_format"), ("10:00", "10:00", "time_order"),
    ("23:00", "01:00", "time_order"), ("", "10:00", "time_format"),
])
def test_time_format_and_order(start, end, expected):
    state = make_state()
    spot = state.current_itinerary.days[0].spots[0]
    spot.start_time, spot.end_time = start, end
    assert expected in types(validate_plan(state))


def test_overlap_nested_and_adjacent_intervals():
    state = make_state()
    day = state.current_itinerary.days[0]
    day.spots[0].end_time = "14:00"
    day.spots.extend([
        SpotItem(name="Nested", start_time="10:00", end_time="11:00"),
        SpotItem(name="Nested2", start_time="12:00", end_time="13:00"),
        SpotItem(name="Adjacent", start_time="14:00", end_time="15:00"),
    ])
    overlaps = [v for v in validate_plan(state).violations if v.type == "activity_overlap"]
    assert len(overlaps) == 2
    assert all(v.activity_ref and v.day_index == 1 for v in overlaps)


@pytest.mark.parametrize("ids,names,duplicate", [
    (("same", "same"), ("Old name", "New name"), True),
    (("a", "b"), ("Same", "Same"), False),
    ((None, None), (" Museum   A ", "museum a"), True),
    (("a", None), ("Museum A", "museum a"), True),
    ((None, None), ("Museum A", "Museum A Annex"), False),
])
def test_duplicate_matching(ids, names, duplicate):
    state = make_state()
    for index, day in enumerate(state.current_itinerary.days):
        day.spots[0].poi_id, day.spots[0].name = ids[index], names[index]
    assert ("duplicate_attraction" in types(validate_plan(state))) == duplicate


def test_duplicate_rule_does_not_apply_to_restaurants_or_hotels():
    state = make_state()
    for day in state.current_itinerary.days:
        day.meals = [MealItem(name="Same restaurant", poi_id="same", meal_type="lunch")]
    assert validate_plan(state).valid


def test_must_visit_and_avoid_use_exact_names_or_explicit_ids():
    state = make_state(must_visit=[{"entity_id": "a", "name": "not used"}, " park   b "], avoid=["Museum"])
    assert validate_plan(state).valid
    state.constraints.must_visit = [PlaceReference(entity_id="missing", name="Museum A")]
    state.constraints.avoid = [PlaceReference(entity_id="b")]
    assert types(validate_plan(state)) == {"must_visit_missing", "avoided_attraction"}


def test_blank_place_constraint_is_invalid():
    assert "invalid_place_reference" in types(validate_plan(make_state(must_visit=[" "])))


@pytest.mark.parametrize("estimate,limit,exceeded", [(100, "100", False), (100.01, "100", True), (0, "0", False)])
def test_budget_decimal_boundary(estimate, limit, exceeded):
    state = make_state(budget_limit=limit)
    state.current_itinerary.estimated_budget = estimate
    state.current_itinerary.budget_breakdown.total = estimate
    assert ("budget_exceeded" in types(validate_plan(state))) == exceeded


def test_budget_mismatch_uses_conservative_total_and_preserves_estimates():
    state = make_state(budget_limit=100)
    state.current_itinerary.budget_breakdown.total = 101
    report = validate_plan(state)
    assert "budget_exceeded" in types(report)
    assert any(v.type == "budget_estimate_mismatch" and v.severity == "warning" for v in report.violations)
    assert state.current_itinerary.estimated_budget == 100


@pytest.mark.parametrize("start,end,kind", [("09:30", "18:00", "daily_time_window"),
    ("25:00", None, "constraint_time_format"), ("18:00", "09:00", "constraint_time_order")])
def test_daily_window(start, end, kind):
    assert kind in types(validate_plan(make_state(daily_start_time=start, daily_end_time=end)))


def test_missing_times_and_unsupported_hard_facts_are_unknown():
    state = make_state(daily_start_time="09:00", daily_end_time="18:00", hard_constraints=["opening_hours", "route_feasibility", "real_ticket_price"], mobility_constraints=["wheelchair"], transport_preference="walk")
    state.current_itinerary.days[0].spots[0].end_time = None
    state.current_itinerary.days[1].meals = [MealItem(name="Lunch", meal_type="lunch")]
    report = validate_plan(state)
    assert not report.valid and report.status == "unverifiable"
    assert {v.expected for v in report.unknowns if v.type == "unverifiable_hard_constraint"} == {
        "opening_hours", "route_feasibility", "real_ticket_price", "mobility_suitability", "transport_preference"}
    assert any(v.type == "activity_time_missing" for v in report.unknowns)
    assert "real_ticket_price" in report.checked_constraints


def test_soft_preferences_do_not_invalidate():
    assert validate_plan(make_state(soft_preferences=["luxury", "quiet", "never walk"])).valid


def test_adapter_preserves_identity_and_does_not_invent_meal_times():
    state = make_state()
    state.current_itinerary.days[0].meals = [MealItem(name="Lunch", meal_type="lunch")]
    activities = scheduled_activities(state.current_itinerary)
    assert activities[0].entity_id == "a"
    assert activities[1].start_time is None


def test_metadata_retains_original_constraints_and_versions():
    state = make_state()
    plan = attach_validation(state.current_itinerary, state.trip_request)
    assert plan.constraints.budget_limit == 100 and plan.planning_version == 1
    plan.estimated_budget = plan.budget_breakdown.total = 120
    attach_validation(plan, increment_version=True)
    assert not plan.validation_report.valid
    assert plan.constraints.budget_limit == 100 and plan.planning_version == 2
    assert Itinerary.model_validate_json(plan.model_dump_json()).constraints.budget_limit == 100


def test_legacy_plan_without_original_request_cannot_claim_validity():
    plan = make_state().current_itinerary
    attach_validation(plan)
    assert plan.validation_report.status == "unverifiable"
    assert any(v.type == "missing_trip_request" for v in plan.validation_report.unknowns)
