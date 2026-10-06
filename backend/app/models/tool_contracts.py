"""Transport-independent tool outcomes, never plan feasibility decisions."""
from __future__ import annotations
from enum import StrEnum
from hashlib import sha256
import json
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import AwareDatetime, BeforeValidator, Field, StringConstraints, field_validator, model_validator
from app.models.planning_facts import FrozenModel, Coordinates as FactCoordinates, reject_bool_route_number

SCHEMA_VERSION = "travel-tools-v1"
POLICY_VERSION = "amap-tools-v1"
Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
RouteNumber = Annotated[float, BeforeValidator(reject_bool_route_number), Field(ge=0, allow_inf_nan=False)]
Category = Literal["spot", "meal", "hotel"]

class ToolStatus(StrEnum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"
    INVALID_ARGUMENT = "invalid_argument"
    RATE_LIMITED = "rate_limited"

class Coordinates(FactCoordinates):
    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def reject_bool(cls, value):
        return reject_bool_route_number(value)

class SearchPOIRequest(FrozenModel):
    query: Nonblank
    city_or_adcode: Nonblank
    category: Category
    page: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=5, ge=1, le=25, strict=True)
    city_limit: bool = Field(default=True, strict=True)

class GetPOIRequest(FrozenModel):
    poi_id: Nonblank

class DrivingRouteRequest(FrozenModel):
    origin: Coordinates
    destination: Coordinates
    mode: Nonblank = "driving"
    strategy: int = Field(default=0, strict=True)

class POIData(FrozenModel):
    poi_id: Nonblank
    name: Nonblank
    address: str | None = None
    coordinates: Coordinates | None = None
    # Exact lookup currently supplies no reliable semantic category; never invent one.
    category: Category | None = None
    adcode: str | None = None
    provider_type: str | None = None

class POISearchResult(FrozenModel):
    items: tuple[POIData, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=25)

class DrivingRouteData(FrozenModel):
    origin: Coordinates
    destination: Coordinates
    mode: Literal["driving"] = "driving"
    strategy: Literal[0] = 0
    distance_meters: RouteNumber
    duration_seconds: RouteNumber

class ToolSource(FrozenModel):
    provider: Nonblank
    endpoint: Nonblank
    api_version: Nonblank

class ToolMetadata(FrozenModel):
    request_id: Nonblank
    query_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version: Literal["amap-tools-v1"] = POLICY_VERSION
    cache_hit: bool = False
    provider_attempts: int = Field(default=0, ge=0, strict=True)
    deduplicated: bool = False

T = TypeVar("T")
class ToolResult(FrozenModel, Generic[T]):
    schema_version: Literal["travel-tools-v1"] = SCHEMA_VERSION
    status: ToolStatus
    data: T | None = None
    source: ToolSource
    fetched_at: AwareDatetime | None = None
    reason: Nonblank | None = None
    metadata: ToolMetadata

    @model_validator(mode="after")
    def outcome_invariants(self):
        if self.status == ToolStatus.SUCCESS:
            if self.data is None or self.fetched_at is None or self.reason is not None:
                raise ValueError("Success requires data and aware fetched_at without failure reason")
        elif self.data is not None or self.reason is None:
            raise ValueError("Failure requires a reason and no data")
        return self

REQUESTS = {"search_poi": SearchPOIRequest, "get_poi": GetPOIRequest, "get_driving_route": DrivingRouteRequest}
RESULTS = {"search_poi": ToolResult[POISearchResult], "get_poi": ToolResult[POIData], "get_driving_route": ToolResult[DrivingRouteData]}
ENDPOINTS = {"search_poi": "/place/text", "get_poi": "/place/detail", "get_driving_route": "/direction/driving"}

def build_tool_query_key(tool_name, arguments, *, provider="amap", api_version="v3",
                         schema_version=SCHEMA_VERSION, policy_version=POLICY_VERSION):
    # Only normalized request arguments belong here, never request/trace metadata.
    args = REQUESTS[tool_name].model_validate(arguments).model_dump(mode="json")
    payload = dict(tool_name=tool_name, schema_version=schema_version, provider=provider,
                   api_version=api_version, policy_version=policy_version, normalized_arguments=args)
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def source_for(name):
    return ToolSource(provider="amap", endpoint=ENDPOINTS[name], api_version="v3")

def failure_result(name, status, reason, query_hash="0" * 64, request_id="failure", attempts=0):
    return RESULTS[name](status=status, reason=reason, source=source_for(name),
        metadata=ToolMetadata(request_id=request_id, query_hash=query_hash, provider_attempts=attempts))

def validate_result(name, arguments, raw):
    """Validate an untrusted transport/cache result against the requested query."""
    if not isinstance(raw, dict) or "schema_version" not in raw or "policy_version" not in raw.get("metadata", {}):
        raise ValueError("Missing explicit schema/policy provenance")
    result = RESULTS[name].model_validate(raw)
    request = REQUESTS[name].model_validate(arguments)
    if result.metadata.query_hash != build_tool_query_key(name, arguments) or result.source != source_for(name):
        raise ValueError("Tool result provenance/query mismatch")
    if result.status == ToolStatus.SUCCESS:
        if name == "get_poi" and (result.data.poi_id != request.poi_id or result.data.coordinates is None):
            raise ValueError("Exact POI binding mismatch")
        if name == "get_driving_route" and any(getattr(result.data, k) != getattr(request, k)
                                               for k in ("origin", "destination", "mode", "strategy")):
            raise ValueError("Route query binding mismatch")
        if name == "search_poi" and (result.data.page != request.page or result.data.page_size != request.page_size
                or len(result.data.items) > request.page_size or any(p.category != request.category for p in result.data.items)):
            raise ValueError("Search query binding mismatch")
    return result
