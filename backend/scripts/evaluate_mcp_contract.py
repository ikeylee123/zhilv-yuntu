"""Offline contract evaluation through real loopback Streamable HTTP, not planning accuracy."""
from contextlib import contextmanager
from datetime import datetime
import asyncio
import json
import logging
from jsonschema import Draft202012Validator
from pathlib import Path
import socket
import sys
from threading import Thread
from time import monotonic, sleep
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app.mcp.server import create_testable_app
from app.mcp.client import MCPToolProvider
from app.services.tool_adapter import DirectToolProvider
from app.services.tool_replay import ReplayBackend

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "eval/mcp_contract_cases.json"

@contextmanager
def running_gateway(factory, *, app=None):
    """Bind only loopback, use an OS-assigned port, always terminate the test server."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app if app is not None else create_testable_app(factory), log_level="critical", access_log=False))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not server.started:
            if not thread.is_alive() or monotonic() > deadline:
                raise RuntimeError("Local MCP test server did not start")
            sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp/"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local MCP test server failed to stop")

def provider_for(case):
    now = datetime.fromisoformat(case.get("fetched_at", "2026-10-06T00:00:00+00:00"))
    return DirectToolProvider(backend=ReplayBackend(case), cache_get=lambda k: None,
        cache_set=lambda *a, **k: None, clock=lambda: now)

def comparable(result):
    data = result.model_dump(mode="json")
    data["metadata"].pop("request_id")
    return data

def evaluate(cases):
    active = [None]
    def factory(): return provider_for(active[0])
    rows = []
    schema_checks = []
    with running_gateway(factory) as url:
        async def run():
            client = MCPToolProvider(url)
            tools = await client.discover()
            discovered = {t.name for t in tools}
            schemas = {t.name: t for t in tools}
            for tool in tools:
                Draft202012Validator.check_schema(tool.input_schema)
                Draft202012Validator.check_schema(tool.output_schema)
            for case in cases:
                active[0] = case
                direct = provider_for(case).execute(case["tool"], case["arguments"])
                remote_provider = MCPToolProvider(url)
                remote = await remote_provider.acall(case["tool"], case["arguments"])
                duplicate = await remote_provider.acall(case["tool"], case["arguments"])
                replayed = await MCPToolProvider(url).acall(case["tool"], case["arguments"])
                tool_schema = schemas[case["tool"]]
                schema_checks.append(not list(Draft202012Validator(tool_schema.input_schema).iter_errors(case["arguments"]))
                                     and not list(Draft202012Validator(tool_schema.output_schema).iter_errors(remote.model_dump(mode="json"))))
                rows.append(dict(id=case["id"], equivalent=comparable(direct)==comparable(remote),
                    expected=case["expected"] == {"status": remote.status.value, "reason": remote.reason},
                    status=remote.status.value, reason=remote.reason,
                    schema_ok=schema_checks[-1],
                    replay_deterministic=comparable(remote)==comparable(replayed),
                    dedup_ok=duplicate.metadata.deduplicated and duplicate.metadata.provider_attempts==0,
                    protocol_ok=remote.reason not in ("TOOL_UNAVAILABLE", "TOOL_TIMEOUT", "MALFORMED_TOOL_RESULT")))
            return discovered
        discovered = asyncio.run(run())
    return dict(cases=len(cases), discovery_ok=discovered=={"search_poi","get_poi","get_driving_route"},
        contract_equivalence_rate=sum(r["equivalent"] for r in rows)/len(rows),
        error_mapping_accuracy=sum(r["expected"] for r in rows)/len(rows),
        tool_schema_pass_rate=sum(schema_checks)/len(schema_checks),
        replay_determinism=sum(r["replay_deterministic"] for r in rows)/len(rows),
        same_run_duplicate_suppression_rate=sum(r["dedup_ok"] for r in rows)/len(rows),
        mcp_protocol_success_rate=sum(r["protocol_ok"] for r in rows)/len(rows),
        mismatches=[r for r in rows if not all(r[k] for k in ("equivalent", "expected", "schema_ok", "replay_deterministic", "dedup_ok", "protocol_ok"))])

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    result = evaluate(json.loads(DEFAULT_CASES.read_text(encoding="utf-8"))["cases"])
    print(json.dumps(result, indent=2))
    raise SystemExit(int(bool(result["mismatches"]) or not result["discovery_ok"]))
