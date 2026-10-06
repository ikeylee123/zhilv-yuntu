from datetime import datetime, timezone
from pathlib import Path
import sys
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import TripConstraints, TripEditRequest, ValidationReport
from app.services import planning_fact_service as service, trip_service, storage_service, map_service
from app.services.plan_validator import attach_validation
from route_fixtures import state, facts, trusted


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a, **k: pytest.fail("live network"))
    monkeypatch.setattr(service, "get_cached_json", lambda key: None)
    monkeypatch.setattr(service, "set_cached_json", lambda *a, **k: None)
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)
    monkeypatch.setattr(trip_service, "collect_trip_context", lambda **kw: ([], {}, {}, {}))
    monkeypatch.setattr(trip_service, "generate_planner_draft", lambda *a: (None, {}))
    monkeypatch.setattr(trip_service, "generate_dynamic_planner_draft", lambda **kw: (None, {}))
    monkeypatch.setattr(trip_service, "generate_day_edit_draft", lambda *a: (None, {}))
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *a: pytest.fail("unexpected route call"))
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda *a: pytest.fail("unexpected identity call"))


def test_local_generation_keeps_zero_pair_limitation():
    s = state(); s.trip_request.constraints = s.constraints
    plan = trip_service.generate_trip_itinerary(s.trip_request)
    assert plan.planning_facts is not None
    assert plan.validation_report.route_coverage.required_pairs == 0
    assert not plan.validation_report.valid
    assert any(v.type == "route_feasibility_unverifiable" for v in plan.validation_report.unknowns)
    assert plan.planning_facts.metrics.actual_provider_calls == 0


def test_dynamic_generation_keeps_zero_pair_limitation():
    from app.services.place_candidate_service import PlaceCandidate, PlaceCandidateCategory, CityCandidatePool
    s = state(); s.trip_request.constraints = s.constraints
    pool = CityCandidatePool(city="Test City", adcode=None, candidates={category: [PlaceCandidate(
        poi_id=category.value, name=category.value, category=category, address=None, city="Test City",
        district=None, type_name=None, latitude=30, longitude=120, image_url=None)] for category in PlaceCandidateCategory})
    plan = trip_service.generate_dynamic_trip_itinerary(s.trip_request, pool)
    assert plan.validation_report.route_coverage.required_pairs == 0
    assert plan.planning_facts.metrics.actual_provider_calls == 0
    assert not plan.validation_report.valid


def test_local_collection_runs_after_last_map_modification(monkeypatch):
    s = state(); s.trip_request.constraints = s.constraints
    def enrich(plan, city=None):
        plan.days[0].spots = s.current_itinerary.days[0].spots
        return plan
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", True)
    monkeypatch.setattr(trip_service, "enrich_itinerary_with_map_data", enrich)
    plan = trip_service.generate_trip_itinerary(s.trip_request)
    assert plan.validation_report.route_coverage.required_pairs == 1
    assert plan.planning_facts.route_facts[0].reason == "identity_uncertain"
    assert plan.planning_facts.metrics.identity_provider_calls == 0


def test_edit_discards_old_snapshot_rebinds_and_recollects(monkeypatch):
    s = state(); s.trip_request.constraints = s.constraints
    plan = attach_validation(s.current_itinerary, s.trip_request, facts=facts(s, 0), evaluation_time=s.evaluation_time)
    plan.days[0].spots[1].start_time = "11:10"
    identities = trusted(s); route_calls = []
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda id: identities[id])
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *a: route_calls.append(a) or
                        {"distance_meters": 5000, "duration_seconds": 900})
    before = plan.model_dump_json()
    edited = trip_service.edit_trip_itinerary(TripEditRequest(trip_id=plan.trip_id,
        current_itinerary=plan, user_instruction="unrelated note", edit_scope="day_1"))
    assert len(route_calls) == 1
    assert edited.planning_facts.metadata.plan_fingerprint != plan.planning_facts.metadata.plan_fingerprint
    assert any(v.type == "route_time_violation" for v in edited.validation_report.violations)
    assert plan.model_dump_json() == before


def test_save_ignores_forged_client_facts_and_persists_server_evidence(monkeypatch):
    s = state(); s.trip_request.constraints = s.constraints
    plan = attach_validation(s.current_itinerary, s.trip_request, facts=facts(s, 0), evaluation_time=s.evaluation_time)
    plan.validation_report = ValidationReport()
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda id: trusted(s)[id])
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *a: {"distance_meters": 5000, "duration_seconds": 2100})
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(storage_service, "engine", engine)
    monkeypatch.setattr(storage_service, "SessionLocal", sessionmaker(bind=engine))
    before = plan.model_dump_json()
    try:
        storage_service.save_itinerary(plan)
        loaded = storage_service.get_itinerary_by_trip_id(plan.trip_id).itinerary
        assert loaded.validation_report.status == "invalid"
        assert loaded.planning_facts.route_facts[0].duration_seconds == 2100
        assert loaded.planning_facts.metadata.run_id != plan.planning_facts.metadata.run_id
        assert plan.model_dump_json() == before
    finally:
        engine.dispose()


def test_save_cannot_reuse_forged_success_when_provider_fails(monkeypatch):
    s = state(); s.trip_request.constraints = s.constraints
    plan = attach_validation(s.current_itinerary, s.trip_request, facts=facts(s, 0), evaluation_time=s.evaluation_time)
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda id: trusted(s)[id])
    def fail(*args): raise httpx.ReadTimeout("timeout")
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", fail)
    result = service.attach_server_validation(plan)
    assert result.validation_report.status == "unverifiable"
    assert result.planning_facts.route_facts[0].reason == "timeout"


def test_direct_attachment_does_not_trust_itinerary_metadata():
    s = state(); s.trip_request.constraints = s.constraints
    plan = s.current_itinerary; plan.planning_facts = facts(s, 0)
    attach_validation(plan, s.trip_request)
    assert plan.planning_facts is None
    assert plan.validation_report.status == "unverifiable"


@pytest.mark.parametrize("mutation", ["coordinates", "mode", "identity"])
def test_edit_rechecks_changed_binding_or_mode(monkeypatch, mutation):
    s = state(); s.trip_request.constraints = s.constraints
    plan = attach_validation(s.current_itinerary, s.trip_request, facts=facts(s, 0), evaluation_time=s.evaluation_time)
    identities = trusted(s)
    if mutation == "coordinates": plan.days[0].spots[0].longitude = 121
    if mutation == "mode": plan.constraints.transport_preference = "walking"
    if mutation == "identity": plan.days[0].spots[0].poi_id = "not-found"
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda id: identities.get(id))
    edited = trip_service.edit_trip_itinerary(TripEditRequest(trip_id=plan.trip_id,
        current_itinerary=plan, user_instruction="unrelated note"))
    assert edited.validation_report.status == "unverifiable"
    assert edited.planning_facts.metrics.actual_provider_calls == 0
    assert edited.planning_facts.route_facts[0].reason == (
        "unsupported_mode" if mutation == "mode" else "identity_uncertain")
