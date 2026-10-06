"""One normalization/cache owner shared by direct workflows and the MCP gateway."""
from __future__ import annotations
from concurrent.futures import Future
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Protocol
from uuid import uuid4
import logging
import httpx
from pydantic import ValidationError
from app.models.tool_contracts import (
    REQUESTS, RESULTS, ToolStatus, ToolMetadata, ToolResult, Coordinates, POIData,
    POISearchResult, DrivingRouteData, build_tool_query_key, source_for, failure_result, validate_result,
)

logger = logging.getLogger(__name__)
TTLS = {"get_driving_route": 900, "get_poi": 900, "search_poi": 86400}

class ToolProvider(Protocol):
    def execute(self, name: str, arguments: dict) -> ToolResult: ...
    def previous(self, name: str, arguments: dict) -> ToolResult | None: ...

class RunRegistry:
    """One process, one explicit run; merge both in-flight and completed outcomes."""
    def __init__(self):
        self._lock = Lock()
        self._requests = {}

    def execute(self, key, call):
        with self._lock:
            future = self._requests.get(key)
            owner = future is None
            if owner:
                future = self._requests[key] = Future()
        if owner:
            try:
                future.set_result(call())
            except BaseException as exc:
                future.set_exception(exc)
                raise
        result = future.result()
        if owner:
            return result
        return result.model_copy(update={"metadata": result.metadata.model_copy(update={
            "request_id": str(uuid4()), "provider_attempts": 0, "deduplicated": True})})

class MapBackend:
    """Lazy imports keep contract/replay users independent of backend config side effects."""
    def search(self, request):
        from app.services import map_service
        return map_service.search_places(keyword=request.query, city=request.city_or_adcode,
            types={"spot": "\u98ce\u666f\u540d\u80dc", "meal": "\u9910\u996e\u670d\u52a1", "hotel": "\u4f4f\u5bbf\u670d\u52a1"}[request.category],
            page=request.page, page_size=request.page_size, city_limit=request.city_limit, use_cache=False)

    def lookup(self, request):
        from app.services import map_service
        return map_service.fetch_place_identity(request.poi_id)

    def route(self, request):
        from app.services import map_service
        return map_service.fetch_driving_route_fact(request.origin, request.destination)

