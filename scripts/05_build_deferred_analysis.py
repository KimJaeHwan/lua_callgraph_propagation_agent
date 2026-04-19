#!/usr/bin/env python3
"""
Build compact analysis records for deferred propagation cases.

The propagation runner intentionally refuses to accept mappings when graph
evidence is weak, tied, or conflicted. This script turns those deferred rows
into reviewable analyst tasks.

Typical command from the project root:

  python3 scripts/05_build_deferred_analysis.py \
    --input-json data/eval/results/anchor_propagation_lua547_summary.json \
    --embedding-root ../lua_function_embedding \
    --output-json data/eval/results/representative/deferred_analysis_lua547.json

The output is deliberately compact enough to keep in Git. It can also be used
as the deterministic input payload for a later Local LLM analyst layer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("data/eval/results/anchor_propagation_lua547_summary.json")
DEFAULT_OUTPUT = Path("data/eval/results/representative/deferred_analysis_lua547.json")
DEFAULT_EMBEDDING_ROOT = Path("../lua_function_embedding")


REASON_DESCRIPTIONS = {
    "low_score_margin": "Top candidates are too close to safely accept one mapping.",
    "multiple_candidates_same_final_score": "Multiple candidates share the same final score.",
    "no_anchor_evidence": "No accepted caller/callee anchor supports this mapping.",
    "insufficient_primary_graph_matches": "The best candidate lacks primary O0 graph evidence.",
    "duplicate_accepted_mapping_in_query_scope": "More than one query function maps to the same reference function.",
    "no_candidates": "No retrieval or graph-expanded candidates are available.",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build compact deferred-case analysis records."
    )
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--embedding-root",
        type=Path,
        default=DEFAULT_EMBEDDING_ROOT,
        help="lua_function_embedding project root used to resolve query feature files",
    )
    parser.add_argument(
        "--top-candidates",
        type=int,
        default=5,
        help="number of top candidates to preserve per deferred case",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(path: str | Path, *, base: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (base / p).resolve()


def top_items(mapping: dict, *, limit: int) -> list[dict]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(mapping.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def summarize_names(names: list[str], *, limit: int = 12) -> dict:
    visible = [name for name in names if name and not name.startswith(("FUNC_", "FUN_", "sub_"))]
    anonymous = len(names) - len(visible)
    return {
        "total_count": len(names),
        "visible_count": len(visible),
        "anonymous_count": anonymous,
        "visible_names": visible[:limit],
    }


def summarize_compare(compare: dict, *, limit: int = 8) -> dict:
    flattened = []
    for offset, values in compare.items():
        for value in values[:limit]:
            flattened.append({"offset": offset, "value": value})
            if len(flattened) >= limit:
                return {"offset_count": len(compare), "sample": flattened}
    return {"offset_count": len(compare), "sample": flattened}


def feature_tags(row: dict) -> list[str]:
    tags = []
    strings = row.get("strings") or []
    callees = row.get("callees") or []
    pcode_count = int(row.get("pcode_instruction_count") or 0)
    bb_count = int(row.get("basic_block_count") or 0)
    if strings:
        tags.append("string_rich")
    if any("chunk" in s or "lua_integer" in s or "lua_number" in s for s in strings):
        tags.append("binary_chunk_or_loader_related_strings")
    if pcode_count >= 500 or bb_count >= 40:
        tags.append("large_complex_function")
    if pcode_count <= 120 and bb_count <= 5:
        tags.append("small_api_wrapper_like_function")
    if len(callees) >= 8:
        tags.append("call_heavy")
    if not strings:
        tags.append("no_strings")
    if summarize_names(row.get("callers") or [])["visible_count"] <= 1:
        tags.append("few_visible_callers")
    return tags


def summarize_feature(row: dict | None) -> dict | None:
    if row is None:
        return None
    histogram = row.get("pcode_opcode_histogram") or {}
    return {
        "function_name_debug_label": row.get("function_name"),
        "architecture": row.get("architecture"),
        "lua_version": row.get("lua_version"),
        "basic_block_count": row.get("basic_block_count"),
        "pcode_instruction_count": row.get("pcode_instruction_count"),
        "top_pcode_opcodes": top_items(histogram, limit=10),
        "strings": (row.get("strings") or [])[:20],
        "string_count": len(row.get("strings") or []),
        "callees": summarize_names(row.get("callees") or []),
        "callers": summarize_names(row.get("callers") or []),
        "struct_offsets": (row.get("struct_offsets") or [])[:20],
        "struct_offset_count": len(row.get("struct_offsets") or []),
        "compare_constants": summarize_compare(row.get("compare") or {}),
        "feature_tags": feature_tags(row),
    }


def load_query_features(embedding_root: Path, rows: list[dict]) -> dict[str, dict]:
    cache: dict[Path, list[dict]] = {}
    features: dict[str, dict] = {}
    for row in rows:
        query_file = row.get("query_file")
        query_func = row.get("query_func")
        if not query_file or not query_func:
            continue
        path = resolve_path(query_file, base=embedding_root)
        if path not in cache:
            cache[path] = load_json(path)
        query_row = next(
            (item for item in cache[path] if item.get("function_name") == query_func),
            None,
        )
        if query_row is not None:
            features[row["case_id"]] = query_row
    return features


def candidate_role_hint(name: str | None) -> dict:
    if not name:
        return {"role": "unknown", "rationale": "missing candidate name"}
    if name.startswith("luaL_"):
        return {"role": "Lua auxiliary library API/helper", "rationale": "luaL_ prefix"}
    if name.startswith("lua_"):
        return {"role": "Lua public C API", "rationale": "lua_ prefix"}
    if name.startswith("luaV_"):
        return {"role": "Lua VM execution/runtime", "rationale": "luaV_ prefix"}
    if name.startswith("luaD_"):
        return {"role": "Lua call/stack/error runtime", "rationale": "luaD_ prefix"}
    if name.startswith("luaU_"):
        return {"role": "Lua binary chunk undump/load support", "rationale": "luaU_ prefix"}
    if name.startswith("luaX_") or name == "llex":
        return {"role": "Lua lexer/parser front-end", "rationale": "lexer-related name"}
    if name.startswith("luaC_"):
        return {"role": "Lua garbage collector", "rationale": "luaC_ prefix"}
    return {"role": "Lua internal/static helper or custom candidate", "rationale": "no public Lua API prefix"}


def candidate_delta(top_candidates: list[dict]) -> float | None:
    if len(top_candidates) < 2:
        return None
    return round(
        float(top_candidates[0].get("final_score", 0.0))
        - float(top_candidates[1].get("final_score", 0.0)),
        6,
    )


def infer_review_category(row: dict) -> str:
    reasons = set(row.get("status_reasons", []))
    if "multiple_candidates_same_final_score" in reasons or "low_score_margin" in reasons:
        return "ambiguous_candidate_tie"
    if "no_anchor_evidence" in reasons:
        return "needs_more_graph_anchors"
    if "duplicate_accepted_mapping_in_query_scope" in reasons:
        return "mapping_conflict"
    return "manual_review"


def recommended_action(row: dict) -> str:
    category = infer_review_category(row)
    if category == "ambiguous_candidate_tie":
        return (
            "Compare the tied candidates with local function features or send this "
            "record to the Local LLM analyst layer for semantic disambiguation."
        )
    if category == "needs_more_graph_anchors":
        return (
            "Do not accept automatically. First propagate additional high-confidence "
            "caller/callee anchors, then re-run graph scoring."
        )
    if category == "mapping_conflict":
        return "Resolve one-to-one mapping conflict before accepting either candidate."
    return "Keep deferred and inspect manually."


def compact_candidate(candidate: dict) -> dict:
    graph = candidate.get("graph_breakdown", {})
    function_name = candidate.get("candidate_function_name")
    return {
        "rank": candidate.get("final_rank"),
        "function_name": function_name,
        "role_hint": candidate_role_hint(function_name),
        "source": candidate.get("candidate_source"),
        "retrieval_prior": candidate.get("retrieval_prior"),
        "graph_score": candidate.get("graph_score"),
        "final_score": candidate.get("final_score"),
        "primary_matches": graph.get("primary_matches"),
        "auxiliary_matches": graph.get("auxiliary_matches"),
        "missing_anchor_edges": graph.get("missing_anchor_edges"),
        "evidence": candidate.get("evidence", [])[:5],
    }


def build_analysis_case(row: dict, *, top_n: int, query_feature: dict | None) -> dict:
    top_candidates = row.get("top_candidates", [])[:top_n]
    reason_details = [
        {
            "reason": reason,
            "description": REASON_DESCRIPTIONS.get(reason, "No description available."),
        }
        for reason in row.get("status_reasons", [])
    ]
    anchor_summary = row.get("anchor_summary", {})
    query_feature_summary = summarize_feature(query_feature)
    return {
        "case_id": row.get("case_id"),
        "query_file": row.get("query_file"),
        "query_func": row.get("query_func"),
        "architecture": row.get("architecture"),
        "expected_function": row.get("expected_function"),
        "current_top_prediction": row.get("predicted_function_name"),
        "expected_final_rank": row.get("expected_final_rank"),
        "review_category": infer_review_category(row),
        "status_reasons": reason_details,
        "recommended_action": recommended_action(row),
        "score_margin_top1_top2": candidate_delta(top_candidates),
        "top_tied_candidates": row.get("top_tied_candidates", []),
        "anchor_counts": {
            "callee": anchor_summary.get("callee_anchor_count", 0),
            "caller": anchor_summary.get("caller_anchor_count", 0),
        },
        "anchors": {
            "callees": anchor_summary.get("callee_anchors", []),
            "callers": anchor_summary.get("caller_anchors", []),
        },
        "candidate_counts": {
            "total": row.get("candidate_count"),
            "retrieval": row.get("retrieval_candidate_count"),
            "expanded": row.get("expanded_candidate_count"),
        },
        "query_feature_summary": query_feature_summary,
        "top_candidates": [compact_candidate(candidate) for candidate in top_candidates],
        "llm_payload": {
            "schema_version": "0.2",
            "task": (
                "Review one deferred Lua function mapping case. Decide whether one "
                "candidate has enough evidence to recommend, or whether the case "
                "should remain deferred for more anchors/manual review."
            ),
            "label_leakage_warning": (
                "Function names in query_func and expected_function may be debug/eval labels. "
                "Do not use them as identity evidence. Use feature, retrieval, and graph evidence."
            ),
            "query_function": row.get("query_func"),
            "query_feature_summary": query_feature_summary,
            "deferred_reasons": row.get("status_reasons", []),
            "candidate_summaries": [compact_candidate(candidate) for candidate in top_candidates],
            "graph_anchor_summary": {
                "callee_anchor_count": anchor_summary.get("callee_anchor_count", 0),
                "caller_anchor_count": anchor_summary.get("caller_anchor_count", 0),
                "top_tied_candidates": row.get("top_tied_candidates", []),
            },
            "answer_format": {
                "classification": "prefer_candidate | remain_deferred | need_more_anchors | possible_custom",
                "recommended_candidate": "string or null",
                "confidence": "0.0-1.0",
                "reasoning_summary": ["short evidence bullets"],
                "risks": ["short risk bullets"],
            },
        },
    }


def main() -> None:
    args = parse_args()
    source = load_json(args.input_json)
    deferred_rows = [row for row in source.get("results", []) if row.get("status") == "deferred"]
    conflict_rows = [row for row in source.get("results", []) if row.get("status") == "conflict"]
    query_features = load_query_features(args.embedding_root, deferred_rows + conflict_rows)
    cases = [
        build_analysis_case(
            row,
            top_n=args.top_candidates,
            query_feature=query_features.get(row["case_id"]),
        )
        for row in deferred_rows
    ]
    conflicts = [
        build_analysis_case(
            row,
            top_n=args.top_candidates,
            query_feature=query_features.get(row["case_id"]),
        )
        for row in conflict_rows
    ]

    category_counts: dict[str, int] = {}
    for case in cases + conflicts:
        category = case["review_category"]
        category_counts[category] = category_counts.get(category, 0) + 1

    output = {
        "schema_version": "0.1",
        "description": (
            "Compact deferred/conflict analysis generated from anchor propagation output. "
            "This is suitable for manual review and later Local LLM analyst input."
        ),
        "source_file": str(args.input_json),
        "embedding_root": str(args.embedding_root),
        "source_suite_name": source.get("suite_name"),
        "summary": {
            "deferred_count": len(cases),
            "conflict_count": len(conflicts),
            "category_counts": category_counts,
            "feature_summary_attached": sum(
                1 for case in cases + conflicts if case.get("query_feature_summary")
            ),
        },
        "deferred_cases": cases,
        "conflict_cases": conflicts,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote deferred analysis: {args.output_json}")
    print(json.dumps(output["summary"], indent=2, ensure_ascii=False))
    for case in cases:
        print(
            f"{case['case_id']}: {case['review_category']} "
            f"top={case['current_top_prediction']} expected_rank={case['expected_final_rank']} "
            f"action={case['recommended_action']}"
        )


if __name__ == "__main__":
    main()
