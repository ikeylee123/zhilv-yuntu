"""Pure identity checks and internal server-owned state persistence.

No HTTP, LLM or MCP operations. Storage functions are not public mutation endpoints.
"""
import json
from uuid import uuid4
from sqlalchemy import update
from app.models.db_models import TripRecord
from app.models.replanning import AuthoritativePlanState, STATE_SCHEMA_VERSION, StateError, require_managed_versions
from app.models.schemas import Itinerary, PlanningState
from app.services.plan_validator import validate_plan


def new_activity_id():
    return "activity_" + uuid4().hex


def materialize_activity_ids(plan, *, id_factory=new_activity_id, fill_missing=True):
    seen = set()
    for day in plan.days:
        for spot in day.spots:
            value = spot.activity_id
            if value is None and fill_missing:
                value = id_factory()
            if not isinstance(value, str) or not value.strip():
                raise StateError("INVALID_ACTIVITY_ID")
            value = value.strip()
            if value in seen:
                raise StateError("DUPLICATE_ACTIVITY_ID")
            seen.add(value)
            spot.activity_id = value
    return plan


def _storage():
    # Runtime lookup allows the existing isolated database fixtures to work.
    from app.services import storage_service
    storage_service.init_db()
    return storage_service


def _check_payload_types(raw, canonical):
    """Reject JSON type coercion without changing frozen models' JSON tuple handling."""
    if type(raw) in (int, float) and type(canonical) in (int, float):
        return
    if type(raw) is not type(canonical):
        raise StateError("MANAGED_STATE_CORRUPT")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in canonical:
                raise StateError("MANAGED_STATE_CORRUPT")
            _check_payload_types(value, canonical[key])
    elif isinstance(raw, list):
        if len(raw) != len(canonical):
            raise StateError("MANAGED_STATE_CORRUPT")
        for value, parsed in zip(raw, canonical):
            _check_payload_types(value, parsed)


def validate_managed_itinerary_payload(payload, *, trip_id, planning_version,
                                       state_schema_version, baseline_trusted, archived):
    """One strict boundary for the exact JSON written and subsequently read.

    Managed corruption never enters legacy migration and is never repaired here.
    """
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise StateError("MANAGED_STATE_CORRUPT")
        require_managed_versions(planning_version, state_schema_version)
        require_managed_versions(raw.get("planning_version"), raw.get("state_schema_version"))
        if raw["planning_version"] != planning_version or raw["state_schema_version"] != state_schema_version:
            raise StateError("STATE_VERSION_MISMATCH")
        if raw.get("trip_id") != trip_id:
            raise StateError("STATE_ID_MISMATCH")
        # Existing nested validators materialize JSON arrays before tuple parsing;
        # preserve their format, then independently reject JSON type coercions.
        plan = Itinerary.model_validate_json(payload)
        _check_payload_types(raw, json.loads(plan.model_dump_json()))
        materialize_activity_ids(plan, fill_missing=False)
        if any(a.get("activity_id") != b.activity_id
               for raw_day, day in zip(raw["days"], plan.days)
               for a, b in zip(raw_day["spots"], day.spots)):
            raise StateError("INVALID_ACTIVITY_ID")
        return AuthoritativePlanState(trip_id=trip_id, itinerary=plan,
            planning_version=planning_version, state_schema_version=state_schema_version,
            baseline_trusted=baseline_trusted, archived=archived)
    except StateError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise StateError("MANAGED_STATE_CORRUPT") from exc


def _serialize_managed(plan):
    try:
        return plan.model_dump_json(warnings="error")
    except (ValueError, TypeError, AttributeError) as exc:
        raise StateError("MANAGED_STATE_CORRUPT") from exc


def _decode(record):
    return validate_managed_itinerary_payload(record.itinerary_json,
        trip_id=record.trip_id, planning_version=record.planning_version,
        state_schema_version=record.state_schema_version,
        baseline_trusted=record.baseline_trusted, archived=record.archived)


