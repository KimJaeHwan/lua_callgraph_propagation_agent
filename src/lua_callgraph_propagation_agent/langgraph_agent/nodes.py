"""LangGraph node implementations for local LLM + MCP automation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clients import IdaMcpClient, LuaMcpClient
from .config import load_config, resolve_paths
from .confirmed import ConfirmedMapBuilder
from .reasoner import LocalLlmReasoner
from .state import (
    AgentState,
    CandidateContext,
    GraphConfig,
    IdaEvidence,
    RuntimePaths,
    ToolResult,
    VerificationDecision,
    merge_unique_dicts,
)


class LangGraphAgentNodes:
    def __init__(self, lua: LuaMcpClient, ida: IdaMcpClient | None = None, reasoner: LocalLlmReasoner | None = None):
        self.lua = lua
        self.ida = ida
        self.reasoner = reasoner or LocalLlmReasoner()

    def init_state(self, state: AgentState) -> AgentState:
        config_path = state["config_path"]
        config = load_config(config_path)
        runtime_paths = RuntimePaths.from_resolved_paths(resolve_paths(config))
        graph_config = GraphConfig(**state.get("graph_config", {}))
        return {
            **state,
            "paths": runtime_paths.to_dict(),
            "graph_config": graph_config.to_dict(),
            "max_rounds": graph_config.max_rounds,
            "phase": "initialized",
            "ida_available": self.ida is not None,
        }

    def run_extraction(self, state: AgentState) -> AgentState:
        config = load_config(state["config_path"])
        extraction = config.get("extraction", {})
        session_name = config.get("session_name", "runtime_session")
        result = self.lua.extract_query_features(
            binary=extraction.get("binary", ""),
            lua_version=extraction.get("lua_version", config.get("analysis", {}).get("lua_version", "Lua_547")),
            architecture=extraction.get("architecture", config.get("analysis", {}).get("architecture", "x86_64")),
            session_name=session_name,
            opt_level=extraction.get("opt_level", "O2"),
            strip_mode=extraction.get("strip_mode", "stripped"),
        )
        return self._record_tool_result(state, result, phase="extracted")

    def detect_scope(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        query_json = self._query_json_or_manifest_feature(paths)
        result = self.lua.detect_lua_scope(
            query_json=query_json,
            output_json=paths["lua_scope_json"],
        )
        return self._record_tool_result(state, result, phase="scope_detected")

    def run_bulk_retrieval(self, state: AgentState, *, patched: bool = False) -> AgentState:
        paths = state["paths"]
        config = load_config(state["config_path"])
        retrieval_cfg = config.get("analysis", {}).get("retrieval", {})
        query_json = paths.get("patched_query_json") if patched else self._query_json_or_manifest_feature(paths)
        result = self.lua.bulk_query_retrieval(
            query_json=query_json,
            index=paths["retrieval_index"],
            output_json=paths["retrieval_json"],
            candidate_pool=int(retrieval_cfg.get("candidate_pool", 200)),
            topk=int(retrieval_cfg.get("topk", 50)),
            scoring_mode=retrieval_cfg.get("scoring_mode", "bonus_v2"),
            scope_json=paths.get("lua_scope_json") or None,
        )
        return self._record_tool_result(state, result, phase="retrieval_done")

    def select_seed(self, state: AgentState, *, include_targeted: bool = False) -> AgentState:
        paths = state["paths"]
        config = load_config(state["config_path"])
        seed_cfg = config.get("analysis", {}).get("seed_anchors", {})
        kwargs: dict[str, Any] = {
            "retrieval_json": paths["retrieval_json"],
            "output_json": paths["seed_anchor_json"],
            "query_json": self._query_json_or_manifest_feature(paths),
            "reference_db": paths["reference_db"],
            "min_top1_score": float(seed_cfg.get("min_top1_score", 0.92)),
            "min_margin": float(seed_cfg.get("min_margin", 0.05)),
            "dedup_max_per_ref": int(seed_cfg.get("dedup_max_per_ref", 1)),
            "scope_json": paths.get("lua_scope_json") or None,
        }
        if include_targeted and paths.get("targeted_json"):
            kwargs["targeted_json"] = paths["targeted_json"]
        result = self.lua.select_seed_anchors(**kwargs)
        return self._record_tool_result(state, result, phase="seed_selected")

    def build_suite(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        config = load_config(state["config_path"])
        lua_version = config.get("analysis", {}).get("lua_version") or config.get("extraction", {}).get("lua_version") or "Lua_547"
        result = self.lua.build_runtime_suite(
            retrieval_json=paths["retrieval_json"],
            anchor_json=paths["seed_anchor_json"],
            output_json=paths["suite_json"],
            propagation_output_json=paths["propagation_json"],
            lua_version=lua_version,
            reference_db=paths["reference_db"],
        )
        return self._record_tool_result(state, result, phase="suite_built")

    def run_downstream(self, state: AgentState) -> AgentState:
        result = self.lua.run_downstream(state["config_path"])
        return self._record_tool_result(state, result, phase="downstream_done")

    def update_metrics(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        report = self.lua.read_final_report(paths["final_report_json"])
        summary = report.result.get("summary", {}) if report.ok else {}
        current = int(summary.get("accepted") or state.get("current_accepted") or 0)
        last = int(state.get("current_accepted") or 0)
        delta = current - last
        cfg = GraphConfig(**state.get("graph_config", {}))
        convergence = int(state.get("convergence_count") or 0)
        if delta < cfg.min_delta_accepted:
            convergence += 1
        else:
            convergence = 0
        updated = self._record_tool_result(state, report, phase="metrics_updated")
        updated.update({
            "last_accepted": last,
            "current_accepted": current,
            "delta_accepted": delta,
            "convergence_count": convergence,
            "round_index": int(state.get("round_index") or 0) + 1,
            "last_report_summary": summary,
        })
        return updated

    def analyze_distribution(self, state: AgentState) -> AgentState:
        cfg = GraphConfig(**state.get("graph_config", {}))
        result = self.lua.get_mapping_distribution(state["config_path"], cfg.suspicious_threshold)
        updated = self._record_tool_result(state, result, phase="distribution_analyzed")
        if result.ok:
            updated["last_distribution"] = result.result
        return updated

    def update_noise(self, state: AgentState) -> AgentState:
        cfg = GraphConfig(**state.get("graph_config", {}))
        current = set(state.get("noise_blacklist") or [])
        suspicious = state.get("last_distribution", {}).get("suspicious_names", [])
        to_add = [
            row["reference_name"] for row in suspicious
            if int(row.get("query_count") or 0) >= cfg.auto_blacklist_threshold
            and row.get("reference_name") not in current
        ]
        if not to_add:
            return {**state, "phase": "noise_noop"}
        result = self.lua.update_noise_blacklist(state["paths"]["suite_json"], add=to_add)
        updated = self._record_tool_result(state, result, phase="noise_updated")
        if result.ok:
            updated["noise_blacklist"] = result.result.get("current_blacklist", sorted(current | set(to_add)))
        return updated

    def export_trusted(self, state: AgentState) -> AgentState:
        result = self.lua.export_trusted_mappings(
            config_path=state["config_path"],
            max_count=1,
            exclude_prefixes="FUN_,sub_",
        )
        updated = self._record_tool_result(state, result, phase="trusted_exported")
        if result.ok:
            ranked = self.reasoner.rank_trusted(result.result.get("mappings", []), updated)
            updated["pending_trusted"] = ranked
        return updated

    def analyze_deferred(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        result = self.lua.list_deferred_cases(paths["final_report_json"])
        updated = self._record_tool_result(state, result, phase="deferred_analyzed")
        if result.ok:
            cases = result.result.get("cases") or result.result.get("deferred") or []
            updated["pending_deferred"] = self.reasoner.select_deferred_cases(cases, updated)
        return updated

    def plan_ida_verification(self, state: AgentState) -> AgentState:
        queue = merge_unique_dicts(state.get("pending_trusted", []), state.get("pending_deferred", []), "case_id")
        cfg = GraphConfig(**state.get("graph_config", {}))
        return {**state, "verification_queue": queue[: cfg.max_ida_cases_per_round], "phase": "verification_planned"}

    def collect_ida_evidence(self, state: AgentState) -> AgentState:
        if self.ida is None:
            return {**state, "ida_available": False, "phase": "ida_unavailable"}
        queue = state.get("verification_queue", [])
        if not queue:
            return {**state, "phase": "verification_queue_empty"}
        context = CandidateContext.from_mapping(queue[0])
        entry = context.entry_point
        errors: list[str] = []
        self.ida.open_function(entry)
        callers = self.ida.get_callers(entry)
        callees = self.ida.get_callees(entry)
        decomp = self.ida.decompile_function(entry)
        strings = self.ida.inspect_strings(entry)
        for res in (callers, callees, decomp, strings):
            if not res.ok:
                errors.append(f"{res.tool_name}: {res.error}")
        evidence = IdaEvidence(
            entry_point=entry,
            current_name=str(decomp.result.get("name") or ""),
            decompiled_code=str(decomp.result.get("code") or decomp.result.get("decompiled") or ""),
            callers=list(callers.result.get("callers") or []),
            callees=list(callees.result.get("callees") or []),
            strings=list(strings.result.get("strings") or []),
            constants=list(strings.result.get("constants") or []),
            errors=errors,
        )
        return {**state, "current_candidate_context": context.to_dict(), "current_ida_evidence": evidence.to_dict(), "phase": "ida_evidence_collected"}

    def llm_verify_candidate(self, state: AgentState) -> AgentState:
        context = CandidateContext(**state.get("current_candidate_context", {}))
        evidence = IdaEvidence(**state.get("current_ida_evidence", {"entry_point": context.entry_point}))
        cfg = GraphConfig(**state.get("graph_config", {}))
        decision = self.reasoner.verify_candidate(context, evidence, cfg, noise_blacklist=state.get("noise_blacklist", []))
        decisions = state.get("verified_decisions", []) + [decision.to_dict()]
        return {**state, "current_decision": decision.to_dict(), "verified_decisions": decisions, "phase": "candidate_verified"}

    def apply_ida_rename(self, state: AgentState) -> AgentState:
        if self.ida is None:
            return state
        decision = VerificationDecision(**state.get("current_decision", {}))
        if decision.accepted and decision.rename_in_ida:
            result = self.ida.rename_function(decision.entry_point, decision.candidate_name)
            return self._record_tool_result(state, result, phase="ida_renamed")
        return {**state, "phase": "ida_rename_skipped"}

    def build_confirmed_map(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        query_json = paths.get("query_json") or self._query_json_or_manifest_feature(paths)
        builder = ConfirmedMapBuilder(query_json)
        decisions = [VerificationDecision(**item) for item in state.get("verified_decisions", [])]
        new_map = builder.build(decisions)
        merged = {**state.get("confirmed_map", {}), **new_map}
        anchors = builder.to_force_anchors(decisions)
        return {**state, "confirmed_map": merged, "pending_force_anchors": anchors, "phase": "confirmed_map_built"}

    def register_force_anchors(self, state: AgentState) -> AgentState:
        anchors = state.get("pending_force_anchors", [])
        if not anchors:
            return {**state, "phase": "no_force_anchors"}
        result = self.lua.batch_register_force_anchors(state["config_path"], anchors)
        return self._record_tool_result(state, result, phase="force_anchors_registered")

    def patch_features(self, state: AgentState) -> AgentState:
        confirmed = state.get("confirmed_map", {})
        if not confirmed:
            return {**state, "phase": "patch_skipped"}
        query_json = self._query_json_or_manifest_feature(state["paths"])
        result = self.lua.patch_features_with_confirmed(query_json, confirmed)
        updated = self._record_tool_result(state, result, phase="features_patched")
        if result.ok:
            paths = dict(updated["paths"])
            paths["patched_query_json"] = result.result.get("patched_query_json", "")
            updated["paths"] = paths
        return updated

    def targeted_retrieval(self, state: AgentState) -> AgentState:
        paths = state["paths"]
        config = load_config(state["config_path"])
        lua_version = config.get("analysis", {}).get("lua_version") or config.get("extraction", {}).get("lua_version") or "Lua_547"
        result = self.lua.targeted_retrieval(
            query_json=paths.get("patched_query_json") or self._query_json_or_manifest_feature(paths),
            anchors_json=paths["seed_anchor_json"],
            reference_db=paths["reference_db"],
            output_json=paths["targeted_json"],
            lua_version=lua_version,
            min_vote_score=0.0,
            min_voters=1,
        )
        return self._record_tool_result(state, result, phase="targeted_done")

    def finalize(self, state: AgentState) -> AgentState:
        summary = state.get("last_report_summary", {})
        final = (
            f"LangGraph automation finished: accepted={summary.get('accepted')}, "
            f"deferred={summary.get('deferred')}, conflict={summary.get('conflict')}, "
            f"rounds={state.get('round_index')}, confirmed={len(state.get('confirmed_map', {}))}, "
            f"blacklist={len(state.get('noise_blacklist', []))}"
        )
        return {**state, "done": True, "phase": "done", "final_summary": final}

    def _record_tool_result(self, state: AgentState, result: ToolResult, *, phase: str) -> AgentState:
        failures = list(state.get("tool_failures", []))
        if not result.ok:
            failures.append(result.to_dict())
        return {**state, "phase": phase if result.ok else f"{phase}_failed", "tool_failures": failures}

    def _query_json_or_manifest_feature(self, paths: dict[str, Any]) -> str:
        query_json = paths.get("query_json")
        if query_json and Path(query_json).exists():
            return str(query_json)
        manifest = paths.get("extract_manifest")
        if manifest and Path(manifest).exists():
            try:
                data = json.loads(Path(manifest).read_text(encoding="utf-8"))
                feature_files = data.get("feature_files") or []
                if feature_files:
                    return str(feature_files[0])
            except Exception:
                pass
        return str(query_json or manifest or "")
