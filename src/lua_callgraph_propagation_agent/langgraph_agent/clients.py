"""Thin MCP client adapters used by the LangGraph agent nodes.

The adapters accept any object exposing a synchronous ``call_tool(name, args)``
method. This keeps the agent independent from a specific MCP transport while
still making every tool result explicit and testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .state import ToolResult


def _summarize_payload(payload: dict[str, Any], max_items: int = 4) -> str:
    if not payload:
        return "{}"
    parts: list[str] = []
    for idx, (key, value) in enumerate(payload.items()):
        if idx >= max_items:
            parts.append("...")
            break
        if isinstance(value, (str, int, float, bool)) or value is None:
            parts.append(f"{key}={value!r}")
        elif isinstance(value, list):
            parts.append(f"{key}=list[{len(value)}]")
        elif isinstance(value, dict):
            parts.append(f"{key}=dict[{len(value)}]")
        else:
            parts.append(f"{key}=<{type(value).__name__}>")
    return "{ " + ", ".join(parts) + " }"


class SyncToolSession(Protocol):
    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        ...


ToolCallable = Callable[[str, dict[str, Any]], dict[str, Any]]


class BaseMcpClient:
    def __init__(self, session: SyncToolSession | ToolCallable):
        self._session = session

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        payload = args or {}
        print(f"[mcp] call {name} {_summarize_payload(payload)}", flush=True)
        try:
            if callable(self._session) and not hasattr(self._session, "call_tool"):
                result = self._session(name, payload)
            else:
                result = self._session.call_tool(name, payload)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - transport-specific
            print(f"[mcp] fail {name}: {exc}", flush=True)
            return ToolResult.failure(name, str(exc), payload, retryable=True)

        if not isinstance(result, dict):
            print(f"[mcp] fail {name}: non-dict result {type(result)!r}", flush=True)
            return ToolResult.failure(name, f"tool returned non-dict result: {type(result)!r}", payload)
        if result.get("ok") is False:
            print(f"[mcp] fail {name}: {result.get('error') or result}", flush=True)
            return ToolResult.failure(name, str(result.get("error") or result), payload, retryable=False)
        print(f"[mcp] ok   {name}", flush=True)
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


def _first_payload_row(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("items", "results", "functions", "rows", "matches", "data"):
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _extract_lookup_function(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("result")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("fn"), dict):
                return row["fn"]
    row = _first_payload_row(payload)
    if isinstance(row.get("fn"), dict):
        return row["fn"]
    return {}


def _ida_addr(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return text
    if not text.startswith("0x"):
        text = f"0x{text}"
    return text


class CodexIdaMcpClient:
    """Concrete adapter for the currently used `ida_pro_mcp` tool profile.

    This adapter maps the generic LangGraph evidence needs onto the concrete
    tools exposed by the active IDA MCP server used in this project:

    - `lookup_funcs`
    - `xref_query`
    - `callees`
    - `decompile`
    - `analyze_function`
    - `rename`
    """

    def __init__(self, session: SyncToolSession | ToolCallable):
        self.base = BaseMcpClient(session)

    def open_function(self, entry_point: str) -> ToolResult:
        result = self.base.call_tool("lookup_funcs", {"queries": [_ida_addr(entry_point)]})
        if not result.ok:
            return result
        return ToolResult.success(
            "open_function",
            {"function": _extract_lookup_function(result.result)},
            {"entry_point": entry_point},
        )

    def get_callers(self, entry_point: str) -> ToolResult:
        args = {
            "queries": [{
                "addr": _ida_addr(entry_point),
                "direction": "to",
                "xref_type": "code",
                "include_fn": True,
                "count": 128,
            }],
        }
        result = self.base.call_tool("xref_query", args)
        if not result.ok:
            return result
        row = _first_payload_row(result.result)
        xrefs = row.get("xrefs") or result.result.get("xrefs") or []
        callers = [
            x.get("fn_name") or x.get("name") or x.get("addr")
            for x in xrefs
            if isinstance(x, dict)
        ]
        return ToolResult.success("get_callers", {"callers": callers, "xrefs": xrefs}, {"entry_point": entry_point})

    def get_callees(self, entry_point: str) -> ToolResult:
        result = self.base.call_tool("callees", {"addrs": [_ida_addr(entry_point)], "limit": 128})
        if not result.ok:
            return result
        row = _first_payload_row(result.result)
        callees = row.get("callees") or result.result.get("callees") or []
        names = [
            c.get("name") or c.get("func_name") or c.get("addr")
            for c in callees
            if isinstance(c, dict)
        ]
        return ToolResult.success("get_callees", {"callees": names, "rows": callees}, {"entry_point": entry_point})

    def decompile_function(self, entry_point: str) -> ToolResult:
        decomp = self.base.call_tool("decompile", {"addr": _ida_addr(entry_point), "include_addresses": False})
        if not decomp.ok:
            return decomp
        lookup = self.base.call_tool("lookup_funcs", {"queries": [_ida_addr(entry_point)]})
        name = ""
        if lookup.ok:
            name = str(_first_payload_row(lookup.result).get("name") or "")
        code = decomp.result.get("pseudocode") or decomp.result.get("code") or decomp.result.get("decompiled") or ""
        return ToolResult.success("decompile_function", {"name": name, "code": code}, {"entry_point": entry_point})

    def inspect_strings(self, entry_point: str) -> ToolResult:
        result = self.base.call_tool("analyze_function", {"addr": _ida_addr(entry_point), "include_asm": False})
        if not result.ok:
            return result
        strings = result.result.get("strings") or []
        constants = result.result.get("constants") or []
        callers = result.result.get("callers") or []
        callees = result.result.get("callees") or []
        decompiled = result.result.get("decompiled") or result.result.get("code") or ""
        current_name = str(result.result.get("name") or "")
        return ToolResult.success(
            "inspect_strings",
            {
                "strings": strings,
                "constants": constants,
                "callers": callers,
                "callees": callees,
                "decompiled": decompiled,
                "name": current_name,
            },
            {"entry_point": entry_point},
        )

    def rename_function(self, entry_point: str, new_name: str) -> ToolResult:
        result = self.base.call_tool(
            "rename",
            {
                "batch": {
                    "func": [{"addr": _ida_addr(entry_point), "name": new_name}],
                    "stop_on_error": True,
                },
            },
        )
        if not result.ok:
            return result
        return ToolResult.success("rename_function", result.result, {"entry_point": entry_point, "new_name": new_name})