def load_authoritative_plan(trip_id, *, id_factory=new_activity_id):
    store = _storage()
    with store.SessionLocal() as session:
        record = session.query(TripRecord).filter_by(trip_id=trip_id).first()
        if record is None:
            raise StateError("STATE_NOT_FOUND")
        if record.state_schema_version is not None:
            return _decode(record)
        if record.planning_version is not None:
            raise StateError("STATE_VERSION_MISMATCH")
        original_json = record.itinerary_json
        plan = Itinerary.model_validate_json(original_json)
        if plan.state_schema_version is not None:
            raise StateError("STATE_VERSION_MISMATCH")
        # Historical versions were client-influenced counters, not committed revisions.
        plan.trip_id = record.trip_id
        plan.planning_version = 1
        plan.state_schema_version = STATE_SCHEMA_VERSION
        materialize_activity_ids(plan, id_factory=id_factory)
        # Historical evidence/constraints have no established server provenance.
        plan.planning_facts = None
        plan.validation_report = None
        payload = _serialize_managed(plan)
        validate_managed_itinerary_payload(payload, trip_id=record.trip_id, planning_version=1,
            state_schema_version=STATE_SCHEMA_VERSION, baseline_trusted=False, archived=record.archived)
        result = session.execute(update(TripRecord).where(
            TripRecord.id == record.id, TripRecord.state_schema_version.is_(None),
            TripRecord.planning_version.is_(None), TripRecord.itinerary_json == original_json,
        ).values(itinerary_json=payload, planning_version=1,
                 state_schema_version=STATE_SCHEMA_VERSION, baseline_trusted=False),
                 execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            session.rollback()
            raise StateError("VERSION_CONFLICT")
        session.commit()
    # Return the stored result; a second load cannot assign IDs again.
    return load_authoritative_plan(trip_id, id_factory=id_factory)


migrate_authoritative_plan_if_needed = load_authoritative_plan


def create_authoritative_draft(itinerary, *, id_factory=new_activity_id, trip_id_factory=None):
    """Internal generation boundary only. Never pass an HTTP itinerary here."""
    plan = itinerary.model_copy(deep=True)
    plan.trip_id = (trip_id_factory or (lambda: "trip_" + uuid4().hex))()
    # New generation never adopts externally supplied identity assignments.
    for day in plan.days:
        for spot in day.spots:
            spot.activity_id = None
    materialize_activity_ids(plan, id_factory=id_factory)
    plan.planning_version = 1
    plan.state_schema_version = STATE_SCHEMA_VERSION
    payload = _serialize_managed(plan)
    validate_managed_itinerary_payload(payload, trip_id=plan.trip_id, planning_version=1,
        state_schema_version=STATE_SCHEMA_VERSION, baseline_trusted=True, archived=False)
    store = _storage()
    with store.SessionLocal() as session:
        session.add(TripRecord(trip_id=plan.trip_id, destination=plan.destination,
            summary=plan.summary, itinerary_json=payload, planning_version=1,
            state_schema_version=STATE_SCHEMA_VERSION, baseline_trusted=True, archived=False))
        session.commit()
    return load_authoritative_plan(plan.trip_id)


def is_managed_plan(trip_id):
    store = _storage()
    with store.SessionLocal() as session:
        row = session.query(TripRecord).filter_by(trip_id=trip_id).first()
        return row is not None and (row.state_schema_version is not None
                                   or row.planning_version is not None)


def archive_authoritative_plan(trip_id):
    load_authoritative_plan(trip_id)
    store = _storage()
    with store.SessionLocal() as session:
        result = session.execute(update(TripRecord).where(
            TripRecord.trip_id == trip_id, TripRecord.state_schema_version == STATE_SCHEMA_VERSION,
        ).values(archived=True))
        if result.rowcount != 1:
            raise StateError("STATE_NOT_FOUND")
        session.commit()
    return trip_id


def compare_and_swap_plan(trip_id, base_version, candidate, *, evaluation_time=None):
    """Internal storage primitive; candidate is server-produced, never an HTTP body.

    Pure full validation runs before a short conditional update. This does not implement
    any edit/patch operation or collect facts; future orchestration owns those steps.
    """
    if type(base_version) is not int or base_version < 1:
        raise StateError("VERSION_CONFLICT")
    base = load_authoritative_plan(trip_id)
    if base.planning_version != base_version:
        raise StateError("VERSION_CONFLICT")
    base.require_replan_eligible()
    if not isinstance(candidate, Itinerary):
        raise StateError("MANAGED_STATE_CORRUPT")
    plan = candidate.model_copy(deep=True)
    require_managed_versions(plan.planning_version, plan.state_schema_version)
    if plan.trip_id != trip_id:
        raise StateError("STATE_ID_MISMATCH")
    if type(plan.planning_version) is not int or plan.planning_version != base_version:
        raise StateError("STATE_VERSION_MISMATCH")
    plan = validate_managed_itinerary_payload(_serialize_managed(plan), trip_id=trip_id,
        planning_version=base_version, state_schema_version=STATE_SCHEMA_VERSION,
        baseline_trusted=base.baseline_trusted, archived=base.archived).itinerary
    if (plan.original_request is None or plan.constraints is None
            or plan.original_request.model_dump() != base.itinerary.original_request.model_dump()
            or plan.constraints.model_dump() != base.itinerary.constraints.model_dump()
            or plan.destination != base.itinerary.destination):
        raise StateError("BASELINE_CHANGE_FORBIDDEN")
    report = validate_plan(PlanningState(trip_request=base.itinerary.original_request,
        constraints=base.itinerary.constraints, current_itinerary=plan,
        planning_facts=plan.planning_facts, evaluation_time=evaluation_time, version=base_version))
    if report.status != "valid":
        raise StateError("VALIDATION_REJECTED" if report.status == "invalid" else "BASELINE_UNVERIFIABLE")
    plan.validation_report = report
    plan.trip_id = trip_id
    plan.planning_version = base_version + 1
    plan.state_schema_version = STATE_SCHEMA_VERSION
    payload = _serialize_managed(plan)
    committed = validate_managed_itinerary_payload(payload, trip_id=trip_id,
        planning_version=base_version + 1, state_schema_version=STATE_SCHEMA_VERSION,
        baseline_trusted=base.baseline_trusted, archived=base.archived)
    store = _storage()
    with store.SessionLocal() as session:
        result = session.execute(update(TripRecord).where(
            TripRecord.trip_id == trip_id, TripRecord.planning_version == base_version,
            TripRecord.state_schema_version == STATE_SCHEMA_VERSION,
        ).values(itinerary_json=payload, planning_version=base_version + 1,
                 destination=plan.destination, summary=plan.summary))
        if result.rowcount != 1:
            session.rollback()
            raise StateError("VERSION_CONFLICT")
        session.commit()
    return committed.itinerary
