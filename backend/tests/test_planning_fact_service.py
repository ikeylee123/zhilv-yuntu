from datetime import timedelta
from pathlib import Path
import sys
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.planning_facts import FactSource
from app.services import planning_fact_service as service, map_service
from app.services.route_validation import query_identity, discover_pairs
from route_fixtures import state, trusted, NOW

REAL_FETCH_IDENTITY = map_service.fetch_place_identity


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Unexpected network/identity lookup")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(service.map_service, "fetch_place_identity", blocked)
    monkeypatch.setattr(service, "get_cached_json", lambda key: None)
    monkeypatch.setattr(service, "set_cached_json", lambda *args, **kwargs: None)


def collect(s, **kwargs):
    return service.collect_route_facts(s.current_itinerary, s.constraints, trusted_pois=trusted(s), now=NOW, **kwargs)


@pytest.mark.parametrize("response,reason", [
    ({"distance_meters": "5000", "duration_seconds": "1200.01"}, None),
    (None, "no_data"), ({"distance_meters": 1}, "invalid_response"),
    ({"distance_meters": 1, "duration_seconds": -1}, "invalid_response"),
    ({"distance_meters": 1, "duration_seconds": float("nan")}, "invalid_response"),
    ({"distance_meters": float("inf"), "duration_seconds": 1}, "invalid_response"),
    ({"duration_seconds": 1, "distance_meters": None}, "invalid_response"),
])
def test_normalize_provider_response(monkeypatch, response, reason):
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: response)
    s = state(); before = s.model_dump_json(); snapshot = collect(s)
    fact = snapshot.route_facts[0]
    assert fact.status == ("unknown" if reason else "known")
    assert fact.reason == reason
    assert snapshot.metrics.actual_provider_calls == 1
    assert s.model_dump_json() == before
    if not reason:
        assert fact.source.fetched_at == NOW
        assert fact.source.evidence_kind == "provider_estimate"
        assert fact.duration_seconds == 1200.01
        assert "key" not in snapshot.model_dump_json()


@pytest.mark.parametrize("exc,reason,outcome", [(httpx.ReadTimeout("timeout"), "timeout", "timeout"),
    (map_service.AmapServiceError("rejected", reason="10001"), "provider_error", "provider_error"),
    (map_service.AmapServiceError("bad", reason="invalid_response"), "invalid_response", "invalid_response")])
def test_failure_mapping(monkeypatch, exc, reason, outcome):
    def fail(*args): raise exc
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", fail)
    snapshot = collect(state())
    assert snapshot.route_facts[0].reason == reason
    assert snapshot.collection_outcomes[0].status == outcome


@pytest.mark.parametrize("mode", [None, "walking", "transit", "cycling", "mixed", "taxi"])
def test_unsupported_mode_never_calls_provider(monkeypatch, mode):
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: pytest.fail("must not call driving"))
    s = state(); s.constraints.transport_preference = mode
    snapshot = collect(s)
    assert snapshot.route_facts[0].reason == "unsupported_mode"
    assert snapshot.metrics.actual_provider_calls == snapshot.metrics.identity_provider_calls == 0


@pytest.mark.parametrize("case", ["coordinates", "identity", "zero", "not_required", "local_uncertain"])
def test_preconditions_do_not_call_provider(monkeypatch, case):
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: pytest.fail("unnecessary call"))
    s = state()
    if case == "coordinates": s.current_itinerary.days[0].spots[0].longitude = None
    if case == "identity": s.current_itinerary.days[0].spots[0].poi_id = None
    if case == "zero": s.current_itinerary.days[0].spots.pop()
    if case == "not_required": s.constraints.require_route_feasibility = False
    snapshot = service.collect_route_facts(s.current_itinerary, s.constraints,
        trusted_pois={} if case == "local_uncertain" else trusted(s), allow_identity_lookup=False, now=NOW)
    assert snapshot.metrics.actual_provider_calls == 0
    assert snapshot.metrics.unnecessary_calls == 0
    if case == "coordinates": assert snapshot.route_facts[0].reason == "missing_coordinates"
    if case in ("identity", "local_uncertain"): assert snapshot.route_facts[0].reason == "identity_uncertain"


def test_identical_queries_are_deduplicated_but_bound_to_distinct_pairs(monkeypatch):
    calls = []
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: calls.append(args) or
                        {"distance_meters": 100, "duration_seconds": 60})
    s = state(); s.current_itinerary.days.append(s.current_itinerary.days[0].model_copy(deep=True, update={"day_index": 2}))
    snapshot = collect(s)
    assert len(calls) == 1 and len(snapshot.route_facts) == 2
    assert snapshot.metrics.logical_route_queries == 2
    assert snapshot.metrics.deduplicated_calls == 1
    assert snapshot.route_facts[0].origin.day_index != snapshot.route_facts[1].origin.day_index


