"""LangGraph wiring for the local LLM automation workflow."""

from __future__ import annotations

import importlib
from typing import Literal

from .nodes import LangGraphAgentNodes
from .state import AgentState, GraphConfig

Route = Literal[
    "run_extraction",
    "detect_scope",
    "run_bulk_retrieval",
    "select_seed",
    "build_suite",
    "run_downstream",
    "update_metrics",
    "analyze_distribution",
    "update_noise",
    "export_trusted",
    "analyze_deferred",
    "plan_ida_verification",
    "collect_ida_evidence",
    "llm_verify_candidate",
    "apply_ida_rename",
    "build_confirmed_map",
    "register_force_anchors",
    "patch_features",
    "fresh_bulk_retrieval",
    "targeted_retrieval",
    "reselect_seed_with_targeted",
    "finalize",
]


def route_after_init(state: AgentState) -> Route:
    paths = state.get("paths", {})
    query_json = paths.get("query_json")
    return "detect_scope" if query_json else "run_extraction"


def route_after_distribution(state: AgentState) -> Route:
    cfg = GraphConfig(**state.get("graph_config", {}))
    current = set(state.get("noise_blacklist") or [])
    suspicious = state.get("last_distribution", {}).get("suspicious_names", [])
    new_noise = [
        row.get("reference_name") for row in suspicious
        if int(row.get("query_count") or 0) >= cfg.auto_blacklist_threshold
        and row.get("reference_name") not in current
    ]
    return "update_noise" if new_noise else "export_trusted"


def route_after_trusted(state: AgentState) -> Route:
    return "plan_ida_verification" if state.get("pending_trusted") else "analyze_deferred"


def route_after_deferred(state: AgentState) -> Route:
    return "plan_ida_verification" if state.get("pending_deferred") else "finalize"


def route_after_verification(state: AgentState) -> Route:
    decision = state.get("current_decision", {})
    return "apply_ida_rename" if decision.get("accepted") else "analyze_deferred"


def route_after_rename(state: AgentState) -> Route:
    return "build_confirmed_map"


def route_after_patch(state: AgentState) -> Route:
    cfg = GraphConfig(**state.get("graph_config", {}))
    new_confirmed = len(state.get("confirmed_map") or {})
    if cfg.allow_fresh_retrieval and new_confirmed >= cfg.fresh_retrieval_anchor_delta:
        return "fresh_bulk_retrieval"
    return "targeted_retrieval"


def route_after_metrics(state: AgentState) -> Route:
    cfg = GraphConfig(**state.get("graph_config", {}))
    if int(state.get("round_index") or 0) >= cfg.max_rounds:
        return "finalize"
    if int(state.get("convergence_count") or 0) >= cfg.convergence_patience:
        return "finalize"
    if len(state.get("tool_failures") or []) >= cfg.max_tool_failures:
        return "finalize"
    return "analyze_distribution"


def build_graph(nodes: LangGraphAgentNodes):
    """Build and compile the LangGraph workflow.

    LangGraph is imported lazily so the package can be installed without the
    optional agent dependency when only the MCP server is needed.
    """
    try:
        langgraph_graph = importlib.import_module("langgraph.graph")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Install optional dependency with: pip install -e .[agent]") from exc

    END = langgraph_graph.END
    StateGraph = langgraph_graph.StateGraph

    builder = StateGraph(AgentState)
    builder.add_node("init_state", nodes.init_state)
    builder.add_node("run_extraction", nodes.run_extraction)
    builder.add_node("detect_scope", nodes.detect_scope)
    builder.add_node("run_bulk_retrieval", nodes.run_bulk_retrieval)
    builder.add_node("select_seed", nodes.select_seed)
    builder.add_node("build_suite", nodes.build_suite)
    builder.add_node("run_downstream", nodes.run_downstream)
    builder.add_node("update_metrics", nodes.update_metrics)
    builder.add_node("analyze_distribution", nodes.analyze_distribution)
    builder.add_node("update_noise", nodes.update_noise)
    builder.add_node("export_trusted", nodes.export_trusted)
    builder.add_node("analyze_deferred", nodes.analyze_deferred)
    builder.add_node("plan_ida_verification", nodes.plan_ida_verification)
    builder.add_node("collect_ida_evidence", nodes.collect_ida_evidence)
    builder.add_node("llm_verify_candidate", nodes.llm_verify_candidate)
    builder.add_node("apply_ida_rename", nodes.apply_ida_rename)
    builder.add_node("build_confirmed_map", nodes.build_confirmed_map)
    builder.add_node("register_force_anchors", nodes.register_force_anchors)
    builder.add_node("patch_features", nodes.patch_features)
    builder.add_node("fresh_bulk_retrieval", lambda state: nodes.run_bulk_retrieval(state, patched=True))
    builder.add_node("targeted_retrieval", nodes.targeted_retrieval)
    builder.add_node("reselect_seed_with_targeted", lambda state: nodes.select_seed(state, include_targeted=True))
    builder.add_node("finalize", nodes.finalize)

    builder.set_entry_point("init_state")
    builder.add_conditional_edges("init_state", route_after_init)
    builder.add_edge("run_extraction", "detect_scope")
    builder.add_edge("detect_scope", "run_bulk_retrieval")
    builder.add_edge("run_bulk_retrieval", "select_seed")
    builder.add_edge("select_seed", "build_suite")
    builder.add_edge("build_suite", "run_downstream")
    builder.add_edge("run_downstream", "update_metrics")
    builder.add_conditional_edges("update_metrics", route_after_metrics)
    builder.add_conditional_edges("analyze_distribution", route_after_distribution)
    builder.add_edge("update_noise", "run_downstream")
    builder.add_conditional_edges("export_trusted", route_after_trusted)
    builder.add_conditional_edges("analyze_deferred", route_after_deferred)
    builder.add_edge("plan_ida_verification", "collect_ida_evidence")
    builder.add_edge("collect_ida_evidence", "llm_verify_candidate")
    builder.add_conditional_edges("llm_verify_candidate", route_after_verification)
    builder.add_conditional_edges("apply_ida_rename", route_after_rename)
    builder.add_edge("build_confirmed_map", "register_force_anchors")
    builder.add_edge("register_force_anchors", "patch_features")
    builder.add_conditional_edges("patch_features", route_after_patch)
    builder.add_edge("fresh_bulk_retrieval", "targeted_retrieval")
    builder.add_edge("targeted_retrieval", "reselect_seed_with_targeted")
    builder.add_edge("reselect_seed_with_targeted", "run_downstream")
    builder.add_edge("finalize", END)
    return builder.compile()
