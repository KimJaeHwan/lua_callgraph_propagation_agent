#!/usr/bin/env python3
"""
Propagate function mappings from high-confidence call graph anchors.

This script is the first Agent-shaped evaluation flow:
  1. Load retrieval candidates from lua_function_embedding eval output.
  2. Load seed anchors that are already considered reliable.
  3. Use anchored caller/callee neighbors to expand and re-rank candidates.
  4. Classify each mapping as accepted, deferred, or conflict.

Typical command from the project root:

  python3 scripts/04_propagate_from_anchors.py \
    --suite data/eval/cases/anchor_propagation_lua547_eval.json

The eval suite may enable visible-name anchors for controlled fixtures. That is
useful for measuring the propagation policy, but real anonymous binaries should
prefer explicit anchors produced by earlier high-confidence mapping steps.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PRIMARY_EDGE_BONUS = 0.04
AUXILIARY_EDGE_BONUS = 0.015
MISSING_ANCHOR_PENALTY = 0.002


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Propagate function mappings from accepted callgraph anchors."
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("data/eval/cases/anchor_propagation_lua547_eval.json"),
        help="anchor propagation suite JSON",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="override output JSON path from suite",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(path: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (base / p).resolve()


def normalize_architecture(arch: str) -> str:
    return "aarch64" if arch in {"arm64", "aarch64"} else arch


def is_visible_reference_name(name: str, *, reference_names: set[str], exclude_prefixes: list[str]) -> bool:
    if not name or name not in reference_names:
        return False
    return not any(name.startswith(prefix) for prefix in exclude_prefixes)


class ReferenceGraphDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def reference_function_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT function_name FROM functions WHERE graph_role = 'reference'"
        ).fetchall()
        return {row["function_name"] for row in rows}

    def edge_opt_levels(
        self,
        *,
        src_name: str,
        dst_name: str,
        lua_version: str,
        architecture: str,
        strip_mode: str,
    ) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT opt_level
            FROM edges
            WHERE graph_role = 'reference'
              AND lua_version = ?
              AND architecture = ?
              AND strip_mode = ?
              AND src_name = ?
              AND dst_name = ?
            """,
            (lua_version, architecture, strip_mode, src_name, dst_name),
        ).fetchall()
        return {row["opt_level"] for row in rows}

    def expansion_candidates(
        self,
        *,
        callee_anchors: list[str],
        caller_anchors: list[str],
        reference_names: set[str],
        lua_version: str,
        architecture: str,
        strip_mode: str,
        primary_opt: str,
        exclude_names: set[str],
        limit: int,
    ) -> list[dict]:
        expanded: dict[str, dict] = {}

        def add_candidate(name: str, relation: str, anchor: str, opt_level: str) -> None:
            if not name or name in exclude_names or name not in reference_names:
                return
            item = expanded.setdefault(
                name,
                {
                    "candidate_function_name": name,
                    "primary_support": 0,
                    "auxiliary_support": 0,
                    "supporting_edges": [],
                },
            )
            if opt_level == primary_opt:
                item["primary_support"] += 1
            else:
                item["auxiliary_support"] += 1
            item["supporting_edges"].append(
                {"relation": relation, "anchor": anchor, "opt_level": opt_level}
            )

        for anchor in callee_anchors:
            rows = self.conn.execute(
                """
                SELECT DISTINCT src_name, opt_level
                FROM edges
                WHERE graph_role = 'reference'
                  AND lua_version = ?
                  AND architecture = ?
                  AND strip_mode = ?
                  AND dst_name = ?
                """,
                (lua_version, architecture, strip_mode, anchor),
            ).fetchall()
            for row in rows:
                add_candidate(row["src_name"], "candidate_calls_anchor", anchor, row["opt_level"])

        for anchor in caller_anchors:
            rows = self.conn.execute(
                """
                SELECT DISTINCT dst_name, opt_level
                FROM edges
                WHERE graph_role = 'reference'
                  AND lua_version = ?
                  AND architecture = ?
                  AND strip_mode = ?
                  AND src_name = ?
                """,
                (lua_version, architecture, strip_mode, anchor),
            ).fetchall()
            for row in rows:
                add_candidate(row["dst_name"], "anchor_calls_candidate", anchor, row["opt_level"])

        return sorted(
            expanded.values(),
            key=lambda item: (
                item["primary_support"],
                item["auxiliary_support"],
                item["candidate_function_name"],
            ),
            reverse=True,
        )[:limit]


def load_query_function(embedding_root: Path, retrieval_case: dict) -> dict:
    query_file = resolve_path(retrieval_case["query_file"], base=embedding_root)
    rows = load_json(query_file)
    query_func = retrieval_case["query_func"]
    for row in rows:
        if row.get("function_name") == query_func:
            return row
    raise KeyError(f"query function not found: {query_func} in {query_file}")


