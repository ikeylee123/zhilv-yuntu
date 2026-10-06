"""Read/write state invariants against real SQLite; no provider or LLM calls."""
import json
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from app.models.db_models import TripRecord
from app.models.replanning import AuthoritativePlanState, StateError
from app.services import authoritative_state as service, storage_service as store
from test_authoritative_state import database, create, plan


def row_snapshot(trip_id):
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=trip_id).one()
        return tuple(getattr(row, col.name) for col in TripRecord.__table__.columns)


@pytest.mark.parametrize("field,values", [
    ("state_schema_version", [1.0, True, False, "1", None, -1, 0, 2]),
    ("planning_version", [1.0, 2.0, True, False, "1", None, -1, 0, 99]),
])
@pytest.mark.parametrize("method", ["assignment", "model_copy"])
def test_invalid_versions_never_update(database, field, values, method):
    state = create()
    before = row_snapshot(state.trip_id)
    for value in values:
        candidate = state.itinerary.model_copy(deep=True)
        if method == "assignment": setattr(candidate, field, value)
        else: candidate = candidate.model_copy(update={field: value})
        updates = []
        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE"): updates.append(statement)
        event.listen(database, "before_cursor_execute", record)
        try:
            with pytest.raises(StateError) as error:
                service.compare_and_swap_plan(state.trip_id, 1, candidate)
            assert error.value.code == "STATE_VERSION_MISMATCH"
            assert not updates
        finally: event.remove(database, "before_cursor_execute", record)
        assert row_snapshot(state.trip_id) == before
        loaded = service.load_authoritative_plan(state.trip_id)
        assert loaded.planning_version == 1 and loaded.itinerary == state.itinerary


def test_successful_commit_immediate_read_exact_revision(database):
    state = create()
    for revision in range(1, 4):
        candidate = state.itinerary.model_copy(deep=True)
        candidate.summary = f"revision {revision + 1}"
        committed = service.compare_and_swap_plan(state.trip_id, revision, candidate)
        state = service.load_authoritative_plan(state.trip_id)
        assert state.planning_version == committed.planning_version == revision + 1
        assert state.itinerary.model_dump_json() == committed.model_dump_json()
        with store.SessionLocal() as session:
            row = session.query(TripRecord).filter_by(trip_id=state.trip_id).one()
            raw = json.loads(row.itinerary_json)
            assert type(raw["planning_version"]) is type(raw["state_schema_version"]) is int
            assert raw["planning_version"] == row.planning_version == revision + 1
            assert raw["state_schema_version"] == row.state_schema_version == 1


@pytest.mark.parametrize("field,value", [("trip_id", "wrong"), ("planning_version", 99),
    ("state_schema_version", 2), ("state_schema_version", 1.0),
    ("state_schema_version", True), ("planning_version", True)])
def test_model_rejects_outer_metadata_conflicts(database, field, value):
    state = create()
    data = dict(trip_id=state.trip_id, planning_version=1, state_schema_version=1,
                baseline_trusted=True, itinerary=state.itinerary)
    data[field] = value
    with pytest.raises(ValidationError): AuthoritativePlanState(**data)


@pytest.mark.parametrize("field,value", [("trip_id", "wrong"), ("planning_version", 99),
    ("state_schema_version", 2), ("state_schema_version", 1.0),
    ("state_schema_version", True), ("planning_version", True)])
def test_model_rejects_inner_mutated_metadata(database, field, value):
    state = create()
    candidate = state.itinerary.model_copy(update={field: value})
    with pytest.raises(ValidationError):
        AuthoritativePlanState(trip_id=state.trip_id, planning_version=1, itinerary=candidate)


def test_eligibility_is_derived_not_client_assignable(database):
    state = create()
    untrusted = AuthoritativePlanState(trip_id=state.trip_id, planning_version=1,
                                      itinerary=state.itinerary, baseline_trusted=False)
    assert not untrusted.replan_eligible
    with pytest.raises(ValidationError):
        AuthoritativePlanState(trip_id=state.trip_id, planning_version=1,
            itinerary=state.itinerary, baseline_trusted=False, replan_eligible=True)
    missing = state.itinerary.model_copy(update={"original_request": None})
    assert not AuthoritativePlanState(trip_id=state.trip_id, planning_version=1,
                                      itinerary=missing, baseline_trusted=True).replan_eligible


