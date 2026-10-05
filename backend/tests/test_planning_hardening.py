"""Independent adversarial regressions; no evaluation fixtures or labels are used."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import Itinerary, PlanningState, TripConstraints, TripRequest
from app.services.plan_validator import attach_validation, constraints_from_request, validate_plan


def request(overrides):
    return TripRequest(destination="Audit City", start_date="2026-08-01", end_date="2026-08-01",
                       travelers=1, budget=100, preferences=["quiet"], constraints=overrides)


def plan_payload():
    return {"trip_id": "hardening", "destination": "Audit City", "summary": "Independent fixture",
            "days": [{"day_index": 1, "date": "2026-08-01", "spots": [
                {"name": "Gallery A", "poi_id": "a", "start_time": "09:00", "end_time": "10:00"}]}],
            "budget_breakdown": {}}


def report(payload, **constraints):
    state = PlanningState(trip_request=request(None), constraints=TripConstraints(**constraints),
                          current_itinerary=Itinerary.model_validate(payload))
    # Presence must survive ordinary serialization, not just direct construction.
    state = PlanningState.model_validate_json(state.model_dump_json())
    before = state.model_dump_json()
    result = validate_plan(state)
    assert state.model_dump_json() == before
    return result


@pytest.mark.parametrize("overrides,field,expected,present", [
    ({}, "budget_limit", Decimal("100"), False),
    ({"must_visit": ["Missing"]}, "budget_limit", Decimal("100"), False),
    ({"budget_limit": None}, "budget_limit", None, True),
    ({"budget_limit": "80.25"}, "budget_limit", Decimal("80.25"), True),
    ({}, "soft_preferences", ["quiet"], False),
    ({"soft_preferences": None}, "soft_preferences", None, True),
    ({"soft_preferences": ["art"]}, "soft_preferences", ["art"], True),
])
def test_override_presence_survives_default_json(overrides, field, expected, present):
    original = request(overrides)
    wire = json.loads(original.model_dump_json())
    assert (field in wire["constraints"]) is present
    restored = TripRequest.model_validate_json(json.dumps(wire))
    assert getattr(constraints_from_request(restored), field) == expected
    # Standalone/nested dict serialization obeys the same wire representation.
    assert original.model_dump(mode="json")["constraints"] == wire["constraints"]
    assert set(original.model_dump()["constraints"]) == set(wire["constraints"])


@pytest.mark.parametrize("metadata", ["absent", "empty", "partial"])
def test_request_fallback_after_json_sqlite_roundtrip(metadata):
    payload = plan_payload()
    payload.update(estimated_budget=150, budget_breakdown={"total": 150})
    plan = attach_validation(Itinerary.model_validate(payload), request({}))
    assert plan.constraints.budget_limit == 100
    assert plan.constraints.soft_preferences == ["quiet"]
    wire = json.loads(plan.model_dump_json())
    wire.pop("constraints")
    if metadata != "absent":
        wire["constraints"] = {} if metadata == "empty" else {"daily_start_time": "08:00"}
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE plans (payload TEXT)")
        connection.execute("INSERT INTO plans VALUES (?)", (json.dumps(wire),))
        loaded = Itinerary.model_validate_json(connection.execute("SELECT payload FROM plans").fetchone()[0])
    attach_validation(loaded, increment_version=True)
    assert loaded.constraints.budget_limit == 100
    assert loaded.constraints.soft_preferences == ["quiet"]
    assert loaded.validation_report.status == "invalid"
    assert {v.type for v in loaded.validation_report.violations} == {"budget_exceeded"}


def test_resolved_explicit_null_overrides_request_on_revalidation():
    payload = plan_payload()
    payload.update(estimated_budget=150, budget_breakdown={"total": 150})
    plan = attach_validation(Itinerary.model_validate(payload), request({}))
    plan.constraints = TripConstraints(budget_limit=None, soft_preferences=None)
    plan = Itinerary.model_validate_json(plan.model_dump_json())
    attach_validation(plan, increment_version=True)
    assert plan.constraints.budget_limit is None
    assert plan.constraints.soft_preferences is None
    assert plan.validation_report.status == "valid"


@pytest.mark.parametrize("estimate,total,partial,limit,status,errors,warnings", [
    (None, None, False, 0, "unverifiable", set(), set()),
    (None, None, True, 0, "unverifiable", set(), set()),
    (120, None, False, 100, "invalid", {"budget_exceeded"}, set()),
    (None, 120, False, 100, "invalid", {"budget_exceeded"}, set()),
    (80, None, False, 100, "valid", set(), set()),
    (None, 80, False, 100, "valid", set(), set()),
    (0, None, False, 0, "valid", set(), set()),
    (None, 0, False, 0, "valid", set(), set()),
    (50, 120, False, 100, "invalid", {"budget_exceeded"}, {"budget_estimate_mismatch"}),
    (120, 50, False, 100, "invalid", {"budget_exceeded"}, {"budget_estimate_mismatch"}),
    (50, 80, False, 100, "valid", set(), {"budget_estimate_mismatch"}),
])
def test_budget_evidence(estimate, total, partial, limit, status, errors, warnings):
    payload = plan_payload()
    if estimate is not None:
        payload["estimated_budget"] = estimate
    if total is not None:
        payload["budget_breakdown"]["total"] = total
    if partial:
        payload["budget_breakdown"]["tickets"] = 500
    result = report(payload, budget_limit=limit)
    assert result.status == status
    assert result.valid is (status == "valid")
    assert {v.type for v in result.violations if v.severity == "error"} == errors
    assert {v.type for v in result.violations if v.severity == "warning"} == warnings
    assert {v.type for v in result.unknowns} == ({"budget_unverifiable"} if status == "unverifiable" else set())


@pytest.mark.parametrize("field,reference_id,activity_id,activity_name,status,error", [
    ("avoid", "a", "a", "Different name", "invalid", "avoided_attraction"),
    ("avoid", "a", "b", "Gallery A", "valid", None),
    ("avoid", "a", None, "  GALLERY   A ", "unverifiable", None),
    ("must_visit", "a", None, "  GALLERY   A ", "unverifiable", None),
    ("avoid", None, None, "  GALLERY   A ", "invalid", "avoided_attraction"),
    ("must_visit", None, None, "  GALLERY   A ", "valid", None),
    ("must_visit", "a", "b", "Gallery A", "invalid", "must_visit_missing"),
    ("must_visit", "a", "a", "Different name", "valid", None),
    ("avoid", "a", None, "Another place", "valid", None),
    ("must_visit", "a", None, "Another place", "invalid", "must_visit_missing"),
])
def test_authoritative_identity_uncertainty(field, reference_id, activity_id, activity_name, status, error):
    payload = plan_payload()
    payload["days"][0]["spots"][0].update(poi_id=activity_id, name=activity_name)
    reference = {"name": "Gallery A"}
    if reference_id is not None:
        reference["entity_id"] = reference_id
    result = report(payload, **{field: [reference]})
    assert result.status == status
    assert {v.type for v in result.violations} == ({error} if error else set())
    assert {v.type for v in result.unknowns} == ({"place_identity_unverifiable"} if status == "unverifiable" else set())


def test_hard_error_keeps_precedence_over_both_new_unknowns():
    payload = plan_payload()
    payload["days"][0]["spots"][0].update(poi_id=None, end_time="09:00")
    result = report(payload, budget_limit=0, avoid=[{"entity_id": "a", "name": "Gallery A"}])
    assert result.status == "invalid" and not result.valid
    assert {v.type for v in result.violations} == {"time_order"}
    assert {v.type for v in result.unknowns} == {"budget_unverifiable", "place_identity_unverifiable"}


def test_known_required_match_is_sufficient_despite_another_uncertain_activity():
    payload = plan_payload()
    payload["days"][0]["spots"].append({"name": "Gallery A", "start_time": "10:00", "end_time": "11:00"})
    result = report(payload, must_visit=[{"entity_id": "a", "name": "Gallery A"}])
    assert result.valid and not result.unknowns  # Adjacent intervals remain allowed.


def test_id_only_reference_cannot_rule_out_unidentified_activity():
    payload = plan_payload()
    payload["days"][0]["spots"][0]["poi_id"] = None
    result = report(payload, avoid=[{"entity_id": "a"}])
    assert result.status == "unverifiable"
    assert {v.type for v in result.unknowns} == {"place_identity_unverifiable"}


@pytest.mark.parametrize("overrides,status", [
    ({}, "unverifiable"),
    ({"budget_limit": None}, "valid"),
    ({"budget_limit": None, "avoid": [{"entity_id": "a", "name": "Gallery A"}]}, "unverifiable"),
])
def test_edit_save_load_preserves_evidence_and_resolved_overrides(monkeypatch, overrides, status):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.schemas import TripEditRequest, ValidationReport
    from app.services import storage_service, trip_service

    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(storage_service, "engine", engine)
    monkeypatch.setattr(storage_service, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(trip_service, "generate_day_edit_draft", lambda *args: (None, {}))
    monkeypatch.setattr(trip_service, "_maybe_enrich_itinerary_with_map_data", lambda plan, **kwargs: plan)
    payload = plan_payload()
    payload["budget_breakdown"]["tickets"] = 500
    payload["days"][0]["spots"][0]["poi_id"] = None
    plan = attach_validation(Itinerary.model_validate(payload), request(overrides))
    before = plan.model_dump_json()
    try:
        edited = trip_service.edit_trip_itinerary(TripEditRequest(
            trip_id=plan.trip_id, current_itinerary=Itinerary.model_validate_json(before),
            user_instruction="unrelated note", edit_scope="day_1"))
        assert edited.constraints == plan.constraints
        assert edited.validation_report.status == status
        edited.validation_report = ValidationReport()  # save must recompute, not trust it
        storage_service.save_itinerary(edited)
        loaded = storage_service.get_itinerary_by_trip_id(plan.trip_id).itinerary
        assert loaded.constraints == plan.constraints
        assert loaded.validation_report.status == status
        assert "estimated_budget" not in loaded.model_dump()
        assert "total" not in loaded.budget_breakdown.model_dump()
        assert plan.model_dump_json() == before
    finally:
        engine.dispose()
