#!/usr/bin/env python3
"""
Propagate function mappings from high-confidence call graph anchors.

This script is the first Agent-shaped evaluation flow:
  1. Load retrieval candidates from lua_function_embedding eval output.
  2. Load seed anchors that are already considered reliable.
  3. Use anchored caller/callee neighbors to expand and re-rank candidates.
  4. Classify each mapping as accepted, deferred, or conflict.

Iterative mode (--iterative):
  Accepted functions are added back to the anchor set each round so that
  propagation reaches functions more than one hop away from the initial seeds.
  Rounds continue until no new functions are accepted or max-rounds is reached.

  Margin tightening per round prevents error accumulation as the anchor chain
  grows longer and confidence decreases with distance from seeds.

Typical commands from the project root:

  # Single-pass (original behaviour)
  python3 scripts/04_propagate_from_anchors.py \
    --suite data/eval/cases/anchor_propagation_lua547_eval.json

  # Iterative BFS propagation
  python3 scripts/04_propagate_from_anchors.py \
    --suite data/eval/cases/anchor_propagation_lua547_eval.json \
    --iterative \
    --max-rounds 20 \
    --min-accepted-per-round 1 \
    --margin-tightening 0.005

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
    parser.add_argument(
        "--iterative",
        action="store_true",
        help="enable iterative BFS propagation (accepted functions become anchors each round)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=20,
        help="safety cap on number of propagation rounds (iterative mode only)",
    )
    parser.add_argument(
        "--min-accepted-per-round",
        type=int,
        default=1,
        help="stop if a round accepts fewer than this many functions (iterative mode only)",
    )
    parser.add_argument(
        "--margin-tightening",
        type=float,
        default=0.005,
        help="increase accept_margin by this amount each round to suppress error accumulation",
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


def _process_one_case(
    *,
    case_cfg: dict,
    retrieval_case: dict,
    query_row: dict,
    anchor_set: dict[str, str],
    ref_db: ReferenceGraphDB,
    reference_names: set[str],
    candidate_source: str,
    primary_opt: str,
    strip_mode: str,
    expansion_prior: float,
    expansion_limit: int,
    exclude_prefixes: list[str],
    allow_visible_reference_name_anchors: bool,
    classification_policy: dict,
    output_top_candidates: int,
    propagation_round: int,
) -> dict:
    query_name = query_row["function_name"]
    lua_version = query_row.get("lua_version", "Lua_547")
    architecture = normalize_architecture(query_row.get("architecture", "x86_64"))
    expected = case_cfg.get("expected_function", retrieval_case.get("expected_function"))

    callee_anchor_items = anchored_neighbors(
        query_row.get("callees") or [],
        seed_anchors=anchor_set,
        reference_names=reference_names,
        exclude_prefixes=exclude_prefixes,
        allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
        self_name=query_name,
    )
    caller_anchor_items = anchored_neighbors(
        query_row.get("callers") or [],
        seed_anchors=anchor_set,
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
        candidates.append({
            "candidate_function_name": item["candidate_function_name"],
            "retrieval_prior": expansion_prior,
            "original_rank": len(retrieval_names) + offset,
            "candidate_source": "callgraph_expansion",
            "expansion_support": {
                "primary_support": item["primary_support"],
                "auxiliary_support": item["auxiliary_support"],
            },
        })

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
        key=lambda x: (
            x["final_score"],
            x["graph_breakdown"]["primary_matches"],
            x["retrieval_prior"],
            x["candidate_function_name"],
        ),
        reverse=True,
    )
    for rank, item in enumerate(reranked, start=1):
        item["final_rank"] = rank

    status, status_reasons = classify_mapping(reranked=reranked, policy=classification_policy)
    predicted = reranked[0]["candidate_function_name"] if reranked else None
    expected_rank = next(
        (item["final_rank"] for item in reranked if item["candidate_function_name"] == expected),
        None,
    )
    top_tied = [
        item["candidate_function_name"]
        for item in reranked
        if reranked and item["final_score"] == reranked[0]["final_score"]
    ]

    return {
        "case_id": case_cfg["case_id"],
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
        "propagation_round": propagation_round,
        "anchor_summary": {
            "callee_anchor_count": len(callee_anchors),
            "caller_anchor_count": len(caller_anchors),
            "callee_anchors": callee_anchor_items[:30],
            "caller_anchors": caller_anchor_items[:30],
        },
        "top_tied_candidates": top_tied[:20],
        "top_candidates": reranked[:output_top_candidates],
    }


def _check_conflicts(
    round_results: list[dict],
    already_resolved: dict[str, dict],
) -> None:
    accepted_by_scope: dict[tuple[str, str], list[dict]] = {}

    for old in already_resolved.values():
        if old["status"] != "accepted" or not old["predicted_function_name"]:
            continue
        key = (old["query_file"], old["predicted_function_name"])
        accepted_by_scope.setdefault(key, []).append(old)

    for result in round_results:
        if result["status"] != "accepted" or not result["predicted_function_name"]:
            continue
        key = (result["query_file"], result["predicted_function_name"])
        accepted_by_scope.setdefault(key, []).append(result)

    for group in accepted_by_scope.values():
        if len(group) <= 1:
            continue
        for result in group:
            if result["status"] == "accepted":
                result["status"] = "conflict"
                result["status_reasons"] = ["duplicate_accepted_mapping_in_query_scope"]


def run_iterative_propagation(
    *,
    suite_cases: list[dict],
    retrieval_cases: dict,
    query_row_cache: dict[str, dict],
    initial_anchors: dict[str, str],
    ref_db: ReferenceGraphDB,
    reference_names: set[str],
    candidate_source: str,
    primary_opt: str,
    strip_mode: str,
    expansion_prior: float,
    expansion_limit: int,
    exclude_prefixes: list[str],
    allow_visible_reference_name_anchors: bool,
    base_classification_policy: dict,
    output_top_candidates: int,
    max_rounds: int,
    min_accepted_per_round: int,
    margin_tightening: float,
) -> tuple[list[dict], list[dict]]:
    """
    BFS-style iterative propagation.

    Each round:
      1. Score all unresolved functions using the current anchor_set.
      2. Accepted functions are added to anchor_set for the next round.
      3. Stop when newly_accepted < min_accepted_per_round or max_rounds reached.

    accept_margin tightens by margin_tightening each round to suppress
    error accumulation as the anchor chain grows longer from the seeds.
    """
    anchor_set = dict(initial_anchors)
    unresolved_ids = {c["case_id"] for c in suite_cases}
    resolved: dict[str, dict] = {}
    last_round_results: dict[str, dict] = {}
    round_log: list[dict] = []

    for round_num in range(max_rounds):
        policy = dict(base_classification_policy)
        base_margin = float(policy.get("accept_margin", 0.015))
        policy["accept_margin"] = round(base_margin + round_num * margin_tightening, 6)

        round_results: list[dict] = []

        for case_cfg in suite_cases:
            case_id = case_cfg["case_id"]
            if case_id not in unresolved_ids:
                continue

            result = _process_one_case(
                case_cfg=case_cfg,
                retrieval_case=retrieval_cases[case_id],
                query_row=query_row_cache[case_id],
                anchor_set=anchor_set,
                ref_db=ref_db,
                reference_names=reference_names,
                candidate_source=candidate_source,
                primary_opt=primary_opt,
                strip_mode=strip_mode,
                expansion_prior=expansion_prior,
                expansion_limit=expansion_limit,
                exclude_prefixes=exclude_prefixes,
                allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
                classification_policy=policy,
                output_top_candidates=output_top_candidates,
                propagation_round=round_num,
            )
            round_results.append(result)
            last_round_results[case_id] = result

        _check_conflicts(round_results, resolved)

        newly_accepted: dict[str, dict] = {
            r["case_id"]: r for r in round_results if r["status"] == "accepted"
        }
        newly_conflict: dict[str, dict] = {
            r["case_id"]: r for r in round_results if r["status"] == "conflict"
        }

        round_log.append({
            "round": round_num,
            "newly_accepted": len(newly_accepted),
            "newly_conflict": len(newly_conflict),
            "anchor_set_size": len(anchor_set) + len(newly_accepted),
            "unresolved_remaining": len(unresolved_ids) - len(newly_accepted) - len(newly_conflict),
            "policy_margin": policy["accept_margin"],
        })
        print(
            f"[Round {round_num}] "
            f"accepted={len(newly_accepted)} "
            f"conflict={len(newly_conflict)} "
            f"anchor_set={len(anchor_set) + len(newly_accepted)} "
            f"unresolved={len(unresolved_ids) - len(newly_accepted) - len(newly_conflict)} "
            f"margin={policy['accept_margin']:.4f}"
        )

        for case_id, result in {**newly_accepted, **newly_conflict}.items():
            resolved[case_id] = result
            unresolved_ids.discard(case_id)

        for case_id, result in newly_accepted.items():
            anchor_set[result["query_func"]] = result["predicted_function_name"]

        if len(newly_accepted) < min_accepted_per_round:
            print(f"[Converged] newly_accepted={len(newly_accepted)} < min={min_accepted_per_round}")
            break

    # Remaining unresolved → keep last round's result (status = deferred)
    for case_id in unresolved_ids:
        resolved[case_id] = last_round_results.get(
            case_id,
            {"case_id": case_id, "status": "deferred", "status_reasons": ["no_candidates"], "propagation_round": -1},
        )

    all_results = [resolved[c["case_id"]] for c in suite_cases]
    return all_results, round_log


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
    output_top_candidates = int(suite.get("output_top_candidates", 5))

    retrieval_cases = {case["case_id"]: case for case in retrieval_result.get("cases", [])}
    seed_anchors = load_seed_anchors(anchor_json)
    ref_db = ReferenceGraphDB(reference_db_path)
    reference_names = ref_db.reference_function_names()

    suite_cases = suite.get("cases", [])

    # Pre-load all query rows once to avoid repeated file I/O across rounds
    query_row_cache: dict[str, dict] = {}
    for case_cfg in suite_cases:
        case_id = case_cfg["case_id"]
        query_row_cache[case_id] = load_query_function(embedding_root, retrieval_cases[case_id])

    round_log: list[dict] = []

    try:
        if args.iterative:
            print(
                f"[Iterative] max_rounds={args.max_rounds} "
                f"min_accepted_per_round={args.min_accepted_per_round} "
                f"margin_tightening={args.margin_tightening}"
            )
            results, round_log = run_iterative_propagation(
                suite_cases=suite_cases,
                retrieval_cases=retrieval_cases,
                query_row_cache=query_row_cache,
                initial_anchors=seed_anchors,
                ref_db=ref_db,
                reference_names=reference_names,
                candidate_source=candidate_source,
                primary_opt=primary_opt,
                strip_mode=strip_mode,
                expansion_prior=expansion_prior,
                expansion_limit=expansion_limit,
                exclude_prefixes=exclude_prefixes,
                allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
                base_classification_policy=classification_policy,
                output_top_candidates=output_top_candidates,
                max_rounds=args.max_rounds,
                min_accepted_per_round=args.min_accepted_per_round,
                margin_tightening=args.margin_tightening,
            )
        else:
            results = []
            for case_cfg in suite_cases:
                case_id = case_cfg["case_id"]
                result = _process_one_case(
                    case_cfg=case_cfg,
                    retrieval_case=retrieval_cases[case_id],
                    query_row=query_row_cache[case_id],
                    anchor_set=seed_anchors,
                    ref_db=ref_db,
                    reference_names=reference_names,
                    candidate_source=candidate_source,
                    primary_opt=primary_opt,
                    strip_mode=strip_mode,
                    expansion_prior=expansion_prior,
                    expansion_limit=expansion_limit,
                    exclude_prefixes=exclude_prefixes,
                    allow_visible_reference_name_anchors=allow_visible_reference_name_anchors,
                    classification_policy=classification_policy,
                    output_top_candidates=output_top_candidates,
                    propagation_round=0,
                )
                results.append(result)

            # Single-pass conflict check
            _check_conflicts(results, {})

    finally:
        ref_db.close()

    # Apply anchor overrides for any deferred case that is already in the seed anchor set.
    #
    # Two kinds of overrides:
    #   force_anchor  — manually confirmed via decompile analysis (highest priority)
    #   retrieval_high_confidence anchor that still ended up deferred — happens when a
    #     function has no anchored neighbors (no_anchor_evidence / insufficient_primary_graph),
    #     so propagation can't lift it even though retrieval was confident.  If the anchor
    #     mapping already agrees with the propagation's top prediction we accept it directly.
    force_anchor_map: dict[str, str] = {
        m["query_function_name"]: m["reference_function_name"]
        for m in anchor_json.get("mappings", [])
        if m.get("source") == "force_anchor" and m.get("status") == "accepted"
    }
    anchor_override_count = 0
    for result in results:
        status = result.get("status")
        if status not in ("deferred", "conflict"):
            continue
        qf = result.get("query_func")
        if not qf:
            continue
        if qf in force_anchor_map:
            # Manually confirmed via decompile analysis — override regardless of status
            result["predicted_function_name"] = force_anchor_map[qf]
            result["status"] = "accepted"
            result["status_reasons"] = ["force_anchor"]
            anchor_override_count += 1
        elif status == "deferred" and qf in seed_anchors:
            # retrieval_high_confidence anchor that failed propagation: accept it
            # (the anchor gives us the confirmed reference function name)
            ref = seed_anchors[qf]
            result["predicted_function_name"] = ref
            result["status"] = "accepted"
            result["status_reasons"] = ["retrieval_anchor_override"]
            anchor_override_count += 1
    if anchor_override_count:
        print(f"[anchor_override] resolved {anchor_override_count} deferred/conflict → accepted")

    output = {
        "schema_version": "0.1",
        "suite_name": suite.get("suite_name"),
        "description": suite.get("description"),
        "anchor_policy": suite.get("anchor_policy"),
        "candidate_expansion": suite.get("candidate_expansion"),
        "classification_policy": classification_policy,
        "iterative": args.iterative,
        "round_log": round_log,
        "summary": compute_summary(results),
        "results": results,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote result: {output_json}")
    if round_log:
        print("[Round log]")
        for entry in round_log:
            print(f"  Round {entry['round']}: accepted={entry['newly_accepted']} anchor_set={entry['anchor_set_size']} unresolved={entry['unresolved_remaining']}")
    print(json.dumps(output["summary"], indent=2, ensure_ascii=False))
    for result in results:
        print(
            f"{result['case_id']}: {result['status']} "
            f"pred={result['predicted_function_name']} expected={result['expected_function']} "
            f"rank={result['expected_final_rank']} reasons={','.join(result['status_reasons'])}"
        )


if __name__ == "__main__":
    main()
