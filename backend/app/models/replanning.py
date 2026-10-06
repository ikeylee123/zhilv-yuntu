"""Authoritative state and single-operation deterministic replanning contracts."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator
from app.models.schemas import Itinerary

STATE_SCHEMA_VERSION = 1


class StateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require_managed_versions(planning_version, state_schema_version):
    if (type(planning_version) is not int or planning_version < 1
            or type(state_schema_version) is not int
            or state_schema_version != STATE_SCHEMA_VERSION):
        raise StateError("STATE_VERSION_MISMATCH")


class ManagedStateMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    planning_version: int = Field(strict=True, ge=1)
    state_schema_version: Literal[1] = 1
    @field_validator("state_schema_version", mode="before")
    @classmethod
    def strict_schema(cls, value):
        require_managed_versions(1, value)
        return value

    baseline_trusted: bool = Field(default=False, strict=True)
    archived: bool = Field(default=False, strict=True)


class AuthoritativePlanState(ManagedStateMetadata):
    trip_id: str
    itinerary: Itinerary

    @model_validator(mode="after")
    def consistent_metadata(self):
        require_managed_versions(self.itinerary.planning_version, self.itinerary.state_schema_version)
        if self.trip_id != self.itinerary.trip_id:
            raise StateError("STATE_ID_MISMATCH")
        if (self.planning_version != self.itinerary.planning_version
                or self.state_schema_version != self.itinerary.state_schema_version):
            raise StateError("STATE_VERSION_MISMATCH")
        return self

    @computed_field
    @property
    def replan_eligible(self) -> bool:
        return (self.baseline_trusted and self.itinerary.original_request is not None
                and self.itinerary.constraints is not None)

    def require_replan_eligible(self) -> None:
        if not self.replan_eligible:
            raise StateError("BASELINE_UNVERIFIABLE")


# Phase 3B: public requests reference server data; patches remain internal.
from enum import StrEnum
from typing import Annotated
from pydantic import AwareDatetime, StringConstraints
from app.models.planning_facts import FrozenModel
from app.models.tool_contracts import POIData
from app.models.schemas import ValidationReport, SpotItem

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
ClockTime = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
REPLAN_POLICY = "single-attraction-v1"


class ChangeType(StrEnum):
    REPLACE = "replace"
    REMOVE = "remove"
    ADD = "add"
    MODIFY_TIME = "modify_time"


class PlanChangeRequest(FrozenModel):
    trip_id: Identifier
    base_plan_version: int = Field(strict=True, ge=1)
    change_type: ChangeType
    target_day_index: int = Field(strict=True, ge=1)
    target_activity_id: Identifier | None = None
    candidate_set_id: Identifier | None = None
    candidate_id: Identifier | None = None
    before_activity_id: Identifier | None = None
    append: bool = Field(default=False, strict=True)
    start_time: ClockTime | None = None
    end_time: ClockTime | None = None

    @model_validator(mode="after")
    def operation_fields(self):
        selected = self.change_type in (ChangeType.REPLACE, ChangeType.ADD)
        if selected != (self.candidate_id is not None and self.candidate_set_id is not None):
            raise ValueError("Candidate set and candidate required only for replace/add")
        if not selected and (self.candidate_id is not None or self.candidate_set_id is not None):
            raise ValueError("Unexpected candidate")
        if self.change_type == ChangeType.ADD:
            if self.target_activity_id is not None or (self.before_activity_id is not None) == self.append:
                raise ValueError("Add needs exactly one insertion anchor or append")
        elif self.target_activity_id is None or self.before_activity_id is not None or self.append:
            raise ValueError("Operation requires one target, no insertion anchor")
        timed = self.change_type in (ChangeType.ADD, ChangeType.MODIFY_TIME)
        if timed:
            if self.start_time is None or self.end_time is None or self.start_time >= self.end_time:
                raise ValueError("Explicit same-day increasing time slot required")
        elif self.start_time is not None or self.end_time is not None:
            raise ValueError("Time override not authorized")
        return self


class CandidatePreparationRequest(FrozenModel):
    trip_id: Identifier
    base_plan_version: int = Field(strict=True, ge=1)
    target_day_index: int = Field(strict=True, ge=1)
    change_type: Literal["replace", "add"]
    target_activity_id: Identifier | None = None
    preference: Literal["attraction", "museum", "park"] = "attraction"

    @model_validator(mode="after")
    def target_fields(self):
        if (self.change_type == "replace") != (self.target_activity_id is not None):
            raise ValueError("Only replacement requires a target")
        return self


class GroundedCandidate(FrozenModel):
    poi: POIData
    estimated_cost: float = Field(ge=0, allow_inf_nan=False)
    cost_policy: Literal["existing-ticket-estimate-v1"] = "existing-ticket-estimate-v1"


class AllowedCandidateSet(FrozenModel):
    candidate_set_id: str
    trip_id: str
    base_plan_version: int
    target_day: int
    target_activity_id: str | None = None
    operation: Literal["replace", "add"]
    candidates: tuple[GroundedCandidate, ...]
    created_at: AwareDatetime
    fetched_at: AwareDatetime
    policy_version: Literal["single-attraction-v1"] = REPLAN_POLICY


class ReplanningScope(FrozenModel):
    trip_id: str
    base_plan_version: int
    affected_day: int
    affected_activity_ids: tuple[str, ...]
    preserved_activity_ids: tuple[str, ...]
    allowed_operation: ChangeType
    allowed_fields: tuple[str, ...]
    old_required_route_queries: tuple[str, ...] = ()
    new_required_route_queries: tuple[str, ...] = ()
    facts_to_invalidate: tuple[str, ...] = ()
    facts_to_recollect: tuple[str, ...] = ()
    reused_queries: tuple[str, ...] = ()
    eligible_reuse_queries: tuple[str, ...] = ()
    recollected_queries: tuple[str, ...] = ()
    unknown_queries: tuple[str, ...] = ()
    constraints_to_recheck: tuple[str, ...] = ("all",)
    scope_reason: str


class ReplacePatch(FrozenModel):
    kind: Literal["replace"] = "replace"
    target_activity_id: str
    activity: SpotItem


class RemovePatch(FrozenModel):
    kind: Literal["remove"] = "remove"
    target_activity_id: str


class AddPatch(FrozenModel):
    kind: Literal["add"] = "add"
    activity: SpotItem
    before_activity_id: str | None = None
    append: bool = False


class ModifyTimePatch(FrozenModel):
    kind: Literal["modify_time"] = "modify_time"
    target_activity_id: str
    start_time: str
    end_time: str


class PlanPatch(FrozenModel):
    base_plan_version: int
    target_day: int
    operation: Annotated[ReplacePatch | RemovePatch | AddPatch | ModifyTimePatch, Field(discriminator="kind")]


class ReplanDecision(FrozenModel):
    decision: Literal["accepted", "rejected", "unverifiable", "conflict"]
    reason: str
    base_plan_version: int
    candidate_plan_version: int | None = None
    validation_report: ValidationReport | None = None
    change_summary: dict = Field(default_factory=dict)
    scope: ReplanningScope | None = None
    itinerary: Itinerary | None = None
