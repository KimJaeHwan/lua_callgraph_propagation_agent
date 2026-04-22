#!/usr/bin/env python3
"""
Build a propagation suite JSON for one real query feature file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build runtime propagation suite from bulk retrieval output."
    )
    parser.add_argument("--retrieval-json", type=Path, required=True)
    parser.add_argument("--anchor-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--reference-db", type=Path, default=PROJECT_ROOT / "data" / "inputs" / "callgraphs" / "reference_callgraph.sqlite")
    parser.add_argument("--embedding-project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--propagation-output-json", type=Path, required=True)
    return parser.parse_args()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    args = parse_args()
    retrieval = load_json(args.retrieval_json)
    cases = []
    for row in retrieval.get("cases", []):
        cases.append(
            {
                "case_id": row["case_id"],
                "expected_function": None,
            }
        )

    output = {
        "schema_version": "0.1",
        "suite_name": "runtime_name_mapping_suite",
        "description": "Runtime propagation suite generated for one target query binary.",
        "embedding_project_root": str(args.embedding_project_root.resolve()),
        "retrieval_result_json": str(args.retrieval_json.resolve()),
        "anchor_json": str(args.anchor_json.resolve()),
        "reference_db": str(args.reference_db.resolve()),
        "output_json": str(args.propagation_output_json.resolve()),
        "candidate_source": "unique_topk_preview",
        "output_top_candidates": 5,
        "anchor_policy": {
            "type": "seed_anchors_only",
            "description": "Use only explicit seed anchors selected from retrieval confidence thresholds.",
            "allow_visible_reference_name_anchors": False,
            "exclude_prefixes": ["FUNC_", "FUN_", "sub_"],
        },
        "candidate_expansion": {
            "default_prior": 0.65,
            "max_candidates_per_case": 80
        },
        "scoring": {
            "primary_opt": "O0",
            "strip_mode": "nostrip"
        },
        "classification_policy": {
            "accept_margin": 0.015,
            "min_primary_matches": 1,
            "max_tied_top_candidates": 1
        },
        "cases": cases,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] saved runtime propagation suite: {args.output_json}")
    print(f"[INFO] suite cases: {len(cases)}")


if __name__ == "__main__":
    main()
