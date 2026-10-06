"""Single optional MCP gateway; raw requests use the same typed adapter contract."""
from contextlib import asynccontextmanager
import anyio
from mcp.server.lowlevel import Server
from mcp.types import Tool, ToolAnnotations, ListToolsResult, CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount
from app.models.tool_contracts import REQUESTS, RESULTS
from app.services.tool_adapter import DirectToolProvider

DESCRIPTIONS = {
    "search_poi": "Search POIs by category and city; does not select or modify an itinerary.",
    "get_poi": "Look up an exact provider POI ID; no name-search substitution.",
    "get_driving_route": "Return a driving estimate in meters/seconds, never a feasibility decision.",
}


def create_gateway(provider_factory=DirectToolProvider):
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    tools = [Tool(name=name, description=DESCRIPTIONS[name], inputSchema=model.model_json_schema(),
                  outputSchema=RESULTS[name].model_json_schema(), annotations=annotations)
             for name, model in REQUESTS.items()]

    async def list_tools(context, params):
        return ListToolsResult(tools=tools)

    async def call_tool(context, params):
        if params.name not in REQUESTS:
            return CallToolResult(content=[TextContent(type="text", text="UNKNOWN_TOOL")], isError=True)
        # No generated wrapper may drop extra arguments before the contract sees them.
        provider = provider_factory()
        result = await anyio.to_thread.run_sync(lambda: provider.execute(params.name, params.arguments or {}))
        return CallToolResult(structuredContent=result.model_dump(mode="json"),
            content=[TextContent(type="text", text=result.model_dump_json())],
            isError=result.status not in ("success", "no_data"))

    server = Server("Travel Tools", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: REQUESTS[name].model_json_schema() if name in REQUESTS else None)
    # Tool definitions and the provider factory are reusable; SDK managers are not.
    runtime = None

    async def dispatch(scope, receive, send):
        if runtime is None:
            raise RuntimeError("MCP gateway lifespan is not running")
        await runtime(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal runtime
        gateway = server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True,
            max_request_body_size=65536,
        )
        async with gateway.router.lifespan_context(gateway):
            runtime = gateway
            try:
                yield
            finally:
                runtime = None

    return Starlette(routes=[Mount("/", app=dispatch)], lifespan=lifespan)


def mount_gateway(app, provider_factory=DirectToolProvider):
    """Explicit opt-in only. Preserve the application's existing lifespan."""
    gateway = create_gateway(provider_factory)
    prior_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with prior_lifespan(application):
            async with gateway.router.lifespan_context(gateway):
                yield
    app.router.lifespan_context = lifespan
    app.mount("/mcp", gateway)


def create_testable_app(provider_factory=DirectToolProvider):
    """Standalone ASGI factory for local evaluator/transport tests; no new deployment."""
    gateway = create_gateway(provider_factory)
    @asynccontextmanager
    async def lifespan(app):
        async with gateway.router.lifespan_context(gateway):
            yield
    return Starlette(routes=[Mount("/mcp", app=gateway)], lifespan=lifespan)