def load_seed_anchors(anchor_json: dict) -> dict[str, str]:
    anchors: dict[str, str] = {}
    for mapping in anchor_json.get("mappings", []):
        if mapping.get("status") != "accepted":
            continue
        query_name = mapping.get("query_function_name") or mapping.get("query_function_id")
        ref_name = mapping.get("reference_function_name")
        if query_name and ref_name:
            anchors[query_name] = ref_name
    return anchors


def retrieval_candidates(retrieval_case: dict, candidate_source: str) -> list[dict]:
    candidates = []
    for rank, row in enumerate(retrieval_case.get(candidate_source, []), start=1):
        name = row.get("function_name")
        if not name:
            continue
        candidates.append(
            {
                "candidate_function_name": name,
                "retrieval_prior": float(row.get("score_total", 0.0)),
                "original_rank": rank,
                "candidate_source": "retrieval",
            }
        )
    return candidates


def anchored_neighbors(
    names: list[str],
    *,
    seed_anchors: dict[str, str],
    reference_names: set[str],
    exclude_prefixes: list[str],
    allow_visible_reference_name_anchors: bool,
    self_name: str,
) -> list[dict]:
    result = []
    seen = set()
    for name in names:
        if not name or name == self_name:
            continue
        ref_name = seed_anchors.get(name)
        source = "seed_anchor"
        if not ref_name and allow_visible_reference_name_anchors:
            if is_visible_reference_name(
                name, reference_names=reference_names, exclude_prefixes=exclude_prefixes
            ):
                ref_name = name
                source = "visible_reference_name_anchor"
        if not ref_name or ref_name in seen:
            continue
        seen.add(ref_name)
        result.append(
            {
                "query_neighbor_name": name,
                "reference_function_name": ref_name,
                "source": source,
            }
        )
    return result


def score_candidate(
    *,
    candidate_name: str,
    retrieval_prior: float,
    callee_anchors: list[str],
    caller_anchors: list[str],
    ref_db: ReferenceGraphDB,
    lua_version: str,
    architecture: str,
    primary_opt: str,
    strip_mode: str,
) -> dict:
    evidence = []
    primary_matches = 0
    auxiliary_matches = 0
    missing_anchor_edges = 0

    for anchor in callee_anchors:
        opts = ref_db.edge_opt_levels(
            src_name=candidate_name,
            dst_name=anchor,
            lua_version=lua_version,
            architecture=architecture,
            strip_mode=strip_mode,
        )
        if primary_opt in opts:
            primary_matches += 1
            evidence.append(f"primary_callee_anchor_match:{candidate_name}->{anchor}")
        elif opts:
            auxiliary_matches += 1
            evidence.append(f"aux_callee_anchor_match:{candidate_name}->{anchor}:{','.join(sorted(opts))}")
        else:
            missing_anchor_edges += 1

    for anchor in caller_anchors:
        opts = ref_db.edge_opt_levels(
            src_name=anchor,
            dst_name=candidate_name,
            lua_version=lua_version,
            architecture=architecture,
            strip_mode=strip_mode,
        )
        if primary_opt in opts:
            primary_matches += 1
            evidence.append(f"primary_caller_anchor_match:{anchor}->{candidate_name}")
        elif opts:
            auxiliary_matches += 1
            evidence.append(f"aux_caller_anchor_match:{anchor}->{candidate_name}:{','.join(sorted(opts))}")
        else:
            missing_anchor_edges += 1

    graph_score = (
        primary_matches * PRIMARY_EDGE_BONUS
        + auxiliary_matches * AUXILIARY_EDGE_BONUS
        - missing_anchor_edges * MISSING_ANCHOR_PENALTY
    )
    return {
        "candidate_function_name": candidate_name,
        "retrieval_prior": round(retrieval_prior, 6),
        "graph_score": round(graph_score, 6),
        "final_score": round(retrieval_prior + graph_score, 6),
        "graph_breakdown": {
            "primary_matches": primary_matches,
            "auxiliary_matches": auxiliary_matches,
            "missing_anchor_edges": missing_anchor_edges,
            "total_anchor_edges": len(callee_anchors) + len(caller_anchors),
        },
        "evidence": evidence[:30],
    }


