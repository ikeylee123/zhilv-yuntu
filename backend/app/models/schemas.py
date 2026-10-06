from __future__ import annotations

from datetime import date as DateType, datetime
from decimal import Decimal
from typing import Any, Literal

from app.models.planning_facts import PlanningFacts, RouteCoverage

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_serializer, model_validator


class PlaceReference(BaseModel):
    """An explicit ID takes precedence over a name; strings mean exact names."""

    model_config = ConfigDict(extra="forbid")
    entity_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def require_identity(self) -> "PlaceReference":
        if not (self.entity_id and self.entity_id.strip()) and not (self.name and self.name.strip()):
            raise ValueError("A place reference needs a nonempty entity_id or name")
        if self.entity_id is not None and not self.entity_id.strip():
            raise ValueError("entity_id cannot be blank")
        return self


class TripConstraints(BaseModel):
    """Typed fields are hard requirements; soft_preferences are not scored.

    Omitted budget/preferences inherit the original request. Explicit null opts out.
    Unimplemented hard_constraints are reported as unknown, never assumed satisfied.
    """

    model_config = ConfigDict(extra="forbid")
    require_route_feasibility: bool | None = None
    budget_limit: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    must_visit: list[PlaceReference | str] | None = None
    avoid: list[PlaceReference | str] | None = None
    daily_start_time: str | None = None
    daily_end_time: str | None = None
    transport_preference: str | None = None
    mobility_constraints: list[str] | None = None
    hard_constraints: list[str] | None = None
    soft_preferences: list[str] | None = None

    @model_serializer(mode="wrap")
    def serialize_overrides(self, handler):
        # Missing keys stay absent on the wire; explicit null keys stay present.
        # This persists override presence, including when nested in a request/plan.
        values = handler(self)
        return {key: value for key, value in values.items() if key in self.model_fields_set}



class ConstraintViolation(BaseModel):
    type: str
    severity: Literal["error", "warning", "unknown"]
    message: str
    day_index: int | None = None
    activity_ref: str | None = None
    expected: Any = None
    actual: Any = None


class ValidationReport(BaseModel):
    """valid means no hard errors AND no unverifiable hard checks in this scope."""

    violations: list[ConstraintViolation] = Field(default_factory=list)
    unknowns: list[ConstraintViolation] = Field(default_factory=list)
    checked_constraints: list[str] = Field(default_factory=list)
    route_coverage: RouteCoverage = Field(default_factory=RouteCoverage)

    @model_validator(mode="after")
    def check_buckets(self) -> "ValidationReport":
        if any(item.severity == "unknown" for item in self.violations):
            raise ValueError("Unknown findings belong in unknowns")
        if any(item.severity != "unknown" for item in self.unknowns):
            raise ValueError("unknowns can only contain unknown findings")
        return self

    @computed_field
    @property
    def status(self) -> Literal["valid", "invalid", "unverifiable"]:
        if any(item.severity == "error" for item in self.violations):
            return "invalid"
        return "unverifiable" if self.unknowns else "valid"

    @computed_field
    @property
    def valid(self) -> bool:
        return self.status == "valid"

    @computed_field
    @property
    def summary(self) -> str:
        errors = sum(item.severity == "error" for item in self.violations)
        warnings = sum(item.severity == "warning" for item in self.violations)
        return f"{self.status}: {errors} errors, {warnings} warnings, {len(self.unknowns)} unknowns"


class ScheduledActivity(BaseModel):
    """Validator adapter model; raw time strings remain available for error reporting."""

    day_index: int
    activity_ref: str
    activity_type: Literal["attraction", "meal", "hotel", "transport"]
    entity_id: str | None = None
    name: str
    start_time: str | None = None
    end_time: str | None = None
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)


class TripRequest(BaseModel):
    """用于生成新行程的请求体。"""

    constraints: TripConstraints | None = None

    destination: str = Field(..., description="目的地，例如大理")
    start_date: DateType = Field(..., description="出行开始日期")
    end_date: DateType = Field(..., description="出行结束日期")
    travelers: int = Field(..., ge=1, description="出行人数")
    budget: float = Field(..., ge=0, description="总预算")
    preferences: list[str] = Field(default_factory=list, description="旅行偏好标签")
    pace: str | None = Field(default=None, description="旅行节奏，例如轻松、适中、紧凑")
    dietary_preferences: list[str] = Field(
        default_factory=list,
        description="饮食偏好或忌口",
    )
    hotel_level: str | None = Field(default=None, description="酒店档次偏好")
    special_notes: str | None = Field(default=None, description="额外要求")


