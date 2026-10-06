import asyncio
from pathlib import Path
import sys
from time import sleep
import pytest
import httpx
from mcp import Client
from mcp.types import CallToolResult
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_mcp_contract import running_gateway,provider_for,comparable
from app.mcp.client import MCPToolProvider
from app.services.tool_adapter import ReplayToolProvider
from app.models.tool_contracts import build_tool_query_key
from test_tool_adapter import CASES,make
from test_tool_contracts import ARGS,success
@pytest.fixture(scope='module')
def gateway():
    active=[CASES[0]]
    with running_gateway(lambda:provider_for(active[0])) as url:yield url,active
@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_equivalence(gateway,case):
    url,active=gateway;active[0]=case
    direct=provider_for(case).execute(case['tool'],case['arguments'])
    remote=MCPToolProvider(url).execute(case['tool'],case['arguments'])
    assert comparable(direct)==comparable(remote)
    assert {'status':remote.status.value,'reason':remote.reason}==case['expected']
def test_discovery_schemas_invalid_arguments(gateway):
    url,active=gateway;active[0]=CASES[0]
    async def run():
        async with Client(url) as c:
            tools=(await c.list_tools()).tools
            assert {t.name for t in tools}=={'search_poi','get_poi','get_driving_route'}
            for t in tools:
                assert t.input_schema and t.output_schema
                assert t.annotations.read_only_hint and not t.annotations.destructive_hint
            route=next(t for t in tools if t.name=='get_driving_route')
            assert 'origin' in route.input_schema['properties']
            assert (await c.call_tool('get_driving_route',{'origin':{'latitude':True,'longitude':120}})).is_error
            assert (await c.call_tool('save_itinerary',{})).is_error
            extra=await c.call_tool('get_poi',{'poi_id':'a','arbitrary_url':'http://evil'})
            assert extra.is_error
    asyncio.run(run())
def test_disconnect_unavailable():
    with running_gateway(lambda:provider_for(CASES[0])) as url:
        assert MCPToolProvider(url).execute('get_driving_route',ARGS).status=='success'
    r=MCPToolProvider(url,timeout=5).execute('get_driving_route',ARGS)
    assert r.status=='provider_error' and r.reason=='TOOL_UNAVAILABLE'
@pytest.mark.parametrize('mutation',['missing','wrong_key','bool','source','contradictory','endpoint'])
def test_malformed_result(mutation):
    raw=success().model_dump(mode='json')
    if mutation=='missing':raw=None
    if mutation=='wrong_key':raw['metadata']['query_hash']='0'*64
    if mutation=='bool':raw['data']['distance_meters']=True
    if mutation=='source':raw['source']['provider']='attacker'
    if mutation=='endpoint':raw['data']['origin']['latitude']=0
    response=CallToolResult(content=[],structuredContent=raw,isError=mutation=='contradictory')
    r=MCPToolProvider.parse('get_driving_route',ARGS,response)
    assert r.status=='invalid_response' and r.reason=='MALFORMED_TOOL_RESULT'
def test_timeout():
    class Slow:
        def route(self,request):sleep(.2);return dict(distance_meters=1200,duration_seconds=600)
    with running_gateway(lambda:make(CASES[0],backend=Slow())) as url:
        r=MCPToolProvider(url,timeout=.05).execute('get_driving_route',ARGS)
        assert r.status=='timeout' and r.reason=='TOOL_TIMEOUT'
def test_concurrent_client_dedup():
    class Backend:
        calls=0
        def route(self,request):self.calls+=1;sleep(.05);return dict(distance_meters=1200,duration_seconds=600)
    b=Backend()
    with running_gateway(lambda:make(CASES[0],backend=b)) as url:
        p=MCPToolProvider(url)
        async def run():return await asyncio.gather(*(p.acall('get_driving_route',ARGS) for _ in range(6)))
        results=asyncio.run(run())
        assert b.calls==1 and sum(r.metadata.deduplicated for r in results)==5