def classify_mapping(
    *,
    reranked: list[dict],
    policy: dict,
) -> tuple[str, list[str]]:
    if not reranked:
        return "deferred", ["no_candidates"]

    top = reranked[0]
    second = reranked[1] if len(reranked) > 1 else None
    margin = top["final_score"] - second["final_score"] if second else top["final_score"]
    graph = top["graph_breakdown"]
    tied_top = [
        item["candidate_function_name"]
        for item in reranked
        if item["final_score"] == top["final_score"]
    ]

    reasons = []
    if graph["total_anchor_edges"] == 0:
        reasons.append("no_anchor_evidence")
    if graph["primary_matches"] < int(policy.get("min_primary_matches", 1)):
        reasons.append("insufficient_primary_graph_matches")
    if margin < float(policy.get("accept_margin", 0.015)):
        reasons.append("low_score_margin")
    if len(tied_top) > int(policy.get("max_tied_top_candidates", 1)):
        reasons.append("multiple_candidates_same_final_score")

    if reasons:
        return "deferred", reasons
    return "accepted", ["accepted_by_margin_and_graph_evidence"]


def compute_summary(results: list[dict]) -> dict:
    total = len(results)
    accepted = [r for r in results if r["status"] == "accepted"]
    deferred = [r for r in results if r["status"] == "deferred"]
    conflicts = [r for r in results if r["status"] == "conflict"]
    expected_items = [r for r in results if r.get("expected_function")]
    top1_hits = [r for r in expected_items if r["predicted_function_name"] == r["expected_function"]]
    top5_hits = [
        r
        for r in expected_items
        if r.get("expected_final_rank") is not None and r["expected_final_rank"] <= 5
    ]
    return {
        "num_cases": total,
        "accepted": len(accepted),
        "deferred": len(deferred),
        "conflict": len(conflicts),
        "expected_count": len(expected_items),
        "top1_accuracy": round(len(top1_hits) / len(expected_items), 6) if expected_items else None,
        "top5_accuracy": round(len(top5_hits) / len(expected_items), 6) if expected_items else None,
    }


