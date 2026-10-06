"""Actual HTTPX request logging with an offline provider and a sentinel credential."""
import logging
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import map_service
from app.services.tool_adapter import DirectToolProvider
from test_tool_contracts import ARGS

SECRET = "TEST_SECRET_AMAP_KEY_ABC123"


@pytest.mark.parametrize("outcome,status", [
    ("success", "success"), ("http_error", "provider_error"),
    ("timeout", "timeout"), ("connection_error", "provider_error"),
    ("invalid_response", "invalid_response"), ("provider_error", "provider_error"),
])
@pytest.mark.parametrize("through_adapter", [False, True], ids=["service", "adapter"])
def test_provider_key_reaches_provider_but_not_logs(monkeypatch, caplog, outcome, status, through_adapter):
    received = []
    cache_keys = []
    monkeypatch.setattr(map_service, "AMAP_API_KEY", SECRET)
    monkeypatch.setattr(map_service, "AMAP_BASE_URL", "https://amap.invalid/v3")
    caplog.set_level(logging.INFO)
    for name in ("httpx", "httpcore", "httpcore.http11", map_service.__name__):
        caplog.set_level(logging.INFO, logger=name)

    def respond(request):
        received.append(request.url.params["key"])
        assert request.url.params["key"] == SECRET
        # httpcore currently uses DEBUG for traces; exercise its INFO boundary too.
        logging.getLogger("httpcore.http11").info("provider request %s", request.url)
        if outcome == "timeout":
            raise httpx.ReadTimeout(f"timeout {request.url}", request=request)
        if outcome == "connection_error":
            raise httpx.ConnectError(f"failed {request.url}", request=request)
        if outcome == "http_error":
            return httpx.Response(503, text=SECRET)
        if outcome == "invalid_response":
            return httpx.Response(200, text=SECRET)
        if outcome == "provider_error":
            return httpx.Response(200, json={"status": "0", "info": SECRET, "infocode": SECRET})
        return httpx.Response(200, json={"status": "1", "route": {
            "paths": [{"distance": "1200", "duration": "600"}]}})

    monkeypatch.setattr(map_service, "_build_client", lambda: httpx.Client(
        transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda *a, **kw: pytest.fail("Live provider network"))
    if through_adapter:
        provider = DirectToolProvider(cache_get=lambda key: cache_keys.append(key),
                                      cache_set=lambda key, *a, **kw: cache_keys.append(key))
        result = provider.execute("get_driving_route", ARGS)
        assert result.status == status
        assert SECRET not in result.model_dump_json()
        assert cache_keys and all(SECRET not in key for key in cache_keys)
        if outcome == "success":
            assert result.data.distance_meters == 1200
            assert result.data.duration_seconds == 600
    elif outcome == "success":
        assert map_service._request_amap("/direction/driving", {})["status"] == "1"
    else:
        with pytest.raises(map_service.AmapServiceError):
            try:
                map_service._request_amap("/direction/driving", {})
            except map_service.AmapServiceError:
                # Ordinary callers may log exceptions using another project logger.
                logging.getLogger("app.provider_caller").exception("Provider failed")
                raise
        assert "Provider failed" in caplog.text  # Business errors remain visible.
    assert received == [SECRET]
    assert SECRET not in caplog.text
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert "***" in caplog.text
    if outcome not in ("timeout", "connection_error"):
        assert any(r.name == "httpx" and "HTTP Request" in r.getMessage() for r in caplog.records)
    assert any(r.name == "httpcore.http11" for r in caplog.records)


@pytest.mark.parametrize("name", ["httpx", "httpcore.http11", map_service.__name__])
def test_redaction_covers_structured_args_and_exception_text(monkeypatch, caplog, name):
    monkeypatch.setattr(map_service, "AMAP_API_KEY", SECRET)
    map_service._protect_amap_logs()
    caplog.set_level(logging.INFO)
    logger = logging.getLogger(name)
    logger.info("request %(url)s", {"url": f"https://amap.invalid/?key={SECRET}"})
    try:
        raise RuntimeError(f"provider rejected key={SECRET}")
    except RuntimeError:
        logger.exception("request failed %s", {"key": SECRET})
    assert SECRET not in caplog.text
    assert "key=***" in caplog.text
    assert "RuntimeError" in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert len(caplog.records) == 2
