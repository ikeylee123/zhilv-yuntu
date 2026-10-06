"""Independent raw-payload regression tests for route evidence hardening."""
from decimal import Decimal
from pathlib import Path
import sys
import httpx
import pytest
from pydantic import ValidationError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.planning_facts import RouteFact, RouteCoverage, FactSource
from app.services import planning_fact_service as service, map_service
from app.services.route_validation import discover_pairs, query_identity
from route_fixtures import state, facts, trusted, NOW


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a, **k: pytest.fail("live network"))
    monkeypatch.setattr(service, "get_cached_json", lambda key: None)
    monkeypatch.setattr(service, "set_cached_json", lambda *a, **k: None)
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda *a: pytest.fail("identity lookup"))


@pytest.mark.parametrize("distance,duration,accepted", [
    (True, 600, False), (1200, False, False), (True, False, False),
    ("1200", "600", True), ("1200.5", "600.5", True), (0, 0, True),
    (0.0, 0.0, True), (1200, 600, True), (1200.5, 600.5, True),
    (-1, 600, False), (1200, -1, False), (float("nan"), 600, False),
    (1200, float("nan"), False), (float("inf"), 600, False),
    (1200, float("-inf"), False), ("", 600, False), (1200, "bad", False),
    ("NaN", 600, False), (1200, "Infinity", False), (1200, "-Infinity", False),
])
def test_raw_provider_to_server_report(monkeypatch, distance, duration, accepted):
    # Keep the actual adapter and collector: supply raw provider fields only.
    monkeypatch.setattr(map_service, "_request_amap", lambda *a: {
        "route": {"paths": [{"distance": distance, "duration": duration}]}})
    s = state(); plan = s.current_itinerary
    plan.constraints = s.constraints; plan.original_request = s.trip_request
    result = service.attach_server_validation(plan, trusted_pois=trusted(s), allow_identity_lookup=False)
    fact = result.planning_facts.route_facts[0]
    outcome = result.planning_facts.collection_outcomes[0]
    report = result.validation_report
    if accepted:
        assert fact.status == "known" and outcome.status == "success"
        assert fact.distance_meters == float(distance) and fact.duration_seconds == float(duration)
        assert report.status == "valid" and report.route_coverage.verified_pairs == 1
    else:
        assert fact.status == "unknown" and fact.reason == "invalid_response"
        assert outcome.status == "invalid_response"
        assert report.status == "unverifiable"
        assert report.route_coverage.verified_pairs == 0
        assert report.route_coverage.unknown_pairs == 1
        assert any(v.type == "route_feasibility_unverifiable" for v in report.unknowns)


@pytest.mark.parametrize("field", ["distance_meters", "duration_seconds"])
@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("model", [RouteFact, service.CachedRoute])
def test_direct_models_reject_bool(field, value, model):
    raw = facts(state()).route_facts[0].model_dump() if model is RouteFact else dict(
        query_id="query", source=FactSource(fetched_at=NOW), distance_meters=1200, duration_seconds=600)
    raw[field] = value
    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize("field", ["distance_meters", "duration_seconds"])
def test_bool_cache_cannot_be_reused(monkeypatch, field):
    s = state(); pair = discover_pairs(s.current_itinerary)[0][0]
    cached = dict(query_id=query_identity(pair.origin.coordinates, pair.destination.coordinates),
                  source=FactSource(fetched_at=NOW).model_dump(mode="json"),
                  distance_meters=1200, duration_seconds=600)
    cached[field] = False
    monkeypatch.setattr(service, "get_cached_json", lambda key: cached)
    calls = []
    def provider(*args):
        calls.append(1)
        return {"route": {"paths": [{"distance": True, "duration": False}]}}
    monkeypatch.setattr(map_service, "_request_amap", provider)
    result = service.collect_route_facts(s.current_itinerary, s.constraints, trusted_pois=trusted(s), now=NOW)
    assert calls == [1] and result.metrics.cache_hits == 0
    assert result.route_facts[0].status == "unknown"
    assert result.route_facts[0].reason == "invalid_response"


@pytest.mark.parametrize("endpoint", ["origin", "destination"])
@pytest.mark.parametrize("field,value", [("entity_id", " "), ("entity_id", ""),
    ("entity_id", None), ("day_index", -1), ("activity_ref", " "), ("name", "")])
def test_known_endpoint_rejects_unusable_identity(endpoint, field, value):
    raw = facts(state()).route_facts[0].model_dump(); raw[endpoint][field] = value
    with pytest.raises(ValidationError): RouteFact.model_validate(raw)


def test_known_valid_endpoint_and_unknown_partial_endpoint():
    raw = facts(state()).route_facts[0].model_dump(); raw["origin"]["day_index"] = 0
    assert RouteFact.model_validate(raw).status == "known"
    raw["status"] = "unknown"; raw["origin"]["entity_id"] = None
    assert RouteFact.model_validate(raw).status == "unknown"


@pytest.mark.parametrize("counts", [dict(required_pairs=1, verified_pairs=2),
    dict(required_pairs=1, verified_pairs=1, unknown_pairs=1), dict(unknown_pairs=1),
    *[{field: -1} for field in ("required_pairs", "verified_pairs", "unknown_pairs", "skipped_pairs")]])
def test_coverage_rejects_impossible_counts(counts):
    with pytest.raises(ValidationError): RouteCoverage(**counts)


def test_coverage_mixed_zero_and_roundtrip():
    coverage = RouteCoverage(required_pairs=2, verified_pairs=1, unknown_pairs=1, skipped_pairs=10)
    assert coverage.verified_fraction == 0.5
    assert RouteCoverage.model_validate_json(coverage.model_dump_json()) == coverage
    assert RouteCoverage(skipped_pairs=10).verified_fraction is None


def test_bool_response_does_not_hide_other_hard_errors(monkeypatch):
    monkeypatch.setattr(map_service, "_request_amap", lambda *a: {
        "route": {"paths": [{"distance": True, "duration": False}]}})
    s = state(); s.constraints.budget_limit = Decimal("0")
    s.current_itinerary.constraints = s.constraints; s.current_itinerary.original_request = s.trip_request
    result = service.attach_server_validation(s.current_itinerary, trusted_pois=trusted(s))
    assert result.validation_report.status == "invalid"
    assert result.validation_report.route_coverage.verified_pairs == 0
    assert any(v.type == "budget_exceeded" for v in result.validation_report.violations)
    assert any(v.type == "route_feasibility_unverifiable" for v in result.validation_report.unknowns)
