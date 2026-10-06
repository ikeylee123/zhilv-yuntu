import json
from sqlalchemy import inspect, update
from app.models.replanning import StateError

from app.services.planning_fact_service import attach_server_validation
from app.config import SessionLocal, engine
from app.models.db_models import TripRecord
from app.models.schemas import (
    Itinerary,
    TokenStatsResponse,
    TokenUsage,
    TripDetailResponse,
    TripListResponse,
    TripSummaryItem,
    TripTokenStatsItem,
)


def init_db() -> None:
    """初始化数据库表结构。"""
    from app.config import Base

    # SQLite serializes schema upgrades across processes, including first startup.
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(bind=connection)
        columns = {column["name"] for column in inspect(connection).get_columns("trip_records")}
        additions = {"planning_version": "INTEGER", "state_schema_version": "INTEGER",
                     "baseline_trusted": "BOOLEAN NOT NULL DEFAULT 0",
                     "archived": "BOOLEAN NOT NULL DEFAULT 1"}
        for name, definition in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE trip_records ADD COLUMN {name} {definition}")


def save_itinerary(itinerary: Itinerary) -> str:
    """保存或更新完整 itinerary，并返回 trip_id。"""
    from app.services.authoritative_state import is_managed_plan
    if is_managed_plan(itinerary.trip_id) or itinerary.state_schema_version is not None:
        raise StateError("LEGACY_WRITE_FORBIDDEN")
    itinerary = attach_server_validation(itinerary.model_copy(deep=True))
    init_db()

    session = SessionLocal()
    try:
        itinerary_json = json.dumps(
            itinerary.model_dump(mode="json"),
            ensure_ascii=False,
        )

        existing_record = (
            session.query(TripRecord)
            .filter(TripRecord.trip_id == itinerary.trip_id)
            .first()
        )

        if existing_record is None:
            record = TripRecord(
                trip_id=itinerary.trip_id,
                destination=itinerary.destination,
                summary=itinerary.summary,
                itinerary_json=itinerary_json,
            )
            session.add(record)
        else:
            if existing_record.state_schema_version is not None or existing_record.planning_version is not None:
                raise StateError("LEGACY_WRITE_FORBIDDEN")
            result = session.execute(update(TripRecord).where(
                TripRecord.id == existing_record.id,
                TripRecord.state_schema_version.is_(None), TripRecord.planning_version.is_(None),
                TripRecord.itinerary_json == existing_record.itinerary_json,
            ).values(destination=itinerary.destination, summary=itinerary.summary,
                     itinerary_json=itinerary_json), execution_options={"synchronize_session": False})
            if result.rowcount != 1:
                raise StateError("LEGACY_WRITE_FORBIDDEN")

        session.commit()
        return itinerary.trip_id
    finally:
        session.close()


def get_itinerary_by_trip_id(trip_id: str) -> TripDetailResponse | None:
    """根据 trip_id 读取已保存 itinerary，找不到时返回 None。"""
    init_db()

    session = SessionLocal()
    try:
        record = session.query(TripRecord).filter(TripRecord.trip_id == trip_id).first()
        if record is None:
            return None

        if record.state_schema_version is not None or record.planning_version is not None:
            from app.services.authoritative_state import _decode
            itinerary = _decode(record).itinerary
        else:
            itinerary = Itinerary.model_validate_json(record.itinerary_json)

        return TripDetailResponse(
            trip_id=record.trip_id,
            itinerary=itinerary,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
    finally:
        session.close()


def list_saved_itineraries() -> TripListResponse:
    """返回已保存行程的摘要列表。"""
    init_db()

    session = SessionLocal()
    try:
        records = (
            session.query(TripRecord)
            .filter(TripRecord.archived.is_(True))
            .order_by(TripRecord.updated_at.desc(), TripRecord.id.desc())
            .all()
        )

        items = [
            TripSummaryItem(
                trip_id=record.trip_id,
                destination=record.destination,
                summary=record.summary,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in records
        ]
        return TripListResponse(total=len(items), items=items)
    finally:
        session.close()


def get_token_stats() -> TokenStatsResponse:
    """统计所有已保存行程的 token 消耗。"""
    init_db()

    session = SessionLocal()
    try:
        records = (
            session.query(TripRecord)
            .filter(TripRecord.archived.is_(True))
            .order_by(TripRecord.updated_at.desc(), TripRecord.id.desc())
            .all()
        )

        items: list[TripTokenStatsItem] = []
        total_prompt = 0
        total_completion = 0

        for record in records:
            itinerary_data = json.loads(record.itinerary_json)
            itinerary = Itinerary(**itinerary_data)
            usage = itinerary.token_usage
            if usage is None:
                usage = TokenUsage()

            items.append(
                TripTokenStatsItem(
                    trip_id=record.trip_id,
                    destination=record.destination,
                    token_usage=usage,
                )
            )
            total_prompt += usage.total_prompt_tokens
            total_completion += usage.total_completion_tokens

        return TokenStatsResponse(
            trip_count=len(items),
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_tokens=total_prompt + total_completion,
            items=items,
        )
    finally:
        session.close()


def delete_itinerary_by_trip_id(trip_id: str) -> bool:
    """根据 trip_id 删除已保存行程，删除成功返回 True。"""
    init_db()

    session = SessionLocal()
    try:
        record = session.query(TripRecord).filter(TripRecord.trip_id == trip_id).first()
        if record is None:
            return False

        session.delete(record)
        session.commit()
        return True
    finally:
        session.close()
