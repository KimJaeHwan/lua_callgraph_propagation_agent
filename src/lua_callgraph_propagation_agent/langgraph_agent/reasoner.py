"""Local LLM reasoner contract and deterministic fallback implementation."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .state import CandidateContext, GraphConfig, IdaEvidence, VerificationDecision


class JsonLocalModel(Protocol):
    def invoke_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        ...


VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["case_id", "query_func", "entry_point", "candidate_name", "confidence", "accepted", "reason"],
    "properties": {
        "case_id": {"type": "string"},
        "query_func": {"type": "string"},
        "entry_point": {"type": "string"},
        "candidate_name": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "accepted": {"type": "boolean"},
        "rename_in_ida": {"type": "boolean"},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
    },
}


class LocalLlmReasoner:
    def __init__(self, model: JsonLocalModel | None = None):
        self.model = model

    def rank_trusted(self, mappings: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = GraphConfig(**state.get("graph_config", {}))
        confirmed_eps = set((state.get("confirmed_map") or {}).keys())
        filtered = [
            m for m in mappings
            if float(m.get("final_score") or 0.0) >= cfg.trusted_min_score
            and str(m.get("entry_point") or "").lower().lstrip("0x").lstrip("0") not in confirmed_eps
            and m.get("entry_point")
        ]
        return sorted(filtered, key=lambda item: (-float(item.get("final_score") or 0.0), str(item.get("query_func") or "")))

    def select_deferred_cases(self, cases: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = GraphConfig(**state.get("graph_config", {}))
        selected = []
        for case in cases:
            top_candidates = list(case.get("top_candidates") or [])
            top = top_candidates[0] if top_candidates else {}
            score = float(
                case.get("final_score")
                or case.get("score")
                or top.get("final_score")
                or top.get("retrieval_prior")
                or 0.0
            )
            if score and score < cfg.decompile_min_score:
                continue
            predicted = (
                case.get("predicted")
                or case.get("predicted_name")
                or case.get("predicted_function_name")
                or case.get("current_top_prediction")
                or top.get("reference_function_name")
                or top.get("function_name")
            )
            if not predicted:
                continue
            margin = float(case.get("score_margin_top1_top2") or 0.0)
            anchor_counts = case.get("anchor_counts") or {}
            total_anchor_edges = int(anchor_counts.get("total") or 0)
            if margin == 0.0 and total_anchor_edges == 0 and score < max(cfg.decompile_min_score, 0.90):
                continue
            selected.append(case)
        selected.sort(
            key=lambda item: (
                -float(
                    item.get("final_score")
                    or item.get("score")
                    or ((item.get("top_candidates") or [{}])[0].get("final_score") or 0.0)
                ),
                -int((item.get("anchor_counts") or {}).get("total") or 0),
                -float(item.get("score_margin_top1_top2") or 0.0),
            )
        )
        return selected[: cfg.max_ida_cases_per_round]

    def verify_candidate(
        self,
        context: CandidateContext,
        evidence: IdaEvidence,
        graph_config: GraphConfig,
        *,
        noise_blacklist: list[str] | None = None,
    ) -> VerificationDecision:
        if self.model is not None:
            return self._verify_with_model(context, evidence, graph_config, noise_blacklist or [])
        return self._deterministic_verify(context, evidence, graph_config, noise_blacklist or [])

    def _verify_with_model(
        self,
        context: CandidateContext,
        evidence: IdaEvidence,
        graph_config: GraphConfig,
        noise_blacklist: list[str],
    ) -> VerificationDecision:
        prompt = build_verification_prompt(context, evidence, graph_config, noise_blacklist)
        raw = self.model.invoke_json(prompt, VERIFICATION_SCHEMA)
        return coerce_decision(raw, context, graph_config)

    def _deterministic_verify(
        self,
        context: CandidateContext,
        evidence: IdaEvidence,
        graph_config: GraphConfig,
        noise_blacklist: list[str],
    ) -> VerificationDecision:
        contradictions: list[str] = []
        support: list[str] = []
        candidate = context.predicted_name

        if not candidate:
            return VerificationDecision.rejected(context, "missing candidate name")
        if candidate in set(noise_blacklist):
            return VerificationDecision.rejected(context, "candidate is noise-blacklisted", ["noise_blacklist"])
        if context.mapping_count and context.mapping_count > 1:
            contradictions.append(f"mapping_count={context.mapping_count}")
        if context.recommended_action and "manual" in context.recommended_action.lower():
            contradictions.append(f"recommended_action={context.recommended_action}")
        if context.final_score >= graph_config.trusted_min_score:
            support.append(f"final_score={context.final_score:.4f}")
        if context.score_margin_top1_top2 > 0:
            support.append(f"margin={context.score_margin_top1_top2:.4f}")
        if context.graph_breakdown:
            total_edges = int(context.graph_breakdown.get("total_anchor_edges") or 0)
            primary = int(context.graph_breakdown.get("primary_matches") or 0)
            if total_edges > 0 and primary > 0:
                support.append(f"graph primary_matches={primary}/{total_edges}")
        if context.registered_anchors_for_query:
            support.append(f"registered_anchors={len(context.registered_anchors_for_query)}")
        if evidence.decompiled_code and candidate.lower().replace("lua", "")[:4] in evidence.decompiled_code.lower():
            support.append("candidate-like token appears in decompile")
        if evidence.callers or evidence.callees:
            support.append("IDA caller/callee evidence collected")
        if evidence.strings:
            support.append("IDA string evidence collected")
        if evidence.errors:
            contradictions.extend(evidence.errors)

        accepted = len(support) >= 2 and not contradictions
        confidence = min(0.99, max(context.final_score, 0.70 + 0.08 * len(support))) if accepted else 0.0
        return VerificationDecision(
            case_id=context.case_id,
            query_func=context.query_func,
            entry_point=context.entry_point,
            candidate_name=candidate,
            confidence=confidence,
            accepted=accepted,
            rename_in_ida=bool(accepted and graph_config.allow_auto_rename and confidence >= 0.92),
            reason="; ".join(support) if accepted else "not enough non-contradictory evidence",
            evidence=support,
            contradictions=contradictions,
        )

    def should_blacklist(self, distribution_item: dict[str, Any], state: dict[str, Any]) -> bool:
        cfg = GraphConfig(**state.get("graph_config", {}))
        name = distribution_item.get("reference_name")
        return bool(name) and int(distribution_item.get("query_count") or 0) >= cfg.auto_blacklist_threshold

    def needs_fresh_retrieval(self, state: dict[str, Any], new_confirmed_count: int) -> bool:
        cfg = GraphConfig(**state.get("graph_config", {}))
        return bool(cfg.allow_fresh_retrieval and new_confirmed_count >= cfg.fresh_retrieval_anchor_delta)

    def should_stop(self, state: dict[str, Any]) -> bool:
        cfg = GraphConfig(**state.get("graph_config", {}))
        if int(state.get("round_index") or 0) >= cfg.max_rounds:
            return True
        if int(state.get("convergence_count") or 0) >= cfg.convergence_patience:
            return True
        if len(state.get("tool_failures") or []) >= cfg.max_tool_failures:
            return True
        if not state.get("pending_trusted") and not state.get("pending_deferred") and int(state.get("delta_accepted") or 0) < cfg.min_delta_accepted:
            return True
        return False


def build_verification_prompt(
    context: CandidateContext,
    evidence: IdaEvidence,
    graph_config: GraphConfig,
    noise_blacklist: list[str],
) -> str:
    payload = {
        "task": "Verify whether a stripped query function should be force-anchored to the candidate Lua reference name.",
        "policy": {
            "prefer_deferred_over_guess": graph_config.prefer_deferred_over_guess,
            "minimum_confidence_for_accept": graph_config.trusted_min_score,
            "never_accept_if_blacklisted": True,
            "require_structured_json_only": True,
        },
        "candidate_context": context.to_dict(),
        "ida_evidence": evidence.to_dict(),
        "noise_blacklist": noise_blacklist,
        "output_schema": VERIFICATION_SCHEMA,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def coerce_decision(raw: dict[str, Any], context: CandidateContext, graph_config: GraphConfig) -> VerificationDecision:
    candidate_name = str(raw.get("candidate_name") or context.predicted_name)
    confidence = float(raw.get("confidence") or 0.0)
    accepted = bool(raw.get("accepted")) and bool(candidate_name) and confidence >= graph_config.decompile_min_score
    contradictions = [str(x) for x in raw.get("contradictions") or []]
    if contradictions:
        accepted = False
    return VerificationDecision(
        case_id=str(raw.get("case_id") or context.case_id),
        query_func=str(raw.get("query_func") or context.query_func),
        entry_point=str(raw.get("entry_point") or context.entry_point),
        candidate_name=candidate_name,
        confidence=confidence if accepted else 0.0,
        accepted=accepted,
        rename_in_ida=bool(raw.get("rename_in_ida")) and accepted and graph_config.allow_auto_rename,
        reason=str(raw.get("reason") or ""),
        evidence=[str(x) for x in raw.get("evidence") or []],
        contradictions=contradictions,
    )
