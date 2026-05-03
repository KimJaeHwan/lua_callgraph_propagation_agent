#!/usr/bin/env python3
"""
Run retrieval for every function inside one extracted query feature JSON.

Optimization: all semantic texts are encoded in a single batch call before
the scoring loop, so the embedding model runs once (not once per function).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RETRIEVAL_SCRIPT = (
    PROJECT_ROOT
    / "src"
    / "lua_callgraph_propagation_agent"
    / "vendor"
    / "hybrid_retrieval_embedding.py"
).resolve()


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


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
    # Scope filter — only encode/retrieve functions inside the detected Lua scope
    parser.add_argument(
        "--scope-json", type=Path, default=None,
        help="lua_scope.json from 12b_detect_lua_scope.py. When provided, only functions "
             "that appear in this scope are passed to the embedding model and retrieval index. "
             "Reduces encoding from ~16k to ~800 functions (20x speedup) and eliminates "
             "game-code false positives at the retrieval stage.",
    )
    parser.add_argument(
        "--scope-min-confidence", default="low",
        choices=["high", "medium", "low"],
        help="Minimum scope confidence level to pass the filter (default: low).",
    )
    return parser.parse_args()


def _load_scope_set(scope_json: Path, min_confidence: str) -> set[str]:
    """Return function names that pass the scope confidence gate."""
    with open(scope_json, encoding="utf-8") as f:
        data = json.load(f)
    min_rank = _CONFIDENCE_RANK.get(min_confidence, 1)
    return {
        name
        for name, info in data.get("functions", {}).items()
        if _CONFIDENCE_RANK.get(info.get("confidence", "low"), 1) >= min_rank
    }


def load_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("runtime_hybrid_retrieval_module", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load retrieval module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def collapse_by_function_name(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
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
    print(f"[INFO] loading retrieval index: {args.index.resolve()}")
    index = module.load_index(args.index.resolve())
    print(f"[INFO] retrieval index loaded: {args.index.resolve()}")

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

    print(f"[INFO] loading query feature JSON: {query_json}")
    with open(query_json, "r", encoding="utf-8") as f:
        rows = json.load(f)

    if not isinstance(rows, list):
        raise SystemExit(f"Expected list in query JSON: {query_json}")

    valid_rows = [r for r in rows if r.get("function_name")]
    print(f"[INFO] total functions in feature JSON: {len(valid_rows)}")

    # ── Scope filter (optional) ───────────────────────────────────────────────
    if args.scope_json:
        if not args.scope_json.exists():
            print(f"[WARN] --scope-json not found: {args.scope_json} — scope filter disabled")
        else:
            scope_set = _load_scope_set(args.scope_json, args.scope_min_confidence)
            before = len(valid_rows)
            valid_rows = [r for r in valid_rows if r["function_name"] in scope_set]
            skipped = before - len(valid_rows)
            print(
                f"[INFO] scope filter applied ({args.scope_min_confidence} confidence): "
                f"{len(valid_rows)} in scope, {skipped} skipped "
                f"({skipped/before*100:.1f}% of binary excluded from retrieval)"
            )

    print(f"[INFO] building query records for {len(valid_rows)} functions...")

    # ── 1. 모든 query record 빌드 ──────────────────────────────────────────────
    query_records = []
    for row in valid_rows:
        qr = module.build_query_record_from_file(query_json.resolve(), row["function_name"])
        query_records.append(qr)

    # ── 2. 전체 semantic text 배치 인코딩 (모델 1회 호출) ─────────────────────
    print(f"[INFO] batch encoding {len(query_records)} semantic texts...")
    model = module.load_embedding_model(index.semantic_model_name)
    import numpy as np
    all_texts = [qr.semantic_text for qr in query_records]
    all_embeddings = model.encode(
        all_texts,
        batch_size=64,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32)
    print(f"[INFO] encoding done. running retrieval for {len(query_records)} functions...")

    # ── 3. 함수별 검색 (embedding 재사용) ─────────────────────────────────────
    cases = []
    for idx, (row, qr) in enumerate(
        tqdm(
            zip(valid_rows, query_records),
            total=len(valid_rows),
            desc="  retrieval",
            unit="func",
            ncols=72,
            file=sys.stdout,
            mininterval=2.0,
        )
    ):
        raw_results = module.search_index_with_embedding(
            index=index,
            query_record=qr,
            query_embedding=all_embeddings[idx],
            topk=max(args.topk, 50),
            exclude_same_id=False,
            candidate_pool=args.candidate_pool,
            scoring_mode=args.scoring_mode,
        )
        unique_results = collapse_by_function_name(raw_results)
        entry_point = row.get("entry_point") or "unknown"
        case_id = f"{qr.function_name}@{entry_point}"

        cases.append(
            {
                "case_id": case_id,
                "mode": args.mode,
                "query_file": str(query_json.resolve()),
                "query_func": qr.function_name,
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
