from fastapi import APIRouter, HTTPException
from app.models.replanning import AuthoritativePlanState, StateError
from app.services.authoritative_state import (
    create_authoritative_draft, load_authoritative_plan, archive_authoritative_plan,
    is_managed_plan,
)


def _state_call(function, *args):
    try:
        return function(*args)
    except StateError as exc:
        raise HTTPException(status_code=404 if exc.code == "STATE_NOT_FOUND" else 409,
                            detail={"code": exc.code}) from exc


def _legacy_copy(plan):
    """Legacy HTTP data is display input, never trusted baseline/evidence metadata."""
    plan = plan.model_copy(deep=True)
    plan.original_request = None
    plan.constraints = None
    plan.planning_facts = None
    plan.validation_report = None
    plan.planning_version = None
    plan.state_schema_version = None
    for day in plan.days:
        for spot in day.spots:
            spot.activity_id = None
    return plan


from app.models.schemas import (
    Itinerary,
    TokenStatsResponse,
    TripDetailResponse,
    TripEditRequest,
    TripListResponse,
    TripRequest,
    TripSaveRequest,
)
from app.services.plan_validator import ensure_date_range
from app.services.city_registry_service import CityCoverageTier
from app.services.city_resolver_service import (
    CityResolutionUnavailableError,
    resolve_city,
)
from app.services.place_candidate_service import (
    CandidateCollectionUnavailableError,
    PlaceCandidateCategory,
    collect_city_candidate_pool,
)
from app.services.storage_service import (
    delete_itinerary_by_trip_id,
    get_itinerary_by_trip_id,
    get_token_stats,
    list_saved_itineraries,
    save_itinerary,
)
from app.services.trip_service import (
    edit_trip_itinerary,
    generate_dynamic_trip_itinerary,
    generate_trip_itinerary,
)


router = APIRouter(prefix="/trip", tags=["trip"])


@router.get("", response_model=TripListResponse)
def list_trips() -> TripListResponse:
    """返回已保存行程的摘要列表。"""
    return list_saved_itineraries()


@router.post("/generate", response_model=Itinerary)
def generate_trip(request: TripRequest, managed_state: bool = False) -> Itinerary:
    """生成结构化 itinerary。"""
    try:
        ensure_date_range(request)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_date_range", "message": str(exc)},
        ) from exc
    try:
        city_resolution = resolve_city(request.destination)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_destination",
                "message": str(exc),
            },
        ) from exc
    except CityResolutionUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "city_resolution_unavailable",
                "message": str(exc),
                "reason": exc.reason,
            },
        ) from exc

    if city_resolution.tier is CityCoverageTier.INSUFFICIENT_DATA:
        is_province_scope = (
            city_resolution.resolution_reason == "province_requires_city"
        )
        province_example = (
            "，例如输入“西宁”"
            if "青海" in city_resolution.city
            else ""
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": (
                    "unsupported_destination_scope"
                    if is_province_scope
                    else "insufficient_city_data"
                ),
                "message": (
                    (
                        f"“{city_resolution.city}”是省级目的地，当前版本支持单城市规划。"
                        f"请输入省内具体城市{province_example}后重试。"
                    )
                    if is_province_scope
                    else (
                        f"暂时无法确认“{city_resolution.city}”是可规划的旅游目的地，"
                        "请检查城市名称后重试。"
                    )
                ),
                "destination": city_resolution.city,
                "coverage_tier": city_resolution.tier.value,
                "administrative_level": city_resolution.administrative_level,
            },
        )

    if city_resolution.tier is CityCoverageTier.DYNAMIC:
        try:
            candidate_pool = collect_city_candidate_pool(
                city=city_resolution.city,
                adcode=city_resolution.adcode,
                administrative_level=city_resolution.administrative_level,
            )
        except CandidateCollectionUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "candidate_collection_unavailable",
                    "message": str(exc),
                    "destination": city_resolution.city,
                    "reason": exc.reason,
                    "category": exc.category,
                },
            ) from exc

        candidate_counts = {
            category.value: len(candidate_pool.candidates_for(category))
            for category in PlaceCandidateCategory
        }
        minimum_counts = {
            category.value: minimum
            for category, minimum in candidate_pool.minimum_counts.items()
        }
        if not candidate_pool.meets_minimum:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "insufficient_candidate_data",
                    "message": (
                        f"“{city_resolution.city}”的可验证地点候选不足，"
                        "暂时无法生成可靠行程。"
                    ),
                    "destination": city_resolution.city,
                    "coverage_tier": city_resolution.tier.value,
                    "candidate_counts": candidate_counts,
                    "minimum_counts": minimum_counts,
                    "shortages": {
                        category.value: shortage
                        for category, shortage in candidate_pool.shortages.items()
                    },
                },
            )

        normalized_request = request.model_copy(
            update={"destination": city_resolution.city},
        )
        itinerary = generate_dynamic_trip_itinerary(
            normalized_request,
            candidate_pool,
        )
        itinerary = Itinerary.model_validate(itinerary)
        itinerary.original_request = request.model_copy(deep=True)
        return create_authoritative_draft(itinerary).itinerary if managed_state else itinerary

    normalized_request = request.model_copy(
        update={"destination": city_resolution.city},
    )
    itinerary = Itinerary.model_validate(generate_trip_itinerary(normalized_request))
    itinerary.original_request = request.model_copy(deep=True)
    return create_authoritative_draft(itinerary).itinerary if managed_state else itinerary