@pytest.mark.parametrize("cache_case", ["fresh", "stale", "no_timestamp", "wrong_query", "future", "legacy"])
def test_versioned_cache_provenance(monkeypatch, cache_case):
    s = state(); pair = discover_pairs(s.current_itinerary)[0][0]
    query = query_identity(pair.origin.coordinates, pair.destination.coordinates)
    source = FactSource(fetched_at=NOW-timedelta(seconds=60)).model_dump(mode="json")
    cached = dict(query_id=query, source=source, distance_meters=100, duration_seconds=60)
    if cache_case == "stale": source["fetched_at"] = (NOW-timedelta(seconds=901)).isoformat()
    if cache_case == "no_timestamp": source["fetched_at"] = None
    if cache_case == "wrong_query": cached["query_id"] = "another-direction"
    if cache_case == "future": source["fetched_at"] = (NOW+timedelta(seconds=1)).isoformat()
    if cache_case == "legacy": cached = {"distance_km": 1, "estimated_minutes": 0}
    keys, writes, calls = [], [], []
    monkeypatch.setattr(service, "get_cached_json", lambda key: keys.append(key) or cached)
    monkeypatch.setattr(service, "set_cached_json", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: calls.append(1) or
                        {"distance_meters": 200, "duration_seconds": 120})
    snapshot = collect(s)
    assert keys[0].startswith("planning:route:v1:")
    if cache_case == "fresh":
        assert not calls and not writes and snapshot.metrics.cache_hits == 1
        assert snapshot.route_facts[0].source.fetched_at == NOW-timedelta(seconds=60)
    else:
        assert calls == [1] and len(writes) == 1
        assert writes[0][1]["expire_seconds"] == 900
        assert snapshot.route_facts[0].source.fetched_at == NOW


def test_stale_cache_refresh_failure_retains_stale_not_known(monkeypatch):
    s = state(); pair = discover_pairs(s.current_itinerary)[0][0]
    cached = dict(query_id=query_identity(pair.origin.coordinates, pair.destination.coordinates),
                  source=FactSource(fetched_at=NOW-timedelta(hours=1)).model_dump(mode="json"),
                  distance_meters=100, duration_seconds=60)
    monkeypatch.setattr(service, "get_cached_json", lambda key: cached)
    def fail(*args): raise httpx.ReadTimeout("offline")
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", fail)
    snapshot = collect(s)
    assert snapshot.route_facts[0].status == "stale"
    assert snapshot.route_facts[0].reason == "stale_fact"
    assert snapshot.collection_outcomes[0].reason == "timeout"


def test_query_identity_includes_direction_mode_provider_strategy():
    pair = discover_pairs(state().current_itinerary)[0][0]
    key = query_identity(pair.origin.coordinates, pair.destination.coordinates)
    assert key != query_identity(pair.destination.coordinates, pair.origin.coordinates)
    assert all(word in key for word in ("amap", "driving", "strategy", "v3"))


@pytest.mark.parametrize("mismatch", [None, "id", "name", "coordinates"])
def test_id_lookup_requires_exact_binding(monkeypatch, mismatch):
    s = state(); places = trusted(s); calls = []
    if mismatch == "id": places["a"]["poi_id"] = "wrong"
    if mismatch == "name": places["a"]["name"] = "Wrong museum"
    if mismatch == "coordinates": places["a"]["longitude"] = 10
    monkeypatch.setattr(map_service, "fetch_place_identity", lambda key: places[key])
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", lambda *args: calls.append(1) or
                        {"distance_meters": 100, "duration_seconds": 60})
    snapshot = service.collect_route_facts(s.current_itinerary, s.constraints, now=NOW)
    assert snapshot.route_facts[0].status == ("unknown" if mismatch else "known")
    assert len(calls) == (0 if mismatch else 1)


def test_fresh_route_adapter_bypasses_legacy_cache_and_preserves_seconds(monkeypatch):
    pair = discover_pairs(state().current_itinerary)[0][0]; calls = []
    monkeypatch.setattr(map_service, "get_cached_json", lambda *args: pytest.fail("legacy cache must not be used"))
    def provider(path, params):
        calls.append((path, params))
        return {"route": {"paths": [{"distance": "5000", "duration": "1200.1"}]}}
    monkeypatch.setattr(map_service, "_request_amap", provider)
    result = map_service.fetch_driving_route_fact(pair.origin.coordinates, pair.destination.coordinates)
    assert result == {"distance_meters": "5000", "duration_seconds": "1200.1"}
    assert calls[0] == ("/direction/driving", {"origin": "120.0,30.0", "destination": "120.1,30.1", "strategy": 0})


@pytest.mark.parametrize("payload,empty", [({"route": {"paths": []}}, True), ({}, False),
    ({"route": []}, False), ({"route": {"paths": [None]}}, False)])
def test_route_adapter_empty_and_malformed(monkeypatch, payload, empty):
    monkeypatch.setattr(map_service, "_request_amap", lambda *args: payload)
    pair = discover_pairs(state().current_itinerary)[0][0]
    if empty:
        assert map_service.fetch_driving_route_fact(pair.origin.coordinates, pair.destination.coordinates) is None
    else:
        with pytest.raises(map_service.AmapServiceError, match="Malformed"):
            map_service.fetch_driving_route_fact(pair.origin.coordinates, pair.destination.coordinates)


def test_id_adapter_never_picks_an_unrelated_first_result(monkeypatch):
    calls = []
    def provider(path, params):
        calls.append((path, params))
        return {"pois": [{"id": "wrong", "name": "Wrong", "location": "1,2"},
                         {"id": "a", "name": "Museum", "location": "120,30"}]}
    monkeypatch.setattr(map_service, "_request_amap", provider)
    assert REAL_FETCH_IDENTITY("a") == {"poi_id": "a", "name": "Museum", "latitude": 30, "longitude": 120}
    assert calls == [("/place/detail", {"id": "a", "extensions": "base"})]


def test_same_run_failed_query_is_also_deduplicated(monkeypatch):
    s = state(); s.current_itinerary.days.append(s.current_itinerary.days[0].model_copy(deep=True, update={"day_index": 2}))
    calls = []
    def fail(*args):
        calls.append(1)
        raise httpx.ReadTimeout("offline")
    monkeypatch.setattr(map_service, "fetch_driving_route_fact", fail)
    snapshot = collect(s)
    assert calls == [1]
    assert snapshot.metrics.deduplicated_calls == 1
    assert all(f.status == "unknown" and f.reason == "timeout" for f in snapshot.route_facts)
