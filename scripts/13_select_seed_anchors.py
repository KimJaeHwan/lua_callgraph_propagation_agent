#!/usr/bin/env python3
"""
Select high-confidence seed anchors from bulk retrieval output.

This gives the propagation step a deterministic starting point when a real
binary has no manually prepared anchor file yet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build seed anchors from retrieval confidence thresholds."
    )
    parser.add_argument("--retrieval-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--min-top1-score", type=float, default=0.92)
    parser.add_argument("--min-margin", type=float, default=0.05)
    return parser.parse_args()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    args = parse_args()
    source = load_json(args.retrieval_json)
    mappings = []

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
        mappings.append(
            {
                "query_function_name": case.get("query_func"),
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

    output = {
        "schema_version": "0.1",
        "description": "Auto-selected seed anchors from high-confidence retrieval output.",
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
    print(f"[INFO] selected anchors: {len(mappings)}")


if __name__ == "__main__":
    main()