class TripEditRequest(BaseModel):
    """用于修改已有行程的请求体。"""

    trip_id: str = Field(..., description="需要编辑的行程 ID")
    current_itinerary: "Itinerary" = Field(..., description="当前完整 itinerary")
    user_instruction: str = Field(..., description="用户新的修改要求")
    edit_scope: str | None = Field(default=None, description="编辑范围")
    preserve_constraints: list[str] = Field(
        default_factory=list,
        description="需要尽量保留的条件",
    )


class TripSaveRequest(BaseModel):
    """用于保存当前 itinerary 的请求体。"""

    trip_id: str = Field(..., description="需要保存的行程 ID")
    itinerary: "Itinerary" = Field(..., description="完整行程数据")
    user_id: str | None = Field(default=None, description="用户 ID，当前版本可留空")


class SpotItem(BaseModel):
    # Legacy parsing is permissive; authoritative materialization enforces identity.
    activity_id: str | None = None

    """单个景点安排。"""

    name: str = Field(..., description="景点名称")
    start_time: str | None = Field(default=None, description="开始时间")
    end_time: str | None = Field(default=None, description="结束时间")
    description: str | None = Field(default=None, description="景点安排说明")
    estimated_cost: float = Field(default=0.0, ge=0, description="预估花费")
    location: str | None = Field(default=None, description="景点位置描述")
    image_url: str | None = Field(default=None, description="景点图片地址")
    address: str | None = Field(default=None, description="景点详细地址")
    latitude: float | None = Field(default=None, description="景点纬度")
    longitude: float | None = Field(default=None, description="景点经度")
    poi_id: str | None = Field(default=None, description="地图服务返回的 POI 标识")


class MealItem(BaseModel):
    """单个餐饮安排。"""

    name: str = Field(..., description="餐厅或餐饮建议名称")
    meal_type: str = Field(..., description="早餐、午餐、晚餐等")
    estimated_cost: float = Field(default=0.0, ge=0, description="预估花费")
    notes: str | None = Field(default=None, description="补充说明")
    address: str | None = Field(default=None, description="餐饮地点详细地址")
    latitude: float | None = Field(default=None, description="餐饮地点纬度")
    longitude: float | None = Field(default=None, description="餐饮地点经度")
    poi_id: str | None = Field(default=None, description="地图服务返回的 POI 标识")
    image_url: str | None = Field(default=None, description="餐饮地点图片地址")


class HotelItem(BaseModel):
    """单个住宿安排。"""

    name: str = Field(..., description="酒店名称")
    level: str | None = Field(default=None, description="酒店档次")
    estimated_cost: float = Field(default=0.0, ge=0, description="预估花费")
    location: str | None = Field(default=None, description="酒店位置")
    address: str | None = Field(default=None, description="酒店详细地址")
    latitude: float | None = Field(default=None, description="酒店纬度")
    longitude: float | None = Field(default=None, description="酒店经度")
    poi_id: str | None = Field(default=None, description="地图服务返回的 POI 标识")
    image_url: str | None = Field(default=None, description="酒店图片地址")


class TransportItem(BaseModel):
    """单段交通安排。"""

    mode: str = Field(..., description="交通方式，例如步行、打车、公交")
    from_place: str | None = Field(default=None, description="出发地")
    to_place: str | None = Field(default=None, description="目的地")
    estimated_cost: float = Field(default=0.0, ge=0, description="预估花费")
    duration: str | None = Field(default=None, description="预计耗时")
    distance_km: float | None = Field(default=None, ge=0, description="预计距离，单位公里")
    estimated_minutes: int | None = Field(default=None, ge=0, description="预计耗时，单位分钟")


class BudgetBreakdown(BaseModel):
    """预算拆分。"""

    transport: float = Field(default=0.0, ge=0, description="交通预算")
    hotel: float = Field(default=0.0, ge=0, description="住宿预算")
    meals: float = Field(default=0.0, ge=0, description="餐饮预算")
    tickets: float = Field(default=0.0, ge=0, description="门票预算")
    other: float = Field(default=0.0, ge=0, description="其他预算")
    total: float = Field(default=0.0, ge=0, description="预算总计")

    @model_serializer(mode="wrap")
    def serialize_budget_evidence(self, handler):
        values = handler(self)
        if "total" not in self.model_fields_set:
            values.pop("total", None)
        return values


class DayPlan(BaseModel):
    """单日行程安排。"""

    day_index: int = Field(..., ge=1, description="第几天")
    date: DateType | None = Field(default=None, description="当天日期")
    theme: str | None = Field(default=None, description="当天主题")
    spots: list[SpotItem] = Field(default_factory=list, description="景点安排")
    meals: list[MealItem] = Field(default_factory=list, description="餐饮安排")
    hotel: HotelItem | None = Field(default=None, description="住宿安排")
    transport: list[TransportItem] = Field(default_factory=list, description="交通安排")
    notes: list[str] = Field(default_factory=list, description="补充说明")


