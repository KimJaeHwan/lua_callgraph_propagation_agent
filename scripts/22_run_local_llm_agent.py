#!/usr/bin/env python3
"""Run the local-LLM analyst loop without requiring LangGraph at runtime.

This script uses the same node implementations and routing policy as the
LangGraph design, but executes them with a lightweight manual orchestrator so
the workflow is immediately usable in environments where only FastMCP and the
project package are installed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from datetime import timedelta
from typing import Any
import urllib.error
import urllib.request

from fastmcp import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lua_callgraph_propagation_agent import mcp_server
from lua_callgraph_propagation_agent.langgraph_agent import (
    AgentStateModel,
    CodexIdaMcpClient,
    GraphConfig,
    LangGraphAgentNodes,
    LmStudioJsonModel,
    LocalLlmReasoner,
    LuaMcpClient,
)
from lua_callgraph_propagation_agent.langgraph_agent.graph import (
    route_after_deferred,
    route_after_distribution,
    route_after_init,
    route_after_metrics,
    route_after_patch,
    route_after_trusted,
    route_after_verification,
)
from lua_callgraph_propagation_agent.langgraph_agent.config import load_config, resolve_paths


class DirectLuaToolSession:
    """In-process dispatcher for the project's FastMCP tool functions."""

    def __init__(self):
        self._tool_map = {
            "extract_query_features": mcp_server.extract_query_features,
            "detect_lua_scope": mcp_server.detect_lua_scope,
            "bulk_query_retrieval": mcp_server.bulk_query_retrieval,
            "targeted_retrieval": mcp_server.targeted_retrieval,
            "select_seed_anchors": mcp_server.select_seed_anchors,
            "build_runtime_suite": mcp_server.build_runtime_suite,
            "run_downstream": mcp_server.run_downstream,
            "read_final_report": mcp_server.read_final_report,
            "read_propagation_summary": mcp_server.read_propagation_summary,
            "get_mapping_distribution": mcp_server.get_mapping_distribution,
            "list_deferred_cases": mcp_server.list_deferred_cases,
            "show_candidate_context": mcp_server.show_candidate_context,
            "export_trusted_mappings": mcp_server.export_trusted_mappings,
            "batch_register_force_anchors": mcp_server.batch_register_force_anchors,
            "update_noise_blacklist": mcp_server.update_noise_blacklist,
            "patch_features_with_confirmed": mcp_server.patch_features_with_confirmed,
        }

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = self._tool_map.get(name)
        if fn is None:
            raise KeyError(f"unknown Lua MCP tool: {name}")
        print(f"[lua-mcp] -> {name}", flush=True)
        return fn(**args)