@pytest.mark.parametrize("mutation,expected", [
    ("list", "MANAGED_STATE_CORRUPT"), ("null", "MANAGED_STATE_CORRUPT"),
    ("broken_json", "MANAGED_STATE_CORRUPT"), ("missing_summary", "MANAGED_STATE_CORRUPT"),
    ("wrong_days", "MANAGED_STATE_CORRUPT"), ("wrong_spot_type", "MANAGED_STATE_CORRUPT"),
    ("wrong_cost", "MANAGED_STATE_CORRUPT"), ("duplicate", "DUPLICATE_ACTIVITY_ID"),
    ("schema_missing", "STATE_VERSION_MISMATCH"), ("schema_float", "STATE_VERSION_MISMATCH"),
    ("schema_bool", "STATE_VERSION_MISMATCH"), ("version_float", "STATE_VERSION_MISMATCH"),
    ("version_bool", "STATE_VERSION_MISMATCH"), ("version_missing", "STATE_VERSION_MISMATCH"),
])
def test_corruption_stable_errors_no_repair(database, mutation, expected, monkeypatch):
    state = create()
    raw = state.itinerary.model_dump(mode="json")
    if mutation == "missing_summary": raw.pop("summary")
    if mutation == "wrong_days": raw["days"] = {}
    if mutation == "wrong_spot_type": raw["days"][0]["spots"][0] = "bad"
    if mutation == "wrong_cost": raw["days"][0]["spots"][0]["estimated_cost"] = "0"
    if mutation == "duplicate": raw["days"][0]["spots"][1]["activity_id"] = "activity-a"
    if mutation == "schema_missing": raw.pop("state_schema_version")
    if mutation == "schema_float": raw["state_schema_version"] = 1.0
    if mutation == "schema_bool": raw["state_schema_version"] = True
    if mutation == "version_float": raw["planning_version"] = 1.0
    if mutation == "version_bool": raw["planning_version"] = True
    if mutation == "version_missing": raw.pop("planning_version")
    payload = {"list": "[]", "null": "null", "broken_json": "{"}.get(mutation, json.dumps(raw))
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=state.trip_id).one()
        row.itinerary_json = payload
        session.commit()
    before = row_snapshot(state.trip_id)
    with pytest.raises(StateError) as error:
        service.load_authoritative_plan(state.trip_id, id_factory=lambda: pytest.fail("repair"))
    assert error.value.code == expected
    assert row_snapshot(state.trip_id) == before


def test_actual_final_payload_validated_before_update(database, monkeypatch):
    state = create()
    before = row_snapshot(state.trip_id)
    original = service._serialize_managed
    calls = []
    def corrupt_final(plan):
        raw = json.loads(original(plan))
        calls.append(plan.planning_version)
        if plan.planning_version == 2: raw["state_schema_version"] = 1.0
        return json.dumps(raw)
    monkeypatch.setattr(service, "_serialize_managed", corrupt_final)
    with pytest.raises(StateError): service.compare_and_swap_plan(state.trip_id, 1, state.itinerary)
    assert calls == [1, 2]
    assert row_snapshot(state.trip_id) == before


@pytest.mark.parametrize("mutation", ["days", "summary", "spot"])
def test_model_copy_bad_structure_is_state_error(database, mutation):
    state = create()
    candidate = state.itinerary.model_copy(deep=True)
    if mutation == "days": candidate.days = ["bad"]
    if mutation == "summary": candidate.summary = {"wrong": "type"}
    if mutation == "spot": candidate.days[0].spots[0].estimated_cost = "zero"
    before = row_snapshot(state.trip_id)
    with pytest.raises(StateError) as error: service.compare_and_swap_plan(state.trip_id, 1, candidate)
    assert error.value.code == "MANAGED_STATE_CORRUPT"
    assert row_snapshot(state.trip_id) == before
