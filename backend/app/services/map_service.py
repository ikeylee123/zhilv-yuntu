from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from app.config import (
    AMAP_API_KEY,
    AMAP_BASE_URL,
    AMAP_DEFAULT_CITY,
    AMAP_TIMEOUT_SECONDS,
    REDIS_MAP_TTL_SECONDS,
)
from app.models.schemas import HotelItem, Itinerary, SpotItem, TransportItem
from app.services.cache_service import get_cached_json, set_cached_json


logger = logging.getLogger(__name__)


def _redact_amap_secret(text: str) -> str:
    if AMAP_API_KEY:
        for value in {AMAP_API_KEY, quote(AMAP_API_KEY, safe=""), quote_plus(AMAP_API_KEY)}:
            text = text.replace(value, "***")
    return text


class _AmapSecretFilter(logging.Filter):
    """Redact rendered URL/structured args and exception text, without dropping logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not AMAP_API_KEY:
            return True
        message = record.getMessage()
        redacted = _redact_amap_secret(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_info:
            exception = logging.Formatter().formatException(record.exc_info)
            redacted = _redact_amap_secret(exception)
            if redacted != exception:
                record.exc_text, record.exc_info = redacted, None
        elif record.exc_text:
            record.exc_text = _redact_amap_secret(record.exc_text)
        return True


_amap_secret_filter = _AmapSecretFilter()


def _protect_amap_logs() -> None:
    # Logger filters do not propagate to children. Install on HTTP emitters after
    # client construction has loaded httpcore, including optional protocol loggers.
    names = {__name__, "httpx", "httpcore", "httpcore.http11", "httpcore.http2",
             "httpcore.connection", "httpcore.proxy", "httpcore.socks"}
    names.update(name for name in list(logging.Logger.manager.loggerDict)
                 if name.startswith(("httpx.", "httpcore.")))
    for name in names:
        logging.getLogger(name).addFilter(_amap_secret_filter)


class AmapServiceError(RuntimeError):
    """不包含 API Key 等敏感请求信息的高德服务错误。"""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(_redact_amap_secret(message))
        self.reason = _redact_amap_secret(reason) if reason else reason


def _ensure_amap_api_key() -> None:
    """确保当前环境已经配置高德地图 Key。"""
    if not AMAP_API_KEY:
        raise AmapServiceError(
            "当前环境未配置 AMAP_API_KEY，无法调用高德地图服务。",
            reason="missing_api_key",
        )


def _build_client() -> httpx.Client:
    """创建访问高德 HTTP API 的客户端。"""
    return httpx.Client(timeout=AMAP_TIMEOUT_SECONDS)


def _request_amap(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """调用高德地图 API 并返回 JSON 结果。"""
    _ensure_amap_api_key()

    request_params = {
        "key": AMAP_API_KEY,
        **params,
    }

    try:
        with _build_client() as client:
            _protect_amap_logs()
            response = client.get(f"{AMAP_BASE_URL}{path}", params=request_params)
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise AmapServiceError(
            "高德地图接口请求超时。",
            reason="request_timeout",
        ) from None
    except httpx.HTTPStatusError as exc:
        raise AmapServiceError(
            f"高德地图接口返回 HTTP {exc.response.status_code}。",
            reason=f"http_{exc.response.status_code}",
        ) from None
    except httpx.RequestError as exc:
        raise AmapServiceError(
            "暂时无法连接高德地图接口。",
            reason="request_error",
        ) from None
    except ValueError as exc:
        raise AmapServiceError(
            "高德地图接口返回了无法解析的数据。",
            reason="invalid_response",
        ) from None

    if not isinstance(payload, dict):
        raise AmapServiceError(
            "高德地图接口返回了格式异常的数据。",
            reason="invalid_response",
        )

    if payload.get("status") != "1":
        info = payload.get("info", "未知错误")
        infocode = str(payload.get("infocode") or "amap_rejected")
        raise AmapServiceError(
            f"高德地图接口调用失败：{info}",
            reason=infocode,
        )

    return payload


def _parse_float(value: str | None) -> float | None:
    """把字符串安全转换成浮点数。"""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_location(location: str | None) -> tuple[float | None, float | None]:
    """把高德返回的 '经度,纬度' 文本拆成两个浮点数。"""
    if not location or "," not in location:
        return None, None

    longitude_text, latitude_text = location.split(",", 1)
    return _parse_float(latitude_text), _parse_float(longitude_text)


def _normalize_cache_text(value: str | None) -> str:
    """把缓存 key 里用到的文本做简单标准化。"""
    if value is None:
        return ""
    return value.strip().lower()


def geocode_address(address: str, city: str | None = None) -> dict[str, Any] | None:
    """根据地址获取经纬度信息。"""
    cache_key = (
        f"map:geocode:{_normalize_cache_text(address)}:{_normalize_cache_text(city or AMAP_DEFAULT_CITY)}"
    )
    cached_value = get_cached_json(cache_key)
    if cached_value is not None:
        logger.info("map geocode cache hit: address=%s city=%s", address, city or AMAP_DEFAULT_CITY)
        return cached_value
    logger.info("map geocode cache miss: address=%s city=%s", address, city or AMAP_DEFAULT_CITY)

    payload = _request_amap(
        "/geocode/geo",
        {
            "address": address,
            "city": city or AMAP_DEFAULT_CITY,
        },
    )

    geocodes = payload.get("geocodes", [])
    if not geocodes:
        return None

    first = geocodes[0]
    latitude, longitude = _split_location(first.get("location"))
    result = {
        "formatted_address": first.get("formatted_address", address),
        "province": first.get("province"),
        "city": first.get("city"),
        "district": first.get("district"),
        "adcode": first.get("adcode"),
        "latitude": latitude,
        "longitude": longitude,
    }
    set_cached_json(cache_key, result, expire_seconds=REDIS_MAP_TTL_SECONDS)
    return result


def resolve_administrative_area(keyword: str) -> dict[str, Any] | None:
    """根据目的地名称查询高德行政区，确认城市或旅游目的地是否存在。"""
    normalized_keyword = _normalize_cache_text(keyword)
    cache_key = f"map:district:{normalized_keyword}"
    cached_value = get_cached_json(cache_key)
    if cached_value is not None:
        logger.info("map district cache hit: keyword=%s", keyword)
        return cached_value
    logger.info("map district cache miss: keyword=%s", keyword)

    payload = _request_amap(
        "/config/district",
        {
            "keywords": keyword,
            "subdistrict": 0,
            "extensions": "base",
        },
    )

    districts = payload.get("districts", [])
    supported_levels = {"province", "city", "district"}
    candidates = [
        district
        for district in districts
        if isinstance(district, dict)
        and district.get("name")
        and district.get("level") in supported_levels
    ]

    selected: dict[str, Any] | None = None
    for candidate in candidates:
        candidate_name = _normalize_cache_text(str(candidate.get("name") or ""))
        candidate_name_without_city = (
            candidate_name[:-1] if candidate_name.endswith("市") else candidate_name
        )
        if candidate_name_without_city == normalized_keyword:
            selected = candidate
            break

    if selected is None:
        for candidate in candidates:
            candidate_name = _normalize_cache_text(str(candidate.get("name") or ""))
            if normalized_keyword in candidate_name:
                selected = candidate
                break

    if selected is None:
        return None

    latitude, longitude = _split_location(selected.get("center"))
    result = {
        "name": selected.get("name"),
        "adcode": selected.get("adcode"),
        "citycode": selected.get("citycode"),
        "level": selected.get("level"),
        "latitude": latitude,
        "longitude": longitude,
    }
    set_cached_json(cache_key, result, expire_seconds=REDIS_MAP_TTL_SECONDS)
    return result


def search_places(
    keyword: str = "",
    city: str | None = None,
    page_size: int = 5,
    types: str | None = None,
    page: int = 1,
    city_limit: bool = False,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """根据关键词搜索 POI。"""
    if not keyword.strip() and not (types or "").strip():
        raise ValueError("POI 搜索必须提供 keyword 或 types。")
    if not 1 <= page_size <= 25:
        raise ValueError("POI 搜索 page_size 必须在 1 到 25 之间。")
    if page < 1:
        raise ValueError("POI 搜索 page 必须大于等于 1。")

    cache_key = (
        "map:place:"
        f"{_normalize_cache_text(keyword)}:"
        f"{_normalize_cache_text(types)}:"
        f"{_normalize_cache_text(city or AMAP_DEFAULT_CITY)}:"
        f"{page_size}:{page}:{city_limit}"
    )
    cached_value = get_cached_json(cache_key) if use_cache else None
    if cached_value is not None:
        logger.info("map place cache hit: keyword=%s city=%s", keyword, city or AMAP_DEFAULT_CITY)
        return cached_value
    if use_cache:
        logger.info("map place cache miss: keyword=%s city=%s", keyword, city or AMAP_DEFAULT_CITY)

    request_params: dict[str, Any] = {
        "keywords": keyword,
        "city": city or AMAP_DEFAULT_CITY,
        "offset": page_size,
        "page": page,
        "extensions": "all",
    }
    if types:
        request_params["types"] = types
    if city_limit:
        request_params["citylimit"] = "true"

    payload = _request_amap("/place/text", request_params)

    pois = payload.get("pois", [])
    results: list[dict[str, Any]] = []
    for poi in pois:
        latitude, longitude = _split_location(poi.get("location"))
        photos = poi.get("photos") if isinstance(poi.get("photos"), list) else []
        first_photo = photos[0] if photos and isinstance(photos[0], dict) else {}
        results.append(
            {
                "name": poi.get("name"),
                "address": poi.get("address"),
                "province": poi.get("pname"),
                "cityname": poi.get("cityname"),
                "adname": poi.get("adname"),
                "adcode": poi.get("adcode"),
                "type": poi.get("type"),
                "poi_id": poi.get("id"),
                "image_url": first_photo.get("url"),
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    if use_cache:
        set_cached_json(cache_key, results, expire_seconds=REDIS_MAP_TTL_SECONDS)
    return results


def estimate_route(
    origin_longitude: float,
    origin_latitude: float,
    destination_longitude: float,
    destination_latitude: float,
) -> dict[str, Any] | None:
    """估算两点之间的驾车距离和耗时。"""
    cache_key = (
        "map:route:"
        f"{origin_longitude:.6f},{origin_latitude:.6f}:"
        f"{destination_longitude:.6f},{destination_latitude:.6f}"
    )
    cached_value = get_cached_json(cache_key)
    if cached_value is not None:
        logger.info(
            "map route cache hit: origin=%s,%s destination=%s,%s",
            origin_longitude,
            origin_latitude,
            destination_longitude,
            destination_latitude,
        )
        return cached_value
    logger.info(
        "map route cache miss: origin=%s,%s destination=%s,%s",
        origin_longitude,
        origin_latitude,
        destination_longitude,
        destination_latitude,
    )

    payload = _request_amap(
        "/direction/driving",
        {
            "origin": f"{origin_longitude},{origin_latitude}",
            "destination": f"{destination_longitude},{destination_latitude}",
            "strategy": 0,
        },
    )

    route = payload.get("route", {})
    paths = route.get("paths", [])
    if not paths:
        return None

    first_path = paths[0]
    distance_meters = _parse_float(first_path.get("distance"))
    duration_seconds = _parse_float(first_path.get("duration"))

    result = {
        "distance_meters": distance_meters,
        "distance_km": round(distance_meters / 1000, 2) if distance_meters is not None else None,
        "duration_seconds": duration_seconds,
        "estimated_minutes": round(duration_seconds / 60) if duration_seconds is not None else None,
        "taxi_cost": _parse_float(route.get("taxi_cost")),
    }
    set_cached_json(cache_key, result, expire_seconds=REDIS_MAP_TTL_SECONDS)
    return result


def _pick_best_place(keyword: str, city: str | None = None) -> dict[str, Any] | None:
    """优先选择名称匹配且带照片的 POI，避免首条结果没有图片。"""
    results = search_places(keyword=keyword, city=city, page_size=5)
    if not results:
        return None

    normalized_keyword = _normalize_cache_text(keyword)
    for result in results:
        normalized_name = _normalize_cache_text(str(result.get("name") or ""))
        if (
            result.get("image_url")
            and normalized_name
            and (
                normalized_name in normalized_keyword
                or normalized_keyword in normalized_name
            )
        ):
            return result

    return results[0]


def _enrich_spot(spot: SpotItem, city: str | None = None) -> bool:
    """补全单个景点的地址、经纬度和 POI 信息。"""
    place = _pick_best_place(spot.name, city=city)
    if place is None and spot.location:
        place = _pick_best_place(spot.location, city=city)

    if place is None:
        query_address = spot.address or spot.location or spot.name
        geocode = geocode_address(query_address, city=city)
        if geocode is None:
            return False
        spot.address = geocode.get("formatted_address") or spot.address
        spot.latitude = geocode.get("latitude")
        spot.longitude = geocode.get("longitude")
        return True

    spot.address = place.get("address") or spot.address
    spot.image_url = place.get("image_url") or spot.image_url
    spot.latitude = place.get("latitude")
    spot.longitude = place.get("longitude")
    spot.poi_id = place.get("poi_id") or spot.poi_id
    return True


def _enrich_hotel(hotel: HotelItem, city: str | None = None) -> bool:
    """补全单个酒店的地址和经纬度。"""
    place = _pick_best_place(hotel.name, city=city)
    if place is None and hotel.location:
        place = _pick_best_place(hotel.location, city=city)

    if place is None:
        query_address = hotel.address or hotel.location or hotel.name
        geocode = geocode_address(query_address, city=city)
        if geocode is None:
            return False
        hotel.address = geocode.get("formatted_address") or hotel.address
        hotel.latitude = geocode.get("latitude")
        hotel.longitude = geocode.get("longitude")
        return True

    hotel.address = place.get("address") or hotel.address
    hotel.latitude = place.get("latitude")
    hotel.longitude = place.get("longitude")
    hotel.poi_id = place.get("poi_id") or hotel.poi_id
    hotel.image_url = place.get("image_url") or hotel.image_url
    return True


def _geocode_place_text(place_text: str | None, city: str | None = None) -> dict[str, Any] | None:
    """把文本地点尽量解析成带经纬度的结果。"""
    if not place_text:
        return None

    place = _pick_best_place(place_text, city=city)
    if place is not None:
        return {
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "address": place.get("address"),
        }

    geocode = geocode_address(place_text, city=city)
    if geocode is not None:
        return {
            "latitude": geocode.get("latitude"),
            "longitude": geocode.get("longitude"),
            "address": geocode.get("formatted_address"),
        }
    return None


def _enrich_transport(transport: TransportItem, city: str | None = None) -> bool:
    """补全单段交通的距离和耗时信息。"""
    origin = _geocode_place_text(transport.from_place, city=city)
    destination = _geocode_place_text(transport.to_place, city=city)
    if not origin or not destination:
        return False

    if origin.get("latitude") is None or origin.get("longitude") is None:
        return False
    if destination.get("latitude") is None or destination.get("longitude") is None:
        return False

    route = estimate_route(
        origin_longitude=origin["longitude"],
        origin_latitude=origin["latitude"],
        destination_longitude=destination["longitude"],
        destination_latitude=destination["latitude"],
    )
    if route is None:
        return False

    transport.distance_km = route.get("distance_km")
    transport.estimated_minutes = route.get("estimated_minutes")
    if route.get("estimated_minutes") is not None and not transport.duration:
        transport.duration = f"{route['estimated_minutes']} 分钟"
    return True


def enrich_itinerary_with_map_data(itinerary: Itinerary, city: str | None = None) -> Itinerary:
    """使用高德服务补全 itinerary 里的地图字段。"""
    enriched_count = 0

    for day in itinerary.days:
        for spot in day.spots:
            try:
                if _enrich_spot(spot, city=city or itinerary.destination):
                    enriched_count += 1
            except Exception:
                continue

        if day.hotel is not None:
            try:
                if _enrich_hotel(day.hotel, city=city or itinerary.destination):
                    enriched_count += 1
            except Exception:
                pass

        for transport in day.transport:
            try:
                if _enrich_transport(transport, city=city or itinerary.destination):
                    enriched_count += 1
            except Exception:
                continue

    if enriched_count > 0:
        note = "已补充高德地图地址、坐标或路线估算信息。"
        if note not in itinerary.source_notes:
            itinerary.source_notes.append(note)

    return itinerary


def fetch_driving_route_fact(origin, destination) -> dict[str, Any] | None:
    """Fresh, uncached driving estimate for Phase 2; never use the legacy 24h cache."""
    payload = _request_amap("/direction/driving", {
        "origin": f"{origin.longitude},{origin.latitude}",
        "destination": f"{destination.longitude},{destination.latitude}", "strategy": 0,
    })
    route = payload.get("route")
    if not isinstance(route, dict) or not isinstance(route.get("paths"), list):
        raise AmapServiceError("Malformed route response", reason="invalid_response")
    if not route["paths"]:
        return None
    path = route["paths"][0]
    if not isinstance(path, dict):
        raise AmapServiceError("Malformed route path", reason="invalid_response")
    # Preserve raw units and missing fields; the typed collector validates numeric data.
    return {"distance_meters": path.get("distance"), "duration_seconds": path.get("duration")}


def fetch_place_identity(poi_id: str) -> dict[str, Any] | None:
    """Exact ID lookup for edit/save binding; no name search or first-result substitution."""
    payload = _request_amap("/place/detail", {"id": poi_id, "extensions": "base"})
    places = payload.get("pois")
    if not isinstance(places, list):
        raise AmapServiceError("Malformed POI response", reason="invalid_response")
    matches = [place for place in places if isinstance(place, dict) and place.get("id") == poi_id]
    if len(matches) != 1:
        return None
    place = matches[0]
    latitude, longitude = _split_location(place.get("location"))
    return {"poi_id": place.get("id"), "name": place.get("name"),
            "latitude": latitude, "longitude": longitude}
