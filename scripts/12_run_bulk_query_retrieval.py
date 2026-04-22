#!/usr/bin/env python3
"""
Run retrieval for every function inside one extracted query feature JSON.

This wrapper keeps the runtime flow inside lua_callgraph_propagation_agent while
loading the vendored hybrid retrieval implementation from this repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RETRIEVAL_SCRIPT = (
    PROJECT_ROOT
    / "src"
    / "lua_callgraph_propagation_agent"
    / "vendor"
    / "hybrid_retrieval_embedding.py"
).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run retrieval for all query functions in one feature JSON."
    )
    parser.add_argument("--query-json", type=Path, default=None)
    parser.add_argument("--extract-manifest", type=Path, default=None)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--retrieval-script", type=Path, default=DEFAULT_RETRIEVAL_SCRIPT)
    parser.add_argument("--candidate-pool", type=int, default=200)
    parser.add_argument("--topk", type=int, default=50)
    parser.add_argument("--scoring-mode", choices=["jaccard", "bonus", "bonus_v2"], default="bonus_v2")
    parser.add_argument("--mode", default="runtime_query")
    return parser.parse_args()


def load_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("runtime_hybrid_retrieval_module", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load retrieval module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def collapse_by_function_name(results: list[dict]) -> list[dict]:
    seen = set()
    collapsed = []
    for row in results:
        name = row.get("function_name")
        if not name or name in seen:
            continue
        seen.add(name)
        collapsed.append(row)
    return collapsed


def main() -> None:
    args = parse_args()
    module = load_module(args.retrieval_script.resolve())
    index = module.load_index(args.index.resolve())

    query_json = args.query_json
    if query_json is None and args.extract_manifest is None:
        raise SystemExit("Either --query-json or --extract-manifest is required.")
    if query_json is None:
        with open(args.extract_manifest, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        feature_files = manifest_data.get("feature_files") or []
        if not feature_files:
            raise SystemExit(f"No feature files in manifest: {args.extract_manifest}")
        query_json = Path(feature_files[0]).resolve()

    with open(query_json, "r", encoding="utf-8") as f:
        rows = json.load(f)

    if not isinstance(rows, list):
        raise SystemExit(f"Expected list in query JSON: {args.query_json}")

    cases = []
    for row in rows:
        query_func = row.get("function_name")
        if not query_func:
            continue

        query = module.build_query_record_from_file(query_json.resolve(), query_func)
        raw_results = module.search_index(
            index=index,
            query_record=query,
            topk=max(args.topk, 50),
            exclude_same_id=False,
            candidate_pool=args.candidate_pool,
            scoring_mode=args.scoring_mode,
        )
        unique_results = collapse_by_function_name(raw_results)
        entry_point = row.get("entry_point") or "unknown"
        case_id = f"{query_func}@{entry_point}"

        cases.append(
            {
                "case_id": case_id,
                "mode": args.mode,
                "query_file": str(query_json.resolve()),
                "query_func": query_func,
                "expected_function": None,
                "topk": args.topk,
                "raw_top1_function": raw_results[0]["function_name"] if raw_results else "",
                "raw_top1_hit": None,
                "raw_topk_hit": None,
                "unique_top1_function": unique_results[0]["function_name"] if unique_results else "",
                "unique_top1_hit": None,
                "unique_topk_hit": None,
                "raw_topk_preview": [
                    {
                        "function_name": item["function_name"],
                        "score_total": item["score_total"],
                        "source_json": item["source_json"],
                    }
                    for item in raw_results[: args.topk]
                ],
                "unique_topk_preview": [
                    {
                        "function_name": item["function_name"],
                        "score_total": item["score_total"],
                        "source_json": item["source_json"],
                    }
                    for item in unique_results[: args.topk]
                ],
            }
        )
        print(f"[OK] retrieval: {case_id} -> {len(unique_results[:args.topk])} unique candidates")

    result = {
        "suite_name": "runtime_bulk_query_retrieval",
        "description": "Bulk retrieval output for one extracted query feature JSON.",
        "scoring_mode": args.scoring_mode,
        "candidate_pool": args.candidate_pool,
        "summary": {
            "num_cases": len(cases),
        },
        "cases": cases,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"[OK] saved retrieval result: {args.output_json}")
    print(f"[INFO] query feature source: {query_json}")


if __name__ == "__main__":
    main()
