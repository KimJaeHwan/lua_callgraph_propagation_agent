"""Thin MCP client adapters used by the LangGraph agent nodes.

The adapters accept any object exposing a synchronous ``call_tool(name, args)``
method. This keeps the agent independent from a specific MCP transport while
still making every tool result explicit and testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .state import ToolResult


class SyncToolSession(Protocol):
    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        ...


ToolCallable = Callable[[str, dict[str, Any]], dict[str, Any]]


class BaseMcpClient:
    def __init__(self, session: SyncToolSession | ToolCallable):
        self._session = session

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        payload = args or {}
        try:
            if callable(self._session) and not hasattr(self._session, "call_tool"):
                result = self._session(name, payload)
            else:
                result = self._session.call_tool(name, payload)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - transport-specific
            return ToolResult.failure(name, str(exc), payload, retryable=True)

        if not isinstance(result, dict):
            return ToolResult.failure(name, f"tool returned non-dict result: {type(result)!r}", payload)
        if result.get("ok") is False:
            return ToolResult.failure(name, str(result.get("error") or result), payload, retryable=False)
        return ToolResult.success(name, result, payload)


class LuaMcpClient(BaseMcpClient):
    def extract_query_features(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("extract_query_features", kwargs)

    def detect_lua_scope(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("detect_lua_scope", kwargs)

    def bulk_query_retrieval(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("bulk_query_retrieval", kwargs)

    def targeted_retrieval(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("targeted_retrieval", kwargs)

    def select_seed_anchors(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("select_seed_anchors", kwargs)

    def build_runtime_suite(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("build_runtime_suite", kwargs)

    def run_downstream(self, config_path: str) -> ToolResult:
        return self.call_tool("run_downstream", {"config_path": config_path})

    def read_final_report(self, report_json: str) -> ToolResult:
        return self.call_tool("read_final_report", {"report_json": report_json})

    def read_propagation_summary(self, config_path: str) -> ToolResult:
        return self.call_tool("read_propagation_summary", {"config_path": config_path})

    def get_mapping_distribution(self, config_path: str, suspicious_threshold: int = 5) -> ToolResult:
        return self.call_tool(
            "get_mapping_distribution",
            {"config_path": config_path, "suspicious_threshold": suspicious_threshold},
        )

    def list_deferred_cases(self, report_json: str) -> ToolResult:
        return self.call_tool("list_deferred_cases", {"report_json": report_json})

    def show_candidate_context(self, config_path: str, case_id: str) -> ToolResult:
        return self.call_tool("show_candidate_context", {"config_path": config_path, "case_id": case_id})

    def export_trusted_mappings(self, **kwargs: Any) -> ToolResult:
        return self.call_tool("export_trusted_mappings", kwargs)

    def batch_register_force_anchors(self, config_path: str, anchors: list[dict[str, str]]) -> ToolResult:
        return self.call_tool("batch_register_force_anchors", {"config_path": config_path, "anchors": anchors})

    def update_noise_blacklist(
        self,
        suite_json: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> ToolResult:
        return self.call_tool("update_noise_blacklist", {"suite_json": suite_json, "add": add or [], "remove": remove or []})

    def patch_features_with_confirmed(self, query_json: str, confirmed_map: dict[str, str]) -> ToolResult:
        return self.call_tool(
            "patch_features_with_confirmed",
            {"query_json": query_json, "confirmed_map": confirmed_map},
        )


class IdaMcpClient(BaseMcpClient):
    """IDA MCP adapter.

    Tool names differ between IDA MCP plugins. Constructor parameters let a
    caller map this generic interface to the concrete server's names.
    """

    def __init__(
        self,
        session: SyncToolSession | ToolCallable,
        *,
        open_tool: str = "open_function",
        callers_tool: str = "get_callers",
        callees_tool: str = "get_callees",
        decompile_tool: str = "decompile_function",
        strings_tool: str = "inspect_strings",
        rename_tool: str = "rename_function",
    ):
        super().__init__(session)
        self.open_tool = open_tool
        self.callers_tool = callers_tool
        self.callees_tool = callees_tool
        self.decompile_tool = decompile_tool
        self.strings_tool = strings_tool
        self.rename_tool = rename_tool

    def open_function(self, entry_point: str) -> ToolResult:
        return self.call_tool(self.open_tool, {"entry_point": entry_point})

    def get_callers(self, entry_point: str) -> ToolResult:
        return self.call_tool(self.callers_tool, {"entry_point": entry_point})

    def get_callees(self, entry_point: str) -> ToolResult:
        return self.call_tool(self.callees_tool, {"entry_point": entry_point})

    def decompile_function(self, entry_point: str) -> ToolResult:
        return self.call_tool(self.decompile_tool, {"entry_point": entry_point})

    def inspect_strings(self, entry_point: str) -> ToolResult:
        return self.call_tool(self.strings_tool, {"entry_point": entry_point})

    def rename_function(self, entry_point: str, new_name: str) -> ToolResult:
        return self.call_tool(self.rename_tool, {"entry_point": entry_point, "new_name": new_name})