class DirectToolProvider:
    def __init__(self, *, backend=None, registry=None, cache_get=None, cache_set=None, clock=None):
        self.backend = backend or MapBackend()
        self.registry = registry or RunRegistry()
        if cache_get is None or cache_set is None:
            from app.services.cache_service import get_cached_json, set_cached_json
            cache_get = cache_get or get_cached_json
            cache_set = cache_set or set_cached_json
        self.cache_get, self.cache_set = cache_get, cache_set
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._previous = {}

    def previous(self, name, arguments):
        return self._previous.get(build_tool_query_key(name, arguments))

    def search_poi(self, **arguments):
        return self.execute("search_poi", arguments)

    def get_poi(self, **arguments):
        return self.execute("get_poi", arguments)

    def get_driving_route(self, **arguments):
        return self.execute("get_driving_route", arguments)

    def execute(self, name, arguments):
        if name not in REQUESTS:
            raise ValueError("Tool not in allowlist")
        start = perf_counter()
        request_id = str(uuid4())
        try:
            request = REQUESTS[name].model_validate(arguments)
            key = build_tool_query_key(name, request.model_dump())
        except (ValidationError, ValueError, TypeError):
            result = failure_result(name, "invalid_argument", "INVALID_ARGUMENT", request_id=request_id)
        else:
            result = self.registry.execute(key, lambda: self._execute(name, request, key, request_id))
        logger.info("tool_execution %s", dict(request_id=result.metadata.request_id, tool_name=name,
            query_hash=result.metadata.query_hash, provider=result.source.provider, status=result.status.value,
            reason=result.reason, latency_ms=round((perf_counter()-start)*1000, 3),
            cache_hit=result.metadata.cache_hit, provider_attempts=result.metadata.provider_attempts,
            fetched_at=result.fetched_at.isoformat() if result.fetched_at else None,
            schema_version=result.schema_version, policy_version=result.metadata.policy_version))
        return result

    def _execute(self, name, request, key, request_id):
        if name == "get_driving_route" and (request.mode != "driving" or request.strategy != 0):
            return failure_result(name, "unsupported", "UNSUPPORTED", key, request_id)
        cache_key = "tools:v1:" + key
        try:
            raw = self.cache_get(cache_key)
            if raw is not None:
                candidate = validate_result(name, request.model_dump(), raw)
                if candidate.status == ToolStatus.SUCCESS:
                    age = (self.clock() - candidate.fetched_at).total_seconds()
                    if age >= 0:
                        self._previous[key] = candidate
                        if age <= TTLS[name]:
                            return candidate.model_copy(update={"metadata": candidate.metadata.model_copy(update={
                                "request_id": request_id, "cache_hit": True, "provider_attempts": 0, "deduplicated": False})})
        except Exception:
            pass  # Missing/old/invalid cache is not fresh evidence; never stamp it with now.
        try:
            raw = getattr(self.backend, {"search_poi": "search", "get_poi": "lookup", "get_driving_route": "route"}[name])(request)
            if raw is None or (name == "search_poi" and raw == []):
                return failure_result(name, "no_data", "NO_DATA", key, request_id, 1)
            if name == "get_driving_route":
                data = DrivingRouteData(origin=request.origin, destination=request.destination, **raw)
            elif name == "get_poi":
                if raw.get("poi_id") != request.poi_id:
                    return failure_result(name, "invalid_response", "ID_MISMATCH", key, request_id, 1)
                data = self._poi(raw, None)
                if data.coordinates is None:
                    raise ValueError("Missing coordinates")
            else:
                data = POISearchResult(items=tuple(self._poi(p, request.category) for p in raw),
                                       page=request.page, page_size=request.page_size)
            result = RESULTS[name](status="success", data=data, source=source_for(name), fetched_at=self.clock(),
                metadata=ToolMetadata(request_id=request_id, query_hash=key, provider_attempts=1))
            result = validate_result(name, request.model_dump(), result.model_dump())
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(exc, httpx.TimeoutException) or reason == "request_timeout":
                status = "timeout"
            elif isinstance(exc, (ValidationError, ValueError, TypeError, KeyError, AttributeError)) or reason == "invalid_response":
                status = "invalid_response"
            else:
                status = "provider_error"
            return failure_result(name, status, status.upper(), key, request_id, 1)
        try:
            self.cache_set(cache_key, result.model_dump(mode="json"), expire_seconds=TTLS[name])
        except Exception:
            pass  # Cache availability never changes a successful provider observation.
        return result

    @staticmethod
    def _poi(raw, category):
        lat, lon = raw.get("latitude"), raw.get("longitude")
        coordinates = None if lat is None and lon is None else Coordinates(latitude=lat, longitude=lon)
        return POIData(poi_id=raw.get("poi_id"), name=raw.get("name"), address=raw.get("address"),
            coordinates=coordinates, category=category, adcode=raw.get("adcode"), provider_type=raw.get("type"))

class ReplayToolProvider:
    """Normalized fixed observations only; fixture miss NEVER falls back to network."""
    def __init__(self, observations, *, registry=None):
        self.observations = observations
        self.registry = registry or RunRegistry()

    def previous(self, name, arguments):
        return None

    def execute(self, name, arguments):
        if name not in REQUESTS:
            raise ValueError("Tool not in allowlist")
        try:
            key = build_tool_query_key(name, arguments)
        except (ValueError, TypeError, ValidationError):
            return failure_result(name, "invalid_argument", "INVALID_ARGUMENT")
        def replay():
            if key not in self.observations:
                return failure_result(name, "provider_error", "REPLAY_MISS", key)
            try:
                result = validate_result(name, arguments, self.observations[key])
                return result.model_copy(update={"metadata": result.metadata.model_copy(update={
                    "request_id": str(uuid4()), "provider_attempts": 0, "cache_hit": False, "deduplicated": False})})
            except (ValueError, TypeError, ValidationError):
                return failure_result(name, "invalid_response", "INVALID_REPLAY", key)
        return self.registry.execute(key, replay)