def test_replay_gateway_no_provider_network(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',lambda *a:pytest.fail('provider network'))
    observation=success().model_dump(mode='json')
    with running_gateway(lambda:ReplayToolProvider({build_tool_query_key('get_driving_route',ARGS):observation})) as url:
        assert MCPToolProvider(url).execute('get_driving_route',ARGS).data.duration_seconds==600
        assert MCPToolProvider(url).execute('get_poi',{'poi_id':'missing'}).reason=='REPLAY_MISS'


def test_fastapi_mount_lifecycle():
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from app.mcp.server import mount_gateway
    events=[]
    @asynccontextmanager
    async def lifespan(app):
        events.append('start')
        yield
        events.append('stop')
    app=FastAPI(lifespan=lifespan)
    mount_gateway(app,lambda:provider_for(CASES[0]))
    with running_gateway(None,app=app) as url:
        assert MCPToolProvider(url).execute('get_driving_route',ARGS).status=='success'
        assert events==['start']
    assert events==['start','stop']

@pytest.mark.parametrize('bad',['missing','bool','wrong_source'])
def test_malformed_peer_over_protocol(bad):
    from mcp.server.lowlevel import Server
    from mcp.types import Tool,ListToolsResult,TextContent
    from app.models.tool_contracts import REQUESTS,RESULTS
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from contextlib import asynccontextmanager
    async def listing(ctx,params):
        return ListToolsResult(tools=[Tool(name='get_driving_route',inputSchema=REQUESTS['get_driving_route'].model_json_schema(),outputSchema=RESULTS['get_driving_route'].model_json_schema())])
    async def call(ctx,params):
        raw=success().model_dump(mode='json')
        if bad=='bool':raw['data']['duration_seconds']=False
        if bad=='wrong_source':raw['source']['provider']='fake'
        if bad=='missing':return CallToolResult(content=[TextContent(type='text',text='not structured')])
        return CallToolResult(content=[],structuredContent=raw)
    server=Server('malformed-fixture',on_list_tools=listing,on_call_tool=call)
    gateway=server.streamable_http_app(streamable_http_path='/',stateless_http=True,json_response=True)
    @asynccontextmanager
    async def lifespan(app):
        async with gateway.router.lifespan_context(gateway):yield
    app=Starlette(routes=[Mount('/mcp',gateway)],lifespan=lifespan)
    with running_gateway(None,app=app) as url:
        r=MCPToolProvider(url).execute('get_driving_route',ARGS)
        assert r.status=='invalid_response' and r.reason=='MALFORMED_TOOL_RESULT'


def test_collector_uses_mcp_provider_without_business_branch(gateway):
    from route_fixtures import state,trusted,NOW
    from app.services.planning_fact_service import collect_route_facts
    from app.services.plan_validator import validate_plan
    url,active=gateway;active[0]=CASES[0]
    s=state();s.planning_facts=collect_route_facts(s.current_itinerary,s.constraints,trusted_pois=trusted(s),now=NOW,tool_provider=MCPToolProvider(url))
    assert validate_plan(s).status=='valid' and s.planning_facts.route_facts[0].duration_seconds==600


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
def test_production_app_repeated_lifespan(monkeypatch, caplog, enabled):
    """Same production FastAPI app, real TCP transport and SDK, three lifetimes."""
    import builtins
    import importlib.util
    import logging
    from contextlib import asynccontextmanager
    from app.mcp import server

    provider = provider_for(CASES[0])
    original_mount = server.mount_gateway
    mounts = []

    def mount(app):
        mounts.append(app)
        original_mount(app, lambda: provider)

    monkeypatch.setattr(server, "mount_gateway", mount)
    monkeypatch.setenv("ENABLE_TRAVEL_MCP", str(enabled).lower())
    if not enabled:
        original_import = builtins.__import__
        def no_mcp(name, *args, **kwargs):
            if name == "mcp" or name.startswith(("mcp.", "app.mcp")):
                pytest.fail("Disabled app attempted to import MCP")
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", no_mcp)
        monkeypatch.setattr(server, "create_gateway", lambda *a: pytest.fail("Disabled runtime"))

    spec = importlib.util.spec_from_file_location(
        "hardening_production_app", Path(__file__).resolve().parents[1] / "app/api/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.app
    assert len(mounts) == int(enabled)
    events = []
    prior = app.router.lifespan_context

    @asynccontextmanager
    async def monitored(application):
        before = set(asyncio.all_tasks())
        async with prior(application):
            events.append("start")
            yield
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() - before if not t.done()]
        events.append("stop")

    app.router.lifespan_context = monitored
    caplog.set_level(logging.WARNING)
    for cycle in range(3):
        with running_gateway(None, app=app) as url:
            assert httpx.get(url.replace("/mcp/", "/health")).json() == {"status": "ok"}
            if enabled:
                async def exercise():
                    async with Client(url) as client:
                        tools = (await client.list_tools()).tools
                        assert len(tools) == 3
                        assert {t.name for t in tools} == {"search_poi", "get_poi", "get_driving_route"}
                        result = await client.call_tool("get_driving_route", ARGS)
                        assert not result.is_error
                        assert result.structured_content["status"] == "success"
                asyncio.run(exercise())
            else:
                assert httpx.get(url).status_code == 404
        assert events == ["start", "stop"] * (cycle + 1)
    assert not any(text in caplog.text.lower() for text in
                   ("task was destroyed", "unclosed", "never awaited", "can only be called once"))
