"""State and data models for the LangGraph local-LLM automation layer.

The models in this module are intentionally plain dataclasses / TypedDicts so
local runners can use them without requiring Pydantic or a specific LLM stack.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict


@dataclass(slots=True)
class RuntimePaths:
    query_json: str = ""
    extract_manifest: str = ""
    lua_scope_json: str = ""
    retrieval_json: str = ""
    seed_anchor_json: str = ""
    suite_json: str = ""
    propagation_json: str = ""
    deferred_json: str = ""
    final_report_json: str = ""
    patched_query_json: str = ""
    targeted_json: str = ""
    reference_db: str = ""
    retrieval_index: str = ""
    ida_type_root: str = ""
    ida_signature_db: str = ""
    vanilla_lua_source_root: str = ""
    manual_force_anchors_json: str = ""

    @classmethod
    def from_resolved_paths(cls, paths: dict[str, Any]) -> "RuntimePaths":
        result_dir = Path(paths.get("retrieval_output_json", "")).parent
        return cls(
            query_json=str(paths.get("query_feature_json") or ""),
            extract_manifest=str(paths.get("extract_manifest_json") or ""),
            lua_scope_json=str(result_dir / "lua_scope.json") if str(result_dir) != "." else "",
            retrieval_json=str(paths.get("retrieval_output_json") or ""),
            seed_anchor_json=str(paths.get("seed_anchor_json") or ""),
            suite_json=str(paths.get("runtime_suite_json") or ""),
            propagation_json=str(paths.get("propagation_output_json") or ""),
            deferred_json=str(paths.get("deferred_output_json") or ""),
            final_report_json=str(paths.get("final_report_json") or ""),
            patched_query_json="",
            targeted_json=str(result_dir / "targeted_retrieval.json") if str(result_dir) != "." else "",
            reference_db=str(paths.get("reference_db") or ""),
            retrieval_index=str(paths.get("retrieval_index") or ""),
            ida_type_root=str(paths.get("ida_type_root") or "data/inputs/ida_types"),
            ida_signature_db=str(
                paths.get("ida_signature_db") or "data/inputs/ida_types/lua_function_signatures.sqlite"
            ),
            vanilla_lua_source_root=str(
                paths.get("vanilla_lua_source_root") or "data/inputs/lua_source_vanilla"
            ),
            manual_force_anchors_json=str(
                paths.get("manual_force_anchors_json") or result_dir / "manual_force_anchors.json"
            ) if str(result_dir) != "." else str(paths.get("manual_force_anchors_json") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GraphConfig:
    max_rounds: int = 20
    convergence_patience: int = 3
    min_delta_accepted: int = 5
    suspicious_threshold: int = 5
    auto_blacklist_threshold: int = 10
    seed_min_top1_score: float = 0.92
    seed_min_margin: float = 0.05
    seed_dedup_max_per_ref: int = 1
    targeted_min_score: float = 0.75
    targeted_min_margin: float = 0.15
    trusted_min_score: float = 0.92
    decompile_min_score: float = 0.85
    deferred_min_score_relaxation: float = 0.05
    deferred_no_graph_min_score: float = 0.90
    max_ida_cases_per_round: int = 10
    fresh_retrieval_anchor_delta: int = 20
    allow_auto_rename: bool = True
    allow_fresh_retrieval: bool = True
    prefer_deferred_over_guess: bool = True
    max_tool_failures: int = 3
    rename_min_score: float = 0.92
    rename_relaxed_min_score: float = 0.90
    enable_ida_type_injection: bool = True
    ida_type_injection_mode: str = "vanilla_headers"
    safe_auto_rename_prefixes: list[str] = field(
        default_factory=lambda: ["luaD_", "luaZ_", "luaV_finish", "luaopen_"]
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    retry_count: int = 0

    @classmethod
    def success(cls, tool_name: str, result: dict[str, Any], args: dict[str, Any] | None = None) -> "ToolResult":
        return cls(ok=True, tool_name=tool_name, args=args or {}, result=result)

    @classmethod
    def failure(
        cls,
        tool_name: str,
        error: str,
        args: dict[str, Any] | None = None,
        *,
        retryable: bool = False,
    ) -> "ToolResult":
        return cls(ok=False, tool_name=tool_name, args=args or {}, error=error, retryable=retryable)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateContext:
    case_id: str
    query_func: str
    entry_point: str = ""
    predicted_name: str = ""
    final_score: float = 0.0
    score_margin_top1_top2: float = 0.0
    mapping_count: int = 0
    recommended_action: str = ""
    status_reasons: list[str] = field(default_factory=list)
    top_candidates: list[dict[str, Any]] = field(default_factory=list)
    graph_breakdown: dict[str, Any] = field(default_factory=dict)
    propagation_evidence: list[str] = field(default_factory=list)
    query_feature_summary: dict[str, Any] = field(default_factory=dict)
    registered_anchors_for_query: list[dict[str, Any]] = field(default_factory=list)
    reference_db: str = ""
    source_kind: str = "mapping"

    @staticmethod
    def _entry_point_from_case_id(case_id: str) -> str:
        if "@" not in case_id:
            return ""
        _, _, suffix = case_id.rpartition("@")
        return normalize_entry_point(suffix)

    @classmethod
    def from_context_bundle(cls, bundle: dict[str, Any]) -> "CandidateContext":
        triage = bundle.get("triage_case") or {}
        mapping = bundle.get("mapping_record") or {}
        top_candidates = list(triage.get("top_candidates") or mapping.get("top_candidates") or [])
        top = top_candidates[0] if top_candidates else {}
        case_id = str(bundle.get("case_id") or mapping.get("case_id") or "")
        entry_point = (
            str(mapping.get("entry_point") or "")
            or str(bundle.get("entry_point") or "")
            or cls._entry_point_from_case_id(case_id)
        )
        graph_breakdown = dict(top.get("graph_breakdown") or mapping.get("graph_breakdown") or {})
        status_reasons = list(triage.get("status_reasons") or mapping.get("status_reasons") or mapping.get("reasons") or [])
        return cls(
            case_id=case_id,
            query_func=str(bundle.get("query_func") or mapping.get("query_func") or mapping.get("query_function_name") or ""),
            entry_point=entry_point,
            predicted_name=str(
                triage.get("current_top_prediction")
                or mapping.get("predicted_name")
                or mapping.get("predicted_function_name")
                or top.get("reference_function_name")
                or top.get("function_name")
                or ""
            ),
            final_score=float(
                mapping.get("final_score")
                or top.get("final_score")
                or top.get("retrieval_prior")
                or 0.0
            ),
            score_margin_top1_top2=float(triage.get("score_margin_top1_top2") or 0.0),
            mapping_count=int(mapping.get("mapping_count") or 0),
            recommended_action=str(triage.get("recommended_action") or ""),
            status_reasons=status_reasons,
            top_candidates=top_candidates,
            graph_breakdown=graph_breakdown,
            propagation_evidence=list(mapping.get("evidence") or top.get("evidence") or []),
            query_feature_summary=dict(bundle.get("query_feature_summary") or {}),
            registered_anchors_for_query=list(bundle.get("registered_anchors_for_query") or []),
            reference_db=str(bundle.get("reference_db") or ""),
            source_kind="context_bundle",
        )

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "CandidateContext":
        top = (mapping.get("top_candidates") or [{}])[0]
        case_id = str(mapping.get("case_id") or "")
        entry_point = str(mapping.get("entry_point") or "") or cls._entry_point_from_case_id(case_id)
        return cls(
            case_id=case_id,
            query_func=str(mapping.get("query_func") or mapping.get("query_function_name") or ""),
            entry_point=entry_point,
            predicted_name=str(
                mapping.get("predicted_name")
                or mapping.get("predicted_function_name")
                or mapping.get("current_top_prediction")
                or top.get("reference_function_name")
                or top.get("function_name")
                or ""
            ),
            final_score=float(mapping.get("final_score") or top.get("final_score") or 0.0),
            score_margin_top1_top2=float(mapping.get("score_margin_top1_top2") or 0.0),
            mapping_count=int(mapping.get("mapping_count") or 0),
            recommended_action=str(mapping.get("recommended_action") or ""),
            status_reasons=list(mapping.get("status_reasons") or mapping.get("reasons") or []),
            top_candidates=list(mapping.get("top_candidates") or []),
            graph_breakdown=dict(mapping.get("graph_breakdown") or top.get("graph_breakdown") or {}),
            propagation_evidence=list(mapping.get("evidence") or top.get("evidence") or []),
            query_feature_summary=dict(mapping.get("query_feature_summary") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IdaEvidence:
    entry_point: str
    current_name: str = ""
    decompiled_code: str = ""
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    function_size: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VerificationDecision:
    case_id: str
    query_func: str
    entry_point: str
    candidate_name: str
    confidence: float
    accepted: bool
    rename_in_ida: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)

    @classmethod
    def rejected(cls, context: CandidateContext, reason: str, contradictions: list[str] | None = None) -> "VerificationDecision":
        return cls(
            case_id=context.case_id,
            query_func=context.query_func,
            entry_point=context.entry_point,
            candidate_name=context.predicted_name,
            confidence=0.0,
            accepted=False,
            reason=reason,
            contradictions=contradictions or [reason],
        )

    def to_force_anchor(self) -> dict[str, str]:
        return {
            "query_func": self.query_func,
            "reference_func": self.candidate_name,
            "reason": self.reason or "verified_by_langgraph_local_llm",
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentStateModel:
    config_path: str
    paths: RuntimePaths = field(default_factory=RuntimePaths)
    graph_config: GraphConfig = field(default_factory=GraphConfig)
    phase: str = "start"
    round_index: int = 0
    max_rounds: int = 20
    last_accepted: int = 0
    current_accepted: int = 0
    delta_accepted: int = 0
    convergence_count: int = 0
    confirmed_map: dict[str, str] = field(default_factory=dict)
    pending_trusted: list[dict[str, Any]] = field(default_factory=list)
    pending_deferred: list[dict[str, Any]] = field(default_factory=list)
    verification_queue: list[dict[str, Any]] = field(default_factory=list)
    verified_decisions: list[dict[str, Any]] = field(default_factory=list)
    current_candidate_context: dict[str, Any] = field(default_factory=dict)
    current_ida_evidence: dict[str, Any] = field(default_factory=dict)
    current_decision: dict[str, Any] = field(default_factory=dict)
    ida_resolution_cache: dict[str, str] = field(default_factory=dict)
    ida_seen_functions: list[str] = field(default_factory=list)
    ida_boundary_mismatch_functions: list[str] = field(default_factory=list)
    skipped_case_ids: list[str] = field(default_factory=list)
    reviewed_case_ids: list[str] = field(default_factory=list)
    noise_blacklist: list[str] = field(default_factory=list)
    last_report_summary: dict[str, Any] = field(default_factory=dict)
    last_propagation_summary: dict[str, Any] = field(default_factory=dict)
    last_distribution: dict[str, Any] = field(default_factory=dict)
    new_confirmed_count: int = 0
    tool_failures: list[dict[str, Any]] = field(default_factory=list)
    ida_available: bool = True
    done: bool = False
    final_summary: str = ""

    def to_dict(self) -> "AgentState":
        data = asdict(self)
        data["paths"] = self.paths.to_dict()
        data["graph_config"] = self.graph_config.to_dict()
        return data  # type: ignore[return-value]


class AgentState(TypedDict, total=False):
    config_path: str
    paths: dict[str, Any]
    graph_config: dict[str, Any]
    phase: str
    round_index: int
    max_rounds: int
    last_accepted: int
    current_accepted: int
    delta_accepted: int
    convergence_count: int
    confirmed_map: dict[str, str]
    pending_trusted: list[dict[str, Any]]
    pending_deferred: list[dict[str, Any]]
    verification_queue: list[dict[str, Any]]
    verified_decisions: list[dict[str, Any]]
    current_candidate_context: dict[str, Any]
    current_ida_evidence: dict[str, Any]
    current_decision: dict[str, Any]
    ida_resolution_cache: dict[str, str]
    ida_seen_functions: list[str]
    ida_boundary_mismatch_functions: list[str]
    skipped_case_ids: list[str]
    reviewed_case_ids: list[str]
    noise_blacklist: list[str]
    last_report_summary: dict[str, Any]
    last_propagation_summary: dict[str, Any]
    last_distribution: dict[str, Any]
    new_confirmed_count: int
    tool_failures: list[dict[str, Any]]
    ida_available: bool
    done: bool
    final_summary: str


def normalize_entry_point(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = text.lstrip("0")
    return text or "0"


def merge_unique_dicts(existing: list[dict[str, Any]], new_items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result = list(existing)
    seen = {str(item.get(key)) for item in result if item.get(key) is not None}
    for item in new_items:
        marker = str(item.get(key))
        if marker and marker not in seen:
            result.append(item)
            seen.add(marker)
    return result