@router.get("/stats", response_model=TokenStatsResponse)
def get_trip_token_stats() -> TokenStatsResponse:
    """返回所有已保存行程的 token 消耗统计。"""
    return get_token_stats()


@router.post("/edit", response_model=Itinerary)
def edit_trip(request: TripEditRequest) -> Itinerary:
    """根据用户编辑指令返回更新后的 itinerary。"""
    if request.trip_id != request.current_itinerary.trip_id:
        raise HTTPException(status_code=409, detail={"code": "STATE_ID_MISMATCH"})
    if is_managed_plan(request.trip_id) or request.current_itinerary.state_schema_version is not None:
        raise HTTPException(status_code=409, detail={"code": "LEGACY_WRITE_FORBIDDEN"})
    clean_request = request.model_copy(update={"current_itinerary": _legacy_copy(request.current_itinerary)})
    return edit_trip_itinerary(clean_request)


@router.post("/save")
def save_trip(request: TripSaveRequest) -> dict[str, str]:
    """保存 itinerary，并返回 trip_id。"""
    if request.trip_id != request.itinerary.trip_id:
        raise HTTPException(status_code=409, detail={"code": "STATE_ID_MISMATCH"})
    if is_managed_plan(request.trip_id):
        # Only archive metadata changes; the supplied JSON is never used as plan state.
        saved_trip_id = _state_call(archive_authoritative_plan, request.trip_id)
    elif request.itinerary.state_schema_version is not None:
        raise HTTPException(status_code=409, detail={"code": "MANAGED_STATE_REQUIRED"})
    else:
        saved_trip_id = _state_call(save_itinerary, _legacy_copy(request.itinerary))
    return {
        "message": "Trip itinerary saved successfully.",
        "trip_id": saved_trip_id,
    }


@router.get("/{trip_id}/state", response_model=AuthoritativePlanState)
def get_authoritative_state(trip_id: str):
    return _state_call(load_authoritative_plan, trip_id)


@router.get("/{trip_id}", response_model=TripDetailResponse)
def get_trip_detail(trip_id: str) -> TripDetailResponse:
    """根据 trip_id 查询已保存 itinerary。"""
    trip_detail = _state_call(get_itinerary_by_trip_id, trip_id)
    if trip_detail is None:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return trip_detail


@router.delete("/{trip_id}")
def delete_trip(trip_id: str) -> dict[str, str]:
    """根据 trip_id 删除已保存 itinerary。"""
    deleted = delete_itinerary_by_trip_id(trip_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return {
        "message": "Trip itinerary deleted successfully.",
        "trip_id": trip_id,
    }


# Structured Phase 3B endpoints; managed legacy /edit remains closed.
from fastapi import Depends
from app.models.replanning import PlanChangeRequest, CandidatePreparationRequest, ReplanDecision, AllowedCandidateSet
from app.services.replanning_service import ReplanningService
from app.models.candidate_selection import ReplanSelectionRequest, SelectionDecision
from app.services.replanning_selection import BoundedSelectionService


def get_replanning_service():
    return ReplanningService()


@router.post("/{trip_id}/replan/candidates", response_model=AllowedCandidateSet)
def prepare_replan_candidates(trip_id: str, request: CandidatePreparationRequest,
                              service: ReplanningService = Depends(get_replanning_service)):
    if trip_id != request.trip_id:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CHANGE_REQUEST"})
    return _state_call(service.prepare, request)


@router.post("/{trip_id}/replan", response_model=SelectionDecision)
def replan_trip(trip_id: str, request: ReplanSelectionRequest,
                service: ReplanningService = Depends(get_replanning_service)):
    if trip_id != request.trip_id:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CHANGE_REQUEST"})
    return BoundedSelectionService(service).replan(request)