class TokenUsage(BaseModel):
    """LLM 调用的 token 消耗统计。"""

    rewrite_prompt_tokens: int = Field(default=0, ge=0, description="Query Rewrite 输入 token")
    rewrite_completion_tokens: int = Field(default=0, ge=0, description="Query Rewrite 输出 token")
    embedding_prompt_tokens: int = Field(default=0, ge=0, description="Query Embedding 输入 token")
    embedding_completion_tokens: int = Field(default=0, ge=0, description="Query Embedding 输出 token")
    planner_prompt_tokens: int = Field(default=0, ge=0, description="行程生成输入 token")
    planner_completion_tokens: int = Field(default=0, ge=0, description="行程生成输出 token")
    rerank_prompt_tokens: int = Field(default=0, ge=0, description="Rerank 输入 token")
    rerank_completion_tokens: int = Field(default=0, ge=0, description="Rerank 输出 token")

    @property
    def total_prompt_tokens(self) -> int:
        return (
            self.rewrite_prompt_tokens
            + self.embedding_prompt_tokens
            + self.planner_prompt_tokens
            + self.rerank_prompt_tokens
        )

    @property
    def total_completion_tokens(self) -> int:
        return (
            self.rewrite_completion_tokens
            + self.embedding_completion_tokens
            + self.planner_completion_tokens
            + self.rerank_completion_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens


class Itinerary(BaseModel):
    """完整行程。"""

    planning_facts: PlanningFacts | None = None
    original_request: TripRequest | None = None
    constraints: TripConstraints | None = None
    validation_report: ValidationReport | None = None
    planning_version: int | None = Field(default=None, ge=1)
    state_schema_version: int | None = Field(default=None, ge=1)

    trip_id: str = Field(..., description="行程唯一标识")
    destination: str = Field(..., description="目的地")
    summary: str = Field(..., description="整趟行程的概述")
    days: list[DayPlan] = Field(default_factory=list, description="逐日行程")
    estimated_budget: float = Field(default=0.0, ge=0, description="预算总计")
    budget_breakdown: BudgetBreakdown = Field(..., description="预算明细")
    tips: list[str] = Field(default_factory=list, description="旅行建议")
    source_notes: list[str] = Field(
        default_factory=list,
        description="RAG 或规则生成产生的补充说明",
    )
    token_usage: TokenUsage | None = Field(default=None, description="LLM token 消耗统计")

    @model_serializer(mode="wrap")
    def serialize_budget_evidence(self, handler):
        values = handler(self)
        if "estimated_budget" not in self.model_fields_set:
            values.pop("estimated_budget", None)
        return values


class TripDetailResponse(BaseModel):
    """查询已保存行程时返回的响应体。"""

    trip_id: str = Field(..., description="行程 ID")
    itinerary: Itinerary = Field(..., description="已保存的完整行程")
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class TripSummaryItem(BaseModel):
    """已保存行程的摘要信息。"""

    trip_id: str = Field(..., description="行程 ID")
    destination: str = Field(..., description="目的地")
    summary: str = Field(..., description="行程概述")
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class TripListResponse(BaseModel):
    """行程列表接口的响应结构。"""

    total: int = Field(..., ge=0, description="列表总数")
    items: list[TripSummaryItem] = Field(default_factory=list, description="行程摘要列表")


class TripTokenStatsItem(BaseModel):
    """单个行程的 token 消耗。"""

    trip_id: str = Field(..., description="行程 ID")
    destination: str = Field(..., description="目的地")
    token_usage: TokenUsage = Field(..., description="token 消耗")


class TokenStatsResponse(BaseModel):
    """Token 消耗统计接口的响应结构。"""

    trip_count: int = Field(..., ge=0, description="统计行程数")
    total_prompt_tokens: int = Field(default=0, ge=0, description="总输入 token")
    total_completion_tokens: int = Field(default=0, ge=0, description="总输出 token")
    total_tokens: int = Field(default=0, ge=0, description="总 token")
    items: list[TripTokenStatsItem] = Field(default_factory=list, description="各行程 token 明细")


class PlanningState(BaseModel):
    """Ephemeral orchestration value; persisted metadata lives on Itinerary JSON."""

    trip_request: TripRequest | None = None
    constraints: TripConstraints = Field(default_factory=TripConstraints)
    current_itinerary: Itinerary
    planning_facts: PlanningFacts | None = None
    evaluation_time: datetime | None = None
    validation_report: ValidationReport | None = None
    version: int = Field(default=1, ge=1)
