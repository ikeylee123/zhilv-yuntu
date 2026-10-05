"""Offline workflow/storage regression tests. All model/tool boundaries are replaced."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.main import app
from app.api.routes import trip as routes
from app.models.schemas import (
    BudgetBreakdown, DayPlan, Itinerary, TripConstraints, TripEditRequest, TripRequest, ValidationReport,
)
from app.services import plan_validator, storage_service, trip_service
from app.services.city_registry_service import CityCoverageTier
from app.services.place_candidate_service import CityCandidatePool, PlaceCandidate, PlaceCandidateCategory


def request(**extra):
    return TripRequest(destination="Test City", start_date="2026-08-01", end_date="2026-08-02",
                       travelers=2, budget=1000, **extra)


def pool():
    return CityCandidatePool(city="Test City", adcode="100100", minimum_counts={c: 1 for c in PlaceCandidateCategory},
        candidates={c: [PlaceCandidate(poi_id=f"{c.value}-{i}", name=f"{c.value} {i}", category=c,
                       address="Test Road", city="Test City", district="District", type_name=c.value,
                       latitude=30.0, longitude=120.0, image_url=None) for i in (1, 2)]
                    for c in PlaceCandidateCategory})


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Fail, rather than silently use a real model or API, if a new path bypasses these stubs.
    import httpx
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in Phase 1 tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)
    monkeypatch.setattr(trip_service, "collect_trip_context", lambda **kw: ([], {}, {}, {}))
    monkeypatch.setattr(trip_service, "generate_planner_draft", lambda *args: (None, {}))
    monkeypatch.setattr(trip_service, "generate_dynamic_planner_draft", lambda **kw: (None, {}))
    monkeypatch.setattr(trip_service, "generate_day_edit_draft", lambda *args: (None, {}))


@pytest.mark.parametrize("dynamic", [False, True])
def test_generate_runs_validator_once_and_exposes_invalid_report_without_repair(monkeypatch, dynamic):
    calls, model_calls = [], []
    original = plan_validator.validate_plan
    def spy(state):
        calls.append(state)
        return original(state)
    def model(*args, **kwargs):
        model_calls.append(1)
        return None, {}
    monkeypatch.setattr(plan_validator, "validate_plan", spy)
    monkeypatch.setattr(trip_service, "generate_planner_draft", model)
    monkeypatch.setattr(trip_service, "generate_dynamic_planner_draft", model)
    tier = CityCoverageTier.DYNAMIC if dynamic else CityCoverageTier.CURATED
    monkeypatch.setattr(routes, "resolve_city", lambda city: SimpleNamespace(
        tier=tier, city="Test City", adcode="100100", administrative_level="city"))
    monkeypatch.setattr(routes, "collect_city_candidate_pool", lambda **kw: pool())
    payload = request(constraints=TripConstraints(must_visit=["Definitely Missing"])).model_dump(mode="json", exclude_unset=True)
    response = TestClient(app).post("/trip/generate", json=payload)
    assert response.status_code == 200
    result = response.json()
    assert len(calls) == 1 and len(model_calls) == 1  # No hidden repair invocation.
    assert not result["validation_report"]["valid"]
    assert any(v["type"] == "must_visit_missing" for v in result["validation_report"]["violations"])
    assert result["original_request"]["budget"] == 1000
    assert result["constraints"]["budget_limit"] == "1000.0"
    assert result["planning_version"] == 1
    assert calls[0].validation_report is not None


def test_reverse_dates_fail_before_city_tools_and_models(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("No tool/model may run for invalid dates")
    monkeypatch.setattr(routes, "resolve_city", unexpected)
    invalid = request()
    invalid.end_date = invalid.start_date.replace(month=7)
    response = TestClient(app).post("/trip/generate", json=invalid.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_date_range"
    monkeypatch.setattr(trip_service, "collect_trip_context", unexpected)
    monkeypatch.setattr(trip_service, "generate_dynamic_planner_draft", unexpected)
    with pytest.raises(ValueError):
        trip_service.generate_trip_itinerary(invalid)
    with pytest.raises(ValueError):
        trip_service.generate_dynamic_trip_itinerary(invalid, pool())


def test_report_is_computed_after_map_enrichment(monkeypatch):
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", True)
    def enrich(plan, city=None):
        from app.models.schemas import SpotItem
        plan.days[0].spots = [SpotItem(name="Mapped", start_time="09:00", end_time="10:00", estimated_cost=2000)]
        return plan
    monkeypatch.setattr(trip_service, "enrich_itinerary_with_map_data", enrich)
    plan = trip_service.generate_trip_itinerary(request())
    assert any(v.type == "budget_exceeded" for v in plan.validation_report.violations)


def test_edit_keeps_request_and_budget_limit_and_revalidates(monkeypatch):
    initial = request(constraints=TripConstraints(must_visit=[{"entity_id": "spot-1"}], budget_limit=10))
    plan = trip_service.generate_dynamic_trip_itinerary(initial, pool())
    before = plan.model_dump_json()
    edited = trip_service.edit_trip_itinerary(TripEditRequest(
        trip_id=plan.trip_id, current_itinerary=plan, edit_scope="day_1",
        user_instruction="\u53d6\u6d88\u666f\u70b9",  # existing deterministic cancellation phrase
    ))
    assert plan.model_dump_json() == before
    assert edited.original_request == plan.original_request
    assert edited.constraints.budget_limit == 10
    assert edited.planning_version == 2
    assert edited.days[1] == plan.days[1]
    assert any(v.type == "must_visit_missing" for v in edited.validation_report.violations)
    assert not edited.validation_report.valid


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'phase1.db').as_posix()}")
    monkeypatch.setattr(storage_service, "engine", engine)
    monkeypatch.setattr(storage_service, "SessionLocal", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


def test_storage_roundtrip_recomputes_report_and_preserves_metadata(isolated_store):
    plan = trip_service.generate_dynamic_trip_itinerary(request(), pool())
    plan.constraints.must_visit = ["Missing after client edit"]
    plan.validation_report = ValidationReport()  # stale/forged success report
    plan.planning_version = 3
    storage_service.save_itinerary(plan)
    loaded = storage_service.get_itinerary_by_trip_id(plan.trip_id).itinerary
    assert loaded.original_request == plan.original_request
    assert loaded.constraints == plan.constraints
    assert loaded.planning_version == 3
    assert not loaded.validation_report.valid
    assert any(v.type == "must_visit_missing" for v in loaded.validation_report.violations)
    assert plan.validation_report.valid  # saving does not mutate the caller's object


def test_old_json_row_loads_without_metadata_and_can_be_saved(isolated_store):
    from app.models.db_models import TripRecord
    storage_service.init_db()
    payload = dict(trip_id="legacy", destination="Test City", summary="Legacy", days=[],
                   budget_breakdown={"total":0}, estimated_budget=0)
    with storage_service.SessionLocal() as session:
        session.add(TripRecord(trip_id="legacy", destination="Test City", summary="Legacy", itinerary_json=json.dumps(payload)))
        session.commit()
    plan = storage_service.get_itinerary_by_trip_id("legacy").itinerary
    assert plan.original_request is None and plan.validation_report is None
    storage_service.save_itinerary(plan)
    loaded = storage_service.get_itinerary_by_trip_id("legacy").itinerary
    assert loaded.validation_report.status == "unverifiable"
    assert loaded.planning_version == 1
