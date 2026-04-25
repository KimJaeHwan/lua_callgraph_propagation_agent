#!/usr/bin/env python3
"""
Export one compact final report from propagation and deferred analysis.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build final compact runtime name-mapping report."
    )
    parser.add_argument("--propagation-json", type=Path, required=True)
    parser.add_argument("--deferred-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--session-name", required=True)
    return parser.parse_args()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_mapping_record(row: dict, section: str) -> dict:
    top_candidates = row.get("top_candidates") or []
    top_candidate = top_candidates[0] if top_candidates else {}
    return {
        "case_id": row.get("case_id"),
        "query_func": row.get("query_func"),
        "query_file": row.get("query_file"),
        "architecture": row.get("architecture"),
        "predicted_function_name": row.get("predicted_function_name"),
        "status": row.get("status"),
        "status_reasons": row.get("status_reasons"),
        "section": section,
        "expected_final_rank": row.get("expected_final_rank"),
        "top1_hit": row.get("top1_hit"),
        "propagation_round": row.get("propagation_round"),
        "decision_trace": {
            "anchor_summary": row.get("anchor_summary"),
            "top_tied_candidates": row.get("top_tied_candidates"),
            "top_candidate": {
                "candidate_function_name": top_candidate.get("candidate_function_name"),
                "candidate_source": top_candidate.get("candidate_source"),
                "retrieval_prior": top_candidate.get("retrieval_prior"),
                "graph_score": top_candidate.get("graph_score"),
                "final_score": top_candidate.get("final_score"),
                "graph_breakdown": top_candidate.get("graph_breakdown"),
                "evidence": top_candidate.get("evidence"),
            },
        },
        "provenance": {
            "decision_source": "propagation_result",
            "reverse_validation_ready": True,
        },
    }


def main() -> None:
    args = parse_args()
    propagation = load_json(args.propagation_json)
    deferred = load_json(args.deferred_json)

    accepted = []
    deferred_rows = []
    conflicts = []
    mapping_records = []

    for row in tqdm(propagation.get("results", []), desc="build final report", unit="case"):
        compact = {
            "case_id": row.get("case_id"),
            "query_func": row.get("query_func"),
            "query_file": row.get("query_file"),
            "architecture": row.get("architecture"),
            "predicted_function_name": row.get("predicted_function_name"),
            "status": row.get("status"),
            "status_reasons": row.get("status_reasons"),
            "expected_final_rank": row.get("expected_final_rank"),
            "propagation_round": row.get("propagation_round"),
        }
        if row.get("status") == "accepted":
            accepted.append(compact)
            mapping_records.append(build_mapping_record(row, "accepted"))
        elif row.get("status") == "conflict":
            conflicts.append(compact)
            mapping_records.append(build_mapping_record(row, "conflicts"))
        else:
            deferred_rows.append(compact)
            mapping_records.append(build_mapping_record(row, "deferred"))

    output = {
        "schema_version": "0.1",
        "session_name": args.session_name,
        "description": "Compact final runtime name-mapping report.",
        "summary": {
            "accepted": len(accepted),
            "deferred": len(deferred_rows),
            "conflict": len(conflicts),
        },
        "accepted": accepted,
        "deferred": deferred_rows,
        "conflicts": conflicts,
        "mapping_records": mapping_records,
        "deferred_analysis_summary": deferred.get("summary"),
        "round_log": propagation.get("round_log", []),
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] saved final mapping report: {args.output_json}")
    print(json.dumps(output["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
