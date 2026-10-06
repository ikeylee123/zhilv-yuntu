"""Small MCP ToolProvider for evaluators. No retries or provider/network fallback."""
import asyncio
from uuid import uuid4
from mcp import Client
from pydantic import ValidationError
from app.models.tool_contracts import REQUESTS, build_tool_query_key, failure_result, validate_result
from app.services.tool_adapter import RunRegistry

class MCPToolProvider:
    def __init__(self, url, *, timeout=25, registry=None):
        self.url, self.timeout = url, timeout
        self.registry = registry or RunRegistry()

    def previous(self, name, arguments):
        return None  # Transport failure carries no stale data. Never invent a prior observation.

    async def discover(self):
        async with asyncio.timeout(self.timeout):
            async with Client(self.url, read_timeout_seconds=self.timeout) as client:
                return (await client.list_tools()).tools

    def execute(self, name, arguments):
        if name not in REQUESTS:
            raise ValueError("Tool not in allowlist")
        try:
            key = build_tool_query_key(name, arguments)
        except (ValueError, TypeError):
            return failure_result(name, "invalid_argument", "INVALID_ARGUMENT")
        # Synchronous ToolProvider; async consumers use acall (or to_thread).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.registry.execute(key, lambda: asyncio.run(self._call(name, arguments)))
        raise RuntimeError("Use acall or asyncio.to_thread(execute) from an async consumer")

    async def acall(self, name, arguments):
        # Use the same thread-safe run registry for synchronous and async callers.
        return await asyncio.to_thread(self.execute, name, arguments)

    async def _call(self, name, arguments):
        if name not in REQUESTS:
            raise ValueError("Tool not in allowlist")
        try:
            key = build_tool_query_key(name, arguments)
        except (ValueError, TypeError):
            return failure_result(name, "invalid_argument", "INVALID_ARGUMENT")
        request_id = str(uuid4())
        try:
            async with asyncio.timeout(self.timeout):
                async with Client(self.url, read_timeout_seconds=self.timeout) as client:
                    try:
                        # Public session API performs one call; no autonomous rounds or header-recovery retry.
                        response = await client.session.call_tool(name, arguments)
                    except (RuntimeError, ValidationError):
                        return failure_result(name, "invalid_response", "MALFORMED_TOOL_RESULT", key, request_id)
            return self.parse(name, arguments, response, request_id)
        except TimeoutError:
            return failure_result(name, "timeout", "TOOL_TIMEOUT", key, request_id)
        except Exception:
            return failure_result(name, "provider_error", "TOOL_UNAVAILABLE", key, request_id)

    @staticmethod
    def parse(name, arguments, response, request_id="mcp-response"):
        key = build_tool_query_key(name, arguments)
        try:
            if response.structured_content is None:
                raise ValueError("Missing structured result")
            result = validate_result(name, arguments, response.structured_content)
            if response.is_error and result.status == "success":
                raise ValueError("Contradictory success")
            return result
        except (ValueError, TypeError, AttributeError):
            return failure_result(name, "invalid_response", "MALFORMED_TOOL_RESULT", key, request_id)
