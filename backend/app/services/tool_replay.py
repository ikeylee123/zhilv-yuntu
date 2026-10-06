"""Offline raw-provider fixtures for contract replay; no network dependencies."""
from copy import deepcopy
import httpx

class ReplayBackend:
    def __init__(self, case):
        self.case = deepcopy(case)
        self.calls = 0

    def _respond(self, tool, request):
        from app.models.tool_contracts import build_tool_query_key
        if tool != self.case["tool"] or build_tool_query_key(tool, request.model_dump()) != build_tool_query_key(tool, self.case["arguments"]):
            raise ValueError("Replay query missing")
        self.calls += 1
        if self.case.get("exception") == "timeout":
            raise httpx.ReadTimeout("fixed replay timeout")
        if self.case.get("exception") == "provider_error":
            raise RuntimeError("fixed replay provider failure")
        return deepcopy(self.case["provider_response"])

    def route(self, request): return self._respond("get_driving_route", request)
    def search(self, request): return self._respond("search_poi", request)
    def lookup(self, request): return self._respond("get_poi", request)
