#!/usr/bin/env python3
"""
Select high-confidence seed anchors from bulk retrieval output.

This gives the propagation step a deterministic starting point when a real
binary has no manually prepared anchor file yet.

Two anchor sources (in priority order):
  1. name_visible  — query functions whose names were NOT stripped and match a
                     known Lua reference function name.  Confidence = 1.0.
  2. retrieval_high_confidence — retrieval top-1 score above threshold.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

# Ghidra auto-generated names that indicate the symbol was stripped
_GHIDRA_STRIPPED = re.compile(
    r"^(FUN_|thunk_FUN_|DAT_|LAB_|UNK_|PTR_|ARRAY_|SWITCH_|CASE_|BYTE_|WORD_|DWORD_|QWORD_)",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build seed anchors from retrieval confidence thresholds."
    )
    parser.add_argument("--retrieval-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--min-top1-score", type=float, default=0.92)
    parser.add_argument("--min-margin", type=float, default=0.05)
    # optional: visible-name detection
    parser.add_argument(
        "--query-json",
        type=Path,
        default=None,
        help="Query feature JSON produced by the extractor (contains function_name per func)",
    )
    parser.add_argument(
        "--reference-db",
        type=Path,
        default=None,
        help="Reference callgraph SQLite DB for validating visible names",
    )
    return parser.parse_args()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_reference_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute("SELECT DISTINCT function_name FROM functions")
        return {row[0] for row in cur.fetchall()}
    except Exception:
        # fallback: try nodes table used by some schema versions
        try:
            cur = con.execute("SELECT DISTINCT name FROM nodes")
            return {row[0] for row in cur.fetchall()}
        except Exception:
            return set()
    finally:
        con.close()


def _is_ghidra_stripped(name: str) -> bool:
    return bool(_GHIDRA_STRIPPED.match(name))


def detect_visible_name_anchors(
    query_json_path: Path,
    reference_db_path: Path,
    already_registered: set[str],
) -> list[dict]:
    reference_names = _load_reference_names(reference_db_path)
    if not reference_names:
        print("[WARN] reference DB returned no function names — skipping visible-name detection")
        return []

    query_data = load_json(query_json_path)
    # query JSON can be a list of functions or {"functions": [...]}
    if isinstance(query_data, list):
        functions = query_data
    else:
        functions = query_data.get("functions", query_data.get("results", []))

    anchors: list[dict] = []
    for func in functions:
        name = func.get("function_name", "")
        if not name:
            continue
        # skip Ghidra auto-generated names (stripped)
        if _is_ghidra_stripped(name):
            continue
        if name in already_registered:
            continue

        if name not in reference_names:
            continue

        entry_point = func.get("entry_point", "")
        query_func_id = entry_point if entry_point else name
        anchors.append(
            {
                "query_function_name": query_func_id,
                "reference_function_name": name,
                "confidence": 1.0,
                "source": "name_visible",
                "status": "accepted",
                "evidence": [f"ghidra_symbol_name_exact_match: {name}"],
            }
        )

    print(f"[INFO] visible-name anchors detected: {len(anchors)}")
    return anchors


def main() -> None:
    args = parse_args()
    source = load_json(args.retrieval_json)

    # ── 1. Visible-name anchors (confidence 1.0, highest priority) ──────────
    visible_anchors: list[dict] = []
    if args.query_json and args.reference_db:
        if args.query_json.exists() and args.reference_db.exists():
            visible_anchors = detect_visible_name_anchors(
                args.query_json, args.reference_db, already_registered=set()
            )
        else:
            if not args.query_json.exists():
                print(f"[WARN] --query-json not found: {args.query_json}")
            if not args.reference_db.exists():
                print(f"[WARN] --reference-db not found: {args.reference_db}")

    already_query_funcs = {a["query_function_name"] for a in visible_anchors}

    # ── 2. Retrieval-based anchors ───────────────────────────────────────────
    retrieval_anchors: list[dict] = []
    for case in source.get("cases", []):
        preview = case.get("unique_topk_preview", [])
        if not preview:
            continue
        top1 = preview[0]
        score1 = float(top1.get("score_total", 0.0))
        score2 = float(preview[1].get("score_total", 0.0)) if len(preview) > 1 else 0.0
        margin = score1 - score2
        if score1 < args.min_top1_score or margin < args.min_margin:
            continue
        qfunc = case.get("query_func")
        if qfunc in already_query_funcs:
            # visible-name already covers this query function — skip
            continue
        retrieval_anchors.append(
            {
                "query_function_name": qfunc,
                "reference_function_name": top1.get("function_name"),
                "confidence": round(score1, 6),
                "source": "retrieval_high_confidence",
                "status": "accepted",
                "evidence": [
                    f"top1_score={score1:.6f}",
                    f"top1_margin={margin:.6f}",
                ],
            }
        )

    # visible anchors first so propagation treats them as the firmest seeds
    mappings = visible_anchors + retrieval_anchors

    output = {
        "schema_version": "0.1",
        "description": "Auto-selected seed anchors (name_visible + retrieval_high_confidence).",
        "thresholds": {
            "min_top1_score": args.min_top1_score,
            "min_margin": args.min_margin,
        },
        "mappings": mappings,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] saved seed anchors: {args.output_json}")
    print(f"[INFO] total anchors: {len(mappings)}  (visible={len(visible_anchors)}, retrieval={len(retrieval_anchors)})")


if __name__ == "__main__":
    main()
