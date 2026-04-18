#!/usr/bin/env python3
"""
Evaluate real lua_function_embedding results with callgraph score correction.

This script reads a suite file that points to:
  - lua_function_embedding/data/eval/result_dir_index.json
  - masked query feature JSON files
  - data/inputs/callgraphs/reference_callgraph.sqlite

For this integration evaluation, anchors are created from query caller/callee
names that are still visible and exist in the vanilla reference DB.

Typical command:

  python3 scripts/03_eval_hybrid_callgraph_cases.py \
    --suite data/eval/cases/hybrid_callgraph_lua547_eval.json
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
        description="Evaluate hybrid retrieval results with callgraph correction."
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("data/eval/cases/hybrid_callgraph_lua547_eval.json"),
        help="hybrid callgraph evaluation suite JSON",
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


def candidate_rows(retrieval_case: dict, candidate_source: str) -> list[dict]:
    rows = retrieval_case.get(candidate_source, [])
    candidates = []
    for rank, row in enumerate(rows, start=1):
        name = row.get("function_name")
        if not name:
            continue
        candidates.append(
            {
                "candidate_function_name": name,
                "score_total": float(row.get("score_total", 0.0)),
                "rank": rank,
                "source_json": row.get("source_json"),
            }
        )
    return candidates


def load_query_function(embedding_root: Path, retrieval_case: dict) -> dict:
    query_file = resolve_path(retrieval_case["query_file"], base=embedding_root)
    rows = load_json(query_file)
    query_func = retrieval_case["query_func"]
    for row in rows:
        if row.get("function_name") == query_func:
            return row
    raise KeyError(f"query function not found: {query_func} in {query_file}")


def visible_reference_neighbors(
    names: list[str],
    *,
    reference_names: set[str],
    exclude_prefixes: list[str],
    self_name: str,
) -> list[str]:
    result = []
    seen = set()
    for name in names:
        if not name or name == self_name:
            continue
        if any(name.startswith(prefix) for prefix in exclude_prefixes):
            continue
        if name not in reference_names:
            continue
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
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
            "callee_anchor_count": len(callee_anchors),
            "caller_anchor_count": len(caller_anchors),
        },
        "evidence": evidence[:50],
    }


def compute_summary(results: list[dict]) -> dict:
    total = len(results)
    retrieval_hits = sum(1 for r in results if r["retrieval_top1_hit"])
    propagation_hits = sum(1 for r in results if r["propagation_top1_hit"])
    improved = sum(1 for r in results if not r["retrieval_top1_hit"] and r["propagation_top1_hit"])
    regressed = sum(1 for r in results if r["retrieval_top1_hit"] and not r["propagation_top1_hit"])
    return {
        "num_cases": total,
        "retrieval_top1_accuracy": round(retrieval_hits / total, 6) if total else 0.0,
        "propagation_top1_accuracy": round(propagation_hits / total, 6) if total else 0.0,
        "improved": improved,
        "regressed": regressed,
    }


def main() -> None:
    args = parse_args()
    suite = load_json(resolve_path(args.suite))

    embedding_root = resolve_path(suite["embedding_project_root"])
    retrieval_result = load_json(resolve_path(suite["retrieval_result_json"]))
    reference_db_path = resolve_path(suite["reference_db"])
    output_json = resolve_path(suite["output_json"])
    candidate_source = suite.get("candidate_source", "unique_topk_preview")
    exclude_prefixes = suite.get("anchor_policy", {}).get("exclude_prefixes", [])
    primary_opt = suite.get("scoring", {}).get("primary_opt", "O0")
    strip_mode = suite.get("scoring", {}).get("strip_mode", "nostrip")

    retrieval_cases = {case["case_id"]: case for case in retrieval_result.get("cases", [])}
    ref_db = ReferenceGraphDB(reference_db_path)
    reference_names = ref_db.reference_function_names()

    results = []
    try:
        for case_cfg in suite.get("cases", []):
            case_id = case_cfg["case_id"]
            retrieval_case = retrieval_cases[case_id]
            expected = case_cfg.get("expected_function", retrieval_case.get("expected_function"))
            query_row = load_query_function(embedding_root, retrieval_case)
            query_name = query_row["function_name"]
            architecture = normalize_architecture(query_row.get("architecture", "x86_64"))
            lua_version = query_row.get("lua_version", "Lua_547")

            callee_anchors = visible_reference_neighbors(
                query_row.get("callees") or [],
                reference_names=reference_names,
                exclude_prefixes=exclude_prefixes,
                self_name=query_name,
            )
            caller_anchors = visible_reference_neighbors(
                query_row.get("callers") or [],
                reference_names=reference_names,
                exclude_prefixes=exclude_prefixes,
                self_name=query_name,
            )

            candidates = candidate_rows(retrieval_case, candidate_source)
            scored = []
            for candidate in candidates:
                item = score_candidate(
                    candidate_name=candidate["candidate_function_name"],
                    retrieval_prior=candidate["score_total"],
                    callee_anchors=callee_anchors,
                    caller_anchors=caller_anchors,
                    ref_db=ref_db,
                    lua_version=lua_version,
                    architecture=architecture,
                    primary_opt=primary_opt,
                    strip_mode=strip_mode,
                )
                item["original_rank"] = candidate["rank"]
                item["source_json"] = candidate.get("source_json")
                scored.append(item)

            reranked = sorted(
                scored,
                key=lambda item: (
                    item["final_score"],
                    item["graph_breakdown"]["primary_matches"],
                    item["retrieval_prior"],
                ),
                reverse=True,
            )
            for rank, item in enumerate(reranked, start=1):
                item["final_rank"] = rank

            retrieval_top1 = candidates[0]["candidate_function_name"] if candidates else ""
            propagation_top1 = reranked[0]["candidate_function_name"] if reranked else ""
            results.append(
                {
                    "case_id": case_id,
                    "mode": retrieval_case.get("mode"),
                    "query_file": retrieval_case.get("query_file"),
                    "query_func": query_name,
                    "architecture": architecture,
                    "expected_function": expected,
                    "retrieval_top1": retrieval_top1,
                    "propagation_top1": propagation_top1,
                    "retrieval_top1_hit": retrieval_top1 == expected,
                    "propagation_top1_hit": propagation_top1 == expected,
                    "top1_changed": retrieval_top1 != propagation_top1,
                    "candidate_count": len(candidates),
                    "anchor_summary": {
                        "callee_anchor_count": len(callee_anchors),
                        "caller_anchor_count": len(caller_anchors),
                        "callee_anchors": callee_anchors[:50],
                        "caller_anchors": caller_anchors[:50],
                    },
                    "candidates": reranked,
                }
            )
    finally:
        ref_db.close()

    summary = compute_summary(results)
    by_mode: dict[str, list[dict]] = {}
    for result in results:
        by_mode.setdefault(result.get("mode") or "unknown", []).append(result)

    output = {
        "schema_version": "0.1",
        "suite_name": suite.get("suite_name"),
        "description": suite.get("description"),
        "anchor_policy": suite.get("anchor_policy"),
        "candidate_source": candidate_source,
        "scoring": {
            "primary_opt": primary_opt,
            "strip_mode": strip_mode,
            "primary_edge_bonus": PRIMARY_EDGE_BONUS,
            "auxiliary_edge_bonus": AUXILIARY_EDGE_BONUS,
            "missing_anchor_penalty": MISSING_ANCHOR_PENALTY,
        },
        "summary": summary,
        "by_mode": {mode: compute_summary(items) for mode, items in by_mode.items()},
        "cases": results,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote result: {output_json}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    for result in results:
        print(
            f"{result['case_id']}: {result['retrieval_top1']} -> "
            f"{result['propagation_top1']} expected={result['expected_function']} "
            f"anchors={result['anchor_summary']['callee_anchor_count']}c/"
            f"{result['anchor_summary']['caller_anchor_count']}r"
        )


if __name__ == "__main__":
    main()