class HttpMcpSession:
    """Simple synchronous wrapper around a FastMCP HTTP client."""

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
            if not raw:
                return {}
            return json.loads(raw)

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
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex-ida-client", "version": "0.1.0"},
            },
        }
        self._post_json(init_payload)
        initialized_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": None,
        }
        self._post_notification(initialized_payload)
        self._initialized = True

    def _call_once(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        print(f"[ida-mcp] -> {name}", flush=True)
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        result = self._post_json(payload)
        if "error" in result:
            error = str(result["error"])
            print(f"[ida-mcp] !! {name}: {error}", flush=True)
            return {"ok": False, "error": error}
        rpc_result = result.get("result") or {}
        if isinstance(rpc_result.get("structuredContent"), dict):
            payload = dict(rpc_result["structuredContent"])
        elif isinstance(rpc_result, dict):
            payload = dict(rpc_result)
        else:
            payload = {"content": rpc_result}
        payload.setdefault("ok", True)
        print(f"[ida-mcp] <- {name} ok", flush=True)
        return payload

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._call_once(name, args)
        except Exception as exc:
            # If the MCP server restarted, redo the handshake and retry once.
            self._initialized = False
            try:
                return self._call_once(name, args)
            except Exception:
                raise RuntimeError(f"Client failed to connect: {exc}") from exc

    def close(self) -> None:
        self._initialized = False


def build_state(config_path: str, args: argparse.Namespace) -> dict[str, Any]:
    graph_cfg = GraphConfig(
        max_rounds=args.max_rounds,
        convergence_patience=args.convergence_patience,
        min_delta_accepted=args.min_delta_accepted,
        suspicious_threshold=args.suspicious_threshold,
        auto_blacklist_threshold=args.auto_blacklist_threshold,
        trusted_min_score=args.trusted_min_score,
        decompile_min_score=args.decompile_min_score,
        max_ida_cases_per_round=args.max_ida_cases_per_round,
        fresh_retrieval_anchor_delta=args.fresh_retrieval_anchor_delta,
        allow_auto_rename=not args.no_auto_rename,
        allow_fresh_retrieval=not args.no_fresh_retrieval,
        prefer_deferred_over_guess=not args.allow_guessy_accept,
        max_tool_failures=args.max_tool_failures,
    )
    return AgentStateModel(
        config_path=config_path,
        graph_config=graph_cfg,
    ).to_dict()


def validate_preextracted_inputs(config_path: str, *, allow_extraction: bool) -> None:
    config = load_config(config_path)
    paths = resolve_paths(config)
    query_json = str(paths.get("query_feature_json") or "")
    if query_json and Path(query_json).exists():
        return
    if allow_extraction:
        return
    raise RuntimeError(
        "Pre-extracted query feature JSON is required for this runner in no-extraction mode. "
        "Set paths.query_feature_json in the runtime config, or explicitly pass --allow-extraction "
        "if you really want the runner to invoke Ghidra extraction."
    )


def _log_route(route: str, state: dict[str, Any]) -> None:
    round_index = int(state.get("round_index") or 0)
    phase = state.get("phase") or "unknown"
    summary = state.get("last_report_summary") or {}
    accepted = summary.get("accepted", "-")
    deferred = summary.get("deferred", "-")
    conflict = summary.get("conflict", "-")
    print(
        f"[agent] round={round_index} route={route} phase={phase} "
        f"accepted={accepted} deferred={deferred} conflict={conflict}",
        flush=True,
    )


def _log_state_after(route: str, state: dict[str, Any]) -> None:
    phase = state.get("phase") or "unknown"
    tool_failures = len(state.get("tool_failures") or [])
    pending_trusted = len(state.get("pending_trusted") or [])
    pending_deferred = len(state.get("pending_deferred") or [])
    verification_queue = len(state.get("verification_queue") or [])
    confirmed = len(state.get("confirmed_map") or {})
    print(
        f"[agent] done route={route} -> phase={phase} "
        f"trusted={pending_trusted} deferred={pending_deferred} "
        f"verify_q={verification_queue} confirmed={confirmed} "
        f"tool_failures={tool_failures}",
        flush=True,
    )


def run_manual_orchestrator(nodes: LangGraphAgentNodes, state: dict[str, Any]) -> dict[str, Any]:
    state = nodes.init_state(state)
    route = route_after_init(state)
    if route == "run_extraction" and not state.get("allow_extraction", False):
        raise RuntimeError(
            "Runner resolved to run_extraction, but extraction is disabled. "
            "Provide a pre-extracted query_feature_json or rerun with --allow-extraction."
        )

    while not state.get("done"):
        _log_route(route, state)
        if route == "run_extraction":
            state = nodes.run_extraction(state)
            _log_state_after(route, state)
            route = "detect_scope"
        elif route == "detect_scope":
            state = nodes.detect_scope(state)
            _log_state_after(route, state)
            route = "run_bulk_retrieval"
        elif route == "run_bulk_retrieval":
            state = nodes.run_bulk_retrieval(state)
            _log_state_after(route, state)
            route = "select_seed"
        elif route == "select_seed":
            state = nodes.select_seed(state)
            _log_state_after(route, state)
            route = "build_suite"
        elif route == "build_suite":
            state = nodes.build_suite(state)
            _log_state_after(route, state)
            route = "run_downstream"
        elif route == "run_downstream":
            state = nodes.run_downstream(state)
            _log_state_after(route, state)
            route = "update_metrics"
        elif route == "update_metrics":
            state = nodes.update_metrics(state)
            _log_state_after(route, state)
            route = route_after_metrics(state)
        elif route == "analyze_distribution":
            state = nodes.analyze_distribution(state)
            _log_state_after(route, state)
            route = route_after_distribution(state)
        elif route == "update_noise":
            state = nodes.update_noise(state)
            _log_state_after(route, state)
            route = "run_downstream"
        elif route == "export_trusted":
            state = nodes.export_trusted(state)
            _log_state_after(route, state)
            route = route_after_trusted(state)
        elif route == "analyze_deferred":
            state = nodes.analyze_deferred(state)
            _log_state_after(route, state)
            route = route_after_deferred(state)
        elif route == "plan_ida_verification":
            state = nodes.plan_ida_verification(state)
            _log_state_after(route, state)
            route = "collect_ida_evidence"
        elif route == "collect_ida_evidence":
            state = nodes.collect_ida_evidence(state)
            _log_state_after(route, state)
            route = "llm_verify_candidate"
        elif route == "llm_verify_candidate":
            state = nodes.llm_verify_candidate(state)
            _log_state_after(route, state)
            route = route_after_verification(state)
        elif route == "apply_ida_rename":
            state = nodes.apply_ida_rename(state)
            _log_state_after(route, state)
            route = "build_confirmed_map"
        elif route == "build_confirmed_map":
            state = nodes.build_confirmed_map(state)
            _log_state_after(route, state)
            route = "register_force_anchors"
        elif route == "register_force_anchors":
            state = nodes.register_force_anchors(state)
            _log_state_after(route, state)
            route = "patch_features"
        elif route == "patch_features":
            state = nodes.patch_features(state)
            _log_state_after(route, state)
            route = route_after_patch(state)
        elif route == "fresh_bulk_retrieval":
            state = nodes.run_bulk_retrieval(state, patched=True)
            _log_state_after(route, state)
            route = "targeted_retrieval"
        elif route == "targeted_retrieval":
            state = nodes.targeted_retrieval(state)
            _log_state_after(route, state)
            route = "reselect_seed_with_targeted"
        elif route == "reselect_seed_with_targeted":
            state = nodes.select_seed(state, include_targeted=True)
            _log_state_after(route, state)
            route = "run_downstream"
        elif route == "finalize":
            state = nodes.finalize(state)
            _log_state_after(route, state)
            break
        else:
            raise RuntimeError(f"unsupported route: {route}")
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local LLM analyst loop for Lua mapping.")
    parser.add_argument("--config", required=True, help="Runtime config JSON path.")
    parser.add_argument("--lmstudio-model", default="", help="LM Studio model id. Leave empty for deterministic fallback.")
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1", help="LM Studio OpenAI-compatible base URL.")
    parser.add_argument("--lmstudio-api-key", default="lm-studio", help="API key placeholder for LM Studio.")
    parser.add_argument("--lmstudio-temperature", type=float, default=0.0, help="LM Studio generation temperature.")
    parser.add_argument("--ida-url", default="", help="IDA MCP streamable HTTP URL. Example: http://127.0.0.1:13337/mcp")
    parser.add_argument("--no-ida", action="store_true", help="Disable IDA verification and use context-only fallback.")
    parser.add_argument("--no-auto-rename", action="store_true", help="Disable automatic IDA rename even after positive verification.")
    parser.add_argument("--allow-extraction", action="store_true", help="Allow this runner to invoke Ghidra feature extraction. Default is off to keep extraction as a separate phase.")
    parser.add_argument("--allow-guessy-accept", action="store_true", help="Loosen the conservative decision policy.")
    parser.add_argument("--no-fresh-retrieval", action="store_true", help="Disable patched-feature bulk retrieval refresh.")
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--convergence-patience", type=int, default=3)
    parser.add_argument("--min-delta-accepted", type=int, default=5)
    parser.add_argument("--suspicious-threshold", type=int, default=5)
    parser.add_argument("--auto-blacklist-threshold", type=int, default=10)
    parser.add_argument("--trusted-min-score", type=float, default=0.92)
    parser.add_argument("--decompile-min-score", type=float, default=0.85)
    parser.add_argument("--max-ida-cases-per-round", type=int, default=10)
    parser.add_argument("--fresh-retrieval-anchor-delta", type=int, default=20)
    parser.add_argument("--max-tool-failures", type=int, default=3)
    parser.add_argument("--write-summary-json", default="", help="Optional path to save the final state as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = str(Path(args.config).resolve())
    validate_preextracted_inputs(config_path, allow_extraction=args.allow_extraction)

    lua_client = LuaMcpClient(DirectLuaToolSession())

    ida_client = None
    ida_session = None
    if not args.no_ida and args.ida_url:
        ida_session = HttpMcpSession(args.ida_url)
        ida_client = CodexIdaMcpClient(ida_session)

    model = None
    if args.lmstudio_model:
        model = LmStudioJsonModel(
            base_url=args.lmstudio_base_url,
            model=args.lmstudio_model,
            api_key=args.lmstudio_api_key,
            temperature=args.lmstudio_temperature,
        )

    reasoner = LocalLlmReasoner(model=model)
    nodes = LangGraphAgentNodes(lua=lua_client, ida=ida_client, reasoner=reasoner)
    state = build_state(config_path, args)
    state["allow_extraction"] = bool(args.allow_extraction)
    try:
        result = run_manual_orchestrator(nodes, state)
    finally:
        if ida_session is not None:
            ida_session.close()

    summary = {
        "phase": result.get("phase"),
        "final_summary": result.get("final_summary"),
        "round_index": result.get("round_index"),
        "accepted": (result.get("last_report_summary") or {}).get("accepted"),
        "deferred": (result.get("last_report_summary") or {}).get("deferred"),
        "conflict": (result.get("last_report_summary") or {}).get("conflict"),
        "confirmed": len(result.get("confirmed_map") or {}),
        "blacklist": len(result.get("noise_blacklist") or []),
        "tool_failures": len(result.get("tool_failures") or []),
        "ida_enabled": ida_client is not None,
        "lmstudio_enabled": bool(model),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.write_summary_json:
        Path(args.write_summary_json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
