#!/usr/bin/env python3
"""Apply persistent manual force anchors, optionally rename in IDA, and rerun downstream.

Deprecated operationally: prefer re-running 22_run_local_llm_agent.py, which
now auto-resumes and auto-applies manual force anchors.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lua_callgraph_propagation_agent.langgraph_agent import CodexIdaMcpClient
from lua_callgraph_propagation_agent.langgraph_agent.config import load_config, resolve_paths
from lua_callgraph_propagation_agent.langgraph_agent.manual_force_anchors import (
    apply_manual_force_anchors,
    apply_manual_force_anchor_ida_updates,
)
from lua_callgraph_propagation_agent import mcp_server


def _resolve_existing_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    for root in (Path.cwd(), PROJECT_ROOT, PROJECT_ROOT.parent):
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return (Path.cwd() / candidate).resolve()


class HttpMcpSession:
    """Simple synchronous JSON-RPC wrapper for FastMCP HTTP servers."""

    def __init__(self, url: str, *, timeout_seconds: float = 60.0):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._initialized = False
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}

    def _post_notification(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds):
            return

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._post_json(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "manual-force-anchor-sync", "version": "0.1.0"},
                },
            }
        )
        self._post_notification(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": None}
        )
        self._initialized = True

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        result = self._post_json(payload)
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        rpc_result = result.get("result") or {}
        if isinstance(rpc_result.get("structuredContent"), dict):
            payload = dict(rpc_result["structuredContent"])
        elif isinstance(rpc_result, dict):
            payload = dict(rpc_result)
        else:
            payload = {"content": rpc_result}
        payload.setdefault("ok", True)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply manual force anchors into seed_anchors.json")
    parser.add_argument("--config", required=True, help="runtime config JSON")
    parser.add_argument("--ida-url", default="http://127.0.0.1:13337/mcp", help="IDA MCP HTTP URL")
    parser.add_argument(
        "--run-downstream",
        action="store_true",
        help="rerun build_suite -> propagation -> deferred_analysis -> final_report after applying anchors",
    )
    parser.add_argument(
        "--skip-ida-rename",
        action="store_true",
        help="do not rename/type confirmed manual anchors in IDA",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    paths = resolve_paths(config)
    seed_anchor_json = _resolve_existing_path(str(paths["seed_anchor_json"]))
    query_json = _resolve_existing_path(str(paths["query_feature_json"]))
    manual_force_anchors_json = _resolve_existing_path(
        str(paths.get("manual_force_anchors_json") or seed_anchor_json.parent / "manual_force_anchors.json")
    )

    result = apply_manual_force_anchors(
        seed_anchor_json=seed_anchor_json,
        query_json=query_json,
        manual_force_anchors_json=manual_force_anchors_json,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("errors"):
        return 1

    ida_result: dict[str, Any] = {"renamed": [], "skipped": [], "errors": []}
    if not args.skip_ida_rename:
        graph_cfg = dict(config.get("graph_config") or config.get("agent", {}).get("graph_config") or {})
        ida_result = apply_manual_force_anchor_ida_updates(
            ida=CodexIdaMcpClient(HttpMcpSession(args.ida_url)),
            manual_force_anchors_json=manual_force_anchors_json,
            lua_version=str(
                paths.get("target_lua_version")
                or paths.get("lua_version")
                or config.get("analysis", {}).get("lua_version")
                or config.get("extraction", {}).get("lua_version")
                or "Lua_547"
            ),
            ida_type_root=str(paths.get("ida_type_root") or ""),
            ida_signature_db=str(paths.get("ida_signature_db") or ""),
            vanilla_source_root=str(paths.get("vanilla_lua_source_root") or ""),
            type_mode=str(graph_cfg.get("ida_type_injection_mode") or "vanilla_headers"),
            enable_type_injection=bool(graph_cfg.get("enable_ida_type_injection", True)),
        )
        print(json.dumps({"ida_manual_anchor_renames": ida_result}, ensure_ascii=False, indent=2))
        if ida_result.get("errors"):
            return 1

    if args.run_downstream:
        downstream = mcp_server.run_downstream(args.config)
        print(json.dumps(downstream, ensure_ascii=False, indent=2))
        return 0 if downstream.get("ok") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
