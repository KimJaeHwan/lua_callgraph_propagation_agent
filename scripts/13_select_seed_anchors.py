#!/usr/bin/env python3
"""
Select high-confidence seed anchors from bulk retrieval output.

Two anchor sources (in priority order):
  1. name_visible             — query functions whose names were NOT stripped
                                and match a known Lua reference function name.
                                Confidence = 1.0.
  2. retrieval_high_confidence — retrieval top-1 score above threshold, after
                                 dedup-first filtering and optional scope gating.

Key improvements over the original version
-------------------------------------------
DEDUP-FIRST (always enabled)
  The original version accepted every query function that crossed the score
  threshold, even if many functions all mapped to the same reference name.
  This caused reference names like 'luaD_poscall' or 'match' to appear as
  seed anchors for dozens of game-code functions, poisoning propagation.

  Now: after collecting all threshold-passing candidates, we keep at most ONE
  per reference name — the highest-scoring query function.  Any reference name
  that appears more than --dedup-max-per-ref times in the raw candidates is
  treated as inherently ambiguous and rejected entirely.

SCOPE GATE (optional, --scope-json)
  If a Lua scope JSON produced by 12b_detect_lua_scope.py is provided, only
  query functions that appear in that scope are eligible for
  retrieval_high_confidence anchors.  This prevents game-code functions from
  ever becoming seeds, regardless of how high their retrieval score is.

Usage
-----
  # Minimal (original behaviour, but with dedup-first)
  python scripts/13_select_seed_anchors.py \\
      --retrieval-json data/runtime/results/.../retrieval_result.json \\
      --output-json    data/runtime/results/.../seed_anchors.json

  # Recommended (dedup + scope gate)
  python scripts/13_select_seed_anchors.py \\
      --retrieval-json data/runtime/results/.../retrieval_result.json \\
      --output-json    data/runtime/results/.../seed_anchors.json \\
      --scope-json     data/runtime/results/.../lua_scope.json \\
      --scope-min-confidence medium \\
      --query-json     data/runtime/query_features/.../libengine.json \\
      --reference-db   data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

_GHIDRA_STRIPPED = re.compile(
    r"^(FUN_|thunk_FUN_|DAT_|LAB_|UNK_|PTR_|ARRAY_|SWITCH_|CASE_|BYTE_|WORD_|DWORD_|QWORD_)",
    re.IGNORECASE,
)

# Confidence ordering for scope gate filtering
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build seed anchors from retrieval confidence thresholds."
    )
    parser.add_argument("--retrieval-json", type=Path, required=True)
    parser.add_argument("--output-json",    type=Path, required=True)
    parser.add_argument("--min-top1-score", type=float, default=0.92)
    parser.add_argument("--min-margin",     type=float, default=0.05)

    # Dedup-first
    parser.add_argument(
        "--dedup-max-per-ref", type=int, default=1,
        help="Maximum number of query functions allowed to map to the same "
             "reference name as retrieval_high_confidence seeds. "
             "Reference names with more raw candidates than this are rejected. "
             "Default 1 (strictest: keep only unambiguous 1:1 mappings).",
    )

    # Scope gate
    parser.add_argument(
        "--scope-json", type=Path, default=None,
        help="Lua scope JSON from 12b_detect_lua_scope.py.  "
             "When provided, only functions in this scope are eligible for "
             "retrieval_high_confidence seed selection.",
    )
    parser.add_argument(
        "--scope-min-confidence", default="low",
        choices=["high", "medium", "low"],
        help="Minimum scope confidence level to pass the gate (default: low). "
             "Use 'medium' or 'high' to be more conservative.",
    )

    # Targeted retrieval (12c output)
    parser.add_argument(
        "--targeted-json", type=Path, default=None,
        help="Targeted retrieval JSON from 12c_targeted_retrieval.py. "
             "Processed as a third anchor source ('targeted_high_confidence') "
             "with its own, lower score threshold.",
    )
    parser.add_argument(
        "--targeted-min-score", type=float, default=0.75,
        help="Minimum vote_score for targeted_high_confidence anchors (default 0.75). "
             "Lower than regular retrieval threshold since structural evidence is strong.",
    )
    parser.add_argument(
        "--targeted-min-margin", type=float, default=0.15,
        help="Minimum score gap between top-1 and top-2 targeted candidates (default 0.15).",
    )

    # Visible-name detection
    parser.add_argument(
        "--query-json", type=Path, default=None,
        help="Query feature JSON for visible-name anchor detection.",
    )
    parser.add_argument(
        "--reference-db", type=Path, default=None,
        help="Reference callgraph SQLite DB for validating visible names.",
    )
    return parser.parse_args()


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Visible-name helpers (unchanged) ──────────────────────────────────────────

def _load_reference_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute("SELECT DISTINCT function_name FROM functions")
        return {row[0] for row in cur.fetchall()}
    except Exception:
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
    if isinstance(query_data, dict) and "feature_files" in query_data:
        functions: list[dict] = []
        for ff_path_str in query_data.get("feature_files", []):
            ff_path = Path(ff_path_str)
            if not ff_path.exists():
                print(f"[WARN] feature file not found, skipping: {ff_path}")
                continue
            try:
                ff_data = load_json(ff_path)
            except Exception as exc:
                print(f"[WARN] could not read feature file {ff_path}: {exc}")
                continue
            if isinstance(ff_data, list):
                functions.extend(ff_data)
            else:
                functions.extend(ff_data.get("functions", ff_data.get("results", [])))
        print(f"[INFO] loaded {len(functions)} function records from manifest")
    elif isinstance(query_data, list):
        functions = query_data
    else:
        functions = query_data.get("functions", query_data.get("results", []))

    anchors: list[dict] = []
    for func in functions:
        name = func.get("function_name", "")
        if not name or _is_ghidra_stripped(name) or name in already_registered:
            continue
        if name not in reference_names:
            continue
        anchors.append({
            "query_function_name": name,
            "reference_function_name": name,
            "confidence": 1.0,
            "source": "name_visible",
            "status": "accepted",
            "evidence": [f"ghidra_symbol_name_exact_match: {name}"],
        })

    print(f"[INFO] visible-name anchors detected: {len(anchors)}")
    return anchors


def _load_preserved_anchors(output_json: Path) -> list[dict]:
    AUTO_SOURCES = {"retrieval_high_confidence", "name_visible"}
    if not output_json.exists():
        return []
    try:
        data = load_json(output_json)
        return [
            m for m in data.get("mappings", [])
            if m.get("source") not in AUTO_SOURCES and m.get("status") == "accepted"
        ]
    except Exception as exc:
        print(f"[WARN] could not read existing anchor file ({exc}); starting fresh")
        return []


# ── Scope gate helper ──────────────────────────────────────────────────────────

def _load_scope_set(scope_json_path: Path, min_confidence: str) -> set[str]:
    """Return the set of function names that pass the scope confidence gate."""
    data = load_json(scope_json_path)
    min_rank = _CONFIDENCE_RANK.get(min_confidence, 1)
    scope_funcs = data.get("functions", {})
    return {
        name
        for name, info in scope_funcs.items()
        if _CONFIDENCE_RANK.get(info.get("confidence", "low"), 1) >= min_rank
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    source = load_json(args.retrieval_json)

    # ── 0. Preserve manually registered anchors ───────────────────────────────
    preserved_anchors = _load_preserved_anchors(args.output_json)
    preserved_names   = {m["query_function_name"] for m in preserved_anchors}
    if preserved_anchors:
        print(f"[INFO] preserving {len(preserved_anchors)} manually registered anchor(s)")

    # ── 1. Visible-name anchors (confidence 1.0) ──────────────────────────────
    visible_anchors: list[dict] = []
    if args.query_json and args.reference_db:
        if args.query_json.exists() and args.reference_db.exists():
            visible_anchors = detect_visible_name_anchors(
                args.query_json, args.reference_db,
                already_registered=preserved_names,
            )
        else:
            if not args.query_json.exists():
                print(f"[WARN] --query-json not found: {args.query_json}")
            if not args.reference_db.exists():
                print(f"[WARN] --reference-db not found: {args.reference_db}")

    already_query_funcs = preserved_names | {a["query_function_name"] for a in visible_anchors}

    # ── 2. Load scope gate (optional) ────────────────────────────────────────
    lua_scope: set[str] | None = None
    if args.scope_json and args.scope_json.exists():
        lua_scope = _load_scope_set(args.scope_json, args.scope_min_confidence)
        print(f"[INFO] scope gate loaded: {len(lua_scope)} functions "
              f"(min_confidence={args.scope_min_confidence})")
    elif args.scope_json:
        print(f"[WARN] --scope-json not found: {args.scope_json} — scope gate disabled")

    # ── 3. Collect raw retrieval candidates ───────────────────────────────────
    #   Collect ALL threshold-passing candidates first, before any dedup.
    raw_candidates: list[dict] = []
    rejected_scope = 0
    rejected_threshold = 0

    for case in source.get("cases", []):
        preview = case.get("unique_topk_preview", [])
        if not preview:
            continue

        top1   = preview[0]
        score1 = float(top1.get("score_total", 0.0))
        score2 = float(preview[1].get("score_total", 0.0)) if len(preview) > 1 else 0.0
        margin = score1 - score2

        qfunc    = case.get("query_func")
        ref_name = top1.get("function_name")

        if not qfunc or not ref_name:
            continue
        if qfunc in already_query_funcs:
            continue

        # Threshold gate
        if score1 < args.min_top1_score or margin < args.min_margin:
            rejected_threshold += 1
            continue

        # Scope gate
        if lua_scope is not None and qfunc not in lua_scope:
            rejected_scope += 1
            continue

        raw_candidates.append({
            "query_func": qfunc,
            "ref_func":   ref_name,
            "score":      score1,
            "margin":     margin,
        })

    print(f"[INFO] raw candidates passing threshold: {len(raw_candidates)}")
    if lua_scope is not None:
        print(f"[INFO] rejected by scope gate: {rejected_scope}")

    # ── 4. DEDUP-FIRST: keep best query per reference name ───────────────────
    #   Group by reference name, keep highest-scoring query.
    #   Reject if a reference name has more than dedup_max_per_ref candidates
    #   (highly ambiguous → likely noise).
    ref_to_candidates: dict[str, list[dict]] = defaultdict(list)
    for cand in raw_candidates:
        ref_to_candidates[cand["ref_func"]].append(cand)

    retrieval_anchors: list[dict] = []
    dedup_rejected_ambiguous = 0
    dedup_accepted = 0

    for ref_name, candidates in ref_to_candidates.items():
        if len(candidates) > args.dedup_max_per_ref:
            # Too many query functions match this reference name → noise
            dedup_rejected_ambiguous += 1
            continue

        # Keep the highest-scoring candidate for this reference name
        best = max(candidates, key=lambda c: c["score"])

        if best["query_func"] in already_query_funcs:
            continue

        retrieval_anchors.append({
            "query_function_name":     best["query_func"],
            "reference_function_name": ref_name,
            "confidence":              round(best["score"], 6),
            "source":                  "retrieval_high_confidence",
            "status":                  "accepted",
            "evidence": [
                f"top1_score={best['score']:.6f}",
                f"top1_margin={best['margin']:.6f}",
                f"dedup_candidates={len(candidates)}",
            ],
        })
        dedup_accepted += 1

    print(f"[INFO] dedup-first results:")
    print(f"         raw candidates:            {len(raw_candidates)}")
    print(f"         rejected (ambiguous ref):  {dedup_rejected_ambiguous}")
    print(f"         accepted (1:1 after dedup): {dedup_accepted}")

    # ── 4b. Targeted retrieval anchors (12c output, optional) ────────────────
    #   Processed AFTER regular retrieval so already_query_funcs stays fresh.
    already_query_funcs = (
        already_query_funcs
        | {a["query_function_name"] for a in retrieval_anchors}
    )

    targeted_anchors: list[dict] = []
    if args.targeted_json and args.targeted_json.exists():
        targeted_source = load_json(args.targeted_json)
        targeted_raw: list[dict] = []
        targeted_rejected_scope = 0
        targeted_rejected_threshold = 0

        for case in targeted_source.get("cases", []):
            preview = case.get("unique_topk_preview", [])
            if not preview:
                continue
            top1   = preview[0]
            score1 = float(top1.get("score_total", 0.0))
            score2 = float(preview[1].get("score_total", 0.0)) if len(preview) > 1 else 0.0
            margin = score1 - score2
            qfunc    = case.get("query_func")
            ref_name = top1.get("function_name")

            if not qfunc or not ref_name:
                continue
            if qfunc in already_query_funcs:
                continue

            # Threshold gate (lower bar than regular retrieval)
            if score1 < args.targeted_min_score or margin < args.targeted_min_margin:
                targeted_rejected_threshold += 1
                continue

            # Scope gate (same as regular retrieval)
            if lua_scope is not None and qfunc not in lua_scope:
                targeted_rejected_scope += 1
                continue

            targeted_raw.append({
                "query_func":   qfunc,
                "ref_func":     ref_name,
                "score":        score1,
                "margin":       margin,
                "voter_count":  case.get("voter_count", 0),
            })

        # Dedup-first (same policy as regular retrieval)
        targeted_ref_to_cands: dict[str, list[dict]] = defaultdict(list)
        for cand in targeted_raw:
            targeted_ref_to_cands[cand["ref_func"]].append(cand)

        targeted_dedup_rejected = 0
        targeted_dedup_accepted = 0
        for ref_name, candidates in targeted_ref_to_cands.items():
            if len(candidates) > args.dedup_max_per_ref:
                targeted_dedup_rejected += 1
                continue
            best = max(candidates, key=lambda c: c["score"])
            if best["query_func"] in already_query_funcs:
                continue
            targeted_anchors.append({
                "query_function_name":     best["query_func"],
                "reference_function_name": ref_name,
                "confidence":              round(best["score"], 6),
                "source":                  "targeted_high_confidence",
                "status":                  "accepted",
                "evidence": [
                    f"vote_score={best['score']:.6f}",
                    f"margin={best['margin']:.6f}",
                    f"voter_count={best['voter_count']}",
                ],
            })
            targeted_dedup_accepted += 1

        print(f"[INFO] targeted retrieval results:")
        print(f"         raw cases:                  {len(targeted_raw)}")
        print(f"         rejected (threshold):       {targeted_rejected_threshold}")
        if lua_scope is not None:
            print(f"         rejected (scope gate):     {targeted_rejected_scope}")
        print(f"         rejected (ambiguous ref):   {targeted_dedup_rejected}")
        print(f"         accepted:                   {targeted_dedup_accepted}")
    elif args.targeted_json:
        print(f"[WARN] --targeted-json not found: {args.targeted_json} — skipped")

    # ── 5. Assemble and save ──────────────────────────────────────────────────
    mappings = preserved_anchors + visible_anchors + retrieval_anchors + targeted_anchors

    output = {
        "schema_version": "0.1",
        "description": "Auto-selected seed anchors (dedup-first + optional scope gate + targeted).",
        "thresholds": {
            "min_top1_score":       args.min_top1_score,
            "min_margin":           args.min_margin,
            "dedup_max_per_ref":    args.dedup_max_per_ref,
            "scope_json":           str(args.scope_json) if args.scope_json else None,
            "scope_min_confidence": args.scope_min_confidence,
            "targeted_json":        str(args.targeted_json) if args.targeted_json else None,
            "targeted_min_score":   args.targeted_min_score,
            "targeted_min_margin":  args.targeted_min_margin,
        },
        "stats": {
            "preserved":                    len(preserved_anchors),
            "visible":                      len(visible_anchors),
            "retrieval_raw":                len(raw_candidates),
            "retrieval_rejected_scope":     rejected_scope,
            "retrieval_rejected_ambiguous": dedup_rejected_ambiguous,
            "retrieval_accepted":           dedup_accepted,
            "targeted_accepted":            len(targeted_anchors),
        },
        "mappings": mappings,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] saved seed anchors: {args.output_json}")
    print(f"[INFO] total anchors: {len(mappings)}"
          f"  (preserved={len(preserved_anchors)}"
          f", visible={len(visible_anchors)}"
          f", retrieval={len(retrieval_anchors)}"
          f", targeted={len(targeted_anchors)})")


if __name__ == "__main__":
    main()
