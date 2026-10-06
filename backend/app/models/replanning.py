"""State foundation only: no patch or replanning models."""
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