def main() -> None:
    args = parse_args()
    suite = load_json(resolve_path(args.suite))

    embedding_root = resolve_path(suite["embedding_project_root"])
    retrieval_result = load_json(resolve_path(suite["retrieval_result_json"]))
    anchor_json = load_json(resolve_path(suite["anchor_json"]))
    output_json = resolve_path(args.output_json or suite["output_json"])
    reference_db_path = resolve_path(suite["reference_db"])

    candidate_source = suite.get("candidate_source", "unique_topk_preview")
    primary_opt = suite.get("scoring", {}).get("primary_opt", "O0")
    strip_mode = suite.get("scoring", {}).get("strip_mode", "nostrip")
    expansion_prior = float(suite.get("candidate_expansion", {}).get("default_prior", 0.65))
    expansion_limit = int(suite.get("candidate_expansion", {}).get("max_candidates_per_case", 80))
    exclude_prefixes = suite.get("anchor_policy", {}).get("exclude_prefixes", [])
    allow_visible_reference_name_anchors = bool(
        suite.get("anchor_policy", {}).get("allow_visible_reference_name_anchors", False)
    )
    classification_policy = suite.get("classification_policy", {})

    retrieval_cases = {case["case_id"]: case for case in retrieval_result.get("cases", [])}
    seed_anchors = load_seed_anchors(anchor_json)
    ref_db = ReferenceGraphDB(reference_db_path)
    reference_names = ref_db.reference_function_names()

    results = []
    try:
        for case_cfg in suite.get("cases", []):
            case_id = case_cfg["case_id"]
            retrieval_case = retrieval_cases[case_id]
            query_row = load_query_function(embedding_root, retrieval_case)
            query_name = query_row["function_name"]
            lua_version = query_row.get("lua_version", "Lua_547")
            architecture = normalize_architecture(query_row.get("architecture", "x86_64"))
            expected = case_cfg.get("expected_function", retrieval_case.get("expected_function"))

            callee_anchor_items = anchored_neighbors(
                query_row.get("callees") or [],
                seed_anchors=seed_anchors,
                reference_names=reference_names,
                exclude_prefixes=exclude_prefixes,
                allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
                self_name=query_name,
            )
            caller_anchor_items = anchored_neighbors(
                query_row.get("callers") or [],
                seed_anchors=seed_anchors,
                reference_names=reference_names,
                exclude_prefixes=exclude_prefixes,
                allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
                self_name=query_name,
            )
            callee_anchors = [item["reference_function_name"] for item in callee_anchor_items]
            caller_anchors = [item["reference_function_name"] for item in caller_anchor_items]

            candidates = retrieval_candidates(retrieval_case, candidate_source)
            retrieval_names = {c["candidate_function_name"] for c in candidates}
            expanded = ref_db.expansion_candidates(
                callee_anchors=callee_anchors,
                caller_anchors=caller_anchors,
                reference_names=reference_names,
                lua_version=lua_version,
                architecture=architecture,
                strip_mode=strip_mode,
                primary_opt=primary_opt,
                exclude_names=retrieval_names,
                limit=expansion_limit,
            )
            for offset, item in enumerate(expanded, start=1):
                candidates.append(
                    {
                        "candidate_function_name": item["candidate_function_name"],
                        "retrieval_prior": expansion_prior,
                        "original_rank": len(retrieval_names) + offset,
                        "candidate_source": "callgraph_expansion",
                        "expansion_support": {
                            "primary_support": item["primary_support"],
                            "auxiliary_support": item["auxiliary_support"],
                        },
                    }
                )

            scored = []
            for candidate in candidates:
                item = score_candidate(
                    candidate_name=candidate["candidate_function_name"],
                    retrieval_prior=candidate["retrieval_prior"],
                    callee_anchors=callee_anchors,
                    caller_anchors=caller_anchors,
                    ref_db=ref_db,
                    lua_version=lua_version,
                    architecture=architecture,
                    primary_opt=primary_opt,
                    strip_mode=strip_mode,
                )
                item["candidate_source"] = candidate["candidate_source"]
                item["original_rank"] = candidate["original_rank"]
                if candidate.get("expansion_support"):
                    item["expansion_support"] = candidate["expansion_support"]
                scored.append(item)

            reranked = sorted(
                scored,
                key=lambda item: (
                    item["final_score"],
                    item["graph_breakdown"]["primary_matches"],
                    item["retrieval_prior"],
                    item["candidate_function_name"],
                ),
                reverse=True,
            )
            for rank, item in enumerate(reranked, start=1):
                item["final_rank"] = rank

            status, status_reasons = classify_mapping(
                reranked=reranked,
                policy=classification_policy,
            )
            predicted = reranked[0]["candidate_function_name"] if reranked else None
            expected_rank = next(
                (
                    item["final_rank"]
                    for item in reranked
                    if item["candidate_function_name"] == expected
                ),
                None,
            )
            top_tied = [
                item["candidate_function_name"]
                for item in reranked
                if reranked and item["final_score"] == reranked[0]["final_score"]
            ]

            results.append(
                {
                    "case_id": case_id,
                    "mode": retrieval_case.get("mode"),
                    "query_file": retrieval_case.get("query_file"),
                    "query_func": query_name,
                    "architecture": architecture,
                    "expected_function": expected,
                    "predicted_function_name": predicted,
                    "status": status,
                    "status_reasons": status_reasons,
                    "expected_final_rank": expected_rank,
                    "top1_hit": predicted == expected if expected else None,
                    "candidate_count": len(candidates),
                    "retrieval_candidate_count": len(retrieval_names),
                    "expanded_candidate_count": len(expanded),
                    "anchor_summary": {
                        "callee_anchor_count": len(callee_anchors),
                        "caller_anchor_count": len(caller_anchors),
                        "callee_anchors": callee_anchor_items[:30],
                        "caller_anchors": caller_anchor_items[:30],
                    },
                    "top_tied_candidates": top_tied[:20],
                    "top_candidates": reranked[: int(suite.get("output_top_candidates", 5))],
                }
            )
    finally:
        ref_db.close()

    # A one-to-one conflict check is intentionally conservative. It flags only
    # accepted mappings inside the same query file that point to the same
    # reference function.
    accepted_by_scope: dict[tuple[str, str], list[dict]] = {}
    for result in results:
        if result["status"] != "accepted" or not result["predicted_function_name"]:
            continue
        key = (result["query_file"], result["predicted_function_name"])
        accepted_by_scope.setdefault(key, []).append(result)

    for group in accepted_by_scope.values():
        if len(group) <= 1:
            continue
        for result in group:
            result["status"] = "conflict"
            result["status_reasons"] = ["duplicate_accepted_mapping_in_query_scope"]

    output = {
        "schema_version": "0.1",
        "suite_name": suite.get("suite_name"),
        "description": suite.get("description"),
        "anchor_policy": suite.get("anchor_policy"),
        "candidate_expansion": suite.get("candidate_expansion"),
        "classification_policy": classification_policy,
        "summary": compute_summary(results),
        "results": results,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote result: {output_json}")
    print(json.dumps(output["summary"], indent=2, ensure_ascii=False))
    for result in results:
        print(
            f"{result['case_id']}: {result['status']} "
            f"pred={result['predicted_function_name']} expected={result['expected_function']} "
            f"rank={result['expected_final_rank']} reasons={','.join(result['status_reasons'])}"
        )


if __name__ == "__main__":
    main()
