#!/usr/bin/env python3
"""
12c_targeted_retrieval.py

Targeted retrieval using callgraph neighbor constraints with optional
reference-string tie-breaking.

For each unconfirmed query function Q that shares a callgraph edge with at
least one confirmed anchor, restricts the candidate pool to the reference
callgraph neighbors of those anchors and scores by vote consensus.

No embedding model required - primarily structural evidence, with a small
reference string bonus used only to break ties between structurally similar
candidates.

Scoring (vote_score)
--------------------
  • Q calls A  (A confirmed as ref_A)  → Q is a *caller*  of ref_A
       → reference callers  of ref_A are candidates for Q
  • A calls Q  (A confirmed as ref_A)  → Q is a *callee* of ref_A
       → reference callees of ref_A are candidates for Q

  vote_score(Q → N) = confirmed_neighbors_that_vote_for_N
                      / total_confirmed_neighbors_of_Q
  Range 0.0–1.0.  1.0 = all confirmed neighbors unanimously agree.

  score_total = vote_score + string_bonus
  string_bonus is capped and intentionally small, so callgraph structure
  remains the dominant signal while exact/near-exact string overlap can
  resolve ties more reliably.

Output: targeted_retrieval.json  (same "cases" schema as retrieval_result.json
        so 13_select_seed_anchors.py can consume it directly)

Usage
-----
  python scripts/12c_targeted_retrieval.py \\
      --query-json   data/runtime/query_features/.../libengine_patched.json \\
      --anchors-json data/runtime/results/.../seed_anchors.json \\
      --reference-db data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite \\
      --output-json  data/runtime/results/.../targeted_retrieval.json \\
      --lua-version  Lua_536

Notes
-----
  • Use the *patched* query JSON (from patch_features_with_confirmed / 22 runner resume flow)
    so confirmed callee/caller names appear as real Lua names, giving more voting edges.
  • For anchors-json, accepts either seed_anchors.json or propagation_result.json.
    Using propagation_result.json (accepted entries) gives the richest confirmed set.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Targeted retrieval via callgraph neighbor voting (no embeddings)."
    )
    p.add_argument("--query-json",     type=Path, required=True,
                   help="Query feature JSON (list of functions) or extract_manifest.")
    p.add_argument("--anchors-json",   type=Path, required=True,
                   help="seed_anchors.json or propagation_result.json - confirmed mappings.")
    p.add_argument("--reference-db",   type=Path, required=True,
                   help="Reference callgraph SQLite DB.")
    p.add_argument("--output-json",    type=Path, required=True,
                   help="Output path for targeted_retrieval.json.")
    p.add_argument("--topk",           type=int,  default=10,
                   help="Max candidates to emit per query function (default 10).")
    p.add_argument("--min-vote-score", type=float, default=0.0,
                   help="Minimum vote_score to include a case in output (default 0.0 = all).")
    p.add_argument("--min-voters",     type=int,  default=1,
                   help="Minimum confirmed neighbors required to vote (default 1).")
    p.add_argument("--lua-version",    type=str,  default=None,
                   help="Filter reference DB edges by lua_version (e.g. Lua_536). "
                        "Omit to aggregate across all versions (broader neighbor set).")
    p.add_argument("--string-bonus-max", type=float, default=0.08,
                   help="Maximum additive bonus from reference string overlap (default 0.08). "
                        "Used only to break structural ties; vote_score remains dominant.")
    return p.parse_args()


# ── JSON helpers ───────────────────────────────────────────────────────────────

def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def normalize_strings(values: object) -> set[str]:
    if not isinstance(values, list):
        return set()
    out: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip().lower()
        if text:
            out.add(text)
    return out


def string_tokens(strings: set[str]) -> set[str]:
    tokens: set[str] = set()
    for text in strings:
        for token in _TOKEN_RE.findall(text):
            tokens.add(token.lower())
    return tokens


def overlap_bonus(
    query_strings: set[str],
    query_tokens: set[str],
    ref_strings: set[str],
    ref_tokens: set[str],
    max_bonus: float,
) -> tuple[float, int, int]:
    exact_overlap = len(query_strings & ref_strings)
    token_overlap = len(query_tokens & ref_tokens)
    bonus = 0.0
    if exact_overlap >= 1:
        bonus += 0.03
    if exact_overlap >= 2:
        bonus += 0.02
    if token_overlap >= 2:
        bonus += 0.01
    if token_overlap >= 4:
        bonus += 0.01
    return min(bonus, max_bonus), exact_overlap, token_overlap


# ── Confirmed anchor loading ───────────────────────────────────────────────────

def load_confirmed_map(anchors_path: Path) -> dict[str, str]:
    """Return {query_func_name → ref_func_name} from seed_anchors or propagation JSON.

    Handles two formats:
      • seed_anchors.json  : {"mappings": [{query_function_name, reference_function_name, status}]}
      • propagation_result.json: {"results": [{query_func, predicted_function_name, status}]}
    """
    data = _load_json(anchors_path)
    confirmed: dict[str, str] = {}

    if "mappings" in data:
        for m in data["mappings"]:
            if m.get("status") == "accepted":
                q = m.get("query_function_name", "")
                r = m.get("reference_function_name", "")
                if q and r:
                    confirmed[q] = r
        return confirmed

    if "results" in data:
        for r in data["results"]:
            if r.get("status") == "accepted":
                q = r.get("query_func", "")
                n = r.get("predicted_function_name", "")
                if q and n:
                    confirmed[q] = n
        return confirmed

    raise ValueError(
        f"Unrecognised anchors JSON format in {anchors_path}. "
        "Expected 'mappings' (seed_anchors) or 'results' (propagation_result)."
    )


# ── Reference callgraph neighbor index ────────────────────────────────────────

def build_ref_neighbor_index(
    db_path: Path,
    lua_version: str | None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Load edges from reference DB and return (ref_callees, ref_callers).

    ref_callees[func] = set of functions func calls
    ref_callers[func] = set of functions that call func
    """
    con = sqlite3.connect(str(db_path))
    try:
        if lua_version:
            rows = con.execute(
                "SELECT DISTINCT src_name, dst_name FROM edges "
                "WHERE edge_type='calls' AND lua_version=?",
                (lua_version,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT src_name, dst_name FROM edges WHERE edge_type='calls'"
            ).fetchall()
    finally:
        con.close()

    ref_callees: dict[str, set[str]] = defaultdict(set)
    ref_callers: dict[str, set[str]] = defaultdict(set)
    for src, dst in rows:
        ref_callees[src].add(dst)
        ref_callers[dst].add(src)

    return dict(ref_callees), dict(ref_callers)


def build_ref_string_index(
    db_path: Path,
    lua_version: str | None,
) -> dict[str, dict[str, set[str]]]:
    con = sqlite3.connect(str(db_path))
    try:
        if lua_version:
            rows = con.execute(
                "SELECT DISTINCT function_name, string_value FROM function_strings "
                "WHERE lua_version=?",
                (lua_version,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT function_name, string_value FROM function_strings"
            ).fetchall()
    finally:
        con.close()

    by_name: dict[str, set[str]] = defaultdict(set)
    for function_name, string_value in rows:
        if function_name and string_value:
            by_name[function_name].add(str(string_value).strip().lower())

    index: dict[str, dict[str, set[str]]] = {}
    for function_name, strings in by_name.items():
        index[function_name] = {
            "strings": strings,
            "tokens": string_tokens(strings),
        }
    return index


# ── Query feature loading ──────────────────────────────────────────────────────

def load_query_functions(query_json_path: Path) -> list[dict]:
    """Load function list, handling plain list and manifest formats."""
    data = _load_json(query_json_path)
    if isinstance(data, list):
        return data
    if "feature_files" in data:
        funcs: list[dict] = []
        for ff_str in data.get("feature_files", []):
            ff = Path(ff_str)
            if ff.exists():
                try:
                    fd = _load_json(ff)
                    funcs.extend(fd if isinstance(fd, list) else fd.get("functions", []))
                except Exception as exc:
                    print(f"[WARN] could not read feature file {ff}: {exc}")
        return funcs
    return data.get("functions", data.get("results", []))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # ── Load confirmed map ────────────────────────────────────────────────────
    confirmed_map = load_confirmed_map(args.anchors_json)
    print(f"[12c] Confirmed anchors: {len(confirmed_map)}")

    # Build extended lookup that handles BOTH:
    #   FUN_xxx → ref_name  (original Ghidra names)
    #   ref_name → ref_name (real names that appear in patched feature callees/callers)
    all_name_to_ref: dict[str, str] = dict(confirmed_map)
    for ref_name in confirmed_map.values():
        all_name_to_ref[ref_name] = ref_name  # identity for already-real names

    # ── Load query features ────────────────────────────────────────────────────
    funcs = load_query_functions(args.query_json)
    print(f"[12c] Query functions: {len(funcs)}")

    # Index callees/callers by function name
    func_callees: dict[str, list[str]] = {}
    func_callers: dict[str, list[str]] = {}
    func_strings: dict[str, set[str]] = {}
    func_string_tokens: dict[str, set[str]] = {}
    for f in funcs:
        name = f.get("function_name", "")
        if name:
            func_callees[name] = f.get("callees", [])
            func_callers[name] = f.get("callers", [])
            q_strings = normalize_strings(f.get("strings", []))
            func_strings[name] = q_strings
            func_string_tokens[name] = string_tokens(q_strings)

    # ── Build reference neighbor index ────────────────────────────────────────
    print(f"[12c] Building reference neighbor index "
          f"(lua_version={args.lua_version or 'all'})...")
    ref_callees, ref_callers = build_ref_neighbor_index(args.reference_db, args.lua_version)
    ref_string_index = build_ref_string_index(args.reference_db, args.lua_version)
    print(f"[12c] Reference index: {len(ref_callees)} functions with callees, "
          f"{len(ref_callers)} functions with callers")
    print(f"[12c] Reference string index: {len(ref_string_index)} functions with strings")

    confirmed_query_names = set(confirmed_map.keys())

    # ── Core voting loop ──────────────────────────────────────────────────────
    cases: list[dict] = []
    skipped_no_voters = 0

    for func_name in func_callees:
        if func_name in confirmed_query_names:
            continue  # already confirmed, skip

        votes: dict[str, int] = defaultdict(int)   # ref_candidate → vote count
        voter_count = 0
        voter_details: list[dict] = []

        # ── Callees of Q: Q calls them ────────────────────────────────────────
        # Q calls A (confirmed as ref_A)  →  Q is a *caller* of ref_A in reference
        # Candidates for Q = reference callers of ref_A
        for callee_name in func_callees.get(func_name, []):
            ref_callee = all_name_to_ref.get(callee_name)
            if ref_callee is None:
                continue
            candidates = ref_callers.get(ref_callee, set())
            for cand in candidates:
                votes[cand] += 1
            voter_count += 1
            voter_details.append({
                "neighbor":   callee_name,
                "ref":        ref_callee,
                "role":       "callee",          # Q calls this neighbor
                "candidates": len(candidates),
            })

        # ── Callers of Q: they call Q ─────────────────────────────────────────
        # A calls Q (A confirmed as ref_A)  →  Q is a *callee* of ref_A in reference
        # Candidates for Q = reference callees of ref_A
        for caller_name in func_callers.get(func_name, []):
            ref_caller = all_name_to_ref.get(caller_name)
            if ref_caller is None:
                continue
            candidates = ref_callees.get(ref_caller, set())
            for cand in candidates:
                votes[cand] += 1
            voter_count += 1
            voter_details.append({
                "neighbor":   caller_name,
                "ref":        ref_caller,
                "role":       "caller",          # this neighbor calls Q
                "candidates": len(candidates),
            })

        if voter_count < args.min_voters or not votes:
            skipped_no_voters += 1
            continue

        # Score = vote_count / total_voters  (0.0–1.0)
        query_strs = func_strings.get(func_name, set())
        query_toks = func_string_tokens.get(func_name, set())

        scored = []
        for name, cnt in votes.items():
            vote_score = cnt / voter_count
            ref_str_meta = ref_string_index.get(name, {})
            bonus, exact_overlap, token_overlap = overlap_bonus(
                query_strs,
                query_toks,
                ref_str_meta.get("strings", set()),
                ref_str_meta.get("tokens", set()),
                args.string_bonus_max,
            )
            scored.append({
                "function_name": name,
                "vote_score": vote_score,
                "string_bonus": bonus,
                "exact_string_overlap": exact_overlap,
                "token_overlap": token_overlap,
                "score_total": vote_score + bonus,
                "vote_count": cnt,
                "voter_count": voter_count,
            })

        scored.sort(
            key=lambda item: (
                -item["score_total"],
                -item["vote_score"],
                -item["exact_string_overlap"],
                -item["token_overlap"],
                item["function_name"],
            )
        )
        scored = scored[:args.topk]

        top_score = scored[0]["score_total"]
        if top_score < args.min_vote_score:
            continue

        cases.append({
            "query_func":          func_name,
            "voter_count":         voter_count,
            "voter_details":       voter_details,
            "unique_topk_preview": [
                {
                    **item,
                    "score_total": round(item["score_total"], 6),
                    "vote_score": round(item["vote_score"], 6),
                    "string_bonus": round(item["string_bonus"], 6),
                }
                for item in scored
            ],
        })

    # ── Stats ─────────────────────────────────────────────────────────────────
    print(f"[12c] Targeted cases generated: {len(cases)}")
    print(f"[12c] Skipped (no confirmed neighbors): {skipped_no_voters}")
    if cases:
        full_consensus = sum(
            1 for c in cases
            if c["unique_topk_preview"][0]["score_total"] >= 1.0
        )
        high_confidence = sum(
            1 for c in cases
            if c["unique_topk_preview"][0]["score_total"] >= 0.75
        )
        print(f"[12c] vote_score = 1.0 (unanimous): {full_consensus}")
        print(f"[12c] vote_score >= 0.75:            {high_confidence}")

    # ── Save ──────────────────────────────────────────────────────────────────
    output = {
        "schema_version": "0.1",
        "description": (
            "Targeted retrieval via callgraph neighbor voting. "
            "No embedding model required."
        ),
        "stats": {
            "confirmed_anchors":  len(confirmed_map),
            "query_functions":    len(func_callees),
            "targeted_cases":     len(cases),
            "skipped_no_voters":  skipped_no_voters,
        },
        "cases": cases,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[12c] Saved: {args.output_json}")


if __name__ == "__main__":
    main()
