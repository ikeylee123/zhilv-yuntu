"""Small immutable route evidence contracts; no tools, config or persistence imports."""
from __future__ import annotations

from datetime import date as DateType
from enum import StrEnum
from typing import Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

SCHEMA_VERSION = "route-facts-v1"
POLICY_VERSION = "driving-estimate-v1"
ROUTE_FACT_MAX_AGE_SECONDS = 15 * 60
ROUTE_BUFFER_SECONDS = 0


def reject_bool_route_number(value):
    """Reject bool before float coercion; preserve legitimate numeric strings."""
    if isinstance(value, bool):
        raise ValueError("Boolean is not numeric route evidence")
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FactStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"
    CONFLICTING = "conflicting"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"


class FactSource(FrozenModel):
    provider: Literal["amap"] = "amap"
    endpoint: Literal["/direction/driving"] = "/direction/driving"
    api_version: Literal["v3"] = "v3"
    strategy: Literal[0] = 0
    fetched_at: AwareDatetime | None = None
    evidence_kind: Literal["provider_estimate"] = "provider_estimate"


class Coordinates(FrozenModel):
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    system: Literal["GCJ-02"] = "GCJ-02"


class ActivityReference(FrozenModel):
    activity_ref: str
    day_index: int
    date: DateType | None = None
    activity_type: Literal["attraction"] = "attraction"
    entity_id: str | None = None
    name: str
    start_time: str | None = None
    end_time: str | None = None
    coordinates: Coordinates | None = None


class ActivityPair(FrozenModel):
    origin: ActivityReference
    destination: ActivityReference


class RouteFact(FrozenModel):
    fact_id: str
    status: FactStatus
    origin: ActivityReference
    destination: ActivityReference
    mode: Literal["driving"] = "driving"
    distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source: FactSource | None = None
    reason: str | None = None

    @field_validator("distance_meters", "duration_seconds", mode="before")
    @classmethod
    def reject_boolean_estimates(cls, value):
        return reject_bool_route_number(value)

    @model_validator(mode="after")
    def known_requires_evidence(self):
        if self.status == FactStatus.KNOWN:
            if (self.duration_seconds is None or self.distance_meters is None
                    or self.source is None or self.source.fetched_at is None
                    or not self.origin.entity_id or not self.destination.entity_id
                    or self.origin.coordinates is None or self.destination.coordinates is None):
                raise ValueError("Known routes require bound endpoints, numeric estimates and timestamped source")
            for endpoint in (self.origin, self.destination):
                if (endpoint.day_index < 0 or not endpoint.entity_id.strip()
                        or not endpoint.activity_ref.strip() or not endpoint.name.strip()):
                    raise ValueError("Known endpoints require nonblank identity, reference, name and nonnegative day index")
            if self.reason is not None:
                raise ValueError("Known route cannot carry a failure reason")
        return self


class FactMetadata(FrozenModel):
    run_id: str
    plan_fingerprint: str
    collected_at: AwareDatetime
    schema_version: str = SCHEMA_VERSION
    policy_version: str = POLICY_VERSION


class CollectionOutcome(FrozenModel):
    pair: ActivityPair
    status: OutcomeStatus
    reason: str | None = None
    query_id: str | None = None
    cache_hit: bool = False
    deduplicated: bool = False


class ToolCallMetrics(FrozenModel):
    logical_route_queries: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    actual_provider_calls: int = Field(default=0, ge=0)
    deduplicated_calls: int = Field(default=0, ge=0)
    unnecessary_calls: int = Field(default=0, ge=0)
    identity_provider_calls: int = Field(default=0, ge=0)


class PlanningFacts(FrozenModel):
    metadata: FactMetadata
    route_facts: tuple[RouteFact, ...] = ()
    collection_outcomes: tuple[CollectionOutcome, ...] = ()
    metrics: ToolCallMetrics = Field(default_factory=ToolCallMetrics)


class RouteCoverage(FrozenModel):
    requested: bool = False
    scope: Literal["same_day_adjacent_timed_attractions"] = "same_day_adjacent_timed_attractions"
    required_pairs: int = Field(default=0, ge=0)
    verified_pairs: int = Field(default=0, ge=0)
    unknown_pairs: int = Field(default=0, ge=0)
    skipped_pairs: int = Field(default=0, ge=0)
    reasons: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def discard_derived_fraction(cls, values):
        if isinstance(values, dict):
            return {key: value for key, value in values.items() if key != "verified_fraction"}
        return values

    @model_validator(mode="after")
    def counts_fit_required_pairs(self):
        if self.verified_pairs + self.unknown_pairs > self.required_pairs:
            raise ValueError("Verified and unknown pairs cannot exceed required pairs")
        return self

    @computed_field
    @property
    def verified_fraction(self) -> float | None:
        return self.verified_pairs / self.required_pairs if self.required_pairs else None
