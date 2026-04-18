#!/usr/bin/env python3
"""
Re-rank retrieval candidates with deterministic call graph evidence.

This is the first propagation MVP:
  - retrieval_topk.json provides candidate priors.
  - query_callgraph.json provides caller/callee edges in the target binary.
  - anchor_mapping.json provides high-confidence neighbor mappings.
  - reference_callgraph.sqlite provides vanilla Lua reference edges.

Typical command from the project root:

  python3 scripts/02_score_with_callgraph.py \
    --retrieval data/eval/fixtures/retrieval_topk_minimal.json \
    --query-graph data/eval/fixtures/query_callgraph_minimal.json \
    --anchors data/eval/fixtures/anchor_mapping_minimal.json \
    --reference-db data/inputs/callgraphs/reference_callgraph.sqlite \
    --expected query::00119970=luaV_execute \
    --output-json data/eval/fixtures/result_callgraph_minimal.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_RETRIEVAL = Path("data/eval/fixtures/retrieval_topk_minimal.json")
DEFAULT_QUERY_GRAPH = Path("data/eval/fixtures/query_callgraph_minimal.json")
DEFAULT_ANCHORS = Path("data/eval/fixtures/anchor_mapping_minimal.json")
DEFAULT_REFERENCE_DB = Path("data/inputs/callgraphs/reference_callgraph.sqlite")
DEFAULT_OUTPUT = Path("data/eval/fixtures/result_callgraph_minimal.json")

PRIMARY_OPT = "O0"
DEFAULT_STRIP_MODE = "nostrip"

# Conservative weights: graph evidence should re-rank close calls, not bulldoze
# strong retrieval evidence.
PRIMARY_EDGE_BONUS = 0.04
AUXILIARY_EDGE_BONUS = 0.015
MISSING_ANCHOR_PENALTY = 0.002


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-rank retrieval candidates using call graph anchor evidence."
    )
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--query-graph", type=Path, default=DEFAULT_QUERY_GRAPH)
    parser.add_argument("--anchors", type=Path, default=DEFAULT_ANCHORS)
    parser.add_argument("--reference-db", type=Path, default=DEFAULT_REFERENCE_DB)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--expected",
        action="append",
        default=[],
        metavar="QUERY_ID=FUNCTION_NAME",
        help="expected mapping used for before/after accuracy reporting",
    )
    parser.add_argument("--lua-version", default="Lua_547")
    parser.add_argument(
        "--primary-opt",
        default=PRIMARY_OPT,
        help="primary reference optimization level",
    )
    parser.add_argument(
        "--strip-mode",
        default=DEFAULT_STRIP_MODE,
        help="reference strip mode",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_expected(items: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"expected mapping must use QUERY_ID=FUNCTION_NAME: {item}")
        query_id, function_name = item.split("=", 1)
        expected[query_id] = function_name
    return expected


def build_query_adjacency(query_graph: dict) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in query_graph.get("edges", []):
        if edge.get("edge_type", "calls") != "calls":
            continue
        src = edge.get("src")
        dst = edge.get("dst")
        if not src or not dst:
            continue
        outgoing[src].add(dst)
        incoming[dst].add(src)
    return outgoing, incoming


def load_anchor_names(anchor_json: dict) -> dict[str, str]:
    anchors: dict[str, str] = {}
    for mapping in anchor_json.get("mappings", []):
        if mapping.get("status") != "accepted":
            continue
        query_id = mapping.get("query_function_id")
        ref_name = mapping.get("reference_function_name")
        if query_id and ref_name:
            anchors[query_id] = ref_name
    return anchors


class ReferenceGraphDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

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


def score_candidate(
    *,
    candidate_name: str,
    query_id: str,
    retrieval_prior: float,
    architecture: str,
    lua_version: str,
    primary_opt: str,
    strip_mode: str,
    outgoing: dict[str, set[str]],
    incoming: dict[str, set[str]],
    anchors: dict[str, str],
    ref_db: ReferenceGraphDB,
) -> dict:
    evidence: list[str] = []
    primary_matches = 0
    auxiliary_matches = 0
    missing_anchor_edges = 0
    unknown_extra_neighbors = 0

    # Callee evidence: query candidate -> anchored query callee should match
    # reference candidate -> anchored reference callee.
    for neighbor_id in sorted(outgoing.get(query_id, set())):
        anchor_name = anchors.get(neighbor_id)
        if not anchor_name:
            unknown_extra_neighbors += 1
            evidence.append(f"unanchored_query_callee:{neighbor_id}")
            continue

        opt_levels = ref_db.edge_opt_levels(
            src_name=candidate_name,
            dst_name=anchor_name,
            lua_version=lua_version,
            architecture=architecture,
            strip_mode=strip_mode,
        )
        if primary_opt in opt_levels:
            primary_matches += 1
            evidence.append(f"primary_callee_anchor_match:{candidate_name}->{anchor_name}")
        elif opt_levels:
            auxiliary_matches += 1
            evidence.append(
                f"aux_callee_anchor_match:{candidate_name}->{anchor_name}:"
                f"{','.join(sorted(opt_levels))}"
            )
        else:
            missing_anchor_edges += 1
            evidence.append(f"missing_callee_anchor_edge:{candidate_name}->{anchor_name}")

    # Caller evidence: anchored query caller -> query candidate should match
    # reference anchor caller -> reference candidate.
    for neighbor_id in sorted(incoming.get(query_id, set())):
        anchor_name = anchors.get(neighbor_id)
        if not anchor_name:
            unknown_extra_neighbors += 1
            evidence.append(f"unanchored_query_caller:{neighbor_id}")
            continue

        opt_levels = ref_db.edge_opt_levels(
            src_name=anchor_name,
            dst_name=candidate_name,
            lua_version=lua_version,
            architecture=architecture,
            strip_mode=strip_mode,
        )
        if primary_opt in opt_levels:
            primary_matches += 1
            evidence.append(f"primary_caller_anchor_match:{anchor_name}->{candidate_name}")
        elif opt_levels:
            auxiliary_matches += 1
            evidence.append(
                f"aux_caller_anchor_match:{anchor_name}->{candidate_name}:"
                f"{','.join(sorted(opt_levels))}"
            )
        else:
            missing_anchor_edges += 1
            evidence.append(f"missing_caller_anchor_edge:{anchor_name}->{candidate_name}")

    graph_bonus = primary_matches * PRIMARY_EDGE_BONUS + auxiliary_matches * AUXILIARY_EDGE_BONUS
    graph_penalty = missing_anchor_edges * MISSING_ANCHOR_PENALTY
    graph_score = graph_bonus - graph_penalty
    final_score = retrieval_prior + graph_score

    return {
        "candidate_function_name": candidate_name,
        "retrieval_prior": round(retrieval_prior, 6),
        "graph_score": round(graph_score, 6),
        "final_score": round(final_score, 6),
        "graph_breakdown": {
            "primary_matches": primary_matches,
            "auxiliary_matches": auxiliary_matches,
            "missing_anchor_edges": missing_anchor_edges,
            "unknown_extra_neighbors": unknown_extra_neighbors,
            "primary_edge_bonus": PRIMARY_EDGE_BONUS,
            "auxiliary_edge_bonus": AUXILIARY_EDGE_BONUS,
            "missing_anchor_penalty": MISSING_ANCHOR_PENALTY,
        },
        "evidence": evidence,
        "custom_suspected": unknown_extra_neighbors > 0,
    }


def evaluate_top1(results: list[dict], expected: dict[str, str]) -> dict:
    if not expected:
        return {
            "expected_count": 0,
            "retrieval_top1_accuracy": None,
            "propagation_top1_accuracy": None,
            "improved": 0,
            "regressed": 0,
        }

    total = 0
    retrieval_correct = 0
    propagation_correct = 0
    improved = 0
    regressed = 0

    for item in results:
        query_id = item["query_function_id"]
        expected_name = expected.get(query_id)
        if not expected_name:
            continue

        total += 1
        retrieval_ok = item["retrieval_top1"] == expected_name
        propagation_ok = item["propagation_top1"] == expected_name
        retrieval_correct += int(retrieval_ok)
        propagation_correct += int(propagation_ok)
        improved += int((not retrieval_ok) and propagation_ok)
        regressed += int(retrieval_ok and (not propagation_ok))

    return {
        "expected_count": total,
        "retrieval_top1_accuracy": round(retrieval_correct / total, 6) if total else None,
        "propagation_top1_accuracy": round(propagation_correct / total, 6) if total else None,
        "improved": improved,
        "regressed": regressed,
    }


def main() -> None:
    args = parse_args()
    retrieval_json = load_json(args.retrieval)
    query_graph_json = load_json(args.query_graph)
    anchor_json = load_json(args.anchors)
    expected = parse_expected(args.expected)

    outgoing, incoming = build_query_adjacency(query_graph_json)
    anchors = load_anchor_names(anchor_json)
    ref_db = ReferenceGraphDB(args.reference_db)

    results: list[dict] = []
    try:
        for query in retrieval_json.get("queries", []):
            query_id = query["query_function_id"]
            architecture = query.get("metadata", {}).get("architecture", "x86_64")
            candidates = query.get("candidates", [])
            if not candidates:
                continue

            retrieval_ranked = sorted(candidates, key=lambda c: c.get("rank", 999999))
            retrieval_top1 = retrieval_ranked[0]["candidate_function_name"]

            scored_candidates = []
            for candidate in candidates:
                candidate_name = candidate["candidate_function_name"]
                scored = score_candidate(
                    candidate_name=candidate_name,
                    query_id=query_id,
                    retrieval_prior=float(candidate.get("score_total", 0.0)),
                    architecture=architecture,
                    lua_version=args.lua_version,
                    primary_opt=args.primary_opt,
                    strip_mode=args.strip_mode,
                    outgoing=outgoing,
                    incoming=incoming,
                    anchors=anchors,
                    ref_db=ref_db,
                )
                scored["original_rank"] = candidate.get("rank")
                scored_candidates.append(scored)

            reranked = sorted(
                scored_candidates,
                key=lambda item: (
                    item["final_score"],
                    item["graph_breakdown"]["primary_matches"],
                    item["retrieval_prior"],
                ),
                reverse=True,
            )
            for idx, item in enumerate(reranked, start=1):
                item["final_rank"] = idx

            propagation_top1 = reranked[0]["candidate_function_name"]
            results.append(
                {
                    "query_function_id": query_id,
                    "query_function_name": query.get("query_function_name"),
                    "architecture": architecture,
                    "expected_function_name": expected.get(query_id),
                    "retrieval_top1": retrieval_top1,
                    "propagation_top1": propagation_top1,
                    "top1_changed": retrieval_top1 != propagation_top1,
                    "candidates": reranked,
                }
            )
    finally:
        ref_db.close()

    metrics = evaluate_top1(results, expected)
    output = {
        "schema_version": "0.1",
        "mode": "deterministic_callgraph_propagation_mvp",
        "inputs": {
            "retrieval": str(args.retrieval),
            "query_graph": str(args.query_graph),
            "anchors": str(args.anchors),
            "reference_db": str(args.reference_db),
        },
        "scoring": {
            "primary_opt": args.primary_opt,
            "strip_mode": args.strip_mode,
            "primary_edge_bonus": PRIMARY_EDGE_BONUS,
            "auxiliary_edge_bonus": AUXILIARY_EDGE_BONUS,
            "missing_anchor_penalty": MISSING_ANCHOR_PENALTY,
        },
        "metrics": metrics,
        "results": results,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote result: {args.output_json}")
    print(
        "Accuracy: retrieval_top1="
        f"{metrics['retrieval_top1_accuracy']} propagation_top1="
        f"{metrics['propagation_top1_accuracy']} "
        f"improved={metrics['improved']} regressed={metrics['regressed']}"
    )
    for item in results:
        print(
            f"{item['query_function_id']}: retrieval_top1={item['retrieval_top1']} "
            f"-> propagation_top1={item['propagation_top1']} "
            f"expected={item.get('expected_function_name')}"
        )
        for candidate in item["candidates"]:
            print(
                f"  #{candidate['final_rank']} {candidate['candidate_function_name']}: "
                f"prior={candidate['retrieval_prior']:.3f} "
                f"graph={candidate['graph_score']:.3f} "
                f"final={candidate['final_score']:.3f} "
                f"primary={candidate['graph_breakdown']['primary_matches']} "
                f"missing={candidate['graph_breakdown']['missing_anchor_edges']}"
            )


if __name__ == "__main__":
    main()
