"""Real SQLite state, CAS races, migrations and hostile legacy HTTP payloads."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
import json
import sys
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import Itinerary, TripRequest, TripConstraints, ValidationReport
from app.models.db_models import TripRecord
from app.models.replanning import StateError
from app.services import authoritative_state as service, storage_service as store


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}", connect_args={"timeout": 15})
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    store.init_db()
    yield engine
    engine.dispose()


def plan():
    request = TripRequest(destination="Test City", start_date="2026-10-06", end_date="2026-10-06",
                          travelers=1, budget=100)
    return Itinerary(trip_id="old", destination="Test City", summary="original",
        original_request=request, constraints=TripConstraints(budget_limit=100),
        days=[{"day_index": 1, "date": "2026-10-06", "spots": [
            {"name": "A", "start_time": "09:00", "end_time": "10:00"},
            {"name": "B", "start_time": "11:00", "end_time": "12:00"}]}],
        budget_breakdown={"total": 0}, estimated_budget=0)


def create():
    ids = iter(["activity-a", "activity-b"])
    return service.create_authoritative_draft(plan(), id_factory=lambda: next(ids))


def legacy(payload=None):
    raw = payload if payload is not None else plan().model_dump(mode="json")
    raw.pop("state_schema_version", None)
    with store.SessionLocal() as session:
        session.add(TripRecord(trip_id="old", destination="Test City", summary="original",
                               itinerary_json=json.dumps(raw)))
        session.commit()
    return raw


def snapshot(trip_id):
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=trip_id).one()
        return (row.itinerary_json, row.planning_version, row.state_schema_version, row.baseline_trusted)


def code(expected, function, *args, **kwargs):
    with pytest.raises(StateError) as error:
        function(*args, **kwargs)
    assert error.value.code == expected


def test_generated_ids_version_and_draft_archive(database):
    state = create()
    assert state.trip_id.startswith("trip_") and state.trip_id != "old"
    assert [s.activity_id for s in state.itinerary.days[0].spots] == ["activity-a", "activity-b"]
    assert state.planning_version == state.itinerary.planning_version == 1
    assert state.state_schema_version == 1 and state.replan_eligible
    assert not state.archived and store.list_saved_itineraries().total == 0
    before = snapshot(state.trip_id)
    service.archive_authoritative_plan(state.trip_id)
    assert snapshot(state.trip_id) == before
    assert store.list_saved_itineraries().total == 1
    loaded = service.load_authoritative_plan(state.trip_id)
    assert loaded.archived and loaded.itinerary == state.itinerary
    assert Itinerary.model_validate_json(loaded.itinerary.model_dump_json()) == state.itinerary


def test_generation_discards_supplied_ids_version(database):
    p = plan()
    p.planning_version = 999
    for s in p.days[0].spots: s.activity_id = "forged"
    result = service.create_authoritative_draft(p)
    assert result.planning_version == 1
    assert len({s.activity_id for s in result.itinerary.days[0].spots}) == 2
    assert all(s.activity_id != "forged" for s in result.itinerary.days[0].spots)
    assert p.planning_version == 999


@pytest.mark.parametrize("bad,expected", [("", "INVALID_ACTIVITY_ID"), ("  ", "INVALID_ACTIVITY_ID"),
    (None, "INVALID_ACTIVITY_ID"), ("activity-a", "DUPLICATE_ACTIVITY_ID")])
def test_managed_invalid_ids_rejected(database, bad, expected):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    candidate.days[0].spots[1].activity_id = bad
    before = snapshot(state.trip_id)
    code(expected, service.compare_and_swap_plan, state.trip_id, 1, candidate)
    assert snapshot(state.trip_id) == before


def test_cas_success_then_stale_preserves_ids(database):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    candidate.summary = "new"
    updated = service.compare_and_swap_plan(state.trip_id, 1, candidate)
    assert updated.planning_version == 2
    assert candidate.planning_version == 1
    assert updated.days == state.itinerary.days
    before = snapshot(state.trip_id)
    code("VERSION_CONFLICT", service.compare_and_swap_plan, state.trip_id, 1, candidate)
    assert snapshot(state.trip_id) == before
    loaded = service.load_authoritative_plan(state.trip_id)
    assert loaded.planning_version == loaded.itinerary.planning_version == 2


@pytest.mark.parametrize("version", [0, -1, 999, True, "1"])
def test_arbitrary_candidate_version_rejected(database, version):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    candidate.planning_version = version
    before = snapshot(state.trip_id)
    code("STATE_VERSION_MISMATCH", service.compare_and_swap_plan, state.trip_id, 1, candidate)
    assert snapshot(state.trip_id) == before


@pytest.mark.parametrize("kind", ["invalid", "unknown"])
def test_failed_validation_no_increment(database, kind):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    candidate.days[0].spots[0].end_time = "08:00" if kind == "invalid" else None
    candidate.validation_report = ValidationReport()
    before = snapshot(state.trip_id)
    code("VALIDATION_REJECTED" if kind == "invalid" else "BASELINE_UNVERIFIABLE",
         service.compare_and_swap_plan, state.trip_id, 1, candidate)
    assert snapshot(state.trip_id) == before


def test_two_actual_concurrent_cas_exactly_one_wins(database, monkeypatch):
    state = create()
    barrier = Barrier(2)
    original_load = service.load_authoritative_plan
    def synchronized_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(service, "load_authoritative_plan", synchronized_load)
    def write(label):
        candidate = state.itinerary.model_copy(deep=True)
        candidate.summary = label
        try:
            return service.compare_and_swap_plan(state.trip_id, 1, candidate).summary
        except StateError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ["left", "right"]))
    assert results.count("VERSION_CONFLICT") == 1
    winner = next(value for value in results if value != "VERSION_CONFLICT")
    loaded = original_load(state.trip_id)
    assert loaded.planning_version == 2 and loaded.itinerary.summary == winner


@pytest.mark.parametrize("field,value", [("planning_version", 9), ("planning_version", True),
    ("state_schema_version", None), ("trip_id", "other")])
def test_storage_mismatch_is_detected(database, field, value):
    state = create()
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=state.trip_id).one()
        raw = json.loads(row.itinerary_json)
        raw[field] = value
        row.itinerary_json = json.dumps(raw)
        session.commit()
    before = snapshot(state.trip_id)
    with pytest.raises(StateError): service.load_authoritative_plan(state.trip_id)
    assert snapshot(state.trip_id) == before


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("old_version", [None, 7])
def test_migration_only_missing_ids_idempotent_untrusted(database, partial, old_version):
    raw = plan().model_dump(mode="json")
    raw["planning_version"] = old_version
    raw["validation_report"] = ValidationReport().model_dump(mode="json")
    if partial: raw["days"][0]["spots"][0]["activity_id"] = "existing-id"
    legacy(raw)
    ids = iter(["filled-1", "filled-2"])
    migrated = service.load_authoritative_plan("old", id_factory=lambda: next(ids))
    assert migrated.planning_version == 1
    assert migrated.itinerary.validation_report is None and migrated.itinerary.planning_facts is None
    assert not migrated.replan_eligible
    code("BASELINE_UNVERIFIABLE", migrated.require_replan_eligible)
    values = [s.activity_id for s in migrated.itinerary.days[0].spots]
    assert values == (["existing-id", "filled-1"] if partial else ["filled-1", "filled-2"])
    before = snapshot("old")
    loaded = service.load_authoritative_plan("old", id_factory=lambda: pytest.fail("regenerated"))
    assert loaded == migrated and snapshot("old") == before


@pytest.mark.parametrize("failure", ["duplicate", "blank", "factory"])
def test_migration_failure_rolls_back(database, failure):
    raw = plan().model_dump(mode="json")
    if failure == "duplicate":
        for spot in raw["days"][0]["spots"]: spot["activity_id"] = "same"
    if failure == "blank": raw["days"][0]["spots"][1]["activity_id"] = " "
    legacy(raw)
    before = snapshot("old")
    def fail(): raise RuntimeError("factory failure")
    with pytest.raises((StateError, RuntimeError)):
        service.load_authoritative_plan("old", id_factory=fail if failure == "factory" else service.new_activity_id)
    assert snapshot("old") == before


def test_transaction_commit_failure_rolls_back_migration(database, monkeypatch):
    from sqlalchemy.orm import Session
    legacy()
    before = snapshot("old")
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("disk error")))
        with pytest.raises(RuntimeError): service.load_authoritative_plan("old")
    assert snapshot("old") == before


def test_legacy_without_request_readable_archivable(database):
    raw = plan().model_dump(mode="json")
    raw.pop("original_request"); raw.pop("constraints")
    legacy(raw)
    state = service.load_authoritative_plan("old")
    assert state.itinerary.original_request is None and state.itinerary.constraints is None
    assert not state.replan_eligible
    service.archive_authoritative_plan("old")
    assert store.get_itinerary_by_trip_id("old").itinerary == state.itinerary


def test_old_schema_upgrade_is_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE trip_records (id INTEGER PRIMARY KEY, trip_id VARCHAR UNIQUE, "
            "destination VARCHAR, summary TEXT, itinerary_json TEXT, created_at DATETIME, updated_at DATETIME)")
        connection.execute(text("INSERT INTO trip_records (trip_id,destination,summary,itinerary_json) "
            "VALUES ('old','Test City','original',:raw)"), {"raw": plan().model_dump_json()})
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    store.init_db(); store.init_db()
    assert service.load_authoritative_plan("old").planning_version == 1
    engine.dispose()


@pytest.mark.parametrize("operation", ["save", "edit", "downgrade_save", "downgrade_edit"])
def test_http_bypass_cannot_replace_managed_base(database, operation, monkeypatch):
    from app.api.main import app
    from app.services import trip_service
    state = create()
    forged = state.itinerary.model_dump(mode="json")
    forged["summary"] = "attacker"
    forged["days"][0]["spots"][0].update(name="fake B", activity_id="fake-id")
    forged["constraints"] = {"budget_limit": None}
    forged["planning_version"] = 999
    forged["validation_report"] = ValidationReport().model_dump(mode="json")
    if operation.startswith("downgrade"): forged.pop("state_schema_version")
    before = snapshot(state.trip_id)
    monkeypatch.setattr(trip_service, "generate_day_edit_draft", lambda *a: pytest.fail("LLM called"))
    with TestClient(app) as client:
        if operation.endswith("save"):
            response = client.post("/trip/save", json={"trip_id":state.trip_id,"itinerary":forged})
            assert response.status_code == 200  # Archive only, no plan mutation.
        else:
            response = client.post("/trip/edit", json={"trip_id":state.trip_id,
                "current_itinerary":forged,"user_instruction":"replace everything"})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "LEGACY_WRITE_FORBIDDEN"
    assert snapshot(state.trip_id) == before


def test_unknown_http_client_cannot_create_managed_state(database):
    from app.api.main import app
    raw = plan().model_dump(mode="json")
    raw["state_schema_version"] = 1
    raw["planning_version"] = 99
    with TestClient(app) as client:
        response = client.post("/trip/save", json={"trip_id":"old","itinerary":raw})
        assert response.status_code == 409
    code("STATE_NOT_FOUND", service.load_authoritative_plan, "old")


def test_legacy_http_import_metadata_not_trusted(database):
    from app.api.main import app
    raw = plan().model_dump(mode="json")
    raw["planning_version"] = 999
    for spot in raw["days"][0]["spots"]: spot["activity_id"] = "client-id"
    with TestClient(app) as client:
        assert client.post("/trip/save", json={"trip_id":"old","itinerary":raw}).status_code == 200
    state = service.load_authoritative_plan("old")
    assert not state.replan_eligible and state.planning_version == 1
    assert state.itinerary.constraints is not None  # empty resolved legacy constraints, not client rules
    assert state.itinerary.constraints.budget_limit is None
    assert state.itinerary.original_request is None
    assert all(s.activity_id != "client-id" for s in state.itinerary.days[0].spots)


def test_internal_legacy_save_protected(database):
    state = create()
    downgraded = state.itinerary.model_copy(deep=True)
    downgraded.state_schema_version = None
    before = snapshot(state.trip_id)
    code("LEGACY_WRITE_FORBIDDEN", store.save_itinerary, downgraded)
    assert snapshot(state.trip_id) == before


def test_generate_opt_in_materializes_server_draft(database, monkeypatch):
    from app.api.main import app
    from app.api.routes import trip
    from app.services.city_registry_service import CityCoverageTier
    from types import SimpleNamespace
    monkeypatch.setattr(trip,"resolve_city",lambda _:SimpleNamespace(tier=CityCoverageTier.CURATED,city="Test City"))
    monkeypatch.setattr(trip,"generate_trip_itinerary",lambda _:plan())
    with TestClient(app) as client:
        response = client.post("/trip/generate?managed_state=true",json=plan().original_request.model_dump(mode="json"))
    assert response.status_code == 200
    result = response.json()
    assert result["state_schema_version"] == result["planning_version"] == 1
    loaded = service.load_authoritative_plan(result["trip_id"])
    assert loaded.replan_eligible and not loaded.archived
    assert len({s["activity_id"] for s in result["days"][0]["spots"]}) == 2


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "blank", "db_version"])
def test_managed_load_rejects_corrupt_identity_or_row_version(database, mutation):
    state = create()
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=state.trip_id).one()
        raw = json.loads(row.itinerary_json)
        if mutation == "db_version": row.planning_version = 10
        elif mutation == "missing": raw["days"][0]["spots"][0].pop("activity_id")
        elif mutation == "duplicate": raw["days"][0]["spots"][1]["activity_id"] = "activity-a"
        else: raw["days"][0]["spots"][0]["activity_id"] = " "
        row.itinerary_json = json.dumps(raw)
        session.commit()
    before = snapshot(state.trip_id)
    with pytest.raises(StateError): service.load_authoritative_plan(state.trip_id)
    assert snapshot(state.trip_id) == before


def test_legacy_forged_facts_are_discarded(database):
    from route_fixtures import state, facts
    base = state()
    raw = plan().model_dump(mode="json")
    raw["planning_facts"] = facts(base).model_dump(mode="json")
    legacy(raw)
    migrated = service.load_authoritative_plan("old")
    assert migrated.itinerary.planning_facts is None
    assert migrated.itinerary.validation_report is None
    assert not migrated.baseline_trusted


def test_legacy_write_racing_migration_cannot_downgrade(database, monkeypatch):
    legacy()
    import app.services.planning_fact_service as fact_service
    def migrate_during_validation(candidate):
        service.load_authoritative_plan("old")
        return candidate
    monkeypatch.setattr(store, "attach_server_validation", migrate_during_validation)
    code("LEGACY_WRITE_FORBIDDEN", store.save_itinerary, plan())
    assert service.load_authoritative_plan("old").state_schema_version == 1


def test_state_api_reports_eligibility_and_stable_migration(database):
    from app.api.main import app
    legacy()
    with TestClient(app) as client:
        first = client.get("/trip/old/state")
        second = client.get("/trip/old/state")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["replan_eligible"] is False


@pytest.mark.parametrize("field", ["constraints", "original_request", "destination"])
def test_internal_cas_cannot_change_baseline(database, field):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    if field == "constraints": candidate.constraints.budget_limit = None
    elif field == "original_request": candidate.original_request.travelers = 99
    else: candidate.destination = "Other City"
    before = snapshot(state.trip_id)
    code("BASELINE_CHANGE_FORBIDDEN", service.compare_and_swap_plan, state.trip_id, 1, candidate)
    assert snapshot(state.trip_id) == before
